"""paper tracking of recommendations

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-11 12:00:00.000000

Бумажное исполнение каждой рекомендации (§15.4): paper_trade (действия плана ребалансировки, показанные идеи,
все кандидаты топ-20) и paper_outcome (исход по горизонту: доходность бумаги, бенчмарка и избыточная).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Значения как в investbot.data.db на момент миграции (миграция не импортирует код приложения).
Id = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
Money = sa.Numeric(precision=28, scale=10).with_variant(sa.String(length=40), "sqlite")


def upgrade() -> None:
    op.create_table(
        "paper_trade",
        sa.Column("id", Id, autoincrement=True, nullable=False),
        sa.Column("rec_key", sa.String(length=160), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("source_arm", sa.String(length=64), nullable=True),
        sa.Column("shown", sa.Boolean(), nullable=False),
        sa.Column("ticker", sa.String(length=64), nullable=False),
        sa.Column("asset_class", sa.String(length=16), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("entry_price", Money, nullable=False),
        sa.Column("price_unit", sa.String(length=16), nullable=False),
        sa.Column("benchmark", sa.String(length=32), nullable=True),
        sa.Column("horizon_weeks", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_paper_trade")),
        sa.UniqueConstraint("rec_key", name="uq_paper_trade_rec_key"),
    )
    with op.batch_alter_table("paper_trade", schema=None) as batch_op:
        batch_op.create_index("ix_paper_trade_as_of", ["as_of"], unique=False)

    op.create_table(
        "paper_outcome",
        sa.Column("paper_trade_id", Id, nullable=False),
        sa.Column("horizon_weeks", sa.Integer(), nullable=False),
        sa.Column("settled_on", sa.Date(), nullable=False),
        sa.Column("ret", Money, nullable=False),
        sa.Column("bench_ret", Money, nullable=False),
        sa.Column("excess", Money, nullable=False),
        sa.ForeignKeyConstraint(
            ["paper_trade_id"],
            ["paper_trade.id"],
            name=op.f("fk_paper_outcome_paper_trade_id_paper_trade"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("paper_trade_id", "horizon_weeks", name="pk_paper_outcome"),
    )


def downgrade() -> None:
    op.drop_table("paper_outcome")
    with op.batch_alter_table("paper_trade", schema=None) as batch_op:
        batch_op.drop_index("ix_paper_trade_as_of")
    op.drop_table("paper_trade")
