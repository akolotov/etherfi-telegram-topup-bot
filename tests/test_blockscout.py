from __future__ import annotations

from decimal import Decimal
from typing import Iterable

import httpx
import pytest

from etherfi_bot.blockscout import (
    USER_AGENT,
    BlockscoutBalanceProvider,
    BlockscoutErc20BalanceReader,
    BlockscoutJsonRpcClient,
    BlockscoutJsonRpcError,
)
from etherfi_bot.domain import BalanceReadError
from tests.conftest import make_user


async def test_balance_provider_denominates_large_precise_balance() -> None:
    provider = BlockscoutBalanceProvider(
        StaticTokenReader(
            balance_base_units=123456789012345678901234567890,
            decimals=6,
        )
    )

    assert await provider.get_balance(make_user()) == Decimal(
        "123456789012345678901234.56789"
    )


async def test_balance_provider_wraps_invalid_and_rpc_errors() -> None:
    for error in [BlockscoutJsonRpcError("unavailable"), ValueError("bad")]:
        provider = BlockscoutBalanceProvider(FailingTokenReader(error))
        with pytest.raises(BalanceReadError):
            await provider.get_balance(make_user())


async def test_erc20_reader_preloads_unique_decimals_and_caches() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": uint256_hex(6)})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = BlockscoutJsonRpcClient("proapi_test", client=http_client)
    reader = BlockscoutErc20BalanceReader(client)
    token = "0x0000000000000000000000000000000000000001"

    await reader.preload_decimals([token, token.lower()])
    assert await reader.get_decimals(token) == 6
    assert len(requests) == 1
    await http_client.aclose()


async def test_json_rpc_client_sends_authenticated_async_eth_call() -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x1234"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = BlockscoutJsonRpcClient(
        "proapi_test", chain_id="10", client=http_client
    )

    assert await client.eth_call(to="0xabc", data=b"\x12\x34") == "0x1234"
    request = captured[0]
    assert str(request.url) == "https://api.blockscout.com/10/json-rpc"
    assert request.headers["Authorization"] == "Bearer proapi_test"
    assert request.headers["User-Agent"] == USER_AGENT
    assert request.headers["Accept"] == "application/json"
    assert request.extensions.get("timeout") is not None
    assert __import__("json").loads(request.content)["params"] == [
        {"to": "0xabc", "data": "0x1234"},
        "latest",
    ]
    await http_client.aclose()


async def test_json_rpc_client_retries_429_and_5xx_with_async_backoff() -> None:
    statuses = [429, 503, 200]
    delays: list[float] = []

    async def handler(_request: httpx.Request) -> httpx.Response:
        status = statuses.pop(0)
        if status == 200:
            return httpx.Response(200, json={"result": "0x01"})
        return httpx.Response(status, json={"error": "temporary"})

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = BlockscoutJsonRpcClient(
        "proapi_test",
        client=http_client,
        max_attempts=3,
        retry_initial_delay_seconds=0.5,
        retry_backoff_factor=2,
        sleeper=sleeper,
    )

    assert await client.eth_call(to="0xabc", data="0x01") == "0x01"
    assert delays == [0.5, 1.0]
    await http_client.aclose()


async def test_json_rpc_client_uses_unauthenticated_fallback_after_transient_failure(
) -> None:
    primary_requests: list[httpx.Request] = []
    fallback_requests: list[httpx.Request] = []
    delays: list[float] = []

    async def primary_handler(request: httpx.Request) -> httpx.Response:
        primary_requests.append(request)
        return httpx.Response(503, json={"error": "upstream unavailable"})

    async def fallback_handler(request: httpx.Request) -> httpx.Response:
        fallback_requests.append(request)
        return httpx.Response(200, json={"result": "0x1234"})

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    primary_client = httpx.AsyncClient(transport=httpx.MockTransport(primary_handler))
    fallback_client = httpx.AsyncClient(transport=httpx.MockTransport(fallback_handler))
    client = BlockscoutJsonRpcClient(
        "proapi_test",
        chain_id="42161",
        fallback_url="https://arb1.arbitrum.io/rpc",
        client=primary_client,
        fallback_client=fallback_client,
        max_attempts=2,
        sleeper=sleeper,
    )

    assert await client.eth_call(to="0xabc", data="0x01") == "0x1234"
    assert len(primary_requests) == 2
    assert len(fallback_requests) == 1
    assert all(
        request.headers["Authorization"] == "Bearer proapi_test"
        for request in primary_requests
    )
    assert "Authorization" not in fallback_requests[0].headers
    assert str(fallback_requests[0].url) == "https://arb1.arbitrum.io/rpc"
    assert fallback_requests[0].headers["User-Agent"] == USER_AGENT
    assert delays == [0.5]
    await primary_client.aclose()
    await fallback_client.aclose()


async def test_json_rpc_client_uses_fallback_for_transient_json_rpc_error() -> None:
    fallback_calls = 0

    async def primary_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"error": {"code": -32603, "message": "upstream internal error"}},
        )

    async def fallback_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal fallback_calls
        fallback_calls += 1
        return httpx.Response(200, json={"result": "0x02"})

    primary_client = httpx.AsyncClient(transport=httpx.MockTransport(primary_handler))
    fallback_client = httpx.AsyncClient(transport=httpx.MockTransport(fallback_handler))
    client = BlockscoutJsonRpcClient(
        "proapi_test",
        fallback_url="https://rpc.example.test",
        client=primary_client,
        fallback_client=fallback_client,
        max_attempts=1,
    )

    assert await client.eth_call(to="0xabc", data="0x01") == "0x02"
    assert fallback_calls == 1
    await primary_client.aclose()
    await fallback_client.aclose()


async def test_json_rpc_client_does_not_retry_non_transient_http_error() -> None:
    calls = 0
    fallback_calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": "invalid key"})

    async def fallback_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal fallback_calls
        fallback_calls += 1
        return httpx.Response(200, json={"result": "0x01"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fallback_client = httpx.AsyncClient(transport=httpx.MockTransport(fallback_handler))
    client = BlockscoutJsonRpcClient(
        "proapi_test",
        fallback_url="https://rpc.example.test",
        client=http_client,
        fallback_client=fallback_client,
    )

    with pytest.raises(BlockscoutJsonRpcError, match="HTTP 401 after 1 attempt"):
        await client.eth_call(to="0xabc", data="0x01")
    assert calls == 1
    assert fallback_calls == 0
    await http_client.aclose()
    await fallback_client.aclose()


async def test_json_rpc_client_rejects_invalid_response() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": "not-hex"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = BlockscoutJsonRpcClient("proapi_test", client=http_client)
    with pytest.raises(BlockscoutJsonRpcError, match="response is invalid"):
        await client.eth_call(to="0xabc", data="0x01")
    await http_client.aclose()


class StaticTokenReader:
    def __init__(self, *, balance_base_units: int, decimals: int) -> None:
        self.balance_base_units = balance_base_units
        self.decimals = decimals

    async def get_balance_base_units(
        self, token_address: str, account_address: str
    ) -> int:
        del token_address, account_address
        return self.balance_base_units

    async def get_decimals(self, token_address: str) -> int:
        del token_address
        return self.decimals

    async def preload_decimals(self, token_addresses: Iterable[str]) -> None:
        del token_addresses


class FailingTokenReader(StaticTokenReader):
    def __init__(self, error: Exception) -> None:
        super().__init__(balance_base_units=0, decimals=6)
        self.error = error

    async def get_balance_base_units(
        self, token_address: str, account_address: str
    ) -> int:
        del token_address, account_address
        raise self.error


def uint256_hex(value: int) -> str:
    return f"0x{value:064x}"
