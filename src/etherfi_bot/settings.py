from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class RuntimeSettings:
    bot_token: str
    blockscout_pro_api_key: str
    safe_transaction_service_api_key: str
    ingress_mode: str = "polling"
    telegram_api_base_url: str = "https://api.telegram.org"
    safe_tx_service_base_url: str = "https://api.safe.global/tx-service/arb1"
    config_path: Path = Path("data/config.json")
    state_dir: Path = Path("data/user_states")
    polling_offset_path: Path = Path("data/polling_offset.json")
    polling_pending_update_path: Path = Path("data/polling_pending_update.json")
    poll_timeout_seconds: int = 25
    blockscout_max_attempts: int = 3
    blockscout_retry_initial_delay_seconds: float = 0.5
    blockscout_retry_backoff_factor: float = 2
    log_level: str = "INFO"

    @classmethod
    def from_env_file(
        cls,
        path: str | Path = ".env",
        *,
        environ: Mapping[str, str] | None = None,
    ) -> "RuntimeSettings":
        values = load_env_file(path)
        values.update(os.environ if environ is None else environ)
        bot_token = values.get("BOT_TOKEN", "")
        if not bot_token:
            raise RuntimeError("BOT_TOKEN is required")
        blockscout_pro_api_key = values.get("BLOCKSCOUT_PRO_API_KEY", "")
        if not _looks_like_blockscout_key(blockscout_pro_api_key):
            raise RuntimeError("BLOCKSCOUT_PRO_API_KEY is required")
        safe_transaction_service_api_key = values.get(
            "SAFE_TRANSACTION_SERVICE_API_KEY",
            "",
        )
        if not _looks_like_safe_transaction_service_key(
            safe_transaction_service_api_key
        ):
            raise RuntimeError("SAFE_TRANSACTION_SERVICE_API_KEY is required")
        blockscout_max_attempts = int(values.get("BLOCKSCOUT_MAX_ATTEMPTS", "3"))
        blockscout_retry_initial_delay_seconds = float(
            values.get("BLOCKSCOUT_RETRY_INITIAL_DELAY_SECONDS", "0.5")
        )
        blockscout_retry_backoff_factor = float(
            values.get("BLOCKSCOUT_RETRY_BACKOFF_FACTOR", "2")
        )
        if blockscout_max_attempts < 1:
            raise RuntimeError("BLOCKSCOUT_MAX_ATTEMPTS must be >= 1")
        if blockscout_retry_initial_delay_seconds < 0:
            raise RuntimeError("BLOCKSCOUT_RETRY_INITIAL_DELAY_SECONDS must be >= 0")
        if blockscout_retry_backoff_factor < 1:
            raise RuntimeError("BLOCKSCOUT_RETRY_BACKOFF_FACTOR must be >= 1")
        ingress_mode = values.get("INGRESS_MODE", "polling")
        if ingress_mode != "polling":
            raise RuntimeError(f"Unsupported INGRESS_MODE={ingress_mode!r}; use 'polling'")
        return cls(
            bot_token=bot_token,
            blockscout_pro_api_key=blockscout_pro_api_key,
            safe_transaction_service_api_key=safe_transaction_service_api_key,
            ingress_mode=ingress_mode,
            telegram_api_base_url=values.get(
                "TELEGRAM_API_BASE_URL",
                "https://api.telegram.org",
            ),
            safe_tx_service_base_url=values.get(
                "SAFE_TX_SERVICE_BASE_URL",
                "https://api.safe.global/tx-service/arb1",
            ),
            config_path=Path(values.get("CONFIG_PATH", "data/config.json")),
            state_dir=Path(values.get("STATE_DIR", "data/user_states")),
            polling_offset_path=Path(
                values.get("POLLING_OFFSET_PATH", "data/polling_offset.json")
            ),
            polling_pending_update_path=Path(
                values.get(
                    "POLLING_PENDING_UPDATE_PATH",
            blockscout_max_attempts=blockscout_max_attempts,
            blockscout_retry_initial_delay_seconds=blockscout_retry_initial_delay_seconds,
            blockscout_retry_backoff_factor=blockscout_retry_backoff_factor,
                    "data/polling_pending_update.json",
                )
            ),
            poll_timeout_seconds=int(values.get("POLL_TIMEOUT_SECONDS", "25")),
            log_level=values.get("LOG_LEVEL", "INFO"),
        )


def load_env_file(path: str | Path) -> dict[str, str]:
    env_path = Path(path)
    if not env_path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = _strip_env_value(value.strip())
        if key:
            values[key] = value
    return values


def _strip_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _looks_like_blockscout_key(value: str) -> bool:
    return value.startswith("proapi_") and value not in {"proapi_", "proapi_..."}


def _looks_like_safe_transaction_service_key(value: str) -> bool:
    return bool(value and value.strip() and value.strip() != "...")
