from __future__ import annotations

import logging
from typing import Any, Protocol

from etherfi_bot.dispatcher import BotDispatcher


class CallbackAnswerer(Protocol):
    def answer_callback_query(self, callback_query_id: str) -> bool:
        """Acknowledge a Telegram callback query."""


class TelegramUpdateAdapter:
    def __init__(
        self,
        dispatcher: BotDispatcher,
        *,
        callback_answerer: CallbackAnswerer | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._callback_answerer = callback_answerer
        self._logger = logger or logging.getLogger(__name__)

    def handle_update(self, update: dict[str, Any]) -> str:
        update_id = update.get("update_id")
        if "message" in update:
            return self._handle_message(update["message"], update_id=update_id)
        if "callback_query" in update:
            return self._handle_callback_query(update["callback_query"], update_id=update_id)
        if "my_chat_member" in update:
            return self._handle_my_chat_member(update["my_chat_member"], update_id=update_id)
        if "message_reaction" in update:
            return self._handle_message_reaction(update["message_reaction"], update_id=update_id)
        self._logger.debug(
            "telegram_update_ignored action=ignored_unsupported_update telegram_update_id=%s "
            "reason=unsupported_update update_keys=%s",
            update_id,
            ",".join(sorted(str(key) for key in update)),
        )
        return "ignored_unsupported_update"

    def _handle_message(self, message: dict[str, Any], *, update_id: Any) -> str:
        if not _is_private_chat(message.get("chat")):
            chat = message.get("chat") or {}
            self._logger.debug(
                "telegram_update_ignored action=ignored_non_private_message "
                "telegram_update_id=%s reason=non_private_chat chat_type=%s",
                update_id,
                chat.get("type") if isinstance(chat, dict) else None,
            )
            return "ignored_non_private_message"
        telegram_user_id = _user_id(message.get("from"))
        if telegram_user_id is None:
            self._logger.debug(
                "telegram_update_ignored action=ignored_message_without_user "
                "telegram_update_id=%s reason=missing_user",
                update_id,
            )
            return "ignored_message_without_user"
        if _is_start_command(message):
            self._dispatcher.start(telegram_user_id)
            return "start"
        self._dispatcher.ignore_event(telegram_user_id)
        self._logger.debug(
            "telegram_update_ignored action=ignored_message telegram_update_id=%s "
            "telegram_user_id=%s reason=non_start_message",
            update_id,
            telegram_user_id,
        )
        return "ignored_message"

    def _handle_callback_query(self, callback_query: dict[str, Any], *, update_id: Any) -> str:
        callback_query_id = callback_query.get("id")
        telegram_user_id = _user_id(callback_query.get("from"))
        message = callback_query.get("message") or {}
        message_id = message.get("message_id")
        data = callback_query.get("data")
        action = "ignored_callback"

        if telegram_user_id is not None and message_id is not None:
            if data == "top_up":
                self._dispatcher.callback_top_up(telegram_user_id, int(message_id))
                action = "callback_top_up"
            elif data == "ignore":
                self._dispatcher.callback_ignore(telegram_user_id, int(message_id))
                action = "callback_ignore"
            else:
                self._dispatcher.ignore_event(telegram_user_id)
                self._logger.debug(
                    "telegram_update_ignored action=ignored_callback telegram_update_id=%s "
                    "telegram_user_id=%s message_id=%s callback_data=%s reason=unsupported_callback_data",
                    update_id,
                    telegram_user_id,
                    message_id,
                    data,
                )
        else:
            self._logger.debug(
                "telegram_update_ignored action=ignored_callback telegram_update_id=%s "
                "telegram_user_id=%s message_id=%s callback_data=%s reason=missing_user_or_message",
                update_id,
                telegram_user_id,
                message_id,
                data,
            )

        if self._callback_answerer is not None and callback_query_id is not None:
            try:
                self._callback_answerer.answer_callback_query(str(callback_query_id))
            except Exception as error:
                self._logger.warning(
                    "callback_ack_failed callback_query_id=%s telegram_user_id=%s "
                    "message_id=%s callback_data=%s error_type=%s error=%s",
                    callback_query_id,
                    telegram_user_id,
                    message_id,
                    data,
                    type(error).__name__,
                    error,
                )
        return action

    def _handle_my_chat_member(self, member_update: dict[str, Any], *, update_id: Any) -> str:
        if not _is_private_chat(member_update.get("chat")):
            chat = member_update.get("chat") or {}
            self._logger.debug(
                "telegram_update_ignored action=ignored_non_private_chat_member "
                "telegram_update_id=%s reason=non_private_chat chat_type=%s",
                update_id,
                chat.get("type") if isinstance(chat, dict) else None,
            )
            return "ignored_non_private_chat_member"
        telegram_user_id = _user_id(member_update.get("from"))
        if telegram_user_id is None:
            self._logger.debug(
                "telegram_update_ignored action=ignored_chat_member_without_user "
                "telegram_update_id=%s reason=missing_user",
                update_id,
            )
            return "ignored_chat_member_without_user"
        new_status = (member_update.get("new_chat_member") or {}).get("status")
        if new_status == "kicked":
            self._dispatcher.user_blocked(telegram_user_id)
            return "user_blocked"
        self._dispatcher.ignore_event(telegram_user_id)
        self._logger.debug(
            "telegram_update_ignored action=ignored_chat_member telegram_update_id=%s "
            "telegram_user_id=%s new_status=%s reason=unsupported_chat_member_status",
            update_id,
            telegram_user_id,
            new_status,
        )
        return "ignored_chat_member"

    def _handle_message_reaction(self, reaction: dict[str, Any], *, update_id: Any) -> str:
        if not _is_private_chat(reaction.get("chat")):
            chat = reaction.get("chat") or {}
            self._logger.debug(
                "telegram_update_ignored action=ignored_non_private_reaction "
                "telegram_update_id=%s reason=non_private_chat chat_type=%s",
                update_id,
                chat.get("type") if isinstance(chat, dict) else None,
            )
            return "ignored_non_private_reaction"
        telegram_user_id = _user_id(reaction.get("user"))
        if telegram_user_id is None:
            self._logger.debug(
                "telegram_update_ignored action=ignored_reaction_without_user "
                "telegram_update_id=%s reason=missing_user",
                update_id,
            )
            return "ignored_reaction_without_user"
        self._dispatcher.ignore_event(telegram_user_id)
        self._logger.debug(
            "telegram_update_ignored action=ignored_reaction telegram_update_id=%s "
            "telegram_user_id=%s reason=reaction_ignored",
            update_id,
            telegram_user_id,
        )
        return "ignored_reaction"


def _is_start_command(message: dict[str, Any]) -> bool:
    text = str(message.get("text", ""))
    entities = message.get("entities") or []
    if not text.startswith("/start"):
        return False
    return any(
        entity.get("offset") == 0
        and entity.get("type") == "bot_command"
        and text[: int(entity.get("length", 0))].split("@", 1)[0] == "/start"
        for entity in entities
    )


def _is_private_chat(chat: Any) -> bool:
    return isinstance(chat, dict) and chat.get("type") == "private"


def _user_id(user: Any) -> int | None:
    if not isinstance(user, dict) or user.get("is_bot") is True:
        return None
    value = user.get("id")
    if value is None:
        return None
    return int(value)
