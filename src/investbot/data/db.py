"""Схема БД и движок (ADR-07): PostgreSQL в проде, SQLite для локального запуска и тестов.

Таблицы описаны на SQLAlchemy Core и не используют диалект-специфичных типов, кроме вариантов BigInteger.
Миграции — alembic (`migrations/`), команда `investbot db upgrade`.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

# BIGINT в Postgres, INTEGER PRIMARY KEY (rowid) в SQLite — иначе SQLite не автоинкрементирует
Id = BigInteger().with_variant(Integer(), "sqlite")

metadata = MetaData(naming_convention={
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
})

news_item = Table(
    "news_item", metadata,
    Column("id", Id, primary_key=True, autoincrement=True),
    Column("source", String(64), nullable=False),
    Column("external_id", String(512), nullable=False),
    Column("url", Text),
    Column("title", Text, nullable=False),
    Column("lead", Text, nullable=False, server_default=""),
    Column("published_at", DateTime(timezone=True)),
    Column("first_seen_at", DateTime(timezone=True), nullable=False),
    Column("sha1", String(40), nullable=False),
    Column("story_id", Id, nullable=False),
    Column("priority", Integer),
    UniqueConstraint("source", "external_id", name="uq_news_item_source_external_id"),
)
Index("ix_news_item_first_seen_at", news_item.c.first_seen_at)
Index("ix_news_item_sha1", news_item.c.sha1)
Index("ix_news_item_story_id", news_item.c.story_id)

news_link = Table(
    "news_link", metadata,
    Column("news_id", Id, ForeignKey("news_item.id", ondelete="CASCADE"), primary_key=True),
    Column("ticker", String(32), primary_key=True),
    Column("method", String(16), nullable=False),
    Column("confidence", Numeric(4, 3), nullable=False),
)
Index("ix_news_link_ticker", news_link.c.ticker)

source_health = Table(
    "source_health", metadata,
    Column("source", String(64), primary_key=True),
    Column("last_ok_at", DateTime(timezone=True)),
    Column("last_error_at", DateTime(timezone=True)),
    Column("last_error", Text),
    Column("consecutive_empty", Integer, nullable=False, server_default="0"),
    Column("consecutive_errors", Integer, nullable=False, server_default="0"),
    Column("last_new_items", Integer, nullable=False, server_default="0"),
    Column("overrun", Integer, nullable=False, server_default="0"),
)

kv_state = Table(
    "kv_state", metadata,
    Column("key", String(128), primary_key=True),
    Column("value", Text, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


def default_sqlite_url(data_dir: Path) -> str:
    return f"sqlite+aiosqlite:///{data_dir / 'investbot.db'}"


def make_engine(url: str) -> AsyncEngine:
    if url.startswith("sqlite"):
        return create_async_engine(url, connect_args={"timeout": 30})
    return create_async_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)
