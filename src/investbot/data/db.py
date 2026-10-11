"""Схема БД и движок (ADR-07): PostgreSQL в проде, SQLite для локального запуска и тестов.

Таблицы описаны на SQLAlchemy Core и не используют диалект-специфичных типов, кроме вариантов BigInteger
и `Money` (NUMERIC в Postgres, точная строка в SQLite).
Миграции — alembic (`migrations/`), команда `investbot db upgrade`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.types import TypeDecorator, TypeEngine

# BIGINT в Postgres, INTEGER PRIMARY KEY (rowid) в SQLite — иначе SQLite не автоинкрементирует
Id = BigInteger().with_variant(Integer(), "sqlite")

_MONEY_STEP = Decimal("1e-10")


def plain_decimal(d: Decimal) -> Decimal:
    """Без хвостовых нулей и без экспоненты у целых: Decimal('100.0000000000') → Decimal('100')."""
    n = d.normalize()
    return n.quantize(Decimal(1)) if n.is_finite() and n == n.to_integral_value() else n


class Money(TypeDecorator[Decimal]):
    """Деньги и количества: NUMERIC(28, 10) в Postgres; в SQLite — точная строка.

    У SQLite нет десятичного типа: SQLAlchemy гнал бы Decimal через float (потеря точности после ~15 знаков).
    В SQLite значение округляется до 10 знаков после запятой так же, как это делает NUMERIC(28, 10) в Postgres.
    """

    impl = Numeric(28, 10)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(40))
        return dialect.type_descriptor(Numeric(28, 10))

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> Any:
        if value is None:
            return None
        if dialect.name == "sqlite":
            return format(plain_decimal(Decimal(value).quantize(_MONEY_STEP, rounding=ROUND_HALF_UP)), "f")
        return value

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        return None if value is None else plain_decimal(Decimal(str(value)))


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


# Ручной портфель (бывший config/manual_portfolio.yaml): счета, позиции-карточки (в т.ч. ЦФА), остатки денег.
# Порядок счетов и позиций — sort_order (как порядок в YAML).
manual_account = Table(
    "manual_account", metadata,
    Column("id", String(64), primary_key=True),
    Column("name", Text, nullable=False),
    Column("kind", String(16), nullable=False),
    Column("tinvest_account_id", String(64)),
    Column("sort_order", Integer, nullable=False),
)

manual_position = Table(
    "manual_position", metadata,
    Column("account_id", String(64), ForeignKey("manual_account.id", ondelete="CASCADE"), nullable=False),
    Column("ticker", String(64), nullable=False),
    Column("sort_order", Integer, nullable=False),
    Column("name", Text, nullable=False),
    Column("asset_class", String(16), nullable=False),
    Column("qty", Money, nullable=False),
    Column("price", Money),
    Column("price_pct", Money),
    Column("nominal", Money),
    Column("accrued", Money, nullable=False),
    Column("currency", String(16), nullable=False),
    Column("price_date", Date),
    Column("is_government", Boolean, nullable=False),
    Column("coupon_kind", String(16), nullable=False),
    Column("rating", String(64)),
    Column("fund_focus", String(16)),
    Column("operator", Text),
    Column("maturity", Date),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("account_id", "ticker", name="pk_manual_position"),
)

manual_cash = Table(
    "manual_cash", metadata,
    Column("account_id", String(64), ForeignKey("manual_account.id", ondelete="CASCADE"), nullable=False),
    Column("currency", String(16), nullable=False),
    Column("amount", Money, nullable=False),
    PrimaryKeyConstraint("account_id", "currency", name="pk_manual_cash"),
)

# Append-only журнал (ADR-13): UPDATE/DELETE/TRUNCATE запрещены триггерами (миграция 0002).
# ts — ровно та строка, что вошла в хэш; payload — JSON. prev уникален: цепочка не может ветвиться.
journal_record = Table(
    "journal_record", metadata,
    Column("seq", Id, primary_key=True, autoincrement=True),
    Column("ts", String(64), nullable=False),
    Column("kind", String(64), nullable=False),
    Column("author", String(64), nullable=False),
    Column("payload", Text, nullable=False),
    Column("prev", String(64), nullable=False),
    Column("hash", String(64), nullable=False),
    UniqueConstraint("hash", name="uq_journal_record_hash"),
    UniqueConstraint("prev", name="uq_journal_record_prev"),
)

# Бумажное исполнение рекомендаций (§15.4): одна строка на рекомендацию (действие плана, показанную идею или
# непоказанного кандидата топ-20), исходы — по горизонтам. rec_key — ключ идемпотентности регистрации:
# "plan:<plan_id>:<seq>" или "idea:<idea_id>". Это производные данные: источник истины — journal_record.
paper_trade = Table(
    "paper_trade", metadata,
    Column("id", Id, primary_key=True, autoincrement=True),
    Column("rec_key", String(160), nullable=False),
    Column("kind", String(32), nullable=False),  # rebalance_action | entry_idea | idea_candidate
    Column("source_arm", String(64)),
    Column("shown", Boolean, nullable=False),
    Column("ticker", String(64), nullable=False),
    Column("asset_class", String(16), nullable=False),
    Column("side", String(8), nullable=False),  # buy | sell
    Column("as_of", Date, nullable=False),
    Column("entry_price", Money, nullable=False),
    Column("price_unit", String(16), nullable=False),  # rub | pct_nominal
    Column("benchmark", String(32)),  # NULL — бенчмарк класса не задан, исход не считается
    Column("horizon_weeks", Integer, nullable=False),  # собственный горизонт рекомендации
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("rec_key", name="uq_paper_trade_rec_key"),
)
Index("ix_paper_trade_as_of", paper_trade.c.as_of)

# ret и bench_ret — доходности бумаги и бенчмарка по TR-рядам как есть; excess — с учётом стороны
# (для продажи знак обратный: избежать падения — успех).
paper_outcome = Table(
    "paper_outcome", metadata,
    Column("paper_trade_id", Id, ForeignKey("paper_trade.id", ondelete="CASCADE"), nullable=False),
    Column("horizon_weeks", Integer, nullable=False),
    Column("settled_on", Date, nullable=False),
    Column("ret", Money, nullable=False),
    Column("bench_ret", Money, nullable=False),
    Column("excess", Money, nullable=False),
    PrimaryKeyConstraint("paper_trade_id", "horizon_weeks", name="pk_paper_outcome"),
)


def default_sqlite_url(data_dir: Path) -> str:
    return f"sqlite+aiosqlite:///{data_dir / 'investbot.db'}"


def make_engine(url: str) -> AsyncEngine:
    if url.startswith("sqlite"):
        return create_async_engine(url, connect_args={"timeout": 30})
    return create_async_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)


@asynccontextmanager
async def write_tx(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """Транзакция записи с чтением внутри (read-modify-write).

    В SQLite — `BEGIN IMMEDIATE`: блокировка записи берётся сразу, конкурирующие писатели ждут (timeout=30),
    и прочитанное внутри транзакции не устаревает до COMMIT. В Postgres — обычная транзакция.
    """
    async with engine.begin() as conn:
        if conn.dialect.name == "sqlite":
            await conn.exec_driver_sql("BEGIN IMMEDIATE")  # pysqlite сам BEGIN не шлёт до первого DML
        yield conn


@asynccontextmanager
async def read_snapshot(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """Несколько SELECT по одному согласованному снимку (REPEATABLE READ в Postgres, явный BEGIN в SQLite)."""
    async with engine.connect() as conn:
        if conn.dialect.name == "sqlite":
            await conn.exec_driver_sql("BEGIN")  # иначе pysqlite выполняет каждый SELECT в автокоммите
        else:
            await conn.execution_options(isolation_level="REPEATABLE READ")
        yield conn


# Вызовы LLM (G10, G11): учёт токенов и стоимости для бюджета, версии промптов и моделей. Без текстов запросов.
llm_call = Table(
    "llm_call", metadata,
    Column("id", Id, primary_key=True, autoincrement=True),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("role", String(32), nullable=False),
    Column("provider", String(32), nullable=False),
    Column("model", String(128), nullable=False),
    Column("prompt_id", String(64), nullable=False),
    Column("input_tokens", Integer, nullable=False),
    Column("output_tokens", Integer, nullable=False),
    Column("cost_rub", Money, nullable=False),
    Column("ok", Boolean, nullable=False),
    Column("error", Text),
)
Index("ix_llm_call_at", llm_call.c.at)

# LLM-разметка новостей (Tagger, §8.2 п. 6): событие из закрытой таксономии и тон к каждой связанной бумаге.
news_annotation = Table(
    "news_annotation", metadata,
    Column("news_id", Id, ForeignKey("news_item.id", ondelete="CASCADE"), nullable=False),
    Column("ticker", String(32), nullable=False),
    Column("event_type", String(32), nullable=False),
    Column("tone", Money, nullable=False),  # −1…+1 к бумаге
    Column("relevance", Money, nullable=False),  # 0…1
    Column("confidence", Money, nullable=False),  # 0…1
    Column("is_ad", Boolean, nullable=False),
    Column("is_rumor", Boolean, nullable=False),
    Column("summary", Text, nullable=False),
    Column("model", String(128), nullable=False),
    Column("prompt_id", String(64), nullable=False),
    Column("annotated_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("news_id", "ticker", name="pk_news_annotation"),
)
Index("ix_news_annotation_ticker", news_annotation.c.ticker)
