"""Alembic: асинхронный движок, URL из настроек investbot."""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.engine import Connection

from investbot.data.db import make_engine, metadata
from investbot.settings import Settings

config = context.config
url = config.get_main_option("sqlalchemy.url") or Settings().db_url()


def run_offline() -> None:
    context.configure(url=url, target_metadata=metadata, literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def _run(conn: Connection) -> None:
    context.configure(connection=conn, target_metadata=metadata, render_as_batch=url.startswith("sqlite"))
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    engine = make_engine(url)
    async with engine.connect() as conn:
        await conn.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    asyncio.run(run_online())
