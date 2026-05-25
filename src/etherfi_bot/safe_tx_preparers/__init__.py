from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from eth_abi import encode as encode_abi
from hexbytes import HexBytes
from web3 import Web3


@dataclass(frozen=True)
class SafeTxCall:
    to: str
    value: int
    data: bytes
    operation: int


class SafeTxDataPreparer(Protocol):
    def preflight_check(
        self,
        safe_address: str,
        amount: Decimal,
        target_account: str,
    ) -> None:
        """Raise when the intended Safe transaction should not be proposed."""

    def prepare_transaction(
        self,
        safe_address: str,
        amount: Decimal,
        target_account: str,
    ) -> SafeTxCall:
        """Return one Safe transaction call for the requested transfer."""


def decimal_to_base_units(amount: Decimal, decimals: int) -> int:
    scale = Decimal(10) ** int(decimals)
    base_units = amount * scale
    if base_units != base_units.to_integral_value():
        raise ValueError(f"amount {amount} has more precision than {decimals} decimals")
    return int(base_units)


def encode_contract_method(
    name: str,
    solidity_types: list[str],
    values: list[Any],
) -> bytes:
    selector = Web3.keccak(text=f"{name}({','.join(solidity_types)})")[:4]
    return bytes(selector + encode_abi(solidity_types, values))


def uint256_from_hex(value: str) -> int:
    if not value.startswith("0x"):
        raise ValueError("uint256 result must be 0x-prefixed")
    result = HexBytes(value)
    if len(result) > 32:
        raise ValueError("uint256 result is longer than 32 bytes")
    return int.from_bytes(result.rjust(32, b"\x00"), "big")


def checksum(address: str) -> str:
    return Web3.to_checksum_address(address)


from etherfi_bot.safe_tx_preparers.aave import (  # noqa: E402
    AAVE_V3_ARBITRUM_POOL,
    ARBITRUM_AAVE_NATIVE_USDC_ATOKEN,
    ARBITRUM_NATIVE_USDC,
    USDC_DECIMALS,
    AaveV3NativeUsdcWithdrawPreparer,
)


__all__ = [
    "AAVE_V3_ARBITRUM_POOL",
    "ARBITRUM_AAVE_NATIVE_USDC_ATOKEN",
    "ARBITRUM_NATIVE_USDC",
    "AaveV3NativeUsdcWithdrawPreparer",
    "SafeTxCall",
    "SafeTxDataPreparer",
    "USDC_DECIMALS",
    "checksum",
    "decimal_to_base_units",
    "encode_contract_method",
    "uint256_from_hex",
]
