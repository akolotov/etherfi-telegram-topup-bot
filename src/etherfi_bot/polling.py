from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

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


class JsonPollingPendingUpdateStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def load(self) -> dict[str, Any] | None:
        if not self._path.exists():
            return None
        data = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError(
                f"Pending Telegram update at {self._path} must be a JSON object"
            )
        return data

    def save(self, update: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(update, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp_path.replace(self._path)

    def clear(self) -> None:
        try:
            self._path.unlink()
        except FileNotFoundError:
            return


class PollingBotRunner:
    def __init__(
        self,
        *,
        api: TelegramBotApiClient,
        adapter: TelegramUpdateAdapter,
        dispatcher: BotDispatcher,
        offset_store: JsonPollingOffsetStore,
        pending_update_store: JsonPollingPendingUpdateStore | None = None,
        poll_timeout_seconds: int = 25,
        idle_sleep_seconds: float = 1.0,
        error_sleep_seconds: float = 5.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self._api = api
        self._adapter = adapter
        self._dispatcher = dispatcher
        self._offset_store = offset_store
        self._pending_update_store = pending_update_store
        self._poll_timeout_seconds = poll_timeout_seconds
        self._idle_sleep_seconds = idle_sleep_seconds
        self._error_sleep_seconds = error_sleep_seconds
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
            try:
                self.process_once()
                time.sleep(self._idle_sleep_seconds)
            except Exception as error:
                self._logger.exception(
                    "polling_loop_error error_type=%s error=%s",
                    type(error).__name__,
                    error,
                )
                time.sleep(self._error_sleep_seconds)

    def process_once(self) -> int:
        if self._pending_update_store is not None:
            pending_update = self._pending_update_store.load()
            if pending_update is not None:
                update_id = int(pending_update["update_id"])
                self._save_next_offset(update_id)
                self._process_update(pending_update, source="pending")
                self._pending_update_store.clear()
                self.process_due_ticks()
                return 1

        self.process_due_ticks()
        poll_timeout_seconds = self._poll_timeout_for_next_request()
        updates = self._api.get_updates(
            offset=self._offset,
            timeout_seconds=poll_timeout_seconds,
            allowed_updates=ALLOWED_UPDATES,
        )
        processed_count = 0
        for update in updates:
            update_id = int(update["update_id"])
            if self._pending_update_store is not None:
                self._pending_update_store.save(update)
            self._save_next_offset(update_id)
            self._process_update(update, source="telegram")
            if self._pending_update_store is not None:
                self._pending_update_store.clear()
            processed_count += 1

        self.process_due_ticks()
        return processed_count

    def process_due_ticks(self) -> int:
        processed_count = 0
        for telegram_user_id in self._dispatcher.due_user_ids():
            try:
                state = self._dispatcher.balance_tick(telegram_user_id)
            except Exception as error:
                self._logger.exception(
                    "balance_tick failed telegram_user_id=%s error_type=%s error=%s",
                    telegram_user_id,
                    type(error).__name__,
                    error,
                )
                processed_count += 1
                continue
            processed_count += 1
            self._logger.debug(
                "balance_tick processed telegram_user_id=%s state=%s",
                telegram_user_id,
                None if state is None else state.state.value,
            )
        return processed_count

    def _process_update(self, update: dict[str, Any], *, source: str) -> None:
        update_id = int(update["update_id"])
        try:
            action = self._adapter.handle_update(update)
        except Exception as error:
            self._logger.exception(
                "ingress_update failed ingress_mode=polling source=%s telegram_update_id=%s "
                "next_polling_offset=%s error_type=%s error=%s",
                source,
                update_id,
                self._offset,
                type(error).__name__,
                error,
            )
        else:
            self._logger.info(
                "ingress_update accepted ingress_mode=polling source=%s telegram_update_id=%s "
                "action=%s next_polling_offset=%s",
                source,
                update_id,
                action,
                self._offset,
            )

    def _save_next_offset(self, update_id: int) -> None:
        self._offset = max(self._offset, int(update_id) + 1)
        self._offset_store.save(self._offset)

    def _poll_timeout_for_next_request(self) -> int:
        seconds_until_due = self._dispatcher.seconds_until_next_due_tick()
        if seconds_until_due is None:
            return self._poll_timeout_seconds
        return min(self._poll_timeout_seconds, max(0, seconds_until_due))
