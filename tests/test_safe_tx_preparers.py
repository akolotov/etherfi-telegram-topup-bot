from __future__ import annotations

from decimal import Decimal

import pytest
from eth_abi import decode as decode_abi

from etherfi_bot.blockscout import BlockscoutJsonRpcError
from etherfi_bot.domain import InsufficientSafeBalanceError, SafeTxCreateError
from etherfi_bot.safe_tx_preparers import (
    AAVE_V3_ARBITRUM_POOL,
    ARBITRUM_AAVE_NATIVE_USDC_ATOKEN,
    ARBITRUM_NATIVE_USDC,
    AaveV3NativeUsdcWithdrawPreparer,
    checksum,
    decimal_to_base_units,
)


async def test_aave_preflight_passes_when_safe_has_enough_ausdc() -> None:
    balances = RecordingBalances(balance_base_units=2_000_000)
    preparer = AaveV3NativeUsdcWithdrawPreparer(balances)

    await preparer.preflight_check(
        "0x0000000000000000000000000000000000000001",
        Decimal("1.5"),
        "0x0000000000000000000000000000000000000002",
    )

    assert balances.calls == [
        {
            "token_address": checksum(ARBITRUM_AAVE_NATIVE_USDC_ATOKEN),
            "account_address": checksum("0x0000000000000000000000000000000000000001"),
        }
    ]


async def test_aave_preflight_fails_when_safe_ausdc_balance_is_insufficient() -> None:
    balances = RecordingBalances(balance_base_units=999_999)
    preparer = AaveV3NativeUsdcWithdrawPreparer(balances)

    with pytest.raises(InsufficientSafeBalanceError, match="required 1000000"):
        await preparer.preflight_check(
            "0x0000000000000000000000000000000000000001",
            Decimal("1"),
            "0x0000000000000000000000000000000000000002",
        )


async def test_aave_preflight_wraps_blockscout_errors_as_safe_tx_create_failed() -> None:
    preparer = AaveV3NativeUsdcWithdrawPreparer(FailingBalances())

    with pytest.raises(SafeTxCreateError, match="AAVE preflight balance check failed"):
        await preparer.preflight_check(
            "0x0000000000000000000000000000000000000001",
            Decimal("1"),
            "0x0000000000000000000000000000000000000002",
        )


def test_decimal_to_base_units_requires_exact_token_precision() -> None:
    assert decimal_to_base_units(Decimal("1.234567"), 6) == 1_234_567

    with pytest.raises(ValueError, match="more precision"):
        decimal_to_base_units(Decimal("0.0000001"), 6)


def test_aave_prepare_transaction_builds_withdraw_call() -> None:
    preparer = AaveV3NativeUsdcWithdrawPreparer(
        RecordingBalances(balance_base_units=0)
    )
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


class RecordingBalances:
    def __init__(self, *, balance_base_units: int) -> None:
        self.balance_base_units = balance_base_units
        self.calls: list[dict[str, str]] = []

    async def get_balance_base_units(
        self, token_address: str, account_address: str
    ) -> int:
        self.calls.append(
            {
                "token_address": token_address,
                "account_address": account_address,
            }
        )
        return self.balance_base_units


class FailingBalances:
    async def get_balance_base_units(
        self, token_address: str, account_address: str
    ) -> int:
        del token_address, account_address
        raise BlockscoutJsonRpcError("unavailable")
