"""Связывание новостей с бумагами по точным алиасам (§8.2 п. 4, §8.3 Ф2a): тикер, ISIN, название.

Чистый модуль без LLM, pymorphy3 и rapidfuzz. Цель Ф2a — точность, а не полнота: лучше не связать, чем связать
не с той бумагой. Неоднозначные названия (омонимы) до Ф3 не связываются вовсе.

Правила сопоставления:
- **Тикер** — целый токен, только капсом («SBER», но не «SBERBANK» и не «sber»), длина ≥ 3, есть буква,
  не из `TICKER_STOPLIST` (английские слова и финансовые аббревиатуры, которые пишут капсом).
- **ISIN** — целый токен, без учёта регистра.
- **Название** из справочника (только акции и облигации): без орг.-правовых форм (ПАО, АО, МКПАО…), признаков
  класса («ао», «ап», «-п», «привилегированные акции»), обобщающих слов по краям («Группа», «России», «Холдинг»),
  у облигаций — без номера выпуска («001Р-02R»). Фраза ищется подряд по токенам.
  * Падежи: основа (слово без окончания −ий/−ая/… или одной гласной/ь/й) + до 3 кириллических букв
    («Газпрома», «Сбербанку», «Норильского никеля»). Основа первого слова — ≥ 5 букв, следующих слов
    фразы — ≥ 3 («Газпром нефти»); короче — только точное совпадение.
  * Регистр: первое слово в тексте — с заглавной буквы; короткое (< 5 букв) капсовое название («ВТБ», «ПИК»,
    «МТС») — только капсом. Так «пик продаж» не даёт ПИК, а «интерес» — Интер РАО.
  * Перекрытия: из вложенных совпадений остаётся самое длинное («Газпром нефти» → SIBN, но не GAZP).
  * Головное слово многословного названия («Аэрофлот» из «Аэрофлот — Российские авиалинии») добавляется
    отдельным алиасом, только если оно кириллическое, ≥ 6 букв, не прилагательное, не обобщающее, хвост названия
    не обобщающий («Газпром нефть», «Сбербанк Лизинг», «Россети Центр» — другая компания группы) и голова не
    совпадает с названием или головой другой бумаги индекса. Проверка уникальности надёжна, только если индекс
    строится по всему справочнику, а не по одному портфелю. «Сбербанк России» → «Сбербанк» даёт срез
    обобщающего края («России»), а не голова.
  * Фонды, валюта, ЦФА — без алиасов по названию («Золото», «Денежный рынок» дают одни ложные связи);
    для них — тикер, ISIN, связи API или ручные алиасы.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from investbot.core.models import AssetClass, InstrumentMeta
from investbot.news.models import LinkMethod, NewsLink, RawNewsItem

CONFIDENCE: dict[LinkMethod, Decimal] = {
    "api": Decimal(1),
    "alias_isin": Decimal("0.9"),
    "alias_ticker": Decimal("0.9"),
    "alias_name": Decimal("0.7"),
}
_RANK: dict[LinkMethod, int] = {"api": 3, "alias_isin": 2, "alias_ticker": 1, "alias_name": 0}

# Названия-омонимы: до Ф3 по ним не связываем (тикер и ISIN работают). Только настоящие омонимы, где
# нарицательное слово или другое значение встречается в деловых новостях заметно чаще, чем с заглавной буквы
# в начале предложения. «Аэрофлот» сюда не входит: это имя собственное, в новостях почти всегда о компании
# (группа «Аэрофлот» и есть эмитент AFLT). «Мечел», «Газпром» и т. п. — не слова языка.
DEFAULT_AMBIGUOUS: frozenset[str] = frozenset({
    "Магнит",  # магнит, «Магнитка» (ММК)
    "Полюс",  # полюс роста, Северный полюс
    "Система",  # система (АФК «Система» как фраза — однозначна и связывается)
    "Самолет",  # самолёт
    "Лента",  # «лента новостей»
    "Эталон",  # эталон, эталонный
    "Позитив",  # позитив на рынке
    "Positive",
    "Белуга",  # рыба; Белуга Групп переименована в Новабев
    "Ренессанс",  # Ренессанс Страхование vs Ренессанс Капитал, Ренессанс Кредит
    "Мосбиржа",  # площадка торгов, а не эмитент MOEX: «акции на Мосбирже выросли»
    "Московская биржа",
})

# Тикеры, совпадающие с английскими словами и аббревиатурами, которые пишут капсом. MOEX — префикс площадки
# («MOEX:SBER», «индекс MOEX»), а не эмитент. Кириллица — на случай ручных тикеров.
TICKER_STOPLIST: frozenset[str] = frozenset({
    "ALL", "AND", "ARE", "BEST", "BIG", "BOND", "BUY", "CASH", "DATA", "FIVE", "FOR", "FREE", "GOLD", "GOOD",
    "HEAD", "HOLD", "LIFE", "LONG", "MAIL", "MORE", "NEW", "NEWS", "NOT", "NOW", "ONE", "OPEN", "PLUS", "PRO",
    "RUN", "SELL", "SHORT", "THE", "TOP", "TWO", "WIN", "YES", "ZERO",
    "API", "CEO", "CFO", "CNY", "CPI", "EBITDA", "EPS", "ESG", "ETF", "EUR", "FCF", "FED", "GDP", "IFRS", "IMOEX",
    "IPO", "LSE", "MOEX", "MSCI", "NYSE", "OFZ", "OPEC", "OTC", "PMI", "RGBI", "RTS", "RUB", "SPB", "SPO", "USD",
    "ВВП", "ИИС", "МСФО", "НДС", "ОФЗ", "РСБУ", "США", "ЦФА",
})

_LEGAL_FORMS = frozenset({
    "пао", "ао", "оао", "зао", "нао", "ооо", "мкпао", "мкао", "сз", "гк", "нк", "гмк", "лк", "ук", "ик", "акб", "кб",
    "мфк",
    "pjsc", "jsc", "ojsc", "plc", "ltd", "llc", "inc", "corp", "ag", "nv",
})
_CLASS_WORDS = frozenset({
    "ап", "прив", "привилегированные", "привилегированная", "обыкновенные", "обыкновенная", "акции", "акция",
    "обл", "облигации", "облигация", "бо", "биржевые", "выпуск", "серия", "гдр", "gdr", "adr", "др",
    "депозитарные", "расписки", "pref", "ord",
})
_GENERIC_EDGE = frozenset({
    "группа", "групп", "груп", "компаний", "компания", "холдинг", "корпорация", "россии", "россия", "ру", "рф",
    "специализированный", "застройщик", "group", "holding", "holdings", "company", "corporation", "ru", "com",
})
# Сами по себе не алиас и не голова многословного названия.
_GENERIC_WORDS = _GENERIC_EDGE | frozenset({
    "банк", "нефть", "газ", "энерго", "капитал", "финанс", "финансы", "инвест", "инвестиции", "фонд", "биржа",
    "рынок", "денежный", "золото", "серебро", "индекс", "офз", "первая", "первый", "новая", "новый", "сеть",
    "торговый", "дом", "ресурс", "ресурсы", "технологии", "лизинг", "девелопмент", "страхование", "энергия",
    "сбыт", "сети", "мир", "бизнес", "трейд", "интер", "транс", "агро", "центр", "регион", "юг", "север",
    "сибирь", "урал", "волга",
    "bank", "capital", "finance", "invest", "fund", "technologies", "retail", "energy", "gold", "money", "market",
    "index", "global", "united", "international",
})
_ADJ_ENDINGS = ("ий", "ый", "ой", "ая", "яя", "ое", "ее", "ые", "ие")
_SOFT_ENDINGS = frozenset("аяоеиыьй")
MIN_STEM = 5
MIN_STEM_TAIL = 3  # для второго и следующих слов фразы
MAX_ENDING = 3
MIN_HEAD = 6
SHORT_ALIAS = 5  # короче — без падежей; капсовое — только капсом

_INVISIBLE = re.compile("[­​-‏⁠﻿]")
_TOKEN = re.compile(r"[^\W_]+")
_ISIN = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")
_OFZ_TICKER = re.compile(r"SU(\d{5})")


def _raw_tokens(text: str) -> list[str]:
    return _TOKEN.findall(_INVISIBLE.sub("", unicodedata.normalize("NFKC", text)))


def _norm(tok: str) -> str:
    return tok.lower().replace("ё", "е")


def _norm_phrase(s: str) -> str:
    return " ".join(_norm(t) for t in _raw_tokens(s))


def _is_cyr(s: str) -> bool:
    return bool(s) and all("а" <= c <= "я" for c in s)


def _stem(word: str, min_len: int = MIN_STEM) -> str | None:
    """Основа для сопоставления с падежными формами; None — только точное совпадение."""
    if not _is_cyr(word):
        return None
    if word.endswith(_ADJ_ENDINGS):
        stem = word[:-2]
    elif word[-1] in _SOFT_ENDINGS:
        stem = word[:-1]
    else:
        stem = word
    return stem if len(stem) >= min_len else None


@dataclass(frozen=True, slots=True)
class _Word:
    text: str
    stem: str | None

    def matches(self, tok: str) -> bool:
        if tok == self.text:
            return True
        if self.stem is None or not tok.startswith(self.stem):
            return False
        rest = tok[len(self.stem) :]
        return len(rest) <= MAX_ENDING and _is_cyr(rest)


@dataclass(slots=True)
class _NameAlias:
    words: tuple[_Word, ...]
    upper_first: bool  # первое слово в тексте — только капсом
    tickers: set[str] = field(default_factory=set)

    @property
    def key(self) -> str:
        return " ".join(w.text for w in self.words)


def _make_words(tokens: Sequence[str]) -> tuple[tuple[_Word, ...], bool]:
    """Слова алиаса: у первого основа ≥ 5 букв, у следующих ≥ 3 — фраза сама по себе даёт контекст
    («Газпром нефти», «МТС-Банка»)."""
    norm = [_norm(t) for t in tokens]
    upper_first = tokens[0].isupper() and len(norm[0]) < SHORT_ALIAS
    first = _Word(norm[0], _stem(norm[0]) if len(norm[0]) >= SHORT_ALIAS else None)
    return (first, *(_Word(n, _stem(n, MIN_STEM_TAIL)) for n in norm[1:])), upper_first


def _clean_name(name: str, asset_class: AssetClass) -> list[str]:
    """Токены названия без орг.-правовых форм, признаков класса, номеров выпусков и обобщающих краёв."""
    out: list[str] = []
    for tok in _raw_tokens(name):
        n = _norm(tok)
        if n in _LEGAL_FORMS or n in _CLASS_WORDS:
            continue
        if asset_class is AssetClass.BOND and (any(c.isdigit() for c in n) or len(n) == 1):
            continue  # «001Р-02R», «Б-1-284»: номер выпуска, в новостях так не пишут
        out.append(tok)
    while out and _norm(out[-1]) in {"п", "ао", "ап"}:  # «Сбербанк-п», «Мечел ао»
        out.pop()
    while out and _norm(out[0]) in _GENERIC_EDGE:
        out.pop(0)
    while out and _norm(out[-1]) in _GENERIC_EDGE:
        out.pop()
    return out


def _usable(tokens: Sequence[str]) -> bool:
    norm = [_norm(t) for t in tokens]
    if not norm or all(n in _GENERIC_WORDS or n.isdigit() for n in norm):
        return False
    return len(norm) > 1 or len(norm[0]) >= 3


def _head(tokens: Sequence[str]) -> str | None:
    """Головное слово многословного названия, если хвост описательный, а не признак «дочки».

    «Аэрофлот — Российские авиалинии» → «аэрофлот»; «Газпром нефть», «Сбербанк Лизинг», «Россети Центр» → нет:
    обобщающий хвост означает другую компанию группы, и голова указывала бы на родителя.
    """
    if len(tokens) < 2:
        return None
    h = _norm(tokens[0])
    if not _is_cyr(h) or len(h) < MIN_HEAD or h.endswith(_ADJ_ENDINGS) or h in _GENERIC_WORDS:
        return None
    if any(_norm(t) in _GENERIC_WORDS or any(c.isdigit() for c in t) for t in tokens[1:]):
        return None
    return h


def _case_ok(tok: str, upper_first: bool) -> bool:
    if upper_first:
        return tok.isupper()
    return tok[0].isupper() or not tok[0].isalpha()


def _better(a: LinkMethod, b: LinkMethod | None) -> bool:
    return b is None or (CONFIDENCE[a], _RANK[a]) > (CONFIDENCE[b], _RANK[b])


class AliasIndex:
    """Словарь алиасов «токен/фраза → тикеры». Строится из справочника инструментов и ручных алиасов."""

    def __init__(self, tickers: Mapping[str, set[str]], isins: Mapping[str, set[str]],
                 names: Iterable[_NameAlias]) -> None:
        self._tickers = {k: frozenset(v) for k, v in tickers.items()}
        self._isins = {k: frozenset(v) for k, v in isins.items()}
        self._by_exact: dict[str, list[_NameAlias]] = {}
        self._by_stem: dict[str, list[_NameAlias]] = {}
        self._names: list[_NameAlias] = []
        for a in names:
            self._names.append(a)
            first = a.words[0]
            bucket = self._by_stem if first.stem is not None else self._by_exact
            bucket.setdefault(first.stem or first.text, []).append(a)

    @classmethod
    def build(
        cls,
        instruments: Iterable[InstrumentMeta],
        extra_aliases: Mapping[str, Sequence[str]] | None = None,
        ambiguous: Iterable[str] = DEFAULT_AMBIGUOUS,
    ) -> AliasIndex:
        tickers: dict[str, set[str]] = {}
        isins: dict[str, set[str]] = {}
        derived: dict[str, _NameAlias] = {}  # из справочника
        heads: dict[str, list[_NameAlias]] = {}
        for inst in instruments:
            t = inst.ticker.strip()
            if not t:
                continue
            tu = t.upper()
            if len(tu) >= 3 and tu.isalnum() and any(c.isalpha() for c in tu) and tu not in TICKER_STOPLIST:
                tickers.setdefault(tu, set()).add(t)
            if inst.isin and _ISIN.fullmatch(inst.isin.strip().upper()):
                isins.setdefault(inst.isin.strip().upper(), set()).add(t)
            phrases: list[list[str]] = []
            if inst.asset_class in (AssetClass.EQUITY, AssetClass.BOND) and inst.name:
                cleaned = _clean_name(inst.name, inst.asset_class)
                if _usable(cleaned):
                    phrases.append(cleaned)
            if inst.asset_class is AssetClass.BOND and inst.is_government:
                m = _OFZ_TICKER.match(tu) or re.search(r"\b(\d{5})\b", inst.name)
                if m:
                    phrases.append(["ОФЗ", m.group(1)])  # «ОФЗ 26238» — так выпуск называют в новостях
            for tokens in phrases:
                words, upper = _make_words(tokens)
                a = derived.setdefault(" ".join(w.text for w in words), _NameAlias(words, upper))
                a.upper_first = a.upper_first and upper
                if not a.tickers:
                    h = _head(tokens)
                    if h is not None:
                        heads.setdefault(h, []).append(a)
                a.tickers.add(t)

        manual: dict[str, _NameAlias] = {}
        for ticker, aliases in (extra_aliases or {}).items():
            for alias in (aliases,) if isinstance(aliases, str) else aliases:
                tokens = _raw_tokens(alias)
                if not tokens or len("".join(tokens)) < 2:
                    continue
                words, upper = _make_words(tokens)
                a = manual.setdefault(" ".join(w.text for w in words), _NameAlias(words, upper))
                a.upper_first = a.upper_first and upper
                a.tickers.add(ticker.strip())

        names: dict[str, _NameAlias] = {}
        for src in (derived, manual):
            for key, a in src.items():
                cur = names.setdefault(key, _NameAlias(a.words, a.upper_first))
                cur.upper_first = cur.upper_first and a.upper_first
                cur.tickers |= a.tickers
        for h, owners in heads.items():
            # голова уникальна: одна компания и нет такого же однословного названия или ручного алиаса у другой
            if len(owners) == 1 and (h not in names or names[h].tickers <= owners[0].tickers):
                cur = names.setdefault(h, _NameAlias((_Word(h, _stem(h)),), False))
                cur.tickers |= owners[0].tickers

        banned = {_norm_phrase(s) for s in ambiguous}
        return cls(tickers, isins, (a for k, a in sorted(names.items()) if k not in banned and a.tickers))

    def name_aliases(self, ticker: str) -> list[str]:
        """Нормализованные алиасы-названия бумаги (для отладки и команды /alias)."""
        return sorted(a.key for a in self._names if ticker in a.tickers)

    def _candidates(self, tok: str) -> list[_NameAlias]:
        out = list(self._by_exact.get(tok, ()))
        for k in range(MAX_ENDING + 1):
            stem = tok[: len(tok) - k] if k else tok
            if len(stem) < MIN_STEM:
                break
            if k and not _is_cyr(tok[len(tok) - k :]):
                break
            out.extend(self._by_stem.get(stem, ()))
        return out

    def scan(self, text: str) -> dict[str, LinkMethod]:
        """Тикеры, упомянутые в тексте, с сильнейшим методом связывания."""
        raw = _raw_tokens(text)
        toks = [_norm(t) for t in raw]
        found: dict[str, LinkMethod] = {}

        def put(tks: Iterable[str], method: LinkMethod) -> None:
            for tk in tks:
                if _better(method, found.get(tk)):
                    found[tk] = method

        for tok in raw:
            put(self._isins.get(tok.upper(), ()), "alias_isin")
            put(self._tickers.get(tok, ()), "alias_ticker")

        spans: list[tuple[int, int, frozenset[str]]] = []
        for i, tok in enumerate(toks):
            for a in self._candidates(tok):
                end = i + len(a.words)
                if end <= len(toks) and _case_ok(raw[i], a.upper_first) and all(
                    w.matches(toks[i + j]) for j, w in enumerate(a.words)
                ):
                    spans.append((i, end, frozenset(a.tickers)))
        for s, e, tks in spans:
            if not any(s2 <= s and e <= e2 and e2 - s2 > e - s for s2, e2, _ in spans):
                put(tks, "alias_name")
        return found

    def link(self, item: RawNewsItem) -> list[NewsLink]:
        """Связи новости с бумагами: API источника (1.0), тикер/ISIN (0.9), название (0.7); по тикеру — сильнейшая."""
        best: dict[str, LinkMethod] = {}
        for t in item.instrument_tickers:
            if t.strip():
                best[t.strip()] = "api"  # связь от источника сохраняется, даже если бумаги нет в справочнике
        for text in (item.title, item.lead):
            for ticker, method in self.scan(text).items():
                if _better(method, best.get(ticker)):
                    best[ticker] = method
        links = [NewsLink(ticker=t, method=m, confidence=CONFIDENCE[m]) for t, m in best.items()]
        return sorted(links, key=lambda x: (-x.confidence, -_RANK[x.method], x.ticker))


def load_aliases(path: Path | str) -> tuple[dict[str, tuple[str, ...]], frozenset[str]]:
    """Ручные алиасы и омонимы из `config/aliases.yaml` (формат — `config/aliases.example.yaml`).

    Нет файла — пустые алиасы и `DEFAULT_AMBIGUOUS`. Омонимы из файла дополняют встроенный список;
    `ambiguous_defaults: false` — заменяют его.
    """
    p = Path(path)
    if not p.exists():
        return {}, DEFAULT_AMBIGUOUS
    raw: Any = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: ожидается словарь с ключами aliases, ambiguous, ambiguous_defaults")
    unknown = set(raw) - {"aliases", "ambiguous", "ambiguous_defaults"}
    if unknown:
        raise ValueError(f"{p}: неизвестные ключи {sorted(map(str, unknown))}")
    aliases: dict[str, tuple[str, ...]] = {}
    for ticker, names in (raw.get("aliases") or {}).items():
        if not isinstance(ticker, str):
            raise ValueError(f"{p}: тикер {ticker!r} должен быть строкой (возьмите в кавычки)")
        seq = [names] if isinstance(names, str) else names
        if not isinstance(seq, list) or not all(isinstance(n, str) and n.strip() for n in seq):
            raise ValueError(f"{p}: алиасы {ticker} — строка или список непустых строк")
        aliases[ticker.strip()] = tuple(n.strip() for n in seq)
    extra = raw.get("ambiguous") or []
    if not isinstance(extra, list) or not all(isinstance(n, str) for n in extra):
        raise ValueError(f"{p}: ambiguous — список строк")
    defaults = raw.get("ambiguous_defaults", True)
    if not isinstance(defaults, bool):
        raise ValueError(f"{p}: ambiguous_defaults — true или false")
    return aliases, (DEFAULT_AMBIGUOUS if defaults else frozenset()) | frozenset(extra)
