from __future__ import annotations

import hmac
import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from etherfi_bot.dispatcher import BotDispatcher
from etherfi_bot.polling import ALLOWED_UPDATES
from etherfi_bot.telegram_adapter import TelegramUpdateAdapter
from etherfi_bot.telegram_api import TelegramBotApiClient

TELEGRAM_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
MAX_WEBHOOK_BODY_BYTES = 1_048_576


class WebhookBotRunner:
    """Receive Telegram updates over HTTP and keep the FSM timer running."""

    def __init__(
        self,
        *,
        api: TelegramBotApiClient,
        adapter: TelegramUpdateAdapter,
        dispatcher: BotDispatcher,
        webhook_url: str,
        webhook_path: str,
        secret_token: str,
        listen_host: str = "0.0.0.0",
        listen_port: int = 8080,
        logger: logging.Logger | None = None,
    ) -> None:
        self._api = api
        self._adapter = adapter
        self._dispatcher = dispatcher
        self._webhook_url = webhook_url
        self._webhook_path = webhook_path
        self._secret_token = secret_token
        self._listen_host = listen_host
        self._listen_port = listen_port
        self._logger = logger or logging.getLogger(__name__)
        self._update_lock = threading.Lock()
        self._wake_scheduler = threading.Event()
        self._stop_scheduler = threading.Event()
        self._started = False

    def setup(self) -> None:
        me = self._api.get_me()
        recovered_user_ids = self._dispatcher.recover_missing_user_states()
        self._api.set_webhook(
            url=self._webhook_url,
            secret_token=self._secret_token,
            allowed_updates=ALLOWED_UPDATES,
            max_connections=1,
            drop_pending_updates=False,
        )
        self._logger.info(
            "webhook_setup ingress_mode=webhook bot_username=%s webhook_url=%s "
            "webhook_path=%s allowed_updates=%s recovered_user_count=%s",
            me.get("username"),
            self._webhook_url,
            self._webhook_path,
            ",".join(ALLOWED_UPDATES),
            len(recovered_user_ids),
        )
        self._started = True
        self._wake_scheduler.set()

    def create_server(self) -> ThreadingHTTPServer:
        runner = self

        class Handler(_WebhookRequestHandler):
            _runner = runner

        server = _WebhookHttpServer((self._listen_host, self._listen_port), Handler)
        self._logger.info(
            "webhook_listener_ready ingress_mode=webhook listen_host=%s listen_port=%s",
            self._listen_host,
            server.server_port,
        )
        return server

    def run_forever(self) -> None:
        server = self.create_server()
        scheduler: threading.Thread | None = None
        try:
            if not self._started:
                self.setup()
            self._stop_scheduler.clear()
            scheduler = threading.Thread(
                target=self._run_scheduler,
                name="telegram-webhook-balance-scheduler",
                daemon=True,
            )
            scheduler.start()
            server.serve_forever(poll_interval=0.5)
        finally:
            self._stop_scheduler.set()
            self._wake_scheduler.set()
            if scheduler is not None:
                scheduler.join(timeout=5)
            server.server_close()

    def handle_update(self, update: dict[str, Any]) -> str:
        update_id = int(update["update_id"])
        with self._update_lock:
            action = self._adapter.handle_update(update)
        self._logger.info(
            "webhook_request status=accepted ingress_mode=webhook telegram_update_id=%s action=%s",
            update_id,
            action,
        )
        self._wake_scheduler.set()
        return action

    def process_due_ticks(self) -> int:
        processed_count = 0
        with self._update_lock:
            for telegram_user_id in self._dispatcher.due_user_ids():
                try:
                    state = self._dispatcher.balance_tick(telegram_user_id)
                except Exception as error:
                    self._logger.exception(
                        "balance_tick failed telegram_user_id=%s error_type=%s error=%s",
                        telegram_user_id,
                        type(error).__name__,
                        error,
                    )
                else:
                    self._logger.debug(
                        "balance_tick processed telegram_user_id=%s state=%s",
                        telegram_user_id,
                        None if state is None else state.state.value,
                    )
                processed_count += 1
        return processed_count

    def _run_scheduler(self) -> None:
        while not self._stop_scheduler.is_set():
            self.process_due_ticks()
            seconds = self._dispatcher.seconds_until_next_due_tick()
            timeout = 60.0 if seconds is None else max(1.0, float(seconds))
            self._wake_scheduler.wait(timeout=timeout)
            self._wake_scheduler.clear()


class _WebhookHttpServer(ThreadingHTTPServer):
    daemon_threads = True


class _WebhookRequestHandler(BaseHTTPRequestHandler):
    _runner: WebhookBotRunner

    def do_GET(self) -> None:
        if urlsplit(self.path).path == "/healthz":
            self._respond(HTTPStatus.OK)
            return
        self._respond(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if urlsplit(self.path).path != self._runner._webhook_path:
            self._respond(HTTPStatus.NOT_FOUND)
            return
        if not hmac.compare_digest(
            self.headers.get(TELEGRAM_SECRET_HEADER, ""),
            self._runner._secret_token,
        ):
            self._runner._logger.warning(
                "webhook_request status=forbidden ingress_mode=webhook reason=secret_mismatch"
            )
            self._respond(HTTPStatus.FORBIDDEN)
            return
        try:
            update = self._read_update()
            self._runner.handle_update(update)
        except _BadWebhookRequest as error:
            self._runner._logger.warning(
                "webhook_request status=bad_request ingress_mode=webhook error=%s",
                error,
            )
            self._respond(HTTPStatus.BAD_REQUEST)
        except Exception as error:
            self._runner._logger.exception(
                "webhook_request status=failed ingress_mode=webhook error_type=%s error=%s",
                type(error).__name__,
                error,
            )
            self._respond(HTTPStatus.INTERNAL_SERVER_ERROR)
        else:
            self._respond(HTTPStatus.OK)

    def _read_update(self) -> dict[str, Any]:
        raw_content_length = self.headers.get("Content-Length")
        if raw_content_length is None:
            raise _BadWebhookRequest("missing_content_length")
        try:
            content_length = int(raw_content_length)
        except ValueError as error:
            raise _BadWebhookRequest("invalid_content_length") from error
        if content_length < 1 or content_length > MAX_WEBHOOK_BODY_BYTES:
            raise _BadWebhookRequest("invalid_body_size")
        try:
            update = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise _BadWebhookRequest("invalid_json") from error
        if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
            raise _BadWebhookRequest("invalid_telegram_update")
        return update

    def _respond(self, status: HTTPStatus) -> None:
        self.send_response(int(status))
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _BadWebhookRequest(ValueError):
    pass
