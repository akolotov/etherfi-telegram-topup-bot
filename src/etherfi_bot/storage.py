from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from etherfi_bot.domain import BotConfig, UserState


class JsonConfigRepository:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def load(self) -> BotConfig:
        with self._path.open("r", encoding="utf-8") as file:
            payload: dict[str, Any] = json.load(file)
        return BotConfig.from_dict(payload)


class JsonStateRepository:
    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory)
        self._directory.mkdir(parents=True, exist_ok=True)

    def load(self, telegram_user_id: int) -> UserState:
        path = self._path_for(telegram_user_id)
        if not path.exists():
            return UserState.new(telegram_user_id)
        with path.open("r", encoding="utf-8") as file:
            return UserState.from_dict(json.load(file))

    def save(self, state: UserState) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._path_for(state.telegram_user_id)
        payload = state.to_dict()
        tmp_path = path.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2, sort_keys=True)
            file.write("\n")
        tmp_path.replace(path)

    def list_states(self) -> list[UserState]:
        states: list[UserState] = []
        for path in sorted(self._directory.glob("*.json")):
            with path.open("r", encoding="utf-8") as file:
                states.append(UserState.from_dict(json.load(file)))
        return states

    def _path_for(self, telegram_user_id: int) -> Path:
        return self._directory / f"{int(telegram_user_id)}.json"

