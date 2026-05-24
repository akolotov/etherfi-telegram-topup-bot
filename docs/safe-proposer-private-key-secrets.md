# Safe proposer private key storage

Date: 2026-05-24

This document records the runtime secret handling decision for the Safe proposer
private key. It complements `docs/safe-transaction-service-api-research.md`,
which describes how the bot uses Safe Transaction Service as a Safe Wallet
Proposer.

## Decision

The bot will use a dedicated Safe proposer private key, not a Safe owner private
key.

The private key is provided to the bot only through a file path stored in
`data/config.json`:

```json
{
  "safe_proposer_key_file": "/path/to/safe_proposer_private_key"
}
```

The bot must not support passing the private key value directly through an
environment variable, including in tests. The config may contain only the path to
the private key file.

## Rationale

Using a proposer key reduces the blast radius of a bot compromise:

- The proposer can create queued Safe Transaction Service proposals.
- The proposer cannot produce Safe threshold approvals.
- The proposer cannot execute a Safe transaction by itself.
- Safe owners still need to review, sign, and execute the proposed transaction.

The proposer key is still sensitive. If compromised, an attacker could create
misleading or malicious proposals that appear as trusted queued proposals in Safe
Wallet UI. The bot must therefore keep proposed transaction content
deterministic, narrow, and auditable.

## Runtime contract

The application reads `safe_proposer_key_file` from the user config, then reads
the private key from that file.

The application should fail fast when:

- `safe_proposer_key_file` is not set.
- The file does not exist.
- The file cannot be read.
- The file content is not a valid EOA private key.
- The derived proposer address does not match the expected configured proposer
  address, if such a guard is configured.
- The derived proposer address is one of the Safe owners, if the bot can verify
  the owner list.

The application may trim surrounding whitespace from the file content. It must
never log the private key value.

## Docker runtime

In Docker, the private key file is mounted through Docker Compose secrets and is
read from `/run/secrets`.

Example:

```yaml
services:
  bot:
    secrets:
      - safe_proposer_private_key

secrets:
  safe_proposer_private_key:
    file: ./.secrets/safe_proposer_private_key
```

The matching user entry in `data/config.json` points to the mounted secret file:

```json
{
  "safe_proposer_key_file": "/run/secrets/safe_proposer_private_key"
}
```

Inside the container, the key is visible as a plaintext file to the bot process.
Docker secrets do not protect the key from code that is already allowed to run
inside the bot container. Their value here is operational: the key does not need
to be baked into an image, placed in `.env`, or exposed as a raw environment
variable.

## Local runtime

Without Docker, the same application contract is used. The environment variable
is not used; the user config points directly to a local file:

```json
{
  "safe_proposer_key_file": "./.secrets/safe_proposer_private_key"
}
```

The local `.secrets` directory must be excluded from version control and from
Docker build context:

```text
.secrets/
```

Recommended local file permissions:

```bash
chmod 700 .secrets
chmod 600 .secrets/safe_proposer_private_key
```

## Guardrails

The bot should only propose the specific top-up transaction shape required by
the product:

- Known Safe address.
- Known chain, Arbitrum One.
- Known target account.
- Known asset and token address, when ERC-20 is used.
- Bounded top-up amount.
- `operation=Call`.
- No arbitrary calldata accepted from Telegram input.

This keeps the proposer key useful only for a narrow class of proposals. A
stronger production model can later replace the raw private key file with an
external signer, KMS, Vault, or HSM-backed signer without changing the rest of
the Safe Transaction Service flow.
