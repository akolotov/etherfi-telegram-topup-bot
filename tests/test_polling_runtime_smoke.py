from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from telegram.ext import TypeHandler

from etherfi_bot.runtime import ALLOWED_UPDATES, build_runtime, run_runtime
from etherfi_bot.settings import RuntimeSettings
from tests.conftest import make_user, write_config


async def test_ptb_runtime_builds_polling_updater_and_typed_update_handler(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    components = build_runtime(settings)
    try:
        assert components.application.updater is not None
        assert any(
            isinstance(handler, TypeHandler)
            for handlers in components.application.handlers.values()
            for handler in handlers
        )
    finally:
        await components.shutdown()


async def test_ptb_webhook_runtime_keeps_updater_required_by_run_webhook(tmp_path: Path) -> None:
    settings = replace(
        _settings(tmp_path),
        ingress_mode="webhook",
        webhook_public_base_url="https://gateway.example.test",
        webhook_path="/hooks/test/webhook",
        webhook_secret_token="secret-token",
    )
    components = build_runtime(settings)
    try:
        assert components.application.updater is not None
    finally:
        await components.shutdown()


def test_webhook_runtime_delegates_server_and_registration_to_ptb(tmp_path: Path) -> None:
    settings = replace(
        _settings(tmp_path),
        ingress_mode="webhook",
        webhook_public_base_url="https://gateway.example.test",
        webhook_path="/hooks/test/webhook",
        webhook_secret_token="secret-token",
        webhook_listen_host="0.0.0.0",
        webhook_listen_port=8080,
    )
    calls: list[dict] = []

    class FakeApplication:
        post_init = None
        post_stop = None
        post_shutdown = None

        def run_webhook(self, **kwargs) -> None:
            calls.append(kwargs)

    async def lifecycle_callback(_application=None) -> None:
        return None

    components = SimpleNamespace(
        application=FakeApplication(),
        startup=lifecycle_callback,
        stop=lifecycle_callback,
        shutdown=lifecycle_callback,
    )

    run_runtime(components, settings)

    assert calls == [
        {
            "listen": "0.0.0.0",
            "port": 8080,
            "url_path": "hooks/test/webhook",
            "webhook_url": "https://gateway.example.test/hooks/test/webhook",
            "allowed_updates": ALLOWED_UPDATES,
            "drop_pending_updates": False,
            "max_connections": 1,
            "secret_token": "secret-token",
        }
    ]


def _settings(tmp_path: Path) -> RuntimeSettings:
    key_path = tmp_path / "safe-proposer-key"
    key_path.write_text(f"{1:064x}", encoding="utf-8")
    user = replace(make_user(), safe_proposer_key_file=str(key_path))
    config_path = write_config(tmp_path / "config.json", [user])
    return RuntimeSettings(
        bot_token="123:ABC",
        blockscout_pro_api_key="proapi_test",
        safe_transaction_service_api_key="safe-api-key",
        config_path=config_path,
        state_dir=tmp_path / "states",
    )
