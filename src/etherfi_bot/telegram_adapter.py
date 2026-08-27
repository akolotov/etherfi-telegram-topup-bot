from __future__ import annotations

import logging

from telegram import MessageEntity, Update
from telegram.constants import ChatMemberStatus, ChatType

from etherfi_bot.dispatcher import BotDispatcher


class TelegramUpdateAdapter:
    """Route typed PTB updates into the application dispatcher."""

    def __init__(
        self,
        dispatcher: BotDispatcher,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._logger = logger or logging.getLogger(__name__)

    async def handle_update(self, update: Update, _context: object = None) -> str:
        if update.message is not None:
            return await self._handle_message(update)
        if update.callback_query is not None:
            return await self._handle_callback_query(update)
        if update.my_chat_member is not None:
            return await self._handle_my_chat_member(update)
        if update.message_reaction is not None:
            return await self._handle_message_reaction(update)
        self._logger.debug(
            "telegram_update_ignored action=ignored_unsupported_update "
            "telegram_update_id=%s reason=unsupported_update",
            update.update_id,
        )
        return "ignored_unsupported_update"

    async def _handle_message(self, update: Update) -> str:
        message = update.message
        assert message is not None
        if message.chat.type != ChatType.PRIVATE:
            self._logger.debug(
                "telegram_update_ignored action=ignored_non_private_message "
                "telegram_update_id=%s reason=non_private_chat chat_type=%s",
                update.update_id,
                message.chat.type,
            )
            return "ignored_non_private_message"
        user = message.from_user
        if user is None or user.is_bot:
            self._logger.debug(
                "telegram_update_ignored action=ignored_message_without_user "
                "telegram_update_id=%s reason=missing_user",
                update.update_id,
            )
            return "ignored_message_without_user"
        if _is_start_command(message.text or "", message.entities):
            await self._dispatcher.start(user.id)
            return "start"
        await self._dispatcher.ignore_event(user.id)
        self._logger.debug(
            "telegram_update_ignored action=ignored_message telegram_update_id=%s "
            "telegram_user_id=%s reason=non_start_message",
            update.update_id,
            user.id,
        )
        return "ignored_message"

    async def _handle_callback_query(self, update: Update) -> str:
        callback = update.callback_query
        assert callback is not None
        user = callback.from_user
        message = callback.message
        message_id = None if message is None else message.message_id
        chat = None if message is None else message.chat
        data = callback.data
        action = "ignored_callback"
        is_private_callback = (
            chat is not None
            and chat.type == ChatType.PRIVATE
            and chat.id == user.id
            and message_id is not None
            and not user.is_bot
        )
        try:
            if is_private_callback:
                if data == "top_up":
                    await self._dispatcher.callback_top_up(user.id, int(message_id))
                    action = "callback_top_up"
                elif data == "ignore":
                    await self._dispatcher.callback_ignore(user.id, int(message_id))
                    action = "callback_ignore"
                else:
                    await self._dispatcher.ignore_event(user.id)
                    self._logger.debug(
                        "telegram_update_ignored action=ignored_callback "
                        "telegram_update_id=%s telegram_user_id=%s message_id=%s "
                        "callback_data=%s reason=unsupported_callback_data",
                        update.update_id,
                        user.id,
                        message_id,
                        data,
                    )
            else:
                self._logger.debug(
                    "telegram_update_ignored action=ignored_callback telegram_update_id=%s "
                    "telegram_user_id=%s chat_id=%s message_id=%s callback_data=%s "
                    "reason=missing_user_or_message_or_non_private_chat",
                    update.update_id,
                    user.id,
                    None if chat is None else chat.id,
                    message_id,
                    data,
                )
        finally:
            try:
                await callback.answer()
            except Exception as error:
                self._logger.warning(
                    "callback_ack_failed callback_query_id=%s telegram_user_id=%s "
                    "message_id=%s callback_data=%s error_type=%s error=%s",
                    callback.id,
                    user.id,
                    message_id,
                    data,
                    type(error).__name__,
                    error,
                )
        return action

    async def _handle_my_chat_member(self, update: Update) -> str:
        member_update = update.my_chat_member
        assert member_update is not None
        if member_update.chat.type != ChatType.PRIVATE:
            self._logger.debug(
                "telegram_update_ignored action=ignored_non_private_chat_member "
                "telegram_update_id=%s reason=non_private_chat chat_type=%s",
                update.update_id,
                member_update.chat.type,
            )
            return "ignored_non_private_chat_member"
        user = member_update.from_user
        if user.is_bot:
            self._logger.debug(
                "telegram_update_ignored action=ignored_chat_member_without_user "
                "telegram_update_id=%s reason=missing_user",
                update.update_id,
            )
            return "ignored_chat_member_without_user"
        if member_update.new_chat_member.status == ChatMemberStatus.BANNED:
            await self._dispatcher.user_blocked(user.id)
            return "user_blocked"
        await self._dispatcher.ignore_event(user.id)
        self._logger.debug(
            "telegram_update_ignored action=ignored_chat_member telegram_update_id=%s "
            "telegram_user_id=%s new_status=%s reason=unsupported_chat_member_status",
            update.update_id,
            user.id,
            member_update.new_chat_member.status,
        )
        return "ignored_chat_member"

    async def _handle_message_reaction(self, update: Update) -> str:
        reaction = update.message_reaction
        assert reaction is not None
        if reaction.chat.type != ChatType.PRIVATE:
            self._logger.debug(
                "telegram_update_ignored action=ignored_non_private_reaction "
                "telegram_update_id=%s reason=non_private_chat chat_type=%s",
                update.update_id,
                reaction.chat.type,
            )
            return "ignored_non_private_reaction"
        user = reaction.user
        if user is None or user.is_bot:
            self._logger.debug(
                "telegram_update_ignored action=ignored_reaction_without_user "
                "telegram_update_id=%s reason=missing_user",
                update.update_id,
            )
            return "ignored_reaction_without_user"
        await self._dispatcher.ignore_event(user.id)
        self._logger.debug(
            "telegram_update_ignored action=ignored_reaction telegram_update_id=%s "
            "telegram_user_id=%s reason=reaction_ignored",
            update.update_id,
            user.id,
        )
        return "ignored_reaction"


def _is_start_command(text: str, entities: tuple[MessageEntity, ...]) -> bool:
    if not text.startswith("/start"):
        return False
    return any(
        entity.offset == 0
        and entity.type == MessageEntity.BOT_COMMAND
        and text[: entity.length].split("@", 1)[0] == "/start"
        for entity in entities
    )
