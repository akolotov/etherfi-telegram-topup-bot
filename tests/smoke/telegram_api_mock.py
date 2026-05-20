from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from typing import Any
from urllib.parse import urlparse

HOST = os.getenv("TELEGRAM_MOCK_HOST", "127.0.0.1")
PORT = int(os.getenv("TELEGRAM_MOCK_PORT", "18081"))
STATE_PATH = Path(os.getenv("TELEGRAM_MOCK_STATE", "/tmp/etherfi-telegram-mock.json"))
BOT_ID = int(os.getenv("TELEGRAM_MOCK_BOT_ID", "7999990001"))
BOT_USERNAME = os.getenv("TELEGRAM_MOCK_BOT_USERNAME", "etherfi_test_bot")


class MockStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> dict[str, Any]:
        with self._lock:
            if not self.path.exists():
                return _default_state()
            return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, state: dict[str, Any]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")

    def mutate(self, callback) -> dict[str, Any]:
        with self._lock:
            if self.path.exists():
                state = json.loads(self.path.read_text(encoding="utf-8"))
            else:
                state = _default_state()
            callback(state)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
            return state


class ThreadingTelegramApiMockServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


class TelegramApiMockHandler(BaseHTTPRequestHandler):
    state_store: MockStateStore
    bot_id: int = BOT_ID
    bot_username: str = BOT_USERNAME

    def do_GET(self) -> None:
        if self.path == "/healthz":
            state = self.state_store.load()
            self._json(
                {
                    "status": "ok",
                    "queued_updates": len(state["updates"]),
                    "webhook": state.get("webhook"),
                }
            )
            return
        if self.path == "/__admin/state":
            self._json(self.state_store.load())
            return
        self._json({"ok": False, "description": "not found"}, status=404)

    def do_POST(self) -> None:
        payload = self._read_json()
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/__admin/reset":
            self.state_store.save(_default_state())
            self._json({"ok": True, "result": True})
            return

        if path == "/__admin/enqueue":
            updates = payload if isinstance(payload, list) else [payload]

            def _append(state: dict[str, Any]) -> None:
                state["updates"].extend(updates)

            state = self.state_store.mutate(_append)
            self._json({"ok": True, "result": {"queued_updates": len(state["updates"])}})
            return

        method = self._telegram_method(path)
        if method is None:
            self._json({"ok": False, "description": "not found"}, status=404)
            return

        self._record_request(method, payload)
        handler = getattr(self, f"_handle_{method}", None)
        if handler is None:
            self._json({"ok": True, "result": True})
            return
        handler(payload)

    def _telegram_method(self, path: str) -> str | None:
        parts = path.strip("/").split("/")
        if len(parts) != 2:
            return None
        bot_token, method = parts
        if not bot_token.startswith("bot"):
            return None
        return method

    def _handle_getMe(self, _payload: dict[str, Any]) -> None:
        self._json(
            {
                "ok": True,
                "result": {
                    "id": self.bot_id,
                    "is_bot": True,
                    "first_name": "ether.fi Test",
                    "username": self.bot_username,
                },
            }
        )

    def _handle_getUpdates(self, payload: dict[str, Any]) -> None:
        offset = int(payload.get("offset", 0) or 0)
        limit = int(payload.get("limit", 100) or 100)
        timeout = float(payload.get("timeout", 0) or 0)
        if timeout > 0:
            time.sleep(min(timeout, 0.05))
        state = self.state_store.load()
        updates = [
            update
            for update in state["updates"]
            if int(update.get("update_id", 0)) >= offset
        ][:limit]
        self._json({"ok": True, "result": updates})

    def _handle_setWebhook(self, payload: dict[str, Any]) -> None:
        def _set(state: dict[str, Any]) -> None:
            state["webhook"] = payload

        self.state_store.mutate(_set)
        self._json({"ok": True, "result": True, "description": "Webhook was set"})

    def _handle_deleteWebhook(self, _payload: dict[str, Any]) -> None:
        def _delete(state: dict[str, Any]) -> None:
            state["webhook"] = None

        self.state_store.mutate(_delete)
        self._json({"ok": True, "result": True})

    def _handle_sendMessage(self, payload: dict[str, Any]) -> None:
        message = self._record_outbound("sendMessage", payload)
        self._json({"ok": True, "result": message})

    def _handle_editMessageReplyMarkup(self, payload: dict[str, Any]) -> None:
        self._record_outbound("editMessageReplyMarkup", payload)
        self._json({"ok": True, "result": True})

    def _handle_answerCallbackQuery(self, payload: dict[str, Any]) -> None:
        self._record_outbound("answerCallbackQuery", payload)
        self._json({"ok": True, "result": True})

    def _record_request(self, method: str, payload: dict[str, Any]) -> None:
        def _append(state: dict[str, Any]) -> None:
            state["requests"].append({"method": method, "payload": payload})

        self.state_store.mutate(_append)

    def _record_outbound(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}

        def _append(state: dict[str, Any]) -> None:
            message_id = int(state["next_message_id"])
            state["next_message_id"] = message_id + 1
            result.update(
                {
                    "message_id": message_id,
                    "date": int(time.time()),
                    "chat": {"id": payload.get("chat_id"), "type": "private"},
                    "text": payload.get("text", ""),
                }
            )
            state["outbound"].append(
                {"method": method, "payload": payload, "result": result}
            )

        self.state_store.mutate(_append)
        return result

    def _read_json(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length == 0:
            return {}
        raw = self.rfile.read(content_length)
        return json.loads(raw.decode("utf-8") or "{}")

    def _json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _default_state() -> dict[str, Any]:
    return {
        "updates": [],
        "requests": [],
        "outbound": [],
        "webhook": None,
        "next_message_id": 10000,
    }


def create_server(
    host: str = HOST,
    port: int = PORT,
    *,
    state_path: Path = STATE_PATH,
    bot_id: int = BOT_ID,
    bot_username: str = BOT_USERNAME,
) -> ThreadingHTTPServer:
    TelegramApiMockHandler.state_store = MockStateStore(state_path)
    TelegramApiMockHandler.bot_id = bot_id
    TelegramApiMockHandler.bot_username = bot_username
    return ThreadingTelegramApiMockServer((host, port), TelegramApiMockHandler)


def main() -> None:
    server = create_server()
    server.serve_forever()


if __name__ == "__main__":
    main()
