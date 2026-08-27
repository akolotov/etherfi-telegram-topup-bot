from __future__ import annotations

import asyncio
from decimal import Decimal
from math import isfinite
from typing import Any, Awaitable, Callable, Iterable, Protocol
from urllib.parse import quote

import httpx

from etherfi_bot.domain import BalanceReadError, UserConfig
from etherfi_bot.evm import checksum, encode_contract_method, uint256_from_hex


OPTIMISM_CHAIN_ID = "10"
BLOCKSCOUT_BASE_URL = "https://api.blockscout.com"
USER_AGENT = "etherfi-topup-bot/0.1.0"


class Erc20BalanceReader(Protocol):
    async def get_balance_base_units(
        self, token_address: str, account_address: str
    ) -> int:
        """Read one ERC-20 token balance in base units."""

    async def get_decimals(self, token_address: str) -> int:
        """Read or return cached decimals for one ERC-20 token."""

    async def preload_decimals(self, token_addresses: Iterable[str]) -> None:
        """Read decimals for all unique configured tokens ahead of polling."""


class BlockscoutBalanceProvider:
    def __init__(self, token_reader: Erc20BalanceReader) -> None:
        self._token_reader = token_reader

    async def get_balance(self, user: UserConfig) -> Decimal:
        try:
            balance_base_units = await self._token_reader.get_balance_base_units(
                user.balance_token_address, user.target_account
            )
            decimals = await self._token_reader.get_decimals(user.balance_token_address)
            return _decimal_from_raw_token_units(balance_base_units, decimals)
        except BlockscoutJsonRpcError as error:
            raise BalanceReadError(str(error)) from error
        except ValueError as error:
            raise BalanceReadError("Blockscout balance response is invalid") from error


class BlockscoutErc20BalanceReader:
    def __init__(
        self,
        rpc_client: "BlockscoutJsonRpcClient",
        *,
        decimals_by_token_address: dict[str, int] | None = None,
    ) -> None:
        self._rpc_client = rpc_client
        self._decimals_by_token_address: dict[str, int] = {}
        self._decimals_locks: dict[str, asyncio.Lock] = {}
        for token_address, decimals in (decimals_by_token_address or {}).items():
            self._decimals_by_token_address[_token_cache_key(token_address)] = (
                _validate_decimals(decimals)
            )

    async def get_balance_base_units(
        self, token_address: str, account_address: str
    ) -> int:
        data = encode_contract_method(
            "balanceOf", ["address"], [checksum(account_address)]
        )
        raw_balance = await self._rpc_client.eth_call(
            to=checksum(token_address), data=data
        )
        return uint256_from_hex(raw_balance)

    async def get_decimals(self, token_address: str) -> int:
        cache_key = _token_cache_key(token_address)
        if cache_key in self._decimals_by_token_address:
            return self._decimals_by_token_address[cache_key]
        lock = self._decimals_locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            if cache_key not in self._decimals_by_token_address:
                raw_decimals = await self._rpc_client.eth_call(
                    to=checksum(token_address),
                    data=encode_contract_method("decimals", [], []),
                )
                self._decimals_by_token_address[cache_key] = _validate_decimals(
                    uint256_from_hex(raw_decimals)
                )
        return self._decimals_by_token_address[cache_key]

    async def preload_decimals(self, token_addresses: Iterable[str]) -> None:
        unique_addresses = {
            _token_cache_key(token_address): token_address
            for token_address in token_addresses
        }
        await asyncio.gather(
            *(self.get_decimals(address) for address in unique_addresses.values())
        )


class BlockscoutJsonRpcError(RuntimeError):
    """A Blockscout PRO JSON-RPC request failed or returned invalid data."""


class BlockscoutJsonRpcClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BLOCKSCOUT_BASE_URL,
        chain_id: str = "42161",
        timeout_seconds: float = 10,
        max_attempts: int = 3,
        retry_initial_delay_seconds: float = 0.5,
        retry_backoff_factor: float = 2,
        client: httpx.AsyncClient | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if not isfinite(retry_initial_delay_seconds) or retry_initial_delay_seconds < 0:
            raise ValueError("retry_initial_delay_seconds must be finite and >= 0")
        if not isfinite(retry_backoff_factor) or retry_backoff_factor < 1:
            raise ValueError("retry_backoff_factor must be finite and >= 1")
        self._base_url = base_url.rstrip("/")
        self._chain_id = str(chain_id)
        self._max_attempts = max_attempts
        self._retry_initial_delay_seconds = retry_initial_delay_seconds
        self._retry_backoff_factor = retry_backoff_factor
        self._sleeper = sleeper
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._client.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            }
        )

    async def eth_call(
        self,
        *,
        to: str,
        data: bytes | str,
        block: str = "latest",
    ) -> str:
        data_hex = data.hex() if isinstance(data, bytes) else data
        if not data_hex.startswith("0x"):
            data_hex = f"0x{data_hex}"
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": to, "data": data_hex}, block],
        }
        url = f"{self._base_url}/{quote(self._chain_id, safe='')}/json-rpc"
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.post(url, json=payload)
                response.raise_for_status()
            except httpx.HTTPStatusError as error:
                status_code = error.response.status_code
                request_cause: Exception = error
                request_error = BlockscoutJsonRpcError(
                    f"Blockscout JSON-RPC request failed with HTTP {status_code}"
                )
                retryable = status_code == 429 or 500 <= status_code < 600
            except httpx.RequestError as error:
                request_cause = error
                request_error = BlockscoutJsonRpcError(
                    "Blockscout JSON-RPC request failed"
                )
                retryable = True
            else:
                try:
                    return _extract_eth_call_result(response.json())
                except (TypeError, ValueError) as error:
                    raise BlockscoutJsonRpcError(
                        "Blockscout JSON-RPC response is invalid "
                        f"after {attempt} attempt{'s' if attempt != 1 else ''}"
                    ) from error

            if not retryable or attempt == self._max_attempts:
                raise BlockscoutJsonRpcError(
                    f"{request_error} after {attempt} attempt{'s' if attempt != 1 else ''}"
                ) from request_cause
            await self._sleeper(
                self._retry_initial_delay_seconds
                * self._retry_backoff_factor ** (attempt - 1)
            )

        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _extract_eth_call_result(data: Any) -> str:
    if not isinstance(data, dict):
        raise ValueError("JSON-RPC response must be an object")
    if "error" in data:
        raise ValueError(f"JSON-RPC error response: {data['error']!r}")
    result = data.get("result")
    if not isinstance(result, str) or not result.startswith("0x"):
        raise ValueError("JSON-RPC result must be a 0x-prefixed string")
    return result


def _token_cache_key(token_address: str) -> str:
    return checksum(token_address).lower()


def _validate_decimals(decimals: int) -> int:
    decimal_count = int(decimals)
    if decimal_count < 0 or decimal_count > 255:
        raise ValueError("token decimals must be between 0 and 255")
    return decimal_count


def _decimal_from_raw_token_units(value: Any, decimal_count: int) -> Decimal:
    raw_value = str(value)
    sign = 0
    if raw_value.startswith("-"):
        sign = 1
        raw_value = raw_value[1:]
    if not raw_value or not raw_value.isdecimal():
        raise ValueError("token balance value must be an integer string")
    digits = tuple(int(digit) for digit in raw_value.lstrip("0") or "0")
    exponent = -decimal_count
    while exponent < 0 and digits[-1] == 0:
        digits = digits[:-1] or (0,)
        exponent += 1
    return Decimal((sign, digits, exponent))
