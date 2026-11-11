"""Управление окном, треем и службой как равноправными клиентами.

Окно, иконка в трее и командная строка ничего не знают друг о друге напрямую:
они находят друг друга через маленькие сокеты в $XDG_RUNTIME_DIR.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from . import ipc

# Куда смотрит этот экземпляр программы. Окно, трей и служба должны работать
# с одним и тем же state.json и одним и тем же сокетом, поэтому пути из
# командной строки протаскиваются во все дочерние процессы.
_paths: dict[str, Optional[Path]] = {"state": None, "socket": None}


def configure(state=None, socket=None) -> None:
    """Запомнить --state/--socket текущего запуска (None — значения по умолчанию)."""
    _paths["state"] = Path(state).expanduser() if state else None
    _paths["socket"] = Path(socket).expanduser() if socket else None


def state_path() -> Optional[Path]:
    return _paths["state"]


def socket_path() -> Optional[Path]:
    return _paths["socket"]


def child_args() -> list[str]:
    """Аргументы, которые надо передать дочернему процессу, чтобы он смотрел туда же."""
    args: list[str] = []
    if _paths["state"] is not None:
        args += ["--state", str(_paths["state"])]
    if _paths["socket"] is not None:
        args += ["--socket", str(_paths["socket"])]
    return args


def make_client():
    """Клиент демона с теми же путями, что у этого процесса."""
    from .client import DaemonClient  # локальный импорт: client тянет controller

    return DaemonClient(socket_path=_paths["socket"], state_path=_paths["state"])


def launcher() -> Optional[Path]:
    """Путь к nightfall.py — через него запускаются окно и трей."""
    candidate = Path(__file__).resolve().parent.parent / "nightfall.py"
    return candidate if candidate.exists() else None


def gui_socket() -> Path:
    return ipc.runtime_dir() / f"nightfall-gui-{os.getuid()}.sock"


def tray_socket() -> Path:
    return ipc.runtime_dir() / f"nightfall-tray-{os.getuid()}.sock"


# --------------------------------------------------------------------------- #
#  Окно
# --------------------------------------------------------------------------- #
def request_gui(cmd: str, *, timeout: float = 2.0) -> bool:
    try:
        return bool(ipc.request({"cmd": cmd}, path=gui_socket(), timeout=timeout).get("ok"))
    except ipc.IpcError:
        return False


def gui_running() -> bool:
    return request_gui("ping")


def show_window() -> bool:
    """Показать окно: разбудить спрятанное или открыть новое."""
    if request_gui("show"):
        return True
    return spawn_gui()


def hide_window() -> bool:
    return request_gui("hide")


def quit_window() -> bool:
    return request_gui("quit")


# --------------------------------------------------------------------------- #
#  Трей
# --------------------------------------------------------------------------- #
def tray_running() -> bool:
    try:
        return bool(ipc.request({"cmd": "ping"}, path=tray_socket(), timeout=1.0).get("ok"))
    except ipc.IpcError:
        return False


def request_tray(cmd: str) -> bool:
    try:
        return bool(ipc.request({"cmd": cmd}, path=tray_socket(), timeout=2.0).get("ok"))
    except ipc.IpcError:
        return False


# --------------------------------------------------------------------------- #
#  Запуск процессов
# --------------------------------------------------------------------------- #
def spawn(args: list[str], *, interpreter: Optional[str] = None) -> bool:
    script = launcher()
    if script is None:
        return False
    try:
        subprocess.Popen(
            [interpreter or sys.executable, str(script), *args, *child_args()],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except OSError:
        return False


def spawn_gui() -> bool:
    return spawn([])


def _candidate_interpreters() -> list[str]:
    candidates = [sys.executable]
    for extra in ("/usr/bin/python3", shutil.which("python3")):
        if extra and extra not in candidates:
            candidates.append(extra)
    return candidates


def detect_all() -> dict[str, str]:
    """Какие бэкенды трея доступны и каким интерпретатором их запускать.

    Проверяем в отдельных процессах: в системном Python может быть PyGObject,
    а в том, которым запущен Nightfall, — нет.
    """
    script = launcher()
    if script is None:
        return {}
    found: dict[str, str] = {}
    for interpreter in _candidate_interpreters():
        try:
            result = subprocess.run(
                [interpreter, str(script), "--tray-backend", "probe"],
                capture_output=True,
                text=True,
                timeout=8,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode != 0:
            continue
        for backend in (result.stdout or "").split():
            found.setdefault(backend, interpreter)
    return found


def detect_tray() -> tuple[Optional[str], Optional[str]]:
    """Лучший бэкенд трея и подходящий для него интерпретатор."""
    found = detect_all()
    for backend in ("appindicator", "statusicon", "yad"):
        if backend in found:
            return backend, found[backend]
    return None, None


def interpreter_for(backend: str) -> Optional[str]:
    """Интерпретатор, в котором есть нужный бэкенд (для --tray-backend X)."""
    return detect_all().get(backend)


def ensure_tray() -> bool:
    """Поднять иконку в трее, если её ещё нет и если системе это по силам."""
    if tray_running():
        return True
    forced = os.environ.get("NIGHTFALL_TRAY_BACKEND", "").strip()
    if forced and forced != "auto":
        # бэкенду на GTK нужен интерпретатор с PyGObject — ищем его
        backend, interpreter = forced, interpreter_for(forced)
        args = ["--tray", "--tray-backend", forced]
    else:
        backend, interpreter = detect_tray()
        args = ["--tray"]
    if backend is None:
        return False
    if not spawn(args, interpreter=interpreter):
        return False
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if tray_running():
            return True
        time.sleep(0.1)
    return False


def stop_tray() -> bool:
    return request_tray("quit")


def quit_everything(client=None) -> None:
    """Полный выход: окно, трей и служба."""
    quit_window()
    stop_tray()
    if client is not None:
        try:
            client.quit_daemon()
        except Exception:  # noqa: BLE001 - службы может уже не быть
            pass
