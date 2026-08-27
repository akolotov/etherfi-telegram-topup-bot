from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from etherfi_bot.domain import (
    BalanceReadError,
    SafeTxCreateError,
    SafeTxStatusReadError,
    SafeTxStatus,
    TelegramForbiddenError,
    UserConfig,
)


class MockClock:
    def __init__(self, initial: datetime | None = None) -> None:
        self._now = initial or datetime(2026, 1, 1, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = value.astimezone(timezone.utc)

    def advance(self, seconds: int) -> datetime:
        self._now = self._now + timedelta(seconds=seconds)
        return self._now


@dataclass(frozen=True)
class MockTelegramMessage:
    message_id: int
    telegram_user_id: int
    kind: str
    buttons: bool
    text: str


class MockTelegramGateway:
    def __init__(self) -> None:
        self.messages: list[MockTelegramMessage] = []
        self.removed_buttons: list[tuple[int, int]] = []
        self.admin_errors: list[tuple[int, str]] = []
        self.private_chat_checks: list[int] = []
        self.unreachable_private_chat_user_ids: set[int] = set()
        self.private_chat_check_errors: dict[int, Exception] = {}
        self.forbidden_user_ids: set[int] = set()
        self.forbidden_operations_by_user: dict[int, set[str]] = {}
        self._next_message_id = 1

    async def send_low_balance_prompt(self, user: UserConfig, balance: Decimal) -> int:
        return self._send_user(
            user.telegram_user_id,
            "low_balance_prompt",
            "send_low_balance_prompt",
            buttons=True,
            text=f"Balance is low: {balance}. Top up?",
        )

    async def send_safe_tx_created(self, user: UserConfig, safe_tx_id: str) -> int:
        return self._send_user(
            user.telegram_user_id,
            "safe_tx_created",
            "send_safe_tx_created",
            buttons=False,
            text="Safe transaction was created. Please sign and execute it.",
        )

    async def send_safe_tx_pending_prompt(self, user: UserConfig, safe_tx_id: str) -> int:
        return self._send_user(
            user.telegram_user_id,
            "safe_tx_pending_prompt",
            "send_safe_tx_pending_prompt",
            buttons=False,
            text=(
                "Balance is still low. A top-up Safe transaction is pending. "
                "Please sign and execute it."
            ),
        )

    async def send_existing_safe_tx_notice(self, user: UserConfig, safe_tx_id: str) -> int:
        return self._send_user(
            user.telegram_user_id,
            "existing_safe_tx_notice",
            "send_existing_safe_tx_notice",
            buttons=False,
            text=(
                "Balance is still low. A top-up Safe transaction is pending. "
                "Please sign and execute it."
            ),
        )

    async def remove_buttons(self, telegram_user_id: int, message_id: int) -> None:
        self._raise_if_forbidden(telegram_user_id, "remove_buttons")
        self.removed_buttons.append((int(telegram_user_id), int(message_id)))

    async def send_admin_error(self, admin_telegram_user_id: int, message: str) -> None:
        self.admin_errors.append((int(admin_telegram_user_id), message))

    async def can_reach_private_chat(self, telegram_user_id: int) -> bool:
        normalized_user_id = int(telegram_user_id)
        self.private_chat_checks.append(normalized_user_id)
        error = self.private_chat_check_errors.get(normalized_user_id)
        if error is not None:
            raise error
        return normalized_user_id not in self.unreachable_private_chat_user_ids

    def forbid_operation(self, telegram_user_id: int, operation: str) -> None:
        operations = self.forbidden_operations_by_user.setdefault(int(telegram_user_id), set())
        operations.add(operation)

    def _send_user(
        self,
        telegram_user_id: int,
        kind: str,
        operation: str,
        buttons: bool,
        text: str,
    ) -> int:
        self._raise_if_forbidden(telegram_user_id, operation)
        message_id = self._next_message_id
        self._next_message_id += 1
        self.messages.append(
            MockTelegramMessage(
                message_id=message_id,
                telegram_user_id=int(telegram_user_id),
                kind=kind,
                buttons=buttons,
                text=text,
            )
        )
        return message_id

    def _raise_if_forbidden(self, telegram_user_id: int, operation: str) -> None:
        normalized_user_id = int(telegram_user_id)
        if normalized_user_id in self.forbidden_user_ids:
            raise TelegramForbiddenError(f"Telegram 403 for {telegram_user_id}")
        if operation in self.forbidden_operations_by_user.get(normalized_user_id, set()):
            raise TelegramForbiddenError(
                f"Telegram 403 for {telegram_user_id} during {operation}"
            )


class MockBalanceProvider:
    def __init__(self) -> None:
        self.balances: dict[tuple[str, str], Decimal] = {}
        self.fail_accounts: set[str] = set()
        self.fail_balances: set[tuple[str, str]] = set()
        self.reads: list[tuple[str, str]] = []

    def set_balance(
        self,
        target_account: str,
        balance: Decimal | str | int,
        balance_token_address: str | None = None,
    ) -> None:
        token_address = balance_token_address or _default_balance_token_address()
        self.balances[(target_account, token_address)] = Decimal(str(balance))

    async def get_balance(self, user: UserConfig) -> Decimal:
        balance_key = (user.target_account, user.balance_token_address)
        self.reads.append(balance_key)
        if user.target_account in self.fail_accounts or balance_key in self.fail_balances:
            raise BalanceReadError("Could not read balance")
        return self.balances.get(balance_key, Decimal("0"))


@dataclass(frozen=True)
class MockSafeTx:
    safe_tx_id: str
    telegram_user_id: int
    safe_account: str
    recipient: str
    amount: Decimal
    private_key: str


class MockSafeWalletClient:
    def __init__(self) -> None:
        self.created_txs: list[MockSafeTx] = []
        self.statuses: dict[str, SafeTxStatus] = {}
        self.fail_create_for_users: set[int] = set()
        self.fail_status_for_txs: set[str] = set()
        self.status_checks: list[str] = []
        self._next_tx_number = 1

    async def create_top_up_tx(
        self,
        user: UserConfig,
        amount: Decimal,
        safe_proposer_private_key: str,
    ) -> str:
        if user.telegram_user_id in self.fail_create_for_users:
            raise SafeTxCreateError("Could not create Safe tx")
        safe_tx_id = f"safe-tx-{self._next_tx_number}"
        self._next_tx_number += 1
        self.created_txs.append(
            MockSafeTx(
                safe_tx_id=safe_tx_id,
                telegram_user_id=user.telegram_user_id,
                safe_account=user.safe_account,
                recipient=user.target_account,
                amount=amount,
                private_key=safe_proposer_private_key,
            )
        )
        self.statuses[safe_tx_id] = SafeTxStatus.PENDING
        return safe_tx_id

    async def get_tx_status(self, user: UserConfig, safe_tx_id: str) -> SafeTxStatus:
        self.status_checks.append(safe_tx_id)
        if safe_tx_id in self.fail_status_for_txs:
            raise SafeTxStatusReadError("Could not read Safe tx status")
        return self.statuses.get(safe_tx_id, SafeTxStatus.PENDING)


class MockPrivateKeyProvider:
    def __init__(self, private_keys: dict[str, str] | None = None) -> None:
        self.private_keys = private_keys or {}
        self.requests: list[str] = []

    async def read_private_key(self, file_path: str) -> str:
        self.requests.append(file_path)
        return self.private_keys[file_path]


def _default_balance_token_address() -> str:
    return "0x0000000000000000000000000000000000000001"
