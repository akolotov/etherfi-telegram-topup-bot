# Balance Check Interval

Balance checks read Optimism token balances through Blockscout PRO API on chain id `10`.
The configured `balance_threshold` is already denominated, so the runtime compares it
with `token balance / 10^decimals`.

As of May 20, 2026, Blockscout's live
[`/api/json/config`](https://api.blockscout.com/api/json/config) has no endpoint-specific
price for `api/v2/addresses/:address_hash/token-balances`, so the `default` price applies:
`20` credits per balance check. The live Free plan from
[`/api/json/plans`](https://api.blockscout.com/api/json/plans) includes `100000`
credits per day and a `5 RPS` limit.

Use this estimate when choosing `balance_check_interval_seconds`:

```text
daily_credits = users_count * 86400 / balance_check_interval_seconds * 20
```

Recommended Free tier defaults:

- `balance_check_interval_seconds >= 60` for 1-3 users.
- 1 user at 60 seconds uses about 28,800 credits/day.
- 3 users at 60 seconds use about 86,400 credits/day.
- For more users, use at least `ceil(users_count * 21.6)` seconds, then round up to a convenient value. This leaves roughly a 20% daily credit buffer.
