from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from etherfi_bot.mocks import (
    MockBalanceProvider,
    MockClock,
    MockPrivateKeyProvider,
    MockSafeWalletClient,
    MockTelegramGateway,
)
from etherfi_bot.dispatcher import BotDispatcher
from etherfi_bot.storage import JsonConfigRepository, JsonStateRepository
from etherfi_bot.telegram_adapter import TelegramUpdateAdapter
from etherfi_bot.webhook import TELEGRAM_SECRET_HEADER, WebhookBotRunner
from tests.conftest import make_user, write_config

WEBHOOK_PATH = "/hooks/etherfi-topup-bot/telegram/webhook"
WEBHOOK_SECRET = "valid_telegram-secret_123"


def test_webhook_registers_tailscale_url_and_processes_a_valid_update(tmp_path: Path) -> None:
    user = make_user(telegram_user_id=1001)
    dispatcher, states = _make_dispatcher(tmp_path, user)
    api = RecordingWebhookApi()
    runner = WebhookBotRunner(
        api=api,
        adapter=TelegramUpdateAdapter(dispatcher),
        dispatcher=dispatcher,
        webhook_url=(
            "https://wabelfish-funnel.taild8e94b.ts.net"
            "/hooks/etherfi-topup-bot/telegram/webhook"
        ),
        webhook_path=WEBHOOK_PATH,
        secret_token=WEBHOOK_SECRET,
        listen_host="127.0.0.1",
        listen_port=_free_port(),
    )
    server = runner.create_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        runner.setup()
        update = _load_fixture("message_command_start")
        status = _post_update(
            f"http://127.0.0.1:{server.server_port}{WEBHOOK_PATH}",
            update,
            secret=WEBHOOK_SECRET,
        )

        assert status == 200
        assert api.webhook_payload == {
            "url": (
                "https://wabelfish-funnel.taild8e94b.ts.net"
                "/hooks/etherfi-topup-bot/telegram/webhook"
            ),
            "secret_token": WEBHOOK_SECRET,
            "allowed_updates": [
                "message",
                "callback_query",
                "my_chat_member",
                "message_reaction",
            ],
            "max_connections": 1,
            "drop_pending_updates": False,
        }
        assert states.load(user.telegram_user_id).telegram_user_id == user.telegram_user_id
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_webhook_rejects_bad_secret_and_invalid_json(tmp_path: Path) -> None:
    user = make_user(telegram_user_id=1001)
    dispatcher, _states = _make_dispatcher(tmp_path, user)
    runner = WebhookBotRunner(
        api=RecordingWebhookApi(),
        adapter=TelegramUpdateAdapter(dispatcher),
        dispatcher=dispatcher,
        webhook_url="https://example.test/hooks/etherfi-topup-bot/telegram/webhook",
        webhook_path=WEBHOOK_PATH,
        secret_token=WEBHOOK_SECRET,
        listen_host="127.0.0.1",
        listen_port=_free_port(),
    )
    server = runner.create_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}{WEBHOOK_PATH}"
        assert _post_update(url, {"update_id": 1}, secret="wrong-secret") == 403
        assert _post_raw(url, b"not-json", secret=WEBHOOK_SECRET) == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _make_dispatcher(tmp_path: Path, user):
    config_path = write_config(tmp_path / "config.json", [user])
    states = JsonStateRepository(tmp_path / "states")
    dispatcher = BotDispatcher(
        config_repository=JsonConfigRepository(config_path),
        state_repository=states,
        telegram=MockTelegramGateway(),
        balances=MockBalanceProvider(),
        safe_wallet=MockSafeWalletClient(),
        private_keys=MockPrivateKeyProvider({user.safe_proposer_key_file: "private-key"}),
        clock=MockClock(),
    )
    return dispatcher, states


def _load_fixture(name: str) -> dict[str, Any]:
    path = (
        Path(__file__).resolve().parent
        / "fixtures"
        / "telegram_updates"
        / f"{name}.anonymized.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _post_update(url: str, payload: dict[str, Any], *, secret: str) -> int:
    return _post_raw(url, json.dumps(payload).encode("utf-8"), secret=secret)


def _post_raw(url: str, body: bytes, *, secret: str) -> int:
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", TELEGRAM_SECRET_HEADER: secret},
        method="POST",
    )
    try:
        with urlopen(request, timeout=3) as response:
            return response.status
    except HTTPError as error:
        return error.code


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class RecordingWebhookApi:
    def __init__(self) -> None:
        self.webhook_payload: dict[str, Any] | None = None

    def get_me(self) -> dict[str, Any]:
        return {"username": "etherfi_test_bot"}

    def set_webhook(self, **payload: Any) -> bool:
        self.webhook_payload = payload
        return True
