"""Точка входа: investbot doctor | certs | portfolio | bot."""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import structlog

from investbot.ops.logging import setup_logging
from investbot.settings import Settings


def _doctor(settings: Settings) -> int:
    from investbot.ops.doctor import run_doctor

    checks = asyncio.run(run_doctor(settings))
    for c in checks:
        print(f"{'✅' if c.ok else '❌'} {c.name}: {c.detail}")
    return 0 if all(c.ok for c in checks) else 1


def _certs(settings: Settings, certs_dir: Path) -> int:
    from investbot.ops.doctor import build_ca_bundle

    out = settings.ca_bundle or settings.data_dir / "ca-bundle.pem"
    path = build_ca_bundle(certs_dir, out)
    print(f"CA bundle: {path}\nДобавьте в .env: INVESTBOT_CA_BUNDLE={path}")
    return 0


ROOT = Path(__file__).resolve().parents[2]
log = structlog.get_logger(__name__)


def _db(settings: Settings, action: str) -> int:
    from alembic import command
    from alembic.config import Config

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", settings.db_url().replace("%", "%%"))
    if action == "upgrade":
        command.upgrade(cfg, "head")
    else:
        command.current(cfg, verbose=True)
    return 0


def _import(settings: Settings, what: str, path: Path, replace: bool) -> int:
    """Перенос ручного портфеля (YAML) или файлового журнала (JSONL) в БД."""
    from investbot.data.db import make_engine
    from investbot.data.manual_import import import_yaml
    from investbot.data.manual_repo import DbManualStore
    from investbot.journal.db_store import DbJournal, import_file_journal
    from investbot.journal.store import Journal

    _db(settings, "upgrade")

    async def go() -> int:
        engine = make_engine(settings.db_url())
        try:
            if what == "manual":
                return await import_yaml(path, DbManualStore(engine), replace=replace)
            return await import_file_journal(Journal(path), DbJournal(engine))
        finally:
            await engine.dispose()

    n = asyncio.run(go())
    print(f"Импортировано: {n} {'счетов' if what == 'manual' else 'записей журнала'}")
    return 0


def _portfolio(settings: Settings) -> int:
    from investbot.bootstrap import build_services
    from investbot.channels.telegram.render import title
    from investbot.core.money import pct, round_money

    async def go() -> None:
        async with build_services(settings) as services:
            ov = await services.portfolio.overview()
        v = ov.valuation
        print(f"Итого: {round_money(v.total_rub, 0)} ₽, счетов {len(ov.snapshot.accounts)}, "
              f"позиций {len(ov.snapshot.positions)}")
        for d in ov.drift or ():
            print(f"  {d.status:10} {title(d.bucket):28} {pct(d.weight):>7}  цель "
                  f"{pct(d.target) if d.target is not None else '—':>7}")
        if not ov.drift:
            for b, w in sorted(v.bucket_weights.items(), key=lambda kv: -kv[1]):
                print(f"  {title(b):28} {pct(w):>7}")
        for warning in ov.warnings:
            print(f"⚠️  {warning}")

    _db(settings, "upgrade")
    asyncio.run(go())
    return 0


def _bot(settings: Settings) -> int:
    from aiogram import Bot, Dispatcher

    from investbot.adapters.tinvest.guard import TradingAccessError
    from investbot.bootstrap import build_services
    from investbot.channels.telegram.allocation import build_allocation_router
    from investbot.channels.telegram.app import run_bot
    from investbot.channels.telegram.calendar import build_calendar_router, calendar_block
    from investbot.channels.telegram.edit import build_edit_router
    from investbot.channels.telegram.ideas import build_ideas_router, ideas_block
    from investbot.channels.telegram.journal import build_journal_router
    from investbot.channels.telegram.news import build_news_router, news_block
    from investbot.channels.telegram.qa import build_qa_router
    from investbot.channels.telegram.report import ReportCenter, build_report_router
    from investbot.jobs.scheduler import SingleInstanceLock, build_scheduler
    from investbot.llm_roles.allocation_advisor import AllocationAdvisor
    from investbot.llm_roles.writer import Writer
    from investbot.news.tone import tone_line
    from investbot.reports.factsheet import build_weekly_factsheet

    if settings.telegram_bot_token is None:
        print("Не задан INVESTBOT_TELEGRAM_BOT_TOKEN", file=sys.stderr)
        return 2
    token = settings.telegram_bot_token.get_secret_value()
    owner = settings.owner_telegram_id
    _db(settings, "upgrade")  # до запуска event loop: env.py alembic сам вызывает asyncio.run

    async def go() -> None:
        lock = SingleInstanceLock(settings.data_dir / "scheduler.lock")
        lock.acquire()
        try:
            async with build_services(settings) as services:
                async def tone_lines(tickers: list[str]) -> dict[str, str]:
                    if services.llm is None:
                        return {}
                    summaries = await services.tone.summaries(tickers, datetime.now(UTC))
                    return {t: line for t, sm in summaries.items() if (line := tone_line(sm))}

                async def events_block(as_of: date) -> str | None:
                    blocks: list[str] = []
                    try:  # публикация идей недели: предрегистрация и бумажное исполнение до отправки отчёта
                        if ib := ideas_block((await services.ideas.weekly(as_of)).ideas):
                            blocks.append(ib)
                    except Exception as e:
                        log.warning("report.ideas_block_failed", error=type(e).__name__)
                    result = await services.calendar.upcoming(as_of, days=7)
                    if cal := calendar_block(result.events, as_of, days=7):
                        blocks.append(cal)
                    try:
                        held = await services.held_tickers()
                        if nb := news_block(await services.news.background(held), tones=await tone_lines(held)):
                            blocks.append(nb)
                    except Exception as e:
                        log.warning("report.news_block_failed", error=type(e).__name__)
                    return "\n\n".join(blocks) or None

                writer = Writer(services.llm) if services.llm is not None else None

                async def commentary(prep: Any) -> str | None:
                    if writer is None:
                        return None
                    published = await services.ideas.latest(prep.plan.as_of)
                    fs = build_weekly_factsheet(prep, published.ideas, settings.privacy_mode)
                    return (await writer.write(fs)).html

                center = ReportCenter(services.rebalance, settings.privacy_mode,
                                      settings.quantity_message_ttl_sec, events_block, commentary=commentary)
                services.qa.set_last_plan(lambda: center.last_weekly)

                async def startup(bot: Bot, dp: Dispatcher) -> None:
                    if owner is None:
                        return  # режим первичной настройки: расписание включится после указания владельца

                    async def weekly() -> None:
                        await center.send_weekly(bot, owner)

                    async def daily_check() -> None:
                        if services.broker is None:
                            return
                        try:
                            await services.broker.recheck_read_only()
                        except TradingAccessError as e:
                            await bot.send_message(owner, f"⛔ {e}\nБот остановлен. Выпустите токен «только чтение».")
                            await dp.stop_polling()

                    async def paper_settle() -> None:
                        await services.paper.settle(date.today())

                    async def monday_refresh() -> None:
                        await center.send_refresh(bot, owner)

                    async def news_rss() -> None:
                        await services.news.collect_due()

                    async def news_tinvest() -> None:
                        await services.news.collect_tinvest()

                    async def news_tone() -> None:
                        if services.llm is None:
                            return
                        await services.tone.annotate_pending(await services.held_tickers(), datetime.now(UTC),
                                                             limit=30)

                    build_scheduler(settings.tz, {
                        "weekly_report": (settings.weekly_report_cron, weekly),
                        "monday_refresh": (settings.monday_refresh_cron, monday_refresh),
                        "paper_settle": ("30 6 * * *", paper_settle),  # после публикации итогов дня в ISS
                        "daily_readonly_check": (settings.daily_check_cron, daily_check),
                        "news_rss": ("*/5 * * * *", news_rss),  # каждый источник — со своим poll_sec
                        "news_tinvest": ("*/15 * * * *", news_tinvest),
                        "news_tone": ("7,37 * * * *", news_tone),  # разметка тона новых новостей по позициям
                    }).start()

                await run_bot(
                    token, services, owner, settings.privacy_mode, settings.telegram_proxy,
                    extra_routers=(
                        build_allocation_router(services.allocation, advisor=AllocationAdvisor(services.llm)
                                                if services.llm is not None else None),
                        build_report_router(center),
                        build_calendar_router(services.calendar),
                        build_news_router(services.news, services.held_tickers, tone=tone_lines),
                        build_journal_router(services.paper),
                        build_ideas_router(services.ideas),
                        build_edit_router(services.manual),  # шаги мастеров принимают свободный текст
                        build_qa_router(services.qa),  # последним: забирает оставшийся свободный текст
                    ),
                    on_startup=startup,
                )
        finally:
            lock.release()

    asyncio.run(go())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="investbot")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="проверить сеть, сертификаты, SDK и права токена")
    certs = sub.add_parser("certs", help="собрать CA bundle: системные корни + сертификаты из каталога")
    certs.add_argument("--dir", type=Path, default=Path("docker/certs"))
    db = sub.add_parser("db", help="миграции БД")
    db.add_argument("action", choices=["upgrade", "current"])
    imp = sub.add_parser("import", help="перенести ручной портфель (YAML) или журнал (JSONL) в БД")
    imp.add_argument("what", choices=["manual", "journal"])
    imp.add_argument("path", type=Path)
    imp.add_argument("--replace", action="store_true", help="для manual: заменить уже сохранённые в БД счета")
    sub.add_parser("portfolio", help="показать портфель в терминале")
    sub.add_parser("bot", help="запустить Telegram-бота")
    args = parser.parse_args(argv)

    setup_logging(args.log_level)
    settings = Settings()
    match args.cmd:
        case "doctor":
            return _doctor(settings)
        case "certs":
            return _certs(settings, args.dir)
        case "db":
            return _db(settings, args.action)
        case "import":
            return _import(settings, args.what, args.path, args.replace)
        case "portfolio":
            return _portfolio(settings)
        case "bot":
            return _bot(settings)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
