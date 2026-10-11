#!/usr/bin/env python3
"""SOCKS5-прокси для бота через VPN-подписку (VLESS) на sing-box — когда с сервера нет прямого доступа к Telegram.

Секрет /run/secrets/vpn_subscription — по строке на запись: ссылка на подписку (https://…) и/или ключи vless://.
Ключи — стартовый список: из российских дата-центров адрес подписки часто закрыт (DPI по имени сайта), тогда бот
стартует на них, а подписку перечитывает уже через сам VPN (внутренний HTTP-вход 127.0.0.1:1081).

Из подходящих серверов собирается группа urltest: sing-box сам держит живой и быстрый сервер, проверяя задержку
до api.telegram.org. SOCKS5 слушает :1080 только внутри docker-сети и пропускает наружу лишь разрешённые домены
(VPN_ALLOW_DOMAINS, по умолчанию telegram.org) — остальное отклоняется. Подписка перечитывается раз в
VPN_REFRESH_HOURS часов; sing-box перезапускается, только если список серверов изменился.

Только стандартная библиотека: скрипт работает в образе sing-box (Alpine + python3). Файл секрета собирается им же
на машине, где подписка открывается: `python3 docker/vpn/vpn.py --secret 'https://…' > secrets/vpn_subscription`.
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
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

SECRET = Path(os.environ.get("VPN_SECRET_FILE", "/run/secrets/vpn_subscription"))
CONFIG = Path(os.environ.get("VPN_CONFIG_FILE", "/tmp/sing-box.json"))  # noqa: S108 — tmpfs read-only контейнера
SING_BOX = os.environ.get("VPN_SING_BOX", "/usr/local/bin/sing-box")
PORT = int(os.environ.get("VPN_PORT", "1080"))
SUB_PORT = 1081  # HTTP-вход только для самого скрипта: загрузка подписки через VPN
ALLOW = [d.strip() for d in os.environ.get("VPN_ALLOW_DOMAINS", "telegram.org").split(",") if d.strip()]
EXCLUDE = os.environ.get("VPN_EXCLUDE", "🇷🇺|Россия|Russia")  # выход в РФ не помогает добраться до Telegram
TEST_URL = os.environ.get("VPN_TEST_URL", "https://api.telegram.org/")
REFRESH_S = float(os.environ.get("VPN_REFRESH_HOURS", "6")) * 3600
USER_AGENT = os.environ.get("VPN_USER_AGENT", "v2rayN/7.0")  # от UA зависит формат ответа панели: нужен список ссылок

Fetch = Callable[[str, bool], str]  # (адрес подписки, через VPN) → тело ответа


def log(msg: str) -> None:
    print(f"vpn: {msg}", file=sys.stderr, flush=True)


def _b64(text: str) -> str:
    return base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_").decode("utf-8")


def decode_links(body: str) -> list[str]:
    """Ссылки vless:// из ответа подписки: base64 или открытый список."""
    body = body.strip()
    if body and "://" not in body:
        body = _b64(body)
    return [line.strip() for line in body.splitlines() if line.strip().startswith("vless://")]


def parse_secret(secret: str) -> tuple[list[str], list[str]]:
    """(адреса подписок, стартовые ключи vless://) из секрета."""
    urls = [s.strip() for s in secret.splitlines() if s.strip().startswith(("http://", "https://"))]
    links = [s.strip() for s in secret.splitlines() if s.strip().startswith("vless://")]
    if not urls and not links and secret.strip():  # содержимое подписки целиком (base64)
        links = decode_links(secret)
    return urls, links


def http_fetch(url: str, via_vpn: bool) -> str:
    proxies = {"http": f"http://127.0.0.1:{SUB_PORT}", "https": f"http://127.0.0.1:{SUB_PORT}"} if via_vpn else {}
    opener = build_opener(ProxyHandler(proxies))  # без via_vpn — напрямую, переменные *_proxy игнорируются
    with opener.open(Request(url, headers={"User-Agent": USER_AGENT}), timeout=30) as r:  # noqa: S310 — http(s) из секрета
        return str(r.read().decode("utf-8", "replace"))


def read_links(secret: str, *, vpn_up: bool = False, fetch: Fetch = http_fetch) -> tuple[list[str], bool]:
    """Ключи для конфига и признак «из подписки» (False — стартовые ключи из секрета: подписка недоступна)."""
    urls, bootstrap = parse_secret(secret)
    errors: list[str] = []
    for url in urls:
        for via_vpn in ((False, True) if vpn_up else (False,)):
            try:
                links = decode_links(fetch(url, via_vpn))
            except Exception as e:
                errors.append(f"{'через VPN' if via_vpn else 'напрямую'}: {type(e).__name__}: {e}")
                continue
            if links:
                if via_vpn:
                    log("подписка загружена через VPN (напрямую с сервера недоступна)")
                return links, True
            errors.append("в ответе нет ключей vless://")
    if errors:
        log("подписка не загружена — " + "; ".join(errors))
    if bootstrap:
        return bootstrap, not urls
    if urls:
        raise ValueError("подписка недоступна, а стартовых ключей vless:// в секрете нет — добавьте их "
                         "(docs/DEPLOY.md, раздел 9.1)")
    raise ValueError("в секрете нет ни ссылки на подписку, ни ключей vless://")


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
        "inbounds": [
            {"type": "socks", "tag": "socks-in", "listen": "0.0.0.0", "listen_port": PORT},  # noqa: S104
            {"type": "mixed", "tag": "sub-in", "listen": "127.0.0.1", "listen_port": SUB_PORT},
        ],
        "outbounds": [
            {"type": "urltest", "tag": "auto", "outbounds": [o["tag"] for o in outbounds], "url": TEST_URL,
             "interval": "3m", "tolerance": 150},
            *outbounds,
        ],
        "route": {
            "rules": [
                {"inbound": ["sub-in"], "outbound": "auto"},  # загрузка подписки самим скриптом
                {"inbound": ["socks-in"], "action": "sniff"},  # домен из TLS SNI, даже если клиент прислал IP
                {"domain_suffix": ALLOW, "outbound": "auto"},
                {"inbound": ["socks-in"], "action": "reject"},
            ],
            "default_domain_resolver": "local",
        },
    }
    return config, skipped


def refresh(current: str | None, vpn_up: bool) -> tuple[str | None, bool]:
    """(новый текст конфига или None, если серверы не изменились; ключи из подписки, а не стартовые)."""
    links, fresh = read_links(SECRET.read_text(encoding="utf-8"), vpn_up=vpn_up)
    config, skipped = build_config(links)
    text = json.dumps(config, ensure_ascii=False, indent=1)
    if text == current:
        return None, fresh
    candidate = CONFIG.with_suffix(".new.json")
    candidate.write_text(text, encoding="utf-8")
    check = subprocess.run([SING_BOX, "check", "-c", str(candidate)],  # noqa: S603
                           capture_output=True, text=True, check=False)
    if check.returncode != 0:
        raise ValueError(f"sing-box отклонил конфиг: {check.stderr.strip()[-300:]}")
    candidate.replace(CONFIG)
    source = "подписка" if fresh else "стартовые ключи из секрета"
    log(f"серверов: {len(config['outbounds']) - 1}, пропущено: {skipped} ({source}); разрешены: {', '.join(ALLOW)}")
    return text, fresh


def export_secret(url: str) -> int:
    """`python3 vpn.py --secret URL > secrets/vpn_subscription` — на машине, где подписка открывается."""
    links = decode_links(http_fetch(url, via_vpn=False))
    if not links:
        log("в подписке нет ключей vless://")
        return 1
    print(url, *links, sep="\n")
    log(f"ключей: {len(links)}")
    return 0


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--secret":
        return export_secret(sys.argv[2])
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    current: str | None = None
    proc: subprocess.Popen[bytes] | None = None
    try:
        while True:
            vpn_up = proc is not None and proc.poll() is None
            wait_s = REFRESH_S
            try:
                text, fresh = refresh(current, vpn_up)
                if text is not None:
                    current = text
                    if proc is not None:
                        proc.terminate()
                        proc.wait(timeout=10)
                        proc = None
                if not fresh:  # работаем на ключах из секрета: подписку пробуем через VPN, как только он поднимется
                    wait_s = 1800 if vpn_up else 60
            except Exception as e:  # подписка недоступна — работаем на прежнем списке серверов
                log(f"конфиг не обновлён: {type(e).__name__}: {e}")
                if current is None:
                    wait_s = 60
            deadline = time.monotonic() + wait_s
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
