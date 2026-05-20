from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from etherfi_bot.domain import TelegramForbiddenError, UserConfig


class TelegramApiError(RuntimeError):
    """Telegram Bot API returned an unsuccessful response."""


class TelegramBotApiClient:
    def __init__(
        self,
        bot_token: str,
        *,
        base_url: str = "https://api.telegram.org",
        timeout_seconds: float = 10,
        logger: logging.Logger | None = None,
    ) -> None:
        if not bot_token:
            raise ValueError("bot_token must not be empty")
        self._bot_token = bot_token
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._logger = logger or logging.getLogger(__name__)

    def get_me(self) -> dict[str, Any]:
        return dict(self._request("getMe", {}))

    def get_updates(
        self,
        *,
        offset: int,
        timeout_seconds: int,
        allowed_updates: list[str],
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        result = self._request(
            "getUpdates",
            {
                "offset": int(offset),
                "timeout": int(timeout_seconds),
                "limit": int(limit),
                "allowed_updates": allowed_updates,
            },
        )
        return list(result)

    def delete_webhook(self, *, drop_pending_updates: bool = False) -> bool:
        return bool(
            self._request(
                "deleteWebhook",
                {"drop_pending_updates": bool(drop_pending_updates)},
            )
        )

    def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": int(chat_id), "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return dict(self._request("sendMessage", payload))

    def edit_message_reply_markup(
        self,
        *,
        chat_id: int,
        message_id: int,
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        payload: dict[str, Any] = {
            "chat_id": int(chat_id),
            "message_id": int(message_id),
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return bool(
            self._request(
                "editMessageReplyMarkup",
                payload,
            )
        )

    def answer_callback_query(self, callback_query_id: str) -> bool:
        return bool(
            self._request(
                "answerCallbackQuery",
                {"callback_query_id": str(callback_query_id)},
            )
        )

    def _request(self, method: str, payload: dict[str, Any]) -> Any:
        self._logger.debug(
            "telegram_api_request method=%s payload=%s",
            method,
            _sanitize_payload(payload),
        )
        request = Request(
            f"{self._base_url}/bot{self._bot_token}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except HTTPError as error:
            if error.code == 403:
                self._logger.warning(
                    "telegram_api_request_failed method=%s status_code=%s error_type=%s",
                    method,
                    error.code,
                    type(error).__name__,
                )
                raise TelegramForbiddenError("Telegram Bot API returned 403") from error
            self._logger.warning(
                "telegram_api_request_failed method=%s status_code=%s error_type=%s",
                method,
                error.code,
                type(error).__name__,
            )
            raise TelegramApiError(f"Telegram Bot API HTTP {error.code} for {method}") from error
        except URLError as error:
            self._logger.warning(
                "telegram_api_request_failed method=%s error_type=%s error=%s",
                method,
                type(error).__name__,
                error,
            )
            raise TelegramApiError(f"Telegram Bot API request failed for {method}: {error}") from error

        data = json.loads(body or "{}")
        if data.get("ok") is not True:
            if data.get("error_code") == 403:
                self._logger.warning(
                    "telegram_api_request_failed method=%s status_code=%s description=%s",
                    method,
                    data.get("error_code"),
                    data.get("description"),
                )
                raise TelegramForbiddenError(
                    str(data.get("description", "Telegram Bot API returned 403"))
                )
            self._logger.warning(
                "telegram_api_request_failed method=%s status_code=%s description=%s",
                method,
                data.get("error_code"),
                data.get("description"),
            )
            raise TelegramApiError(
                f"Telegram Bot API {method} failed: {data.get('description', data)}"
            )
        return data.get("result")


def _sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: "<redacted>" if key == "text" else _sanitize_payload_value(value)
        for key, value in payload.items()
    }


def _sanitize_payload_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _sanitize_payload(value)
    if isinstance(value, list):
        return [_sanitize_payload_value(item) for item in value]
    return value


class TelegramBotGateway:
    def __init__(self, api: TelegramBotApiClient) -> None:
        self._api = api

    def send_low_balance_prompt(self, user: UserConfig, balance: Decimal) -> int:
        message = self._api.send_message(
            chat_id=user.telegram_user_id,
            text=(
                f"Target account balance is low: {balance}. "
                "Create a Safe Wallet top-up transaction?"
            ),
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "Top Up", "callback_data": "top_up"},
                        {"text": "Ignore", "callback_data": "ignore"},
                    ]
                ]
            },
        )
        return int(message["message_id"])

    def send_safe_tx_created(self, user: UserConfig, safe_tx_id: str) -> int:
        message = self._api.send_message(
            chat_id=user.telegram_user_id,
            text=(
                f"Safe transaction {safe_tx_id} was created. "
                "Please add your signature and execute it."
            ),
        )
        return int(message["message_id"])

    def send_safe_tx_pending_prompt(self, user: UserConfig, safe_tx_id: str) -> int:
        message = self._api.send_message(
            chat_id=user.telegram_user_id,
            text=(
                f"Safe transaction {safe_tx_id} is still pending. "
                "Please sign and execute it."
            ),
        )
        return int(message["message_id"])

    def send_existing_safe_tx_notice(self, user: UserConfig, safe_tx_id: str) -> int:
        message = self._api.send_message(
            chat_id=user.telegram_user_id,
            text=f"Use existing Safe transaction {safe_tx_id}.",
        )
        return int(message["message_id"])

    def remove_buttons(self, telegram_user_id: int, message_id: int) -> None:
        self._api.edit_message_reply_markup(
            chat_id=telegram_user_id,
            message_id=message_id,
            reply_markup=None,
        )

    def send_admin_error(self, admin_telegram_user_id: int, message: str) -> None:
        self._api.send_message(chat_id=admin_telegram_user_id, text=message)
