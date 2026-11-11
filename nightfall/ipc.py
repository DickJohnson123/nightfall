"""Локальный IPC Nightfall: Unix-сокет + JSON-строки.

Демон слушает сокет, клиенты (окно, командная строка, всплывающие напоминания)
шлют ему команды и получают снимок состояния. Никаких портов и сети.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
from pathlib import Path
from typing import Callable, Optional


class IpcError(RuntimeError):
    """Демон недоступен или ответил ошибкой."""


class AlreadyRunning(RuntimeError):
    """Сокет уже занят живым процессом — второй экземпляр не нужен."""


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base:
        path = Path(base)
        if path.is_dir():
            return path
    return Path(tempfile.gettempdir())


def socket_path() -> Path:
    return runtime_dir() / f"nightfall-{os.getuid()}.sock"


def pid_path() -> Path:
    return runtime_dir() / f"nightfall-{os.getuid()}.pid"


# --------------------------------------------------------------------------- #
#  Клиент
# --------------------------------------------------------------------------- #
def request(payload: dict, *, timeout: float = 5.0, path: Optional[Path] = None) -> dict:
    """Отправить одну команду демону и вернуть его ответ."""
    target = Path(path) if path else socket_path()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(target))
            sock.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            raw = b""
            while not raw.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                raw += chunk
    except (OSError, socket.timeout) as error:
        raise IpcError(f"демон не отвечает: {error}") from error

    try:
        reply = json.loads(raw.decode("utf-8") or "{}")
    except json.JSONDecodeError as error:
        raise IpcError("некорректный ответ демона") from error
    if not isinstance(reply, dict):
        raise IpcError("некорректный ответ демона")
    return reply


def socket_is_live(path: Path) -> bool:
    """За сокетом кто-то слушает? (файл может остаться от упавшего процесса)"""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            sock.connect(str(path))
        return True
    except OSError:
        return False


def is_running(*, path: Optional[Path] = None, timeout: float = 1.0) -> bool:
    try:
        reply = request({"cmd": "ping"}, timeout=timeout, path=path)
    except IpcError:
        return False
    return bool(reply.get("ok"))


def daemon_pid(*, path: Optional[Path] = None) -> Optional[int]:
    try:
        reply = request({"cmd": "ping"}, timeout=1.0, path=path)
    except IpcError:
        return None
    return reply.get("pid")


# --------------------------------------------------------------------------- #
#  Сервер
# --------------------------------------------------------------------------- #
class Server(threading.Thread):
    """Слушает сокет в отдельном потоке и передаёт команды обработчику."""

    def __init__(self, handler: Callable[[dict], dict], *, path: Optional[Path] = None) -> None:
        super().__init__(name="nightfall-ipc", daemon=True)
        self.handler = handler
        self.path = Path(path) if path else socket_path()
        self._stop = threading.Event()
        self._socket: Optional[socket.socket] = None

    # ------------------------------------------------------------------ #
    def listen(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # чужой живой сокет не отбираем: иначе второй трей/окно тихо «уводит»
        # адрес у первого, и иконка остаётся, но становится немой
        if self.path.exists() and socket_is_live(self.path):
            raise AlreadyRunning(str(self.path))
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.path))
        os.chmod(self.path, 0o600)
        server.listen(16)
        server.settimeout(0.5)
        self._socket = server
        self.start()

    def run(self) -> None:  # pragma: no cover - поток
        assert self._socket is not None
        while not self._stop.is_set():
            try:
                connection, _ = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with connection:
                try:
                    connection.settimeout(2.0)
                    raw = b""
                    while not raw.endswith(b"\n"):
                        chunk = connection.recv(65536)
                        if not chunk:
                            break
                        raw += chunk
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("ожидался объект")
                    reply = self.handler(payload)
                except Exception as error:  # noqa: BLE001 - демон не должен падать
                    reply = {"ok": False, "error": str(error)}
                try:
                    connection.sendall(
                        (json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8")
                    )
                except OSError:
                    pass

    def stop(self) -> None:
        self._stop.set()
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None
        try:
            self.path.unlink()
        except OSError:
            pass
