---
name: telegram-ingress-subagent-orchestration
description: Delegate Telegram Web work to a telegram_operator subagent while the parent agent verifies ether.fi bot runtime behavior through polling or webhook ingress.
---

# Telegram Ingress Subagent Orchestration

Use this skill when a broader task needs Telegram Web interaction, but the parent agent should keep browser noise out of its thread.

This skill is for the parent/orchestrator. The child operator follows `.codex/skills/telegram-bot-ingress-testing/SKILL.md`.

## Important Policy

Only use a subagent when the active session allows it and the user has explicitly requested subagents, delegation, or parallel agent work. Otherwise, perform the work locally or report that live Telegram operation is a later manual step.

## Parent Responsibilities

The parent owns:

- product intent;
- resolving bot alias from runtime context;
- resolving `INGRESS_MODE=polling|webhook`;
- host-side verification;
- interpretation of state/log/outbound evidence;
- deciding whether to retry, continue, or stop.

The child owns:

- Telegram Web interaction;
- Telegram-side visual verification;
- browser artifacts under `.ai/playwright-cli/`;
- compact structured result.

## Stepwise Missions

Send one bounded mission at a time. A good mission includes:

- one Telegram action cluster;
- one visual checkpoint;
- one stop point;
- no host-side verification unless explicitly requested.

Examples:

- send `/start` and confirm a visible bot response or stable chat state;
- click the current `Top Up` button and confirm the button action was submitted;
- block the bot and confirm the chat shows the restart/reset state;
- send ignored plain text and confirm it appears in chat.

## Mission Brief Template

```md
# Mission

## Step Goal
<single Telegram step>

## Bot Identity
Alias: @example_bot
Source: parent runtime context

## Ingress Context
Mode: polling | webhook
Runtime verification owner: parent

## Telegram Objective
<what the child should do in Telegram Web>

## Constraints
- Do not infer a different bot alias.
- Do not delete the chat unless explicitly instructed.
- Stop after this checkpoint.

## Visual Checkpoint
<what must be visually confirmed>

## Return Format
Return strictly as `telegram_test_result_v1`.
```

## Result Schema

```yaml
schema: telegram_test_result_v1
status: success | failure | inconclusive

mission:
  requested_bot_alias: "@..."
  ingress_mode: polling | webhook
  step_goal: "..."

telegram:
  outcome: success | partial | failure
  summary: "What happened in Telegram Web"
  reset_action: none | start | restart_bot | block_bot | other
  bot_visible_response:
    seen: true | false | unknown
    summary: "Visible bot response or absence"
  checkpoint_reached: true | false
  checkpoint_summary: "Exact Telegram-side checkpoint"

artifacts:
  - path: ".ai/playwright-cli/..."
    kind: snapshot | screenshot | video | text
    purpose: "..."

conclusion:
  passed_assertions:
    - "..."
  failed_assertions:
    - "..."
  unknowns:
    - "..."
  continue_ready: true | false
  suggested_next_step: "..."
  important_entities:
    - key: "..."
      value: "..."

failure_point: null | "..."
```

## Parent Follow-up

After the child returns:

1. Check runtime evidence according to the ingress profile.
2. Compare Telegram-side result to state/log/outbound evidence.
3. Preserve disagreements instead of flattening them into a false pass.
4. Reuse the same child thread for the next Telegram step when possible.

For polling, prefer evidence from poller logs, fake Telegram API request capture, and `data/user_states`.

For webhook, prefer evidence from HTTP status, webhook logs, shared ingress logs, and `data/user_states`.
