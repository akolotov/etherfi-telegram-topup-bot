# Development

Use `.venv` for all local Python commands.

## Docker Compose test runtime

Before starting Compose, verify `.env` routing and paths,
`data/config.json` proposer-key paths, `.secrets/` filenames, and config/state
mounts without printing secrets.

Use one shared test stack across agent sessions:

- build only as `etherfi-topup-bot:local`; never add agent, task, or session tags;
- keep `COMPOSE_PROJECT_NAME=etherfi-topup-bot-test`,
  `ETHERFI_TOPUP_BOT_IMAGE=etherfi-topup-bot:local`, and
  `ETHERFI_TOPUP_BOT_PULL_POLICY=never` in `.env`;
- reuse `docker-compose.agent.local.yml`; keep it non-secret and do not delete it
  during cleanup.

Production and test must use different Compose project names, Telegram bot
tokens, Docker aliases, webhook paths, and webhook secrets.

Build, validate, and start with:

```bash
docker build -t etherfi-topup-bot:local .
docker compose -f docker-compose.yml -f docker-compose.agent.local.yml config -q
docker compose -f docker-compose.yml -f docker-compose.agent.local.yml up -d
```

Never use `docker run` or synthetic configuration. If Compose start is blocked,
report the command, read-only mounts, and external effects; wait for the user to
run it before checking logs or webhook delivery.
