from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from etherfi_bot.domain import BalanceReadError, UserConfig


OPTIMISM_CHAIN_ID = "10"
BLOCKSCOUT_BASE_URL = "https://api.blockscout.com"
USER_AGENT = "etherfi-bot/0.1.0"


class BlockscoutBalanceProvider:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BLOCKSCOUT_BASE_URL,
        chain_id: str = OPTIMISM_CHAIN_ID,
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

    def get_balance(self, user: UserConfig) -> Decimal:
        url = (
            f"{self._base_url}/{quote(self._chain_id, safe='')}/api/v2/addresses/"
            f"{quote(user.target_account, safe='')}/token-balances"
        )
        request = Request(
            url,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="GET",
        )
        try:
            with self._opener(request, timeout=self._timeout_seconds) as response:
                body = response.read().decode("utf-8")
            data = json.loads(body)
            return _extract_token_balance(data, user.balance_token_address)
        except HTTPError as error:
            raise BalanceReadError(
                f"Blockscout balance request failed with HTTP {error.code}"
            ) from error
        except (URLError, OSError) as error:
            raise BalanceReadError("Blockscout balance request failed") from error
        except (json.JSONDecodeError, TypeError, ValueError, InvalidOperation) as error:
            raise BalanceReadError("Blockscout balance response is invalid") from error


def _extract_token_balance(data: Any, token_address: str) -> Decimal:
    if not isinstance(data, list):
        raise ValueError("token balance response must be a list")

    normalized_token_address = token_address.lower()
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("token balance item must be an object")
        token = item.get("token")
        if token is None:
            continue
        if not isinstance(token, dict):
            raise ValueError("token field must be an object or null")
        address_hash = token.get("address_hash")
        if not isinstance(address_hash, str):
            raise ValueError("token address_hash must be a string")
        if address_hash.lower() != normalized_token_address:
            continue
        return _denominate_token_balance(item, token)

    return Decimal("0")


def _denominate_token_balance(item: dict[str, Any], token: dict[str, Any]) -> Decimal:
    value = item.get("value")
    decimals = token.get("decimals")
    if value is None or decimals is None:
        raise ValueError("matched token balance is missing value or decimals")
    decimal_count = int(str(decimals))
    if decimal_count < 0:
        raise ValueError("token decimals must not be negative")
    return _decimal_from_raw_token_units(value, decimal_count)


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
