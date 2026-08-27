from __future__ import annotations

import os
import re
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit


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
    webhook_public_base_url: str | None = None
    webhook_path: str = "/telegram/webhook"
    webhook_secret_token: str | None = None
    webhook_listen_host: str = "0.0.0.0"
    webhook_listen_port: int = 8080
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
        ingress_mode = values.get("INGRESS_MODE", "polling")
        if ingress_mode not in {"polling", "webhook"}:
            raise RuntimeError(
                f"Unsupported INGRESS_MODE={ingress_mode!r}; use 'polling' or 'webhook'"
            )
        webhook_public_base_url = values.get("WEBHOOK_PUBLIC_BASE_URL", "").rstrip("/")
        webhook_path = values.get("WEBHOOK_PATH", "/telegram/webhook")
        webhook_secret_token = values.get("WEBHOOK_SECRET_TOKEN", "")
        webhook_listen_host = values.get("WEBHOOK_LISTEN_HOST", "0.0.0.0")
        webhook_listen_port = int(values.get("WEBHOOK_LISTEN_PORT", "8080"))
        if ingress_mode == "webhook":
            _validate_webhook_settings(
                public_base_url=webhook_public_base_url,
                path=webhook_path,
                secret_token=webhook_secret_token,
                listen_host=webhook_listen_host,
                listen_port=webhook_listen_port,
            )
        blockscout_max_attempts = int(values.get("BLOCKSCOUT_MAX_ATTEMPTS", "3"))
        blockscout_retry_initial_delay_seconds = float(
            values.get("BLOCKSCOUT_RETRY_INITIAL_DELAY_SECONDS", "0.5")
        )
        blockscout_retry_backoff_factor = float(
            values.get("BLOCKSCOUT_RETRY_BACKOFF_FACTOR", "2")
        )
        if blockscout_max_attempts < 1:
            raise RuntimeError("BLOCKSCOUT_MAX_ATTEMPTS must be >= 1")
        if (
            not isfinite(blockscout_retry_initial_delay_seconds)
            or blockscout_retry_initial_delay_seconds < 0
        ):
            raise RuntimeError(
                "BLOCKSCOUT_RETRY_INITIAL_DELAY_SECONDS must be finite and >= 0"
            )
        if (
            not isfinite(blockscout_retry_backoff_factor)
            or blockscout_retry_backoff_factor < 1
        ):
            raise RuntimeError(
                "BLOCKSCOUT_RETRY_BACKOFF_FACTOR must be finite and >= 1"
            )
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
            webhook_public_base_url=webhook_public_base_url or None,
            webhook_path=webhook_path,
            webhook_secret_token=webhook_secret_token or None,
            webhook_listen_host=webhook_listen_host,
            webhook_listen_port=webhook_listen_port,
            blockscout_max_attempts=blockscout_max_attempts,
            blockscout_retry_initial_delay_seconds=blockscout_retry_initial_delay_seconds,
            blockscout_retry_backoff_factor=blockscout_retry_backoff_factor,
            log_level=values.get("LOG_LEVEL", "INFO"),
        )

    @property
    def webhook_url(self) -> str:
        if self.webhook_public_base_url is None:
            raise RuntimeError("WEBHOOK_PUBLIC_BASE_URL is required for webhook ingress")
        return f"{self.webhook_public_base_url}{self.webhook_path}"


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


def _validate_webhook_settings(
    *,
    public_base_url: str,
    path: str,
    secret_token: str,
    listen_host: str,
    listen_port: int,
) -> None:
    parsed_url = urlsplit(public_base_url)
    if (
        parsed_url.scheme != "https"
        or not parsed_url.netloc
        or parsed_url.path not in {"", "/"}
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise RuntimeError(
            "WEBHOOK_PUBLIC_BASE_URL must be an HTTPS origin without a path, query, or fragment"
        )
    if not path.startswith("/") or urlsplit(path).query or urlsplit(path).fragment:
        raise RuntimeError("WEBHOOK_PATH must be an absolute URL path without query or fragment")
    if not secret_token or secret_token == "replace-with-random-secret":
        raise RuntimeError("WEBHOOK_SECRET_TOKEN is required for webhook ingress")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", secret_token):
        raise RuntimeError(
            "WEBHOOK_SECRET_TOKEN must contain 1-256 letters, digits, underscores, or hyphens"
        )
    if not listen_host:
        raise RuntimeError("WEBHOOK_LISTEN_HOST must not be empty")
    if not 1 <= listen_port <= 65535:
        raise RuntimeError("WEBHOOK_LISTEN_PORT must be between 1 and 65535")
