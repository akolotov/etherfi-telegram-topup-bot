from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from etherfi_bot.evm import (
    checksum,
    decimal_to_base_units,
    encode_contract_method,
    uint256_from_hex,
)


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
