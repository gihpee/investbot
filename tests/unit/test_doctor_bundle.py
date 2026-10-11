from pathlib import Path

import certifi

from investbot.ops.doctor import bundle_status


def test_empty_bundle_is_reported_not_raised(tmp_path: Path) -> None:
    empty = tmp_path / "ca-bundle.pem"
    empty.write_text("")
    ok, detail = bundle_status(empty)
    assert not ok
    assert "не PEM" in detail


def test_bundle_without_mintsifry_root(tmp_path: Path) -> None:
    ok, detail = bundle_status(Path(certifi.where()))
    assert not ok
    assert "Russian Trusted Root CA" in detail
