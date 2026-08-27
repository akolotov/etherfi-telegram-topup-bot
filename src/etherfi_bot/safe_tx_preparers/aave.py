from __future__ import annotations

from decimal import Decimal

from safe_eth.safe import SafeOperationEnum

from etherfi_bot.blockscout import BlockscoutJsonRpcError, Erc20BalanceReader
from etherfi_bot.domain import SafeTxCreateError
from etherfi_bot.safe_tx_preparers import (
    SafeTxCall,
    checksum,
    decimal_to_base_units,
    encode_contract_method,
)


AAVE_V3_ARBITRUM_POOL = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"
ARBITRUM_NATIVE_USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
ARBITRUM_AAVE_NATIVE_USDC_ATOKEN = "0x724dc807b04555b71ed48a6896b6F41593b8C637"
USDC_DECIMALS = 6


class AaveV3NativeUsdcWithdrawPreparer:
    def __init__(
        self,
        balances: Erc20BalanceReader,
        *,
        pool_address: str = AAVE_V3_ARBITRUM_POOL,
        usdc_address: str = ARBITRUM_NATIVE_USDC,
        ausdc_address: str = ARBITRUM_AAVE_NATIVE_USDC_ATOKEN,
        decimals: int = USDC_DECIMALS,
    ) -> None:
        self.pool_address = checksum(pool_address)
        self.usdc_address = checksum(usdc_address)
        self.ausdc_address = checksum(ausdc_address)
        self.decimals = int(decimals)
        self._balances = balances

    async def preflight_check(
        self,
        safe_address: str,
        amount: Decimal,
        target_account: str,
    ) -> None:
        del target_account
        amount_base_units = decimal_to_base_units(amount, self.decimals)
        try:
            balance_base_units = await self._balances.get_balance_base_units(
                self.ausdc_address,
                checksum(safe_address),
            )
        except (BlockscoutJsonRpcError, ValueError) as error:
            raise SafeTxCreateError("AAVE preflight balance check failed") from error
        if balance_base_units < amount_base_units:
            raise SafeTxCreateError(
                "AAVE preflight balance check failed: "
                f"required {amount_base_units} aUSDC base units, "
                f"available {balance_base_units}"
            )

    def prepare_transaction(
        self,
        safe_address: str,
        amount: Decimal,
        target_account: str,
    ) -> SafeTxCall:
        del safe_address
        amount_base_units = decimal_to_base_units(amount, self.decimals)
        data = encode_contract_method(
            "withdraw",
            ["address", "uint256", "address"],
            [self.usdc_address, amount_base_units, checksum(target_account)],
        )
        return SafeTxCall(
            to=self.pool_address,
            value=0,
            data=data,
            operation=SafeOperationEnum.CALL.value,
        )
