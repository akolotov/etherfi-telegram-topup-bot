from __future__ import annotations

import copy
import json
import socket
import threading
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from etherfi_bot.dispatcher import BotDispatcher
from etherfi_bot.domain import BotState
from etherfi_bot.mocks import (
    MockBalanceProvider,
    MockClock,
    MockKeychain,
    MockSafeWalletClient,
    MockTelegramGateway,
)
from etherfi_bot.polling import (
    JsonPollingOffsetStore,
    JsonPollingPendingUpdateStore,
    PollingBotRunner,
)
from etherfi_bot.storage import JsonConfigRepository, JsonStateRepository
from etherfi_bot.telegram_adapter import TelegramUpdateAdapter
from etherfi_bot.telegram_api import TelegramApiError, TelegramBotApiClient, TelegramBotGateway
from tests.conftest import make_dispatcher, make_user, write_config
from tests.smoke.telegram_api_mock import create_server

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "telegram_updates"


def test_polling_runtime_smoke_covers_low_balance_top_up_and_cooldowns(tmp_path: Path) -> None:
    server = create_server(
        "127.0.0.1",
        _free_port(),
        state_path=tmp_path / "telegram-mock-state.json",
        bot_username="etherfi_test_bot",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        _post_json(base_url, "/__admin/reset", {})
        user = make_user(telegram_user_id=1001, limit=3, interval=60, cooldown=300)
        config_path = write_config(tmp_path / "config.json", [user])
        states = JsonStateRepository(tmp_path / "states")
        api = TelegramBotApiClient("123:ABC", base_url=base_url)
        gateway = TelegramBotGateway(api)
        balances = MockBalanceProvider()
        safe = MockSafeWalletClient()
        keychain = MockKeychain({user.safe_owner_key_ref: "private-key"})
        clock = MockClock()
        dispatcher = BotDispatcher(
            config_repository=JsonConfigRepository(config_path),
            state_repository=states,
            telegram=gateway,
            balances=balances,
            safe_wallet=safe,
            keychain=keychain,
            clock=clock,
        )
        adapter = TelegramUpdateAdapter(dispatcher, callback_answerer=api)
        runner = PollingBotRunner(
            api=api,
            adapter=adapter,
            dispatcher=dispatcher,
            offset_store=JsonPollingOffsetStore(tmp_path / "polling-offset.json"),
            pending_update_store=JsonPollingPendingUpdateStore(
                tmp_path / "polling-pending-update.json"
            ),
            poll_timeout_seconds=0,
        )
        runner.setup()

        _post_json(base_url, "/__admin/enqueue", _load_fixture("message_command_start"))
        assert runner.process_once() == 1
        assert states.load(user.telegram_user_id).state is BotState.MONITORING
        requests = _api_state(base_url)["requests"]
        assert [request["method"] for request in requests[:3]] == [
            "deleteWebhook",
            "getMe",
            "getUpdates",
        ]

        _run_due_tick(runner, clock, user.balance_check_interval_seconds)
        first_low_state = states.load(user.telegram_user_id)
        assert first_low_state.state is BotState.LOW_PROMPT
        assert first_low_state.notification_count == 1
        first_low_payload = _last_outbound(base_url, "sendMessage")["payload"]
        assert first_low_payload["chat_id"] == user.telegram_user_id
        assert first_low_payload["reply_markup"]["inline_keyboard"][0] == [
            {"text": "Top Up", "callback_data": "top_up"},
            {"text": "Ignore", "callback_data": "ignore"},
        ]

        _run_due_tick(runner, clock, user.balance_check_interval_seconds)
        second_low_state = states.load(user.telegram_user_id)
        assert second_low_state.state is BotState.LOW_PROMPT
        assert second_low_state.notification_count == 2
        assert "reply_markup" not in _last_outbound(
            base_url,
            "editMessageReplyMarkup",
        )["payload"]

        _run_due_tick(runner, clock, user.balance_check_interval_seconds)
        cooldown_state = states.load(user.telegram_user_id)
        assert cooldown_state.state is BotState.LOW_COOLDOWN
        assert cooldown_state.notification_count == 3
        low_prompt_count = _outbound_method_count(base_url, "sendMessage")

        _run_due_tick(runner, clock, user.balance_check_interval_seconds)
        assert states.load(user.telegram_user_id).state is BotState.LOW_COOLDOWN
        assert _outbound_method_count(base_url, "sendMessage") == low_prompt_count

        top_up_update = _callback_update(
            "callback_query_top_up",
            update_id=100000020,
            callback_id="smoke-top-up",
            message_id=states.load(user.telegram_user_id).current_message_id,
        )
        _post_json(base_url, "/__admin/enqueue", top_up_update)
        assert runner.process_once() == 1

        pending_state = states.load(user.telegram_user_id)
        assert pending_state.state is BotState.SAFE_TX_PENDING
        assert pending_state.pending_safe_tx_id == "safe-tx-1"
        assert len(safe.created_txs) == 1
        assert _last_outbound(base_url, "answerCallbackQuery")["payload"] == {
            "callback_query_id": "smoke-top-up"
        }
        safe_created_payload = _last_outbound(base_url, "sendMessage")["payload"]
        assert "Safe transaction safe-tx-1 was created" in safe_created_payload["text"]

        send_count_after_created = _outbound_method_count(base_url, "sendMessage")
        _run_due_tick(runner, clock, user.low_balance_notification_cooldown_seconds)
        reminder_state = states.load(user.telegram_user_id)
        assert reminder_state.state is BotState.SAFE_TX_PENDING
        assert reminder_state.pending_safe_tx_id == "safe-tx-1"
        assert _outbound_method_count(base_url, "sendMessage") == send_count_after_created + 1
        assert "still pending" in _last_outbound(base_url, "sendMessage")["payload"]["text"]

        send_count_after_reminder = _outbound_method_count(base_url, "sendMessage")
        _run_due_tick(runner, clock, user.balance_check_interval_seconds)
        assert _outbound_method_count(base_url, "sendMessage") == send_count_after_reminder
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_process_once_runs_due_ticks_before_get_updates(tmp_path: Path) -> None:
    user = make_user(telegram_user_id=1001, interval=60)
    dispatcher, states, telegram, balances, *_rest, clock = _make_polling_dispatcher(
        tmp_path,
        user,
    )
    dispatcher.start(user.telegram_user_id)
    clock.advance(user.balance_check_interval_seconds)
    balances.set_balance(user.target_account, "1", user.balance_token_address)
    api = RecordingPollingApi()
    runner = PollingBotRunner(
        api=api,
        adapter=TelegramUpdateAdapter(dispatcher),
        dispatcher=dispatcher,
        offset_store=JsonPollingOffsetStore(tmp_path / "polling-offset.json"),
        pending_update_store=JsonPollingPendingUpdateStore(
            tmp_path / "polling-pending-update.json"
        ),
        poll_timeout_seconds=25,
    )

    runner.process_once()

    assert api.calls == ["getUpdates"]
    assert states.load(user.telegram_user_id).state is BotState.LOW_PROMPT
    assert telegram.messages[-1].kind == "low_balance_prompt"


def test_process_once_caps_get_updates_timeout_by_next_due_tick(tmp_path: Path) -> None:
    user = make_user(telegram_user_id=1001, interval=60)
    dispatcher, _states, _telegram, _balances, *_rest, clock = _make_polling_dispatcher(
        tmp_path,
        user,
    )
    dispatcher.start(user.telegram_user_id)
    clock.advance(55)
    api = RecordingPollingApi()
    runner = PollingBotRunner(
        api=api,
        adapter=TelegramUpdateAdapter(dispatcher),
        dispatcher=dispatcher,
        offset_store=JsonPollingOffsetStore(tmp_path / "polling-offset.json"),
        pending_update_store=JsonPollingPendingUpdateStore(
            tmp_path / "polling-pending-update.json"
        ),
        poll_timeout_seconds=25,
    )

    runner.process_once()

    assert api.timeouts == [5]


def test_process_once_advances_offset_when_adapter_fails(tmp_path: Path) -> None:
    user = make_user(telegram_user_id=1001)
    dispatcher, *_ = _make_polling_dispatcher(tmp_path, user)
    offset_path = tmp_path / "polling-offset.json"
    pending_path = tmp_path / "polling-pending-update.json"
    runner = PollingBotRunner(
        api=StaticUpdatePollingApi({"update_id": 123, "message": {}}),
        adapter=FailingAdapter(),
        dispatcher=dispatcher,
        offset_store=JsonPollingOffsetStore(offset_path),
        pending_update_store=JsonPollingPendingUpdateStore(pending_path),
        poll_timeout_seconds=0,
    )

    assert runner.process_once() == 1

    assert json.loads(offset_path.read_text(encoding="utf-8")) == {"offset": 124}
    assert not pending_path.exists()


def test_process_once_recovers_pending_update_before_polling(tmp_path: Path) -> None:
    user = make_user(telegram_user_id=1001)
    dispatcher, states, *_ = _make_polling_dispatcher(tmp_path, user)
    offset_store = JsonPollingOffsetStore(tmp_path / "polling-offset.json")
    pending_store = JsonPollingPendingUpdateStore(tmp_path / "polling-pending-update.json")
    pending_update = _load_fixture("message_command_start")
    pending_store.save(pending_update)
    api = RecordingPollingApi()
    runner = PollingBotRunner(
        api=api,
        adapter=TelegramUpdateAdapter(dispatcher),
        dispatcher=dispatcher,
        offset_store=offset_store,
        pending_update_store=pending_store,
        poll_timeout_seconds=0,
    )

    assert runner.process_once() == 1

    assert states.load(user.telegram_user_id).state is BotState.MONITORING
    saved_offset = json.loads(
        (tmp_path / "polling-offset.json").read_text(encoding="utf-8")
    )
    assert saved_offset == {"offset": int(pending_update["update_id"]) + 1}
    assert not (tmp_path / "polling-pending-update.json").exists()
    assert api.calls == []


def test_process_due_ticks_continues_after_user_failure(tmp_path: Path) -> None:
    first_user = make_user(telegram_user_id=1001, interval=60)
    second_user = make_user(telegram_user_id=1002, interval=60)
    dispatcher, states, telegram, _balances, *_rest, clock = make_dispatcher(
        tmp_path,
        [first_user, second_user],
    )
    dispatcher.start(first_user.telegram_user_id)
    dispatcher.start(second_user.telegram_user_id)
    clock.advance(60)
    original_balance_tick = dispatcher.balance_tick

    def fail_first_user(telegram_user_id: int):
        if telegram_user_id == first_user.telegram_user_id:
            raise RuntimeError("tick failure")
        return original_balance_tick(telegram_user_id)

    dispatcher.balance_tick = fail_first_user
    runner = PollingBotRunner(
        api=RecordingPollingApi(),
        adapter=TelegramUpdateAdapter(dispatcher),
        dispatcher=dispatcher,
        offset_store=JsonPollingOffsetStore(tmp_path / "polling-offset.json"),
        pending_update_store=JsonPollingPendingUpdateStore(
            tmp_path / "polling-pending-update.json"
        ),
        poll_timeout_seconds=0,
    )

    assert runner.process_due_ticks() == 2

    assert states.load(second_user.telegram_user_id).state is BotState.LOW_PROMPT
    assert telegram.messages[-1].telegram_user_id == second_user.telegram_user_id


def test_run_forever_logs_and_continues_after_polling_error(tmp_path: Path) -> None:
    user = make_user(telegram_user_id=1001)
    dispatcher, _states, _telegram, *_ = _make_polling_dispatcher(tmp_path, user)
    api = FailingOncePollingApi()
    runner = StopAfterCallsRunner(
        api=api,
        adapter=TelegramUpdateAdapter(dispatcher),
        dispatcher=dispatcher,
        offset_store=JsonPollingOffsetStore(tmp_path / "polling-offset.json"),
        pending_update_store=JsonPollingPendingUpdateStore(
            tmp_path / "polling-pending-update.json"
        ),
        poll_timeout_seconds=0,
        idle_sleep_seconds=0,
        error_sleep_seconds=0,
    )

    runner.run_forever()

    assert api.get_updates_calls == 2


def _run_due_tick(runner: PollingBotRunner, clock: MockClock, seconds: int) -> None:
    clock.advance(seconds)
    runner.process_once()


def _callback_update(
    fixture_name: str,
    *,
    update_id: int,
    callback_id: str,
    message_id: int | None,
) -> dict[str, Any]:
    update = copy.deepcopy(_load_fixture(fixture_name))
    update["update_id"] = update_id
    update["callback_query"]["id"] = callback_id
    update["callback_query"]["message"]["message_id"] = message_id
    return update


def _load_fixture(name: str) -> dict[str, Any]:
    path = FIXTURES_DIR / f"{name}.anonymized.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _api_state(base_url: str) -> dict[str, Any]:
    with urlopen(f"{base_url}/__admin/state", timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def _last_outbound(base_url: str, method: str) -> dict[str, Any]:
    matching = [
        item for item in _api_state(base_url)["outbound"] if item["method"] == method
    ]
    assert matching
    return matching[-1]


def _outbound_method_count(base_url: str, method: str) -> int:
    return sum(1 for item in _api_state(base_url)["outbound"] if item["method"] == method)


def _post_json(base_url: str, path: str, payload: Any) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        f"{base_url}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _make_polling_dispatcher(tmp_path: Path, user):
    config_path = write_config(tmp_path / "config.json", [user])
    states = JsonStateRepository(tmp_path / "states")
    telegram = MockTelegramGateway()
    balances = MockBalanceProvider()
    safe = MockSafeWalletClient()
    keychain = MockKeychain({user.safe_owner_key_ref: "private-key"})
    clock = MockClock()
    dispatcher = BotDispatcher(
        config_repository=JsonConfigRepository(config_path),
        state_repository=states,
        telegram=telegram,
        balances=balances,
        safe_wallet=safe,
        keychain=keychain,
        clock=clock,
    )
    return dispatcher, states, telegram, balances, safe, keychain, clock


class RecordingPollingApi:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.timeouts: list[int] = []

    def get_updates(self, **kwargs) -> list[dict[str, Any]]:
        self.calls.append("getUpdates")
        self.timeouts.append(kwargs["timeout_seconds"])
        return []


class StaticUpdatePollingApi:
    def __init__(self, update: dict[str, Any]) -> None:
        self._update = update
        self._returned = False

    def get_updates(self, **_kwargs) -> list[dict[str, Any]]:
        if self._returned:
            return []
        self._returned = True
        return [self._update]


class FailingOncePollingApi:
    def __init__(self) -> None:
        self.get_updates_calls = 0

    def get_updates(self, **_kwargs) -> list[dict[str, Any]]:
        self.get_updates_calls += 1
        if self.get_updates_calls == 1:
            raise TelegramApiError("temporary getUpdates failure")
        return []


class FailingAdapter:
    def handle_update(self, _update: dict[str, Any]) -> str:
        raise RuntimeError("adapter failure")


class StopAfterCallsRunner(PollingBotRunner):
    def setup(self) -> None:
        self._started = True

    def run_forever(self) -> None:
        if not self._started:
            self.setup()
        for _ in range(2):
            try:
                self.process_once()
            except Exception as error:
                self._logger.exception(
                    "polling_loop_error error_type=%s error=%s",
                    type(error).__name__,
                    error,
                )
