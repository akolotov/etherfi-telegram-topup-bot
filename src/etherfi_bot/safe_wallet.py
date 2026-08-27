from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import httpx
from eth_account import Account
from safe_eth.safe import SafeTx
from safe_eth.util.util import to_0x_hex_str

from etherfi_bot.blockscout import USER_AGENT
from etherfi_bot.domain import (
    SafeTxCreateError,
    SafeTxStatus,
    SafeTxStatusReadError,
    UserConfig,
)
from etherfi_bot.safe_tx_preparers import SafeTxCall, SafeTxDataPreparer, checksum


ARBITRUM_CHAIN_ID = 42161
ARBITRUM_TX_SERVICE_BASE_URL = "https://api.safe.global/tx-service/arb1"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


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
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._client.headers.update(
            {
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": USER_AGENT,
            }
        )

    async def get(self, path: str, **params: Any) -> dict[str, Any]:
        try:
            response = await self._client.get(
                f"{self.base_url}{path}",
                params={key: value for key, value in params.items() if value is not None},
            )
        except httpx.RequestError as error:
            raise SafeTxServiceError("Safe Transaction Service GET failed") from error
        return self._json_or_raise(response)

    async def post(
        self, path: str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        try:
            response = await self._client.post(
                f"{self.base_url}{path}",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        except httpx.RequestError as error:
            raise SafeTxServiceError("Safe Transaction Service POST failed") from error
        if response.status_code == 201:
            return _response_json_or_none(response)
        return self._json_or_raise(response)

    def _json_or_raise(self, response: httpx.Response) -> dict[str, Any]:
        if response.is_success:
            data = _response_json_or_none(response)
            if isinstance(data, dict):
                return data
            raise SafeTxServiceError("Safe Transaction Service response is invalid")
        try:
            payload: Any = response.json()
        except ValueError:
            payload = response.text
        raise SafeTxServiceError(
            "Safe Transaction Service request failed with HTTP "
            f"{response.status_code}: {payload!r}",
            status_code=response.status_code,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


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

    async def create_top_up_tx(
        self,
        user: UserConfig,
        amount: Decimal,
        safe_proposer_private_key: str,
    ) -> str:
        try:
            safe_address = checksum(user.safe_account)
            target_account = checksum(user.target_account)
            await self._top_up_preparer.preflight_check(
                safe_address, amount, target_account
            )
            safe_info = await get_safe_info(self._tx_service, safe_address)
            proposer_address = proposer_address_from_key(safe_proposer_private_key)
            await require_registered_proposer(
                self._tx_service, safe_address, proposer_address
            )
            call = self._top_up_preparer.prepare_transaction(
                safe_address, amount, target_account
            )
            safe_tx = build_safe_tx_from_call(
                safe_address=safe_address,
                safe_info=safe_info,
                call=call,
                chain_id=self._chain_id,
            )
            return await propose_safe_tx(
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

    async def get_tx_status(
        self, user: UserConfig, safe_tx_id: str
    ) -> SafeTxStatus:
        try:
            tx = await get_raw_multisig_tx(self._tx_service, safe_tx_id)
            if tx is None or bool(tx["isExecuted"]):
                return SafeTxStatus.FINAL
            safe_info = await get_safe_info(self._tx_service, user.safe_account)
            if int(safe_info["nonce"]) > int(tx["nonce"]):
                return SafeTxStatus.FINAL
            return SafeTxStatus.PENDING
        except (SafeTxServiceError, KeyError, TypeError, ValueError) as error:
            raise SafeTxStatusReadError("Safe tx status read failed") from error


async def get_safe_info(
    client: SafeTxServiceClient, safe_address: str
) -> dict[str, Any]:
    return await client.get(f"/api/v1/safes/{checksum(safe_address)}/")


async def get_raw_multisig_tx(
    client: SafeTxServiceClient, safe_tx_hash: str
) -> dict[str, Any] | None:
    try:
        return await client.get(f"/api/v2/multisig-transactions/{safe_tx_hash}/")
    except SafeTxServiceError as error:
        if error.status_code == 404:
            return None
        raise


async def require_registered_proposer(
    client: SafeTxServiceClient,
    safe_address: str,
    proposer_address: str,
) -> None:
    proposer_address = checksum(proposer_address)
    page = await client.get(
        "/api/v2/delegates/", safe=checksum(safe_address), limit=100
    )
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


async def propose_safe_tx(
    *,
    client: SafeTxServiceClient,
    safe_address: str,
    safe_tx: SafeTx,
    proposer_private_key: str,
    origin: str,
) -> str:
    payload, safe_tx_hash = await asyncio.to_thread(
        _build_safe_tx_proposal,
        safe_tx,
        proposer_private_key,
        origin,
    )
    await client.post(
        f"/api/v2/safes/{checksum(safe_address)}/multisig-transactions/",
        payload,
    )
    return safe_tx_hash


def _build_safe_tx_proposal(
    safe_tx: SafeTx,
    proposer_private_key: str,
    origin: str,
) -> tuple[dict[str, Any], str]:
    safe_tx.sign(proposer_private_key)
    if not safe_tx.sorted_signers:
        raise SafeTxCreateError("Safe tx proposal signature was not produced")
    safe_tx_hash = to_0x_hex_str(safe_tx.safe_tx_hash)
    return (
        {
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
        },
        safe_tx_hash,
    )


def proposer_address_from_key(proposer_private_key: str) -> str:
    return checksum(Account.from_key(proposer_private_key).address)


def _response_json_or_none(response: httpx.Response) -> dict[str, Any] | None:
    if not response.content:
        return None
    data = response.json()
    return data if isinstance(data, dict) else None
