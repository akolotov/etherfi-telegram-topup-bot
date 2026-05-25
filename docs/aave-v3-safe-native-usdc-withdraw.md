# Aave v3 native USDC Safe top-up transaction

Date: 2026-05-20

This document describes how the bot should compose the Arbitrum Safe Wallet
transaction when the Safe's liquidity is held as native USDC supplied to Aave
v3.

The key simplification is that Aave v3 `Pool.withdraw(asset, amount, to)` already
withdraws the underlying asset and sends it to `to`. For this bot, the Safe does
not need a two-call transaction:

1. Do not withdraw USDC to the Safe and then call `USDC.transfer(...)`.
2. Do not use `MultiSendCallOnly` for this path.
3. Create one Safe transaction whose target is the Aave v3 Pool and whose data
   is `withdraw(nativeUsdc, amount, targetAccountOnArbitrum)`.

The Safe is `msg.sender` for the Pool call, so Aave burns the Safe's aUSDC and
sends native USDC directly to the target account.

## Verified Arbitrum contracts

All addresses are on Arbitrum One, chain ID `42161`.

| Component | Address | Notes |
| --- | --- | --- |
| Aave v3 Pool proxy | `0x794a61358D6845594F94dc1DB02A252b5b4814aD` | Blockscout labels it as Aave v3 official Pool Proxy. |
| Aave v3 Pool implementation | `0xb76c1a8da369FC39AAdCF39D2446828BcDF6Ee56` | Verified as `L2PoolInstance`. |
| Native USDC | `0xaf88d065e77c8cC2239327C5EDb3A432268e5831` | Circle native USDC on Arbitrum, not bridged `USDC.e`. |
| Aave v3 native USDC aToken | `0x724dc807b04555b71ed48a6896b6F41593b8C637` | Returned by `Pool.getReserveAToken(nativeUsdc)`. |

`native USDC.decimals()` and `aUSDC.decimals()` both return `6`.

## Balance check

Before proposing the Safe transaction, check that the Safe has enough Aave v3
native USDC supply balance:

```python
chain_id = 42161
aave_pool = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"
native_usdc = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
a_usdc = "0x724dc807b04555b71ed48a6896b6F41593b8C637"
decimals = 6
```

Recommended read path:

1. Read or configure the aToken address for native USDC.
   - Static config is acceptable if guarded by startup validation.
   - Runtime validation can call `Pool.getReserveAToken(native_usdc)` and expect
     `0x724dc807b04555b71ed48a6896b6F41593b8C637`.
2. Read `aUSDC.balanceOf(safe_account)` through Blockscout.
3. Normalize by `10 ** 6`.
4. Require `a_usdc_balance >= withdraw_amount`.

Minimal ABI for the two read calls:

```json
{
  "inputs": [{"internalType": "address", "name": "asset", "type": "address"}],
  "name": "getReserveAToken",
  "outputs": [{"internalType": "address", "name": "", "type": "address"}],
  "stateMutability": "view",
  "type": "function"
}
```

```json
{
  "inputs": [{"internalType": "address", "name": "user", "type": "address"}],
  "name": "balanceOf",
  "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
  "stateMutability": "view",
  "type": "function"
}
```

`balanceOf` is preferred over manually reading scaled aToken state. Aave aToken
`balanceOf` already returns the current interest-accrued underlying-denominated
balance.

## Safe transaction

The Safe transaction should be a normal call:

| Safe field | Value |
| --- | --- |
| `to` | Aave v3 Pool proxy: `0x794a61358D6845594F94dc1DB02A252b5b4814aD` |
| `value` | `0` |
| `data` | ABI-encoded `withdraw(nativeUsdc, amount, targetAccountOnArbitrum)` |
| `operation` | `0` (`CALL`) |
| `safeTxGas` | `0` on Arbitrum for this no-RPC bot path |
| `baseGas` | `0` |
| `gasPrice` | `0` |
| `gasToken` | zero address |
| `refundReceiver` | zero address |

Minimal ABI for the call:

```json
{
  "inputs": [
    {"internalType": "address", "name": "asset", "type": "address"},
    {"internalType": "uint256", "name": "amount", "type": "uint256"},
    {"internalType": "address", "name": "to", "type": "address"}
  ],
  "name": "withdraw",
  "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
  "stateMutability": "nonpayable",
  "type": "function"
}
```

Transaction Builder shape:

```json
{
  "version": "1.0",
  "chainId": "42161",
  "meta": {
    "name": "ether.fi Aave native USDC top-up",
    "description": "Withdraw native USDC from Aave v3 to target account",
    "txBuilderVersion": "1.16.5",
    "createdFromSafeAddress": "0xSAFE",
    "createdFromOwnerAddress": ""
  },
  "transactions": [
    {
      "to": "0x794a61358D6845594F94dc1DB02A252b5b4814aD",
      "value": "0",
      "data": null,
      "contractMethod": {
        "name": "withdraw",
        "inputs": [
          {"internalType": "address", "name": "asset", "type": "address"},
          {"internalType": "uint256", "name": "amount", "type": "uint256"},
          {"internalType": "address", "name": "to", "type": "address"}
        ],
        "payable": false
      },
      "contractInputsValues": {
        "asset": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        "amount": "1000000",
        "to": "0xTARGET_ON_ARBITRUM"
      }
    }
  ]
}
```

Direct `SafeTx` construction should use the same fields without wrapping them in
MultiSend:

```python
safe_tx = SafeTx(
    None,
    checksum(safe_address),
    checksum("0x794a61358D6845594F94dc1DB02A252b5b4814aD"),
    0,
    encode_contract_method(
        {
            "name": "withdraw",
            "inputs": [
                {"name": "asset", "type": "address"},
                {"name": "amount", "type": "uint256"},
                {"name": "to", "type": "address"},
            ],
        },
        {
            "asset": checksum("0xaf88d065e77c8cC2239327C5EDb3A432268e5831"),
            "amount": withdraw_amount_base_units,
            "to": checksum(target_account_on_arbitrum),
        },
    ),
    SafeOperationEnum.CALL.value,
    0,
    0,
    0,
    None,
    None,
    safe_nonce=int(safe_info["nonce"]),
    safe_version=safe_info["version"],
    chain_id=42161,
)
```

After signing, post this Safe transaction to the Arbitrum Safe Transaction
Service exactly as described in `docs/safe-transaction-service-api-research.md`.

## Amount calculation

Use native USDC base units:

```python
withdraw_amount_base_units = decimal_to_base_units(top_up_amount, decimals=6)
```

For the current bot flow, `top_up_amount` is still:

```text
target_account_max_balance - fresh_target_account_balance
```

The target account balance is measured in the bot's configured balance network.
The Safe transaction sends native USDC to the same EVM address on Arbitrum unless
the config is extended with a separate `target_account_arbitrum`.

Use an exact `amount` for the top-up. Aave supports `type(uint256).max` to
withdraw all supplied balance, but that is not appropriate for this bot because
the intended top-up amount is bounded by `target_account_max_balance`.

## Failure conditions

The aUSDC balance check is necessary, but it is not a complete simulation.
`withdraw` can still revert if:

- the Safe has debt positions and withdrawing USDC would violate Aave health
  factor constraints;
- the native USDC reserve is paused, frozen, or lacks enough available liquidity;
- the target address or Safe address is blocked by native USDC token controls;
- the transaction was built with the bridged `USDC.e` address instead of native
  USDC.

If the Safe only supplies native USDC and has no debt position, then
`aUSDC.balanceOf(safe) >= amount` is the practical preflight check needed for
this bot.

## Blockscout verification notes

The addresses and ABI entries above were verified through the Blockscout MCP
Server on Arbitrum One:

- `get_address_info(42161, Aave Pool)` identifies the Pool proxy and
  implementation.
- `get_contract_abi(42161, Pool implementation)` exposes
  `withdraw(address,uint256,address)` and `getReserveAToken(address)`.
- `read_contract(Pool.getReserveAToken(nativeUSDC))` returned
  `0x724dc807b04555b71ed48a6896b6F41593b8C637`.
- `read_contract(nativeUSDC.decimals())` returned `6`.
- `read_contract(aUSDC.decimals())` returned `6`.
- The verified Pool source delegates `withdraw` to
  `SupplyLogic.executeWithdraw` with `user = msg.sender` and `to = to`.
