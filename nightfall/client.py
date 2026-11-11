"""Клиент демона: снимок состояния + команды через Unix-сокет.

Этим пользуются окно, командная строка и всплывающие напоминания, поэтому
все они равноправны: любое можно закрыть, остальные продолжат работать.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from . import ipc
from .schedule import Settings


@dataclass
class NightSnapshot:
    evening: date
    target: datetime
    shutdown_at: datetime
    actual: Optional[datetime] = None
    skipped: bool = False
    postponed_minutes: int = 0
    settled: bool = False

    @property
    def outcome(self) -> datetime:
        return self.actual or self.shutdown_at


@dataclass
class EventSnapshot:
    at: datetime
    kind: str
    text: str


@dataclass
class RemoteView:
    """Тот же набор полей, что у View в демоне, но собранный из JSON."""

    now: datetime
    phase: str
    night: NightSnapshot
    settings: Settings
    tomorrow: datetime
    remaining: timedelta
    next_reminder_at: Optional[datetime]
    at_goal: bool
    history: list[NightSnapshot] = field(default_factory=list)
    events: list[EventSnapshot] = field(default_factory=list)
    state_path: str = ""

    @property
    def postponed(self) -> int:
        return self.night.postponed_minutes


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value)


def view_from_dict(raw: dict) -> RemoteView:
    night = NightSnapshot(
        evening=date.fromisoformat(raw["evening"]),
        target=datetime.fromisoformat(raw["target"]),
        shutdown_at=datetime.fromisoformat(raw["shutdown_at"]),
        actual=parse_dt(raw.get("actual")),
        skipped=bool(raw.get("skipped")),
        postponed_minutes=int(raw.get("postponed", 0)),
        settled=bool(raw.get("settled")),
    )
    history = [
        NightSnapshot(
            evening=date.fromisoformat(item["evening"]),
            target=datetime.fromisoformat(item["target"]),
            shutdown_at=datetime.fromisoformat(item["outcome"]),
            actual=parse_dt(item.get("actual")),
            skipped=bool(item.get("skipped")),
        )
        for item in raw.get("history", [])
    ]
    events = [
        EventSnapshot(
            at=datetime.fromisoformat(item["at"]),
            kind=str(item.get("kind", "")),
            text=str(item.get("text", "")),
        )
        for item in raw.get("events", [])
    ]
    return RemoteView(
        now=datetime.fromisoformat(raw["now"]),
        phase=str(raw.get("phase", "idle")),
        night=night,
        settings=Settings.from_dict(raw.get("settings") or {}),
        tomorrow=datetime.fromisoformat(raw["tomorrow"]),
        remaining=timedelta(seconds=float(raw.get("remaining_seconds", 0))),
        next_reminder_at=parse_dt(raw.get("next_reminder_at")),
        at_goal=bool(raw.get("at_goal")),
        history=history,
        events=events,
        state_path=str(raw.get("state_path", "")),
    )


class DaemonClient:
    """Тонкая обёртка над сокетом. Все ошибки — ipc.IpcError."""

    def __init__(
        self,
        socket_path: Optional[Path] = None,
        *,
        state_path: Optional[Path] = None,
    ) -> None:
        self.socket_path = Path(socket_path) if socket_path else None
        self.state_path = Path(state_path) if state_path else None
        self.pid: Optional[int] = None
        self.last_view: Optional[RemoteView] = None

    # ------------------------------------------------------------------ #
    def call(self, cmd: str, **kwargs) -> dict:
        reply = ipc.request({"cmd": cmd, **kwargs}, path=self.socket_path)
        if not reply.get("ok"):
            raise ipc.IpcError(str(reply.get("error") or "ошибка демона"))
        self.pid = reply.get("pid", self.pid)
        return reply

    def _view_from(self, reply: dict) -> RemoteView:
        view = view_from_dict(reply["view"])
        self.last_view = view
        return view

    # -- запросы -------------------------------------------------------- #
    def view(self) -> RemoteView:
        return self._view_from(self.call("status"))

    def tick(self) -> RemoteView:
        """Окно зовёт это каждый секунду — это просто чтение состояния."""
        return self.view()

    def export_history(self) -> str:
        return str(self.call("export").get("text", ""))

    # -- команды -------------------------------------------------------- #
    def postpone(self, minutes: Optional[int] = None) -> None:
        self.call("postpone", minutes=minutes)

    def undo_postpone(self) -> None:
        self.call("undo")

    def shutdown_now(self) -> None:
        self.call("shutdown_now")

    def apply_settings(self, settings: Settings) -> None:
        if self.state_path is not None:
            settings = settings  # путь уже известен демону
        self.call("settings", settings=settings.to_dict())

    def set_dry_run(self, value: bool) -> None:
        self.call("settings", dry_run=bool(value))

    def reset_progress(self) -> None:
        self.call("reset")

    def shift(self, minutes: int) -> None:
        self.call("shift", minutes=int(minutes))

    def quit_daemon(self) -> None:
        self.call("quit")

    # ------------------------------------------------------------------ #
    def is_running(self) -> bool:
        try:
            return bool(self.call("ping").get("ok"))
        except ipc.IpcError:
            return False

    def ensure_daemon(self, *, timeout: float = 8.0) -> bool:
        """Запустить демон, если он ещё не работает (окно делает это само)."""
        if self.is_running():
            return True
        script = Path(__file__).resolve().parent.parent / "nightfall.py"
        if not script.exists():
            return False
        args = [sys.executable, str(script), "--service"]
        if self.state_path is not None:
            args += ["--state", str(self.state_path)]
        if self.socket_path is not None:
            args += ["--socket", str(self.socket_path)]
        try:
            subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env={**os.environ},
            )
        except OSError:
            return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.is_running():
                return True
            time.sleep(0.15)
        return False
