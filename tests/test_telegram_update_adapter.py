from __future__ import annotations

import copy
import json
from pathlib import Path

from telegram import Bot, Update

from etherfi_bot.domain import BotState
from etherfi_bot.telegram_adapter import TelegramUpdateAdapter
from tests.conftest import make_dispatcher, make_user


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "telegram_updates"


class RecordingBot(Bot):
    __slots__ = ("_callback_ids", "_fail_callback_answer")

    def __init__(self, *, fail_callback_answer: bool = False) -> None:
        super().__init__("123:ABC")
        self._callback_ids: list[str] = []
        self._fail_callback_answer = fail_callback_answer

    @property
    def callback_ids(self) -> list[str]:
        return self._callback_ids

    async def answer_callback_query(self, callback_query_id: str, **_kwargs) -> bool:
        self._callback_ids.append(callback_query_id)
        if self._fail_callback_answer:
            raise RuntimeError("expired callback")
        return True


def load_fixture(name: str) -> dict:
    path = FIXTURES_DIR / f"{name}.anonymized.json"
    return json.loads(path.read_text(encoding="utf-8"))


def ptb_update(payload: dict, bot: Bot | None = None) -> Update:
    update = Update.de_json(payload, bot or RecordingBot())
    assert update is not None
    return update


async def test_start_update_enters_monitoring_for_configured_user(tmp_path) -> None:
    user = make_user(telegram_user_id=1001)
    dispatcher, states, *_ = make_dispatcher(tmp_path, [user])
    adapter = TelegramUpdateAdapter(dispatcher)

    action = await adapter.handle_update(ptb_update(load_fixture("message_command_start")))

    assert action == "start"
    assert states.load(user.telegram_user_id).state is BotState.MONITORING


async def test_start_update_ignores_unknown_user_without_state_file(tmp_path) -> None:
    user = make_user(telegram_user_id=1001)
    dispatcher, states, *_ = make_dispatcher(tmp_path, [user])
    adapter = TelegramUpdateAdapter(dispatcher)
    payload = load_fixture("message_command_start")
    payload["message"]["from"]["id"] = 9999
    payload["message"]["chat"]["id"] = 9999

    assert await adapter.handle_update(ptb_update(payload)) == "start"
    assert states.list_states() == []


async def test_callbacks_dispatch_and_are_acknowledged(tmp_path) -> None:
    user = make_user(telegram_user_id=1001)
    dispatcher, states, _telegram, balances, safe, *_ = make_dispatcher(tmp_path, [user])
    bot = RecordingBot()
    adapter = TelegramUpdateAdapter(dispatcher)
    await dispatcher.start(user.telegram_user_id)
    balances.set_balance(user.target_account, "1")
    prompt_state = await dispatcher.balance_tick(user.telegram_user_id)
    balances.set_balance(user.target_account, "2")
    payload = load_fixture("callback_query_top_up")
    payload["callback_query"]["message"]["message_id"] = prompt_state.current_message_id

    action = await adapter.handle_update(ptb_update(payload, bot))

    state = states.load(user.telegram_user_id)
    assert action == "callback_top_up"
    assert state.state is BotState.SAFE_TX_PENDING
    assert len(safe.created_txs) == 1
    assert bot.callback_ids == [payload["callback_query"]["id"]]


async def test_callback_ack_failure_does_not_undo_dispatch(tmp_path) -> None:
    user = make_user(telegram_user_id=1001)
    dispatcher, states, _telegram, balances, safe, *_ = make_dispatcher(tmp_path, [user])
    adapter = TelegramUpdateAdapter(dispatcher)
    await dispatcher.start(user.telegram_user_id)
    balances.set_balance(user.target_account, "1")
    prompt = await dispatcher.balance_tick(user.telegram_user_id)
    balances.set_balance(user.target_account, "2")
    payload = load_fixture("callback_query_top_up")
    payload["callback_query"]["message"]["message_id"] = prompt.current_message_id

    action = await adapter.handle_update(
        ptb_update(payload, RecordingBot(fail_callback_answer=True))
    )

    assert action == "callback_top_up"
    assert states.load(user.telegram_user_id).state is BotState.SAFE_TX_PENDING
    assert len(safe.created_txs) == 1


async def test_stale_group_and_unsupported_callbacks_are_noops(tmp_path) -> None:
    user = make_user(telegram_user_id=1001)
    dispatcher, states, _telegram, balances, safe, *_ = make_dispatcher(tmp_path, [user])
    adapter = TelegramUpdateAdapter(dispatcher)
    await dispatcher.start(user.telegram_user_id)
    balances.set_balance(user.target_account, "1")
    prompt = await dispatcher.balance_tick(user.telegram_user_id)
    state_before = states.load(user.telegram_user_id).to_dict()

    stale = load_fixture("callback_query_top_up")
    stale["callback_query"]["message"]["message_id"] = prompt.current_message_id + 99
    assert await adapter.handle_update(ptb_update(stale)) == "callback_top_up"

    unsupported = copy.deepcopy(stale)
    unsupported["callback_query"]["message"]["message_id"] = prompt.current_message_id
    unsupported["callback_query"]["data"] = "unsupported"
    assert await adapter.handle_update(ptb_update(unsupported)) == "ignored_callback"

    group = copy.deepcopy(unsupported)
    group["callback_query"]["data"] = "top_up"
    group["callback_query"]["message"]["chat"] = {
        "id": -1001234567890,
        "type": "supergroup",
    }
    assert await adapter.handle_update(ptb_update(group)) == "ignored_callback"
    assert states.load(user.telegram_user_id).to_dict() == state_before
    assert safe.created_txs == []


async def test_plain_reactions_and_block_updates_use_typed_ptb_fields(tmp_path) -> None:
    user = make_user(telegram_user_id=1001)
    dispatcher, states, _telegram, balances, *_ = make_dispatcher(tmp_path, [user])
    adapter = TelegramUpdateAdapter(dispatcher)
    await dispatcher.start(user.telegram_user_id)
    balances.set_balance(user.target_account, "1")
    await dispatcher.balance_tick(user.telegram_user_id)

    for name in ["message_plain_text", "message_reaction_add", "message_reaction_remove"]:
        assert (await adapter.handle_update(ptb_update(load_fixture(name)))).startswith(
            "ignored"
        )

    action = await adapter.handle_update(ptb_update(load_fixture("my_chat_member_block")))
    assert action == "user_blocked"
    assert states.load(user.telegram_user_id).state is BotState.NOT_STARTED
