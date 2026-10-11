#!/usr/bin/env python3
"""SOCKS5-прокси для бота через VPN-подписку (VLESS) на sing-box — когда с сервера нет прямого доступа к Telegram.

Секрет /run/secrets/vpn_subscription — ссылка на подписку (https://…) или ссылки vless:// по одной на строку.
Из подходящих серверов собирается группа urltest: sing-box сам держит живой и быстрый сервер, проверяя задержку
до api.telegram.org. SOCKS5 слушает :1080 только внутри docker-сети и пропускает наружу лишь разрешённые домены
(VPN_ALLOW_DOMAINS, по умолчанию telegram.org) — остальное отклоняется. Подписка перечитывается раз в
VPN_REFRESH_HOURS часов; sing-box перезапускается, только если список серверов изменился.

Только стандартная библиотека: скрипт работает в образе sing-box (Alpine + python3).
"""

from __future__ import annotations

import base64
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit
from urllib.request import Request, urlopen

SECRET = Path(os.environ.get("VPN_SECRET_FILE", "/run/secrets/vpn_subscription"))
CONFIG = Path(os.environ.get("VPN_CONFIG_FILE", "/tmp/sing-box.json"))  # noqa: S108 — tmpfs read-only контейнера
SING_BOX = os.environ.get("VPN_SING_BOX", "/usr/local/bin/sing-box")
PORT = int(os.environ.get("VPN_PORT", "1080"))
ALLOW = [d.strip() for d in os.environ.get("VPN_ALLOW_DOMAINS", "telegram.org").split(",") if d.strip()]
EXCLUDE = os.environ.get("VPN_EXCLUDE", "🇷🇺|Россия|Russia")  # выход в РФ не помогает добраться до Telegram
TEST_URL = os.environ.get("VPN_TEST_URL", "https://api.telegram.org/")
REFRESH_S = float(os.environ.get("VPN_REFRESH_HOURS", "6")) * 3600
USER_AGENT = os.environ.get("VPN_USER_AGENT", "v2rayN/7.0")  # от UA зависит формат ответа панели: нужен список ссылок


def log(msg: str) -> None:
    print(f"vpn: {msg}", file=sys.stderr, flush=True)


def _b64(text: str) -> str:
    return base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_").decode("utf-8")


def read_links(secret: str, fetch: Any = None) -> list[str]:
    """Ссылки vless:// из подписки (URL, base64 или открытый список) или прямо из секрета."""
    body = secret.strip()
    if body.startswith(("http://", "https://")) and "\n" not in body:
        if fetch is None:
            with urlopen(Request(body, headers={"User-Agent": USER_AGENT}), timeout=30) as r:  # noqa: S310
                body = r.read().decode("utf-8", "replace").strip()
        else:
            body = fetch(body).strip()
    if "://" not in body:
        body = _b64(body)
    return [line.strip() for line in body.splitlines() if line.strip().startswith("vless://")]


def vless_outbound(link: str) -> dict[str, Any] | None:
    """vless://uuid@host:port?…#имя → outbound sing-box; None для того, что sing-box не умеет (xhttp и т. п.)."""
    u = urlsplit(link)
    if u.scheme != "vless" or not u.username or not u.hostname or not u.port:
        return None
    q = dict(parse_qsl(u.query))
    out: dict[str, Any] = {"type": "vless", "server": u.hostname, "server_port": u.port, "uuid": unquote(u.username)}
    if q.get("flow"):
        out["flow"] = q["flow"]
    security = q.get("security", "none")
    if security in ("tls", "reality"):
        tls: dict[str, Any] = {"enabled": True, "server_name": q.get("sni") or u.hostname}
        if q.get("fp"):
            tls["utls"] = {"enabled": True, "fingerprint": q["fp"]}
        if q.get("alpn"):
            tls["alpn"] = unquote(q["alpn"]).split(",")
        if security == "reality":
            if not q.get("pbk"):
                return None
            tls["reality"] = {"enabled": True, "public_key": q["pbk"], "short_id": q.get("sid", "")}
            tls.setdefault("utls", {"enabled": True, "fingerprint": "chrome"})  # Reality работает только с uTLS
        out["tls"] = tls
    elif security != "none":
        return None
    transport = q.get("type", "tcp")
    if transport in ("tcp", "raw"):
        if q.get("headerType", "none") != "none":
            return None
    elif transport == "ws":
        ws: dict[str, Any] = {"type": "ws", "path": unquote(q.get("path", "/"))}
        if q.get("host"):
            ws["headers"] = {"Host": q["host"]}
        out["transport"] = ws
    elif transport == "grpc":
        out["transport"] = {"type": "grpc", "service_name": unquote(q.get("serviceName", ""))}
    else:
        return None
    return out


def build_config(links: list[str]) -> tuple[dict[str, Any], int]:
    """Конфиг sing-box и число пропущенных ссылок (неподдерживаемые или исключённые по имени)."""
    outbounds: list[dict[str, Any]] = []
    skipped = 0
    for link in links:
        name = unquote(urlsplit(link).fragment) or "server"
        ob = vless_outbound(link)
        if ob is None or (EXCLUDE and re.search(EXCLUDE, name)):
            skipped += 1
            continue
        ob["tag"] = f"{len(outbounds) + 1:02d} {name}"
        outbounds.append(ob)
    if not outbounds:
        raise ValueError(f"нет подходящих серверов (ссылок: {len(links)}, пропущено: {skipped})")
    config = {
        "log": {"level": "warn", "timestamp": True},
        "dns": {"servers": [{"type": "local", "tag": "local"}]},
        "inbounds": [{"type": "socks", "tag": "socks-in", "listen": "0.0.0.0", "listen_port": PORT}],  # noqa: S104
        "outbounds": [
            {"type": "urltest", "tag": "auto", "outbounds": [o["tag"] for o in outbounds], "url": TEST_URL,
             "interval": "3m", "tolerance": 150},
            *outbounds,
        ],
        "route": {
            "rules": [
                {"inbound": ["socks-in"], "action": "sniff"},  # домен из TLS SNI, даже если клиент прислал IP
                {"domain_suffix": ALLOW, "outbound": "auto"},
                {"inbound": ["socks-in"], "action": "reject"},
            ],
            "default_domain_resolver": "local",
        },
    }
    return config, skipped


def refresh(current: str | None) -> str | None:
    """Новый текст конфига, если серверы изменились и `sing-box check` его принял; иначе None."""
    config, skipped = build_config(read_links(SECRET.read_text(encoding="utf-8")))
    text = json.dumps(config, ensure_ascii=False, indent=1)
    if text == current:
        return None
    candidate = CONFIG.with_suffix(".new.json")
    candidate.write_text(text, encoding="utf-8")
    check = subprocess.run([SING_BOX, "check", "-c", str(candidate)],  # noqa: S603
                           capture_output=True, text=True, check=False)
    if check.returncode != 0:
        raise ValueError(f"sing-box отклонил конфиг: {check.stderr.strip()[-300:]}")
    candidate.replace(CONFIG)
    log(f"серверов: {len(config['outbounds']) - 1}, пропущено: {skipped}; разрешены: {', '.join(ALLOW)}")
    return text


def main() -> int:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    current: str | None = None
    proc: subprocess.Popen[bytes] | None = None
    try:
        while True:
            try:
                if (text := refresh(current)) is not None:
                    current = text
                    if proc is not None:
                        proc.terminate()
                        proc.wait(timeout=10)
                        proc = None
            except Exception as e:  # подписка недоступна — работаем на прежнем списке серверов
                log(f"подписка не обновлена: {type(e).__name__}: {e}")
            deadline = time.monotonic() + (REFRESH_S if current else 60)
            while time.monotonic() < deadline:
                if current and (proc is None or proc.poll() is not None):
                    if proc is not None:
                        log(f"sing-box завершился с кодом {proc.returncode}, перезапуск")
                        time.sleep(5)
                    proc = subprocess.Popen([SING_BOX, "run", "-c", str(CONFIG)])  # noqa: S603
                time.sleep(2)
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()


if __name__ == "__main__":
    sys.exit(main())
