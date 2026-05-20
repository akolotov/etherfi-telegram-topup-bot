from __future__ import annotations

import logging
from math import ceil

from etherfi_bot.domain import BotConfig, BotState, UserConfig, UserState
from etherfi_bot.fsm import FsmService
from etherfi_bot.ports import (
    BalanceProvider,
    Clock,
    ConfigRepository,
    Keychain,
    SafeWalletClient,
    StateRepository,
    TelegramGateway,
)


class BotDispatcher:
    def __init__(
        self,
        config_repository: ConfigRepository,
        state_repository: StateRepository,
        telegram: TelegramGateway,
        balances: BalanceProvider,
        safe_wallet: SafeWalletClient,
        keychain: Keychain,
        clock: Clock,
        logger: logging.Logger | None = None,
    ) -> None:
        self._config_repository = config_repository
        self._states = state_repository
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)
        self.config: BotConfig = self._config_repository.load()
        self._log_config_loaded()
        self.fsm = FsmService(
            state_repository=state_repository,
            telegram=telegram,
            balances=balances,
            safe_wallet=safe_wallet,
            keychain=keychain,
            clock=clock,
            admin_telegram_user_id=self.config.admin_telegram_user_id,
        )

    def reload_config(self) -> BotConfig:
        self.config = self._config_repository.load()
        self._log_config_loaded()
        return self.config

    def start(self, telegram_user_id: int) -> UserState | None:
        user = self._configured_user(telegram_user_id)
        if user is None:
            self._log_unknown_user("start", telegram_user_id)
            return None
        return self.fsm.start(user)

    def balance_tick(self, telegram_user_id: int) -> UserState | None:
        user = self._configured_user(telegram_user_id)
        if user is None:
            self._log_unknown_user("balance_tick", telegram_user_id)
            return None
        return self.fsm.balance_tick(user)

    def callback_top_up(self, telegram_user_id: int, message_id: int) -> UserState | None:
        user = self._configured_user(telegram_user_id)
        if user is None:
            self._log_unknown_user(
                "callback_top_up",
                telegram_user_id,
                message_id=message_id,
            )
            return None
        return self.fsm.callback_top_up(user, message_id)

    def callback_ignore(self, telegram_user_id: int, message_id: int) -> UserState | None:
        user = self._configured_user(telegram_user_id)
        if user is None:
            self._log_unknown_user(
                "callback_ignore",
                telegram_user_id,
                message_id=message_id,
            )
            return None
        return self.fsm.callback_ignore(user, message_id)

    def user_blocked(self, telegram_user_id: int) -> UserState | None:
        user = self._configured_user(telegram_user_id)
        if user is None:
            self._log_unknown_user("user_blocked", telegram_user_id)
            return None
        return self.fsm.user_blocked(user)

    def ignore_event(self, telegram_user_id: int) -> UserState | None:
        user = self._configured_user(telegram_user_id)
        if user is None:
            self._log_unknown_user("ignore_event", telegram_user_id)
            return None
        return self.fsm.ignore_event(user)

    def restart(self, run_due_ticks: bool = True) -> list[int]:
        due_user_ids = self.due_user_ids()
        if run_due_ticks:
            for telegram_user_id in due_user_ids:
                self.balance_tick(telegram_user_id)
        return due_user_ids

    def due_user_ids(self) -> list[int]:
        now = self._clock.now()
        due: list[int] = []
        for state in self._states.list_states():
            if state.state is BotState.NOT_STARTED:
                continue
            if self._configured_user(state.telegram_user_id) is None:
                continue
            if state.next_tick_at is None or state.next_tick_at <= now:
                due.append(state.telegram_user_id)
        return due

    def seconds_until_next_due_tick(self) -> int | None:
        now = self._clock.now()
        soonest_seconds: int | None = None
        for state in self._states.list_states():
            if state.state is BotState.NOT_STARTED:
                continue
            if self._configured_user(state.telegram_user_id) is None:
                continue
            if state.next_tick_at is None:
                return 0
            seconds = max(0, ceil((state.next_tick_at - now).total_seconds()))
            if soonest_seconds is None or seconds < soonest_seconds:
                soonest_seconds = seconds
        return soonest_seconds

    def _configured_user(self, telegram_user_id: int) -> UserConfig | None:
        return self.config.user(int(telegram_user_id))

    def _log_config_loaded(self) -> None:
        self._logger.info(
            "dispatcher_config_loaded configured_user_count=%s admin_telegram_user_id=%s",
            len(self.config.users_by_telegram_id),
            self.config.admin_telegram_user_id,
        )

    def _log_unknown_user(
        self,
        event: str,
        telegram_user_id: int,
        *,
        message_id: int | None = None,
    ) -> None:
        self._logger.debug(
            "dispatcher_user_ignored event=%s telegram_user_id=%s message_id=%s reason=user_not_configured",
            event,
            int(telegram_user_id),
            message_id,
        )
