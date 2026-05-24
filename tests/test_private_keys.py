from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from etherfi_bot.private_keys import FilePrivateKeyProvider, SECP256K1_ORDER
from etherfi_bot.runtime import build_runtime
from etherfi_bot.settings import RuntimeSettings
from tests.conftest import make_user, write_config


def test_file_private_key_provider_reads_trimmed_valid_key(tmp_path: Path) -> None:
    key = f"{1:064x}"
    path = tmp_path / "safe_proposer_private_key"
    path.write_text(f"\n  {key}\n", encoding="utf-8")

    assert FilePrivateKeyProvider().read_private_key(str(path)) == key


def test_file_private_key_provider_accepts_0x_prefixed_key(tmp_path: Path) -> None:
    key = "0x" + f"{2:064x}"
    path = tmp_path / "safe_proposer_private_key"
    path.write_text(key, encoding="utf-8")

    assert FilePrivateKeyProvider().read_private_key(str(path)) == key


def test_file_private_key_provider_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        FilePrivateKeyProvider().read_private_key(str(tmp_path / "missing"))


def test_file_private_key_provider_rejects_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "safe_proposer_private_key"
    path.write_text(" \n\t", encoding="utf-8")

    with pytest.raises(ValueError, match="empty"):
        FilePrivateKeyProvider().read_private_key(str(path))


@pytest.mark.parametrize(
    "private_key",
    [
        "g" * 64,
        "+" + "0" * 62 + "1",
        "-" + "0" * 62 + "1",
        "1_" + "0" * 62,
    ],
)
def test_file_private_key_provider_rejects_malformed_hex(
    tmp_path: Path,
    private_key: str,
) -> None:
    path = tmp_path / "safe_proposer_private_key"
    path.write_text(private_key, encoding="utf-8")

    with pytest.raises(ValueError, match="32-byte hex"):
        FilePrivateKeyProvider().read_private_key(str(path))


def test_file_private_key_provider_rejects_zero_key(tmp_path: Path) -> None:
    path = tmp_path / "safe_proposer_private_key"
    path.write_text("0" * 64, encoding="utf-8")

    with pytest.raises(ValueError, match="secp256k1 scalar range"):
        FilePrivateKeyProvider().read_private_key(str(path))


def test_file_private_key_provider_rejects_out_of_range_key(tmp_path: Path) -> None:
    path = tmp_path / "safe_proposer_private_key"
    path.write_text(f"{SECP256K1_ORDER:064x}", encoding="utf-8")

    with pytest.raises(ValueError, match="secp256k1 scalar range"):
        FilePrivateKeyProvider().read_private_key(str(path))


def test_build_runtime_validates_configured_private_key_files(tmp_path: Path) -> None:
    user = replace(
        make_user(telegram_user_id=1001),
        safe_proposer_key_file=str(tmp_path / "missing_private_key"),
    )
    config_path = write_config(tmp_path / "config.json", [user])
    settings = RuntimeSettings(
        bot_token="123:ABC",
        blockscout_pro_api_key="proapi_test",
        telegram_api_base_url="http://127.0.0.1",
        config_path=config_path,
        state_dir=tmp_path / "states",
        polling_offset_path=tmp_path / "polling-offset.json",
        polling_pending_update_path=tmp_path / "polling-pending-update.json",
    )

    with pytest.raises(FileNotFoundError):
        build_runtime(settings)
