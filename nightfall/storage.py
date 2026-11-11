"""Хранение состояния Nightfall в JSON-файле (только стандартная библиотека)."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .schedule import Night, Settings, fmt_dt

APP_NAME = "nightfall"
STATE_VERSION = 1
MAX_HISTORY = 180  # сколько ночей помним


def default_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(Path.home(), ".config")
    return Path(base) / APP_NAME


def default_state_path() -> Path:
    return default_config_dir() / "state.json"


@dataclass
class State:
    settings: Settings = field(default_factory=Settings)
    nights: list[Night] = field(default_factory=list)

    @property
    def tonight(self) -> Night | None:
        return self.nights[-1] if self.nights else None

    def to_dict(self) -> dict:
        return {
            "version": STATE_VERSION,
            "settings": self.settings.to_dict(),
            "nights": [night.to_dict() for night in self.nights],
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "State":
        settings = Settings.from_dict(raw.get("settings") or {})
        nights: list[Night] = []
        for item in raw.get("nights") or []:
            try:
                nights.append(Night.from_dict(item))
            except (KeyError, TypeError, ValueError):
                continue  # битую запись просто игнорируем
        nights.sort(key=lambda night: night.evening)
        return cls(settings=settings, nights=nights[-MAX_HISTORY:])


class Store:
    """Атомарная запись состояния в файл."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else default_state_path()

    # ------------------------------------------------------------------ #
    def load(self) -> State:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return State()
        if not isinstance(raw, dict):
            return State()
        return State.from_dict(raw)

    def save(self, state: State) -> None:
        state.nights = state.nights[-MAX_HISTORY:]
        payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(self.path.parent),
            prefix=".state-",
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(handle.name, self.path)
        except OSError:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    def backup(self, state: State) -> Path | None:
        """Копия состояния рядом с основным файлом (перед сбросом прогресса)."""
        if not self.path.exists():
            return None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = self.path.with_name(f"state-{stamp}.json.bak")
        try:
            target.write_bytes(self.path.read_bytes())
        except OSError:
            return None
        return target

    def reset(self) -> State:
        """Сбросить прогресс, сохранив настройки."""
        state = self.load()
        self.backup(state)
        fresh = State(settings=state.settings)
        self.save(fresh)
        return fresh


def export_history(state: State) -> str:
    """Человекочитаемая выгрузка истории (для кнопки «Копировать журнал»)."""
    lines = [
        "дата вечера | план  | перенос | итог  | статус",
        "------------+-------+---------+-------+---------------",
    ]
    for night in state.nights:
        status = "выключено" if night.actual else ("пропущено" if night.skipped else "ожидание")
        lines.append(
            f"{night.evening.isoformat()} | "
            f"{night.target:%H:%M} | "
            f"{night.postponed_minutes:+4d}   | "
            f"{night.outcome:%H:%M} | {status}"
        )
    if not state.nights:
        lines.append("(история пуста)")
    lines.append("")
    lines.append(f"сформировано: {fmt_dt(datetime.now())}")
    return "\n".join(lines)
