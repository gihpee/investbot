"""Асинхронный клиент MOEX ISS: история, облигации, КБД, карточка бумаги (docs/research/02, п. 1)."""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import Awaitable, Callable, Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Self

import httpx
from pydantic import ValidationError

from investbot.adapters.moex_iss.models import (
    Amortization,
    BondDailyBar,
    Bondization,
    BondMarketRow,
    Coupon,
    CurvePoint,
    DailyBar,
    IndexClose,
    IssModel,
    Offer,
    SecurityInfo,
)

ISS_URL = "https://iss.moex.com/iss"
BACKOFF = (0.5, 1.0)  # паузы между тремя попытками
BOND_PAGE = 100
MAX_PAGES = 1000  # страховка от зацикливания пагинации

Params = Mapping[str, str | int]
Row = dict[str, Any]


class IssError(RuntimeError):
    """HTTP 4xx, исчерпанные повторы или неожиданный формат ответа."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class IssSubscriptionRequired(IssError):
    """ISS ответил 200 с HTML «доступно только подписчикам» вместо JSON."""


def parse_block(payload: Mapping[str, Any], name: str) -> list[Row]:
    """Блок ISS {"columns": [...], "data": [[...], ...]} → список словарей."""
    try:
        block = payload[name]
        columns = block["columns"]
        return [dict(zip(columns, row, strict=True)) for row in block["data"]]
    except (KeyError, TypeError, ValueError) as exc:
        raise IssError(f"в ответе ISS нет блока {name!r} или он повреждён") from exc


def _load[M: IssModel](model: type[M], rows: list[Row], rename: Mapping[str, str] | None = None) -> list[M]:
    fields = model.model_fields
    out: list[M] = []
    for row in rows:
        data = {}
        for col, v in row.items():
            key = col.lower()
            key = rename.get(key, key) if rename else key
            if key in fields:
                data[key] = v
        try:
            out.append(model.model_validate(data))
        except ValidationError as exc:
            raise IssError(f"неожиданный формат ISS для {model.__name__}: {exc}") from exc
    return out


def _by_secid(rows: list[Row]) -> dict[str, Row]:
    return {str(r["SECID"]): r for r in rows if r.get("SECID")}


def _nz(v: Any) -> Any:
    """Ноль и пустое значение ISS → None (для полей, где ноль означает «не рассчитано»)."""
    return None if v in (0, "", None) else v


def _bond_row(board: str, sec: Row, md: Row, yl: Row) -> BondMarketRow:
    effective = _nz(yl.get("EFFECTIVEYIELD"))
    spreads_known = effective is not None  # при нулевой доходности спреды ISS — тоже ночные нули
    return BondMarketRow(
        secid=str(sec["SECID"]),
        board=str(sec.get("BOARDID") or board),
        shortname=sec.get("SHORTNAME"),
        isin=sec.get("ISIN"),
        face_value=sec.get("FACEVALUE"),
        face_unit=sec.get("FACEUNIT"),
        coupon_percent=sec.get("COUPONPERCENT"),
        coupon_value=sec.get("COUPONVALUE"),
        next_coupon=sec.get("NEXTCOUPON"),
        accrued_int=sec.get("ACCRUEDINT"),
        mat_date=sec.get("MATDATE"),
        offer_date=sec.get("OFFERDATE"),
        buyback_date=sec.get("BUYBACKDATE"),
        call_option_date=sec.get("CALLOPTIONDATE"),
        put_option_date=sec.get("PUTOPTIONDATE"),
        lot_size=sec.get("LOTSIZE"),
        list_level=sec.get("LISTLEVEL"),
        sec_type=sec.get("SECTYPE"),
        price_pct=_nz(yl.get("PRICE")) or _nz(md.get("LAST")) or _nz(sec.get("PREVPRICE")),
        effective_yield=effective,
        yield_to_offer=_nz(yl.get("YIELDTOOFFER")) or _nz(md.get("YIELDTOOFFER")),
        yield_date=yl.get("YIELDDATE"),
        duration_days=_nz(yl.get("DURATION")) or _nz(md.get("DURATION")),
        gspread_bp=yl.get("GSPREADBP") if spreads_known else None,
        zspread_bp=yl.get("ZSPREADBP") if spreads_known else None,
        val_today=md.get("VALTODAY"),
        num_trades=md.get("NUMTRADES"),
        trading_status=md.get("TRADINGSTATUS"),
    )


def _ssl(verify: str | bool) -> ssl.SSLContext | bool:
    """Строка — путь к bundle корней (в образе с Russian Trusted Root CA)."""
    if isinstance(verify, bool):
        return verify
    if Path(verify).is_dir():
        return ssl.create_default_context(capath=verify)
    return ssl.create_default_context(cafile=verify)


def _retryable(status: int) -> bool:
    return status == 429 or status >= 500


def _decode(path: str, resp: httpx.Response) -> dict[str, Any]:
    if not resp.is_success:
        raise IssError(f"ISS {resp.status_code} на {path}", status=resp.status_code)
    if "html" in resp.headers.get("content-type", ""):
        raise IssSubscriptionRequired(f"ISS: данные только для подписчиков ({path})", status=resp.status_code)
    try:
        payload = resp.json(parse_float=Decimal)  # без float: цены и доходности сразу в Decimal
    except ValueError as exc:
        raise IssSubscriptionRequired(f"ISS: ответ не JSON ({path})", status=resp.status_code) from exc
    if not isinstance(payload, dict):
        raise IssError(f"ISS: неожиданный JSON на {path}")
    return payload


class IssClient:
    """Тонкий клиент ISS. Нули в данных не трогает: 0 → None — задача DQ-гейтов."""

    def __init__(
        self,
        base_url: str = ISS_URL,
        *,
        verify: str | bool = True,
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            base_url=base_url,
            verify=_ssl(verify),
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
            headers={"User-Agent": "investbot/0.1"},
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def index_history(self, secid: str, frm: date, to: date) -> list[IndexClose]:
        """Дневные закрытия индекса (MCFTR, RGBITR, RUCBTRNS, ...)."""
        path = f"/history/engines/stock/markets/index/securities/{secid}.json"
        return _load(IndexClose, await self._history(path, frm, to))

    async def index_closes(self, secid: str, frm: date, to: date) -> list[tuple[date, Decimal]]:
        """Порт core.ports.IndexHistory: дни без CLOSE пропускаются."""
        bars = await self.index_history(secid, frm, to)
        return [(b.tradedate, b.close) for b in bars if b.close is not None]

    async def share_history(self, secid: str, frm: date, to: date, board: str = "TQBR") -> list[DailyBar]:
        path = f"/history/engines/stock/markets/shares/boards/{board}/securities/{secid}.json"
        return _load(DailyBar, await self._history(path, frm, to))

    async def bond_history(self, secid: str, frm: date, to: date, board: str = "TQOB") -> list[BondDailyBar]:
        path = f"/history/engines/stock/markets/bonds/boards/{board}/securities/{secid}.json"
        return _load(BondDailyBar, await self._history(path, frm, to), rename={"duration": "duration_days"})

    async def bondization(self, secid: str) -> Bondization:
        """Купоны, амортизации, оферты. Курсора нет (по умолчанию 20 строк) — листаем до пустой страницы."""
        path = f"/securities/{secid}/bondization.json"
        acc: dict[str, list[Row]] = {"coupons": [], "amortizations": [], "offers": []}
        start = 0
        for _ in range(MAX_PAGES):
            js = await self._get(path, {"start": start, "limit": BOND_PAGE})
            page = {name: parse_block(js, name) for name in acc}
            step = max(len(rows) for rows in page.values())
            if step == 0:
                return Bondization(
                    secid=secid,
                    coupons=tuple(_load(Coupon, acc["coupons"])),
                    amortizations=tuple(_load(Amortization, acc["amortizations"])),
                    offers=tuple(_load(Offer, acc["offers"])),
                )
            for name, rows in page.items():
                acc[name] += rows
            start += step  # шаг по фактическому размеру страницы: сервер может урезать limit
        raise IssError(f"ISS: пагинация {path} не завершилась за {MAX_PAGES} страниц")

    async def zcyc(self, on: date | None = None) -> list[CurvePoint]:
        """КБД: готовые доходности по срокам из блока yearyields."""
        params: dict[str, str | int] = {"iss.only": "yearyields"}
        if on is not None:
            params["date"] = on.isoformat()
        js = await self._get("/engines/stock/zcyc.json", params)
        return _load(CurvePoint, parse_block(js, "yearyields"))

    async def bonds_market(self, board: str) -> list[BondMarketRow]:
        """Все облигации режима (TQOB, TQCB, …) одним запросом: справочник, котировки и доходности по SECID.

        Исключение из правила «нули не трогаем»: в EFFECTIVEYIELD/DURATION/YIELDTOOFFER/PRICE ноль у ISS
        означает «не рассчитано» (ночь, выходные, флоатеры), поэтому BondMarketRow превращает его в None.
        """
        path = f"/engines/stock/markets/bonds/boards/{board}/securities.json"
        js = await self._get(path, {"iss.only": "securities,marketdata,marketdata_yields"})
        market = _by_secid(parse_block(js, "marketdata"))
        yields = _by_secid(parse_block(js, "marketdata_yields"))
        out: list[BondMarketRow] = []
        for sec in parse_block(js, "securities"):
            secid = sec.get("SECID")
            if not secid:
                raise IssError(f"ISS: строка без SECID в {path}")
            try:
                out.append(_bond_row(board, sec, market.get(secid, {}), yields.get(secid, {})))
            except ValidationError as exc:
                raise IssError(f"неожиданный формат ISS для облигации {secid}: {exc}") from exc
        return out

    async def security(self, secid: str) -> SecurityInfo:
        """Карточка бумаги и основной режим торгов (режим не хардкодим: БПИФ переехали в TQBR)."""
        js = await self._get(f"/securities/{secid}.json", {"iss.only": "description,boards"})
        desc = parse_block(js, "description")
        if not desc:
            raise IssError(f"ISS: бумага {secid} не найдена")
        primary = next((b for b in parse_block(js, "boards") if b.get("is_primary") == 1), {})
        try:
            return SecurityInfo(
                secid=secid,
                description={r["name"]: None if r["value"] is None else str(r["value"]) for r in desc},
                boardid=primary.get("boardid"),
                market=primary.get("market"),
                engine=primary.get("engine"),
            )
        except (KeyError, ValidationError) as exc:
            raise IssError(f"неожиданный формат карточки {secid}: {exc}") from exc

    async def _history(self, path: str, frm: date, to: date) -> list[Row]:
        """История листается по блоку history.cursor (INDEX, TOTAL, PAGESIZE)."""
        params = {"from": frm.isoformat(), "till": to.isoformat(), "iss.only": "history,history.cursor"}
        rows: list[Row] = []
        start = 0
        for _ in range(MAX_PAGES):
            js = await self._get(path, {**params, "start": start})
            page = parse_block(js, "history")
            rows += page
            cursor = parse_block(js, "history.cursor") if "history.cursor" in js else []
            if not page or not cursor:
                return rows
            start = int(cursor[0]["INDEX"]) + int(cursor[0]["PAGESIZE"])
            if start >= int(cursor[0]["TOTAL"]):
                return rows
        raise IssError(f"ISS: пагинация {path} не завершилась за {MAX_PAGES} страниц")

    async def _get(self, path: str, params: Params) -> dict[str, Any]:
        """GET с iss.meta=off; повтор на сетевых ошибках, 429 и 5xx с паузами 0.5 и 1 с."""
        query = {"iss.meta": "off", **params}
        for delay in (*BACKOFF, None):
            try:
                resp = await self._http.get(path, params=query)
            except httpx.TransportError as exc:
                if delay is None:
                    raise IssError(f"ISS недоступен ({path}): {exc!r}") from exc
            else:
                if delay is None or not _retryable(resp.status_code):
                    return _decode(path, resp)
            await self._sleep(delay)
        raise AssertionError("unreachable")
