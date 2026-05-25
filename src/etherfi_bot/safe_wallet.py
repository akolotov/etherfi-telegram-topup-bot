from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

import requests
from eth_abi import encode as encode_abi
from eth_account import Account
from hexbytes import HexBytes
from safe_eth.safe import SafeOperationEnum, SafeTx
from safe_eth.util.util import to_0x_hex_str
from web3 import Web3

from etherfi_bot.blockscout import (
    USER_AGENT,
    BlockscoutJsonRpcClient,
    BlockscoutJsonRpcError,
)
from etherfi_bot.domain import (
    SafeTxCreateError,
    SafeTxStatus,
    SafeTxStatusReadError,
    UserConfig,
)


ARBITRUM_CHAIN_ID = 42161
ARBITRUM_TX_SERVICE_BASE_URL = "https://api.safe.global/tx-service/arb1"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

AAVE_V3_ARBITRUM_POOL = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"
ARBITRUM_NATIVE_USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
ARBITRUM_AAVE_NATIVE_USDC_ATOKEN = "0x724dc807b04555b71ed48a6896b6F41593b8C637"
USDC_DECIMALS = 6


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


class AaveV3NativeUsdcWithdrawPreparer:
    def __init__(
        self,
        blockscout: BlockscoutJsonRpcClient,
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
        self._blockscout = blockscout

    def preflight_check(
        self,
        safe_address: str,
        amount: Decimal,
        target_account: str,
    ) -> None:
        del target_account
        amount_base_units = decimal_to_base_units(amount, self.decimals)
        data = encode_contract_method(
            "balanceOf",
            ["address"],
            [checksum(safe_address)],
        )
        try:
            raw_balance = self._blockscout.eth_call(to=self.ausdc_address, data=data)
            balance_base_units = uint256_from_hex(raw_balance)
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


class SafeTxServiceError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SafeTxServiceClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = ARBITRUM_TX_SERVICE_BASE_URL,
        timeout_seconds: float = 15,
        session: requests.Session | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": USER_AGENT,
            }
        )

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        try:
            response = self.session.get(
                f"{self.base_url}{path}",
                params={
                    key: value for key, value in params.items() if value is not None
                },
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as error:
            raise SafeTxServiceError("Safe Transaction Service GET failed") from error
        return self._json_or_raise(response)

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        try:
            response = self.session.post(
                f"{self.base_url}{path}",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as error:
            raise SafeTxServiceError("Safe Transaction Service POST failed") from error
        if response.status_code == 201:
            return _response_json_or_none(response)
        return self._json_or_raise(response)

    def _json_or_raise(self, response: requests.Response) -> dict[str, Any]:
        if response.ok:
            data = _response_json_or_none(response)
            if isinstance(data, dict):
                return data
            raise SafeTxServiceError("Safe Transaction Service response is invalid")
        try:
            payload = response.json()
        except ValueError:
            payload = response.text
        raise SafeTxServiceError(
            f"Safe Transaction Service request failed with HTTP "
            f"{response.status_code}: {payload!r}",
            status_code=response.status_code,
        )


class SafeWalletTransactionServiceClient:
    def __init__(
        self,
        tx_service: SafeTxServiceClient,
        top_up_preparer: SafeTxDataPreparer,
        *,
        chain_id: int = ARBITRUM_CHAIN_ID,
    ) -> None:
        self._tx_service = tx_service
        self._top_up_preparer = top_up_preparer
        self._chain_id = int(chain_id)

    def create_top_up_tx(
        self,
        user: UserConfig,
        amount: Decimal,
        safe_proposer_private_key: str,
    ) -> str:
        try:
            safe_address = checksum(user.safe_account)
            target_account = checksum(user.target_account)
            self._top_up_preparer.preflight_check(
                safe_address,
                amount,
                target_account,
            )
            safe_info = get_safe_info(self._tx_service, safe_address)
            proposer_address = proposer_address_from_key(safe_proposer_private_key)
            require_registered_proposer(
                self._tx_service,
                safe_address,
                proposer_address,
            )
            call = self._top_up_preparer.prepare_transaction(
                safe_address,
                amount,
                target_account,
            )
            safe_tx = build_safe_tx_from_call(
                safe_address=safe_address,
                safe_info=safe_info,
                call=call,
                chain_id=self._chain_id,
            )
            return propose_safe_tx(
                client=self._tx_service,
                safe_address=safe_address,
                safe_tx=safe_tx,
                proposer_private_key=safe_proposer_private_key,
                origin=f"ether.fi-bot:{user.telegram_user_id}:top-up",
            )
        except SafeTxCreateError:
            raise
        except (SafeTxServiceError, KeyError, TypeError, ValueError) as error:
            raise SafeTxCreateError("Safe tx creation failed") from error

    def get_tx_status(self, user: UserConfig, safe_tx_id: str) -> SafeTxStatus:
        try:
            tx = get_raw_multisig_tx(self._tx_service, safe_tx_id)
            if tx is None:
                return SafeTxStatus.FINAL
            if bool(tx["isExecuted"]):
                return SafeTxStatus.FINAL
            safe_info = get_safe_info(self._tx_service, user.safe_account)
            if int(safe_info["nonce"]) > int(tx["nonce"]):
                return SafeTxStatus.FINAL
            return SafeTxStatus.PENDING
        except (SafeTxServiceError, KeyError, TypeError, ValueError) as error:
            raise SafeTxStatusReadError("Safe tx status read failed") from error


def get_safe_info(client: SafeTxServiceClient, safe_address: str) -> dict[str, Any]:
    return client.get(f"/api/v1/safes/{checksum(safe_address)}/")


def get_raw_multisig_tx(
    client: SafeTxServiceClient,
    safe_tx_hash: str,
) -> dict[str, Any] | None:
    try:
        return client.get(f"/api/v2/multisig-transactions/{safe_tx_hash}/")
    except SafeTxServiceError as error:
        if error.status_code == 404:
            return None
        raise


def require_registered_proposer(
    client: SafeTxServiceClient,
    safe_address: str,
    proposer_address: str,
) -> None:
    proposer_address = checksum(proposer_address)
    # MVP provisioning expects one delegate per Safe, so one page is enough here.
    page = client.get("/api/v2/delegates/", safe=checksum(safe_address), limit=100)
    delegates = page.get("results", [])
    if not isinstance(delegates, list):
        raise SafeTxCreateError("Safe delegates response is invalid")
    for row in delegates:
        if not isinstance(row, dict):
            continue
        delegate = row.get("delegate") or ZERO_ADDRESS
        if checksum(str(delegate)) == proposer_address:
            return
    raise SafeTxCreateError(
        f"{proposer_address} is not registered as proposer for {checksum(safe_address)}"
    )


def build_safe_tx_from_call(
    *,
    safe_address: str,
    safe_info: dict[str, Any],
    call: SafeTxCall,
    chain_id: int = ARBITRUM_CHAIN_ID,
) -> SafeTx:
    return SafeTx(
        None,
        checksum(safe_address),
        checksum(call.to),
        int(call.value),
        call.data,
        int(call.operation),
        0,
        0,
        0,
        None,
        None,
        safe_nonce=int(safe_info["nonce"]),
        safe_version=str(safe_info["version"]),
        chain_id=int(chain_id),
    )


def propose_safe_tx(
    *,
    client: SafeTxServiceClient,
    safe_address: str,
    safe_tx: SafeTx,
    proposer_private_key: str,
    origin: str,
) -> str:
    safe_tx.sign(proposer_private_key)
    if not safe_tx.sorted_signers:
        raise SafeTxCreateError("Safe tx proposal signature was not produced")
    safe_tx_hash = to_0x_hex_str(safe_tx.safe_tx_hash)
    payload = {
        "to": safe_tx.to,
        "value": safe_tx.value,
        "data": to_0x_hex_str(safe_tx.data) if safe_tx.data else None,
        "operation": safe_tx.operation,
        "gasToken": safe_tx.gas_token,
        "safeTxGas": safe_tx.safe_tx_gas,
        "baseGas": safe_tx.base_gas,
        "gasPrice": safe_tx.gas_price,
        "refundReceiver": safe_tx.refund_receiver,
        "nonce": safe_tx.safe_nonce,
        "contractTransactionHash": safe_tx_hash,
        "sender": safe_tx.sorted_signers[0],
        "signature": safe_tx.signatures.hex() if safe_tx.signatures else None,
        "origin": origin,
    }
    client.post(
        f"/api/v2/safes/{checksum(safe_address)}/multisig-transactions/",
        payload,
    )
    return safe_tx_hash


def proposer_address_from_key(proposer_private_key: str) -> str:
    return checksum(Account.from_key(proposer_private_key).address)


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


def _response_json_or_none(response: requests.Response) -> dict[str, Any] | None:
    content = getattr(response, "content", b"")
    if not content:
        return None
    return response.json()
