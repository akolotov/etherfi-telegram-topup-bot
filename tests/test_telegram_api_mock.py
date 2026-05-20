from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from tests.smoke.telegram_api_mock import create_server


def test_fake_telegram_api_supports_polling_and_webhook_smoke(tmp_path: Path) -> None:
    server = create_server(
        "127.0.0.1",
        _free_port(),
        state_path=tmp_path / "telegram-mock-state.json",
        bot_username="etherfi_test_bot",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        _post_json(base_url, "/__admin/reset", {})
        update = _load_fixture("message_command_start")
        _post_json(base_url, "/__admin/enqueue", update)

        get_me = _post_json(base_url, "/bot123:ABC/getMe", {})
        assert get_me["result"]["username"] == "etherfi_test_bot"

        first_poll = _post_json(base_url, "/bot123:ABC/getUpdates", {"offset": 0})
        assert first_poll["result"] == [update]

        next_poll = _post_json(
            base_url,
            "/bot123:ABC/getUpdates",
            {"offset": update["update_id"] + 1},
        )
        assert next_poll["result"] == []

        set_webhook = _post_json(
            base_url,
            "/bot123:ABC/setWebhook",
            {"url": "https://example.test/telegram/webhook"},
        )
        assert set_webhook["ok"] is True

        sent = _post_json(
            base_url,
            "/bot123:ABC/sendMessage",
            {"chat_id": 1001, "text": "hello"},
        )
        assert sent["result"]["message_id"] == 10000

        answer = _post_json(
            base_url,
            "/bot123:ABC/answerCallbackQuery",
            {"callback_query_id": "fixture-callback"},
        )
        assert answer["result"] is True

        state = _get_json(base_url, "/__admin/state")
        assert state["webhook"] == {"url": "https://example.test/telegram/webhook"}
        assert [request["method"] for request in state["requests"]] == [
            "getMe",
            "getUpdates",
            "getUpdates",
            "setWebhook",
            "sendMessage",
            "answerCallbackQuery",
        ]
        assert state["outbound"][0]["method"] == "sendMessage"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _load_fixture(name: str) -> dict[str, Any]:
    path = (
        Path(__file__).resolve().parent
        / "fixtures"
        / "telegram_updates"
        / f"{name}.anonymized.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _post_json(base_url: str, path: str, payload: Any) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        f"{base_url}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def _get_json(base_url: str, path: str) -> dict[str, Any]:
    with urlopen(f"{base_url}{path}", timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
