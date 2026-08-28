virtualenv is used in the project

## Docker Compose test runtime

Use the project's virtual environment for local Python commands.

Before starting the bot through Docker Compose, inspect the current local
runtime wiring without printing secret values:

- `.env` values needed for routing and paths;
- `data/config.json`, especially each `safe_proposer_key_file`;
- available files under `.secrets/`;
- config and state mounts.

Use `docker-compose.agent.local.yml` together with `docker-compose.yml` for
local test runs. This ignored, persistent file carries only host-specific
wiring for the current working copy. If it already exists, reuse it and change
only the minimum needed to keep it aligned with the current local config. Do
not delete it during normal test cleanup. Do not put key, token, or secret
values into it.

When testing a locally built bot image, set `ETHERFI_TOPUP_BOT_IMAGE` to the
exact local image tag for both Compose commands below. This must override the
default registry image; for example, `export ETHERFI_TOPUP_BOT_IMAGE=etherfi-topup-bot:local`.

Validate and start the configured bot with:

```bash
docker compose -f docker-compose.yml -f docker-compose.agent.local.yml config -q
docker compose -f docker-compose.yml -f docker-compose.agent.local.yml up -d
```

Never replace the configured bot with an empty or synthetic configuration and
do not bypass this stack with `docker run` when testing the configured bot.

If the harness or guardrails block the Compose start, do not look for a
workaround. Tell the user the exact Compose command to run, identify its
read-only mounts and external effects, and wait for confirmation before
continuing logs, webhook delivery, or runtime verification.
