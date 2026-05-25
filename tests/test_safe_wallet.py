from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
import requests
from eth_abi import decode as decode_abi
from eth_account import Account

from etherfi_bot.blockscout import BlockscoutJsonRpcError
from etherfi_bot.domain import SafeTxCreateError, SafeTxStatus, SafeTxStatusReadError
from etherfi_bot.safe_wallet import (
    AAVE_V3_ARBITRUM_POOL,
    ARBITRUM_AAVE_NATIVE_USDC_ATOKEN,
    ARBITRUM_NATIVE_USDC,
    AaveV3NativeUsdcWithdrawPreparer,
    SafeTxServiceError,
    SafeTxServiceClient,
    SafeWalletTransactionServiceClient,
    checksum,
    decimal_to_base_units,
    proposer_address_from_key,
)
from tests.conftest import make_user


PRIVATE_KEY = "0x" + "1".zfill(64)


def test_aave_preflight_passes_when_safe_has_enough_ausdc() -> None:
    blockscout = RecordingJsonRpc(result=uint256_hex(2_000_000))
    preparer = AaveV3NativeUsdcWithdrawPreparer(blockscout)

    preparer.preflight_check(
        "0x0000000000000000000000000000000000000001",
        Decimal("1.5"),
        "0x0000000000000000000000000000000000000002",
    )

    assert blockscout.calls == [
        {
            "to": checksum(ARBITRUM_AAVE_NATIVE_USDC_ATOKEN),
            "data": "0x70a08231"
            "0000000000000000000000000000000000000000000000000000000000000001",
        }
    ]


def test_aave_preflight_fails_when_safe_ausdc_balance_is_insufficient() -> None:
    blockscout = RecordingJsonRpc(result=uint256_hex(999_999))
    preparer = AaveV3NativeUsdcWithdrawPreparer(blockscout)

    with pytest.raises(SafeTxCreateError, match="required 1000000"):
        preparer.preflight_check(
            "0x0000000000000000000000000000000000000001",
            Decimal("1"),
            "0x0000000000000000000000000000000000000002",
        )


def test_aave_preflight_wraps_blockscout_errors_as_safe_tx_create_failed() -> None:
    preparer = AaveV3NativeUsdcWithdrawPreparer(FailingJsonRpc())

    with pytest.raises(SafeTxCreateError, match="AAVE preflight balance check failed"):
        preparer.preflight_check(
            "0x0000000000000000000000000000000000000001",
            Decimal("1"),
            "0x0000000000000000000000000000000000000002",
        )


def test_decimal_to_base_units_requires_exact_token_precision() -> None:
    assert decimal_to_base_units(Decimal("1.234567"), 6) == 1_234_567

    with pytest.raises(ValueError, match="more precision"):
        decimal_to_base_units(Decimal("0.0000001"), 6)


def test_aave_prepare_transaction_builds_withdraw_call() -> None:
    preparer = AaveV3NativeUsdcWithdrawPreparer(RecordingJsonRpc(result=uint256_hex(0)))
    target = "0x0000000000000000000000000000000000000002"

    call = preparer.prepare_transaction(
        "0x0000000000000000000000000000000000000001",
        Decimal("12.345678"),
        target,
    )

    assert call.to == checksum(AAVE_V3_ARBITRUM_POOL)
    assert call.value == 0
    assert call.operation == 0
    assert call.data[:4].hex() == "69328dec"
    asset, amount, recipient = decode_abi(
        ["address", "uint256", "address"],
        call.data[4:],
    )
    assert checksum(asset) == checksum(ARBITRUM_NATIVE_USDC)
    assert amount == 12_345_678
    assert checksum(recipient) == checksum(target)


def test_safe_client_creates_top_up_proposal_without_listing_pending_transactions() -> None:
    user = make_user(telegram_user_id=1001)
    proposer = proposer_address_from_key(PRIVATE_KEY)
    session = RecordingSafeSession(
        safe_info={"nonce": "18", "version": "1.3.0+L2"},
        delegates=[{"delegate": proposer.lower()}],
    )
    tx_service = SafeTxServiceClient(
        "safe-api-key",
        base_url="https://safe.test",
        session=session,
    )
    blockscout = RecordingJsonRpc(result=uint256_hex(30_000_000))
    client = SafeWalletTransactionServiceClient(
        tx_service,
        AaveV3NativeUsdcWithdrawPreparer(blockscout),
    )

    safe_tx_hash = client.create_top_up_tx(user, Decimal("17"), PRIVATE_KEY)

    assert safe_tx_hash.startswith("0x")
    assert len(safe_tx_hash) == 66
    get_paths = [request["path"] for request in session.get_requests]
    assert get_paths == [
        f"/api/v1/safes/{checksum(user.safe_account)}/",
        "/api/v2/delegates/",
    ]
    assert not any("/multisig-transactions/" in path for path in get_paths)
    assert len(session.post_requests) == 1
    posted = session.post_requests[0]
    assert posted["path"] == (
        f"/api/v2/safes/{checksum(user.safe_account)}/multisig-transactions/"
    )
    payload = posted["json"]
    assert payload["to"] == checksum(AAVE_V3_ARBITRUM_POOL)
    assert payload["value"] == 0
    assert payload["operation"] == 0
    assert payload["nonce"] == 18
    assert payload["contractTransactionHash"] == safe_tx_hash
    assert payload["sender"] == checksum(Account.from_key(PRIVATE_KEY).address)
    assert payload["signature"] and not payload["signature"].startswith("0x")
    assert payload["origin"] == "ether.fi-bot:1001:top-up"


def test_safe_client_fails_creation_when_proposer_is_not_registered() -> None:
    user = make_user(telegram_user_id=1001)
    session = RecordingSafeSession(
        safe_info={"nonce": "18", "version": "1.3.0+L2"},
        delegates=[],
    )
    client = SafeWalletTransactionServiceClient(
        SafeTxServiceClient("safe-api-key", base_url="https://safe.test", session=session),
        AaveV3NativeUsdcWithdrawPreparer(
            RecordingJsonRpc(result=uint256_hex(30_000_000))
        ),
    )

    with pytest.raises(SafeTxCreateError, match="not registered as proposer"):
        client.create_top_up_tx(user, Decimal("17"), PRIVATE_KEY)


@pytest.mark.parametrize(
    ("method", "expected_message"),
    [
        ("get", "GET failed"),
        ("post", "POST failed"),
    ],
)
def test_safe_tx_service_client_wraps_network_errors(
    method: str,
    expected_message: str,
) -> None:
    service = SafeTxServiceClient(
        "safe-api-key",
        base_url="https://safe.test",
        session=FailingSafeSession(),
    )

    with pytest.raises(SafeTxServiceError, match=expected_message):
        if method == "get":
            service.get("/api/v1/safes/0x0000000000000000000000000000000000000001/")
        else:
            service.post(
                "/api/v2/safes/0x0000000000000000000000000000000000000001/"
                "multisig-transactions/",
                {},
            )


def test_safe_client_wraps_creation_network_errors_as_safe_tx_create_error() -> None:
    user = make_user(telegram_user_id=1001)
    client = SafeWalletTransactionServiceClient(
        SafeTxServiceClient(
            "safe-api-key",
            base_url="https://safe.test",
            session=FailingSafeSession(),
        ),
        AaveV3NativeUsdcWithdrawPreparer(
            RecordingJsonRpc(result=uint256_hex(30_000_000))
        ),
    )

    with pytest.raises(SafeTxCreateError):
        client.create_top_up_tx(user, Decimal("17"), PRIVATE_KEY)


def test_safe_client_wraps_status_network_errors_as_safe_tx_status_read_error() -> None:
    user = make_user(telegram_user_id=1001)
    client = SafeWalletTransactionServiceClient(
        SafeTxServiceClient(
            "safe-api-key",
            base_url="https://safe.test",
            session=FailingSafeSession(),
        ),
        AaveV3NativeUsdcWithdrawPreparer(RecordingJsonRpc(result=uint256_hex(0))),
    )

    with pytest.raises(SafeTxStatusReadError):
        client.get_tx_status(user, "0xsafehash")


@pytest.mark.parametrize(
    ("tx_status", "tx_payload", "safe_info", "expected"),
    [
        (404, {"detail": "not found"}, None, SafeTxStatus.FINAL),
        (
            200,
            {"isExecuted": True, "nonce": 18},
            None,
            SafeTxStatus.FINAL,
        ),
        (
            200,
            {"isExecuted": False, "nonce": 18},
            {"nonce": "19", "version": "1.3.0+L2"},
            SafeTxStatus.FINAL,
        ),
        (
            200,
            {"isExecuted": False, "nonce": 18},
            {"nonce": "18", "version": "1.3.0+L2"},
            SafeTxStatus.PENDING,
        ),
    ],
)
def test_safe_client_maps_transaction_status(
    tx_status: int,
    tx_payload: dict[str, Any],
    safe_info: dict[str, Any] | None,
    expected: SafeTxStatus,
) -> None:
    user = make_user(telegram_user_id=1001)
    session = RecordingSafeSession(
        safe_info=safe_info,
        tx_response=FakeSafeResponse(tx_status, tx_payload),
    )
    client = SafeWalletTransactionServiceClient(
        SafeTxServiceClient("safe-api-key", base_url="https://safe.test", session=session),
        AaveV3NativeUsdcWithdrawPreparer(RecordingJsonRpc(result=uint256_hex(0))),
    )

    assert client.get_tx_status(user, "0xsafehash") is expected


def test_safe_client_wraps_status_read_errors() -> None:
    user = make_user(telegram_user_id=1001)
    session = RecordingSafeSession(
        tx_response=FakeSafeResponse(500, {"detail": "server error"}),
    )
    client = SafeWalletTransactionServiceClient(
        SafeTxServiceClient("safe-api-key", base_url="https://safe.test", session=session),
        AaveV3NativeUsdcWithdrawPreparer(RecordingJsonRpc(result=uint256_hex(0))),
    )

    with pytest.raises(SafeTxStatusReadError):
        client.get_tx_status(user, "0xsafehash")


class RecordingJsonRpc:
    def __init__(self, *, result: str) -> None:
        self.result = result
        self.calls: list[dict[str, str]] = []

    def eth_call(self, *, to: str, data: bytes | str, block: str = "latest") -> str:
        del block
        data_hex = data.hex() if isinstance(data, bytes) else data
        if not data_hex.startswith("0x"):
            data_hex = f"0x{data_hex}"
        self.calls.append({"to": to, "data": data_hex})
        return self.result


class FailingJsonRpc:
    def eth_call(self, *, to: str, data: bytes | str, block: str = "latest") -> str:
        del to, data, block
        raise BlockscoutJsonRpcError("unavailable")


class RecordingSafeSession:
    def __init__(
        self,
        *,
        safe_info: dict[str, Any] | None = None,
        delegates: list[dict[str, str]] | None = None,
        tx_response: "FakeSafeResponse | None" = None,
    ) -> None:
        self.headers: dict[str, str] = {}
        self.safe_info = safe_info or {"nonce": "0", "version": "1.3.0+L2"}
        self.delegates = delegates or []
        self.tx_response = tx_response
        self.get_requests: list[dict[str, Any]] = []
        self.post_requests: list[dict[str, Any]] = []

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any],
        timeout: float,
    ) -> "FakeSafeResponse":
        del timeout
        path = url.removeprefix("https://safe.test")
        self.get_requests.append({"path": path, "params": params})
        if path.startswith("/api/v1/safes/"):
            return FakeSafeResponse(200, self.safe_info)
        if path == "/api/v2/delegates/":
            return FakeSafeResponse(200, {"results": self.delegates})
        if path.startswith("/api/v2/multisig-transactions/"):
            return self.tx_response or FakeSafeResponse(404, {"detail": "not found"})
        return FakeSafeResponse(404, {"detail": "not found"})

    def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> "FakeSafeResponse":
        del headers, timeout
        path = url.removeprefix("https://safe.test")
        self.post_requests.append({"path": path, "json": json})
        return FakeSafeResponse(201, None)


class FailingSafeSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}

    def get(self, *args: Any, **kwargs: Any) -> "FakeSafeResponse":
        del args, kwargs
        raise requests.Timeout("safe timed out")

    def post(self, *args: Any, **kwargs: Any) -> "FakeSafeResponse":
        del args, kwargs
        raise requests.ConnectionError("safe unavailable")


class FakeSafeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any] | None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = b"" if payload is None else b"{}"
        self.text = "" if payload is None else str(payload)

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> dict[str, Any]:
        if self._payload is None:
            raise ValueError("empty response")
        return self._payload


def uint256_hex(value: int) -> str:
    return f"0x{value:064x}"
