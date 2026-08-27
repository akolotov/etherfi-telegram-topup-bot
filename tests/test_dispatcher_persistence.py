from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from etherfi_bot.dispatcher import BotDispatcher
from etherfi_bot.domain import BotState, UserState
from etherfi_bot.mocks import (
    MockBalanceProvider,
    MockClock,
    MockPrivateKeyProvider,
    MockSafeWalletClient,
    MockTelegramGateway,
)
from etherfi_bot.storage import JsonConfigRepository, JsonStateRepository

from tests.conftest import make_dispatcher, make_user, write_config


def test_dispatcher_ignores_unknown_users_and_starts_configured(tmp_path) -> None:
    user = make_user(telegram_user_id=1001)
    dispatcher, states, telegram, *_ = make_dispatcher(tmp_path, [user])

    assert dispatcher.start(9999) is None
    assert dispatcher.callback_top_up(9999, 1) is None
    assert dispatcher.callback_ignore(9999, 1) is None
    assert dispatcher.ignore_event(9999) is None
    assert states.list_states() == []
    assert telegram.messages == []

    state = dispatcher.start(user.telegram_user_id)

    assert state is not None
    assert state.state is BotState.MONITORING
    assert states.load(user.telegram_user_id).state is BotState.MONITORING


def test_json_config_repository_round_trips_balance_token_address(tmp_path) -> None:
    user = make_user(
        telegram_user_id=1001,
        balance_token_address="0x9999999999999999999999999999999999999999",
    )
    config_path = write_config(tmp_path / "config.json", [user])

    config = JsonConfigRepository(config_path).load()

    loaded_user = config.user(user.telegram_user_id)
    assert loaded_user is not None
    assert loaded_user.balance_token_address == user.balance_token_address


@pytest.mark.parametrize("invalid_value", [None, "", "   ", 123])
def test_json_config_repository_rejects_invalid_balance_token_address(
    tmp_path,
    invalid_value,
) -> None:
    user = make_user(telegram_user_id=1001)
    payload = {
        "admin_telegram_user_id": 9001,
        "users": [
            {
                "telegram_user_id": user.telegram_user_id,
                "target_account": user.target_account,
                "balance_token_address": invalid_value,
                "balance_threshold": str(user.balance_threshold),
                "target_max_balance": str(user.target_max_balance),
                "balance_check_interval_seconds": user.balance_check_interval_seconds,
                "safe_account": user.safe_account,
                "safe_proposer_key_file": user.safe_proposer_key_file,
                "low_balance_notification_limit": user.low_balance_notification_limit,
                "low_balance_notification_cooldown_seconds": (
                    user.low_balance_notification_cooldown_seconds
                ),
            }
        ],
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="balance_token_address must be a non-empty string"):
        JsonConfigRepository(config_path).load()


@pytest.mark.parametrize("invalid_value", [None, "", "   ", 123])
def test_json_config_repository_rejects_invalid_safe_proposer_key_file(
    tmp_path,
    invalid_value,
) -> None:
    user = make_user(telegram_user_id=1001)
    payload = {
        "admin_telegram_user_id": 9001,
        "users": [
            {
                "telegram_user_id": user.telegram_user_id,
                "target_account": user.target_account,
                "balance_token_address": user.balance_token_address,
                "balance_threshold": str(user.balance_threshold),
                "target_max_balance": str(user.target_max_balance),
                "balance_check_interval_seconds": user.balance_check_interval_seconds,
                "safe_account": user.safe_account,
                "safe_proposer_key_file": invalid_value,
                "low_balance_notification_limit": user.low_balance_notification_limit,
                "low_balance_notification_cooldown_seconds": (
                    user.low_balance_notification_cooldown_seconds
                ),
            }
        ],
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="safe_proposer_key_file must be a non-empty string",
    ):
        JsonConfigRepository(config_path).load()


def test_dispatcher_callbacks_before_start_are_noops(tmp_path) -> None:
    user = make_user(telegram_user_id=1001)
    dispatcher, states, telegram, balances, safe, *_ = make_dispatcher(tmp_path, [user])

    top_up_state = dispatcher.callback_top_up(user.telegram_user_id, 1)
    ignore_state = dispatcher.callback_ignore(user.telegram_user_id, 1)
    ignored_event_state = dispatcher.ignore_event(user.telegram_user_id)

    assert top_up_state is not None
    assert top_up_state.state is BotState.NOT_STARTED
    assert ignore_state is not None
    assert ignore_state.state is BotState.NOT_STARTED
    assert ignored_event_state is not None
    assert ignored_event_state.state is BotState.NOT_STARTED
    assert states.list_states() == []
    assert telegram.messages == []
    assert telegram.removed_buttons == []
    assert balances.reads == []
    assert safe.created_txs == []


def test_recover_missing_user_state_starts_reachable_user(tmp_path) -> None:
    user = make_user(telegram_user_id=4101)
    dispatcher, states, telegram, *_ = make_dispatcher(tmp_path, [user])

    recovered_user_ids = dispatcher.recover_missing_user_states()

    state = states.load(user.telegram_user_id)
    assert recovered_user_ids == [user.telegram_user_id]
    assert state.state is BotState.MONITORING
    assert state.next_tick_at is None
    assert telegram.private_chat_checks == [user.telegram_user_id]


def test_recover_missing_user_state_marks_unreachable_user_not_started(tmp_path) -> None:
    user = make_user(telegram_user_id=4102)
    dispatcher, states, telegram, *_ = make_dispatcher(tmp_path, [user])
    telegram.unreachable_private_chat_user_ids.add(user.telegram_user_id)

    recovered_user_ids = dispatcher.recover_missing_user_states()

    state = states.load(user.telegram_user_id)
    assert recovered_user_ids == []
    assert state.state is BotState.NOT_STARTED
    assert state.next_tick_at is None
    assert telegram.private_chat_checks == [user.telegram_user_id]


def test_recover_missing_user_state_skips_existing_states(tmp_path) -> None:
    active_user = make_user(telegram_user_id=4103)
    not_started_user = make_user(telegram_user_id=4104)
    users = [active_user, not_started_user]
    dispatcher, states, telegram, *_rest, clock = make_dispatcher(tmp_path, users)
    active_state = UserState(
        telegram_user_id=active_user.telegram_user_id,
        state=BotState.MONITORING,
        next_tick_at=clock.now() + timedelta(seconds=60),
    )
    not_started_state = UserState.new(not_started_user.telegram_user_id)
    states.save(active_state)
    states.save(not_started_state)

    recovered_user_ids = dispatcher.recover_missing_user_states()

    assert recovered_user_ids == []
    assert states.load(active_user.telegram_user_id).to_dict() == active_state.to_dict()
    assert (
        states.load(not_started_user.telegram_user_id).to_dict()
        == not_started_state.to_dict()
    )
    assert telegram.private_chat_checks == []


def test_recover_missing_user_state_keeps_state_missing_on_probe_error(tmp_path) -> None:
    user = make_user(telegram_user_id=4105)
    dispatcher, states, telegram, *_ = make_dispatcher(tmp_path, [user])
    telegram.private_chat_check_errors[user.telegram_user_id] = RuntimeError(
        "temporary Telegram failure"
    )

    recovered_user_ids = dispatcher.recover_missing_user_states()

    assert recovered_user_ids == []
    assert states.list_states() == []
    assert telegram.private_chat_checks == [user.telegram_user_id]


def test_multi_user_states_are_independent(tmp_path) -> None:
    user1 = make_user(telegram_user_id=1001)
    user2 = make_user(telegram_user_id=1002, threshold="5", max_balance="8")
    dispatcher, states, telegram, balances, *_ = make_dispatcher(tmp_path, [user1, user2])
    dispatcher.start(user1.telegram_user_id)
    dispatcher.start(user2.telegram_user_id)
    balances.set_balance(user1.target_account, "1")
    balances.set_balance(user2.target_account, "5")

    user1_state = dispatcher.balance_tick(user1.telegram_user_id)
    user2_state = dispatcher.balance_tick(user2.telegram_user_id)

    assert user1_state.state is BotState.LOW_PROMPT
    assert user2_state.state is BotState.MONITORING
    assert telegram.messages[-1].telegram_user_id == user1.telegram_user_id

    dispatcher.user_blocked(user1.telegram_user_id)

    assert states.load(user1.telegram_user_id).state is BotState.NOT_STARTED
    assert states.load(user2.telegram_user_id).state is BotState.MONITORING


def test_multi_user_independently_reaches_different_active_modes(tmp_path) -> None:
    low_cooldown_user = make_user(telegram_user_id=1001, limit=2)
    safe_reminder_user = make_user(telegram_user_id=1002, limit=3)
    dispatcher, states, telegram, balances, safe, *_rest, clock = make_dispatcher(
        tmp_path,
        [low_cooldown_user, safe_reminder_user],
    )
    dispatcher.start(low_cooldown_user.telegram_user_id)
    dispatcher.start(safe_reminder_user.telegram_user_id)
    balances.set_balance(low_cooldown_user.target_account, "1")
    balances.set_balance(safe_reminder_user.target_account, "1")

    dispatcher.balance_tick(low_cooldown_user.telegram_user_id)
    safe_prompt_state = dispatcher.balance_tick(safe_reminder_user.telegram_user_id)
    clock.advance(low_cooldown_user.balance_check_interval_seconds)
    dispatcher.balance_tick(low_cooldown_user.telegram_user_id)
    dispatcher.callback_top_up(
        safe_reminder_user.telegram_user_id,
        safe_prompt_state.current_message_id,
    )
    clock.advance(safe_reminder_user.low_balance_notification_cooldown_seconds)
    dispatcher.balance_tick(safe_reminder_user.telegram_user_id)

    assert states.load(low_cooldown_user.telegram_user_id).state is BotState.LOW_COOLDOWN
    safe_reminder_state = states.load(safe_reminder_user.telegram_user_id)
    assert safe_reminder_state.state is BotState.SAFE_TX_PENDING
    assert safe_reminder_state.pending_safe_tx_id == safe.created_txs[0].safe_tx_id
    assert safe_reminder_state.current_message_id is None
    assert telegram.messages[-1].kind == "safe_tx_pending_prompt"
    assert telegram.messages[-1].buttons is False
    assert len(safe.created_txs) == 1
    assert {message.telegram_user_id for message in telegram.messages} == {
        low_cooldown_user.telegram_user_id,
        safe_reminder_user.telegram_user_id,
    }


def test_multi_user_errors_for_one_user_do_not_change_another_user(tmp_path) -> None:
    user_a = make_user(telegram_user_id=1001)
    user_b = make_user(telegram_user_id=1002)
    dispatcher, states, telegram, balances, safe, *_ = make_dispatcher(
        tmp_path,
        [user_a, user_b],
    )
    dispatcher.start(user_a.telegram_user_id)
    dispatcher.start(user_b.telegram_user_id)
    balances.set_balance(user_a.target_account, "1")
    balances.set_balance(user_b.target_account, "1")
    dispatcher.balance_tick(user_b.telegram_user_id)
    user_b_snapshot = states.load(user_b.telegram_user_id).to_dict()

    balances.fail_accounts.add(user_a.target_account)
    dispatcher.balance_tick(user_a.telegram_user_id)
    assert states.load(user_b.telegram_user_id).to_dict() == user_b_snapshot

    balances.fail_accounts.remove(user_a.target_account)
    prompt_state = dispatcher.balance_tick(user_a.telegram_user_id)
    safe.fail_create_for_users.add(user_a.telegram_user_id)
    dispatcher.callback_top_up(user_a.telegram_user_id, prompt_state.current_message_id)
    assert states.load(user_b.telegram_user_id).to_dict() == user_b_snapshot

    safe.fail_create_for_users.remove(user_a.telegram_user_id)
    prompt_state = dispatcher.balance_tick(user_a.telegram_user_id)
    telegram.forbid_operation(user_a.telegram_user_id, "send_safe_tx_created")
    dispatcher.callback_top_up(user_a.telegram_user_id, prompt_state.current_message_id)

    assert states.load(user_a.telegram_user_id).state is BotState.NOT_STARTED
    assert states.load(user_b.telegram_user_id).to_dict() == user_b_snapshot


def test_json_state_repository_round_trips_datetime_and_decimal(tmp_path) -> None:
    repository = JsonStateRepository(tmp_path / "states")
    now = datetime(2026, 5, 18, 12, 30, tzinfo=timezone.utc)
    state = UserState(
        telegram_user_id=42,
        state=BotState.LOW_COOLDOWN,
        notification_count=3,
        low_cooldown_until=now + timedelta(seconds=300),
        tx_reminder_until=now + timedelta(seconds=600),
        current_message_id=77,
        pending_safe_tx_id="safe-tx-1",
        last_balance_checked_at=now,
        next_tick_at=now + timedelta(seconds=60),
        last_balance=Decimal("0.123456789"),
        low_balance_drop_admin_notified=True,
    )

    repository.save(state)
    loaded = repository.load(42)

    assert loaded.state is BotState.LOW_COOLDOWN
    assert loaded.low_cooldown_until == state.low_cooldown_until
    assert loaded.tx_reminder_until == state.tx_reminder_until
    assert loaded.last_balance == Decimal("0.123456789")
    assert loaded.low_balance_drop_admin_notified is True


async def test_restart_respects_persisted_next_tick_and_runs_missing_next_tick(tmp_path) -> None:
    future_user = make_user(telegram_user_id=4001)
    missing_tick_user = make_user(telegram_user_id=4002)
    due_user = make_user(telegram_user_id=4003)
    users = [future_user, missing_tick_user, due_user]
    config_path = write_config(tmp_path / "config.json", users)
    states = JsonStateRepository(tmp_path / "states")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    states.save(
        UserState(
            telegram_user_id=future_user.telegram_user_id,
            state=BotState.MONITORING,
            next_tick_at=now + timedelta(seconds=60),
        )
    )
    states.save(
        UserState(
            telegram_user_id=missing_tick_user.telegram_user_id,
            state=BotState.MONITORING,
            next_tick_at=None,
        )
    )
    states.save(
        UserState(
            telegram_user_id=due_user.telegram_user_id,
            state=BotState.MONITORING,
            next_tick_at=now,
        )
    )
    telegram = MockTelegramGateway()
    balances = MockBalanceProvider()
    for user in users:
        balances.set_balance(user.target_account, "1")
    dispatcher = BotDispatcher(
        config_repository=JsonConfigRepository(config_path),
        state_repository=states,
        telegram=telegram,
        balances=balances,
        safe_wallet=MockSafeWalletClient(),
        private_keys=MockPrivateKeyProvider(
            {user.safe_proposer_key_file: f"key-{user.telegram_user_id}" for user in users}
        ),
        clock=MockClock(now),
    )

    due_user_ids = await dispatcher.restart(run_due_ticks=True)

    assert due_user_ids == [
        missing_tick_user.telegram_user_id,
        due_user.telegram_user_id,
    ]
    assert states.load(future_user.telegram_user_id).state is BotState.MONITORING
    assert states.load(future_user.telegram_user_id).last_balance_checked_at is None
    assert [message.telegram_user_id for message in telegram.messages] == due_user_ids


def test_restart_runs_due_ticks_from_persisted_state(tmp_path) -> None:
    user = make_user(telegram_user_id=1001)
    dispatcher, states, telegram, balances, *_rest, clock = make_dispatcher(tmp_path, [user])
    dispatcher.start(user.telegram_user_id)
    balances.set_balance(user.target_account, "1")
    clock.advance(user.balance_check_interval_seconds)

    due_user_ids = dispatcher.restart(run_due_ticks=True)

    assert due_user_ids == [user.telegram_user_id]
    assert states.load(user.telegram_user_id).state is BotState.LOW_PROMPT
    assert telegram.messages[-1].telegram_user_id == user.telegram_user_id


async def test_new_dispatcher_restores_persisted_active_states(tmp_path) -> None:
    user_s2 = make_user(telegram_user_id=2001)
    user_s3 = make_user(telegram_user_id=2002, limit=2)
    user_s4 = make_user(telegram_user_id=2003)
    user_s4_after_reminder = make_user(telegram_user_id=2004)
    users = [user_s2, user_s3, user_s4, user_s4_after_reminder]
    config_path = write_config(tmp_path / "config.json", users)
    state_dir = tmp_path / "states"
    states = JsonStateRepository(state_dir)
    telegram = MockTelegramGateway()
    balances = MockBalanceProvider()
    safe = MockSafeWalletClient()
    private_keys = MockPrivateKeyProvider(
        {user.safe_proposer_key_file: f"key-{user.telegram_user_id}" for user in users}
    )
    clock = MockClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    dispatcher = BotDispatcher(
        config_repository=JsonConfigRepository(config_path),
        state_repository=states,
        telegram=telegram,
        balances=balances,
        safe_wallet=safe,
        private_keys=private_keys,
        clock=clock,
    )
    for user in users:
        await dispatcher.start(user.telegram_user_id)
        balances.set_balance(user.target_account, "1")

    await dispatcher.balance_tick(user_s2.telegram_user_id)
    await dispatcher.balance_tick(user_s3.telegram_user_id)
    clock.advance(user_s3.balance_check_interval_seconds)
    await dispatcher.balance_tick(user_s3.telegram_user_id)
    state_s4_prompt = await dispatcher.balance_tick(user_s4.telegram_user_id)
    await dispatcher.callback_top_up(user_s4.telegram_user_id, state_s4_prompt.current_message_id)
    state_s4_after_reminder_prompt = await dispatcher.balance_tick(
        user_s4_after_reminder.telegram_user_id
    )
    await dispatcher.callback_top_up(
        user_s4_after_reminder.telegram_user_id,
        state_s4_after_reminder_prompt.current_message_id,
    )
    clock.advance(user_s4_after_reminder.low_balance_notification_cooldown_seconds)
    await dispatcher.balance_tick(user_s4_after_reminder.telegram_user_id)

    expected_states = {
        user.telegram_user_id: states.load(user.telegram_user_id).to_dict()
        for user in users
    }
    reloaded_states = JsonStateRepository(state_dir)
    reloaded_dispatcher = BotDispatcher(
        config_repository=JsonConfigRepository(config_path),
        state_repository=reloaded_states,
        telegram=MockTelegramGateway(),
        balances=MockBalanceProvider(),
        safe_wallet=MockSafeWalletClient(),
        private_keys=MockPrivateKeyProvider(
            {user.safe_proposer_key_file: f"key-{user.telegram_user_id}" for user in users}
        ),
        clock=clock,
    )

    assert reloaded_dispatcher.config.user(user_s2.telegram_user_id) == user_s2
    assert reloaded_states.load(user_s2.telegram_user_id).state is BotState.LOW_PROMPT
    assert reloaded_states.load(user_s3.telegram_user_id).state is BotState.LOW_COOLDOWN
    assert reloaded_states.load(user_s4.telegram_user_id).state is BotState.SAFE_TX_PENDING
    s4_after_reminder_state = reloaded_states.load(user_s4_after_reminder.telegram_user_id)
    assert s4_after_reminder_state.state is BotState.SAFE_TX_PENDING
    assert s4_after_reminder_state.pending_safe_tx_id is not None
    assert s4_after_reminder_state.tx_reminder_until is not None
    assert s4_after_reminder_state.current_message_id is None
    for telegram_user_id, expected_state in expected_states.items():
        assert reloaded_states.load(telegram_user_id).to_dict() == expected_state


async def test_new_dispatcher_restart_processes_multiple_due_users(tmp_path) -> None:
    user1 = make_user(telegram_user_id=3001)
    user2 = make_user(telegram_user_id=3002)
    users = [user1, user2]
    config_path = write_config(tmp_path / "config.json", users)
    state_dir = tmp_path / "states"
    states = JsonStateRepository(state_dir)
    clock = MockClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    initial_dispatcher = BotDispatcher(
        config_repository=JsonConfigRepository(config_path),
        state_repository=states,
        telegram=MockTelegramGateway(),
        balances=MockBalanceProvider(),
        safe_wallet=MockSafeWalletClient(),
        private_keys=MockPrivateKeyProvider(
            {user.safe_proposer_key_file: f"key-{user.telegram_user_id}" for user in users}
        ),
        clock=clock,
    )
    await initial_dispatcher.start(user1.telegram_user_id)
    await initial_dispatcher.start(user2.telegram_user_id)
    clock.advance(user1.balance_check_interval_seconds)

    reloaded_states = JsonStateRepository(state_dir)
    telegram = MockTelegramGateway()
    balances = MockBalanceProvider()
    for user in users:
        balances.set_balance(user.target_account, "1")
    reloaded_dispatcher = BotDispatcher(
        config_repository=JsonConfigRepository(config_path),
        state_repository=reloaded_states,
        telegram=telegram,
        balances=balances,
        safe_wallet=MockSafeWalletClient(),
        private_keys=MockPrivateKeyProvider(
            {user.safe_proposer_key_file: f"key-{user.telegram_user_id}" for user in users}
        ),
        clock=clock,
    )

    due_user_ids = await reloaded_dispatcher.restart(run_due_ticks=True)

    assert due_user_ids == [user1.telegram_user_id, user2.telegram_user_id]
    assert reloaded_states.load(user1.telegram_user_id).state is BotState.LOW_PROMPT
    assert reloaded_states.load(user2.telegram_user_id).state is BotState.LOW_PROMPT
    assert [message.telegram_user_id for message in telegram.messages] == [
        user1.telegram_user_id,
        user2.telegram_user_id,
    ]
