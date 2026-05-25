from __future__ import annotations

import threading
import time
from datetime import timedelta
from decimal import Decimal

from etherfi_bot.domain import BotState, SafeTxCreateError, SafeTxStatus
from etherfi_bot.fsm import FsmService
from etherfi_bot.mocks import (
    MockBalanceProvider,
    MockClock,
    MockPrivateKeyProvider,
    MockSafeWalletClient,
    MockTelegramGateway,
)
from etherfi_bot.storage import JsonStateRepository

from tests.conftest import FsmHarness, make_user


def start_user(harness: FsmHarness) -> None:
    harness.fsm.start(harness.user)


def assert_tick_scheduled(state, harness: FsmHarness, handled_at) -> None:
    assert state.last_balance_checked_at == handled_at
    assert state.next_tick_at == handled_at + timedelta(
        seconds=harness.user.balance_check_interval_seconds
    )


def make_low_prompt(harness: FsmHarness, balance: Decimal | str = "1") -> int:
    start_user(harness)
    harness.balances.set_balance(harness.user.target_account, balance)
    state = harness.fsm.balance_tick(harness.user)
    assert state.current_message_id is not None
    return state.current_message_id


def make_pending_safe_tx(harness: FsmHarness) -> str:
    message_id = make_low_prompt(harness, "1")
    harness.balances.set_balance(harness.user.target_account, "2")
    state = harness.fsm.callback_top_up(harness.user, message_id)
    assert state.pending_safe_tx_id is not None
    return state.pending_safe_tx_id


def make_low_cooldown(harness: FsmHarness) -> int:
    message_id = make_low_prompt(harness, "1")
    while harness.states.load(harness.user.telegram_user_id).state is not BotState.LOW_COOLDOWN:
        harness.clock.advance(harness.user.balance_check_interval_seconds)
        state = harness.fsm.balance_tick(harness.user)
        assert state.current_message_id is not None
        message_id = state.current_message_id
    return message_id


def send_due_safe_tx_reminder(harness: FsmHarness) -> str:
    tx_id = make_pending_safe_tx(harness)
    harness.clock.advance(harness.user.low_balance_notification_cooldown_seconds)
    handled_at = harness.clock.now()
    harness.balances.set_balance(harness.user.target_account, "1")
    message_count = len(harness.telegram.messages)
    state = harness.fsm.balance_tick(harness.user)

    assert state.state is BotState.SAFE_TX_PENDING
    assert state.pending_safe_tx_id == tx_id
    assert state.current_message_id is None
    assert state.tx_reminder_until == handled_at + timedelta(
        seconds=harness.user.low_balance_notification_cooldown_seconds
    )
    assert len(harness.telegram.messages) == message_count + 1
    assert harness.telegram.messages[-1].kind == "safe_tx_pending_prompt"
    assert harness.telegram.messages[-1].buttons is False
    return tx_id


def test_start_configured_user_enters_monitoring(harness_factory) -> None:
    harness = harness_factory()

    state = harness.fsm.start(harness.user)

    assert state.state is BotState.MONITORING
    assert state.next_tick_at is None


def test_repeated_start_preserves_existing_next_tick(harness_factory) -> None:
    harness = harness_factory()

    harness.fsm.start(harness.user)
    harness.balances.set_balance(harness.user.target_account, "10")
    state = harness.fsm.balance_tick(harness.user)
    first_next_tick = state.next_tick_at
    harness.clock.advance(30)
    repeated = harness.fsm.start(harness.user)

    assert repeated.state is BotState.MONITORING
    assert repeated.next_tick_at == first_next_tick


def test_s1_balance_ok_stays_monitoring(harness_factory) -> None:
    harness = harness_factory()
    start_user(harness)
    harness.balances.set_balance(harness.user.target_account, "10")

    state = harness.fsm.balance_tick(harness.user)

    assert state.state is BotState.MONITORING
    assert state.last_balance == Decimal("10")
    assert state.last_balance_checked_at == harness.clock.now()
    assert len(harness.telegram.messages) == 0


def test_s1_balance_read_failure_notifies_admin_and_keeps_state(harness_factory) -> None:
    harness = harness_factory()
    start_user(harness)
    harness.balances.fail_accounts.add(harness.user.target_account)

    state = harness.fsm.balance_tick(harness.user)

    assert state.state is BotState.MONITORING
    assert len(harness.telegram.admin_errors) == 1
    assert (
        harness.telegram.admin_errors[0][1]
        == f"Balance read failed for target account "
        f"{harness.user.target_account}: Could not read balance"
    )
    assert str(harness.user.telegram_user_id) not in harness.telegram.admin_errors[0][1]
    assert harness.user.balance_token_address not in harness.telegram.admin_errors[0][1]


def test_low_balance_drop_notifies_admin_once_until_balance_recovers_above_threshold(
    harness_factory,
) -> None:
    user = make_user(threshold="200", max_balance="300")
    harness = harness_factory(user)
    start_user(harness)

    def tick(balance: str):
        harness.balances.set_balance(user.target_account, balance)
        return harness.fsm.balance_tick(user)

    tick("201")
    tick("201")

    state = tick("199")

    assert state.low_balance_drop_admin_notified is True
    assert len(harness.telegram.admin_errors) == 1
    assert harness.telegram.admin_errors[-1][0] == 9001
    assert (
        harness.telegram.admin_errors[-1][1]
        == f"{user.target_account} balance dropped below 200, current balance 199"
    )

    tick("199")
    tick("190")
    tick("190")

    assert len(harness.telegram.admin_errors) == 1

    state = tick("250")

    assert state.low_balance_drop_admin_notified is False
    assert len(harness.telegram.admin_errors) == 1

    tick("204")
    state = tick("180")

    assert state.low_balance_drop_admin_notified is True
    assert len(harness.telegram.admin_errors) == 2
    assert (
        harness.telegram.admin_errors[-1][1]
        == f"{user.target_account} balance dropped below 200, current balance 180"
    )


def test_balance_drop_admin_notification_ignores_first_read_and_above_threshold_drop(
    harness_factory,
) -> None:
    user = make_user(threshold="200", max_balance="300")
    first_low_harness = harness_factory(user)
    start_user(first_low_harness)
    first_low_harness.balances.set_balance(user.target_account, "199")

    first_low_state = first_low_harness.fsm.balance_tick(user)

    assert first_low_state.low_balance_drop_admin_notified is False
    assert first_low_harness.telegram.admin_errors == []

    above_threshold_harness = harness_factory(user)
    start_user(above_threshold_harness)
    above_threshold_harness.balances.set_balance(user.target_account, "250")
    above_threshold_harness.fsm.balance_tick(user)
    above_threshold_harness.balances.set_balance(user.target_account, "240")

    above_threshold_state = above_threshold_harness.fsm.balance_tick(user)

    assert above_threshold_state.low_balance_drop_admin_notified is False
    assert above_threshold_harness.telegram.admin_errors == []


def test_balance_read_failure_does_not_send_balance_drop_admin_notification(
    harness_factory,
) -> None:
    user = make_user(threshold="200", max_balance="300")
    harness = harness_factory(user)
    start_user(harness)
    harness.balances.set_balance(user.target_account, "201")
    harness.fsm.balance_tick(user)
    harness.balances.fail_accounts.add(user.target_account)

    state = harness.fsm.balance_tick(user)

    assert state.low_balance_drop_admin_notified is False
    assert len(harness.telegram.admin_errors) == 1
    assert "Balance read failed" in harness.telegram.admin_errors[-1][1]
    assert not any(
        "balance dropped below" in message
        for _, message in harness.telegram.admin_errors
    )


def test_low_prompt_repeats_until_limit_then_ignore_resets(harness_factory) -> None:
    harness = harness_factory()
    first_message_id = make_low_prompt(harness, "1")

    assert harness.states.load(harness.user.telegram_user_id).state is BotState.LOW_PROMPT
    assert harness.states.load(harness.user.telegram_user_id).notification_count == 1

    harness.clock.advance(harness.user.balance_check_interval_seconds)
    state = harness.fsm.balance_tick(harness.user)
    second_message_id = state.current_message_id

    assert state.state is BotState.LOW_PROMPT
    assert state.notification_count == 2
    assert harness.telegram.removed_buttons[-1] == (harness.user.telegram_user_id, first_message_id)

    harness.clock.advance(harness.user.balance_check_interval_seconds)
    state = harness.fsm.balance_tick(harness.user)
    third_message_id = state.current_message_id

    assert state.state is BotState.LOW_COOLDOWN
    assert state.notification_count == 3
    assert state.low_cooldown_until == harness.clock.now() + timedelta(
        seconds=harness.user.low_balance_notification_cooldown_seconds
    )
    assert harness.telegram.removed_buttons[-1] == (harness.user.telegram_user_id, second_message_id)

    state = harness.fsm.callback_ignore(harness.user, third_message_id)

    assert state.state is BotState.MONITORING
    assert state.notification_count == 0
    assert state.low_cooldown_until is None
    assert state.current_message_id is None
    assert harness.telegram.removed_buttons[-1] == (harness.user.telegram_user_id, third_message_id)


def test_latest_ignore_from_s2_resets_to_monitoring(harness_factory) -> None:
    harness = harness_factory()
    message_id = make_low_prompt(harness, "1")

    state = harness.fsm.callback_ignore(harness.user, message_id)

    assert state.state is BotState.MONITORING
    assert state.notification_count == 0
    assert state.low_cooldown_until is None
    assert state.current_message_id is None
    assert harness.telegram.removed_buttons[-1] == (harness.user.telegram_user_id, message_id)


def test_limit_greater_than_one_cooldown_expiry_restarts_cycle_in_s2(harness_factory) -> None:
    user = make_user(limit=2, cooldown=300)
    harness = harness_factory(user)
    cooldown_message_id = make_low_cooldown(harness)
    message_count = len(harness.telegram.messages)

    state = harness.states.load(user.telegram_user_id)
    assert state.state is BotState.LOW_COOLDOWN
    assert state.notification_count == 2

    harness.clock.advance(60)
    active_state = harness.fsm.balance_tick(user)

    assert active_state.state is BotState.LOW_COOLDOWN
    assert len(harness.telegram.messages) == message_count

    harness.clock.advance(300)
    restarted_state = harness.fsm.balance_tick(user)

    assert restarted_state.state is BotState.LOW_PROMPT
    assert restarted_state.notification_count == 1
    assert restarted_state.low_cooldown_until is None
    assert restarted_state.current_message_id != cooldown_message_id
    assert len(harness.telegram.messages) == message_count + 1
    assert harness.telegram.removed_buttons[-1] == (user.telegram_user_id, cooldown_message_id)


def test_balance_ok_from_prompt_or_cooldown_removes_buttons(harness_factory) -> None:
    prompt_harness = harness_factory()
    prompt_message_id = make_low_prompt(prompt_harness, "1")
    prompt_harness.balances.set_balance(prompt_harness.user.target_account, "10")

    prompt_state = prompt_harness.fsm.balance_tick(prompt_harness.user)

    assert prompt_state.state is BotState.MONITORING
    assert prompt_state.current_message_id is None
    assert prompt_state.notification_count == 0
    assert prompt_state.low_cooldown_until is None
    assert prompt_harness.telegram.removed_buttons[-1] == (
        prompt_harness.user.telegram_user_id,
        prompt_message_id,
    )

    cooldown_user = make_user(telegram_user_id=2002, limit=2)
    cooldown_harness = harness_factory(cooldown_user)
    cooldown_message_id = make_low_cooldown(cooldown_harness)
    cooldown_harness.balances.set_balance(cooldown_user.target_account, "10")

    cooldown_state = cooldown_harness.fsm.balance_tick(cooldown_user)

    assert cooldown_state.state is BotState.MONITORING
    assert cooldown_state.current_message_id is None
    assert cooldown_state.notification_count == 0
    assert cooldown_state.low_cooldown_until is None
    assert cooldown_harness.telegram.removed_buttons[-1] == (
        cooldown_user.telegram_user_id,
        cooldown_message_id,
    )


def test_top_up_success_uses_fresh_balance_and_creates_safe_tx(harness_factory) -> None:
    harness = harness_factory()
    message_id = make_low_prompt(harness, "1")
    harness.balances.set_balance(harness.user.target_account, "3")
    now = harness.clock.now()

    state = harness.fsm.callback_top_up(harness.user, message_id)

    assert state.state is BotState.SAFE_TX_PENDING
    assert state.pending_safe_tx_id == "safe-tx-1"
    assert state.current_message_id is None
    assert state.notification_count == 0
    assert state.low_cooldown_until is None
    assert state.tx_reminder_until == now + timedelta(
        seconds=harness.user.low_balance_notification_cooldown_seconds
    )
    assert harness.safe.created_txs[0].amount == Decimal("17")
    assert harness.safe.created_txs[0].safe_account == harness.user.safe_account
    assert harness.safe.created_txs[0].recipient == harness.user.target_account
    assert harness.safe.created_txs[0].private_key == "private-key"
    assert harness.private_keys.requests == [harness.user.safe_proposer_key_file]
    assert harness.telegram.messages[-1].kind == "safe_tx_created"
    assert harness.telegram.removed_buttons[-1] == (
        harness.user.telegram_user_id,
        message_id,
    )
    assert len(harness.telegram.admin_errors) == 1
    assert (
        harness.telegram.admin_errors[-1][1]
        == f"Tx created in safe {harness.user.safe_account} "
        f"to top up {harness.user.target_account}"
    )
    assert "1001" not in harness.telegram.admin_errors[-1][1]
    assert harness.user.balance_token_address not in harness.telegram.admin_errors[-1][1]
    assert "safe-tx-1" not in harness.telegram.admin_errors[-1][1]


def test_balance_tick_reads_configured_balance_token_address(harness_factory) -> None:
    user = make_user(
        telegram_user_id=2401,
        balance_token_address="0x9999999999999999999999999999999999999999",
    )
    harness = harness_factory(user)
    start_user(harness)
    harness.balances.set_balance(user.target_account, "50")
    harness.balances.set_balance(user.target_account, "1", user.balance_token_address)

    state = harness.fsm.balance_tick(user)

    assert state.state is BotState.LOW_PROMPT
    assert harness.balances.reads == [(user.target_account, user.balance_token_address)]


def test_top_up_uses_balance_token_for_balance_only_not_safe_tx_recipient(
    harness_factory,
) -> None:
    user = make_user(
        telegram_user_id=2402,
        balance_token_address="0x8888888888888888888888888888888888888888",
    )
    harness = harness_factory(user)
    start_user(harness)
    harness.balances.set_balance(user.target_account, "1", user.balance_token_address)
    prompt_state = harness.fsm.balance_tick(user)
    harness.balances.set_balance(user.target_account, "3", user.balance_token_address)

    state = harness.fsm.callback_top_up(user, prompt_state.current_message_id)

    assert state.state is BotState.SAFE_TX_PENDING
    assert harness.safe.created_txs[0].recipient == user.target_account
    assert harness.safe.created_txs[0].amount == Decimal("17")
    assert harness.balances.reads == [
        (user.target_account, user.balance_token_address),
        (user.target_account, user.balance_token_address),
    ]


def test_top_up_fresh_balance_read_failed_notifies_admin(harness_factory) -> None:
    harness = harness_factory()
    message_id = make_low_prompt(harness, "1")
    harness.balances.fail_accounts.add(harness.user.target_account)

    state = harness.fsm.callback_top_up(harness.user, message_id)

    assert state.state is BotState.MONITORING
    assert state.pending_safe_tx_id is None
    assert state.notification_count == 0
    assert state.low_cooldown_until is None
    assert state.current_message_id is None
    assert len(harness.safe.created_txs) == 0
    assert (
        harness.telegram.admin_errors[-1][1]
        == f"Fresh balance read failed for target account "
        f"{harness.user.target_account}: Could not read balance"
    )


def test_top_up_fresh_ok_or_non_positive_amount_skips_safe_tx(harness_factory) -> None:
    ok_harness = harness_factory()
    ok_message_id = make_low_prompt(ok_harness, "1")
    ok_harness.balances.set_balance(ok_harness.user.target_account, "10")

    ok_state = ok_harness.fsm.callback_top_up(ok_harness.user, ok_message_id)

    assert ok_state.state is BotState.MONITORING
    assert ok_state.notification_count == 0
    assert ok_state.low_cooldown_until is None
    assert ok_state.current_message_id is None
    assert len(ok_harness.safe.created_txs) == 0
    assert ok_harness.telegram.admin_errors == []

    weird_user = make_user(telegram_user_id=3003, threshold="10", max_balance="5")
    weird_harness = harness_factory(weird_user)
    weird_message_id = make_low_prompt(weird_harness, "1")
    weird_harness.balances.set_balance(weird_user.target_account, "6")

    weird_state = weird_harness.fsm.callback_top_up(weird_user, weird_message_id)

    assert weird_state.state is BotState.MONITORING
    assert weird_state.notification_count == 0
    assert weird_state.low_cooldown_until is None
    assert weird_state.current_message_id is None
    assert len(weird_harness.safe.created_txs) == 0
    assert weird_harness.telegram.admin_errors == []


def test_top_up_safe_create_failed_notifies_admin(harness_factory) -> None:
    harness = harness_factory()
    message_id = make_low_prompt(harness, "1")
    harness.safe.fail_create_for_users.add(harness.user.telegram_user_id)

    state = harness.fsm.callback_top_up(harness.user, message_id)

    assert state.state is BotState.MONITORING
    assert state.pending_safe_tx_id is None
    assert state.notification_count == 0
    assert state.low_cooldown_until is None
    assert state.current_message_id is None
    assert len(harness.safe.created_txs) == 0
    assert harness.private_keys.requests == [harness.user.safe_proposer_key_file]
    assert (
        harness.telegram.admin_errors[-1][1]
        == f"Safe tx creation failed for safe {harness.user.safe_account}: "
        "Could not create Safe tx"
    )
    assert not any(
        "Tx created in safe" in message
        for _, message in harness.telegram.admin_errors
    )


def test_top_up_safe_preflight_failed_notifies_admin_and_returns_to_monitoring(
    harness_factory,
) -> None:
    harness = harness_factory()
    harness.fsm._safe_wallet = PreflightFailingSafeWalletClient()
    message_id = make_low_prompt(harness, "1")

    state = harness.fsm.callback_top_up(harness.user, message_id)

    assert state.state is BotState.MONITORING
    assert state.pending_safe_tx_id is None
    assert state.current_message_id is None
    assert "AAVE preflight balance check failed" in harness.telegram.admin_errors[-1][1]


def test_top_up_from_s3_uses_same_safe_tx_path_as_s2(harness_factory) -> None:
    user = make_user(limit=2)
    harness = harness_factory(user)
    cooldown_message_id = make_low_cooldown(harness)
    harness.balances.set_balance(user.target_account, "4")

    state = harness.fsm.callback_top_up(user, cooldown_message_id)

    assert state.state is BotState.SAFE_TX_PENDING
    assert state.pending_safe_tx_id == "safe-tx-1"
    assert state.notification_count == 0
    assert state.low_cooldown_until is None
    assert state.current_message_id is None
    assert harness.safe.created_txs[0].amount == Decimal("16")


def test_balance_read_failed_in_s2_preserves_prompt_context(harness_factory) -> None:
    harness = harness_factory()
    message_id = make_low_prompt(harness, "1")
    state_before = harness.states.load(harness.user.telegram_user_id)
    harness.balances.fail_accounts.add(harness.user.target_account)

    state = harness.fsm.balance_tick(harness.user)

    assert state.state is BotState.LOW_PROMPT
    assert state.current_message_id == message_id
    assert state.notification_count == state_before.notification_count
    assert state.low_cooldown_until is None
    assert harness.telegram.removed_buttons == []
    assert "Balance read failed" in harness.telegram.admin_errors[-1][1]


def test_balance_read_failed_in_s3_preserves_cooldown_context(harness_factory) -> None:
    user = make_user(limit=2)
    harness = harness_factory(user)
    message_id = make_low_cooldown(harness)
    state_before = harness.states.load(user.telegram_user_id)
    harness.balances.fail_accounts.add(user.target_account)

    state = harness.fsm.balance_tick(user)

    assert state.state is BotState.LOW_COOLDOWN
    assert state.current_message_id == message_id
    assert state.notification_count == state_before.notification_count
    assert state.low_cooldown_until == state_before.low_cooldown_until
    assert harness.telegram.removed_buttons[-1] != (user.telegram_user_id, message_id)
    assert "Balance read failed" in harness.telegram.admin_errors[-1][1]


def test_stale_callbacks_and_irrelevant_events_are_noops(harness_factory) -> None:
    harness = harness_factory()
    message_id = make_low_prompt(harness, "1")

    state = harness.fsm.callback_ignore(harness.user, message_id + 99)

    assert state.state is BotState.LOW_PROMPT
    assert state.current_message_id == message_id
    assert harness.telegram.removed_buttons == []

    top_up_state = harness.fsm.callback_top_up(harness.user, message_id + 99)

    assert top_up_state.state is BotState.LOW_PROMPT
    assert top_up_state.current_message_id == message_id
    assert len(harness.safe.created_txs) == 0
    assert harness.telegram.removed_buttons == []

    ignored = harness.fsm.ignore_event(harness.user)

    assert ignored.state is BotState.LOW_PROMPT
    assert ignored.current_message_id == message_id


def test_stale_callbacks_in_s3_are_noops(harness_factory) -> None:
    user = make_user(limit=2)
    harness = harness_factory(user)
    message_id = make_low_cooldown(harness)
    state_before = harness.states.load(user.telegram_user_id).to_dict()
    message_count = len(harness.telegram.messages)
    removed_count = len(harness.telegram.removed_buttons)
    safe_tx_count = len(harness.safe.created_txs)

    ignored_state = harness.fsm.callback_ignore(user, message_id + 99)

    assert ignored_state.to_dict() == state_before
    assert len(harness.telegram.messages) == message_count
    assert len(harness.telegram.removed_buttons) == removed_count
    assert len(harness.safe.created_txs) == safe_tx_count

    top_up_state = harness.fsm.callback_top_up(user, message_id + 99)

    assert top_up_state.to_dict() == state_before
    assert len(harness.telegram.messages) == message_count
    assert len(harness.telegram.removed_buttons) == removed_count
    assert len(harness.safe.created_txs) == safe_tx_count


def test_ignore_event_and_repeated_start_are_noops_in_s3_and_s4(harness_factory) -> None:
    s3_user = make_user(telegram_user_id=7003, limit=2)
    s3_harness = harness_factory(s3_user)
    make_low_cooldown(s3_harness)
    s3_before = s3_harness.states.load(s3_user.telegram_user_id).to_dict()

    assert s3_harness.fsm.ignore_event(s3_user).to_dict() == s3_before
    assert s3_harness.fsm.start(s3_user).to_dict() == s3_before
    assert s3_harness.states.load(s3_user.telegram_user_id).to_dict() == s3_before

    s4_user = make_user(telegram_user_id=7004)
    s4_harness = harness_factory(s4_user)
    make_pending_safe_tx(s4_harness)
    s4_before = s4_harness.states.load(s4_user.telegram_user_id).to_dict()

    assert s4_harness.fsm.ignore_event(s4_user).to_dict() == s4_before
    assert s4_harness.fsm.start(s4_user).to_dict() == s4_before
    assert s4_harness.states.load(s4_user.telegram_user_id).to_dict() == s4_before


def test_safe_tx_pending_suppresses_until_cooldown_then_sends_reminder(harness_factory) -> None:
    harness = harness_factory()
    tx_id = make_pending_safe_tx(harness)
    created_message_count = len(harness.telegram.messages)

    harness.clock.advance(60)
    harness.balances.set_balance(harness.user.target_account, "1")
    state = harness.fsm.balance_tick(harness.user)

    assert state.state is BotState.SAFE_TX_PENDING
    assert state.pending_safe_tx_id == tx_id
    assert state.current_message_id is None
    assert len(harness.telegram.messages) == created_message_count

    harness.clock.advance(300)
    handled_at = harness.clock.now()
    state = harness.fsm.balance_tick(harness.user)

    assert state.state is BotState.SAFE_TX_PENDING
    assert state.pending_safe_tx_id == tx_id
    assert state.current_message_id is None
    assert state.tx_reminder_until == handled_at + timedelta(
        seconds=harness.user.low_balance_notification_cooldown_seconds
    )
    assert harness.telegram.messages[-1].kind == "safe_tx_pending_prompt"
    assert harness.telegram.messages[-1].buttons is False
    assert len(harness.safe.created_txs) == 1


def test_callbacks_after_safe_tx_creation_are_stale_noops(harness_factory) -> None:
    harness = harness_factory()
    low_message_id = make_low_prompt(harness, "1")
    harness.balances.set_balance(harness.user.target_account, "2")
    created_state = harness.fsm.callback_top_up(harness.user, low_message_id)
    tx_id = created_state.pending_safe_tx_id
    message_count = len(harness.telegram.messages)
    removed_count = len(harness.telegram.removed_buttons)
    safe_tx_count = len(harness.safe.created_txs)

    state = harness.fsm.callback_top_up(harness.user, low_message_id)

    assert state.state is BotState.SAFE_TX_PENDING
    assert state.pending_safe_tx_id == tx_id
    assert state.current_message_id is None
    assert len(harness.safe.created_txs) == safe_tx_count
    assert len(harness.telegram.messages) == message_count
    assert len(harness.telegram.removed_buttons) == removed_count

    ignored_state = harness.fsm.callback_ignore(harness.user, low_message_id)

    assert ignored_state.state is BotState.SAFE_TX_PENDING
    assert ignored_state.pending_safe_tx_id == tx_id
    assert ignored_state.current_message_id is None
    assert len(harness.safe.created_txs) == safe_tx_count
    assert len(harness.telegram.messages) == message_count
    assert len(harness.telegram.removed_buttons) == removed_count
    assert all(message.kind != "existing_safe_tx_notice" for message in harness.telegram.messages)


def test_safe_tx_pending_reminder_repeats_without_buttons_or_message_id(harness_factory) -> None:
    harness = harness_factory()
    tx_id = send_due_safe_tx_reminder(harness)
    message_count = len(harness.telegram.messages)
    removed_count = len(harness.telegram.removed_buttons)
    state_before = harness.states.load(harness.user.telegram_user_id)

    harness.clock.advance(60)
    state = harness.fsm.balance_tick(harness.user)

    assert state.state is BotState.SAFE_TX_PENDING
    assert state.pending_safe_tx_id == tx_id
    assert state.current_message_id is None
    assert state.tx_reminder_until == state_before.tx_reminder_until
    assert len(harness.telegram.messages) == message_count
    assert len(harness.telegram.removed_buttons) == removed_count

    harness.clock.advance(300)
    handled_at = harness.clock.now()
    state = harness.fsm.balance_tick(harness.user)

    assert state.state is BotState.SAFE_TX_PENDING
    assert state.pending_safe_tx_id == tx_id
    assert state.current_message_id is None
    assert state.tx_reminder_until == handled_at + timedelta(
        seconds=harness.user.low_balance_notification_cooldown_seconds
    )
    assert len(harness.telegram.messages) == message_count + 1
    assert harness.telegram.messages[-1].kind == "safe_tx_pending_prompt"
    assert harness.telegram.messages[-1].buttons is False
    assert len(harness.telegram.removed_buttons) == removed_count


def test_safe_tx_final_or_balance_ok_clears_pending(harness_factory) -> None:
    final_harness = harness_factory()
    tx_id = make_pending_safe_tx(final_harness)
    final_harness.safe.statuses[tx_id] = SafeTxStatus.FINAL
    final_harness.balances.set_balance(final_harness.user.target_account, "1")

    final_state = final_harness.fsm.balance_tick(final_harness.user)

    assert final_state.state is BotState.MONITORING
    assert final_state.pending_safe_tx_id is None
    assert final_state.tx_reminder_until is None
    assert final_state.current_message_id is None
    assert final_state.notification_count == 0
    assert final_state.low_cooldown_until is None

    ok_harness = harness_factory(make_user(telegram_user_id=4004))
    send_due_safe_tx_reminder(ok_harness)
    removed_count = len(ok_harness.telegram.removed_buttons)
    ok_harness.balances.set_balance(ok_harness.user.target_account, "10")

    ok_state = ok_harness.fsm.balance_tick(ok_harness.user)

    assert ok_state.state is BotState.MONITORING
    assert ok_state.pending_safe_tx_id is None
    assert ok_state.tx_reminder_until is None
    assert ok_state.current_message_id is None
    assert ok_state.notification_count == 0
    assert ok_state.low_cooldown_until is None
    assert len(ok_harness.telegram.removed_buttons) == removed_count


def test_read_failed_with_pending_keeps_state_but_final_clears_even_on_read_fail(harness_factory) -> None:
    pending_harness = harness_factory()
    make_pending_safe_tx(pending_harness)
    state_before = pending_harness.states.load(pending_harness.user.telegram_user_id)
    message_count = len(pending_harness.telegram.messages)
    removed_count = len(pending_harness.telegram.removed_buttons)
    handled_at = pending_harness.clock.now()
    pending_harness.balances.fail_accounts.add(pending_harness.user.target_account)

    pending_state = pending_harness.fsm.balance_tick(pending_harness.user)

    assert pending_state.state is BotState.SAFE_TX_PENDING
    assert pending_state.pending_safe_tx_id == "safe-tx-1"
    assert pending_state.current_message_id is None
    assert pending_state.tx_reminder_until == state_before.tx_reminder_until
    assert_tick_scheduled(pending_state, pending_harness, handled_at)
    assert len(pending_harness.telegram.messages) == message_count
    assert len(pending_harness.telegram.removed_buttons) == removed_count
    assert "Balance read failed" in pending_harness.telegram.admin_errors[-1][1]

    final_harness = harness_factory(make_user(telegram_user_id=5005))
    tx_id = send_due_safe_tx_reminder(final_harness)
    removed_count = len(final_harness.telegram.removed_buttons)
    final_harness.safe.statuses[tx_id] = SafeTxStatus.FINAL
    final_harness.balances.fail_accounts.add(final_harness.user.target_account)

    final_state = final_harness.fsm.balance_tick(final_harness.user)

    assert final_state.state is BotState.MONITORING
    assert final_state.pending_safe_tx_id is None
    assert final_state.tx_reminder_until is None
    assert final_state.current_message_id is None
    assert final_state.notification_count == 0
    assert final_state.low_cooldown_until is None
    assert len(final_harness.telegram.removed_buttons) == removed_count


def test_balance_and_safe_status_read_failures_in_s4_keep_pending_without_user_output(
    harness_factory,
) -> None:
    harness = harness_factory()
    tx_id = make_pending_safe_tx(harness)
    state_before = harness.states.load(harness.user.telegram_user_id)
    harness.clock.advance(harness.user.low_balance_notification_cooldown_seconds)
    handled_at = harness.clock.now()
    harness.safe.fail_status_for_txs.add(tx_id)
    harness.balances.fail_accounts.add(harness.user.target_account)
    message_count = len(harness.telegram.messages)
    removed_count = len(harness.telegram.removed_buttons)
    admin_count = len(harness.telegram.admin_errors)

    state = harness.fsm.balance_tick(harness.user)

    assert state.state is BotState.SAFE_TX_PENDING
    assert state.pending_safe_tx_id == tx_id
    assert state.current_message_id is None
    assert state.tx_reminder_until == state_before.tx_reminder_until
    assert_tick_scheduled(state, harness, handled_at)
    assert len(harness.telegram.messages) == message_count
    assert len(harness.telegram.removed_buttons) == removed_count
    assert len(harness.telegram.admin_errors) == admin_count + 2
    assert any("Safe tx status read failed" in error for _, error in harness.telegram.admin_errors)
    assert any("Balance read failed" in error for _, error in harness.telegram.admin_errors)


def test_balance_read_failed_in_s4_after_cooldown_sends_buttonless_pending_reminder(
    harness_factory,
) -> None:
    harness = harness_factory()
    tx_id = make_pending_safe_tx(harness)
    harness.clock.advance(harness.user.low_balance_notification_cooldown_seconds)
    handled_at = harness.clock.now()
    harness.balances.fail_accounts.add(harness.user.target_account)
    message_count = len(harness.telegram.messages)
    removed_count = len(harness.telegram.removed_buttons)

    state = harness.fsm.balance_tick(harness.user)

    assert state.state is BotState.SAFE_TX_PENDING
    assert state.pending_safe_tx_id == tx_id
    assert state.current_message_id is None
    assert state.tx_reminder_until == handled_at + timedelta(
        seconds=harness.user.low_balance_notification_cooldown_seconds
    )
    assert len(harness.telegram.messages) == message_count + 1
    assert harness.telegram.messages[-1].kind == "safe_tx_pending_prompt"
    assert harness.telegram.messages[-1].buttons is False
    assert len(harness.telegram.removed_buttons) == removed_count
    assert "Balance read failed" in harness.telegram.admin_errors[-1][1]


def test_safe_status_read_failed_in_s4_keeps_pending_and_schedules_next_tick(harness_factory) -> None:
    harness = harness_factory()
    tx_id = make_pending_safe_tx(harness)
    state_before = harness.states.load(harness.user.telegram_user_id)
    harness.safe.fail_status_for_txs.add(tx_id)
    harness.clock.advance(harness.user.low_balance_notification_cooldown_seconds)
    handled_at = harness.clock.now()
    harness.balances.set_balance(harness.user.target_account, "1")
    message_count = len(harness.telegram.messages)

    state = harness.fsm.balance_tick(harness.user)

    assert state.state is BotState.SAFE_TX_PENDING
    assert state.pending_safe_tx_id == tx_id
    assert state.current_message_id is None
    assert state.tx_reminder_until == state_before.tx_reminder_until
    assert state.next_tick_at == handled_at + timedelta(
        seconds=harness.user.balance_check_interval_seconds
    )
    assert len(harness.telegram.messages) == message_count
    assert (
        harness.telegram.admin_errors[-1][1]
        == f"Safe tx status read failed for safe {harness.user.safe_account}: "
        "Could not read Safe tx status"
    )
    assert str(harness.user.telegram_user_id) not in harness.telegram.admin_errors[-1][1]
    assert tx_id not in harness.telegram.admin_errors[-1][1]


def test_safe_status_read_failed_after_pending_reminder_keeps_context(harness_factory) -> None:
    harness = harness_factory()
    tx_id = send_due_safe_tx_reminder(harness)
    state_before = harness.states.load(harness.user.telegram_user_id)
    harness.safe.fail_status_for_txs.add(tx_id)
    harness.clock.advance(harness.user.low_balance_notification_cooldown_seconds)
    handled_at = harness.clock.now()
    harness.balances.set_balance(harness.user.target_account, "1")
    message_count = len(harness.telegram.messages)
    removed_count = len(harness.telegram.removed_buttons)

    state = harness.fsm.balance_tick(harness.user)

    assert state.state is BotState.SAFE_TX_PENDING
    assert state.pending_safe_tx_id == tx_id
    assert state.current_message_id is None
    assert state.tx_reminder_until == state_before.tx_reminder_until
    assert state.next_tick_at == handled_at + timedelta(
        seconds=harness.user.balance_check_interval_seconds
    )
    assert len(harness.telegram.messages) == message_count
    assert len(harness.telegram.removed_buttons) == removed_count
    assert "Safe tx status read failed" in harness.telegram.admin_errors[-1][1]


def test_safe_status_read_failed_still_clears_pending_when_balance_is_ok(harness_factory) -> None:
    harness = harness_factory()
    tx_id = make_pending_safe_tx(harness)
    harness.safe.fail_status_for_txs.add(tx_id)
    harness.balances.set_balance(harness.user.target_account, "10")

    state = harness.fsm.balance_tick(harness.user)

    assert state.state is BotState.MONITORING
    assert state.pending_safe_tx_id is None
    assert state.tx_reminder_until is None
    assert state.current_message_id is None
    assert "Safe tx status read failed" in harness.telegram.admin_errors[-1][1]


def test_balance_tick_schedules_next_tick_on_main_transitions(harness_factory) -> None:
    s1_to_s2 = harness_factory(make_user(telegram_user_id=7101))
    start_user(s1_to_s2)
    s1_to_s2.balances.set_balance(s1_to_s2.user.target_account, "1")
    handled_at = s1_to_s2.clock.now()
    state = s1_to_s2.fsm.balance_tick(s1_to_s2.user)
    assert state.state is BotState.LOW_PROMPT
    assert_tick_scheduled(state, s1_to_s2, handled_at)

    s2_to_s2 = harness_factory(make_user(telegram_user_id=7102, limit=3))
    make_low_prompt(s2_to_s2, "1")
    s2_to_s2.clock.advance(s2_to_s2.user.balance_check_interval_seconds)
    handled_at = s2_to_s2.clock.now()
    state = s2_to_s2.fsm.balance_tick(s2_to_s2.user)
    assert state.state is BotState.LOW_PROMPT
    assert state.notification_count == 2
    assert_tick_scheduled(state, s2_to_s2, handled_at)

    s2_to_s3 = harness_factory(make_user(telegram_user_id=7103, limit=2))
    make_low_prompt(s2_to_s3, "1")
    s2_to_s3.clock.advance(s2_to_s3.user.balance_check_interval_seconds)
    handled_at = s2_to_s3.clock.now()
    state = s2_to_s3.fsm.balance_tick(s2_to_s3.user)
    assert state.state is BotState.LOW_COOLDOWN
    assert_tick_scheduled(state, s2_to_s3, handled_at)

    s3_to_s2 = harness_factory(make_user(telegram_user_id=7104, limit=2, cooldown=300))
    make_low_cooldown(s3_to_s2)
    s3_to_s2.clock.advance(s3_to_s2.user.low_balance_notification_cooldown_seconds)
    handled_at = s3_to_s2.clock.now()
    state = s3_to_s2.fsm.balance_tick(s3_to_s2.user)
    assert state.state is BotState.LOW_PROMPT
    assert_tick_scheduled(state, s3_to_s2, handled_at)

    s3_to_s3 = harness_factory(make_user(telegram_user_id=7105, limit=2, cooldown=300))
    make_low_cooldown(s3_to_s3)
    s3_to_s3.clock.advance(s3_to_s3.user.balance_check_interval_seconds)
    handled_at = s3_to_s3.clock.now()
    state = s3_to_s3.fsm.balance_tick(s3_to_s3.user)
    assert state.state is BotState.LOW_COOLDOWN
    assert_tick_scheduled(state, s3_to_s3, handled_at)

    s4_to_s1 = harness_factory(make_user(telegram_user_id=7106))
    tx_id = make_pending_safe_tx(s4_to_s1)
    s4_to_s1.safe.statuses[tx_id] = SafeTxStatus.FINAL
    s4_to_s1.balances.set_balance(s4_to_s1.user.target_account, "1")
    handled_at = s4_to_s1.clock.now()
    state = s4_to_s1.fsm.balance_tick(s4_to_s1.user)
    assert state.state is BotState.MONITORING
    assert_tick_scheduled(state, s4_to_s1, handled_at)


def test_user_blocked_and_send_403_reset_to_not_started(harness_factory) -> None:
    blocked_harness = harness_factory()
    make_low_prompt(blocked_harness, "1")

    blocked_state = blocked_harness.fsm.user_blocked(blocked_harness.user)

    assert blocked_state.state is BotState.NOT_STARTED
    assert blocked_state.current_message_id is None
    assert blocked_state.next_tick_at is None

    forbidden_harness = harness_factory(make_user(telegram_user_id=6006))
    start_user(forbidden_harness)
    forbidden_harness.balances.set_balance(forbidden_harness.user.target_account, "1")
    forbidden_harness.telegram.forbidden_user_ids.add(forbidden_harness.user.telegram_user_id)

    forbidden_state = forbidden_harness.fsm.balance_tick(forbidden_harness.user)

    assert forbidden_state.state is BotState.NOT_STARTED
    assert forbidden_state.current_message_id is None
    assert len(forbidden_harness.telegram.messages) == 0


def test_send_403_from_remove_buttons_resets_to_not_started(harness_factory) -> None:
    harness = harness_factory()
    message_id = make_low_prompt(harness, "1")
    harness.telegram.forbid_operation(harness.user.telegram_user_id, "remove_buttons")

    state = harness.fsm.callback_ignore(harness.user, message_id)

    assert state.state is BotState.NOT_STARTED
    assert state.current_message_id is None
    assert state.pending_safe_tx_id is None
    assert state.next_tick_at is None


def test_send_403_after_replacing_low_prompt_resets_to_not_started(harness_factory) -> None:
    harness = harness_factory()
    message_id = make_low_prompt(harness, "1")
    message_count = len(harness.telegram.messages)
    harness.clock.advance(harness.user.balance_check_interval_seconds)
    harness.telegram.forbid_operation(harness.user.telegram_user_id, "send_low_balance_prompt")

    state = harness.fsm.balance_tick(harness.user)

    assert state.state is BotState.NOT_STARTED
    assert state.current_message_id is None
    assert state.pending_safe_tx_id is None
    assert state.next_tick_at is None
    assert len(harness.telegram.messages) == message_count
    assert harness.telegram.removed_buttons[-1] == (harness.user.telegram_user_id, message_id)


def test_concurrent_top_up_callbacks_for_one_user_create_one_safe_tx(tmp_path) -> None:
    user = make_user()
    states = JsonStateRepository(tmp_path / "states")
    telegram = MockTelegramGateway()
    balances = MockBalanceProvider()
    delegate_safe = MockSafeWalletClient()
    safe = BlockingSafeWalletClient(delegate_safe)
    private_keys = MockPrivateKeyProvider({user.safe_proposer_key_file: "private-key"})
    clock = MockClock()
    fsm = FsmService(
        state_repository=states,
        telegram=telegram,
        balances=balances,
        safe_wallet=safe,
        private_keys=private_keys,
        clock=clock,
        admin_telegram_user_id=9001,
    )
    fsm.start(user)
    balances.set_balance(user.target_account, "1")
    message_id = fsm.balance_tick(user).current_message_id
    balances.set_balance(user.target_account, "2")
    results = []
    errors = []

    def top_up() -> None:
        try:
            results.append(fsm.callback_top_up(user, message_id))
        except Exception as error:  # pragma: no cover - surfaced by assertion below
            errors.append(error)

    first = threading.Thread(target=top_up)
    second = threading.Thread(target=top_up)

    first.start()
    assert safe.entered_create.wait(timeout=2)
    second.start()
    time.sleep(0.05)
    safe.release_create.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert safe.create_attempts == 1
    assert len(delegate_safe.created_txs) == 1
    assert len(results) == 2
    assert states.load(user.telegram_user_id).state is BotState.SAFE_TX_PENDING


class BlockingSafeWalletClient:
    def __init__(self, delegate: MockSafeWalletClient) -> None:
        self.delegate = delegate
        self.entered_create = threading.Event()
        self.release_create = threading.Event()
        self.create_attempts = 0

    def create_top_up_tx(self, user, amount, safe_proposer_private_key):
        self.create_attempts += 1
        if self.create_attempts == 1:
            self.entered_create.set()
            assert self.release_create.wait(timeout=2)
        return self.delegate.create_top_up_tx(user, amount, safe_proposer_private_key)

    def get_tx_status(self, user, safe_tx_id):
        return self.delegate.get_tx_status(user, safe_tx_id)


class PreflightFailingSafeWalletClient:
    def create_top_up_tx(self, user, amount, safe_proposer_private_key):
        del user, amount, safe_proposer_private_key
        raise SafeTxCreateError("AAVE preflight balance check failed")

    def get_tx_status(self, user, safe_tx_id):
        del user, safe_tx_id
        return SafeTxStatus.PENDING


def test_send_403_from_safe_tx_created_resets_to_not_started(harness_factory) -> None:
    harness = harness_factory()
    message_id = make_low_prompt(harness, "1")
    harness.balances.set_balance(harness.user.target_account, "2")
    harness.telegram.forbid_operation(harness.user.telegram_user_id, "send_safe_tx_created")

    state = harness.fsm.callback_top_up(harness.user, message_id)

    assert state.state is BotState.NOT_STARTED
    assert state.current_message_id is None
    assert state.pending_safe_tx_id is None
    assert state.next_tick_at is None
    assert len(harness.safe.created_txs) == 1
    assert len(harness.telegram.admin_errors) == 1
    assert (
        harness.telegram.admin_errors[-1][1]
        == f"Tx created in safe {harness.user.safe_account} "
        f"to top up {harness.user.target_account}"
    )
    assert "safe-tx-1" not in harness.telegram.admin_errors[-1][1]


def test_send_403_from_safe_tx_pending_prompt_resets_to_not_started(harness_factory) -> None:
    harness = harness_factory()
    make_pending_safe_tx(harness)
    harness.clock.advance(harness.user.low_balance_notification_cooldown_seconds)
    harness.balances.set_balance(harness.user.target_account, "1")
    harness.telegram.forbid_operation(
        harness.user.telegram_user_id,
        "send_safe_tx_pending_prompt",
    )

    state = harness.fsm.balance_tick(harness.user)

    assert state.state is BotState.NOT_STARTED
    assert state.current_message_id is None
    assert state.pending_safe_tx_id is None
    assert state.next_tick_at is None


def test_callback_on_safe_tx_pending_reminder_is_stale_even_if_existing_notice_would_403(
    harness_factory,
) -> None:
    harness = harness_factory()
    tx_id = send_due_safe_tx_reminder(harness)
    reminder_message_id = harness.telegram.messages[-1].message_id
    message_count = len(harness.telegram.messages)
    removed_count = len(harness.telegram.removed_buttons)
    harness.telegram.forbid_operation(
        harness.user.telegram_user_id,
        "send_existing_safe_tx_notice",
    )

    state = harness.fsm.callback_top_up(harness.user, reminder_message_id)

    assert state.state is BotState.SAFE_TX_PENDING
    assert state.pending_safe_tx_id == tx_id
    assert state.current_message_id is None
    assert state.next_tick_at is not None
    assert len(harness.telegram.messages) == message_count
    assert len(harness.telegram.removed_buttons) == removed_count
    assert all(message.kind != "existing_safe_tx_notice" for message in harness.telegram.messages)
