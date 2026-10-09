"""Нормализация текста и URL новостей (§8.1, шаг «Нормализация»): HTML → текст, пробелы, канонический URL.

Чистые функции без сети: одинаково работают в коллекторах, дедупликации и тестах.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import unquote_plus, urlsplit, urlunsplit

# Невидимые символы, которые ломают дедупликацию: zero-width, BOM, мягкий перенос.
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿­"))
# Теги, после которых в тексте должен быть пробел, иначе слова соседних абзацев склеятся.
_BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt", "figcaption", "figure",
    "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "ol", "p", "pre", "section",
    "table", "td", "th", "tr", "ul",
})
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template"})
_HTML_HINT = re.compile(r"<[a-zA-Z/!]|&[#a-zA-Z0-9]+;")

# Трекинговые параметры: utm_* по префиксу, остальные по имени (регистр не важен).
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_PARAMS = frozenset({"yclid", "ysclid", "fbclid", "gclid", "from", "_openstat"})
_DEFAULT_PORTS = {"http": 80, "https": 443}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def normalize_ws(s: str) -> str:
    """Любые пробельные символы (включая неразрывный) → один пробел; невидимые символы удаляются."""
    return " ".join(s.translate(_INVISIBLE).split())


def strip_html(s: str) -> str:
    """HTML → плоский текст: теги убираются, сущности раскрываются, script/style выбрасываются целиком.

    Текст без признаков разметки возвращается только с нормализованными пробелами: «a < b» не портится.
    """
    if not s:
        return ""
    if not _HTML_HINT.search(s):
        return normalize_ws(s)
    parser = _TextExtractor()
    parser.feed(s)
    parser.close()
    return normalize_ws("".join(parser.parts))


def canonical_url(url: str) -> str:
    """Канонический URL для дедупликации и показа ссылки.

    Убираются фрагмент, трекинговые параметры (utm_*, yclid, ysclid, fbclid, gclid, from, _openstat),
    логин/пароль и порт по умолчанию; хост и схема — в нижнем регистре; путь и порядок прочих параметров
    сохраняются. Относительные и не-URL строки возвращаются как есть (без пробелов по краям).
    """
    raw = url.strip()
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return raw
    host = parts.hostname
    if not parts.scheme or not host:
        return raw
    scheme = parts.scheme.lower()
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        netloc = f"{netloc}:{port}"
    # Пары фильтруются «как есть», без перекодирования: оставшиеся параметры не меняют вид
    kept = [p for p in parts.query.split("&") if p and not _is_tracking(unquote_plus(p.split("=", 1)[0]))]
    return urlunsplit((scheme, netloc, parts.path or "/", "&".join(kept), ""))


def _is_tracking(key: str) -> bool:
    k = key.strip().lower()
    return k.startswith(_TRACKING_PREFIXES) or k in _TRACKING_PARAMS


def clip(s: str, n: int) -> str:
    """Обрезка до n символов включительно; при обрезке последний символ — «…»."""
    if n <= 0:
        return ""
    if len(s) <= n:
        return s
    return s[: n - 1].rstrip() + "…"
