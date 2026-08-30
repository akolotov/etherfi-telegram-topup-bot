from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, Forbidden, NetworkError

from etherfi_bot.domain import TelegramForbiddenError
from etherfi_bot.telegram_api import TelegramBotGateway
from tests.conftest import make_user


async def test_ptb_gateway_sends_messages_and_edits_markup() -> None:
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)),
        edit_message_reply_markup=AsyncMock(return_value=True),
        get_chat=AsyncMock(return_value=SimpleNamespace(id=1001)),
    )
    gateway = TelegramBotGateway(bot)
    user = make_user()

    assert await gateway.send_low_balance_prompt(user, Decimal("1.5")) == 42
    await gateway.remove_buttons(user.telegram_user_id, 42)

    send_call = bot.send_message.await_args.kwargs
    assert send_call["chat_id"] == user.telegram_user_id
    keyboard = send_call["reply_markup"].inline_keyboard
    assert [button.callback_data for button in keyboard[0]] == ["top_up", "ignore"]
    bot.edit_message_reply_markup.assert_awaited_once_with(
        chat_id=user.telegram_user_id,
        message_id=42,
        reply_markup=None,
    )


async def test_ptb_gateway_sends_top_up_outcome_messages() -> None:
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)),
    )
    gateway = TelegramBotGateway(bot)
    user = make_user()

    await gateway.send_top_up_not_needed(user)
    await gateway.send_insufficient_safe_balance(user)

    assert [call.kwargs for call in bot.send_message.await_args_list] == [
        {
            "chat_id": user.telegram_user_id,
            "text": "The latest account balance no longer requires a top-up.",
        },
        {
            "chat_id": user.telegram_user_id,
            "text": (
                "The Safe does not have enough available balance to create this top-up."
            ),
        },
    ]


async def test_ptb_gateway_marks_admin_messages() -> None:
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)),
    )
    gateway = TelegramBotGateway(bot)

    await gateway.send_admin_error(9001, "Safe transaction was created.")

    bot.send_message.assert_awaited_once_with(
        chat_id=9001,
        text="🛠️ Safe transaction was created.",
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (Forbidden("blocked"), False),
        (BadRequest("Chat not found"), False),
        (None, True),
    ],
)
async def test_ptb_gateway_private_chat_reachability(error, expected: bool) -> None:
    get_chat = AsyncMock(
        side_effect=error,
        return_value=SimpleNamespace(id=1001),
    )
    gateway = TelegramBotGateway(SimpleNamespace(get_chat=get_chat))

    assert await gateway.can_reach_private_chat(1001) is expected


async def test_ptb_gateway_propagates_non_chat_not_found_errors() -> None:
    gateway = TelegramBotGateway(
        SimpleNamespace(get_chat=AsyncMock(side_effect=NetworkError("unavailable")))
    )

    with pytest.raises(NetworkError):
        await gateway.can_reach_private_chat(1001)


async def test_ptb_gateway_maps_forbidden_send_to_domain_error() -> None:
    gateway = TelegramBotGateway(
        SimpleNamespace(send_message=AsyncMock(side_effect=Forbidden("blocked")))
    )

    with pytest.raises(TelegramForbiddenError):
        await gateway.send_safe_tx_created(make_user(), "safe-hash")
