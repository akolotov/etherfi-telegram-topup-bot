from __future__ import annotations

import logging

from etherfi_bot.domain import SafeTxStatus
from etherfi_bot.runtime import resolve_log_level
from etherfi_bot.telegram_adapter import TelegramUpdateAdapter
from etherfi_bot.telegram_api import _sanitize_payload

from tests.conftest import FsmHarness, make_dispatcher, make_user


def test_fsm_happy_path_logs_operational_outcomes(harness_factory, caplog) -> None:
    caplog.set_level(logging.INFO, logger="etherfi_bot.fsm")
    harness = harness_factory(make_user(telegram_user_id=8101))

    harness.fsm.start(harness.user)
    harness.balances.set_balance(harness.user.target_account, "1")
    low_state = harness.fsm.balance_tick(harness.user)
    assert low_state.current_message_id is not None

    harness.balances.set_balance(harness.user.target_account, "2")
    pending_state = harness.fsm.callback_top_up(harness.user, low_state.current_message_id)
    assert pending_state.pending_safe_tx_id is not None

    harness.clock.advance(harness.user.low_balance_notification_cooldown_seconds)
    harness.balances.set_balance(harness.user.target_account, "1")
    harness.fsm.balance_tick(harness.user)

    harness.safe.statuses[pending_state.pending_safe_tx_id] = SafeTxStatus.FINAL
    harness.fsm.balance_tick(harness.user)

    messages = _messages(caplog)
    assert _has_event(messages, "fsm_started")
    assert _has_event(messages, "low_balance_prompt_sent")
    assert _has_event(messages, "top_up_requested")
    assert _has_event(messages, "safe_tx_created")
    assert _has_event(messages, "safe_tx_pending_reminder_sent")
    assert _has_event(messages, "safe_tx_cleared")
    assert _has_record_at_level(caplog, "low_balance_prompt_sent", logging.INFO)
    assert any(f"safe_owner_key_ref={harness.user.safe_owner_key_ref}" in message for message in messages)
    assert all("private-key" not in message for message in messages)


def test_fsm_logs_ignore_and_recovery_outcomes(harness_factory, caplog) -> None:
    caplog.set_level(logging.INFO, logger="etherfi_bot.fsm")

    ignore_harness = harness_factory(make_user(telegram_user_id=8201))
    ignore_message_id = _send_low_prompt(ignore_harness)
    ignore_harness.fsm.callback_ignore(ignore_harness.user, ignore_message_id)

    recovery_harness = harness_factory(make_user(telegram_user_id=8202))
    _send_low_prompt(recovery_harness)
    recovery_harness.balances.set_balance(recovery_harness.user.target_account, "10")
    recovery_harness.fsm.balance_tick(recovery_harness.user)

    messages = _messages(caplog)
    assert _has_event(messages, "low_balance_ignored")
    assert _has_event(messages, "balance_recovered")


def test_fsm_failure_logs_cover_external_errors_and_admin_delivery(
    harness_factory,
    caplog,
) -> None:
    caplog.set_level(logging.DEBUG, logger="etherfi_bot.fsm")

    balance_harness = harness_factory(make_user(telegram_user_id=8301))
    balance_harness.fsm.start(balance_harness.user)
    balance_harness.balances.fail_accounts.add(balance_harness.user.target_account)
    balance_harness.fsm.balance_tick(balance_harness.user)

    status_harness = harness_factory(make_user(telegram_user_id=8302))
    safe_tx_id = _make_pending_safe_tx(status_harness)
    status_harness.safe.fail_status_for_txs.add(safe_tx_id)
    status_harness.fsm.balance_tick(status_harness.user)

    create_harness = harness_factory(make_user(telegram_user_id=8303))
    create_message_id = _send_low_prompt(create_harness)
    create_harness.safe.fail_create_for_users.add(create_harness.user.telegram_user_id)
    create_harness.fsm.callback_top_up(create_harness.user, create_message_id)

    forbidden_harness = harness_factory(make_user(telegram_user_id=8304))
    forbidden_message_id = _send_low_prompt(forbidden_harness)
    forbidden_harness.telegram.forbid_operation(
        forbidden_harness.user.telegram_user_id,
        "remove_buttons",
    )
    forbidden_harness.fsm.callback_ignore(forbidden_harness.user, forbidden_message_id)

    no_admin_harness = harness_factory(make_user(telegram_user_id=8305), admin_user_id=None)
    no_admin_harness.fsm.start(no_admin_harness.user)
    no_admin_harness.balances.fail_accounts.add(no_admin_harness.user.target_account)
    no_admin_harness.fsm.balance_tick(no_admin_harness.user)

    admin_failed_harness = harness_factory(make_user(telegram_user_id=8306))

    def fail_admin_delivery(*_args: object) -> None:
        raise RuntimeError("admin delivery failed")

    admin_failed_harness.telegram.send_admin_error = fail_admin_delivery
    admin_failed_harness.fsm.start(admin_failed_harness.user)
    admin_failed_harness.balances.fail_accounts.add(admin_failed_harness.user.target_account)
    admin_failed_harness.fsm.balance_tick(admin_failed_harness.user)

    messages = _messages(caplog)
    assert _has_event(messages, "balance_read_failed")
    assert _has_event(messages, "safe_tx_status_read_failed")
    assert _has_event(messages, "safe_tx_creation_failed")
    assert _has_event(messages, "telegram_forbidden_reset")
    assert _has_event(messages, "admin_notification_skipped")
    assert _has_event(messages, "admin_notification_failed")
    assert _has_record_at_level(caplog, "balance_read_failed", logging.WARNING)
    assert _has_record_at_level(caplog, "safe_tx_status_read_failed", logging.WARNING)
    assert _has_record_at_level(caplog, "safe_tx_creation_failed", logging.ERROR)
    assert _has_record_at_level(caplog, "telegram_forbidden_reset", logging.WARNING)


def test_fsm_debug_events_do_not_appear_at_info_level(harness_factory, caplog) -> None:
    harness = harness_factory(make_user(telegram_user_id=8401))
    harness.fsm.start(harness.user)
    message_id = _send_low_prompt(harness)

    caplog.set_level(logging.INFO, logger="etherfi_bot.fsm")
    caplog.clear()
    harness.fsm.start(harness.user)
    harness.fsm.callback_top_up(harness.user, message_id + 99)
    assert not _has_event(_messages(caplog), "fsm_start_ignored")
    assert not _has_event(_messages(caplog), "callback_stale_ignored")

    caplog.set_level(logging.DEBUG, logger="etherfi_bot.fsm")
    caplog.clear()
    harness.fsm.start(harness.user)
    harness.fsm.callback_top_up(harness.user, message_id + 99)
    messages = _messages(caplog)
    assert _has_event(messages, "fsm_start_ignored")
    assert _has_event(messages, "callback_stale_ignored")
    assert _has_record_at_level(caplog, "fsm_start_ignored", logging.DEBUG)
    assert _has_record_at_level(caplog, "callback_stale_ignored", logging.DEBUG)


def test_runtime_log_level_resolution() -> None:
    assert resolve_log_level("debug") == (logging.DEBUG, "DEBUG", None)
    assert resolve_log_level("WARNING") == (logging.WARNING, "WARNING", None)
    assert resolve_log_level("verbose") == (logging.INFO, "INFO", "verbose")


def test_dispatcher_custom_logger_is_shared_with_fsm(tmp_path, caplog) -> None:
    logger = logging.getLogger("etherfi_bot.custom_test_logger")
    caplog.set_level(logging.INFO, logger=logger.name)
    user = make_user(telegram_user_id=8451)
    dispatcher, *_ = make_dispatcher(tmp_path, [user], logger=logger)

    dispatcher.start(user.telegram_user_id)

    config_records = _records_for_event(caplog, "dispatcher_config_loaded")
    fsm_records = _records_for_event(caplog, "fsm_started")
    assert config_records
    assert fsm_records
    assert all(record.name == logger.name for record in config_records + fsm_records)


def test_adapter_logs_ignored_updates_and_callback_ack_failures(tmp_path, caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="etherfi_bot.telegram_adapter")
    user = make_user(telegram_user_id=8501)
    dispatcher, *_ = make_dispatcher(tmp_path, [user])
    adapter = TelegramUpdateAdapter(dispatcher, callback_answerer=FailingCallbackAnswerer())

    assert adapter.handle_update({"update_id": 101, "unknown": {}}) == "ignored_unsupported_update"
    assert (
        adapter.handle_update(
            {
                "update_id": 102,
                "callback_query": {
                    "id": "callback-102",
                    "from": {"id": user.telegram_user_id, "is_bot": False},
                    "message": {"message_id": 55},
                    "data": "unsupported",
                },
            }
        )
        == "ignored_callback"
    )

    messages = _messages(caplog)
    assert _has_event(messages, "telegram_update_ignored")
    assert _has_event(messages, "callback_ack_failed")
    assert any("telegram_user_id=8501" in message for message in messages)
    assert any("error_type=RuntimeError" in message for message in messages)


def test_telegram_api_debug_payload_sanitizer_redacts_text_fields() -> None:
    sanitized = _sanitize_payload(
        {
            "chat_id": 1,
            "text": "message body",
            "reply_markup": {
                "inline_keyboard": [[{"text": "Top Up", "callback_data": "top_up"}]]
            },
        }
    )

    assert sanitized["text"] == "<redacted>"
    assert sanitized["reply_markup"]["inline_keyboard"][0][0]["text"] == "<redacted>"
    assert sanitized["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "top_up"


class FailingCallbackAnswerer:
    def answer_callback_query(self, callback_query_id: str) -> bool:
        raise RuntimeError(f"expired callback {callback_query_id}")


def _send_low_prompt(harness: FsmHarness) -> int:
    harness.fsm.start(harness.user)
    harness.balances.set_balance(harness.user.target_account, "1")
    state = harness.fsm.balance_tick(harness.user)
    assert state.current_message_id is not None
    return state.current_message_id


def _make_pending_safe_tx(harness: FsmHarness) -> str:
    message_id = _send_low_prompt(harness)
    harness.balances.set_balance(harness.user.target_account, "2")
    state = harness.fsm.callback_top_up(harness.user, message_id)
    assert state.pending_safe_tx_id is not None
    return state.pending_safe_tx_id


def _messages(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records]


def _has_event(messages: list[str], event: str) -> bool:
    return any(message == event or message.startswith(f"{event} ") for message in messages)


def _has_record_at_level(caplog, event: str, level: int) -> bool:
    return any(record.levelno == level for record in _records_for_event(caplog, event))


def _records_for_event(caplog, event: str) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.getMessage() == event or record.getMessage().startswith(f"{event} ")
    ]
