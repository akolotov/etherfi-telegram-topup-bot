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
