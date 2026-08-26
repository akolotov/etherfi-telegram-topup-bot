from __future__ import annotations

import pytest

from etherfi_bot.settings import RuntimeSettings


def test_runtime_settings_reads_blockscout_key_from_env_file(tmp_path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "BOT_TOKEN=123:ABC",
                "BLOCKSCOUT_PRO_API_KEY=proapi_file_key",
                "SAFE_TRANSACTION_SERVICE_API_KEY=safe_file_key",
            ]
        ),
        encoding="utf-8",
    )

    settings = RuntimeSettings.from_env_file(env_path, environ={})

    assert settings.bot_token == "123:ABC"
    assert settings.blockscout_pro_api_key == "proapi_file_key"
    assert settings.safe_transaction_service_api_key == "safe_file_key"


def test_runtime_settings_environment_overrides_env_file_blockscout_key(tmp_path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "BOT_TOKEN=123:ABC",
                "BLOCKSCOUT_PRO_API_KEY=proapi_file_key",
                "SAFE_TRANSACTION_SERVICE_API_KEY=safe_file_key",
            ]
        ),
        encoding="utf-8",
    )

    settings = RuntimeSettings.from_env_file(
        env_path,
        environ={
            "BLOCKSCOUT_PRO_API_KEY": "proapi_environment_key",
            "SAFE_TRANSACTION_SERVICE_API_KEY": "safe_environment_key",
        },
    )

    assert settings.blockscout_pro_api_key == "proapi_environment_key"
    assert settings.safe_transaction_service_api_key == "safe_environment_key"
def test_runtime_settings_reads_blockscout_retry_configuration(tmp_path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "BOT_TOKEN=123:ABC",
                "BLOCKSCOUT_PRO_API_KEY=proapi_file_key",
                "SAFE_TRANSACTION_SERVICE_API_KEY=safe_file_key",
                "BLOCKSCOUT_MAX_ATTEMPTS=4",
                "BLOCKSCOUT_RETRY_INITIAL_DELAY_SECONDS=0.25",
                "BLOCKSCOUT_RETRY_BACKOFF_FACTOR=1.5",
            ]
        ),
        encoding="utf-8",
    )

    settings = RuntimeSettings.from_env_file(env_path, environ={})

    assert settings.blockscout_max_attempts == 4
    assert settings.blockscout_retry_initial_delay_seconds == 0.25
    assert settings.blockscout_retry_backoff_factor == 1.5


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("BLOCKSCOUT_RETRY_INITIAL_DELAY_SECONDS", "nan"),
        ("BLOCKSCOUT_RETRY_INITIAL_DELAY_SECONDS", "inf"),
        ("BLOCKSCOUT_RETRY_BACKOFF_FACTOR", "nan"),
        ("BLOCKSCOUT_RETRY_BACKOFF_FACTOR", "inf"),
    ],
)
def test_runtime_settings_rejects_non_finite_blockscout_retry_configuration(
    tmp_path,
    key: str,
    value: str,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "BOT_TOKEN=123:ABC",
                "BLOCKSCOUT_PRO_API_KEY=proapi_file_key",
                "SAFE_TRANSACTION_SERVICE_API_KEY=safe_file_key",
                f"{key}={value}",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match=key):
        RuntimeSettings.from_env_file(env_path, environ={})




@pytest.mark.parametrize(
    "blockscout_key",
    [
        "",
        "invalid",
        "proapi_",
        "proapi_...",
    ],
)
def test_runtime_settings_rejects_missing_or_invalid_blockscout_key(
    tmp_path,
    blockscout_key: str,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "BOT_TOKEN=123:ABC",
                f"BLOCKSCOUT_PRO_API_KEY={blockscout_key}",
                "SAFE_TRANSACTION_SERVICE_API_KEY=safe_file_key",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="BLOCKSCOUT_PRO_API_KEY"):
        RuntimeSettings.from_env_file(env_path, environ={})


@pytest.mark.parametrize("safe_key", ["", "   ", "..."])
def test_runtime_settings_rejects_missing_or_placeholder_safe_api_key(
    tmp_path,
    safe_key: str,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "BOT_TOKEN=123:ABC",
                "BLOCKSCOUT_PRO_API_KEY=proapi_file_key",
                f"SAFE_TRANSACTION_SERVICE_API_KEY={safe_key}",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="SAFE_TRANSACTION_SERVICE_API_KEY"):
        RuntimeSettings.from_env_file(env_path, environ={})


def test_runtime_settings_reads_safe_tx_service_base_url(tmp_path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "BOT_TOKEN=123:ABC",
                "BLOCKSCOUT_PRO_API_KEY=proapi_file_key",
                "SAFE_TRANSACTION_SERVICE_API_KEY=safe_file_key",
                "SAFE_TX_SERVICE_BASE_URL=https://safe.test",
            ]
        ),
        encoding="utf-8",
    )

    settings = RuntimeSettings.from_env_file(env_path, environ={})

    assert settings.safe_tx_service_base_url == "https://safe.test"
