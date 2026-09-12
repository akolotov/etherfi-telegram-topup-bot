from decimal import Decimal

import pytest

from etherfi_bot.domain import (
    BotState,
    ManualTopUpConfig,
    ManualTopUpError,
    TelegramForbiddenError,
)
from tests.conftest import make_user


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


@pytest.mark.parametrize("amount", ["0", "-1", "5000.000001", "5001", "NaN"])
def test_invalid_manual_amounts_are_rejected(harness_factory, amount: str) -> None:
    harness = harness_factory(configured_user())
    harness.fsm.start(harness.user)
    harness.safe_balances.set_balance(harness.user.safe_account, "10000")
    with pytest.raises(ManualTopUpError):
        harness.fsm.prepare_manual_top_up(harness.user, Decimal(amount))
