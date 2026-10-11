from pathlib import Path

import pytest

from investbot.settings import Settings


def test_empty_env_values_mean_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # docker compose передаёт пустые строки из .env как "", а не пропускает их — первый запуск без ID владельца
    monkeypatch.chdir(tmp_path)
    for key in ("OWNER_TELEGRAM_ID", "TELEGRAM_PROXY", "TINVEST_TOKEN", "DATABASE_URL"):
        monkeypatch.setenv(f"INVESTBOT_{key}", "")
    s = Settings()
    assert s.owner_telegram_id is None
    assert s.telegram_proxy is None
    assert s.tinvest_token is None
    assert s.database_url is None
