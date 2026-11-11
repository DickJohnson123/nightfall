"""Управление окном, треем и службой как равноправными клиентами.

Окно, иконка в трее и командная строка ничего не знают друг о друге напрямую:
они находят друг друга через маленькие сокеты в $XDG_RUNTIME_DIR.
"""

from __future__ import annotations

import contextlib
import json
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


def gui_visible() -> Optional[bool]:
    """Окно на экране? None — окна вообще нет."""
    try:
        answer = ipc.request({"cmd": "status"}, path=gui_socket(), timeout=2.0)
    except ipc.IpcError:
        return None
    if not answer.get("ok"):
        return None
    return bool(answer.get("visible"))


def toggle_window() -> bool:
    """Клик по иконке: открытое окно спрятать, спрятанное — открыть."""
    if request_gui("toggle"):
        return True
    return spawn_gui()


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
def spawn(args: list[str], *, interpreter: Optional[str] = None) -> Optional[subprocess.Popen]:
    script = launcher()
    if script is None:
        return None
    try:
        return subprocess.Popen(
            [interpreter or sys.executable, str(script), *args, *child_args()],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return None


def spawn_gui() -> bool:
    # именно --window: обычный запуск теперь поднимает только иконку в трее,
    # и без флага клик по иконке приводил бы к процессу без окна
    return spawn(["--window"]) is not None


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
    for backend in ("statusicon", "appindicator", "yad"):
        if backend in found:
            return backend, found[backend]
    return None, None


def interpreter_for(backend: str) -> Optional[str]:
    """Интерпретатор, в котором есть нужный бэкенд (для --tray-backend X)."""
    return detect_all().get(backend)


TRAY_WAIT_SECONDS = 15.0      # сколько ждём появления сокета иконки
TRAY_TOTAL_SECONDS = 20.0     # столько ждём иконку вообще, по всем бэкендам
TRAY_LOCK_SECONDS = 10.0      # ждём блокировку, если трей поднимают одновременно


def _tray_lock_path() -> Path:
    return ipc.runtime_dir() / f"nightfall-tray-{os.getuid()}.lock"


def _tray_cache_path() -> Path:
    return ipc.runtime_dir() / f"nightfall-tray-{os.getuid()}.json"


@contextlib.contextmanager
def _tray_lock(timeout: float = TRAY_LOCK_SECONDS):
    """Не даём двум запускам одновременно плодить иконки (автозагрузка + ручной)."""
    import fcntl

    path = _tray_lock_path()
    handle = open(path, "w+")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break  # чужой запуск не отвечает — просто идём дальше
                time.sleep(0.1)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _cached_backend() -> Optional[tuple[str, str]]:
    """Что уже сработало в прошлый раз — чтобы не перебирать интерпретаторы."""
    try:
        raw = json.loads(_tray_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    backend, interpreter = raw.get("backend"), raw.get("interpreter")
    if isinstance(backend, str) and isinstance(interpreter, str) and Path(interpreter).exists():
        return backend, interpreter
    return None


def _remember_backend(backend: str, interpreter: Optional[str]) -> None:
    try:
        _tray_cache_path().write_text(
            json.dumps({"backend": backend, "interpreter": interpreter or sys.executable}),
            encoding="utf-8",
        )
    except OSError:
        pass


def _forget_backend() -> None:
    try:
        _tray_cache_path().unlink()
    except OSError:
        pass


def _tray_attempts() -> list[tuple[str, Optional[str]]]:
    """Кого пробовать и в каком порядке. Первым — то, что сработало в прошлый раз."""
    forced = os.environ.get("NIGHTFALL_TRAY_BACKEND", "").strip()
    if forced and forced != "auto":
        return [(forced, interpreter_for(forced))]
    attempts: list[tuple[str, Optional[str]]] = []
    cached = _cached_backend()
    if cached is not None:
        attempts.append(cached)
    found = detect_all()
    for backend in ("statusicon", "appindicator", "yad"):
        interpreter = found.get(backend)
        if interpreter is None:
            continue
        if any(backend == name for name, _ in attempts):
            continue
        attempts.append((backend, interpreter))
    return attempts


def _wait_for_tray(process: Optional[subprocess.Popen], wait: float) -> bool:
    """Ждём, пока иконка поднимется. Если процесс умер — ждать бессмысленно."""
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if tray_running():
            return True
        if process is not None and process.poll() is not None:
            return tray_running()  # успел подняться и сразу передал сокет?
        time.sleep(0.15)
    return tray_running()


def ensure_tray() -> bool:
    """Поднять иконку в трее, если её ещё нет и если системе это по силам.

    Запуск при входе в систему бывает медленным: PyGObject и GTK на холодную
    стартуют секундами. Поэтому «сокета ещё нет» — это не отказ: ждём до
    TRAY_WAIT_SECONDS, следим, жив ли процесс, и пробуем следующий бэкенд,
    если выбранный сразу упал.
    """
    if tray_running():
        return True
    with _tray_lock():
        if tray_running():
            return True
        deadline = time.monotonic() + TRAY_TOTAL_SECONDS
        for backend, interpreter in _tray_attempts():
            left = deadline - time.monotonic()
            if left <= 0:
                break  # перебрали всё, что успевали: окно откроем, но честно скажем
            args = ["--tray", "--tray-backend", backend]
            process = spawn(args, interpreter=interpreter)
            if process is None:
                continue
            if _wait_for_tray(process, min(TRAY_WAIT_SECONDS, left)):
                _remember_backend(backend, interpreter)
                return True
        _forget_backend()
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
