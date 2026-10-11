"""`investbot doctor`: проверка окружения до запуска (Ф0: сеть, сертификаты, SDK, права токена)."""

from __future__ import annotations

import asyncio
import importlib.util
import socket
import ssl
from dataclasses import dataclass
from pathlib import Path

import certifi

from investbot.settings import Settings

HOSTS = {
    "MOEX ISS": ("iss.moex.com", 443),
    "T-Invest API": ("invest-public-api.tbank.ru", 443),
    "Telegram Bot API": ("api.telegram.org", 443),
}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def build_ca_bundle(certs_dir: Path, out: Path) -> Path:
    """Системные корни (certifi) + сертификаты из certs_dir → единый bundle для httpx и gRPC.

    Сертификаты Минцифры (Russian Trusted Root CA и Sub CA) пользователь кладёт в certs_dir сам;
    в системное хранилище они не устанавливаются.
    """
    extra = sorted([*certs_dir.glob("*.crt"), *certs_dir.glob("*.pem")])
    if not extra:
        raise FileNotFoundError(f"в {certs_dir} нет .crt/.pem — см. docker/certs/README.md")
    parts = [Path(certifi.where()).read_text(encoding="ascii")]
    parts += [p.read_text(encoding="ascii") for p in extra]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(parts), encoding="ascii")
    return out


RU_ROOT_CN = "Russian Trusted Root CA"


def _load_bundle(path: Path) -> ssl.SSLContext | None:
    try:
        return ssl.create_default_context(cafile=str(path))
    except (ssl.SSLError, OSError):
        return None


def bundle_status(path: Path) -> tuple[bool, str]:
    """Bundle читается как PEM и содержит корень Минцифры — иначе подсказка, как пересобрать."""
    ctx = _load_bundle(path)
    if ctx is None:
        return False, f"{path}: пуст или не PEM — пересоберите (docs/DEPLOY.md, шаг 4)"
    if not any(f"'commonName', '{RU_ROOT_CN}'" in str(cert.get("subject")) for cert in ctx.get_ca_certs()):
        return False, f"{path}: нет {RU_ROOT_CN} (Минцифры) — docs/DEPLOY.md, шаг 4"
    return True, str(path)


def _tls_check(host: str, port: int, cafile: str, timeout: float = 8.0) -> Check:
    ctx = ssl.create_default_context(cafile=cafile)
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock, \
                ctx.wrap_socket(sock, server_hostname=host):
            return Check(host, True, "TLS ок")
    except ssl.SSLCertVerificationError as e:
        return Check(host, False, f"сертификат не проверен ({e.verify_message}) — нужен CA Минцифры в bundle")
    except OSError as e:
        return Check(host, False, f"нет соединения: {e.__class__.__name__}")


async def run_doctor(settings: Settings) -> list[Check]:
    checks: list[Check] = []
    bundle = settings.ca_bundle
    if bundle is None or not bundle.exists():
        bundle_ok, detail = False, f"{bundle}: файла нет" if bundle else "не задан (INVESTBOT_CA_BUNDLE)"
    else:
        bundle_ok, detail = bundle_status(bundle)
    checks.append(Check("CA bundle", bundle_ok, detail))
    cafile = str(bundle) if bundle and bundle.exists() and _load_bundle(bundle) else certifi.where()
    for name, (host, port) in HOSTS.items():
        c = await asyncio.to_thread(_tls_check, host, port, cafile)
        c.name = name
        checks.append(c)

    sdk = importlib.util.find_spec("t_tech") is not None
    checks.append(Check("SDK t-tech-investments", sdk, "установлен" if sdk else "не установлен — README"))
    checks.append(Check("Telegram bot token", settings.telegram_bot_token is not None,
                        "задан" if settings.telegram_bot_token else "не задан (INVESTBOT_TELEGRAM_BOT_TOKEN)"))
    checks.append(Check("Владелец бота", settings.owner_telegram_id is not None,
                        "задан" if settings.owner_telegram_id else "не задан — бот сообщит ID при /start"))

    tokens = settings.broker_tokens()
    if tokens and sdk:
        from investbot.adapters.tinvest.broker import open_broker

        try:
            async with open_broker(tokens, settings.tinvest_app_name, settings.ca_bundle) as broker:
                accounts = await broker.accounts()
            kinds = ", ".join(sorted({a.kind.value for a in accounts}))
            checks.append(Check("Токен Т-Инвестиций", True, f"только чтение, счетов: {len(accounts)} ({kinds})"))
        except Exception as e:
            checks.append(Check("Токен Т-Инвестиций", False, f"{type(e).__name__}: {e}"))
    else:
        checks.append(Check("Токен Т-Инвестиций", False, "не задан" if not tokens else "нужен SDK"))
    return checks
