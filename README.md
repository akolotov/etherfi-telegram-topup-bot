# ether.fi Telegram Top-Up Bot

Telegram bot that monitors configured users and starts the top-up flow when a target account needs attention.

## Setup

Create and activate a Python 3.12 virtual environment, then install the project:

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
```

Copy `.env.example` to `.env` and set `BOT_TOKEN` and `BLOCKSCOUT_PRO_API_KEY`:

```bash
cp .env.example .env
```

Configure allowed Telegram users in `data/config.json`. The bot only reacts to Telegram user IDs listed there. Each user also sets `balance_token_address`, which is the token checked for the target account balance.

## Balance Check Interval

Balance checks read Optimism token balances through Blockscout PRO API on chain id `10`.
Use `balance_check_interval_seconds >= 60` for 1-3 users on the Blockscout Free tier.
For larger user counts, see [docs/balance-check-interval.md](docs/balance-check-interval.md).

## Run

The current runtime uses Telegram polling:

```bash
.venv/bin/python -m etherfi_bot.runtime
```

Useful environment overrides:

- `CONFIG_PATH`: path to the JSON bot config, default `data/config.json`
- `STATE_DIR`: persisted FSM state directory, default `data/user_states`
- `POLLING_OFFSET_PATH`: persisted Telegram polling offset, default `data/polling_offset.json`
- `POLLING_PENDING_UPDATE_PATH`: pending Telegram update recovery file, default `data/polling_pending_update.json`
- `POLL_TIMEOUT_SECONDS`: Telegram long-poll timeout, default `25`
- `LOG_LEVEL`: runtime log level, default `INFO`
- `BLOCKSCOUT_PRO_API_KEY`: Blockscout PRO API key, required for balance checks

## Test

```bash
.venv/bin/python -m pytest -q
```
