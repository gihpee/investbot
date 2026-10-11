"""Ряды полной доходности (TR) по данным MOEX ISS для бумажного исполнения (порт journal.paper.TotalReturnSource).

- `index` → закрытия индекса (`index_closes`): бенчмарки MCFTR, RGBITR, RUCBTRNS… сами индексы полной доходности.
- Акции и фонды → закрытия основного режима (`share_history`). **Дивиденды в v1 не учитываются**: TR акции
  занижен на дивиденды с отсечкой внутри окна, и сравнение с MCFTR в сезон отсечек смещено против акций.
  Для фондов без выплат (LQDT, TMOS и т. п.) цена и есть TR. Исправление — ProviderChain дивидендов (§15.3).
- Облигации → грязная цена `close% · face / 100 + НКД` плюс накопленные с первой даты ряда денежные потоки
  без реинвестирования:
    • купоны из `bondization` — в дату фиксации реестра (при T+1 это первый день торгов без купона: НКД там
      уже обнулён), если её нет — в дату выплаты; купон с неизвестной суммой обрывает ряд перед собой;
    • амортизация — по падению номинала в самой истории (сколько номинал упал, столько выплачено деньгами):
      так уровень не зависит от того, в какой день ISS меняет FACEVALUE.
  Поэтому отношение уровней внутри одного ряда — доходность удержания от его первой даты; уровни разных
  вызовов (с разным `frm`) не сравнимы. Дни без сделок (CLOSE = null) пропускаются.
- ЦФА, валюта и прочее → пустой ряд (в ISS их нет): такие рекомендации не оцениваются.

Режим торгов — из карточки бумаги (`security`, основной режим): БПИФ переехали в TQBR, корпоративные облигации
в TQCB, ОФЗ в TQOB. Если карточка недоступна — режим по умолчанию для класса.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import structlog

from investbot.adapters.moex_iss.client import IssClient, IssError
from investbot.adapters.moex_iss.models import Bondization

log = structlog.get_logger(__name__)

INDEX = "index"
SHARE_BOARD = "TQBR"
OFZ_BOARD = "TQOB"
CORP_BOARD = "TQCB"
_MARKET_KIND = {"shares": "share", "bonds": "bond"}
_ZERO = Decimal(0)

Levels = list[tuple[date, Decimal]]


def default_bond_board(secid: str) -> str:
    return OFZ_BOARD if secid.upper().startswith("SU") else CORP_BOARD


class IssTotalReturn:
    def __init__(self, iss: IssClient) -> None:
        self._iss = iss
        self._primary_cache: dict[str, tuple[str | None, str | None]] = {}
        self._bondization_cache: dict[str, Bondization] = {}

    async def tr_levels(self, ticker: str, asset_class: str, frm: date, to: date) -> Levels:
        if frm > to:
            return []
        if asset_class == INDEX:
            return sorted(await self._iss.index_closes(ticker, frm, to))
        if asset_class not in ("equity", "fund", "bond"):
            return []
        market, board = await self._primary(ticker)
        kind = _MARKET_KIND.get(market or "") or ("bond" if asset_class == "bond" else "share")
        if kind == "bond":
            return await self._bond(ticker, board or default_bond_board(ticker), frm, to)
        bars = await self._iss.share_history(ticker, frm, to, board or SHARE_BOARD)
        return sorted((b.tradedate, b.close) for b in bars if b.close is not None and b.close > 0)

    async def _primary(self, secid: str) -> tuple[str | None, str | None]:
        """(рынок, основной режим) из карточки; ошибка ISS — режим по умолчанию (не кэшируется)."""
        if secid in self._primary_cache:
            return self._primary_cache[secid]
        try:
            info = await self._iss.security(secid)
        except IssError as e:
            log.warning("iss_tr.security_failed", secid=secid, error=type(e).__name__)
            return None, None
        self._primary_cache[secid] = (info.market, info.boardid)
        return self._primary_cache[secid]

    async def _bondization(self, secid: str) -> Bondization:
        if secid not in self._bondization_cache:
            self._bondization_cache[secid] = await self._iss.bondization(secid)
        return self._bondization_cache[secid]

    async def _bond(self, secid: str, board: str, frm: date, to: date) -> Levels:
        bars = await self._iss.bond_history(secid, frm, to, board)
        rows = sorted(
            ((b.tradedate, b.close, b.facevalue, b.accint) for b in bars
             if b.close is not None and b.close > 0 and b.facevalue is not None and b.facevalue > 0
             and b.accint is not None),
            key=lambda r: r[0],
        )
        if not rows:
            return []
        base, last = rows[0][0], rows[-1][0]
        coupons = sorted(
            ((c.recorddate or c.coupondate, c.value) for c in (await self._bondization(secid)).coupons
             if base < (c.recorddate or c.coupondate) <= last),
            key=lambda f: f[0],
        )
        unknown = next((d for d, v in coupons if v is None), None)
        if unknown is not None:  # купон с неизвестной суммой (флоатер, нет данных) — дальше уровень не определён
            log.warning("iss_tr.coupon_unknown", secid=secid, on=unknown.isoformat())
            rows = [r for r in rows if r[0] < unknown]

        out: Levels = []
        cash, i = _ZERO, 0
        prev_face: Decimal | None = None
        for d, close, face, accint in rows:
            while i < len(coupons) and coupons[i][0] <= d:
                cash += coupons[i][1] or _ZERO
                i += 1
            if prev_face is not None and face < prev_face:
                cash += prev_face - face  # амортизация: погашенная часть номинала выплачена деньгами
            prev_face = face
            out.append((d, close * face / 100 + accint + cash))
        return out
