from __future__ import annotations

import json
from decimal import Decimal, localcontext
from typing import Iterable
from urllib.error import HTTPError, URLError

import pytest

from etherfi_bot.blockscout import (
    OPTIMISM_CHAIN_ID,
    USER_AGENT,
    BlockscoutBalanceProvider,
    BlockscoutErc20BalanceReader,
    BlockscoutJsonRpcClient,
    BlockscoutJsonRpcError,
)
from etherfi_bot.domain import BalanceReadError
from etherfi_bot.evm import checksum
from tests.conftest import make_user


def test_blockscout_balance_provider_returns_denominated_balance_from_balance_of() -> None:
    user = make_user(
        telegram_user_id=1001,
        balance_token_address="0x9999999999999999999999999999999999999999",
    )
    opener = RecordingOpener(rpc_result(123_450_000))
    rpc_client = BlockscoutJsonRpcClient(
        "proapi_test",
        chain_id=OPTIMISM_CHAIN_ID,
        opener=opener,
    )
    token_reader = BlockscoutErc20BalanceReader(
        rpc_client,
        decimals_by_token_address={user.balance_token_address: 6},
    )
    provider = BlockscoutBalanceProvider(token_reader)

    balance = provider.get_balance(user)

    assert balance == Decimal("123.45")
    assert str(balance) == "123.45"
    assert opener.timeout_seconds == 10
    request = opener.requests[0]
    assert request.full_url == "https://api.blockscout.com/10/json-rpc"
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
                "to": checksum(user.balance_token_address),
                "data": "0x70a08231"
                f"000000000000000000000000{user.target_account[2:]}",
            },
            "latest",
        ],
    }


def test_blockscout_balance_provider_preserves_large_balance_precision() -> None:
    provider = BlockscoutBalanceProvider(
        StaticTokenReader(
            balance_base_units=123_456_789_012_345_678_901_234_567_890,
            decimals=18,
        )
    )

    with localcontext() as context:
        context.prec = 10
        balance = provider.get_balance(make_user())

    assert balance == Decimal("123456789012.345678901234567890")
    assert str(balance) == "123456789012.34567890123456789"


def test_blockscout_balance_provider_removes_fractional_zero_scale_for_display() -> None:
    provider = BlockscoutBalanceProvider(
        StaticTokenReader(balance_base_units=1_000_000, decimals=6)
    )

    balance = provider.get_balance(make_user())

    assert balance == Decimal("1")
    assert str(balance) == "1"


def test_blockscout_balance_provider_returns_zero_from_balance_of() -> None:
    provider = BlockscoutBalanceProvider(
        StaticTokenReader(balance_base_units=0, decimals=18)
    )

    assert provider.get_balance(make_user()) == Decimal("0")


def test_blockscout_balance_provider_rejects_invalid_token_decimals() -> None:
    opener = RecordingOpener(payloads=[rpc_result(123), rpc_result(256)])
    token_reader = BlockscoutErc20BalanceReader(
        BlockscoutJsonRpcClient("proapi_test", chain_id=OPTIMISM_CHAIN_ID, opener=opener)
    )
    provider = BlockscoutBalanceProvider(token_reader)

    with pytest.raises(BalanceReadError):
        provider.get_balance(make_user())


def test_blockscout_balance_provider_rejects_empty_balance_of_result() -> None:
    user = make_user()
    opener = RecordingOpener(rpc_raw_result("0x"))
    token_reader = BlockscoutErc20BalanceReader(
        BlockscoutJsonRpcClient("proapi_test", chain_id=OPTIMISM_CHAIN_ID, opener=opener),
        decimals_by_token_address={user.balance_token_address: 18},
    )
    provider = BlockscoutBalanceProvider(token_reader)

    with pytest.raises(BalanceReadError):
        provider.get_balance(user)


@pytest.mark.parametrize(
    "error",
    [
        BlockscoutJsonRpcError("unavailable"),
        ValueError("invalid address"),
    ],
)
def test_blockscout_balance_provider_wraps_reader_errors(error: Exception) -> None:
    provider = BlockscoutBalanceProvider(FailingTokenReader(error))

    with pytest.raises(BalanceReadError):
        provider.get_balance(make_user())


def test_blockscout_erc20_balance_reader_preloads_and_caches_decimals() -> None:
    token_address = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
    opener = RecordingOpener(rpc_result(6))
    reader = BlockscoutErc20BalanceReader(
        BlockscoutJsonRpcClient("proapi_test", chain_id=OPTIMISM_CHAIN_ID, opener=opener)
    )

    reader.preload_decimals([token_address, token_address.lower()])

    assert reader.get_decimals(token_address) == 6
    assert len(opener.requests) == 1
    request_body = json.loads(opener.requests[0].data.decode("utf-8"))
    assert request_body["params"] == [
        {
            "to": checksum(token_address),
            "data": "0x313ce567",
        },
        "latest",
    ]


@pytest.mark.parametrize("result", ["0x", "0x00", "0x" + ("00" * 31)])
def test_blockscout_erc20_balance_reader_rejects_short_uint256_results(
    result: str,
) -> None:
    token_address = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
    opener = RecordingOpener(rpc_raw_result(result))
    reader = BlockscoutErc20BalanceReader(
        BlockscoutJsonRpcClient("proapi_test", chain_id=OPTIMISM_CHAIN_ID, opener=opener)
    )

    with pytest.raises(ValueError, match="exactly 32 bytes"):
        reader.preload_decimals([token_address])


def test_blockscout_json_rpc_client_sends_eth_call_request_and_returns_result() -> None:
    opener = RecordingOpener(rpc_result(123))
    client = BlockscoutJsonRpcClient("proapi_test", opener=opener)

    result = client.eth_call(
        to="0x724dc807b04555b71ed48a6896b6F41593b8C637",
        data=b"\x12\x34",
    )

    assert result == uint256_hex(123)
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


def test_blockscout_json_rpc_client_retries_transient_failures_with_backoff() -> None:
    unavailable = URLError("network unavailable")
    opener = SequencedOpener([unavailable, unavailable, rpc_result(123)])
    delays: list[float] = []
    client = BlockscoutJsonRpcClient(
        "proapi_test",
        max_attempts=3,
        retry_initial_delay_seconds=0.25,
        retry_backoff_factor=2,
        opener=opener,
        sleeper=delays.append,
    )

    result = client.eth_call(
        to="0x724dc807b04555b71ed48a6896b6F41593b8C637", data="0x1234"
    )

    assert result == uint256_hex(123)
    assert len(opener.requests) == 3
    assert delays == [0.25, 0.5]


def test_blockscout_json_rpc_client_does_not_retry_non_transient_http_errors() -> None:
    opener = SequencedOpener(
        [
            HTTPError(
                "https://api.blockscout.com",
                401,
                "Unauthorized",
                None,
                None,
            )
        ]
    )
    delays: list[float] = []
    client = BlockscoutJsonRpcClient(
        "proapi_test",
        max_attempts=3,
        opener=opener,
        sleeper=delays.append,
    )

    with pytest.raises(BlockscoutJsonRpcError, match="HTTP 401 after 1 attempt"):
        client.eth_call(
            to="0x724dc807b04555b71ed48a6896b6F41593b8C637", data="0x1234"
        )

    assert len(opener.requests) == 1
    assert delays == []


@pytest.mark.parametrize(
    ("parameter", "value"),
    [
        ("retry_initial_delay_seconds", float("nan")),
        ("retry_initial_delay_seconds", float("inf")),
        ("retry_backoff_factor", float("nan")),
        ("retry_backoff_factor", float("inf")),
    ],
)
def test_blockscout_json_rpc_client_rejects_non_finite_retry_configuration(
    parameter: str,
    value: float,
) -> None:
    arguments = {parameter: value}

    with pytest.raises(ValueError, match="finite"):
        BlockscoutJsonRpcClient("proapi_test", **arguments)


def test_blockscout_balance_provider_preserves_final_retry_error_details() -> None:
    provider = BlockscoutBalanceProvider(
        BlockscoutErc20BalanceReader(
            BlockscoutJsonRpcClient(
                "proapi_test",
                max_attempts=2,
                retry_initial_delay_seconds=0,
                opener=SequencedOpener(
                    [URLError("network unavailable"), URLError("network unavailable")]
                ),
            ),
            decimals_by_token_address={make_user().balance_token_address: 6},
        )
    )

    with pytest.raises(BalanceReadError, match="after 2 attempts"):
        provider.get_balance(make_user())


@pytest.mark.parametrize(
    "opener_factory",
    [
        lambda: RecordingOpener(raw_body="{not-json"),
        lambda: RecordingOpener({"jsonrpc": "2.0", "id": 1, "result": 123}),
        lambda: RecordingOpener({"jsonrpc": "2.0", "id": 1, "error": {"code": -1}}),
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


class StaticTokenReader:
    def __init__(self, *, balance_base_units: int, decimals: int) -> None:
        self.balance_base_units = balance_base_units
        self.decimals = decimals
        self.preloaded: list[str] = []

    def get_balance_base_units(self, token_address: str, account_address: str) -> int:
        del token_address, account_address
        return self.balance_base_units

    def get_decimals(self, token_address: str) -> int:
        del token_address
        return self.decimals

    def preload_decimals(self, token_addresses: Iterable[str]) -> None:
        self.preloaded.extend(token_addresses)


class FailingTokenReader:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def get_balance_base_units(self, token_address: str, account_address: str) -> int:
        del token_address, account_address
        raise self.error

    def get_decimals(self, token_address: str) -> int:
        del token_address
        return 6

    def preload_decimals(self, token_addresses: Iterable[str]) -> None:
        del token_addresses


class RecordingOpener:
    def __init__(
        self,
        payload: object | None = None,
        *,
        payloads: list[object] | None = None,
        raw_body: str | None = None,
        error: Exception | None = None,
    ) -> None:
        self.requests = []
        self.timeout_seconds: float | None = None
        if payloads is not None:
            self._raw_bodies = [json.dumps(item) for item in payloads]
        else:
            body = raw_body if raw_body is not None else json.dumps(payload or [])
            self._raw_bodies = [body]
        self._error = error

    def __call__(self, request, *, timeout: float):
        self.requests.append(request)
        self.timeout_seconds = timeout
        if self._error is not None:
            raise self._error
        if not self._raw_bodies:
            raise AssertionError("no response queued")
        return FakeResponse(self._raw_bodies.pop(0))


class SequencedOpener:
    def __init__(self, outcomes: list[Exception | dict[str, object]]) -> None:
        self._outcomes = outcomes
        self.requests = []

    def __call__(self, request, *, timeout: float):
        del timeout
        self.requests.append(request)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(json.dumps(outcome))


class FakeResponse:
    def __init__(self, body: str) -> None:
        self._body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body.encode("utf-8")


def rpc_result(value: int) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": 1, "result": uint256_hex(value)}


def rpc_raw_result(result: str) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": 1, "result": result}


def uint256_hex(value: int) -> str:
    return f"0x{value:064x}"


def _normalized_headers(items: list[tuple[str, str]]) -> dict[str, str]:
    return {key.lower(): value for key, value in items}
