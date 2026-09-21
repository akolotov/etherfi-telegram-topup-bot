from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import pytest

from etherfi_bot.dispatcher import BotDispatcher
from etherfi_bot.domain import ManualTopUpConfig, UserConfig
from etherfi_bot.fsm import FsmService
from etherfi_bot.mocks import (
    MockBalanceProvider,
    MockClock,
    MockPrivateKeyProvider,
    MockSafeBalanceProvider,
    MockSafeWalletClient,
    MockTelegramGateway,
)
from etherfi_bot.storage import JsonConfigRepository, JsonStateRepository


@dataclass
class FsmHarness:
    user: UserConfig
    states: JsonStateRepository
    telegram: MockTelegramGateway
    balances: MockBalanceProvider
    safe: MockSafeWalletClient
    safe_balances: MockSafeBalanceProvider
    private_keys: MockPrivateKeyProvider
    clock: MockClock
    fsm: "AsyncTestFacade"


class AsyncTestFacade:
    """Let legacy synchronous assertions exercise an async service directly."""

    _ASYNC_METHODS = {
        "start",
        "balance_tick",
        "callback_top_up",
        "callback_ignore",
        "callback_ignore_for_24h",
        "manual_top_up_context",
        "prepare_manual_top_up",
        "callback_manual_top_up_confirm",
        "callback_manual_top_up_cancel",
        "user_blocked",
        "ignore_event",
        "recover_missing_user_states",
        "restart",
    }

    def __init__(self, delegate: Any) -> None:
        object.__setattr__(self, "delegate", delegate)

    def __getattr__(self, name: str) -> Any:
        value = getattr(self.delegate, name)
        if name not in self._ASYNC_METHODS:
            return value

        def invoke(*args: Any, **kwargs: Any) -> Any:
            coroutine = value(*args, **kwargs)
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return asyncio.run(coroutine)
            return coroutine

        return invoke

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "delegate":
            object.__setattr__(self, name, value)
        else:
            setattr(self.delegate, name, value)


def make_user(
    telegram_user_id: int = 1001,
    *,
    balance_token_address: str = "0x0000000000000000000000000000000000000001",
    threshold: Decimal | str = "10",
    max_balance: Decimal | str = "20",
    interval: int = 60,
    cooldown: int = 300,
    limit: int = 3,
    manual_top_up: ManualTopUpConfig | None = None,
) -> UserConfig:
    return UserConfig(
        telegram_user_id=telegram_user_id,
        target_account=f"0x{telegram_user_id:040x}"[-42:],
        balance_token_address=balance_token_address,
        balance_threshold=Decimal(str(threshold)),
        target_max_balance=Decimal(str(max_balance)),
        balance_check_interval_seconds=interval,
        safe_account=f"0x{telegram_user_id + 10000:040x}"[-42:],
        safe_proposer_key_file=f"./.secrets/safe_proposer_private_key_{telegram_user_id}",
        low_balance_notification_limit=limit,
        low_balance_notification_cooldown_seconds=cooldown,
        manual_top_up=manual_top_up,
    )


@pytest.fixture
def harness_factory(tmp_path: Path) -> Callable[..., FsmHarness]:
    def factory(user: UserConfig | None = None, admin_user_id: int | None = 9001) -> FsmHarness:
        user_config = user or make_user()
        states = JsonStateRepository(tmp_path / f"states-{user_config.telegram_user_id}")
        telegram = MockTelegramGateway()
        balances = MockBalanceProvider()
        safe = MockSafeWalletClient()
        safe_balances = MockSafeBalanceProvider()
        private_keys = MockPrivateKeyProvider({user_config.safe_proposer_key_file: "private-key"})
        clock = MockClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        fsm_service = FsmService(
            state_repository=states,
            telegram=telegram,
            balances=balances,
            safe_wallet=safe,
            private_keys=private_keys,
            clock=clock,
            admin_telegram_user_id=admin_user_id,
            safe_balances=safe_balances,
        )
        return FsmHarness(
            user=user_config,
            states=states,
            telegram=telegram,
            balances=balances,
            safe=safe,
            safe_balances=safe_balances,
            private_keys=private_keys,
            clock=clock,
            fsm=AsyncTestFacade(fsm_service),
        )

    return factory


def write_config(path: Path, users: list[UserConfig], admin_user_id: int | None = 9001) -> Path:
    payload = {
        "admin_telegram_user_id": admin_user_id,
        "users": [
            {
                "telegram_user_id": user.telegram_user_id,
                "target_account": user.target_account,
                "balance_token_address": user.balance_token_address,
                "balance_threshold": str(user.balance_threshold),
                "target_max_balance": str(user.target_max_balance),
                "balance_check_interval_seconds": user.balance_check_interval_seconds,
                "safe_account": user.safe_account,
                "safe_proposer_key_file": user.safe_proposer_key_file,
                "low_balance_notification_limit": user.low_balance_notification_limit,
                "low_balance_notification_cooldown_seconds": (
                    user.low_balance_notification_cooldown_seconds
                ),
                **(
                    {}
                    if user.manual_top_up is None
                    else {
                        "manual_top_up": {
                            "preset_amounts": [
                                str(amount)
                                for amount in user.manual_top_up.preset_amounts
                            ],
                            "max_custom_amount": str(
                                user.manual_top_up.max_custom_amount
                            ),
                        }
                    }
                ),
            }
            for user in users
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def make_dispatcher(
    tmp_path: Path,
    users: list[UserConfig],
    *,
    admin_user_id: int | None = 9001,
    logger: logging.Logger | None = None,
) -> tuple[
    BotDispatcher,
    JsonStateRepository,
    MockTelegramGateway,
    MockBalanceProvider,
    MockSafeWalletClient,
    MockPrivateKeyProvider,
    MockClock,
]:
    config_path = write_config(tmp_path / "config.json", users, admin_user_id)
    states = JsonStateRepository(tmp_path / "states")
    telegram = MockTelegramGateway()
    balances = MockBalanceProvider()
    safe = MockSafeWalletClient()
    safe_balances = MockSafeBalanceProvider()
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
        logger=logger,
        safe_balances=safe_balances,
    )
    return (
        AsyncTestFacade(dispatcher),
        states,
        telegram,
        balances,
        safe,
        private_keys,
        clock,
    )
