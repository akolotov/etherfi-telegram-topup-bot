from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from threading import Lock, RLock

from etherfi_bot.domain import (
    BalanceReadError,
    BotState,
    SafeTxCreateError,
    SafeTxStatusReadError,
    SafeTxStatus,
    TelegramForbiddenError,
    UserConfig,
    UserState,
)
from etherfi_bot.ports import (
    BalanceProvider,
    Clock,
    PrivateKeyProvider,
    SafeWalletClient,
    StateRepository,
    TelegramGateway,
)


class FsmService:
    def __init__(
        self,
        state_repository: StateRepository,
        telegram: TelegramGateway,
        balances: BalanceProvider,
        safe_wallet: SafeWalletClient,
        private_keys: PrivateKeyProvider,
        clock: Clock,
        admin_telegram_user_id: int | None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._states = state_repository
        self._telegram = telegram
        self._balances = balances
        self._safe_wallet = safe_wallet
        self._private_keys = private_keys
        self._clock = clock
        self._admin_telegram_user_id = admin_telegram_user_id
        self._logger = logger or logging.getLogger(__name__)
        self._locks_guard = Lock()
        self._user_locks: dict[int, RLock] = {}

    def start(self, user: UserConfig) -> UserState:
        with self._user_lock(user.telegram_user_id):
            state = self._states.load(user.telegram_user_id)
            if state.state is not BotState.NOT_STARTED:
                self._log_user_event(
                    logging.DEBUG,
                    "fsm_start_ignored",
                    user,
                    state=state.state,
                    next_tick_at=state.next_tick_at,
                    pending_safe_tx_id=state.pending_safe_tx_id,
                )
                return state
            previous_state = state.state
            state.reset_runtime()
            state.state = BotState.MONITORING
            self._states.save(state)
            self._log_user_event(
                logging.INFO,
                "fsm_started",
                user,
                previous_state=previous_state,
                state=state.state,
                next_tick_at=state.next_tick_at,
            )
            return state

    def balance_tick(self, user: UserConfig) -> UserState:
        with self._user_lock(user.telegram_user_id):
            state = self._states.load(user.telegram_user_id)
            if state.state is BotState.NOT_STARTED:
                self._log_user_event(
                    logging.DEBUG,
                    "balance_tick_skipped",
                    user,
                    state=state.state,
                    reason="not_started",
                )
                return state
            previous_state = state.state
            try:
                self._handle_balance_tick(user, state)
            except TelegramForbiddenError as error:
                state.reset_runtime()
                self._log_user_event(
                    logging.WARNING,
                    "telegram_forbidden_reset",
                    user,
                    operation_context="balance_tick",
                    previous_state=previous_state,
                    state=state.state,
                    error_type=type(error).__name__,
                    error=error,
                )
            self._states.save(state)
            return state

    def callback_ignore(self, user: UserConfig, message_id: int) -> UserState:
        with self._user_lock(user.telegram_user_id):
            state = self._states.load(user.telegram_user_id)
            if not self._is_latest_callback(state, message_id):
                self._log_user_event(
                    logging.DEBUG,
                    "callback_stale_ignored",
                    user,
                    callback_action="ignore",
                    state=state.state,
                    callback_message_id=message_id,
                    current_message_id=state.current_message_id,
                )
                return state
            previous_state = state.state
            try:
                self._telegram.remove_buttons(user.telegram_user_id, message_id)
                self._clear_low_context(state)
                state.state = BotState.MONITORING
                self._log_user_event(
                    logging.INFO,
                    "low_balance_ignored",
                    user,
                    previous_state=previous_state,
                    state=state.state,
                    message_id=message_id,
                )
            except TelegramForbiddenError as error:
                state.reset_runtime()
                self._log_user_event(
                    logging.WARNING,
                    "telegram_forbidden_reset",
                    user,
                    operation_context="callback_ignore",
                    previous_state=previous_state,
                    state=state.state,
                    message_id=message_id,
                    error_type=type(error).__name__,
                    error=error,
                )
            self._states.save(state)
            return state

    def callback_top_up(self, user: UserConfig, message_id: int) -> UserState:
        with self._user_lock(user.telegram_user_id):
            state = self._states.load(user.telegram_user_id)
            if not self._is_latest_callback(state, message_id):
                self._log_user_event(
                    logging.DEBUG,
                    "callback_stale_ignored",
                    user,
                    callback_action="top_up",
                    state=state.state,
                    callback_message_id=message_id,
                    current_message_id=state.current_message_id,
                )
                return state
            previous_state = state.state
            try:
                if state.state in {BotState.LOW_PROMPT, BotState.LOW_COOLDOWN}:
                    self._log_user_event(
                        logging.INFO,
                        "top_up_requested",
                        user,
                        state=state.state,
                        message_id=message_id,
                    )
                    self._handle_new_safe_tx_top_up(user, state, message_id)
            except TelegramForbiddenError as error:
                state.reset_runtime()
                self._log_user_event(
                    logging.WARNING,
                    "telegram_forbidden_reset",
                    user,
                    operation_context="callback_top_up",
                    previous_state=previous_state,
                    state=state.state,
                    message_id=message_id,
                    error_type=type(error).__name__,
                    error=error,
                )
            self._states.save(state)
            return state

    def user_blocked(self, user: UserConfig) -> UserState:
        with self._user_lock(user.telegram_user_id):
            state = self._states.load(user.telegram_user_id)
            previous_state = state.state
            state.reset_runtime()
            self._states.save(state)
            self._log_user_event(
                logging.INFO,
                "user_blocked",
                user,
                previous_state=previous_state,
                state=state.state,
            )
            return state

    def ignore_event(self, user: UserConfig) -> UserState:
        with self._user_lock(user.telegram_user_id):
            return self._states.load(user.telegram_user_id)

    def _handle_balance_tick(self, user: UserConfig, state: UserState) -> None:
        handled_at = self._clock.now()
        safe_status: SafeTxStatus | None = None
        safe_status_error: SafeTxStatusReadError | None = None
        try:
            safe_status = self._read_safe_status_if_needed(user, state)
        except SafeTxStatusReadError as error:
            safe_status_error = error
        balance: Decimal | None = None
        balance_error: BalanceReadError | None = None
        try:
            balance = self._balances.get_balance(user)
        except BalanceReadError as error:
            balance_error = error

        if safe_status_error is not None:
            self._log_user_event(
                logging.WARNING,
                "safe_tx_status_read_failed",
                user,
                state=state.state,
                safe_tx_id=state.pending_safe_tx_id,
                error_type=type(safe_status_error).__name__,
                error=safe_status_error,
            )
            self._notify_admin(
                f"Safe tx status read failed for safe {user.safe_account}: {safe_status_error}"
            )

        if balance_error is not None:
            self._log_user_event(
                logging.WARNING,
                "balance_read_failed",
                user,
                state=state.state,
                target_account=user.target_account,
                pending_safe_tx_id=state.pending_safe_tx_id,
                error_type=type(balance_error).__name__,
                error=balance_error,
            )
            self._notify_admin(
                "Balance read failed for target account "
                f"{user.target_account}: {balance_error}"
            )
            if state.state is BotState.SAFE_TX_PENDING:
                self._handle_safe_tx_tick_with_balance_error(
                    user,
                    state,
                    safe_status,
                    safe_status_error,
                    handled_at,
                )
            self._schedule_next_tick(user, state, handled_at)
            return

        assert balance is not None
        previous_balance = state.last_balance
        self._handle_admin_balance_drop_notification(
            user,
            state,
            balance,
            previous_balance,
        )
        state.last_balance = balance
        if state.state is BotState.SAFE_TX_PENDING:
            if safe_status_error is not None:
                if self._balance_ok(user, balance):
                    safe_tx_id = state.pending_safe_tx_id
                    previous_state = state.state
                    self._clear_low_context(state)
                    self._clear_tx_context(state)
                    state.state = BotState.MONITORING
                    self._log_user_event(
                        logging.INFO,
                        "balance_recovered",
                        user,
                        previous_state=previous_state,
                        state=state.state,
                        balance=balance,
                        threshold=user.balance_threshold,
                        safe_tx_id=safe_tx_id,
                    )
                    self._log_user_event(
                        logging.INFO,
                        "safe_tx_cleared",
                        user,
                        previous_state=previous_state,
                        state=state.state,
                        reason="balance_ok",
                        balance=balance,
                        threshold=user.balance_threshold,
                        safe_tx_id=safe_tx_id,
                        safe_status_read_failed=True,
                    )
                else:
                    self._log_user_event(
                        logging.DEBUG,
                        "balance_tick_noop",
                        user,
                        state=state.state,
                        reason="safe_status_unavailable_balance_still_low",
                        balance=balance,
                        threshold=user.balance_threshold,
                        safe_tx_id=state.pending_safe_tx_id,
                    )
            else:
                self._handle_safe_tx_tick(user, state, balance, safe_status, handled_at)
        else:
            self._handle_plain_balance_tick(user, state, balance, handled_at)
        self._schedule_next_tick(user, state, handled_at)

    def _handle_plain_balance_tick(
        self,
        user: UserConfig,
        state: UserState,
        balance: Decimal,
        handled_at: datetime,
    ) -> None:
        if self._balance_ok(user, balance):
            previous_state = state.state
            previous_message_id = state.current_message_id
            if state.state in {BotState.LOW_PROMPT, BotState.LOW_COOLDOWN}:
                self._remove_current_buttons(user, state)
            self._clear_low_context(state)
            state.state = BotState.MONITORING
            if previous_state is BotState.MONITORING:
                self._log_user_event(
                    logging.DEBUG,
                    "balance_tick_noop",
                    user,
                    state=state.state,
                    reason="balance_ok",
                    balance=balance,
                    threshold=user.balance_threshold,
                )
            else:
                self._log_user_event(
                    logging.INFO,
                    "balance_recovered",
                    user,
                    previous_state=previous_state,
                    state=state.state,
                    balance=balance,
                    threshold=user.balance_threshold,
                    message_id=previous_message_id,
                )
            return

        if state.state is BotState.MONITORING:
            self._send_first_low_prompt(user, state, balance, handled_at)
            return

        if state.state is BotState.LOW_PROMPT:
            self._send_next_low_prompt(user, state, balance, handled_at)
            return

        if state.state is BotState.LOW_COOLDOWN:
            if state.low_cooldown_until is not None and handled_at < state.low_cooldown_until:
                self._log_user_event(
                    logging.DEBUG,
                    "balance_tick_noop",
                    user,
                    state=state.state,
                    reason="low_cooldown_active",
                    balance=balance,
                    threshold=user.balance_threshold,
                    low_cooldown_until=state.low_cooldown_until,
                )
                return
            previous_state = state.state
            old_message_id, new_message_id = self._replace_current_with_low_prompt(
                user,
                state,
                balance,
            )
            state.notification_count = 1
            if user.low_balance_notification_limit == 1:
                state.low_cooldown_until = handled_at + self._cooldown_delta(user)
                state.state = BotState.LOW_COOLDOWN
            else:
                state.low_cooldown_until = None
                state.state = BotState.LOW_PROMPT
            self._log_user_event(
                logging.INFO,
                "low_balance_prompt_replaced",
                user,
                previous_state=previous_state,
                state=state.state,
                balance=balance,
                threshold=user.balance_threshold,
                old_message_id=old_message_id,
                message_id=new_message_id,
                notification_count=state.notification_count,
                notification_limit=user.low_balance_notification_limit,
                low_cooldown_until=state.low_cooldown_until,
            )

    def _handle_safe_tx_tick(
        self,
        user: UserConfig,
        state: UserState,
        balance: Decimal,
        safe_status: SafeTxStatus | None,
        handled_at: datetime,
    ) -> None:
        if state.pending_safe_tx_id is None:
            previous_state = state.state
            self._clear_low_context(state)
            self._clear_tx_context(state)
            state.state = BotState.MONITORING
            self._log_user_event(
                logging.WARNING,
                "fsm_state_inconsistent",
                user,
                previous_state=previous_state,
                state=state.state,
                reason="safe_tx_pending_without_safe_tx_id",
            )
            return

        if self._balance_ok(user, balance) or safe_status is SafeTxStatus.FINAL:
            previous_state = state.state
            safe_tx_id = state.pending_safe_tx_id
            reason = "final_status" if safe_status is SafeTxStatus.FINAL else "balance_ok"
            self._clear_low_context(state)
            self._clear_tx_context(state)
            state.state = BotState.MONITORING
            if reason == "balance_ok":
                self._log_user_event(
                    logging.INFO,
                    "balance_recovered",
                    user,
                    previous_state=previous_state,
                    state=state.state,
                    balance=balance,
                    threshold=user.balance_threshold,
                    safe_tx_id=safe_tx_id,
                )
            self._log_user_event(
                logging.INFO,
                "safe_tx_cleared",
                user,
                previous_state=previous_state,
                state=state.state,
                reason=reason,
                balance=balance,
                threshold=user.balance_threshold,
                safe_tx_id=safe_tx_id,
                safe_status=safe_status,
            )
            return

        self._send_safe_tx_reminder_if_due(user, state, handled_at)

    def _handle_safe_tx_tick_with_balance_error(
        self,
        user: UserConfig,
        state: UserState,
        safe_status: SafeTxStatus | None,
        safe_status_error: SafeTxStatusReadError | None,
        handled_at: datetime,
    ) -> None:
        if state.pending_safe_tx_id is None:
            previous_state = state.state
            self._clear_low_context(state)
            self._clear_tx_context(state)
            state.state = BotState.MONITORING
            self._log_user_event(
                logging.WARNING,
                "fsm_state_inconsistent",
                user,
                previous_state=previous_state,
                state=state.state,
                reason="safe_tx_pending_without_safe_tx_id",
                balance_read_failed=True,
            )
            return

        if safe_status is SafeTxStatus.FINAL:
            previous_state = state.state
            safe_tx_id = state.pending_safe_tx_id
            self._clear_low_context(state)
            self._clear_tx_context(state)
            state.state = BotState.MONITORING
            self._log_user_event(
                logging.INFO,
                "safe_tx_cleared",
                user,
                previous_state=previous_state,
                state=state.state,
                reason="final_status",
                safe_tx_id=safe_tx_id,
                safe_status=safe_status,
                balance_read_failed=True,
            )
            return

        if safe_status_error is None:
            self._send_safe_tx_reminder_if_due(user, state, handled_at)
        else:
            self._log_user_event(
                logging.DEBUG,
                "balance_tick_noop",
                user,
                state=state.state,
                reason="safe_status_and_balance_unavailable",
                safe_tx_id=state.pending_safe_tx_id,
            )

    def _send_safe_tx_reminder_if_due(
        self,
        user: UserConfig,
        state: UserState,
        handled_at: datetime,
    ) -> None:
        assert state.pending_safe_tx_id is not None
        if state.tx_reminder_until is not None and handled_at < state.tx_reminder_until:
            self._log_user_event(
                logging.DEBUG,
                "balance_tick_noop",
                user,
                state=state.state,
                reason="safe_tx_reminder_cooldown_active",
                safe_tx_id=state.pending_safe_tx_id,
                tx_reminder_until=state.tx_reminder_until,
            )
            return
        safe_tx_id = state.pending_safe_tx_id
        message_id = self._telegram.send_safe_tx_pending_prompt(user, safe_tx_id)
        state.current_message_id = None
        state.tx_reminder_until = handled_at + self._cooldown_delta(user)
        state.state = BotState.SAFE_TX_PENDING
        self._log_user_event(
            logging.INFO,
            "safe_tx_pending_reminder_sent",
            user,
            state=state.state,
            safe_tx_id=safe_tx_id,
            tx_reminder_until=state.tx_reminder_until,
            message_id=message_id,
        )

    def _handle_new_safe_tx_top_up(
        self,
        user: UserConfig,
        state: UserState,
        message_id: int,
    ) -> None:
        previous_state = state.state
        self._telegram.remove_buttons(user.telegram_user_id, message_id)
        state.current_message_id = None
        self._clear_low_context(state)
        try:
            fresh_balance = self._balances.get_balance(user)
        except BalanceReadError as error:
            self._log_user_event(
                logging.WARNING,
                "top_up_fresh_balance_read_failed",
                user,
                previous_state=previous_state,
                state=BotState.MONITORING,
                message_id=message_id,
                target_account=user.target_account,
                balance_token_address=user.balance_token_address,
                error_type=type(error).__name__,
                error=error,
            )
            self._notify_admin(
                "Fresh balance read failed for target account "
                f"{user.target_account}: {error}"
            )
            state.state = BotState.MONITORING
            return

        amount = user.target_max_balance - fresh_balance
        if self._balance_ok(user, fresh_balance) or amount <= 0:
            reason = (
                "fresh_balance_ok"
                if self._balance_ok(user, fresh_balance)
                else "non_positive_amount"
            )
            self._log_user_event(
                logging.INFO,
                "top_up_skipped",
                user,
                previous_state=previous_state,
                state=BotState.MONITORING,
                reason=reason,
                message_id=message_id,
                fresh_balance=fresh_balance,
                threshold=user.balance_threshold,
                target_max_balance=user.target_max_balance,
                amount=amount,
            )
            state.state = BotState.MONITORING
            return

        try:
            private_key = self._private_keys.read_private_key(user.safe_proposer_key_file)
            safe_tx_id = self._safe_wallet.create_top_up_tx(user, amount, private_key)
        except (KeyError, OSError, ValueError, SafeTxCreateError) as error:
            self._log_user_event(
                logging.ERROR,
                "safe_tx_creation_failed",
                user,
                previous_state=previous_state,
                state=BotState.MONITORING,
                message_id=message_id,
                fresh_balance=fresh_balance,
                target_max_balance=user.target_max_balance,
                amount=amount,
                safe_proposer_key_file=user.safe_proposer_key_file,
                error_type=type(error).__name__,
                error=error,
            )
            self._notify_admin(
                f"Safe tx creation failed for safe {user.safe_account}: {error}"
            )
            state.state = BotState.MONITORING
            return

        self._log_user_event(
            logging.INFO,
            "safe_tx_created",
            user,
            previous_state=previous_state,
            state=BotState.SAFE_TX_PENDING,
            message_id=message_id,
            safe_tx_id=safe_tx_id,
            amount=amount,
            fresh_balance=fresh_balance,
            target_max_balance=user.target_max_balance,
            safe_proposer_key_file=user.safe_proposer_key_file,
        )
        self._notify_admin(
            f"Tx created in safe {user.safe_account} to top up {user.target_account}"
        )
        # A Telegram 403 here is intentional state signal: the user blocked the bot
        # after requesting top-up, so callback_top_up resets the user to S0.
        self._telegram.send_safe_tx_created(user, safe_tx_id)
        state.pending_safe_tx_id = safe_tx_id
        state.tx_reminder_until = self._clock.now() + self._cooldown_delta(user)
        state.state = BotState.SAFE_TX_PENDING

    def _send_first_low_prompt(
        self,
        user: UserConfig,
        state: UserState,
        balance: Decimal,
        handled_at: datetime,
    ) -> None:
        previous_state = state.state
        message_id = self._telegram.send_low_balance_prompt(user, balance)
        state.current_message_id = message_id
        state.notification_count = 1
        if user.low_balance_notification_limit == 1:
            state.low_cooldown_until = handled_at + self._cooldown_delta(user)
            state.state = BotState.LOW_COOLDOWN
        else:
            state.low_cooldown_until = None
            state.state = BotState.LOW_PROMPT
        self._log_user_event(
            logging.INFO,
            "low_balance_prompt_sent",
            user,
            previous_state=previous_state,
            state=state.state,
            balance=balance,
            threshold=user.balance_threshold,
            message_id=message_id,
            notification_count=state.notification_count,
            notification_limit=user.low_balance_notification_limit,
            low_cooldown_until=state.low_cooldown_until,
        )

    def _send_next_low_prompt(
        self,
        user: UserConfig,
        state: UserState,
        balance: Decimal,
        handled_at: datetime,
    ) -> None:
        previous_state = state.state
        next_count = state.notification_count + 1
        old_message_id, new_message_id = self._replace_current_with_low_prompt(user, state, balance)
        state.notification_count = next_count
        if next_count >= user.low_balance_notification_limit:
            state.notification_count = user.low_balance_notification_limit
            state.low_cooldown_until = handled_at + self._cooldown_delta(user)
            state.state = BotState.LOW_COOLDOWN
        else:
            state.low_cooldown_until = None
            state.state = BotState.LOW_PROMPT
        self._log_user_event(
            logging.INFO,
            "low_balance_prompt_replaced",
            user,
            previous_state=previous_state,
            state=state.state,
            balance=balance,
            threshold=user.balance_threshold,
            old_message_id=old_message_id,
            message_id=new_message_id,
            notification_count=state.notification_count,
            notification_limit=user.low_balance_notification_limit,
            low_cooldown_until=state.low_cooldown_until,
        )

    def _replace_current_with_low_prompt(
        self,
        user: UserConfig,
        state: UserState,
        balance: Decimal,
    ) -> tuple[int | None, int]:
        old_message_id = state.current_message_id
        self._remove_current_buttons(user, state)
        message_id = self._telegram.send_low_balance_prompt(user, balance)
        state.current_message_id = message_id
        return old_message_id, message_id

    def _remove_current_buttons(self, user: UserConfig, state: UserState) -> None:
        if state.current_message_id is not None:
            self._telegram.remove_buttons(user.telegram_user_id, state.current_message_id)
            state.current_message_id = None

    def _read_safe_status_if_needed(
        self,
        user: UserConfig,
        state: UserState,
    ) -> SafeTxStatus | None:
        if state.state is not BotState.SAFE_TX_PENDING:
            return None
        if state.pending_safe_tx_id is None:
            return None
        return self._safe_wallet.get_tx_status(user, state.pending_safe_tx_id)

    def _is_latest_callback(self, state: UserState, message_id: int) -> bool:
        return (
            state.state in {BotState.LOW_PROMPT, BotState.LOW_COOLDOWN}
            and state.current_message_id == int(message_id)
        )

    def _schedule_next_tick(
        self,
        user: UserConfig,
        state: UserState,
        handled_at: datetime,
    ) -> None:
        state.last_balance_checked_at = handled_at
        state.next_tick_at = handled_at + self._tick_delta(user)

    def _handle_admin_balance_drop_notification(
        self,
        user: UserConfig,
        state: UserState,
        balance: Decimal,
        previous_balance: Decimal | None,
    ) -> None:
        if self._balance_ok(user, balance):
            state.low_balance_drop_admin_notified = False
            return
        if previous_balance is None:
            return
        if state.low_balance_drop_admin_notified:
            return
        if balance >= previous_balance:
            return

        state.low_balance_drop_admin_notified = True
        self._log_user_event(
            logging.INFO,
            "low_balance_drop_admin_notified",
            user,
            previous_balance=previous_balance,
            balance=balance,
            threshold=user.balance_threshold,
        )
        self._notify_admin(
            f"{user.target_account} balance dropped below {user.balance_threshold}, "
            f"current balance {balance}"
        )

    def _notify_admin(self, message: str) -> None:
        if self._admin_telegram_user_id is None:
            self._logger.debug("admin_notification_skipped reason=admin_not_configured")
            return
        try:
            self._telegram.send_admin_error(self._admin_telegram_user_id, message)
        except Exception as error:
            self._logger.warning(
                "admin_notification_failed admin_telegram_user_id=%s error_type=%s error=%s",
                self._admin_telegram_user_id,
                type(error).__name__,
                error,
            )
            return

    def _balance_ok(self, user: UserConfig, balance: Decimal) -> bool:
        return balance >= user.balance_threshold

    def _tick_delta(self, user: UserConfig) -> timedelta:
        return timedelta(seconds=user.balance_check_interval_seconds)

    def _cooldown_delta(self, user: UserConfig) -> timedelta:
        return timedelta(seconds=user.low_balance_notification_cooldown_seconds)

    def _clear_low_context(self, state: UserState) -> None:
        state.notification_count = 0
        state.low_cooldown_until = None
        state.current_message_id = None

    def _clear_tx_context(self, state: UserState) -> None:
        state.pending_safe_tx_id = None
        state.tx_reminder_until = None

    def _log_user_event(
        self,
        level: int,
        event: str,
        user: UserConfig,
        **fields: object,
    ) -> None:
        if not self._logger.isEnabledFor(level):
            return
        log_fields: dict[str, object] = {
            "telegram_user_id": user.telegram_user_id,
            "target_account": user.target_account,
            "balance_token_address": user.balance_token_address,
            "safe_account": user.safe_account,
            "safe_proposer_key_file": user.safe_proposer_key_file,
        }
        log_fields.update(fields)
        message = event + " " + " ".join(f"{key}=%s" for key in log_fields)
        values = tuple(_log_value(value) for value in log_fields.values())
        self._logger.log(level, message, *values)

    def _user_lock(self, telegram_user_id: int) -> RLock:
        normalized_user_id = int(telegram_user_id)
        with self._locks_guard:
            lock = self._user_locks.get(normalized_user_id)
            if lock is None:
                lock = RLock()
                self._user_locks[normalized_user_id] = lock
            return lock


def _log_value(value: object) -> object:
    if isinstance(value, (BotState, SafeTxStatus)):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Exception):
        return str(value)
    return value
