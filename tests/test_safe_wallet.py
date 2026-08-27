from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
from eth_account import Account

from etherfi_bot.domain import SafeTxCreateError, SafeTxStatus, SafeTxStatusReadError
from etherfi_bot.safe_tx_preparers import SafeTxCall, checksum
from etherfi_bot.safe_wallet import (
    SafeTxServiceClient,
    SafeTxServiceError,
    SafeWalletTransactionServiceClient,
    proposer_address_from_key,
)
from tests.conftest import make_user


PRIVATE_KEY = "0x" + "1".zfill(64)
TOP_UP_TO = "0x00000000000000000000000000000000000000f0"


async def test_safe_client_creates_top_up_proposal_over_async_http() -> None:
    user = make_user(telegram_user_id=1001)
    transport = SafeTransport(
        safe_info={"nonce": "18", "version": "1.3.0+L2"},
        delegates=[{"delegate": proposer_address_from_key(PRIVATE_KEY).lower()}],
    )
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    service = SafeTxServiceClient(
        "safe-api-key", base_url="https://safe.test", client=http_client
    )
    client = SafeWalletTransactionServiceClient(service, StaticPreparer())

    safe_tx_hash = await client.create_top_up_tx(user, Decimal("17"), PRIVATE_KEY)

    assert safe_tx_hash.startswith("0x") and len(safe_tx_hash) == 66
    assert [request.url.path for request in transport.get_requests] == [
        f"/api/v1/safes/{checksum(user.safe_account)}/",
        "/api/v2/delegates/",
    ]
    request = transport.post_requests[0]
    payload = json.loads(request.content)
    assert payload["to"] == checksum(TOP_UP_TO)
    assert payload["data"] == "0x1234"
    assert payload["nonce"] == 18
    assert payload["contractTransactionHash"] == safe_tx_hash
    assert payload["sender"] == checksum(Account.from_key(PRIVATE_KEY).address)
    assert payload["origin"] == "ether.fi-bot:1001:top-up"
    assert request.headers["Authorization"] == "Bearer safe-api-key"
    await http_client.aclose()


async def test_safe_client_rejects_unregistered_proposer() -> None:
    transport = SafeTransport(
        safe_info={"nonce": "18", "version": "1.3.0+L2"}, delegates=[]
    )
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    client = SafeWalletTransactionServiceClient(
        SafeTxServiceClient(
            "safe-api-key", base_url="https://safe.test", client=http_client
        ),
        StaticPreparer(),
    )
    with pytest.raises(SafeTxCreateError, match="not registered as proposer"):
        await client.create_top_up_tx(make_user(), Decimal("17"), PRIVATE_KEY)
    await http_client.aclose()


@pytest.mark.parametrize("method", ["get", "post"])
async def test_safe_service_wraps_async_network_errors(method: str) -> None:
    async def failing(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unavailable", request=request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(failing))
    service = SafeTxServiceClient(
        "safe-api-key", base_url="https://safe.test", client=http_client
    )
    with pytest.raises(SafeTxServiceError, match=method.upper() + " failed"):
        if method == "get":
            await service.get("/api/v1/safes/0x1/")
        else:
            await service.post("/api/v2/safes/0x1/multisig-transactions/", {})
    await http_client.aclose()


@pytest.mark.parametrize(
    ("tx_status", "tx_payload", "safe_info", "expected"),
    [
        (404, {"detail": "not found"}, None, SafeTxStatus.FINAL),
        (200, {"isExecuted": True, "nonce": 18}, None, SafeTxStatus.FINAL),
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
async def test_safe_client_maps_transaction_status(
    tx_status: int,
    tx_payload: dict[str, Any],
    safe_info: dict[str, Any] | None,
    expected: SafeTxStatus,
) -> None:
    transport = SafeTransport(
        safe_info=safe_info,
        tx_status=tx_status,
        tx_payload=tx_payload,
    )
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    client = SafeWalletTransactionServiceClient(
        SafeTxServiceClient(
            "safe-api-key", base_url="https://safe.test", client=http_client
        ),
        StaticPreparer(),
    )
    assert await client.get_tx_status(make_user(), "0xsafehash") is expected
    await http_client.aclose()


async def test_safe_client_wraps_service_errors_by_operation() -> None:
    async def server_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "server error"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(server_error))
    client = SafeWalletTransactionServiceClient(
        SafeTxServiceClient(
            "safe-api-key", base_url="https://safe.test", client=http_client
        ),
        StaticPreparer(),
    )
    with pytest.raises(SafeTxStatusReadError):
        await client.get_tx_status(make_user(), "0xsafehash")
    with pytest.raises(SafeTxCreateError):
        await client.create_top_up_tx(make_user(), Decimal("1"), PRIVATE_KEY)
    await http_client.aclose()


class StaticPreparer:
    async def preflight_check(
        self, safe_address: str, amount: Decimal, target_account: str
    ) -> None:
        del safe_address, amount, target_account

    def prepare_transaction(
        self, safe_address: str, amount: Decimal, target_account: str
    ) -> SafeTxCall:
        del safe_address, amount, target_account
        return SafeTxCall(to=TOP_UP_TO, value=0, data=b"\x12\x34", operation=0)


class SafeTransport:
    def __init__(
        self,
        *,
        safe_info: dict[str, Any] | None = None,
        delegates: list[dict[str, str]] | None = None,
        tx_status: int = 404,
        tx_payload: dict[str, Any] | None = None,
    ) -> None:
        self.safe_info = safe_info
        self.delegates = delegates if delegates is not None else []
        self.tx_status = tx_status
        self.tx_payload = tx_payload or {"detail": "not found"}
        self.get_requests: list[httpx.Request] = []
        self.post_requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            self.post_requests.append(request)
            return httpx.Response(201)
        self.get_requests.append(request)
        path = request.url.path
        if path.startswith("/api/v1/safes/"):
            return httpx.Response(
                200,
                json=self.safe_info or {"nonce": "0", "version": "1.3.0+L2"},
            )
        if path == "/api/v2/delegates/":
            return httpx.Response(200, json={"results": self.delegates})
        if path.startswith("/api/v2/multisig-transactions/"):
            return httpx.Response(self.tx_status, json=self.tx_payload)
        return httpx.Response(404, json={"detail": "not found"})
