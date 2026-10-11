"""Проверка сети с сервера (Ф0, ADR-04): какие внешние сервисы доступны отсюда напрямую и через прокси.

Ответ на вопрос «подходит ли этот VPS»: Т-Инвестиции и MOEX из-за рубежа доступны не всегда (зависит от сети
хостера), Telegram из РФ может быть ограничен. Проверка делает настоящие запросы, а не только TCP/TLS.
"""

from __future__ import annotations

import asyncio
import ssl
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import certifi
import httpx

from investbot.news.sources import SourceConfig


@dataclass
class NetResult:
    group: str  # broker | market | telegram | llm | news | sdk
    name: str
    ok: bool
    detail: str
    ms: int | None = None
    reachable: bool | None = None  # сеть до сервера есть (даже если не проверился сертификат)

    def __post_init__(self) -> None:
        if self.reachable is None:
            self.reachable = self.ok


def _ctx(cafile: Path | None) -> ssl.SSLContext:
    if cafile and cafile.exists():
        try:
            return ssl.create_default_context(cafile=str(cafile))
        except ssl.SSLError:
            pass  # пустой или битый bundle: о нём сообщает проверка конфигурации, сеть проверяем с системными корнями
    return ssl.create_default_context(cafile=certifi.where())


async def _probe(group: str, name: str, url: str, ctx: ssl.SSLContext, *, proxy: str | None = None,
                 headers: dict[str, str] | None = None, expect_json: bool = False, ok_statuses: Sequence[int] = (),
                 limit_s: float = 12.0) -> NetResult:
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(verify=ctx, proxy=proxy, timeout=limit_s, follow_redirects=False,
                                     trust_env=False) as client:
            r = await client.get(url, headers=headers or {})
    except httpx.ConnectTimeout:
        return NetResult(group, name, False, "таймаут соединения — скорее всего, заблокировано для этой сети")
    except httpx.ReadTimeout:
        return NetResult(group, name, False, "соединение есть, но ответа нет (таймаут)")
    except httpx.ConnectError as e:
        msg = str(e)
        if "CERTIFICATE_VERIFY_FAILED" in msg or "certificate" in msg.lower():
            # TLS-рукопожатие дошло до сервера: сеть есть, не хватает корневого сертификата Минцифры
            return NetResult(group, name, False,
                             "сервер доступен, но нужен сертификат Минцифры (docs/DEPLOY.md, шаг 4)", reachable=True)
        return NetResult(group, name, False, f"нет соединения ({type(e).__name__})")
    except httpx.HTTPError as e:
        return NetResult(group, name, False, f"ошибка ({type(e).__name__})")
    ms = int((time.monotonic() - t0) * 1000)
    ctype = r.headers.get("content-type", "")
    if expect_json and r.status_code == 200 and "json" not in ctype:
        return NetResult(group, name, False, f"200, но не JSON ({ctype or 'без типа'}) — заглушка или гео-блок", ms)
    ok = r.status_code < 500 if not ok_statuses else r.status_code in ok_statuses
    return NetResult(group, name, ok, f"HTTP {r.status_code}", ms)


async def run_netcheck(
    sources: Sequence[SourceConfig],
    cafile: Path | None,
    telegram_proxy: str | None = None,
    llm_base_url: str | None = None,
    llm_api_key: str | None = None,
    llm_proxy: str | None = None,
) -> list[NetResult]:
    ctx = _ctx(cafile)
    checks = [
        _probe("broker", "Т-Инвестиции API (invest-public-api.tbank.ru)", "https://invest-public-api.tbank.ru/", ctx),
        _probe("sdk", "Индекс SDK Т-Банка (opensource.tbank.ru)",
               "https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple/t-tech-investments/", ctx),
        _probe("market", "MOEX ISS (данные)", "https://iss.moex.com/iss/engines.json?iss.meta=off", ctx,
               expect_json=True, ok_statuses=(200,)),
        _probe("market", "ЦБ РФ (cbr.ru)", "https://www.cbr.ru/scripts/XML_daily.asp", ctx, ok_statuses=(200,)),
        _probe("telegram", "Telegram Bot API напрямую", "https://api.telegram.org/", ctx),
    ]
    if telegram_proxy:
        checks.append(_probe("telegram", "Telegram Bot API через прокси", "https://api.telegram.org/", ctx,
                             proxy=telegram_proxy))
    if llm_base_url:
        auth = {"Authorization": f"Bearer {llm_api_key}"} if llm_api_key else {}
        url = llm_base_url.rstrip("/") + "/models"
        checks.append(_probe("llm", "LLM-провайдер напрямую", url, ctx, headers=auth, ok_statuses=(200,)))
        if llm_proxy:
            checks.append(_probe("llm", "LLM-провайдер через прокси", url, ctx, headers=auth, proxy=llm_proxy,
                                 ok_statuses=(200,)))
    for src in sources:
        if src.enabled and src.url and src.kind == "rss":
            checks.append(_probe("news", f"RSS {src.key}", src.url, ctx, ok_statuses=(200, 304), limit_s=10))
    return list(await asyncio.gather(*checks))


def verdict(results: Sequence[NetResult]) -> list[str]:
    """Вывод для выбора топологии (ADR-04)."""
    by = {r.name: r for r in results}

    def reach(prefix: str) -> bool:
        return any(r.reachable for r in results if r.name.startswith(prefix))

    broker, iss = reach("Т-Инвестиции"), by.get("MOEX ISS (данные)", NetResult("", "", False, "")).ok
    tg = reach("Telegram")
    need_ca = any(r.reachable and not r.ok for r in results)
    llm_results = [r for r in results if r.group == "llm"]
    llm = any(r.ok for r in llm_results) if llm_results else None
    news = [r for r in results if r.group == "news"]
    news_ok = sum(r.ok for r in news)
    out: list[str] = []
    if broker and iss and tg and llm is not False:
        out.append("✅ Этот сервер подходит для размещения бота целиком: брокер, MOEX и Telegram доступны.")
    else:
        if not broker:
            out.append("❌ Т-Инвестиции недоступны отсюда — без них нет портфеля из API. Нужен сервер в РФ "
                       "(или другой хостинг/страна, проверьте ещё раз).")
        if not iss:
            out.append("❌ MOEX ISS недоступен — не будет истории, облигаций для идей и кривой ОФЗ. Нужен сервер в РФ "
                       "или другая сеть.")
        if not tg:
            out.append("❌ Telegram напрямую недоступен — нужен прокси INVESTBOT_TELEGRAM_PROXY (socks5:// или "
                       "http://) через небольшой зарубежный сервер (см. docs/DEPLOY.md).")
        if llm is False:
            out.append("❌ LLM-провайдер недоступен — задайте INVESTBOT_LLM_PROXY или проверьте URL и ключ.")
    if need_ca:
        out.append("ℹ️ Часть сервисов отвечает, но нужен сертификат Минцифры в CA bundle (docs/DEPLOY.md, шаг 4).")
    if news:
        out.append(f"ℹ️ Новостные ленты: доступно {news_ok} из {len(news)}.")
    return out
