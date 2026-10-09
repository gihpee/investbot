"""Настройки из окружения и `.env`. Секреты — только здесь, никогда в логах, чате или контексте LLM."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INVESTBOT_", env_file=".env", extra="ignore")

    deployment_mode: Literal["personal"] = "personal"  # ADR-20: другие режимы заблокированы кодом
    tz: str = "Europe/Moscow"

    telegram_bot_token: SecretStr | None = None
    owner_telegram_id: int | None = None
    telegram_proxy: str | None = None  # http(s)://… туннель к api.telegram.org, если нужен

    # Один токен: INVESTBOT_TINVEST_TOKEN. Несколько (по счетам): INVESTBOT_TINVEST_TOKENS='{"main":"…","iis":"…"}'
    tinvest_token: SecretStr | None = None
    tinvest_tokens: dict[str, SecretStr] = {}
    tinvest_app_name: str = "investbot"
    tinvest_sync: bool = True

    ca_bundle: Path | None = None  # системные корни + Russian Trusted Root CA (investbot certs)
    config_dir: Path = Path("config")
    data_dir: Path = Path("data/local")

    privacy_mode: Literal["weights", "full"] = "weights"  # N4: в Telegram по умолчанию только доли

    @field_validator("telegram_proxy")
    @classmethod
    def _proxy(cls, v: str | None) -> str | None:
        if v and not v.startswith(("http://", "https://", "socks5://")):
            raise ValueError("telegram_proxy: ожидается http://, https:// или socks5://")
        return v

    def broker_tokens(self) -> dict[str, str]:
        tokens = {k: v.get_secret_value() for k, v in self.tinvest_tokens.items()}
        if self.tinvest_token is not None:
            tokens.setdefault("main", self.tinvest_token.get_secret_value())
        return tokens if self.tinvest_sync else {}

    @property
    def ips_path(self) -> Path:
        return self.config_dir / "ips.yaml"

    @property
    def manual_portfolio_path(self) -> Path:
        return self.config_dir / "manual_portfolio.yaml"
