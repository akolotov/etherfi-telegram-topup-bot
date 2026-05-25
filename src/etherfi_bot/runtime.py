from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from etherfi_bot.blockscout import (
    OPTIMISM_CHAIN_ID,
    BlockscoutBalanceProvider,
    BlockscoutErc20BalanceReader,
    BlockscoutJsonRpcClient,
)
from etherfi_bot.dispatcher import BotDispatcher
from etherfi_bot.polling import (
    JsonPollingOffsetStore,
    JsonPollingPendingUpdateStore,
    PollingBotRunner,
)
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
from etherfi_bot.telegram_api import TelegramBotApiClient, TelegramBotGateway


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass
class RuntimeComponents:
    api: TelegramBotApiClient
    gateway: TelegramBotGateway
    dispatcher: BotDispatcher
    adapter: TelegramUpdateAdapter
    runner: PollingBotRunner
    balances: BalanceProvider
    safe_wallet: SafeWalletClient
    private_keys: PrivateKeyProvider
    clock: SystemClock


def build_runtime(settings: RuntimeSettings) -> RuntimeComponents:
    config_repository = JsonConfigRepository(settings.config_path)
    config = config_repository.load()
    state_repository = JsonStateRepository(settings.state_dir)
    api = TelegramBotApiClient(
        settings.bot_token,
        base_url=settings.telegram_api_base_url,
    )
    gateway = TelegramBotGateway(api)
    private_keys = FilePrivateKeyProvider()
    for user in config.users_by_telegram_id.values():
        private_keys.read_private_key(user.safe_proposer_key_file)
    optimism_token_reader = BlockscoutErc20BalanceReader(
        BlockscoutJsonRpcClient(
            settings.blockscout_pro_api_key,
            chain_id=OPTIMISM_CHAIN_ID,
        )
    )
    optimism_token_reader.preload_decimals(
        {user.balance_token_address for user in config.users_by_telegram_id.values()}
    )
    balances = BlockscoutBalanceProvider(optimism_token_reader)
    arbitrum_token_reader = BlockscoutErc20BalanceReader(
        BlockscoutJsonRpcClient(
            settings.blockscout_pro_api_key,
            chain_id=str(ARBITRUM_CHAIN_ID),
        )
    )
    top_up_preparer = AaveV3NativeUsdcWithdrawPreparer(arbitrum_token_reader)
    safe_wallet = SafeWalletTransactionServiceClient(
        SafeTxServiceClient(
            settings.safe_transaction_service_api_key,
            base_url=settings.safe_tx_service_base_url,
        ),
        top_up_preparer,
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
    adapter = TelegramUpdateAdapter(dispatcher, callback_answerer=api)
    runner = PollingBotRunner(
        api=api,
        adapter=adapter,
        dispatcher=dispatcher,
        offset_store=JsonPollingOffsetStore(settings.polling_offset_path),
        pending_update_store=JsonPollingPendingUpdateStore(
            settings.polling_pending_update_path
        ),
        poll_timeout_seconds=settings.poll_timeout_seconds,
    )
    return RuntimeComponents(
        api=api,
        gateway=gateway,
        dispatcher=dispatcher,
        adapter=adapter,
        runner=runner,
        balances=balances,
        safe_wallet=safe_wallet,
        private_keys=private_keys,
        clock=clock,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the ether.fi Telegram bot")
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
    logger = logging.getLogger(__name__)
    if invalid_log_level is not None:
        logger.warning(
            "runtime_log_level_invalid configured_log_level=%s fallback_log_level=%s",
            invalid_log_level,
            log_level_name,
        )
    logger.info(
        "runtime_start ingress_mode=%s log_level=%s telegram_api_base_url=%s "
        "config_path=%s state_dir=%s polling_offset_path=%s "
        "polling_pending_update_path=%s poll_timeout_seconds=%s",
        settings.ingress_mode,
        log_level_name,
        settings.telegram_api_base_url,
        settings.config_path,
        settings.state_dir,
        settings.polling_offset_path,
        settings.polling_pending_update_path,
        settings.poll_timeout_seconds,
    )
    if settings.ingress_mode != "polling":
        raise RuntimeError("Only polling ingress is supported in this development phase")
    components = build_runtime(settings)
    components.runner.run_forever()


def resolve_log_level(raw_log_level: str) -> tuple[int, str, str | None]:
    normalized = str(raw_log_level).upper()
    level = getattr(logging, normalized, None)
    if isinstance(level, int):
        return level, normalized, None
    return logging.INFO, "INFO", raw_log_level


if __name__ == "__main__":
    main()
