from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any


class BotState(str, Enum):
    NOT_STARTED = "S0_NOT_STARTED"
    MONITORING = "S1_MONITORING"
    LOW_PROMPT = "S2_LOW_PROMPT"
    LOW_COOLDOWN = "S3_LOW_COOLDOWN"
    SAFE_TX_PENDING = "S4_SAFE_TX_PENDING"


class SafeTxStatus(str, Enum):
    PENDING = "PENDING"
    FINAL = "FINAL"


class TelegramForbiddenError(RuntimeError):
    """Telegram returned 403 for a user-facing operation."""


class BalanceReadError(RuntimeError):
    """The target account balance could not be read."""


class SafeTxCreateError(RuntimeError):
    """The Safe transaction could not be created or registered."""


class InsufficientSafeBalanceError(SafeTxCreateError):
    """The Safe does not have enough balance to fund the requested top-up."""


class SafeTxStatusReadError(RuntimeError):
    """The Safe transaction status could not be read."""


class ManualTopUpError(RuntimeError):
    """A manual top-up request cannot be prepared or confirmed."""


@dataclass(frozen=True)
class ManualTopUpConfig:
    preset_amounts: tuple[Decimal, ...]
    max_custom_amount: Decimal

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ManualTopUpConfig":
        if not isinstance(data, dict):
            raise ValueError("manual_top_up must be an object")
        raw_presets = data.get("preset_amounts")
        if not isinstance(raw_presets, list) or not raw_presets:
            raise ValueError("manual_top_up.preset_amounts must be a non-empty list")
        config = cls(
            preset_amounts=tuple(Decimal(str(value)) for value in raw_presets),
            max_custom_amount=Decimal(str(data["max_custom_amount"])),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.max_custom_amount <= 0:
            raise ValueError("manual_top_up.max_custom_amount must be > 0")
        if len(set(self.preset_amounts)) != len(self.preset_amounts):
            raise ValueError("manual_top_up.preset_amounts must be unique")
        for amount in (*self.preset_amounts, self.max_custom_amount):
            if amount <= 0:
                raise ValueError("manual_top_up amounts must be > 0")
            if amount.as_tuple().exponent < -6:
                raise ValueError("manual_top_up amounts support at most 6 decimals")
        if any(amount > self.max_custom_amount for amount in self.preset_amounts):
            raise ValueError(
                "manual_top_up preset amounts must be <= max_custom_amount"
            )


@dataclass(frozen=True)
class ManualTopUpContext:
    target_balance: Decimal
    safe_balance: Decimal
    maximum_amount: Decimal
    preset_amounts: tuple[Decimal, ...]
    target_account: str
    safe_account: str


@dataclass(frozen=True)
class UserConfig:
    telegram_user_id: int
    target_account: str
    balance_token_address: str
    balance_threshold: Decimal
    target_max_balance: Decimal
    balance_check_interval_seconds: int
    safe_account: str
    safe_proposer_key_file: str
    low_balance_notification_limit: int
    low_balance_notification_cooldown_seconds: int
    manual_top_up: ManualTopUpConfig | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UserConfig":
        balance_token_address = data["balance_token_address"]
        if not isinstance(balance_token_address, str) or not balance_token_address.strip():
            raise ValueError("balance_token_address must be a non-empty string")
        safe_proposer_key_file = data["safe_proposer_key_file"]
        if (
            not isinstance(safe_proposer_key_file, str)
            or not safe_proposer_key_file.strip()
        ):
            raise ValueError("safe_proposer_key_file must be a non-empty string")
        raw_manual_top_up = data.get("manual_top_up")
        config = cls(
            telegram_user_id=int(data["telegram_user_id"]),
            target_account=str(data["target_account"]),
            balance_token_address=balance_token_address.strip(),
            balance_threshold=Decimal(str(data["balance_threshold"])),
            target_max_balance=Decimal(str(data["target_max_balance"])),
            balance_check_interval_seconds=int(data["balance_check_interval_seconds"]),
            safe_account=str(data["safe_account"]),
            safe_proposer_key_file=safe_proposer_key_file.strip(),
            low_balance_notification_limit=int(data["low_balance_notification_limit"]),
            low_balance_notification_cooldown_seconds=int(
                data["low_balance_notification_cooldown_seconds"]
            ),
            manual_top_up=None
            if raw_manual_top_up is None
            else ManualTopUpConfig.from_dict(raw_manual_top_up),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if (
            not isinstance(self.balance_token_address, str)
            or not self.balance_token_address.strip()
        ):
            raise ValueError("balance_token_address must be a non-empty string")
        if (
            not isinstance(self.safe_proposer_key_file, str)
            or not self.safe_proposer_key_file.strip()
        ):
            raise ValueError("safe_proposer_key_file must be a non-empty string")
        if self.low_balance_notification_limit < 1:
            raise ValueError("low_balance_notification_limit must be >= 1")
        if self.balance_check_interval_seconds <= 0:
            raise ValueError("balance_check_interval_seconds must be > 0")
        if self.low_balance_notification_cooldown_seconds <= 0:
            raise ValueError("low_balance_notification_cooldown_seconds must be > 0")
        if self.balance_threshold < 0:
            raise ValueError("balance_threshold must be >= 0")
        if self.target_max_balance < 0:
            raise ValueError("target_max_balance must be >= 0")
        if self.target_max_balance < self.balance_threshold:
            raise ValueError("target_max_balance must be >= balance_threshold")
        if self.manual_top_up is not None:
            self.manual_top_up.validate()


@dataclass(frozen=True)
class BotConfig:
    admin_telegram_user_id: int | None
    users_by_telegram_id: dict[int, UserConfig]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BotConfig":
        users = [UserConfig.from_dict(item) for item in data.get("users", [])]
        return cls(
            admin_telegram_user_id=data.get("admin_telegram_user_id"),
            users_by_telegram_id={user.telegram_user_id: user for user in users},
        )

    def user(self, telegram_user_id: int) -> UserConfig | None:
        return self.users_by_telegram_id.get(int(telegram_user_id))


@dataclass
class UserState:
    telegram_user_id: int
    state: BotState = BotState.NOT_STARTED
    notification_count: int = 0
    low_cooldown_until: datetime | None = None
    low_balance_snoozed_until: datetime | None = None
    tx_reminder_until: datetime | None = None
    current_message_id: int | None = None
    pending_safe_tx_id: str | None = None
    last_balance_checked_at: datetime | None = None
    next_tick_at: datetime | None = None
    last_balance: Decimal | None = None
    low_balance_drop_admin_notified: bool = False
    manual_top_up_request_id: str | None = None
    manual_top_up_amount: Decimal | None = None
    manual_top_up_expires_at: datetime | None = None
    manual_top_up_message_id: int | None = None

    @classmethod
    def new(cls, telegram_user_id: int) -> "UserState":
        return cls(telegram_user_id=int(telegram_user_id))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UserState":
        state = parse_bot_state(data.get("state", BotState.NOT_STARTED.value))
        return cls(
            telegram_user_id=int(data["telegram_user_id"]),
            state=state,
            notification_count=int(data.get("notification_count", 0)),
            low_cooldown_until=parse_datetime(data.get("low_cooldown_until")),
            low_balance_snoozed_until=parse_datetime(
                data.get("low_balance_snoozed_until")
            ),
            tx_reminder_until=parse_datetime(data.get("tx_reminder_until")),
            current_message_id=None
            if state is BotState.SAFE_TX_PENDING
            else data.get("current_message_id"),
            pending_safe_tx_id=data.get("pending_safe_tx_id"),
            last_balance_checked_at=parse_datetime(data.get("last_balance_checked_at")),
            next_tick_at=parse_datetime(data.get("next_tick_at")),
            last_balance=parse_decimal(data.get("last_balance")),
            low_balance_drop_admin_notified=bool(
                data.get("low_balance_drop_admin_notified", False)
            ),
            manual_top_up_request_id=data.get("manual_top_up_request_id"),
            manual_top_up_amount=parse_decimal(data.get("manual_top_up_amount")),
            manual_top_up_expires_at=parse_datetime(
                data.get("manual_top_up_expires_at")
            ),
            manual_top_up_message_id=data.get("manual_top_up_message_id"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "telegram_user_id": self.telegram_user_id,
            "state": self.state.value,
            "notification_count": self.notification_count,
            "low_cooldown_until": format_datetime(self.low_cooldown_until),
            "low_balance_snoozed_until": format_datetime(
                self.low_balance_snoozed_until
            ),
            "tx_reminder_until": format_datetime(self.tx_reminder_until),
            "current_message_id": self.current_message_id,
            "pending_safe_tx_id": self.pending_safe_tx_id,
            "last_balance_checked_at": format_datetime(self.last_balance_checked_at),
            "next_tick_at": format_datetime(self.next_tick_at),
            "last_balance": format_decimal(self.last_balance),
            "low_balance_drop_admin_notified": self.low_balance_drop_admin_notified,
            "manual_top_up_request_id": self.manual_top_up_request_id,
            "manual_top_up_amount": format_decimal(self.manual_top_up_amount),
            "manual_top_up_expires_at": format_datetime(
                self.manual_top_up_expires_at
            ),
            "manual_top_up_message_id": self.manual_top_up_message_id,
        }

    def reset_runtime(self) -> None:
        self.state = BotState.NOT_STARTED
        self.notification_count = 0
        self.low_cooldown_until = None
        self.low_balance_snoozed_until = None
        self.tx_reminder_until = None
        self.current_message_id = None
        self.pending_safe_tx_id = None
        self.last_balance_checked_at = None
        self.next_tick_at = None
        self.last_balance = None
        self.low_balance_drop_admin_notified = False
        self.clear_manual_top_up()

    def clear_manual_top_up(self) -> None:
        self.manual_top_up_request_id = None
        self.manual_top_up_amount = None
        self.manual_top_up_expires_at = None
        self.manual_top_up_message_id = None


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_bot_state(value: Any) -> BotState:
    if value == "S5_SAFE_TX_REMINDER":
        return BotState.SAFE_TX_PENDING
    return BotState(value)


def parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    normalized = value.replace("Z", "+00:00")
    return ensure_utc(datetime.fromisoformat(normalized))


def format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return ensure_utc(value).isoformat()


def parse_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def format_decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value)
