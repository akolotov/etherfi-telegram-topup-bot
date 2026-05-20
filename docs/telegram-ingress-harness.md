# Telegram Ingress Harness

This project carries a transport-neutral Telegram testing harness migrated from the Wabelfish webhook harness.

The harness uses **Telegram ingress** as the shared term for both supported runtime modes:

- `polling`: early development mode, no public URL required.
- `webhook`: future production mode, public HTTPS endpoint required.

Polling now feeds raw Telegram updates through the shared update adapter before the adapter calls `BotDispatcher`.
Webhook remains a placeholder for a later iteration and should use the same adapter boundary.

## Migrated Artifacts

- `.codex/skills/telegram-bot-ingress-testing/SKILL.md`
  Project-specific Telegram Web testing workflow for polling and webhook.
- `.codex/skills/telegram-ingress-subagent-orchestration/SKILL.md`
  Parent-agent protocol for delegating Telegram Web work when delegation is explicitly requested.
- `.codex/agents/telegram_operator.toml`
  Specialized Telegram Web operator agent definition.
- `.ai/scripts/chrome-telegram-test.sh`
  Chrome launcher with remote debugging and a dedicated Telegram test profile.
- `.ai/tmp/telegram-update-fixture-harvest-methodology.md`
  Transport-neutral fixture harvesting runbook.
- `.ai/tmp/telegram-fixture-harvest-prompts.md`
  Bounded live-harvest prompts.
- `tests/smoke/telegram_api_mock.py`
  Local fake Telegram Bot API for polling and webhook smoke tests.
- `tests/fixtures/telegram_updates/`
  Seed raw update fixtures for future update-adapter tests.

## Runtime Contract

Future polling and webhook receivers should both produce a common call shape:

```text
raw Telegram update -> update adapter -> BotDispatcher method
```

Expected mappings:

- `/start` from configured private user -> `BotDispatcher.start(telegram_user_id)`
- `callback_query.data == "top_up"` -> `BotDispatcher.callback_top_up(telegram_user_id, message_id)`
- `callback_query.data == "ignore"` -> `BotDispatcher.callback_ignore(telegram_user_id, message_id)`
- private `my_chat_member` block -> `BotDispatcher.user_blocked(telegram_user_id)`
- ordinary messages, replies, reactions, unsupported callbacks -> `ignore_event` or no-op

## Polling Runtime

Start the development bot with:

```bash
.venv/bin/python -m etherfi_bot.runtime
```

The runtime loads `.env`, defaults `INGRESS_MODE` to `polling`, calls `deleteWebhook`, then polls `getUpdates`.
Required config is `BOT_TOKEN`; optional development overrides are `TELEGRAM_API_BASE_URL`, `CONFIG_PATH`, `STATE_DIR`, `POLLING_OFFSET_PATH`, and `POLL_TIMEOUT_SECONDS`.
Blockchain and Safe Wallet behavior use mocks in this phase.

## Fake Telegram API

The smoke mock supports:

- `getMe`
- `getUpdates`
- `setWebhook`
- `deleteWebhook`
- `sendMessage`
- `editMessageReplyMarkup`
- `answerCallbackQuery`
- `GET /healthz`
- `GET /__admin/state`
- `POST /__admin/reset`
- `POST /__admin/enqueue`

This lets early polling tests run without real Telegram or a public URL, while still keeping enough webhook surface for the later transition.

## Fixture Status

The committed fixtures are seed fixtures. `message_command_start`, `callback_query_top_up`, and `callback_query_ignore` are synthetic-derived from real Telegram shapes and should be replaced by live-harvested fixtures before being used as acceptance evidence.
