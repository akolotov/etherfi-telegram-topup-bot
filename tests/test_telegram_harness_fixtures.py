from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "telegram_updates"
SUPPORTED_UPDATE_KEYS = {
    "message",
    "callback_query",
    "message_reaction",
    "my_chat_member",
}


def load_fixture(name: str) -> dict[str, Any]:
    path = FIXTURES_DIR / f"{name}.anonymized.json"
    return json.loads(path.read_text(encoding="utf-8"))


def update_kind(payload: dict[str, Any]) -> str:
    keys = SUPPORTED_UPDATE_KEYS.intersection(payload)
    assert len(keys) == 1
    return next(iter(keys))


def test_fixture_catalog_contains_expected_seed_shapes() -> None:
    names = {path.name for path in FIXTURES_DIR.glob("*.anonymized.json")}

    assert {
        "message_plain_text.anonymized.json",
        "message_command_start.anonymized.json",
        "callback_query_top_up.anonymized.json",
        "callback_query_ignore.anonymized.json",
        "message_reply_text.anonymized.json",
        "message_reaction_add.anonymized.json",
        "message_reaction_replace.anonymized.json",
        "message_reaction_remove.anonymized.json",
        "my_chat_member_block.anonymized.json",
    }.issubset(names)


def test_all_seed_fixtures_have_one_supported_update_kind() -> None:
    for path in FIXTURES_DIR.glob("*.anonymized.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(payload["update_id"], int), path.name
        assert update_kind(payload) in SUPPORTED_UPDATE_KEYS


def test_start_fixture_preserves_bot_command_shape() -> None:
    payload = load_fixture("message_command_start")

    assert update_kind(payload) == "message"
    assert payload["message"]["chat"]["type"] == "private"
    assert payload["message"]["from"]["id"] == 1001
    assert payload["message"]["text"] == "/start"
    assert payload["message"]["entities"] == [
        {"offset": 0, "length": 6, "type": "bot_command"}
    ]


def test_callback_fixtures_preserve_button_payloads() -> None:
    top_up = load_fixture("callback_query_top_up")
    ignore = load_fixture("callback_query_ignore")

    assert update_kind(top_up) == "callback_query"
    assert top_up["callback_query"]["data"] == "top_up"
    assert top_up["callback_query"]["message"]["message_id"] == 28
    assert top_up["callback_query"]["message"]["reply_markup"]["inline_keyboard"][0][0] == {
        "text": "Top Up",
        "callback_data": "top_up",
    }

    assert update_kind(ignore) == "callback_query"
    assert ignore["callback_query"]["data"] == "ignore"
    assert ignore["callback_query"]["message"]["message_id"] == 29
    assert ignore["callback_query"]["message"]["reply_markup"]["inline_keyboard"][0][1] == {
        "text": "Ignore",
        "callback_data": "ignore",
    }


def test_block_fixture_preserves_private_kicked_signal() -> None:
    payload = load_fixture("my_chat_member_block")

    assert update_kind(payload) == "my_chat_member"
    member_update = payload["my_chat_member"]
    assert member_update["chat"]["type"] == "private"
    assert member_update["from"]["id"] == member_update["chat"]["id"]
    assert member_update["old_chat_member"]["status"] == "member"
    assert member_update["new_chat_member"]["status"] == "kicked"


def test_ignored_fixture_shapes_are_available_for_noop_adapter_tests() -> None:
    plain = load_fixture("message_plain_text")
    reply = load_fixture("message_reply_text")
    reaction_add = load_fixture("message_reaction_add")
    reaction_replace = load_fixture("message_reaction_replace")
    reaction_remove = load_fixture("message_reaction_remove")

    assert plain["message"]["text"] == "hello fixture plain text"
    assert "reply_to_message" in reply["message"]
    assert reaction_add["message_reaction"]["old_reaction"] == []
    assert reaction_add["message_reaction"]["new_reaction"] != []
    assert reaction_replace["message_reaction"]["old_reaction"] != []
    assert reaction_replace["message_reaction"]["new_reaction"] != []
    assert reaction_remove["message_reaction"]["old_reaction"] != []
    assert reaction_remove["message_reaction"]["new_reaction"] == []
