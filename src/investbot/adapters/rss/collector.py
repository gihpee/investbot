"""RSS/Atom-коллектор новостей (ARCHITECTURE §8.1–8.2, docs/research/03 п. 12.1).

- HTTP делает httpx (проверка TLS по bundle с Russian Trusted Root CA), feedparser только разбирает байты:
  сам он сеть не трогает (байты передаются через BytesIO, а не строкой-URL).
- Условный GET: ETag/Last-Modified запоминаются по ключу источника в памяти процесса. После рестарта первый
  запрос безусловный — дубли отсекает дедупликация архива.
- Кодировка: `SourceConfig.encoding` (Финмаркет — windows-1251) принудительно; иначе её определяет feedparser
  по заголовку Content-Type, XML-прологу и BOM.
- `update_health` — чистая функция детекторов «поломка» (пустые ответы подряд) и «пробитие» (все элементы новые).
"""

from __future__ import annotations

import asyncio
import calendar
import hashlib
import io
import re
import ssl
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self
from urllib.parse import urljoin, urlsplit

import feedparser
import httpx

from investbot.news.models import RawNewsItem, SourceHealth
from investbot.news.sources import SourceConfig
from investbot.news.text import canonical_url, clip, normalize_ws, strip_html

BACKOFF = (1.0, 2.0)  # паузы между тремя попытками
LEAD_MAX = 1000  # RawNewsItem.lead: ≤ 1000 символов
TITLE_MAX = 500
ERROR_MAX = 500
OVERRUN_MIN_ITEMS = 10  # «пробитие» засчитывается только на ответе хотя бы из 10 элементов
USER_AGENT = "investbot/0.1 (+personal use)"
ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.5"

_XML_DECLARATION = re.compile(r"^\s*<\?xml[^>]*\?>")
# bozo-исключения, при которых feedparser всё равно разобрал документ: кодировка угадана не с первого раза или
# сервер отдал ленту как text/html. Пустая лента с такими флагами — «пусто», а не «поломка формата».
_BENIGN_BOZO = (feedparser.CharacterEncodingOverride, feedparser.NonXMLContentType)


class CollectorError(RuntimeError):
    """HTTP-ошибка, исчерпанные повторы или ответ, который не разбирается как RSS/Atom."""

    def __init__(self, message: str, *, source: str, status: int | None = None) -> None:
        super().__init__(message)
        self.source = source
        self.status = status


@dataclass(frozen=True, slots=True)
class FetchResult:
    items: list[RawNewsItem] = field(default_factory=list)
    not_modified: bool = False  # 304 на условный GET: лента не менялась с прошлого непустого ответа
    status: int = 200


@dataclass(frozen=True, slots=True)
class _Validators:
    etag: str | None
    last_modified: str | None


def _ssl(verify: str | bool) -> ssl.SSLContext | bool:
    """Строка — путь к bundle корней (в образе с Russian Trusted Root CA). verify=False запрещён линтером (§5.7)."""
    if isinstance(verify, bool):
        return verify
    if Path(verify).is_dir():
        return ssl.create_default_context(capath=verify)
    return ssl.create_default_context(cafile=verify)


def _retryable(status: int) -> bool:
    return status == 429 or status >= 500


def _utc(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now должен быть tz-aware")
    return now.astimezone(UTC)


class RssCollector:
    """Опрос RSS/Atom-лент из config/sources.yaml (kind: rss). Один экземпляр на процесс: в нём кеш валидаторов."""

    def __init__(
        self,
        *,
        verify: str | bool = True,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            verify=_ssl(verify),
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": ACCEPT},
        )
        self._validators: dict[str, _Validators] = {}

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def fetch(self, source: SourceConfig, now: datetime) -> list[RawNewsItem]:
        """Элементы ленты после фильтра категорий; 304 → []. Ошибки — CollectorError."""
        return (await self.poll(source, now)).items

    async def poll(self, source: SourceConfig, now: datetime) -> FetchResult:
        """Как `fetch`, но отличает 304 от пустой ленты — это нужно `update_health(..., not_modified=...)`."""
        if source.kind != "rss" or source.url is None:
            raise ValueError(f"источник {source.key} ({source.kind}) не RSS")
        now = _utc(now)
        resp = await self._get(source, source.url)
        if resp.status_code == 304:
            return FetchResult(not_modified=True, status=304)
        if not resp.is_success:
            raise CollectorError(f"{source.key}: HTTP {resp.status_code}", source=source.key, status=resp.status_code)
        items = await asyncio.to_thread(
            parse_feed, source, resp.content,
            content_type=resp.headers.get("content-type", ""), base_url=str(resp.url), now=now,
        )
        if items:
            self._validators[source.key] = _Validators(resp.headers.get("etag"), resp.headers.get("last-modified"))
        else:
            # Валидаторы пустого ответа не храним: иначе 304 спрятал бы «поломку» ленты
            self._validators.pop(source.key, None)
        return FetchResult(items=items, status=resp.status_code)

    def _conditional_headers(self, key: str) -> dict[str, str]:
        v = self._validators.get(key)
        headers: dict[str, str] = {}
        if v is not None and v.etag:
            headers["If-None-Match"] = v.etag
        if v is not None and v.last_modified:
            headers["If-Modified-Since"] = v.last_modified
        return headers

    async def _get(self, source: SourceConfig, url: str) -> httpx.Response:
        """GET с условными заголовками; повтор на сетевых ошибках, 429 и 5xx (3 попытки, паузы 1 и 2 с)."""
        headers = self._conditional_headers(source.key)
        for delay in (*BACKOFF, None):
            try:
                resp = await self._http.get(url, headers=headers)
            except httpx.TransportError as exc:
                if delay is None:
                    raise CollectorError(f"{source.key}: недоступен: {exc!r}", source=source.key) from exc
            except httpx.HTTPError as exc:  # TooManyRedirects, DecodingError и т. п. — повтор не поможет
                raise CollectorError(f"{source.key}: {exc!r}", source=source.key) from exc
            else:
                if delay is None or not _retryable(resp.status_code):
                    return resp
            await self._sleep(delay)
        raise AssertionError("unreachable")


# --- Разбор ленты (чистые функции, без сети) ---------------------------------------------------------------


def parse_feed(
    source: SourceConfig,
    content: bytes,
    *,
    content_type: str = "",
    base_url: str | None = None,
    now: datetime,
) -> list[RawNewsItem]:
    """Байты RSS/Atom → RawNewsItem: дубли external_id внутри ответа отброшены, фильтр категорий применён.

    CollectorError — документ не распознан как лента и элементов нет (HTML-заглушка, обрыв до первого элемента).
    """
    now = _utc(now)
    base = base_url or source.url
    data, headers = _prepare(content, source.encoding, content_type, base)
    feed = feedparser.parse(io.BytesIO(data), response_headers=headers)
    entries = feed.get("entries") or []
    if not entries:
        exc = feed.get("bozo_exception")
        if not feed.get("version") or (feed.get("bozo") and not isinstance(exc, _BENIGN_BOZO)):
            raise CollectorError(f"{source.key}: ответ не разбирается как RSS/Atom: {exc!r}", source=source.key)
    allowed = {_category_key(c) for c in source.category_filter}
    seen: set[str] = set()
    out: list[RawNewsItem] = []
    for entry in entries:
        item = map_entry(entry, source=source.key, base_url=base, now=now)
        if item is None or item.external_id in seen:
            continue
        seen.add(item.external_id)
        if allowed and not any(_category_key(c) in allowed for c in item.categories):
            continue
        out.append(item)
    return out


def _prepare(
    content: bytes, encoding: str | None, content_type: str, base_url: str | None
) -> tuple[bytes, dict[str, str]]:
    """Байты и заголовки для feedparser. С принудительной кодировкой документ перекодируется в UTF-8."""
    headers: dict[str, str] = {"content-location": base_url} if base_url else {}
    if encoding:
        text = content.decode(encoding, errors="replace").lstrip("﻿")
        text = _XML_DECLARATION.sub("", text, count=1)  # пролог с исходной кодировкой больше не верен
        headers["content-type"] = "application/xml; charset=utf-8"
        return text.encode("utf-8"), headers
    # Без Content-Type feedparser считал бы документ iso-8859-1; application/xml → пролог, BOM, затем utf-8
    headers["content-type"] = content_type or "application/xml"
    return content, headers


def map_entry(entry: Mapping[str, Any], *, source: str, base_url: str | None, now: datetime) -> RawNewsItem | None:
    """Элемент feedparser → RawNewsItem; None — ни заголовка, ни анонса."""
    lead = clip(strip_html(_summary(entry)), LEAD_MAX)
    title = clip(strip_html(str(entry.get("title") or "")), TITLE_MAX) or clip(lead, TITLE_MAX)
    if not title:
        return None
    url = _link(entry, base_url)
    raw_id = normalize_ws(str(entry.get("id") or ""))
    if raw_id.startswith(("http://", "https://")):
        raw_id = canonical_url(raw_id)
    external_id = raw_id or url or "sha1:" + hashlib.sha1(title.encode(), usedforsecurity=False).hexdigest()
    return RawNewsItem(
        source=source,
        external_id=external_id,
        url=url,
        title=title,
        lead=lead,
        published_at=_published(entry),
        fetched_at=_utc(now),
        categories=_categories(entry),
    )


def _summary(entry: Mapping[str, Any]) -> str:
    """summary/description; если их нет (Atom только с content) — первый content."""
    text = entry.get("summary") or entry.get("description")
    if text:
        return str(text)
    for c in entry.get("content") or ():
        value = c.get("value") if isinstance(c, Mapping) else None
        if value:
            return str(value)
    return ""


def _link(entry: Mapping[str, Any], base_url: str | None) -> str | None:
    """Абсолютная http(s)-ссылка в каноническом виде; javascript:, mailto: и прочее — None (ссылку видит человек)."""
    href = str(entry.get("link") or "").strip()
    if not href:
        return None
    if base_url:
        href = urljoin(base_url, href)
    if urlsplit(href).scheme.lower() not in ("http", "https"):
        return None
    return canonical_url(href)


def _published(entry: Mapping[str, Any]) -> datetime | None:
    """published_parsed / updated_parsed — struct_time, который feedparser уже привёл к UTC."""
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            try:
                return datetime.fromtimestamp(calendar.timegm(st), UTC)
            except (OverflowError, ValueError, TypeError, OSError):
                continue
    return None


def _categories(entry: Mapping[str, Any]) -> tuple[str, ...]:
    out: list[str] = []
    for tag in entry.get("tags") or ():
        term = normalize_ws(str(tag.get("term") or tag.get("label") or "")) if isinstance(tag, Mapping) else ""
        if term and term not in out:
            out.append(term)
    return tuple(out)


def _category_key(c: str) -> str:
    return normalize_ws(c).casefold()


# --- Здоровье источника (§8.2 п. 1) ------------------------------------------------------------------------


def update_health(
    prev: SourceHealth,
    items: list[RawNewsItem],
    known_ids: set[str],
    now: datetime,
    error: str | None = None,
    *,
    not_modified: bool = False,
) -> SourceHealth:
    """Новое состояние source_health после опроса.

    - error → last_error_at/last_error, consecutive_errors + 1; счётчик пустых ответов не трогается.
    - Успех → last_ok_at, consecutive_errors = 0, last_new_items = число новых external_id (нет в known_ids).
    - «Поломка»: пустой ответ (после фильтра категорий — так видно и переименование рубрики) → consecutive_empty + 1,
      непустой → 0. 304 (not_modified) — успешный ответ, который счётчик не меняет: валидаторы хранятся только
      для непустых ответов, поэтому 304 означает «лента та же и в ней есть элементы».
    - «Пробитие»: все элементы ответа новые и их ≥ OVERRUN_MIN_ITEMS. На самом первом успешном опросе
      (last_ok_at ещё нет) не засчитывается: сравнивать не с чем.
    """
    now = _utc(now)
    if error is not None:
        return prev.model_copy(update={
            "last_error_at": now,
            "last_error": clip(normalize_ws(error), ERROR_MAX) or "error",
            "consecutive_errors": prev.consecutive_errors + 1,
            "last_new_items": 0,
            "overrun": False,
        })
    ids = {it.external_id for it in items}
    new = len(ids - known_ids)
    empty_streak = prev.consecutive_empty  # 304: лента та же, счётчик не меняется
    if not not_modified:
        empty_streak = 0 if items else empty_streak + 1
    overrun = prev.last_ok_at is not None and len(items) >= OVERRUN_MIN_ITEMS and new == len(ids)
    return prev.model_copy(update={
        "last_ok_at": now,
        "consecutive_errors": 0,
        "consecutive_empty": empty_streak,
        "last_new_items": new,
        "overrun": overrun,
    })
