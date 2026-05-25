from __future__ import annotations

from decimal import Decimal

import pytest
from eth_abi import decode as decode_abi

from etherfi_bot.blockscout import BlockscoutJsonRpcError
from etherfi_bot.domain import SafeTxCreateError
from etherfi_bot.safe_tx_preparers import (
    AAVE_V3_ARBITRUM_POOL,
    ARBITRUM_AAVE_NATIVE_USDC_ATOKEN,
    ARBITRUM_NATIVE_USDC,
    AaveV3NativeUsdcWithdrawPreparer,
    checksum,
    decimal_to_base_units,
)


def test_aave_preflight_passes_when_safe_has_enough_ausdc() -> None:
    blockscout = RecordingJsonRpc(result=uint256_hex(2_000_000))
    preparer = AaveV3NativeUsdcWithdrawPreparer(blockscout)

    preparer.preflight_check(
        "0x0000000000000000000000000000000000000001",
        Decimal("1.5"),
        "0x0000000000000000000000000000000000000002",
    )

    assert blockscout.calls == [
        {
            "to": checksum(ARBITRUM_AAVE_NATIVE_USDC_ATOKEN),
            "data": "0x70a08231"
            "0000000000000000000000000000000000000000000000000000000000000001",
        }
    ]


def test_aave_preflight_fails_when_safe_ausdc_balance_is_insufficient() -> None:
    blockscout = RecordingJsonRpc(result=uint256_hex(999_999))
    preparer = AaveV3NativeUsdcWithdrawPreparer(blockscout)

    with pytest.raises(SafeTxCreateError, match="required 1000000"):
        preparer.preflight_check(
            "0x0000000000000000000000000000000000000001",
            Decimal("1"),
            "0x0000000000000000000000000000000000000002",
        )


def test_aave_preflight_wraps_blockscout_errors_as_safe_tx_create_failed() -> None:
    preparer = AaveV3NativeUsdcWithdrawPreparer(FailingJsonRpc())

    with pytest.raises(SafeTxCreateError, match="AAVE preflight balance check failed"):
        preparer.preflight_check(
            "0x0000000000000000000000000000000000000001",
            Decimal("1"),
            "0x0000000000000000000000000000000000000002",
        )


def test_decimal_to_base_units_requires_exact_token_precision() -> None:
    assert decimal_to_base_units(Decimal("1.234567"), 6) == 1_234_567

    with pytest.raises(ValueError, match="more precision"):
        decimal_to_base_units(Decimal("0.0000001"), 6)


def test_aave_prepare_transaction_builds_withdraw_call() -> None:
    preparer = AaveV3NativeUsdcWithdrawPreparer(RecordingJsonRpc(result=uint256_hex(0)))
    target = "0x0000000000000000000000000000000000000002"

    call = preparer.prepare_transaction(
        "0x0000000000000000000000000000000000000001",
        Decimal("12.345678"),
        target,
    )

    assert call.to == checksum(AAVE_V3_ARBITRUM_POOL)
    assert call.value == 0
    assert call.operation == 0
    assert call.data[:4].hex() == "69328dec"
    asset, amount, recipient = decode_abi(
        ["address", "uint256", "address"],
        call.data[4:],
    )
    assert checksum(asset) == checksum(ARBITRUM_NATIVE_USDC)
    assert amount == 12_345_678
    assert checksum(recipient) == checksum(target)


class RecordingJsonRpc:
    def __init__(self, *, result: str) -> None:
        self.result = result
        self.calls: list[dict[str, str]] = []

    def eth_call(self, *, to: str, data: bytes | str, block: str = "latest") -> str:
        del block
        data_hex = data.hex() if isinstance(data, bytes) else data
        if not data_hex.startswith("0x"):
            data_hex = f"0x{data_hex}"
        self.calls.append({"to": to, "data": data_hex})
        return self.result


class FailingJsonRpc:
    def eth_call(self, *, to: str, data: bytes | str, block: str = "latest") -> str:
        del to, data, block
        raise BlockscoutJsonRpcError("unavailable")


def uint256_hex(value: int) -> str:
    return f"0x{value:064x}"
