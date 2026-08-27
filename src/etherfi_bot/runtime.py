from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, ContextTypes, TypeHandler

from etherfi_bot.blockscout import (
    OPTIMISM_CHAIN_ID,
    BlockscoutBalanceProvider,
    BlockscoutErc20BalanceReader,
    BlockscoutJsonRpcClient,
)
from etherfi_bot.dispatcher import BotDispatcher
from etherfi_bot.ports import BalanceProvider, PrivateKeyProvider, SafeWalletClient
from etherfi_bot.private_keys import FilePrivateKeyProvider
from etherfi_bot.safe_tx_preparers import AaveV3NativeUsdcWithdrawPreparer
from etherfi_bot.safe_wallet import (
    ARBITRUM_CHAIN_ID,
    SafeTxServiceClient,
    SafeWalletTransactionServiceClient,
)
from etherfi_bot.settings import RuntimeSettings
from etherfi_bot.storage import JsonConfigRepository, JsonStateRepository
from etherfi_bot.telegram_adapter import TelegramUpdateAdapter
from etherfi_bot.telegram_api import TelegramBotGateway


ALLOWED_UPDATES = ["message", "callback_query", "my_chat_member", "message_reaction"]


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass
class RuntimeComponents:
    application: Application
    gateway: TelegramBotGateway
    dispatcher: BotDispatcher
    adapter: TelegramUpdateAdapter
    balances: BalanceProvider
    safe_wallet: SafeWalletClient
    private_keys: PrivateKeyProvider
    clock: SystemClock
    optimism_rpc: BlockscoutJsonRpcClient
    arbitrum_rpc: BlockscoutJsonRpcClient
    safe_tx_service: SafeTxServiceClient
    token_readers: tuple[BlockscoutErc20BalanceReader, ...]
    configured_balance_tokens: tuple[str, ...]
    configured_key_files: tuple[str, ...]
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger(__name__))
    _wake_scheduler: asyncio.Event = field(default_factory=asyncio.Event)
    _stop_scheduler: asyncio.Event = field(default_factory=asyncio.Event)
    _scheduler_task: asyncio.Task[None] | None = None

    async def handle_update(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        try:
            action = await self.adapter.handle_update(update, context)
        finally:
            self._wake_scheduler.set()
        self.logger.info(
            "ingress_update accepted telegram_update_id=%s action=%s",
            update.update_id,
            action,
        )

    async def handle_error(
        self, update: object, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        self.logger.error(
            "telegram_update_processing_failed telegram_update_id=%s "
            "error_type=%s error=%s",
            getattr(update, "update_id", None),
            type(context.error).__name__,
            context.error,
            exc_info=context.error,
        )

    async def startup(self, _application: Application | None = None) -> None:
        await asyncio.gather(
            *(self.private_keys.read_private_key(path) for path in self.configured_key_files)
        )
        await self.token_readers[0].preload_decimals(
            self.configured_balance_tokens
        )
        recovered_user_ids = await self.dispatcher.recover_missing_user_states()
        me = await self.application.bot.get_me()
        self.logger.info(
            "telegram_runtime_ready bot_username=%s recovered_user_count=%s",
            me.username,
            len(recovered_user_ids),
        )
        self._stop_scheduler.clear()
        self._wake_scheduler.set()
        self._scheduler_task = asyncio.create_task(
            self._run_scheduler(), name="telegram-balance-scheduler"
        )

    async def stop(self, _application: Application | None = None) -> None:
        self._stop_scheduler.set()
        self._wake_scheduler.set()
        if self._scheduler_task is not None:
            await self._scheduler_task
            self._scheduler_task = None

    async def shutdown(self, _application: Application | None = None) -> None:
        # post_stop is skipped when PTB fails before Application.start(), but
        # post_shutdown still runs. Keep scheduler cleanup safe in both paths.
        await self.stop()
        await asyncio.gather(
            self.optimism_rpc.aclose(),
            self.arbitrum_rpc.aclose(),
            self.safe_tx_service.aclose(),
        )

    async def process_due_ticks(self) -> int:
        due_user_ids = self.dispatcher.due_user_ids()

        async def process(telegram_user_id: int) -> None:
            try:
                state = await self.dispatcher.balance_tick(telegram_user_id)
            except Exception as error:
                self.logger.exception(
                    "balance_tick failed telegram_user_id=%s error_type=%s error=%s",
                    telegram_user_id,
                    type(error).__name__,
                    error,
                )
            else:
                self.logger.debug(
                    "balance_tick processed telegram_user_id=%s state=%s",
                    telegram_user_id,
                    None if state is None else state.state.value,
                )

        await asyncio.gather(*(process(user_id) for user_id in due_user_ids))
        return len(due_user_ids)

    async def _run_scheduler(self) -> None:
        while not self._stop_scheduler.is_set():
            try:
                await self.process_due_ticks()
                self._wake_scheduler.clear()
                seconds = self.dispatcher.seconds_until_next_due_tick()
                timeout = 60.0 if seconds is None else max(1.0, float(seconds))
            except Exception as error:
                self.logger.exception(
                    "telegram_scheduler_failed error_type=%s error=%s",
                    type(error).__name__,
                    error,
                )
                timeout = 5.0
            try:
                await asyncio.wait_for(self._wake_scheduler.wait(), timeout=timeout)
            except TimeoutError:
                pass

def build_runtime(settings: RuntimeSettings) -> RuntimeComponents:
    config_repository = JsonConfigRepository(settings.config_path)
    config = config_repository.load()
    state_repository = JsonStateRepository(settings.state_dir)

    builder = ApplicationBuilder().token(settings.bot_token).concurrent_updates(False)
    if settings.telegram_api_base_url.rstrip("/") != "https://api.telegram.org":
        builder = builder.base_url(
            f"{settings.telegram_api_base_url.rstrip('/')}/bot"
        ).base_file_url(f"{settings.telegram_api_base_url.rstrip('/')}/file/bot")
    application = builder.build()
    gateway = TelegramBotGateway(application.bot)
    private_keys = FilePrivateKeyProvider()

    optimism_rpc = BlockscoutJsonRpcClient(
        settings.blockscout_pro_api_key,
        chain_id=OPTIMISM_CHAIN_ID,
        max_attempts=settings.blockscout_max_attempts,
        retry_initial_delay_seconds=settings.blockscout_retry_initial_delay_seconds,
        retry_backoff_factor=settings.blockscout_retry_backoff_factor,
    )
    optimism_token_reader = BlockscoutErc20BalanceReader(optimism_rpc)
    balances = BlockscoutBalanceProvider(optimism_token_reader)

    arbitrum_rpc = BlockscoutJsonRpcClient(
        settings.blockscout_pro_api_key,
        chain_id=str(ARBITRUM_CHAIN_ID),
        max_attempts=settings.blockscout_max_attempts,
        retry_initial_delay_seconds=settings.blockscout_retry_initial_delay_seconds,
        retry_backoff_factor=settings.blockscout_retry_backoff_factor,
    )
    arbitrum_token_reader = BlockscoutErc20BalanceReader(arbitrum_rpc)
    safe_tx_service = SafeTxServiceClient(
        settings.safe_transaction_service_api_key,
        base_url=settings.safe_tx_service_base_url,
    )
    safe_wallet = SafeWalletTransactionServiceClient(
        safe_tx_service,
        AaveV3NativeUsdcWithdrawPreparer(arbitrum_token_reader),
    )
    clock = SystemClock()
    dispatcher = BotDispatcher(
        config_repository=config_repository,
        state_repository=state_repository,
        telegram=gateway,
        balances=balances,
        safe_wallet=safe_wallet,
        private_keys=private_keys,
        clock=clock,
    )
    adapter = TelegramUpdateAdapter(dispatcher)
    components = RuntimeComponents(
        application=application,
        gateway=gateway,
        dispatcher=dispatcher,
        adapter=adapter,
        balances=balances,
        safe_wallet=safe_wallet,
        private_keys=private_keys,
        clock=clock,
        optimism_rpc=optimism_rpc,
        arbitrum_rpc=arbitrum_rpc,
        safe_tx_service=safe_tx_service,
        token_readers=(optimism_token_reader, arbitrum_token_reader),
        configured_balance_tokens=tuple(
            {user.balance_token_address for user in config.users_by_telegram_id.values()}
        ),
        configured_key_files=tuple(
            user.safe_proposer_key_file for user in config.users_by_telegram_id.values()
        ),
    )
    application.add_handler(TypeHandler(Update, components.handle_update))
    application.add_error_handler(components.handle_error)
    return components


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the ether.fi Telegram top-up bot")
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to the dotenv-style runtime configuration file",
    )
    args = parser.parse_args(argv)
    settings = RuntimeSettings.from_env_file(args.env_file)
    log_level, log_level_name, invalid_log_level = resolve_log_level(settings.log_level)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
    # httpx includes complete request URLs in INFO messages. Telegram embeds the
    # bot token in those URLs, and PTB includes credentials in DEBUG
    # messages, so these loggers must not inherit the configured root level.
    _configure_sensitive_dependency_logging()
    logger = logging.getLogger(__name__)
    if invalid_log_level is not None:
        logger.warning(
            "runtime_log_level_invalid configured_log_level=%s fallback_log_level=%s",
            invalid_log_level,
            log_level_name,
        )
    components = build_runtime(settings)
    run_runtime(components, settings)


def run_runtime(components: RuntimeComponents, settings: RuntimeSettings) -> None:
    """Run PTB with its native polling or webhook ingress."""

    components.application.post_init = components.startup
    components.application.post_stop = components.stop
    components.application.post_shutdown = components.shutdown
    if settings.ingress_mode == "polling":
        components.application.run_polling(
            allowed_updates=ALLOWED_UPDATES,
            drop_pending_updates=False,
        )
        return
    assert settings.webhook_secret_token is not None
    components.application.run_webhook(
        listen=settings.webhook_listen_host,
        port=settings.webhook_listen_port,
        url_path=settings.webhook_path.lstrip("/"),
        webhook_url=settings.webhook_url,
        allowed_updates=ALLOWED_UPDATES,
        drop_pending_updates=False,
        max_connections=1,
        secret_token=settings.webhook_secret_token,
    )


def resolve_log_level(raw_log_level: str) -> tuple[int, str, str | None]:
    normalized = str(raw_log_level).upper()
    level = getattr(logging, normalized, None)
    if isinstance(level, int):
        return level, normalized, None
    return logging.INFO, "INFO", raw_log_level


def _configure_sensitive_dependency_logging() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)


if __name__ == "__main__":
    main()
