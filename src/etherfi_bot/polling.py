from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from etherfi_bot.dispatcher import BotDispatcher
from etherfi_bot.telegram_adapter import TelegramUpdateAdapter
from etherfi_bot.telegram_api import TelegramBotApiClient

ALLOWED_UPDATES = ["message", "callback_query", "my_chat_member", "message_reaction"]


class JsonPollingOffsetStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def load(self) -> int:
        if not self._path.exists():
            return 0
        data = json.loads(self._path.read_text(encoding="utf-8"))
        return int(data.get("offset", 0))

    def save(self, offset: int) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps({"offset": int(offset)}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp_path.replace(self._path)


class PollingBotRunner:
    def __init__(
        self,
        *,
        api: TelegramBotApiClient,
        adapter: TelegramUpdateAdapter,
        dispatcher: BotDispatcher,
        offset_store: JsonPollingOffsetStore,
        poll_timeout_seconds: int = 25,
        idle_sleep_seconds: float = 1.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self._api = api
        self._adapter = adapter
        self._dispatcher = dispatcher
        self._offset_store = offset_store
        self._poll_timeout_seconds = poll_timeout_seconds
        self._idle_sleep_seconds = idle_sleep_seconds
        self._logger = logger or logging.getLogger(__name__)
        self._offset = self._offset_store.load()
        self._started = False

    def setup(self) -> None:
        self._api.delete_webhook(drop_pending_updates=False)
        me = self._api.get_me()
        self._logger.info(
            "polling_setup ingress_mode=polling bot_username=%s polling_offset=%s "
            "poll_timeout_seconds=%s allowed_updates=%s",
            me.get("username"),
            self._offset,
            self._poll_timeout_seconds,
            ",".join(ALLOWED_UPDATES),
        )
        self._started = True

    def run_forever(self) -> None:
        if not self._started:
            self.setup()
        while True:
            self.process_once()
            time.sleep(self._idle_sleep_seconds)

    def process_once(self) -> int:
        updates = self._api.get_updates(
            offset=self._offset,
            timeout_seconds=self._poll_timeout_seconds,
            allowed_updates=ALLOWED_UPDATES,
        )
        processed_count = 0
        for update in updates:
            update_id = int(update["update_id"])
            action = self._adapter.handle_update(update)
            processed_count += 1
            self._offset = max(self._offset, update_id + 1)
            self._offset_store.save(self._offset)
            self._logger.info(
                "ingress_update accepted ingress_mode=polling telegram_update_id=%s "
                "action=%s next_polling_offset=%s",
                update_id,
                action,
                self._offset,
            )

        for telegram_user_id in self._dispatcher.due_user_ids():
            state = self._dispatcher.balance_tick(telegram_user_id)
            self._logger.debug(
                "balance_tick processed telegram_user_id=%s state=%s",
                telegram_user_id,
                None if state is None else state.state.value,
            )
        return processed_count
