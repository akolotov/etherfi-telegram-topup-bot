from datetime import timedelta
from decimal import Decimal

import pytest

from etherfi_bot.domain import (
    BotState,
    ManualTopUpConfig,
    ManualTopUpError,
    TelegramForbiddenError,
    UserState,
)
from tests.conftest import make_dispatcher, make_user


def configured_user():
    return make_user(
        manual_top_up=ManualTopUpConfig(
            preset_amounts=(Decimal("250"), Decimal("500"), Decimal("1000")),
            max_custom_amount=Decimal("5000"),
        )
    )


def test_context_reads_fresh_target_and_safe_balances(harness_factory) -> None:
    harness = harness_factory(configured_user())
    harness.fsm.start(harness.user)
    harness.balances.set_balance(harness.user.target_account, "312.45")
    harness.safe_balances.set_balance(harness.user.safe_account, "4200.25")

    context = harness.fsm.manual_top_up_context(harness.user)

    assert context.target_balance == Decimal("312.45")
    assert context.safe_balance == Decimal("4200.25")
    assert context.maximum_amount == Decimal("4200.25")
    assert context.preset_amounts == (Decimal("250"), Decimal("500"), Decimal("1000"))


def test_custom_amount_is_confirmed_in_chat_then_created_exactly(harness_factory) -> None:
    harness = harness_factory(configured_user())
    harness.fsm.start(harness.user)
    harness.safe_balances.set_balance(harness.user.safe_account, "4000")

    prepared = harness.fsm.prepare_manual_top_up(harness.user, Decimal("1250.50"))
    assert prepared.state is BotState.MONITORING
    assert prepared.manual_top_up_message_id is not None
    assert harness.safe.created_txs == []

    confirmed = harness.fsm.callback_manual_top_up_confirm(
        harness.user,
        prepared.manual_top_up_message_id,
        prepared.manual_top_up_request_id,
    )

    assert confirmed.state is BotState.SAFE_TX_PENDING
    assert harness.safe.created_txs[0].amount == Decimal("1250.50")
    assert confirmed.manual_top_up_request_id is None


def test_confirm_rereads_safe_balance_and_refuses_if_it_dropped(harness_factory) -> None:
    harness = harness_factory(configured_user())
    harness.fsm.start(harness.user)
    harness.safe_balances.set_balance(harness.user.safe_account, "1000")
    prepared = harness.fsm.prepare_manual_top_up(harness.user, Decimal("900"))
    harness.safe_balances.set_balance(harness.user.safe_account, "800")

    state = harness.fsm.callback_manual_top_up_confirm(
        harness.user,
        prepared.manual_top_up_message_id,
        prepared.manual_top_up_request_id,
    )

    assert state.state is BotState.MONITORING
    assert harness.safe.created_txs == []
    assert harness.telegram.messages[-1].kind == "insufficient_safe_balance"


def test_prepare_safe_balance_failure_preserves_low_prompt(harness_factory) -> None:
    harness = harness_factory(configured_user())
    harness.fsm.start(harness.user)
    harness.balances.set_balance(harness.user.target_account, "0")
    low_state = harness.fsm.balance_tick(harness.user)
    low_message_id = low_state.current_message_id
    harness.safe_balances.fail_accounts.add(harness.user.safe_account)

    with pytest.raises(ManualTopUpError, match="Could not refresh the Safe balance"):
        harness.fsm.prepare_manual_top_up(harness.user, Decimal("500"))

    persisted = harness.states.load(harness.user.telegram_user_id)
    assert persisted.state is BotState.LOW_PROMPT
    assert persisted.current_message_id == low_message_id
    assert persisted.notification_count == 1
    assert harness.telegram.removed_buttons == []


def test_created_safe_tx_is_persisted_if_telegram_notification_fails(
    harness_factory,
) -> None:
    harness = harness_factory(configured_user())
    harness.fsm.start(harness.user)
    harness.safe_balances.set_balance(harness.user.safe_account, "1000")
    prepared = harness.fsm.prepare_manual_top_up(harness.user, Decimal("500"))
    harness.telegram.forbid_operation(
        harness.user.telegram_user_id, "send_safe_tx_created"
    )

    with pytest.raises(TelegramForbiddenError):
        harness.fsm.callback_manual_top_up_confirm(
            harness.user,
            prepared.manual_top_up_message_id,
            prepared.manual_top_up_request_id,
        )

    persisted = harness.states.load(harness.user.telegram_user_id)
    assert persisted.state is BotState.SAFE_TX_PENDING
    assert persisted.pending_safe_tx_id == harness.safe.created_txs[0].safe_tx_id
    assert persisted.manual_top_up_request_id is None


def test_expired_confirmation_is_inert(harness_factory) -> None:
    harness = harness_factory(configured_user())
    harness.fsm.start(harness.user)
    harness.safe_balances.set_balance(harness.user.safe_account, "1000")
    prepared = harness.fsm.prepare_manual_top_up(harness.user, Decimal("500"))
    harness.clock.advance(601)

    state = harness.fsm.callback_manual_top_up_confirm(
        harness.user,
        prepared.manual_top_up_message_id,
        prepared.manual_top_up_request_id,
    )

    assert state.manual_top_up_request_id is None
    assert harness.safe.created_txs == []


def test_expired_confirmation_cleanup_failure_does_not_block_monitoring(
    harness_factory,
) -> None:
    harness = harness_factory(configured_user())
    harness.fsm.start(harness.user)
    harness.safe_balances.set_balance(harness.user.safe_account, "1000")
    harness.fsm.prepare_manual_top_up(harness.user, Decimal("500"))
    harness.clock.advance(601)

    async def fail_to_remove_buttons(_telegram_user_id, _message_id) -> None:
        raise RuntimeError("message is already gone")

    harness.telegram.remove_buttons = fail_to_remove_buttons

    state = harness.fsm.balance_tick(harness.user)

    assert state.state is BotState.LOW_PROMPT
    assert state.manual_top_up_request_id is None
    assert harness.states.load(harness.user.telegram_user_id).manual_top_up_request_id is None


def test_recovery_continues_when_top_up_menu_configuration_fails(tmp_path) -> None:
    user = configured_user()
    dispatcher, states, telegram, *_ = make_dispatcher(tmp_path, [user])
    telegram.forbid_operation(user.telegram_user_id, "configure_top_up_menu")

    recovered_user_ids = dispatcher.recover_missing_user_states()

    assert recovered_user_ids == [user.telegram_user_id]
    assert states.load(user.telegram_user_id).state is BotState.MONITORING


def test_recovery_resets_menu_when_manual_top_up_is_disabled(tmp_path) -> None:
    user = make_user()
    dispatcher, states, telegram, *_ = make_dispatcher(tmp_path, [user])
    states.save(
        UserState(
            telegram_user_id=user.telegram_user_id,
            state=BotState.MONITORING,
        )
    )

    dispatcher.recover_missing_user_states()

    assert telegram.reset_top_up_menus == [user.telegram_user_id]


def test_startup_recovery_discards_manual_top_up_confirmation(tmp_path) -> None:
    user = configured_user()
    dispatcher, states, telegram, _balances, safe, _private_keys, clock = (
        make_dispatcher(tmp_path, [user])
    )
    states.save(
        UserState(
            telegram_user_id=user.telegram_user_id,
            state=BotState.MONITORING,
            manual_top_up_request_id="request-before-restart",
            manual_top_up_amount=Decimal("500"),
            manual_top_up_expires_at=clock.now() + timedelta(minutes=10),
            manual_top_up_message_id=42,
        )
    )

    dispatcher.recover_missing_user_states()
    state = dispatcher.callback_manual_top_up_confirm(
        user.telegram_user_id,
        42,
        "request-before-restart",
    )

    assert state is not None
    assert state.manual_top_up_request_id is None
    assert state.manual_top_up_amount is None
    assert state.manual_top_up_message_id is None
    assert telegram.removed_buttons == [(user.telegram_user_id, 42)]
    assert safe.created_txs == []


@pytest.mark.parametrize("amount", ["0", "-1", "5000.000001", "5001", "NaN"])
def test_invalid_manual_amounts_are_rejected(harness_factory, amount: str) -> None:
    harness = harness_factory(configured_user())
    harness.fsm.start(harness.user)
    harness.safe_balances.set_balance(harness.user.safe_account, "10000")
    with pytest.raises(ManualTopUpError):
        harness.fsm.prepare_manual_top_up(harness.user, Decimal(amount))
