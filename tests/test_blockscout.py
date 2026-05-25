from __future__ import annotations

import json
from decimal import Decimal, localcontext
from urllib.error import HTTPError, URLError

import pytest

from etherfi_bot.blockscout import (
    BlockscoutBalanceProvider,
    BlockscoutJsonRpcClient,
    BlockscoutJsonRpcError,
    USER_AGENT,
)
from etherfi_bot.domain import BalanceReadError
from tests.conftest import make_user


def test_blockscout_balance_provider_returns_denominated_balance_and_sends_headers() -> None:
    user = make_user(
        telegram_user_id=1001,
        balance_token_address="0x9999999999999999999999999999999999999999",
    )
    opener = RecordingOpener(
        [
            {
                "token": {
                    "address_hash": "0x8888888888888888888888888888888888888888",
                    "decimals": "18",
                },
                "value": "1",
            },
            {
                "token": {
                    "address_hash": "0x9999999999999999999999999999999999999999".upper(),
                    "decimals": "6",
                },
                "value": "123450000",
            },
        ]
    )
    provider = BlockscoutBalanceProvider("proapi_test", opener=opener)

    balance = provider.get_balance(user)

    assert balance == Decimal("123.45")
    assert str(balance) == "123.45"
    assert opener.timeout_seconds == 10
    request = opener.requests[0]
    assert request.full_url == (
        f"https://api.blockscout.com/10/api/v2/addresses/"
        f"{user.target_account}/token-balances"
    )
    headers = _normalized_headers(request.header_items())
    assert headers["authorization"] == "Bearer proapi_test"
    assert headers["accept"] == "application/json"
    assert headers["user-agent"] == USER_AGENT


def test_blockscout_balance_provider_preserves_large_balance_precision() -> None:
    user = make_user(
        telegram_user_id=1001,
        balance_token_address="0x9999999999999999999999999999999999999999",
    )
    opener = RecordingOpener(
        [
            {
                "token": {
                    "address_hash": "0x9999999999999999999999999999999999999999",
                    "decimals": "18",
                },
                "value": "123456789012345678901234567890",
            }
        ]
    )
    provider = BlockscoutBalanceProvider("proapi_test", opener=opener)

    with localcontext() as context:
        context.prec = 10
        balance = provider.get_balance(user)

    assert balance == Decimal("123456789012.345678901234567890")
    assert str(balance) == "123456789012.34567890123456789"


def test_blockscout_balance_provider_removes_fractional_zero_scale_for_display() -> None:
    user = make_user(
        telegram_user_id=1001,
        balance_token_address="0x9999999999999999999999999999999999999999",
    )
    opener = RecordingOpener(
        [
            {
                "token": {
                    "address_hash": "0x9999999999999999999999999999999999999999",
                    "decimals": "6",
                },
                "value": "1000000",
            }
        ]
    )
    provider = BlockscoutBalanceProvider("proapi_test", opener=opener)

    balance = provider.get_balance(user)

    assert balance == Decimal("1")
    assert str(balance) == "1"


def test_blockscout_balance_provider_returns_zero_when_token_is_absent() -> None:
    user = make_user(
        telegram_user_id=1001,
        balance_token_address="0x9999999999999999999999999999999999999999",
    )
    opener = RecordingOpener(
        [
            {
                "token": {
                    "address_hash": "0x8888888888888888888888888888888888888888",
                    "decimals": "18",
                },
                "value": "1000000000000000000",
            }
        ]
    )
    provider = BlockscoutBalanceProvider("proapi_test", opener=opener)

    assert provider.get_balance(user) == Decimal("0")


def test_blockscout_balance_provider_rejects_matched_token_without_decimals() -> None:
    user = make_user(
        telegram_user_id=1001,
        balance_token_address="0x9999999999999999999999999999999999999999",
    )
    opener = RecordingOpener(
        [
            {
                "token": {
                    "address_hash": "0x9999999999999999999999999999999999999999",
                },
                "value": "123450000",
            }
        ]
    )
    provider = BlockscoutBalanceProvider("proapi_test", opener=opener)

    with pytest.raises(BalanceReadError):
        provider.get_balance(user)


@pytest.mark.parametrize(
    "opener_factory",
    [
        lambda: RecordingOpener(raw_body="{not-json"),
        lambda: RecordingOpener(raw_body=""),
        lambda: RecordingOpener(
            error=HTTPError(
                "https://api.blockscout.com",
                401,
                "Unauthorized",
                None,
                None,
            )
        ),
        lambda: RecordingOpener(
            error=HTTPError(
                "https://api.blockscout.com",
                403,
                "Forbidden",
                None,
                None,
            )
        ),
        lambda: RecordingOpener(error=URLError("network unavailable")),
    ],
)
def test_blockscout_balance_provider_wraps_response_and_request_errors(
    opener_factory,
) -> None:
    opener = opener_factory()
    provider = BlockscoutBalanceProvider("proapi_test", opener=opener)

    with pytest.raises(BalanceReadError):
        provider.get_balance(make_user())


def test_blockscout_json_rpc_client_sends_eth_call_request_and_returns_result() -> None:
    opener = RecordingOpener(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": "0x000000000000000000000000000000000000000000000000000000000000007b",
        }
    )
    client = BlockscoutJsonRpcClient("proapi_test", opener=opener)

    result = client.eth_call(
        to="0x724dc807b04555b71ed48a6896b6F41593b8C637",
        data=b"\x12\x34",
    )

    assert result == "0x000000000000000000000000000000000000000000000000000000000000007b"
    request = opener.requests[0]
    assert request.full_url == "https://api.blockscout.com/42161/json-rpc"
    headers = _normalized_headers(request.header_items())
    assert headers["authorization"] == "Bearer proapi_test"
    assert headers["accept"] == "application/json"
    assert headers["content-type"] == "application/json"
    assert headers["user-agent"] == USER_AGENT
    body = json.loads(request.data.decode("utf-8"))
    assert body == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [
            {
                "to": "0x724dc807b04555b71ed48a6896b6F41593b8C637",
                "data": "0x1234",
            },
            "latest",
        ],
    }


@pytest.mark.parametrize(
    "opener_factory",
    [
        lambda: RecordingOpener(raw_body="{not-json"),
        lambda: RecordingOpener({"jsonrpc": "2.0", "id": 1, "result": 123}),
        lambda: RecordingOpener({"jsonrpc": "2.0", "id": 1, "error": {"code": -1}}),
        lambda: RecordingOpener(
            error=HTTPError(
                "https://api.blockscout.com",
                500,
                "Internal Server Error",
                None,
                None,
            )
        ),
        lambda: RecordingOpener(error=URLError("network unavailable")),
    ],
)
def test_blockscout_json_rpc_client_wraps_response_and_request_errors(
    opener_factory,
) -> None:
    client = BlockscoutJsonRpcClient("proapi_test", opener=opener_factory())

    with pytest.raises(BlockscoutJsonRpcError):
        client.eth_call(to="0x724dc807b04555b71ed48a6896b6F41593b8C637", data="0x1234")


class RecordingOpener:
    def __init__(
        self,
        payload: object | None = None,
        *,
        raw_body: str | None = None,
        error: Exception | None = None,
    ) -> None:
        self.requests = []
        self.timeout_seconds: float | None = None
        self._raw_body = raw_body if raw_body is not None else json.dumps(payload or [])
        self._error = error

    def __call__(self, request, *, timeout: float):
        self.requests.append(request)
        self.timeout_seconds = timeout
        if self._error is not None:
            raise self._error
        return FakeResponse(self._raw_body)


class FakeResponse:
    def __init__(self, body: str) -> None:
        self._body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body.encode("utf-8")


def _normalized_headers(items: list[tuple[str, str]]) -> dict[str, str]:
    return {key.lower(): value for key, value in items}
