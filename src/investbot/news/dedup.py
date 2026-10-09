"""Дедупликация новостей в сюжеты (§8.2 п. 3): SHA-1 нормализованного текста + MinHash LSH по 5-граммам символов.

Чистый модуль без I/O. Окно 7 дней обеспечивает вызывающий код: индекс строится из архива за 7 дней
(или старые записи снимаются через `StoryIndex.remove`).

Решения:
- Нормализация: NFKC, удаление невидимых символов (мягкий перенос, ZWSP), нижний регистр, ё→е,
  вся пунктуация и кавычки «» → пробел, схлопывание пробелов.
- Из лида срезается агентская «шапка» вида «МОСКВА, 15 апреля. /ТАСС/.» или «Москва. 15 апреля. ИНТЕРФАКС -»:
  иначе одна и та же новость Интерфакса и ТАСС отличается первыми ~40 символами.
- Лид обрезается до первых `LEAD_PREFIX` символов после нормализации: источники отдают лиды разной длины.
- Шинглы — 5-граммы символов. Заголовок весит вдвое: его шинглы входят в множество дважды, в двух
  пространствах имён («t» — только заголовок, «b» — заголовок + лид). Простое повторение заголовка в строке
  не работает: Jaccard считается по множеству, повтор не добавляет новых шинглов.
- Кандидаты LSH ищутся с порогом ниже рабочего (по умолчанию threshold − 0,2): при пороге LSH 0,8 и 128
  перестановках вероятность попасть в кандидаты при J = 0,8 лишь ~0,4, при пороге 0,6 — ~0,99. Затем каждый
  кандидат проверяется оценкой Jaccard по MinHash ≥ threshold.
- Короткие тексты (< `MIN_FUZZY_LEN` символов после нормализации) сравниваются только по SHA-1.
- Детерминизм: фиксированный seed перестановок и хеш sha1 из datasketch (не `hash()` Python).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from datasketch import LeanMinHash, MinHash, MinHashLSH

LEAD_PREFIX = 300  # символов нормализованного лида для SHA-1 и шинглов
MIN_FUZZY_LEN = 20  # короче — только точное совпадение по SHA-1
DEFAULT_SEED = 1

_INVISIBLE = re.compile("[­​-‏⁠﻿]")
_NON_WORD = re.compile(r"[\W_]+")
_MONTHS = "января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря"
_AGENCIES = r"интерфакс(?:-\w+)?|тасс|риа новости|прайм|рбк|reuters|рейтер"
# «МОСКВА, 15 апреля. /ТАСС/.», «Москва. 15 апреля. ИНТЕРФАКС -», «Санкт-Петербург, 3 мая.» — только в начале лида.
_DATELINE = re.compile(
    rf"^\s*[^\W\d_][\w\- ]{{0,30}}?[.,]\s*\d{{1,2}}\s+(?:{_MONTHS})[.,]?"
    rf"(?:\s*/?(?:{_AGENCIES})/?)?"
    r"(?:\s*[.\-–—:]+)?\s*",
    re.IGNORECASE,
)


def normalize_for_match(s: str) -> str:
    """Нижний регистр, ё→е, без пунктуации и кавычек, пробелы схлопнуты."""
    s = _INVISIBLE.sub("", unicodedata.normalize("NFKC", s))
    s = s.lower().replace("ё", "е")
    return " ".join(_NON_WORD.sub(" ", s).split())


def _prepare(title: str, lead: str) -> tuple[str, str]:
    lead = _DATELINE.sub("", _INVISIBLE.sub("", unicodedata.normalize("NFKC", lead)), count=1)
    return normalize_for_match(title), normalize_for_match(lead)[:LEAD_PREFIX].rstrip()


def _sha1(title_n: str, lead_n: str) -> str:
    return hashlib.sha1(f"{title_n}\n{lead_n}".encode(), usedforsecurity=False).hexdigest()


def content_sha1(title: str, lead: str = "") -> str:
    """SHA-1 нормализованного заголовка и первых `LEAD_PREFIX` символов нормализованного лида."""
    return _sha1(*_prepare(title, lead))


def shingles(title_n: str, lead_n: str, k: int = 5) -> set[str]:
    """Символьные k-граммы: заголовок дважды (пространства «t» и «b»), лид один раз."""
    body = f"{title_n} {lead_n}".strip()
    feats = {"b" + body[i : i + k] for i in range(len(body) - k + 1)}
    feats.update("t" + title_n[i : i + k] for i in range(len(title_n) - k + 1))
    return feats


@dataclass(frozen=True, slots=True)
class _Prepared:
    sha1: str
    sketch: Any  # LeanMinHash | None (datasketch без аннотаций типов)


@dataclass(frozen=True, slots=True)
class _Entry:
    story_id: int
    sha1: str
    sketch: Any


class StoryIndex:
    """Индекс сюжетов: SHA-1 для точных дублей, MinHash LSH для почти-дублей (перепечатки между агентствами)."""

    def __init__(
        self,
        threshold: float = 0.8,
        num_perm: int = 128,
        shingle: int = 5,
        *,
        candidate_threshold: float | None = None,
        seed: int = DEFAULT_SEED,
    ) -> None:
        if not 0 < threshold <= 1:
            raise ValueError(f"threshold должен быть в (0, 1], получено {threshold}")
        if shingle < 1 or num_perm < 2:
            raise ValueError("shingle ≥ 1 и num_perm ≥ 2")
        cand = max(0.1, threshold - 0.2) if candidate_threshold is None else candidate_threshold
        if not 0 < cand <= threshold:
            raise ValueError(f"candidate_threshold должен быть в (0, threshold], получено {cand}")
        self.threshold = threshold
        self.num_perm = num_perm
        self.shingle = shingle
        self.seed = seed
        self._lsh = MinHashLSH(threshold=cand, num_perm=num_perm)
        self._entries: dict[int, _Entry] = {}
        self._by_sha1: dict[str, set[int]] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, news_id: object) -> bool:
        return news_id in self._entries

    def _prepare(self, title: str, lead: str) -> _Prepared:
        title_n, lead_n = _prepare(title, lead)
        sketch = None
        if len(f"{title_n} {lead_n}".strip()) >= MIN_FUZZY_LEN:
            m = MinHash(num_perm=self.num_perm, seed=self.seed)
            m.update_batch([f.encode() for f in shingles(title_n, lead_n, self.shingle)])
            sketch = LeanMinHash(m)
        return _Prepared(_sha1(title_n, lead_n), sketch)

    def _find(self, p: _Prepared) -> int | None:
        exact = [self._entries[n].story_id for n in self._by_sha1.get(p.sha1, ())]
        if exact:
            return min(exact)
        if p.sketch is None:
            return None
        best: int | None = None
        for key in self._lsh.query(p.sketch):
            e = self._entries[key]
            if float(p.sketch.jaccard(e.sketch)) >= self.threshold and (best is None or e.story_id < best):
                best = e.story_id
        return best

    def _add(self, news_id: int, story_id: int, p: _Prepared) -> None:
        if news_id in self._entries:
            raise ValueError(f"новость {news_id} уже в индексе")
        self._entries[news_id] = _Entry(story_id, p.sha1, p.sketch)
        self._by_sha1.setdefault(p.sha1, set()).add(news_id)
        if p.sketch is not None:
            self._lsh.insert(news_id, p.sketch)

    def find_story(self, title: str, lead: str = "") -> int | None:
        """story_id сюжета, к которому относится текст: сначала SHA-1, затем LSH с проверкой Jaccard.

        Из нескольких подходящих сюжетов выбирается самый ранний (минимальный story_id).
        """
        return self._find(self._prepare(title, lead))

    def add(self, news_id: int, story_id: int, title: str, lead: str = "") -> None:
        self._add(news_id, story_id, self._prepare(title, lead))

    def assign(self, news_id: int, title: str, lead: str = "") -> int:
        """Найти сюжет и добавить новость; новый сюжет получает story_id = news_id."""
        p = self._prepare(title, lead)
        story_id = self._find(p)
        sid = news_id if story_id is None else story_id
        self._add(news_id, sid, p)
        return sid

    def remove(self, news_id: int) -> None:
        """Снять новость из индекса (выход за окно 7 дней). Отсутствующий id игнорируется."""
        e = self._entries.pop(news_id, None)
        if e is None:
            return
        ids = self._by_sha1.get(e.sha1)
        if ids is not None:
            ids.discard(news_id)
            if not ids:
                del self._by_sha1[e.sha1]
        if e.sketch is not None:
            self._lsh.remove(news_id)
