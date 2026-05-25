from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
import requests
from eth_account import Account

from etherfi_bot.domain import SafeTxCreateError, SafeTxStatus, SafeTxStatusReadError
from etherfi_bot.safe_tx_preparers import SafeTxCall, checksum
from etherfi_bot.safe_wallet import (
    SafeTxServiceError,
    SafeTxServiceClient,
    SafeWalletTransactionServiceClient,
    proposer_address_from_key,
)
from tests.conftest import make_user


PRIVATE_KEY = "0x" + "1".zfill(64)
TOP_UP_TO = "0x00000000000000000000000000000000000000f0"


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
    client = SafeWalletTransactionServiceClient(
        tx_service,
        StaticPreparer(),
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
    assert payload["to"] == checksum(TOP_UP_TO)
    assert payload["value"] == 0
    assert payload["data"] == "0x1234"
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
        StaticPreparer(),
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
        StaticPreparer(),
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
        StaticPreparer(),
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
        StaticPreparer(),
    )

    assert client.get_tx_status(user, "0xsafehash") is expected


def test_safe_client_wraps_status_read_errors() -> None:
    user = make_user(telegram_user_id=1001)
    session = RecordingSafeSession(
        tx_response=FakeSafeResponse(500, {"detail": "server error"}),
    )
    client = SafeWalletTransactionServiceClient(
        SafeTxServiceClient("safe-api-key", base_url="https://safe.test", session=session),
        StaticPreparer(),
    )

    with pytest.raises(SafeTxStatusReadError):
        client.get_tx_status(user, "0xsafehash")


class StaticPreparer:
    def preflight_check(
        self,
        safe_address: str,
        amount: Decimal,
        target_account: str,
    ) -> None:
        del safe_address, amount, target_account

    def prepare_transaction(
        self,
        safe_address: str,
        amount: Decimal,
        target_account: str,
    ) -> SafeTxCall:
        del safe_address, amount, target_account
        return SafeTxCall(to=TOP_UP_TO, value=0, data=b"\x12\x34", operation=0)


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
