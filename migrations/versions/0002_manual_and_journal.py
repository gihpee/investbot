"""manual portfolio and append-only journal

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-09 23:40:00.000000

Ручной портфель (manual_account / manual_position / manual_cash) и журнал с хэш-цепочкой (journal_record).
Журнал append-only (ADR-13): UPDATE/DELETE (и TRUNCATE в Postgres) падают с ошибкой в триггерах.
Триггеры alembic не сравнивает при autogenerate/check — они живут только здесь.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Значения как в investbot.data.db на момент миграции (миграция не импортирует код приложения).
Id = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
Money = sa.Numeric(precision=28, scale=10).with_variant(sa.String(length=40), "sqlite")

PG_UPGRADE = (
    """
    CREATE FUNCTION journal_record_append_only() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'journal is append-only: % on journal_record is forbidden', TG_OP;
    END
    $$
    """,
    """
    CREATE TRIGGER journal_record_no_update_delete
    BEFORE UPDATE OR DELETE ON journal_record
    FOR EACH ROW EXECUTE FUNCTION journal_record_append_only()
    """,
    """
    CREATE TRIGGER journal_record_no_truncate
    BEFORE TRUNCATE ON journal_record
    FOR EACH STATEMENT EXECUTE FUNCTION journal_record_append_only()
    """,
)
PG_DOWNGRADE = (
    "DROP TRIGGER IF EXISTS journal_record_no_truncate ON journal_record",
    "DROP TRIGGER IF EXISTS journal_record_no_update_delete ON journal_record",
    "DROP FUNCTION IF EXISTS journal_record_append_only()",
)

SQLITE_UPGRADE = (
    """
    CREATE TRIGGER journal_record_no_update BEFORE UPDATE ON journal_record
    BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END
    """,
    """
    CREATE TRIGGER journal_record_no_delete BEFORE DELETE ON journal_record
    BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END
    """,
)
SQLITE_DOWNGRADE = (
    "DROP TRIGGER IF EXISTS journal_record_no_delete",
    "DROP TRIGGER IF EXISTS journal_record_no_update",
)


def _dialect() -> str:
    # get_context(), а не get_bind(): работает и в offline-режиме (`alembic upgrade --sql`)
    return op.get_context().dialect.name


def upgrade() -> None:
    op.create_table(
        "journal_record",
        sa.Column("seq", Id, autoincrement=True, nullable=False),
        sa.Column("ts", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("author", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("prev", sa.String(length=64), nullable=False),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_journal_record")),
        sa.UniqueConstraint("hash", name="uq_journal_record_hash"),
        sa.UniqueConstraint("prev", name="uq_journal_record_prev"),
    )
    op.create_table(
        "manual_account",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("tinvest_account_id", sa.String(length=64), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_manual_account")),
    )
    op.create_table(
        "manual_cash",
        sa.Column("account_id", sa.String(length=64), nullable=False),
        sa.Column("currency", sa.String(length=16), nullable=False),
        sa.Column("amount", Money, nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["manual_account.id"], name=op.f("fk_manual_cash_account_id_manual_account"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("account_id", "currency", name="pk_manual_cash"),
    )
    op.create_table(
        "manual_position",
        sa.Column("account_id", sa.String(length=64), nullable=False),
        sa.Column("ticker", sa.String(length=64), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("asset_class", sa.String(length=16), nullable=False),
        sa.Column("qty", Money, nullable=False),
        sa.Column("price", Money, nullable=True),
        sa.Column("price_pct", Money, nullable=True),
        sa.Column("nominal", Money, nullable=True),
        sa.Column("accrued", Money, nullable=False),
        sa.Column("currency", sa.String(length=16), nullable=False),
        sa.Column("price_date", sa.Date(), nullable=True),
        sa.Column("is_government", sa.Boolean(), nullable=False),
        sa.Column("coupon_kind", sa.String(length=16), nullable=False),
        sa.Column("rating", sa.String(length=64), nullable=True),
        sa.Column("fund_focus", sa.String(length=16), nullable=True),
        sa.Column("operator", sa.Text(), nullable=True),
        sa.Column("maturity", sa.Date(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["manual_account.id"], name=op.f("fk_manual_position_account_id_manual_account"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("account_id", "ticker", name="pk_manual_position"),
    )

    dialect = _dialect()
    if dialect == "postgresql":
        for stmt in PG_UPGRADE:
            op.execute(stmt)
    elif dialect == "sqlite":
        for stmt in SQLITE_UPGRADE:
            op.execute(stmt)
    else:
        raise NotImplementedError(f"append-only триггеры journal_record не описаны для диалекта {dialect}")


def downgrade() -> None:
    dialect = _dialect()
    if dialect == "postgresql":
        for stmt in PG_DOWNGRADE:
            op.execute(stmt)
    elif dialect == "sqlite":
        for stmt in SQLITE_DOWNGRADE:
            op.execute(stmt)

    op.drop_table("manual_position")
    op.drop_table("manual_cash")
    op.drop_table("manual_account")
    op.drop_table("journal_record")
