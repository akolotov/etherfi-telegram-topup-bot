from __future__ import annotations

import logging
from math import ceil

from decimal import Decimal

from etherfi_bot.domain import BotConfig, BotState, ManualTopUpContext, UserConfig, UserState
from etherfi_bot.fsm import FsmService
from etherfi_bot.ports import (
    BalanceProvider,
    Clock,
    ConfigRepository,
    PrivateKeyProvider,
    SafeWalletClient,
    SafeBalanceProvider,
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
        private_keys: PrivateKeyProvider,
        clock: Clock,
        logger: logging.Logger | None = None,
        safe_balances: SafeBalanceProvider | None = None,
    ) -> None:
        self._config_repository = config_repository
        self._states = state_repository
        self._telegram = telegram
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)
        self.config: BotConfig = self._config_repository.load()
        self._log_config_loaded()
        self.fsm = FsmService(
            state_repository=state_repository,
            telegram=telegram,
            balances=balances,
            safe_wallet=safe_wallet,
            private_keys=private_keys,
            clock=clock,
            admin_telegram_user_id=self.config.admin_telegram_user_id,
            logger=logger,
            safe_balances=safe_balances,
        )

    def reload_config(self) -> BotConfig:
        self.config = self._config_repository.load()
        self._log_config_loaded()
        return self.config

    async def start(self, telegram_user_id: int) -> UserState | None:
        user = self._configured_user(telegram_user_id)
        if user is None:
            self._log_unknown_user("start", telegram_user_id)
            return None
        state = await self.fsm.start(user)
        await self._reconcile_top_up_menu(user)
        return state

    async def manual_top_up_launcher(self, telegram_user_id: int) -> int | None:
        user = self._configured_user(telegram_user_id)
        if user is None or user.manual_top_up is None:
            return None
        state = self._states.load(telegram_user_id)
        if state.state is BotState.NOT_STARTED:
            return None
        return await self._telegram.send_manual_top_up_launcher(user)

    async def manual_top_up_context(
        self, telegram_user_id: int
    ) -> ManualTopUpContext | None:
        user = self._configured_user(telegram_user_id)
        if user is None or user.manual_top_up is None:
            return None
        return await self.fsm.manual_top_up_context(user)

    async def prepare_manual_top_up(
        self, telegram_user_id: int, amount: Decimal
    ) -> UserState | None:
        user = self._configured_user(telegram_user_id)
        if user is None or user.manual_top_up is None:
            return None
        return await self.fsm.prepare_manual_top_up(user, amount)

    async def callback_manual_top_up_confirm(
        self, telegram_user_id: int, message_id: int, request_id: str
    ) -> UserState | None:
        user = self._configured_user(telegram_user_id)
        if user is None:
            return None
        return await self.fsm.callback_manual_top_up_confirm(
            user, message_id, request_id
        )

    async def callback_manual_top_up_cancel(
        self, telegram_user_id: int, message_id: int, request_id: str
    ) -> UserState | None:
        user = self._configured_user(telegram_user_id)
        if user is None:
            return None
        return await self.fsm.callback_manual_top_up_cancel(
            user, message_id, request_id
        )

    async def balance_tick(self, telegram_user_id: int) -> UserState | None:
        user = self._configured_user(telegram_user_id)
        if user is None:
            self._log_unknown_user("balance_tick", telegram_user_id)
            return None
        return await self.fsm.balance_tick(user)

    async def callback_top_up(
        self, telegram_user_id: int, message_id: int
    ) -> UserState | None:
        user = self._configured_user(telegram_user_id)
        if user is None:
            self._log_unknown_user(
                "callback_top_up",
                telegram_user_id,
                message_id=message_id,
            )
            return None
        return await self.fsm.callback_top_up(user, message_id)

    async def callback_ignore(
        self, telegram_user_id: int, message_id: int
    ) -> UserState | None:
        user = self._configured_user(telegram_user_id)
        if user is None:
            self._log_unknown_user(
                "callback_ignore",
                telegram_user_id,
                message_id=message_id,
            )
            return None
        return await self.fsm.callback_ignore(user, message_id)

    async def user_blocked(self, telegram_user_id: int) -> UserState | None:
        user = self._configured_user(telegram_user_id)
        if user is None:
            self._log_unknown_user("user_blocked", telegram_user_id)
            return None
        return await self.fsm.user_blocked(user)

    async def ignore_event(self, telegram_user_id: int) -> UserState | None:
        user = self._configured_user(telegram_user_id)
        if user is None:
            self._log_unknown_user("ignore_event", telegram_user_id)
            return None
        return await self.fsm.ignore_event(user)

    async def recover_missing_user_states(self) -> list[int]:
        persisted_user_ids = {state.telegram_user_id for state in self._states.list_states()}
        recovered_user_ids: list[int] = []
        for user in self.config.users_by_telegram_id.values():
            if user.telegram_user_id in persisted_user_ids:
                state = self._states.load(user.telegram_user_id)
                if state.state is not BotState.NOT_STARTED:
                    await self._reconcile_top_up_menu(user)
                continue
            try:
                can_reach_user = await self._telegram.can_reach_private_chat(
                    user.telegram_user_id
                )
            except Exception as error:
                self._logger.warning(
                    "missing_user_state_recovery_failed telegram_user_id=%s error_type=%s error=%s",
                    user.telegram_user_id,
                    type(error).__name__,
                    error,
                )
                continue
            if can_reach_user:
                await self.fsm.start(user)
                await self._reconcile_top_up_menu(user)
                recovered_user_ids.append(user.telegram_user_id)
                self._logger.info(
                    "missing_user_state_recovered telegram_user_id=%s state=%s",
                    user.telegram_user_id,
                    BotState.MONITORING.value,
                )
            else:
                self._states.save(UserState.new(user.telegram_user_id))
                self._logger.info(
                    "missing_user_state_marked_not_started telegram_user_id=%s",
                    user.telegram_user_id,
                )
        return recovered_user_ids

    async def _reconcile_top_up_menu(self, user: UserConfig) -> None:
        action = "configure" if user.manual_top_up is not None else "reset"
        try:
            if user.manual_top_up is not None:
                await self._telegram.configure_top_up_menu(user)
            else:
                await self._telegram.reset_top_up_menu(user)
        except Exception as error:
            self._logger.warning(
                "top_up_menu_reconciliation_failed telegram_user_id=%s "
                "action=%s error_type=%s error=%s",
                user.telegram_user_id,
                action,
                type(error).__name__,
                error,
            )

    async def restart(self, run_due_ticks: bool = True) -> list[int]:
        due_user_ids = self.due_user_ids()
        if run_due_ticks:
            for telegram_user_id in due_user_ids:
                await self.balance_tick(telegram_user_id)
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
