import hashlib
import hmac
import json
import time
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from starlette.testclient import TestClient
from telegram import Bot

from etherfi_bot.domain import ManualTopUpContext
from etherfi_bot.mini_app import (
    MAX_BODY_BYTES,
    InitDataError,
    create_mini_app,
    validate_init_data,
)


BOT_TOKEN = "123456:test-token"


def signed_init_data(user_id: int, auth_date: int, **extra: str) -> str:
    data = {
        "auth_date": str(auth_date),
        "query_id": "AAEAAAE",
        "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":")),
        **extra,
    }
    check_string = "\n".join(f"{key}={data[key]}" for key in sorted(data))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


def test_valid_init_data_authenticates_user() -> None:
    raw = signed_init_data(1001, 1_700_000_000)
    assert validate_init_data(raw, BOT_TOKEN, now=1_700_000_010) == 1001


def test_tampered_init_data_is_rejected() -> None:
    raw = signed_init_data(1001, 1_700_000_000).replace("1001", "1002")
    with pytest.raises(InitDataError):
        validate_init_data(raw, BOT_TOKEN, now=1_700_000_010)


def test_stale_init_data_is_rejected() -> None:
    raw = signed_init_data(1001, 1_700_000_000)
    with pytest.raises(InitDataError, match="expired"):
        validate_init_data(raw, BOT_TOKEN, now=1_700_000_301)


def test_duplicate_init_data_fields_are_rejected() -> None:
    raw = signed_init_data(1001, 1_700_000_000) + "&auth_date=1700000000"
    with pytest.raises(InitDataError, match="Duplicate"):
        validate_init_data(raw, BOT_TOKEN, now=1_700_000_010)


def test_context_endpoint_uses_authenticated_telegram_identity() -> None:
    class Dispatcher:
        requested_user_id = None

        async def manual_top_up_context(self, user_id):
            self.requested_user_id = user_id
            return ManualTopUpContext(
                target_balance=Decimal("300"),
                safe_balance=Decimal("1200"),
                maximum_amount=Decimal("1200"),
                preset_amounts=(Decimal("250"), Decimal("500")),
                target_account="0x1111111111111111111111111111111111111111",
                safe_account="0x2222222222222222222222222222222222222222",
            )

    dispatcher = Dispatcher()
    application = SimpleNamespace(bot=object(), update_queue=None)
    app = create_mini_app(
        application=application,
        dispatcher=dispatcher,
        bot_token=BOT_TOKEN,
        webhook_path="/hooks/test/webhook",
        webhook_secret_token="webhook-secret",
        mini_app_public_url="https://example.test/apps/bot/topup",
    )
    auth = signed_init_data(1001, int(time.time()))

    with TestClient(app) as client:
        response = client.get(
            "/apps/bot/topup/api/context",
            headers={"Authorization": f"tma {auth}"},
        )

    assert response.status_code == 200
    assert dispatcher.requested_user_id == 1001
    assert response.json() == {
        "target_balance": "300",
        "safe_balance": "1200",
        "maximum_amount": "1200",
        "preset_amounts": ["250", "500"],
        "target_account": "0x1111111111111111111111111111111111111111",
        "safe_account": "0x2222222222222222222222222222222222222222",
    }
    assert response.headers["cache-control"] == "no-store"


def test_context_endpoint_rejects_missing_telegram_authorization() -> None:
    application = SimpleNamespace(bot=object(), update_queue=None)
    app = create_mini_app(
        application=application,
        dispatcher=object(),
        bot_token=BOT_TOKEN,
        webhook_path="/hooks/test/webhook",
        webhook_secret_token="webhook-secret",
        mini_app_public_url="https://example.test/apps/bot/topup",
    )
    with TestClient(app) as client:
        response = client.get("/apps/bot/topup/api/context")
    assert response.status_code == 401


def test_slashless_mini_app_url_redirects_to_canonical_page() -> None:
    application = SimpleNamespace(bot=object(), update_queue=None)
    app = create_mini_app(
        application=application,
        dispatcher=object(),
        bot_token=BOT_TOKEN,
        webhook_path="/hooks/test/webhook",
        webhook_secret_token="webhook-secret",
        mini_app_public_url="https://example.test/apps/bot/topup",
    )

    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/apps/bot/topup")

    assert response.status_code == 307
    assert response.headers["location"] == "/apps/bot/topup/"


def test_context_endpoint_serializes_decimal_values_without_exponents() -> None:
    class Dispatcher:
        async def manual_top_up_context(self, _user_id):
            return ManualTopUpContext(
                target_balance=Decimal("1E+3"),
                safe_balance=Decimal("2E+3"),
                maximum_amount=Decimal("2E+3"),
                preset_amounts=(Decimal("1E+3"),),
                target_account="0x1111111111111111111111111111111111111111",
                safe_account="0x2222222222222222222222222222222222222222",
            )

    application = SimpleNamespace(bot=object(), update_queue=None)
    app = create_mini_app(
        application=application,
        dispatcher=Dispatcher(),
        bot_token=BOT_TOKEN,
        webhook_path="/hooks/test/webhook",
        webhook_secret_token="webhook-secret",
        mini_app_public_url="https://example.test/apps/bot/topup",
    )
    auth = signed_init_data(1001, int(time.time()))

    with TestClient(app) as client:
        response = client.get(
            "/apps/bot/topup/api/context",
            headers={"Authorization": f"tma {auth}"},
        )

    assert response.status_code == 200
    assert response.json()["target_balance"] == "1000"
    assert response.json()["maximum_amount"] == "2000"
    assert response.json()["preset_amounts"] == ["1000"]


def test_webhook_accepts_valid_update_larger_than_mini_app_body_limit() -> None:
    class RecordingQueue:
        def __init__(self) -> None:
            self.items = []

        async def put(self, item) -> None:
            self.items.append(item)

    queue = RecordingQueue()
    application = SimpleNamespace(bot=Bot(BOT_TOKEN), update_queue=queue)
    app = create_mini_app(
        application=application,
        dispatcher=object(),
        bot_token=BOT_TOKEN,
        webhook_path="/hooks/test/webhook",
        webhook_secret_token="webhook-secret",
        mini_app_public_url="https://example.test/apps/bot/topup",
    )
    message = {
        "message_id": 2,
        "date": 1_700_000_000,
        "chat": {"id": 1001, "type": "private", "first_name": "Test"},
        "from": {"id": 1001, "is_bot": False, "first_name": "Test"},
        "text": "x" * 4096,
        "entities": [
            {"type": "bold", "offset": offset, "length": 1}
            for offset in range(100)
        ],
        "reply_to_message": {
            "message_id": 1,
            "date": 1_699_999_999,
            "chat": {"id": 1001, "type": "private", "first_name": "Test"},
            "from": {"id": 1001, "is_bot": False, "first_name": "Test"},
            "text": "y" * 4096,
            "entities": [
                {"type": "italic", "offset": offset, "length": 1}
                for offset in range(100)
            ],
        },
    }
    payload = {"update_id": 123, "message": message}
    assert len(json.dumps(payload).encode()) > MAX_BODY_BYTES

    with TestClient(app) as client:
        response = client.post(
            "/hooks/test/webhook",
            json=payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
        )

    assert response.status_code == 200
    assert [item.update_id for item in queue.items] == [123]
