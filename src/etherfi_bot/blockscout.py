from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Callable, Iterable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from etherfi_bot.domain import BalanceReadError, UserConfig
from etherfi_bot.evm import checksum, encode_contract_method, uint256_from_hex


OPTIMISM_CHAIN_ID = "10"
BLOCKSCOUT_BASE_URL = "https://api.blockscout.com"
USER_AGENT = "etherfi-topup-bot/0.1.0"


class Erc20BalanceReader(Protocol):
    def get_balance_base_units(self, token_address: str, account_address: str) -> int:
        """Read one ERC-20 token balance in base units."""

    def get_decimals(self, token_address: str) -> int:
        """Read or return cached decimals for one ERC-20 token."""

    def preload_decimals(self, token_addresses: Iterable[str]) -> None:
        """Read decimals for all unique configured tokens ahead of polling."""


class BlockscoutBalanceProvider:
    def __init__(self, token_reader: Erc20BalanceReader) -> None:
        self._token_reader = token_reader

    def get_balance(self, user: UserConfig) -> Decimal:
        try:
            balance_base_units = self._token_reader.get_balance_base_units(
                user.balance_token_address,
                user.target_account,
            )
            decimals = self._token_reader.get_decimals(user.balance_token_address)
            return _decimal_from_raw_token_units(balance_base_units, decimals)
        except BlockscoutJsonRpcError as error:
            raise BalanceReadError("Blockscout balance request failed") from error
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
        for token_address, decimals in (decimals_by_token_address or {}).items():
            self._decimals_by_token_address[_token_cache_key(token_address)] = (
                _validate_decimals(decimals)
            )

    def get_balance_base_units(self, token_address: str, account_address: str) -> int:
        data = encode_contract_method(
            "balanceOf",
            ["address"],
            [checksum(account_address)],
        )
        raw_balance = self._rpc_client.eth_call(
            to=checksum(token_address),
            data=data,
        )
        return uint256_from_hex(raw_balance)

    def get_decimals(self, token_address: str) -> int:
        cache_key = _token_cache_key(token_address)
        if cache_key not in self._decimals_by_token_address:
            raw_decimals = self._rpc_client.eth_call(
                to=checksum(token_address),
                data=encode_contract_method("decimals", [], []),
            )
            self._decimals_by_token_address[cache_key] = _validate_decimals(
                uint256_from_hex(raw_decimals)
            )
        return self._decimals_by_token_address[cache_key]

    def preload_decimals(self, token_addresses: Iterable[str]) -> None:
        for token_address in token_addresses:
            self.get_decimals(token_address)


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
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._chain_id = str(chain_id)
        self._timeout_seconds = timeout_seconds
        self._opener = opener

    def eth_call(self, *, to: str, data: bytes | str, block: str = "latest") -> str:
        data_hex = data.hex() if isinstance(data, bytes) else data
        if not data_hex.startswith("0x"):
            data_hex = f"0x{data_hex}"
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": to, "data": data_hex}, block],
        }
        request = Request(
            f"{self._base_url}/{quote(self._chain_id, safe='')}/json-rpc",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=self._timeout_seconds) as response:
                body = response.read().decode("utf-8")
            rpc_response = json.loads(body)
            return _extract_eth_call_result(rpc_response)
        except HTTPError as error:
            raise BlockscoutJsonRpcError(
                f"Blockscout JSON-RPC request failed with HTTP {error.code}"
            ) from error
        except (URLError, OSError) as error:
            raise BlockscoutJsonRpcError("Blockscout JSON-RPC request failed") from error
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise BlockscoutJsonRpcError(
                "Blockscout JSON-RPC response is invalid"
            ) from error


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
