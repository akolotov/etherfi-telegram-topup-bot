---
name: telegram-bot-ingress-testing
description: Operate an ether.fi Telegram bot through Telegram Web and verify behavior through the configured Telegram ingress mode, either polling or webhook.
---

# Telegram Bot Ingress Testing

Use this skill when the current agent is responsible for Telegram Web interaction for the ether.fi bot.

For browser mechanics, also use the generic `playwright-cli` or browser automation skill available in the active environment. This skill defines the project-specific Telegram workflow and the runtime verification split.

## Scope

This skill is transport-neutral. Treat polling and webhook as two ingress profiles for the same raw Telegram update boundary.

Common flow:

1. A Telegram-side action creates a raw Telegram update.
2. The configured ingress profile receives the update.
3. The shared update handler maps the update to the FSM dispatcher.
4. Runtime evidence proves the expected state transition or no-op.

Do not describe the test as passed only because Telegram Web visually changed. Always verify runtime evidence.

## Artifact Location

Store generated workflow artifacts under `.ai/playwright-cli/`.

Before a test flow:

- Ensure the directory exists with `mkdir -p .ai/playwright-cli`.
- Save snapshots, screenshots, videos, storage state, and raw browser traces there.
- Use scenario names in filenames, for example `.ai/playwright-cli/start-after-click.yml` or `.ai/playwright-cli/top-up-callback-after-click.png`.
- Treat `.playwright-cli/` as tool-managed internals only.

## Runtime Profile

Resolve these facts before Telegram interaction:

- `INGRESS_MODE`: `polling` or `webhook`.
- Bot alias, for example `@etherfi_test_bot`.
- Runtime process/container/session that owns logs.
- State evidence path, normally `data/user_states/<telegram_user_id>.json` for the current FSM core.
- Configured allowlist source, normally `data/config.json` or environment.

If `INGRESS_MODE` is absent in early development, default to `polling` and state that assumption in the report.

## Ingress Verification

Use one of these profiles.

### Polling

Verify that the polling receiver observed and processed the update:

- the polling process was running before the Telegram action;
- logs or probe output mention the expected `update_id` or update class;
- the shared update handler invoked the intended dispatcher method;
- JSON state changed as expected, or stayed unchanged for ignored traffic;
- outbound mock/Telegram evidence matches the intended send or edit.

Useful checks once the polling adapter exists:

- poller logs: `ingress_update accepted`, `ingress_mode polling`, `telegram_update_id`;
- fake Telegram API requests: `getUpdates`, `sendMessage`, `editMessageReplyMarkup`, `answerCallbackQuery`;
- state files under `data/user_states/`.

### Webhook

Verify that the webhook receiver admitted and processed the update:

- webhook endpoint returned a successful status for valid Telegram delivery;
- secret validation passed when configured;
- logs mention the expected `update_id` or update class;
- the shared update handler invoked the intended dispatcher method;
- JSON state changed as expected, or stayed unchanged for ignored traffic.

Useful checks once webhook exists:

- `webhook_request` or `ingress_update` logs with `status=accepted`;
- HTTP status and response body;
- state files under `data/user_states/`.

## Default Telegram Web Workflow

1. Attach to the prepared Chrome session, usually `http://localhost:9222`.
2. Open `https://web.telegram.org/a/` if Telegram Web is not already open.
3. Search for the exact bot alias from runtime context.
4. Open the bot chat and inspect the current state.
5. If `START` is visible, click it only when the scenario needs a first start.
6. If `Restart Bot` is visible, click it only when the scenario needs reactivation after block.
7. If the composer is visible, send the scenario input directly.
8. After each meaningful action, capture a fresh snapshot.
9. Return after the requested checkpoint; do not continue into later scenario stages unless asked.

## Scenario Selection

Choose the Telegram action that creates the update shape needed by the test.

- `message_command_start`: use the bot chat `START` control or send `/start`.
- `callback_query_top_up`: first reach a real low-balance prompt, then click the current `Top Up` inline button.
- `callback_query_ignore`: first reach a real low-balance prompt, then click the current `Ignore` inline button.
- `my_chat_member_block`: use Telegram UI to block the bot; do not delete chat unless explicitly asked.
- `message_plain_text`: send ordinary text and verify it is ignored.
- `message_reply_text`: use Telegram native Reply and verify it is ignored.
- `message_reaction_*`: use Telegram reaction controls and verify they are ignored.

When a user asks for a generic smoke test, prefer `/start` for allowlisted users and plain text for ignored traffic.

## Expected Behavior Checkpoints

For the current FSM core:

- Unknown user `/start`: no state file should be created or changed.
- Allowlisted `/start`: state becomes `S1_MONITORING`.
- Plain text/reply/reaction: dispatcher should ignore or no-op.
- Current `Top Up` callback: current buttons are retired and Safe transaction flow starts.
- Current `Ignore` callback: current buttons are retired and the user returns to monitoring.
- Stale callback: no mutation and no button retirement.
- Block or outbound 403: state resets to `S0_NOT_STARTED`.

Use the code and tests as source of truth when behavior evolves.

## Result Contract

When reporting back, summarize:

- bot alias and ingress mode;
- Telegram-side action performed;
- visible Telegram checkpoint;
- runtime evidence checked;
- state file or outbound call evidence;
- artifacts created under `.ai/playwright-cli/`;
- whether the chat was reset or left open.

Prefer concise evidence over raw browser dumps.

## Anti-Patterns

- Do not assume missing visible bot replies means the test failed; check runtime evidence.
- Do not call a test webhook-specific when it only needs raw update ingress.
- Do not guess bot alias from Telegram search results; use parent-provided runtime identity.
- Do not use reply, forward, edit, or reaction shortcuts when the scenario depends on Telegram-generated metadata.
- Do not treat a staged forwarded item in the composer as delivered until it appears in chat history or runtime evidence confirms delivery.
