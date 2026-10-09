"""Точка входа: investbot doctor | certs | portfolio | bot."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

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

    asyncio.run(go())
    return 0


def _bot(settings: Settings) -> int:
    from investbot.bootstrap import build_services
    from investbot.channels.telegram.allocation import build_allocation_router
    from investbot.channels.telegram.app import run_bot

    if settings.telegram_bot_token is None:
        print("Не задан INVESTBOT_TELEGRAM_BOT_TOKEN", file=sys.stderr)
        return 2

    async def go() -> None:
        async with build_services(settings) as services:
            await run_bot(settings.telegram_bot_token.get_secret_value(), services,  # type: ignore[union-attr]
                          settings.owner_telegram_id, settings.privacy_mode, settings.telegram_proxy,
                          extra_routers=(build_allocation_router(services.allocation),))

    asyncio.run(go())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="investbot")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="проверить сеть, сертификаты, SDK и права токена")
    certs = sub.add_parser("certs", help="собрать CA bundle: системные корни + сертификаты из каталога")
    certs.add_argument("--dir", type=Path, default=Path("docker/certs"))
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
        case "portfolio":
            return _portfolio(settings)
        case "bot":
            return _bot(settings)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
