"""Системные уведомления и выключение компьютера (Linux, без внешних пакетов)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Sequence


def notify_send_available() -> bool:
    return shutil.which("notify-send") is not None


def desktop_notify(
    title: str,
    body: str,
    *,
    urgency: str = "normal",
    timeout_ms: int = 20000,
    icon: str = "night-light-symbolic",
) -> bool:
    """Показать уведомление через notify-send. False — если его нет/не вышло."""
    exe = shutil.which("notify-send")
    if not exe:
        return False
    command = [
        exe,
        "--app-name=Nightfall",
        f"--urgency={urgency}",
        f"--expire-time={int(timeout_ms)}",
        f"--icon={icon}",
        title,
        body,
    ]
    try:
        subprocess.run(
            command,
            check=False,
            timeout=5,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def default_shutdown_command() -> tuple[str, ...]:
    """Максимально «безболезненный» способ выключить машину из пользователя."""
    if _command_exists("systemctl"):
        return ("systemctl", "poweroff")
    if _command_exists("shutdown"):
        return ("shutdown", "-h", "now")
    return ("poweroff",)


def run_command(command: Sequence[str], *, dry_run: bool = True) -> tuple[bool, str]:
    """Выполнить команду выключения. В режиме имитации — только показать её."""
    text = " ".join(command)
    if dry_run:
        return True, f"имитация: {text}"
    try:
        process = subprocess.Popen(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError:
        return False, f"команда не найдена: {command[0]}"
    except OSError as error:  # pragma: no cover - зависит от системы
        return False, f"ошибка запуска: {error}"
    try:
        _, stderr = process.communicate(timeout=20)
    except subprocess.TimeoutExpired:  # команда «повисла», но обычно всё уже работает
        return True, f"{text}: запущено в фоне"
    if process.returncode != 0:
        message = (stderr or b"").decode("utf-8", "replace").strip()
        return False, message or f"код возврата {process.returncode}"
    return True, text


def log(message: str) -> None:
    """Простой stdout-лог для headless-режима."""
    print(f"[nightfall] {message}", file=sys.stderr, flush=True)
