from __future__ import annotations

import copy
import json
from pathlib import Path

from etherfi_bot.domain import BotState
from etherfi_bot.telegram_adapter import TelegramUpdateAdapter

from tests.conftest import make_dispatcher, make_user

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "telegram_updates"


class CallbackRecorder:
    def __init__(self) -> None:
        self.ids: list[str] = []

    def answer_callback_query(self, callback_query_id: str) -> bool:
        self.ids.append(callback_query_id)
        return True


class FailingCallbackAnswerer:
    def answer_callback_query(self, callback_query_id: str) -> bool:
        raise RuntimeError(f"expired callback {callback_query_id}")


def load_fixture(name: str) -> dict:
    path = FIXTURES_DIR / f"{name}.anonymized.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_start_update_enters_monitoring_for_configured_user(tmp_path) -> None:
    user = make_user(telegram_user_id=1001)
    dispatcher, states, *_ = make_dispatcher(tmp_path, [user])
    adapter = TelegramUpdateAdapter(dispatcher)

    action = adapter.handle_update(load_fixture("message_command_start"))

    assert action == "start"
    assert states.load(user.telegram_user_id).state is BotState.MONITORING


def test_start_update_ignores_unknown_user_without_state_file(tmp_path) -> None:
    user = make_user(telegram_user_id=1001)
    dispatcher, states, *_ = make_dispatcher(tmp_path, [user])
    adapter = TelegramUpdateAdapter(dispatcher)
    update = load_fixture("message_command_start")
    update["message"]["from"]["id"] = 9999
    update["message"]["chat"]["id"] = 9999

    action = adapter.handle_update(update)

    assert action == "start"
    assert states.list_states() == []


def test_top_up_callback_uses_callback_message_id_and_acknowledges(tmp_path) -> None:
    user = make_user(telegram_user_id=1001)
    dispatcher, states, _telegram, balances, safe, *_ = make_dispatcher(tmp_path, [user])
    callback_recorder = CallbackRecorder()
    adapter = TelegramUpdateAdapter(dispatcher, callback_answerer=callback_recorder)
    dispatcher.start(user.telegram_user_id)
    balances.set_balance(user.target_account, "1")
    prompt_state = dispatcher.balance_tick(user.telegram_user_id)
    balances.set_balance(user.target_account, "2")
    update = load_fixture("callback_query_top_up")
    update["callback_query"]["message"]["message_id"] = prompt_state.current_message_id

    action = adapter.handle_update(update)

    state = states.load(user.telegram_user_id)
    assert action == "callback_top_up"
    assert state.state is BotState.SAFE_TX_PENDING
    assert state.pending_safe_tx_id == "safe-tx-1"
    balance_key = (user.target_account, user.balance_token_address)
    assert safe.created_txs[0].amount == user.target_max_balance - balances.balances[balance_key]
    assert callback_recorder.ids == [update["callback_query"]["id"]]


def test_callback_ack_failure_does_not_undo_dispatch(tmp_path) -> None:
    user = make_user(telegram_user_id=1001)
    dispatcher, states, _telegram, balances, safe, *_ = make_dispatcher(tmp_path, [user])
    adapter = TelegramUpdateAdapter(dispatcher, callback_answerer=FailingCallbackAnswerer())
    dispatcher.start(user.telegram_user_id)
    balances.set_balance(user.target_account, "1")
    prompt_state = dispatcher.balance_tick(user.telegram_user_id)
    balances.set_balance(user.target_account, "2")
    update = load_fixture("callback_query_top_up")
    update["callback_query"]["message"]["message_id"] = prompt_state.current_message_id

    action = adapter.handle_update(update)

    assert action == "callback_top_up"
    assert states.load(user.telegram_user_id).state is BotState.SAFE_TX_PENDING
    assert len(safe.created_txs) == 1


def test_ignore_callback_uses_callback_message_id_and_acknowledges(tmp_path) -> None:
    user = make_user(telegram_user_id=1001)
    dispatcher, states, _telegram, balances, *_ = make_dispatcher(tmp_path, [user])
    callback_recorder = CallbackRecorder()
    adapter = TelegramUpdateAdapter(dispatcher, callback_answerer=callback_recorder)
    dispatcher.start(user.telegram_user_id)
    balances.set_balance(user.target_account, "1")
    prompt_state = dispatcher.balance_tick(user.telegram_user_id)
    update = load_fixture("callback_query_ignore")
    update["callback_query"]["message"]["message_id"] = prompt_state.current_message_id

    action = adapter.handle_update(update)

    assert action == "callback_ignore"
    assert states.load(user.telegram_user_id).state is BotState.MONITORING
    assert callback_recorder.ids == [update["callback_query"]["id"]]


def test_stale_and_unsupported_callbacks_are_noops_but_acknowledged(tmp_path) -> None:
    user = make_user(telegram_user_id=1001)
    dispatcher, states, _telegram, balances, safe, *_ = make_dispatcher(tmp_path, [user])
    callback_recorder = CallbackRecorder()
    adapter = TelegramUpdateAdapter(dispatcher, callback_answerer=callback_recorder)
    dispatcher.start(user.telegram_user_id)
    balances.set_balance(user.target_account, "1")
    prompt_state = dispatcher.balance_tick(user.telegram_user_id)
    state_before = states.load(user.telegram_user_id).to_dict()

    stale = load_fixture("callback_query_top_up")
    stale["callback_query"]["message"]["message_id"] = prompt_state.current_message_id + 99
    stale_action = adapter.handle_update(stale)

    unsupported = copy.deepcopy(stale)
    unsupported["callback_query"]["id"] = "unsupported-callback"
    unsupported["callback_query"]["message"]["message_id"] = prompt_state.current_message_id
    unsupported["callback_query"]["data"] = "unsupported"
    unsupported_action = adapter.handle_update(unsupported)

    assert stale_action == "callback_top_up"
    assert unsupported_action == "ignored_callback"
    assert states.load(user.telegram_user_id).to_dict() == state_before
    assert safe.created_txs == []
    assert callback_recorder.ids == [stale["callback_query"]["id"], "unsupported-callback"]


def test_plain_reply_and_reaction_updates_are_ignored(tmp_path) -> None:
    user = make_user(telegram_user_id=1001)
    dispatcher, states, _telegram, balances, *_ = make_dispatcher(tmp_path, [user])
    adapter = TelegramUpdateAdapter(dispatcher)
    dispatcher.start(user.telegram_user_id)
    balances.set_balance(user.target_account, "1")
    prompt_state = dispatcher.balance_tick(user.telegram_user_id)
    state_before = states.load(user.telegram_user_id).to_dict()

    for name in [
        "message_plain_text",
        "message_reply_text",
        "message_reaction_add",
        "message_reaction_replace",
        "message_reaction_remove",
    ]:
        assert adapter.handle_update(load_fixture(name)).startswith("ignored")

    assert states.load(user.telegram_user_id).to_dict() == state_before
    assert states.load(user.telegram_user_id).current_message_id == prompt_state.current_message_id


def test_private_block_update_resets_user_to_not_started(tmp_path) -> None:
    user = make_user(telegram_user_id=1001)
    dispatcher, states, _telegram, balances, *_ = make_dispatcher(tmp_path, [user])
    adapter = TelegramUpdateAdapter(dispatcher)
    dispatcher.start(user.telegram_user_id)
    balances.set_balance(user.target_account, "1")
    dispatcher.balance_tick(user.telegram_user_id)

    action = adapter.handle_update(load_fixture("my_chat_member_block"))

    assert action == "user_blocked"
    assert states.load(user.telegram_user_id).state is BotState.NOT_STARTED
