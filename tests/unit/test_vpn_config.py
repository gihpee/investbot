"""Сборка конфига sing-box из VPN-подписки (docker/vpn/vpn.py)."""

import base64
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

REALITY = ("vless://11111111-2222-3333-4444-555555555555@nl.example.net:443?type=tcp&security=reality"
           "&pbk=PUBKEY&sid=ab12&sni=www.example.com&fp=chrome&flow=xtls-rprx-vision#%F0%9F%87%B3%F0%9F%87%B1%20NL")
WS_TLS = "vless://uuid-ws@de.example.net:8443?type=ws&security=tls&path=%2Fws&host=cdn.example.net&sni=cdn.example.net#DE"
XHTTP = "vless://uuid-x@kz.example.net:443?type=xhttp&security=reality&pbk=K&sid=1#KZ"
RU = "vless://uuid-ru@ru.example.net:443?type=tcp&security=reality&pbk=K&sid=1#%F0%9F%87%B7%F0%9F%87%BA%20Россия"


@pytest.fixture(scope="module")
def vpn() -> ModuleType:
    path = Path(__file__).parents[2] / "docker" / "vpn" / "vpn.py"
    spec = importlib.util.spec_from_file_location("vpn_script", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reality_link_to_outbound(vpn: ModuleType) -> None:
    ob = vpn.vless_outbound(REALITY)
    assert ob["server"] == "nl.example.net" and ob["server_port"] == 443
    assert ob["uuid"] == "11111111-2222-3333-4444-555555555555"
    assert ob["flow"] == "xtls-rprx-vision"
    assert ob["tls"]["server_name"] == "www.example.com"
    assert ob["tls"]["reality"] == {"enabled": True, "public_key": "PUBKEY", "short_id": "ab12"}
    assert ob["tls"]["utls"]["fingerprint"] == "chrome"
    assert "transport" not in ob


def test_ws_tls_link(vpn: ModuleType) -> None:
    ob = vpn.vless_outbound(WS_TLS)
    assert ob["transport"] == {"type": "ws", "path": "/ws", "headers": {"Host": "cdn.example.net"}}
    assert "reality" not in ob["tls"]


def test_unsupported_transport_skipped(vpn: ModuleType) -> None:
    assert vpn.vless_outbound(XHTTP) is None


def test_subscription_base64_and_exclusions(vpn: ModuleType) -> None:
    sub = base64.urlsafe_b64encode("\n".join([REALITY, WS_TLS, XHTTP, RU]).encode()).decode().rstrip("=")
    links, fresh = vpn.read_links("https://sub.example.net/token", fetch=lambda _url, _via: sub)
    assert len(links) == 4 and fresh
    config, skipped = vpn.build_config(links)
    assert skipped == 2  # xhttp не поддерживается, выход в РФ исключён по имени
    auto, *servers = config["outbounds"]
    assert auto["type"] == "urltest" and auto["outbounds"] == [s["tag"] for s in servers]
    assert [s["tag"] for s in servers] == ["01 🇳🇱 NL", "02 DE"]
    rules = config["route"]["rules"]
    assert {"domain_suffix": ["telegram.org"], "outbound": "auto"} in rules
    assert rules[-1] == {"inbound": ["socks-in"], "action": "reject"}  # остальное от бота наружу не уходит
    sub_in = next(i for i in config["inbounds"] if i["tag"] == "sub-in")
    assert sub_in["listen"] == "127.0.0.1"  # вход для загрузки подписки — только изнутри контейнера


def test_plain_links_in_secret(vpn: ModuleType) -> None:
    assert vpn.read_links(f"{REALITY}\n{WS_TLS}\n") == ([REALITY, WS_TLS], True)


def _blocked_directly(sub: str):  # type: ignore[no-untyped-def]
    calls: list[bool] = []

    def fetch(_url: str, via_vpn: bool) -> str:
        calls.append(via_vpn)
        if not via_vpn:
            raise TimeoutError("The handshake operation timed out")
        return sub

    return fetch, calls


def test_blocked_subscription_falls_back_to_keys_then_vpn(vpn: ModuleType) -> None:
    secret = f"https://sub.example.net/token\n{REALITY}\n"
    fetch, calls = _blocked_directly(f"{REALITY}\n{WS_TLS}")
    # VPN ещё не поднят: подписка недоступна — стартовые ключи из секрета
    assert vpn.read_links(secret, vpn_up=False, fetch=fetch) == ([REALITY], False)
    assert calls == [False]
    # VPN поднят: подписка загружается через него
    assert vpn.read_links(secret, vpn_up=True, fetch=fetch) == ([REALITY, WS_TLS], True)
    assert calls == [False, False, True]


def test_blocked_subscription_without_keys(vpn: ModuleType) -> None:
    fetch, _ = _blocked_directly("")
    with pytest.raises(ValueError, match="стартовых ключей"):
        vpn.read_links("https://sub.example.net/token", fetch=fetch)


def test_no_usable_servers(vpn: ModuleType) -> None:
    with pytest.raises(ValueError, match="нет подходящих серверов"):
        vpn.build_config([XHTTP, RU])
