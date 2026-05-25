from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from etherfi_bot import runtime as runtime_module
from etherfi_bot.private_keys import FilePrivateKeyProvider, SECP256K1_ORDER
from etherfi_bot.runtime import build_runtime
from etherfi_bot.safe_wallet import SafeWalletTransactionServiceClient
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
        safe_transaction_service_api_key="safe-api-key",
        telegram_api_base_url="http://127.0.0.1",
        config_path=config_path,
        state_dir=tmp_path / "states",
        polling_offset_path=tmp_path / "polling-offset.json",
        polling_pending_update_path=tmp_path / "polling-pending-update.json",
    )

    with pytest.raises(FileNotFoundError):
        build_runtime(settings)


def test_build_runtime_wires_real_safe_wallet_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_path = tmp_path / "safe_proposer_private_key"
    key_path.write_text(f"{1:064x}", encoding="utf-8")
    user = replace(
        make_user(telegram_user_id=1001),
        safe_proposer_key_file=str(key_path),
    )
    config_path = write_config(tmp_path / "config.json", [user])
    settings = RuntimeSettings(
        bot_token="123:ABC",
        blockscout_pro_api_key="proapi_test",
        safe_transaction_service_api_key="safe-api-key",
        telegram_api_base_url="http://127.0.0.1",
        config_path=config_path,
        state_dir=tmp_path / "states",
        polling_offset_path=tmp_path / "polling-offset.json",
        polling_pending_update_path=tmp_path / "polling-pending-update.json",
    )
    rpc_clients: list[FakeBlockscoutJsonRpcClient] = []
    token_readers: list[FakeBlockscoutErc20BalanceReader] = []
    FakeBlockscoutJsonRpcClient.created = rpc_clients
    FakeBlockscoutErc20BalanceReader.created = token_readers
    monkeypatch.setattr(
        runtime_module,
        "BlockscoutJsonRpcClient",
        FakeBlockscoutJsonRpcClient,
    )
    monkeypatch.setattr(
        runtime_module,
        "BlockscoutErc20BalanceReader",
        FakeBlockscoutErc20BalanceReader,
    )

    components = build_runtime(settings)

    assert isinstance(components.safe_wallet, SafeWalletTransactionServiceClient)
    assert [client.chain_id for client in rpc_clients] == ["10", "42161"]
    assert token_readers[0].preloaded_token_addresses == [{user.balance_token_address}]


class FakeBlockscoutJsonRpcClient:
    created: list["FakeBlockscoutJsonRpcClient"] = []

    def __init__(self, api_key: str, *, chain_id: str) -> None:
        self.api_key = api_key
        self.chain_id = str(chain_id)
        self.created.append(self)


class FakeBlockscoutErc20BalanceReader:
    created: list["FakeBlockscoutErc20BalanceReader"] = []

    def __init__(self, rpc_client: FakeBlockscoutJsonRpcClient) -> None:
        self.rpc_client = rpc_client
        self.preloaded_token_addresses: list[set[str]] = []
        self.created.append(self)

    def get_balance_base_units(self, token_address: str, account_address: str) -> int:
        del token_address, account_address
        return 0

    def get_decimals(self, token_address: str) -> int:
        del token_address
        return 6

    def preload_decimals(self, token_addresses) -> None:
        self.preloaded_token_addresses.append(set(token_addresses))
