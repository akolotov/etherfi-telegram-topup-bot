from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol

from etherfi_bot.domain import BotConfig, SafeTxStatus, UserConfig, UserState


class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current timezone-aware UTC datetime."""


class TelegramGateway(Protocol):
    def send_low_balance_prompt(self, user: UserConfig, balance: Decimal) -> int:
        """Send a low-balance message with Top Up and Ignore buttons."""

    def send_safe_tx_created(self, user: UserConfig, safe_tx_id: str) -> int:
        """Notify the user that a Safe transaction exists and needs signatures."""

    def send_safe_tx_pending_prompt(self, user: UserConfig, safe_tx_id: str) -> int:
        """Send a pending Safe transaction reminder without inline buttons."""

    def send_existing_safe_tx_notice(self, user: UserConfig, safe_tx_id: str) -> int:
        """Remind the user about an already pending Safe transaction."""

    def remove_buttons(self, telegram_user_id: int, message_id: int) -> None:
        """Remove inline buttons from a previously sent message."""

    def send_admin_error(self, admin_telegram_user_id: int, message: str) -> None:
        """Send an operational error to the configured admin account."""

    def can_reach_private_chat(self, telegram_user_id: int) -> bool:
        """Return whether Telegram currently exposes this private chat to the bot."""


class BalanceProvider(Protocol):
    def get_balance(self, user: UserConfig) -> Decimal:
        """Read the configured token balance for the target account in Optimism."""


class SafeWalletClient(Protocol):
    def create_top_up_tx(
        self,
        user: UserConfig,
        amount: Decimal,
        safe_proposer_private_key: str,
    ) -> str:
        """Create or register a Safe transaction in Arbitrum."""

    def get_tx_status(self, user: UserConfig, safe_tx_id: str) -> SafeTxStatus:
        """Return whether the Safe transaction is pending or final."""


class PrivateKeyProvider(Protocol):
    def read_private_key(self, file_path: str) -> str:
        """Read a Safe proposer private key from a configured file path."""


class ConfigRepository(Protocol):
    def load(self) -> BotConfig:
        """Load bot configuration."""


class StateRepository(Protocol):
    def load(self, telegram_user_id: int) -> UserState:
        """Load state for one Telegram user or return a default S0 state."""

    def save(self, state: UserState) -> None:
        """Persist one user's FSM state."""

    def list_states(self) -> list[UserState]:
        """Load all persisted states."""
