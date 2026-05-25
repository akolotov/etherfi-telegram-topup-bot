from __future__ import annotations

from decimal import Decimal
from typing import Any

from eth_abi import encode as encode_abi
from hexbytes import HexBytes
from web3 import Web3


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
    if len(result) != 32:
        raise ValueError("uint256 result must be exactly 32 bytes")
    return int.from_bytes(result, "big")


def checksum(address: str) -> str:
    return Web3.to_checksum_address(address)
