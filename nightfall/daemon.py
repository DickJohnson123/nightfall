"""Фоновая служба Nightfall.

Демон владеет расписанием: тикает контроллер, шлёт уведомления, выключает
компьютер и слушает Unix-сокет, через который им управляют окно и командная
строка. Окно больше не является «хозяином» процесса — его можно закрывать.
"""

from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import subprocess
import sys
import threading
from datetime import timedelta
from pathlib import Path
from typing import Optional

from . import ipc, notify, schedule
from .controller import Controller, view_to_dict
from .schedule import Settings, fmt_hm
from .storage import Store, export_history

TICK_SECONDS = 15  # разрешение планировщика: напоминания идут с шагом 5 минут


def launcher_path() -> Optional[Path]:
    """Путь к nightfall.py — он же используется для всплывающих окон."""
    candidate = Path(__file__).resolve().parent.parent / "nightfall.py"
    return candidate if candidate.exists() else None


POPUPS_OFF = ("0", "off", "no", "none", "нет")


def popups_available() -> bool:
    """Можно ли нарисовать красивое напоминание внизу экрана."""
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    if importlib.util.find_spec("tkinter") is None:
        return False
    return launcher_path() is not None


class Daemon:
    def __init__(
        self,
        store: Store | None = None,
        *,
        socket: Optional[Path] = None,
        popups: Optional[bool] = None,
        clock=None,
    ) -> None:
        self.store = store or Store()
        self.socket_path = Path(socket) if socket else ipc.socket_path()
        if popups is None:
            forced = os.environ.get("NIGHTFALL_POPUPS", "").strip().lower()
            popups = popups_available() and forced not in POPUPS_OFF
        self.popups_enabled = popups
        # Один канал на одно событие: если есть красивое напоминание внизу экрана,
        # системному «пузырю» вверху делать нечего — он был его дублем.
        self.controller = Controller(
            self.store,
            desktop_notifications=not popups,
            **({"clock": clock} if clock else {}),
        )
        self.controller.on_reminder = self._on_reminder
        self.controller.on_shutdown = self._on_shutdown
        self.server = ipc.Server(self.handle_command, path=self.socket_path)
        self._stop = threading.Event()
        self._popup_process: Optional[subprocess.Popen] = None
        self._lock_file = None

    # ------------------------------------------------------------------ #
    #  Запуск
    # ------------------------------------------------------------------ #
    def acquire_lock(self) -> bool:
        """Не даём запуститься второму демону на том же файле состояния."""
        lock_path = self.socket_path.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "w+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        handle.write(str(os.getpid()))
        handle.flush()
        self._lock_file = handle
        return True

    def run(self) -> int:
        if not self.acquire_lock():
            notify.log("демон уже запущен — второй экземпляр не нужен")
            return 1
        self.server.listen()
        ipc.pid_path().write_text(str(os.getpid()), encoding="utf-8")
        notify.log(f"демон запущен, сокет {self.socket_path}, pid {os.getpid()}")
        view = self.controller.view()
        notify.log(
            f"план: {fmt_hm(view.night.shutdown_at)} "
            f"({'имитация' if view.settings.dry_run else 'реальное выключение'})"
        )
        try:
            while not self._stop.is_set():
                try:
                    self.controller.tick()
                except Exception as error:  # noqa: BLE001 - служба не должна падать
                    notify.log(f"ошибка такта: {error}")
                self._stop.wait(TICK_SECONDS)
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()
        return 0

    def _shutdown(self) -> None:
        self.server.stop()
        self._close_popup()
        try:
            ipc.pid_path().unlink()
        except OSError:
            pass
        if self._lock_file is not None:
            try:
                self._lock_file.close()
            except OSError:
                pass
        notify.log("демон остановлен")

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ #
    #  Команды по сокету
    # ------------------------------------------------------------------ #
    def handle_command(self, payload: dict) -> dict:
        cmd = str(payload.get("cmd", "status"))
        try:
            if cmd == "ping":
                return {"ok": True, "pid": os.getpid()}

            if cmd == "status":
                pass  # ниже вернём свежий снимок

            elif cmd == "postpone":
                minutes = payload.get("minutes")
                self.controller.postpone(int(minutes) if minutes else None)
                self._close_popup()

            elif cmd == "undo":
                self.controller.undo_postpone()
                self._close_popup()

            elif cmd == "shutdown_now":
                self.controller.shutdown_now()

            elif cmd == "settings":
                raw = payload.get("settings")
                if isinstance(raw, dict):
                    self.controller.apply_settings(Settings.from_dict(raw))
                if "dry_run" in payload:
                    current = self.controller.state.settings
                    current.dry_run = bool(payload["dry_run"])
                    self.controller.apply_settings(current)

            elif cmd == "reset":
                self.controller.reset_progress()

            elif cmd == "export":
                return {
                    "ok": True,
                    "text": export_history(self.controller.state),
                    "view": view_to_dict(self.controller.view()),
                }

            elif cmd == "quit":
                self.stop()
                return {"ok": True, "pid": os.getpid(), "stopped": True}

            elif cmd == "shift":
                minutes = int(payload.get("minutes", 0))
                night = self.controller.state.nights[-1] if self.controller.state.nights else None
                if night is not None and minutes:
                    night.target = night.target - timedelta(minutes=minutes)
                    night.fired = []
                    self.controller.save()

            else:
                return {"ok": False, "error": f"неизвестная команда: {cmd}"}

            view = self.controller.tick()
            return {"ok": True, "pid": os.getpid(), "view": view_to_dict(view)}
        except Exception as error:  # noqa: BLE001
            return {"ok": False, "error": str(error)}

    # ------------------------------------------------------------------ #
    #  События контроллера
    # ------------------------------------------------------------------ #
    def _on_reminder(self, night, reminder: schedule.Reminder) -> None:
        minutes = reminder.remaining_minutes
        urgent = minutes <= 10
        accent = "#FB7185" if urgent else "#A78BFA"
        line2 = (
            f"перенос +{night.postponed_minutes} мин"
            if night.postponed_minutes
            else "Успевай закрыть дела — или отложи сон ещё немного."
        )
        self._spawn_popup(
            {
                "kind": "reminder",
                "title": f"Осталось {minutes} мин",
                "line1": f"Выключение в {fmt_hm(reminder.shutdown_at)}",
                "line2": line2,
                "accent": accent,
                "autoclose": 30,
                "postpone": self.controller.state.settings.postpone_minutes,
            }
        )

    def _on_shutdown(self, night, ok: bool, message: str) -> None:
        self._spawn_popup(
            {
                "kind": "shutdown",
                "title": "Выключаем компьютер" if ok else "Не удалось выключить",
                "line1": f"Фактическое время: {fmt_hm(night.actual or self.controller.clock())}",
                "line2": message,
                "accent": "#34D399" if ok else "#FB7185",
                "autoclose": 15,
            }
        )

    # ------------------------------------------------------------------ #
    #  Всплывающие окна (короткоживущий процесс на каждое напоминание)
    # ------------------------------------------------------------------ #
    def _close_popup(self) -> None:
        process = self._popup_process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        self._popup_process = None

    def _spawn_popup(self, payload: dict) -> None:
        """Показать напоминание ровно один раз, каким получится.

        Основной канал — карточка внизу экрана. Если её показать нечем, а
        системных уведомлений контроллер ещё не слал, говорим через систему:
        так событие не потеряется и не задвоится.
        """
        if self._start_popup(payload):
            return
        if not self.controller.desktop_notifications:
            self._system_notify(payload)

    def _start_popup(self, payload: dict) -> bool:
        if not self.popups_enabled:
            return False
        script = launcher_path()
        if script is None:
            return False
        self._close_popup()
        args = [sys.executable, str(script), "--popup-json", json.dumps(payload)]
        if self.socket_path != ipc.socket_path():
            args += ["--socket", str(self.socket_path)]
        try:
            self._popup_process = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as error:
            notify.log(f"не удалось показать окно-напоминание: {error}")
            return False
        return True

    def _system_notify(self, payload: dict) -> None:
        """Запасной канал: системное уведомление. Основной — окно внизу экрана."""
        title = str(payload.get("title") or "Nightfall")
        body = " ".join(
            str(part) for part in (payload.get("line1"), payload.get("line2")) if part
        )
        urgent = payload.get("kind") == "shutdown"
        notify.desktop_notify(title, body, urgency="critical" if urgent else "normal")
