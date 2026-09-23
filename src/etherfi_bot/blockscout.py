from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from math import isfinite
from time import monotonic
from typing import Any, Awaitable, Callable, Iterable, Protocol
from urllib.parse import quote

import httpx

from etherfi_bot.domain import BalanceReadError, UserConfig
from etherfi_bot.evm import checksum, encode_contract_method, uint256_from_hex


OPTIMISM_CHAIN_ID = "10"
BLOCKSCOUT_BASE_URL = "https://api.blockscout.com"
USER_AGENT = "etherfi-topup-bot/0.1.0"


logger = logging.getLogger(__name__)


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

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class BlockscoutJsonRpcClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BLOCKSCOUT_BASE_URL,
        chain_id: str = "42161",
        fallback_url: str | None = None,
        fallback_cooldown_seconds: float = 300,
        timeout_seconds: float = 10,
        max_attempts: int = 3,
        retry_initial_delay_seconds: float = 0.5,
        retry_backoff_factor: float = 2,
        client: httpx.AsyncClient | None = None,
        fallback_client: httpx.AsyncClient | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if not isfinite(retry_initial_delay_seconds) or retry_initial_delay_seconds < 0:
            raise ValueError("retry_initial_delay_seconds must be finite and >= 0")
        if not isfinite(retry_backoff_factor) or retry_backoff_factor < 1:
            raise ValueError("retry_backoff_factor must be finite and >= 1")
        if not isfinite(fallback_cooldown_seconds) or fallback_cooldown_seconds < 0:
            raise ValueError("fallback_cooldown_seconds must be finite and >= 0")
        if fallback_client is not None and fallback_url is None:
            raise ValueError("fallback_url is required with fallback_client")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._chain_id = str(chain_id)
        self._fallback_url = fallback_url.rstrip("/") if fallback_url else None
        self._fallback_cooldown_seconds = fallback_cooldown_seconds
        self._monotonic = monotonic_clock
        self._primary_retry_at = 0.0
        self._primary_probe_lock = asyncio.Lock()
        self._max_attempts = max_attempts
        self._retry_initial_delay_seconds = retry_initial_delay_seconds
        self._retry_backoff_factor = retry_backoff_factor
        self._sleeper = sleeper
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._client.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            }
        )
        self._owns_fallback_client = fallback_url is not None and fallback_client is None
        self._fallback_client = fallback_client
        if self._fallback_url is not None and self._fallback_client is None:
            self._fallback_client = httpx.AsyncClient(timeout=timeout_seconds)
        if self._fallback_client is not None:
            self._fallback_client.headers.update(
                {
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
        if self._should_bypass_primary():
            return await self._call_fallback(payload)
        if self._primary_retry_at > 0:
            async with self._primary_probe_lock:
                if self._should_bypass_primary():
                    return await self._call_fallback(payload)
                return await self._call_primary_or_fallback(url, payload)
        return await self._call_primary_or_fallback(url, payload)

    def _should_bypass_primary(self) -> bool:
        return (
            self._fallback_url is not None
            and self._monotonic() < self._primary_retry_at
        )

    async def _call_primary_or_fallback(
        self, url: str, payload: dict[str, Any]
    ) -> str:
        try:
            result = await self._post_with_retries(
                client=self._client,
                url=url,
                payload=payload,
                provider_name="Blockscout JSON-RPC",
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
        except BlockscoutJsonRpcError as error:
            if not error.retryable or self._fallback_url is None:
                self._primary_retry_at = 0.0
                raise
            primary_failure = error
        else:
            if self._primary_retry_at > 0:
                logger.info(
                    "blockscout_json_rpc_primary_restored chain_id=%s",
                    self._chain_id,
                )
            self._primary_retry_at = 0.0
            return result

        self._primary_retry_at = (
            self._monotonic() + self._fallback_cooldown_seconds
        )
        logger.warning(
            "blockscout_json_rpc_fallback_activated chain_id=%s "
            "cooldown_seconds=%s primary_error=%s",
            self._chain_id,
            self._fallback_cooldown_seconds,
            primary_failure,
        )
        return await self._call_fallback(payload, primary_failure=primary_failure)

    async def _call_fallback(
        self,
        payload: dict[str, Any],
        *,
        primary_failure: BlockscoutJsonRpcError | None = None,
    ) -> str:
        assert self._fallback_client is not None
        assert self._fallback_url is not None
        try:
            return await self._post_with_retries(
                client=self._fallback_client,
                url=self._fallback_url,
                payload=payload,
                provider_name="Fallback JSON-RPC",
            )
        except BlockscoutJsonRpcError as fallback_error:
            if primary_failure is None:
                raise
            raise BlockscoutJsonRpcError(
                f"{primary_failure}; fallback RPC failed: {fallback_error}",
                retryable=fallback_error.retryable,
            ) from fallback_error

    async def _post_with_retries(
        self,
        *,
        client: httpx.AsyncClient,
        url: str,
        payload: dict[str, Any],
        provider_name: str,
        headers: dict[str, str] | None = None,
    ) -> str:
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
            except httpx.HTTPStatusError as error:
                status_code = error.response.status_code
                request_cause: Exception = error
                request_error = BlockscoutJsonRpcError(
                    f"{provider_name} request failed with HTTP {status_code}",
                    retryable=status_code == 429 or 500 <= status_code < 600,
                )
            except httpx.RequestError as error:
                request_cause = error
                request_error = BlockscoutJsonRpcError(
                    f"{provider_name} request failed",
                    retryable=True,
                )
            else:
                try:
                    return _extract_eth_call_result(response.json())
                except (TypeError, ValueError) as error:
                    if _is_retryable_json_rpc_error(response):
                        request_cause = error
                        request_error = BlockscoutJsonRpcError(
                            f"{provider_name} returned a transient error",
                            retryable=True,
                        )
                    else:
                        raise BlockscoutJsonRpcError(
                            f"{provider_name} response is invalid "
                            f"after {attempt} attempt{'s' if attempt != 1 else ''}"
                        ) from error

            if not request_error.retryable or attempt == self._max_attempts:
                raise BlockscoutJsonRpcError(
                    f"{request_error} after {attempt} "
                    f"attempt{'s' if attempt != 1 else ''}",
                    retryable=request_error.retryable,
                ) from request_cause
            await self._sleeper(
                self._retry_initial_delay_seconds
                * self._retry_backoff_factor ** (attempt - 1)
            )

        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        close_tasks = []
        if self._owns_client:
            close_tasks.append(self._client.aclose())
        if self._owns_fallback_client:
            assert self._fallback_client is not None
            close_tasks.append(self._fallback_client.aclose())
        if close_tasks:
            await asyncio.gather(*close_tasks)


def _is_retryable_json_rpc_error(response: httpx.Response) -> bool:
    try:
        data = response.json()
    except ValueError:
        return False
    if not isinstance(data, dict) or "error" not in data:
        return False
    error_text = str(data["error"]).lower()
    return any(
        marker in error_text
        for marker in (
            "internal error",
            "internal server error",
            "upstream",
            "timeout",
            "temporar",
            "unavailable",
            "rate limit",
        )
    )


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
