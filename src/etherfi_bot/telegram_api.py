from __future__ import annotations

from decimal import Decimal

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden

from etherfi_bot.domain import TelegramForbiddenError, UserConfig


class TelegramBotGateway:
    """Async Telegram gateway backed entirely by python-telegram-bot."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send_low_balance_prompt(
        self, user: UserConfig, balance: Decimal
    ) -> int:
        try:
            message = await self._bot.send_message(
                chat_id=user.telegram_user_id,
                text=f"Balance is low: {balance}. Top up?",
                reply_markup=InlineKeyboardMarkup(
                    [[
                        InlineKeyboardButton("Top Up", callback_data="top_up"),
                        InlineKeyboardButton("Ignore", callback_data="ignore"),
                    ]]
                ),
            )
        except Forbidden as error:
            raise TelegramForbiddenError(str(error)) from error
        return int(message.message_id)

    async def send_safe_tx_created(self, user: UserConfig, safe_tx_id: str) -> int:
        del safe_tx_id
        return await self._send_user_message(
            user,
            "Safe transaction was created. Please sign and execute it.",
        )

    async def send_top_up_not_needed(self, user: UserConfig) -> int:
        return await self._send_user_message(
            user,
            "The latest account balance no longer requires a top-up.",
        )

    async def send_insufficient_safe_balance(self, user: UserConfig) -> int:
        return await self._send_user_message(
            user,
            "The Safe does not have enough available balance to create this top-up.",
        )

    async def send_safe_tx_pending_prompt(
        self, user: UserConfig, safe_tx_id: str
    ) -> int:
        del safe_tx_id
        return await self._send_user_message(
            user,
            "Balance is still low. A top-up Safe transaction is pending. "
            "Please sign and execute it.",
        )

    async def send_existing_safe_tx_notice(
        self, user: UserConfig, safe_tx_id: str
    ) -> int:
        return await self.send_safe_tx_pending_prompt(user, safe_tx_id)

    async def remove_buttons(self, telegram_user_id: int, message_id: int) -> None:
        try:
            await self._bot.edit_message_reply_markup(
                chat_id=int(telegram_user_id),
                message_id=int(message_id),
                reply_markup=None,
            )
        except Forbidden as error:
            raise TelegramForbiddenError(str(error)) from error

    async def send_admin_error(
        self, admin_telegram_user_id: int, message: str
    ) -> None:
        try:
            await self._bot.send_message(
                chat_id=int(admin_telegram_user_id), text=f"🛠️ {message}"
            )
        except Forbidden as error:
            raise TelegramForbiddenError(str(error)) from error

    async def can_reach_private_chat(self, telegram_user_id: int) -> bool:
        try:
            await self._bot.get_chat(chat_id=int(telegram_user_id))
        except Forbidden:
            return False
        except BadRequest as error:
            if "chat not found" in str(error).lower():
                return False
            raise
        return True

    async def _send_user_message(self, user: UserConfig, text: str) -> int:
        try:
            message = await self._bot.send_message(
                chat_id=user.telegram_user_id, text=text
            )
        except Forbidden as error:
            raise TelegramForbiddenError(str(error)) from error
        return int(message.message_id)
