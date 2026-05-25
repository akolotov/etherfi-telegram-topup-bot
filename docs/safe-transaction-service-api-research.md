# Safe Transaction Service API research for the top-up bot

Date: 2026-05-20

This document describes how to use Safe Transaction Service API for this bot as a
Safe Wallet Proposer without talking to an RPC node. The rule assumed throughout
this document is strict: all Safe Wallet reads and writes are done through
`https://api.safe.global/tx-service/...`.

In Safe Wallet UI, the "Proposer" feature is implemented through Transaction
Service delegates. For this bot, "proposer" means a registered delegate address
that can create trusted queued proposals in the Transaction Service, but cannot
produce threshold approvals for execution.

The short answer:

- Use the Arbitrum Transaction Service base URL:
  `https://api.safe.global/tx-service/arb1`.
- Use `SAFE_TRANSACTION_SERVICE_API_KEY` as a bearer token.
- Register the bot address as a Safe Wallet Proposer before runtime. In API
  terms, verify it through `GET /api/v2/delegates/?safe={safe}`.
- Use the service `GET /api/v1/safes/{safe}/` to read Safe nonce, threshold and
  Safe version.
- Build `SafeTx` locally with `safe_eth.safe.SafeTx`, but pass
  `ethereum_client=None` and explicitly provide `safe_nonce`, `safe_version`, and
  `chain_id=42161`.
- Sign the proposal payload with the bot's proposer private key. That signature
  authenticates the proposal to the Transaction Service; it is not a Safe
  execution approval.
- Post the signed proposal with `POST /api/v2/safes/{safe}/multisig-transactions/`.
- Monitor by reading raw Transaction Service JSON. Do not use
  `TransactionServiceApi.get_safe_transaction()` in the no-RPC bot path.
- Remove a queued proposal with
  `DELETE /api/v2/multisig-transactions/{safe_tx_hash}/`, signed by the same
  proposer that created it.

## Bot mapping

Current bot ports:

- `SafeWalletClient.create_top_up_tx(user, amount, safe_proposer_private_key) -> str`
  should create one queued Safe transaction on Arbitrum and return the
  `safeTxHash`.
- `SafeWalletClient.get_tx_status(user, safe_tx_id) -> SafeTxStatus` should return
  `PENDING` until the Safe transaction is executed, deleted, or invalidated by a
  later Safe nonce. It should return `FINAL` for executed, failed-executed,
  deleted, or nonce-invalidated transactions.

The current config has `target_account`, `balance_token_address`, `safe_account`,
and `safe_proposer_key_file`. The configured file must contain the bot's
registered proposer private key, not a threshold-signing Safe owner key. Real
integration also needs enough information to know what the Arbitrum top-up
transaction is:

- `safe_chain_id`: fixed to `42161` for this bot.
- `safe_tx_service_base_url`: optional override, default
  `https://api.safe.global/tx-service/arb1`.
- `safe_proposer_key_file`: file path for the bot proposer private key.
- `top_up_asset_kind`: `native` or `erc20`.
- `top_up_token_address`: required for ERC-20 top-up on Arbitrum. Do not assume
  the Optimism `balance_token_address` is also the Arbitrum token address.
- `top_up_token_decimals`: required to convert the decimal bot amount to base
  units.
- `safe_multisend_call_only_address`: optional override. For Arbitrum,
  `safe-eth-py` deployment data currently lists
  `0x9641d764fc13c8B624c04430C7356C1C7C8102e2` for Safe v1.4.1
  MultiSendCallOnly and `0x40A2aCCbd92BCA938b02010E17A5b8929b49130D` for
  Safe v1.3.0 MultiSendCallOnly.

All addresses sent to the service should be checksum addresses. The service
returns `422` with code `1` for checksum validation failures.

## Service basics

Arbitrum One is `arb1` in `safe-eth-py`:

```python
from safe_eth.eth import EthereumNetwork
from safe_eth.safe.api import TransactionServiceApi

api = TransactionServiceApi(EthereumNetwork.ARBITRUM_ONE)
assert api.base_url == "https://api.safe.global/tx-service/arb1"
```

Authentication:

```http
Authorization: Bearer ${SAFE_TRANSACTION_SERVICE_API_KEY}
Accept: application/json
Content-Type: application/json
```

Safe's docs say unauthenticated access is possible for exploration, but
production use should use an API key for higher limits and reliability. The key
is a JWT managed in Safe's developer dashboard.

## Proposer-only model

Safe Wallet Proposers are the right runtime identity for this bot:

- A proposer can create a queued proposal that Safe Wallet users see as trusted.
- A proposer cannot execute the transaction by itself.
- A proposer signature must not be counted as one of the approvals required by
  the Safe threshold.
- A compromised proposer key can create misleading or malicious proposals, so the
  bot must keep proposal content simple, deterministic, and auditable.
- A compromised proposer key still cannot move funds without the normal Safe
  execution process.

The proposer is represented by the Transaction Service delegate API:

```text
GET  /api/v2/delegates/?safe={safe}
POST /api/v2/delegates/
DELETE /api/v2/delegates/{delegate}/
```

Preferred provisioning is through Safe Wallet UI, where the Proposers feature is
managed for the Safe. The bot runtime should only verify that its address is
already registered:

```python
from eth_account import Account


def proposer_address_from_key(proposer_private_key: str) -> str:
    return checksum(Account.from_key(proposer_private_key).address)


def require_registered_proposer(
    client: SafeTxServiceClient,
    safe_address: str,
    proposer_address: str,
) -> None:
    proposer_address = checksum(proposer_address)
    page = client.get("/api/v2/delegates/", safe=checksum(safe_address), limit=100)
    delegates = page.get("results", [])
    if not any(checksum(row["delegate"]) == proposer_address for row in delegates):
        raise PermissionError(
            f"{proposer_address} is not registered as proposer for {safe_address}"
        )
```

The API also exposes `POST /api/v2/delegates/` and
`DELETE /api/v2/delegates/{delegate}/` for separate admin tooling, but this
document intentionally keeps the bot runtime on the proposer-only path. The only
private key loaded by the bot should be the proposer key.

Local smoke receipt on 2026-05-20:

```text
GET /tx-service/arb1/api/v1/about/
200 {"name": "Safe Transaction Service", "version": "6.3.0", "api_version": "v1"}
```

## What `safe-eth-py` can and cannot do without RPC

Useful without RPC:

- `TransactionServiceApi.get_transactions(safe, ...)` returns raw list entries
  and does not need an `EthereumClient`, unless `safe_tx_hash` validation is
  requested.
- `TransactionServiceApi.post_transaction(safe_tx)` can post a locally built
  `SafeTx` signed by the registered proposer if the `SafeTx` has all
  hash-critical fields already set.
- `TransactionServiceApi.delete_transaction(safe_tx_hash, signature)` only
  needs the delete signature from the transaction proposer.
- `SafeTx(None, ..., safe_nonce=..., safe_version=..., chain_id=...)` can compute
  the Safe transaction hash and sign the proposal payload without an RPC client.
- `get_remove_transaction_message(...)` and `eip712_encode_hash(...)` can build
  and hash the deletion EIP-712 message without RPC.

Not safe for the bot's no-RPC path:

- `TransactionServiceApi.get_safe_transaction(safe_tx_hash)` attempts to build a
  `TransactionServiceTx` and validate `safe_tx_hash`. In the current main branch
  of `safe-eth-py`, that object does not receive `safe_version`, so with
  `ethereum_client=None` it tries to access `None.w3` and fails.
- `Safe.build_multisig_tx(...)`, `Safe.retrieve_nonce()`,
  `Safe.retrieve_threshold()`, gas estimation, `safe_tx.call()`, and
  `safe_tx.execute(...)` are RPC-backed paths. Do not use them in this bot.
- `safe-cli tx-builder` itself executes through `SafeOperator`, which is
  RPC-backed. Reuse only its tx-builder file decoder logic, not the execution
  command path.

Experiment receipt from a temporary venv with `safe-eth-py` main:

```text
SafeTx(None, ..., safe_nonce=18, safe_version="1.3.0+L2", chain_id=42161).sign(...)
worked and produced a 32-byte safeTxHash and 65-byte EOA signature.

TransactionServiceApi(EthereumNetwork.SEPOLIA, ethereum_client=None).get_transactions(...)
worked for a public docs Safe.

TransactionServiceApi(..., ethereum_client=None).get_safe_transaction(...)
failed with: AttributeError: 'NoneType' object has no attribute 'w3'
```

## Minimal HTTP client

Use a small wrapper around `requests`. It is clearer than relying on private
`TransactionServiceApi._get_request()` for raw JSON reads.

```python
from __future__ import annotations

import os
from typing import Any

import requests
from web3 import Web3

ARBITRUM_CHAIN_ID = 42161
ARBITRUM_TX_SERVICE = "https://api.safe.global/tx-service/arb1"


class SafeTxServiceError(RuntimeError):
    pass


class SafeTxServiceClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = ARBITRUM_TX_SERVICE,
        timeout: int = 15,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        api_key = api_key or os.environ.get("SAFE_TRANSACTION_SERVICE_API_KEY")
        if api_key:
            self.session.headers["Authorization"] = f"Bearer {api_key}"

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        response = self.session.get(
            f"{self.base_url}{path}",
            params={k: v for k, v in params.items() if v is not None},
            timeout=self.timeout,
        )
        return self._json_or_raise(response)

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        response = self.session.post(
            f"{self.base_url}{path}",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=self.timeout,
        )
        if response.status_code == 201:
            return None if not response.content else response.json()
        return self._json_or_raise(response)

    def delete(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        response = self.session.delete(
            f"{self.base_url}{path}",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=self.timeout,
        )
        if response.status_code in {200, 202, 204}:
            return None if not response.content else response.json()
        return self._json_or_raise(response)

    def _json_or_raise(self, response: requests.Response) -> dict[str, Any]:
        if response.ok:
            return response.json()
        try:
            payload = response.json()
        except ValueError:
            payload = response.text
        raise SafeTxServiceError(f"{response.status_code}: {payload!r}")


def checksum(address: str) -> str:
    return Web3.to_checksum_address(address)
```

## Monitor transactions

Endpoints:

- `GET /api/v1/safes/{address}/` returns Safe status: nonce, threshold,
  version, modules, guards, and other metadata.
- `GET /api/v2/safes/{address}/multisig-transactions/` lists multisig
  transactions. Useful filters include `executed`, `trusted`, `nonce`,
  `nonce__lt`, `nonce__gt`, `safe_tx_hash`, `to`, `value`, `failed`, `limit`,
  and `offset`.
- `GET /api/v2/multisig-transactions/{safe_tx_hash}/` reads one transaction as
  raw JSON.

Raw transaction fields relevant to this bot:

- `safe`, `to`, `value`, `data`, `operation`
- `nonce`
- `safeTxHash`
- `transactionHash`
- `isExecuted`
- `isSuccessful`
- `confirmationsRequired`
- `confirmations`
- `trusted`
- `proposer`
- `proposedByDelegate`
- `origin`

Status mapping:

```python
from enum import Enum


class SafeTxStatus(str, Enum):
    PENDING = "PENDING"
    FINAL = "FINAL"


def get_safe_info(client: SafeTxServiceClient, safe_address: str) -> dict:
    return client.get(f"/api/v1/safes/{checksum(safe_address)}/")


def get_raw_multisig_tx(client: SafeTxServiceClient, safe_tx_hash: str) -> dict | None:
    try:
        return client.get(f"/api/v2/multisig-transactions/{safe_tx_hash}/")
    except SafeTxServiceError as error:
        # Deleted queued txs and unknown txs should be treated as terminal by the
        # bot, because the service no longer has a pending proposal to remind on.
        if str(error).startswith("404:"):
            return None
        raise


def get_bot_tx_status(
    client: SafeTxServiceClient,
    safe_address: str,
    safe_tx_hash: str,
) -> SafeTxStatus:
    tx = get_raw_multisig_tx(client, safe_tx_hash)
    if tx is None:
        return SafeTxStatus.FINAL

    if tx["isExecuted"]:
        # Includes successful execution and failed execution. Either way, the
        # Safe nonce has been consumed.
        return SafeTxStatus.FINAL

    safe_info = get_safe_info(client, safe_address)
    current_nonce = int(safe_info["nonce"])
    tx_nonce = int(tx["nonce"])
    if current_nonce > tx_nonce:
        # A different tx with the same nonce executed, for example a rejection
        # transaction. The queued proposal cannot execute anymore.
        return SafeTxStatus.FINAL

    return SafeTxStatus.PENDING
```

Useful list query for deduplication and admin views:

```python
def list_pending_bot_txs(client: SafeTxServiceClient, safe_address: str) -> list[dict]:
    page = client.get(
        f"/api/v2/safes/{checksum(safe_address)}/multisig-transactions/",
        executed="False",
        trusted="True",
        limit=20,
    )
    return [
        tx for tx in page["results"]
        if (tx.get("origin") or "").startswith("ether.fi-bot:")
    ]
```

Recommendations for the bot:

- Persist the returned `safeTxHash` as `pending_safe_tx_id`.
- On every balance tick in S4, call `get_bot_tx_status(...)`.
- Treat `FINAL` as "clear Safe pending context" exactly as the FSM already does.
- Keep using the Optimism balance as the main finality signal. If balance is OK,
  clear pending even if Safe status read fails.
- If Safe status fails and balance is still low, keep S4 and send an admin error.
- Do not send low-balance prompts while S4 is pending; send only Safe pending
  reminders after the existing cooldown.

## Compose transactions with Transaction Builder JSON

Safe Transaction Builder export format contains a top-level `transactions` array.
Each item usually has:

- `to`
- `value`
- `data`
- `contractMethod`
- `contractInputsValues`

If `data` is present, use it directly. If `data` is `null`, encode the ABI call
from `contractMethod` and `contractInputsValues`.

Native ETH top-up builder:

```json
{
  "version": "1.0",
  "chainId": "42161",
  "meta": {
    "name": "ether.fi top-up",
    "description": "Top up target account from Safe",
    "txBuilderVersion": "1.16.5",
    "createdFromSafeAddress": "0xSAFE"
  },
  "transactions": [
    {
      "to": "0xTARGET",
      "value": "100000000000000000",
      "data": "0x"
    }
  ]
}
```

ERC-20 top-up builder:

```json
{
  "version": "1.0",
  "chainId": "42161",
  "meta": {
    "name": "ether.fi top-up",
    "description": "Top up target account from Safe",
    "txBuilderVersion": "1.16.5",
    "createdFromSafeAddress": "0xSAFE"
  },
  "transactions": [
    {
      "to": "0xARBITRUM_TOKEN",
      "value": "0",
      "data": null,
      "contractMethod": {
        "name": "transfer",
        "inputs": [
          {"internalType": "address", "name": "to", "type": "address"},
          {"internalType": "uint256", "name": "amount", "type": "uint256"}
        ],
        "payable": false
      },
      "contractInputsValues": {
        "to": "0xTARGET",
        "amount": "1000000"
      }
    }
  ]
}
```

Minimal decoder for common bot transactions:

```python
from dataclasses import dataclass
from typing import Any

from eth_abi import encode as encode_abi
from hexbytes import HexBytes
from web3 import Web3


@dataclass(frozen=True)
class BuilderTx:
    to: str
    value: int
    data: bytes


def parse_uint(value: str | int) -> int:
    return int(str(value), 0)


def parse_abi_value(solidity_type: str, value: Any) -> Any:
    if solidity_type.startswith("uint") or solidity_type.startswith("int"):
        return parse_uint(value)
    if solidity_type == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"true", "1"}
    if solidity_type.startswith("bytes"):
        return HexBytes(value)
    return value


def encode_contract_method(method: dict[str, Any], values: dict[str, Any]) -> HexBytes:
    name = method["name"]
    inputs = method.get("inputs", [])
    types = [item["type"] for item in inputs]
    args = [parse_abi_value(item["type"], values[item["name"]]) for item in inputs]
    selector = Web3.keccak(text=f"{name}({','.join(types)})")[:4]
    return HexBytes(selector + encode_abi(types, args))


def tx_builder_to_calls(builder: dict[str, Any], expected_chain_id: int = 42161) -> list[BuilderTx]:
    chain_id = int(str(builder["chainId"]), 10)
    if chain_id != expected_chain_id:
        raise ValueError(f"tx builder chainId {chain_id} != expected {expected_chain_id}")

    calls: list[BuilderTx] = []
    for item in builder["transactions"]:
        data = item.get("data")
        if data is None:
            data = encode_contract_method(
                item["contractMethod"],
                item.get("contractInputsValues", {}),
            )
        calls.append(
            BuilderTx(
                to=checksum(item["to"]),
                value=parse_uint(item.get("value", "0")),
                data=HexBytes(data or "0x"),
            )
        )
    if not calls:
        raise ValueError("tx builder contains no transactions")
    return calls
```

For full Transaction Builder compatibility, reuse or vendor Safe CLI's
`tx_builder_file_decoder.py`. It handles more cases than the minimal decoder:
tuples, arrays, booleans, bytes, and hex integers. Keep in mind that importing
`safe_cli` as a runtime dependency is heavier than copying the small decoder.

## Create a new transaction composed with tx builder

Service endpoint:

```text
POST /api/v2/safes/{safe_address}/multisig-transactions/
```

Low-level payload field names used by `safe-eth-py`:

```json
{
  "to": "0x...",
  "value": 0,
  "data": "0x...",
  "operation": 0,
  "gasToken": "0x0000000000000000000000000000000000000000",
  "safeTxGas": 0,
  "baseGas": 0,
  "gasPrice": 0,
  "refundReceiver": "0x0000000000000000000000000000000000000000",
  "nonce": 18,
  "contractTransactionHash": "0x...",
  "sender": "0xPROPOSER",
  "signature": "0xPROPOSER_SIGNATURE",
  "origin": "ether.fi-bot:telegram-user-id:top-up"
}
```

Safe's TypeScript docs call the same concepts `safeTxHash`, `senderAddress`,
and `senderSignature`; API Kit maps those names to the REST payload.

No-RPC creation snippet:

```python
from decimal import Decimal

from hexbytes import HexBytes
from packaging.version import Version
from safe_eth.safe import SafeOperationEnum, SafeTx
from safe_eth.safe.multi_send import MultiSend, MultiSendOperation, MultiSendTx
from safe_eth.safe.safe_deployments import safe_deployments
from safe_eth.util.util import to_0x_hex_str

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def select_multisend_call_only(chain_id: int) -> str:
    versions = sorted(safe_deployments.keys(), key=Version, reverse=True)
    for version in versions:
        deployment = safe_deployments[version].get("MultiSendCallOnly", {})
        addresses = deployment.get(str(chain_id))
        if addresses:
            return checksum(addresses[0])
    raise RuntimeError(f"No MultiSendCallOnly deployment for chain {chain_id}")


def build_safe_tx_from_calls(
    *,
    safe_address: str,
    safe_info: dict,
    calls: list[BuilderTx],
    chain_id: int = ARBITRUM_CHAIN_ID,
) -> SafeTx:
    safe_address = checksum(safe_address)
    safe_nonce = int(safe_info["nonce"])
    safe_version = safe_info["version"]

    if len(calls) == 1:
        call = calls[0]
        return SafeTx(
            None,
            safe_address,
            call.to,
            call.value,
            call.data,
            SafeOperationEnum.CALL.value,
            0,
            0,
            0,
            None,
            None,
            safe_nonce=safe_nonce,
            safe_version=safe_version,
            chain_id=chain_id,
        )

    multisend_address = select_multisend_call_only(chain_id)
    multisend = MultiSend(address=multisend_address, call_only=True)
    multisend_calls = [
        MultiSendTx(MultiSendOperation.CALL, call.to, call.value, call.data)
        for call in calls
    ]
    return SafeTx(
        None,
        safe_address,
        multisend.address,
        0,
        multisend.build_tx_data(multisend_calls),
        SafeOperationEnum.DELEGATE_CALL.value,
        0,
        0,
        0,
        None,
        None,
        safe_nonce=safe_nonce,
        safe_version=safe_version,
        chain_id=chain_id,
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
    sender = safe_tx.sorted_signers[0]
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
        "sender": sender,
        # Match safe-eth-py's TransactionServiceApi.post_transaction payload:
        # concatenated sorted proposal signatures as hex without the 0x prefix.
        "signature": safe_tx.signatures.hex() if safe_tx.signatures else None,
        "origin": origin,
    }
    client.post(
        f"/api/v2/safes/{checksum(safe_address)}/multisig-transactions/",
        payload,
    )
    return safe_tx_hash
```

Bot use:

```python
def create_top_up_from_builder(
    *,
    client: SafeTxServiceClient,
    safe_address: str,
    proposer_private_key: str,
    builder_json: dict,
    telegram_user_id: int,
) -> str:
    safe_info = get_safe_info(client, safe_address)
    proposer_address = proposer_address_from_key(proposer_private_key)
    require_registered_proposer(client, safe_address, proposer_address)
    calls = tx_builder_to_calls(builder_json)
    safe_tx = build_safe_tx_from_calls(
        safe_address=safe_address,
        safe_info=safe_info,
        calls=calls,
    )
    return propose_safe_tx(
        client=client,
        safe_address=safe_address,
        safe_tx=safe_tx,
        proposer_private_key=proposer_private_key,
        origin=f"ether.fi-bot:{telegram_user_id}:top-up",
    )
```

The `signature` above authenticates the proposal to the Transaction Service. It
does not add an execution approval and should not be displayed to users as if it
reduced `confirmationsRequired`.

Amount conversion for ERC-20:

```python
def decimal_to_base_units(amount: Decimal, decimals: int) -> int:
    scale = Decimal(10) ** decimals
    base_units = amount * scale
    if base_units != base_units.to_integral_value():
        raise ValueError(f"amount {amount} has more precision than {decimals} decimals")
    return int(base_units)
```

Important nonce behavior:

- Use `GET /api/v1/safes/{safe}/` and the returned `nonce` for a new normal
  proposal.
- Multiple queued proposals can share the same nonce. They are alternatives; only
  one can execute.
- The FSM already prevents duplicate creation while `pending_safe_tx_id` exists.
  On restart, if state is lost or manually edited, search `origin` and
  `executed=False` to avoid duplicate bot proposals.
- If the service returns "Nonce already executed", refresh Safe info and clear or
  recreate according to the latest balance.

Gas behavior on Arbitrum:

- Safe docs say multisig gas estimation is disabled for L2 networks and only
  needed for old Safes. Arbitrum Safes should normally use `safeTxGas=0`,
  `baseGas=0`, `gasPrice=0`, `gasToken=0x0`, and `refundReceiver=0x0`.
- The bot should not call RPC-backed `safe_tx.call()` or `safe_tx.execute()` to
  validate. Validation happens when users inspect/sign/execute in Safe Wallet.

## Remove transaction

Service endpoint:

```text
DELETE /api/v2/multisig-transactions/{safe_tx_hash}/
```

Who can delete:

- Only the proposer that created the transaction.
- The proposer must still be valid for that Safe.
- The deletion signature is not a normal SafeTx signature. It is an EIP-712
  `DeleteRequest` over `safeTxHash` plus an hourly TOTP.

No-RPC delete snippet:

```python
from eth_account import Account
from hexbytes import HexBytes
from safe_eth.eth.eip712 import eip712_encode_hash
from safe_eth.safe.api.transaction_service_api.transaction_service_messages import (
    get_remove_transaction_message,
)
from safe_eth.util.util import to_0x_hex_str


def delete_queued_transaction(
    *,
    client: SafeTxServiceClient,
    safe_address: str,
    safe_tx_hash: str,
    proposer_private_key: str,
    chain_id: int = ARBITRUM_CHAIN_ID,
) -> None:
    message = get_remove_transaction_message(
        checksum(safe_address),
        HexBytes(safe_tx_hash),
        chain_id,
    )
    digest = eip712_encode_hash(message)
    signature = Account.from_key(proposer_private_key).unsafe_sign_hash(digest).signature

    client.delete(
        f"/api/v2/multisig-transactions/{safe_tx_hash}/",
        {
            "safeTxHash": safe_tx_hash,
            "signature": to_0x_hex_str(signature),
        },
    )
```

Limitations:

- This removes the queued proposal from the Transaction Service. It does not send
  an on-chain cancellation transaction.
- If the deleted proposal already had enough signatures, a third party who has
  the signatures may still execute it until the Safe nonce is consumed by another
  transaction.
- True invalidation requires executing a different transaction with the same
  nonce, commonly a rejection transaction. That execution is outside this bot's
  no-RPC scope and should be done in Safe Wallet by users.
- Generate and submit the delete signature promptly. The message includes an
  hourly TOTP.

The Safe CLI reference implementation does the same:

1. Build `get_remove_transaction_message(safe, safe_tx_hash, chain_id)`.
2. EIP-712 hash it.
3. Sign it with the transaction proposer.
4. Call `safe_tx_service.delete_transaction(safe_tx_hash, signature)`.

## Recommended `SafeWalletClient` behavior

`create_top_up_tx`:

1. Checksum `user.safe_account`, `user.target_account`, and token addresses.
2. Fetch Safe info from `GET /api/v1/safes/{safe}/`.
3. Verify the private key from `safe_proposer_key_file` resolves to a registered
   proposer by checking
   `GET /api/v2/delegates/?safe={safe}`.
4. Convert decimal amount to base units using the configured Arbitrum asset
   decimals.
5. Build Transaction Builder JSON, or build equivalent `BuilderTx` calls.
6. Convert calls into a direct SafeTx or MultiSend SafeTx.
7. Sign the proposal payload locally with the proposer key.
8. Post the proposal.
9. Return `safeTxHash`.

`get_tx_status`:

1. Raw `GET /api/v2/multisig-transactions/{safe_tx_hash}/`.
2. If 404, return `FINAL`.
3. If `isExecuted` is true, return `FINAL`.
4. Fetch Safe info. If `int(safe_info["nonce"]) > int(tx["nonce"])`, return
   `FINAL`.
5. Otherwise return `PENDING`.

`remove_tx` if added later:

1. Raw `GET /api/v2/multisig-transactions/{safe_tx_hash}/`.
2. Ensure `isExecuted` is false.
3. Ensure the bot still has the same proposer key that created the proposal.
4. Sign and submit the delete request.
5. Clear local `pending_safe_tx_id` only after delete success or after status
   subsequently reads as 404/final.

## Operational limitations and gotchas

- The Transaction Service is an indexer. It can lag chain state. The bot already
  has a separate Optimism balance read; keep that as the strongest "top-up
  worked" signal.
- The service queues and tracks Safe transactions. It does not execute this
  bot's top-up. Safe Wallet users still need to approve and execute it.
- The bot cannot prove a transaction would execute without simulation. Simulation
  and execution are RPC-backed and outside the allowed integration scope.
- A proposer signature only proves who created the queued proposal. It is not an
  execution approval and cannot satisfy the Safe threshold.
- The v2 `dataDecoded` response field is deprecated in docs. If a UI/debugger
  needs decoding, use Safe Decoder Service or local ABI decoding.
- API limits are account-level, not per key. Adding more keys does not increase
  quota.
- `safe-eth-py` main currently reports version `7.21.0`; `safe-cli` main requires
  `safe-eth-py>=7.20.0`. Before implementation, verify the installed package has
  `TransactionServiceApi.delete_transaction` and the Arbitrum `NETWORK_SHORTNAME`
  entry. Install from GitHub if the package index provides an older build.
- Do not let logs include private keys, bearer tokens, signatures, or full
  request payloads. `safeTxHash`, Safe address, nonce, status, and HTTP status
  are enough for normal logs.

## Source references

- Safe Wallet Proposers:
  <https://help.safe.global/articles/1671337645-proposers>
- Safe Transaction Service API reference:
  <https://docs.safe.global/core-api/transaction-service-reference/arbitrum>
- Safe API authentication:
  <https://docs.safe.global/core-api/how-to-use-api-keys>
- `safe-eth-py` TransactionServiceApi:
  <https://github.com/safe-global/safe-eth-py/blob/main/safe_eth/safe/api/transaction_service_api/transaction_service_api.py>
- `safe-eth-py` base API auth headers:
  <https://github.com/safe-global/safe-eth-py/blob/main/safe_eth/safe/api/base_api.py>
- `safe-eth-py` SafeTx hashing/signing:
  <https://github.com/safe-global/safe-eth-py/blob/main/safe_eth/safe/safe_tx.py>
- `safe-eth-py` delete message helper:
  <https://github.com/safe-global/safe-eth-py/blob/main/safe_eth/safe/api/transaction_service_api/transaction_service_messages.py>
- `safe-cli` Transaction Service operator:
  <https://github.com/safe-global/safe-cli/blob/main/src/safe_cli/operators/safe_tx_service_operator.py>
- `safe-cli` Transaction Builder decoder:
  <https://github.com/safe-global/safe-cli/blob/main/src/safe_cli/tx_builder/tx_builder_file_decoder.py>
