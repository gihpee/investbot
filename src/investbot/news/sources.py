"""Реестр новостных источников: config/sources.yaml (ARCHITECTURE §5.7, §8.2 п. 1).

Telegram-каналы, Пульс и форум Smart-lab сюда не входят (ADR-12): для них нет ни `kind`, ни коллектора.
"""

from __future__ import annotations

import codecs
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Self
from urllib.parse import urlsplit

import yaml
from pydantic import Field, field_validator, model_validator

from investbot.core.models import Strict

SourceKind = Literal["rss", "tinvest_news"]
SourcePriority = Literal["P0", "P1", "P2"]
# Пользователю показываются только заголовок, время и ссылка (§1.3); тексты СМИ не пересылаются.
LicenseTag = Literal["headline_link_only"]


class SourceConfig(Strict):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")  # пишется в RawNewsItem.source и в source_health
    title: str = Field(min_length=1)
    kind: SourceKind
    url: str | None = None  # для rss — адрес ленты; для tinvest_news — только хост для doctor --network
    poll_sec: int = Field(ge=60, le=86_400)
    priority: SourcePriority
    q_src: Decimal = Field(ge=0, le=1)  # вес достоверности источника в признаках (§8.2 п. 7)
    enabled: bool = True
    encoding: str | None = None  # принудительная кодировка ответа (Финмаркет — windows-1251); None — по ленте
    category_filter: tuple[str, ...] = ()  # allowlist категорий <category>; пусто — без фильтра
    license_tag: LicenseTag = "headline_link_only"
    tos_ml_allowed: bool = False  # можно ли отдавать тексты в LLM-разметку (§8.2 п. 6); по умолчанию нельзя

    @field_validator("url")
    @classmethod
    def _http_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        parts = urlsplit(v)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError(f"url должен быть абсолютным http(s)-адресом: {v!r}")
        return v

    @field_validator("encoding")
    @classmethod
    def _known_encoding(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            codecs.lookup(v)
        except LookupError as e:
            raise ValueError(f"неизвестная кодировка {v!r}") from e
        return v

    @field_validator("category_filter")
    @classmethod
    def _non_empty_categories(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if any(not c.strip() for c in v):
            raise ValueError("пустая категория в category_filter")
        return v

    @model_validator(mode="after")
    def _rss_needs_url(self) -> Self:
        if self.kind == "rss" and self.url is None:
            raise ValueError(f"источник {self.key}: для rss нужен url")
        return self


class SourcesFile(Strict):
    version: int = Field(ge=1)
    sources: tuple[SourceConfig, ...]

    @field_validator("sources")
    @classmethod
    def _unique_keys(cls, v: tuple[SourceConfig, ...]) -> tuple[SourceConfig, ...]:
        dups = sorted(k for k, n in Counter(s.key for s in v).items() if n > 1)
        if dups:
            raise ValueError(f"повторяющиеся ключи источников: {dups}")
        return v


def load_sources(path: Path | str) -> list[SourceConfig]:
    """Все источники из файла, включая выключенные (enabled=false): их хосты нужны doctor и allowlist."""
    raw: Any = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return list(SourcesFile.model_validate(raw).sources)
