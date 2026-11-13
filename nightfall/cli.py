"""Командная строка Nightfall.

    python3 nightfall.py                 окно (фоновая служба поднимется сама)
    python3 nightfall.py --service       фоновая служба без окна (для systemd)
    python3 nightfall.py --status        что запланировано
    python3 nightfall.py --postpone 15   отложить выключение
    python3 nightfall.py --stop          остановить фоновую службу
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

from . import ipc, notify, session
from .client import DaemonClient, RemoteView, view_from_dict
from datetime import timedelta

from .schedule import fmt_hm, human_delta
from .storage import Store, export_history

BANNER = "Nightfall — плавный сдвиг времени выключения"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nightfall",
        description=BANNER,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "примеры:\n"
            "  python3 nightfall.py                  # иконка в трее, без окна\n"
            "  python3 nightfall.py --window         # сразу с окном\n"
            "  python3 nightfall.py --service        # фоновая служба без окна\n"
            "  python3 nightfall.py --status         # что запланировано\n"
            "  python3 nightfall.py --postpone 15    # отложить на 15 минут\n"
            "  python3 nightfall.py --real           # реальное выключение вместо имитации\n"
            "  python3 nightfall.py --stop           # остановить фон\n"
        ),
    )
    parser.add_argument("--state", type=Path, default=None, help="путь к файлу состояния")
    parser.add_argument("--socket", type=Path, default=None, help="путь к сокету демона")

    mode = parser.add_argument_group("режим запуска")
    mode.add_argument("--service", "--headless", dest="service", action="store_true",
                      help="запустить фоновую службу (без окна)")
    mode.add_argument("--popup-json", default=None, metavar="JSON",
                      help="служебное: показать всплывающее напоминание и выйти")
    mode.add_argument("--window", action="store_true",
                      help="сразу открыть окно (по умолчанию запускается только иконка в трее)")
    mode.add_argument("--tray", action="store_true",
                      help="иконка в системном трее (рядом с часами)")
    mode.add_argument("--tray-backend", default="auto",
                      choices=["auto", "appindicator", "statusicon", "yad", "detect", "probe"],
                      help="какой бэкенд трея использовать (detect/probe — напечатать и выйти)")

    actions = parser.add_argument_group("управление работающей службой")
    actions.add_argument("--status", action="store_true", help="показать план и историю")
    actions.add_argument("--plan", action="store_true", help="то же, что --status")
    actions.add_argument("--postpone", nargs="?", type=int, const=0, default=None,
                         metavar="МИН", help="отложить выключение (без числа — из настроек)")
    actions.add_argument("--undo-postpone", action="store_true", help="отменить последний перенос")
    actions.add_argument("--shutdown-now", action="store_true", help="выключить компьютер сейчас")
    actions.add_argument("--stop", action="store_true", help="остановить фоновую службу")
    actions.add_argument("--show", action="store_true", help="показать окно")
    actions.add_argument("--toggle", action="store_true",
                         help="переключить окно: спрятать, если открыто, открыть, если нет")
    actions.add_argument("--hide", action="store_true", help="спрятать окно в трей")
    actions.add_argument("--quit", action="store_true",
                         help="остановить окно, трей и службу")
    actions.add_argument("--real", action="store_true", help="реальное выключение вместо имитации")
    actions.add_argument("--dry-run", action="store_true", help="вернуть режим имитации")
    actions.add_argument("--reset", action="store_true", help="сбросить прогресс (история -> .bak)")
    actions.add_argument("--shift", type=int, default=None, metavar="МИН",
                         help="служебное: сдвинуть план текущей ночи на N минут раньше")

    diagnostics = parser.add_argument_group("диагностика")
    diagnostics.add_argument("--doctor", action="store_true",
                             help="показать, что видно про дисплей, трей и службу")
    diagnostics.add_argument("--logs", nargs="?", type=int, const=20, default=None,
                             metavar="N", help="последние N строк журнала (по умолчанию 20)")
    return parser


# --------------------------------------------------------------------------- #
#  Печать состояния
# --------------------------------------------------------------------------- #
def _offline_view(store: Store, clock=None) -> RemoteView:
    from .controller import Controller, view_to_dict

    controller = Controller(store, desktop_notifications=False,
                            **({"clock": clock} if clock else {}))
    return view_from_dict(view_to_dict(controller.view()))


def _print_view(view: RemoteView, source: str) -> None:
    print(BANNER)
    print("=" * len(BANNER))
    print(f"источник          : {source}")
    print(f"сейчас            : {view.now:%d.%m %H:%M}")
    print(f"ночь (вечер)      : {view.night.evening:%d.%m.%Y}")
    print(f"план              : {fmt_hm(view.night.target)}")
    print(f"перенесено        : +{view.postponed} мин")
    print(f"фактически        : {fmt_hm(view.night.shutdown_at)}")
    print(f"осталось          : {human_delta(view.remaining)}")
    print(f"цель              : {fmt_hm(view.settings.goal_time)}"
          f" (шаг {view.settings.step_minutes} мин)")
    print(f"следующая ночь    : {fmt_hm(view.tomorrow)}")
    print(f"напоминания       : за {view.settings.warn_lead_minutes} мин, "
          f"каждые {view.settings.warn_interval_minutes} мин")
    print(f"режим             : {'имитация' if view.settings.dry_run else 'реальное выключение'}")
    print(f"состояние         : {view.state_path}")


def command_status(store: Store, client: DaemonClient) -> int:
    try:
        view = client.view()
        source = f"фоновая служба, pid {client.pid}"
        history = client.export_history()
    except ipc.IpcError:
        view = _offline_view(store)
        source = "фон не запущен (данные из файла состояния)"
        history = export_history(store.load())
    _print_view(view, source)
    print()
    print(history)
    return 0


# --------------------------------------------------------------------------- #
#  Управление
# --------------------------------------------------------------------------- #
def _offline_settings(store: Store) -> tuple[object, object]:
    from .controller import Controller

    controller = Controller(store, desktop_notifications=False)
    return controller, controller.state.settings


def command_flag(
    store: Store,
    client: DaemonClient,
    name: str,
    *,
    minutes: Optional[int] = None,
    settings=None,
) -> int:
    """Команды, которые выполняет демон, с разумным офлайн-фолбэком."""
    from . import schedule

    try:
        if name == "postpone":
            client.postpone(minutes)
            view = client.view()
            print(f"отложено → выключение в {fmt_hm(view.night.shutdown_at)} (+{view.postponed} мин)")
            return 0

        if name == "undo":
            client.undo_postpone()
            view = client.view()
            print(f"перенос отменён → выключение в {fmt_hm(view.night.shutdown_at)}")
            return 0

        if name == "shutdown-now":
            client.shutdown_now()
            print("команда выключения отправлена")
            return 0

        if name == "stop":
            client.quit_daemon()
            print("фоновая служба остановлена")
            return 0

        if name == "shift":
            client.shift(minutes or 0)
            view = client.view()
            print(f"план сдвинут → {fmt_hm(view.night.shutdown_at)}")
            return 0

        if name == "settings":
            client.apply_settings(settings)
            return 0

        if name == "reset":
            client.reset_progress()
            print("прогресс сброшен")
            return 0
    except ipc.IpcError:
        pass  # демон не запущен — ниже делаем то же самое напрямую с файлом

    # ---- офлайн-режим ------------------------------------------------ #
    if name == "reset":
        store.reset()
        print(f"прогресс сброшен. Данные: {store.path}")
        return 0

    if name == "settings":
        from .controller import Controller

        controller = Controller(store, desktop_notifications=False)
        controller.apply_settings(settings)
        print("настройки сохранены (фон не запущен)")
        return 0

    from .controller import Controller

    controller = Controller(store, desktop_notifications=False)
    night = controller.state.nights[-1] if controller.state.nights else None
    if night is None:
        print("нет данных о ночах", file=sys.stderr)
        return 1

    if name == "postpone":
        if night.settled:
            print("эта ночь уже закрыта — переносить нечего", file=sys.stderr)
            return 1
        value = minutes or controller.state.settings.postpone_minutes
        schedule.postpone(night, value, controller.clock())
        controller.save()
        print(f"перенесено на {value} мин → выключение в {fmt_hm(night.shutdown_at)} "
              f"(фон не запущен, сохранено в файл)", file=sys.stderr)
        return 0

    if name == "undo":
        schedule.undo_last_postpone(night)
        controller.save()
        print(f"перенос отменён → выключение в {fmt_hm(night.shutdown_at)} (фон не запущен)",
              file=sys.stderr)
        return 0

    if name == "shift":
        night.target = night.target - timedelta(minutes=minutes or 0)
        night.fired = []
        controller.save()
        print(f"план сдвинут → {fmt_hm(night.shutdown_at)} (фон не запущен)", file=sys.stderr)
        return 0

    print("для этой команды нужна запущенная фоновая служба "
          "(python3 nightfall.py --service)", file=sys.stderr)
    return 1


def run_service(store: Store, socket: Optional[Path]) -> int:
    from .daemon import Daemon

    daemon = Daemon(store, socket=socket)
    return daemon.run()


def display_available() -> bool:
    """Есть ли графическая сессия (X11 или Wayland).

    Если DISPLAY не достался по наследству — например, автозапуск сработал
    раньше оконной сессии — ищем сокет X-сервера сами, см. session.discover_display.
    """
    return session.display_available()


def startup_plan(with_window: bool, tray_available: bool, has_display: bool = True) -> bool:
    """Открывать ли окно при запуске.

    По умолчанию программа поднимает службу и иконку в трее, а окно не открывает:
    при автозагрузке никто не хочет, чтобы что-то всплывало на весь экран.
    Но если трей в системе не поднялся, окно открываем — иначе пользователь
    останется без единого видимого признака, что программа работает.
    """
    if with_window:
        return True
    if not has_display:
        return False  # графики нет: окно открывать некуда
    return not tray_available


def run_startup(client: DaemonClient, *, with_window: bool = False) -> int:
    """Обычный запуск: служба + иконка в трее, окно — только по просьбе."""
    if not client.ensure_daemon():
        print("не удалось запустить фоновую службу", file=sys.stderr)
    has_display = display_available()
    tray_available = session.ensure_tray() if has_display else False
    if startup_plan(with_window, tray_available, has_display):
        if not tray_available and not with_window:
            print("иконка в трее недоступна — открываю окно", file=sys.stderr)
        return run_window(client)
    if not has_display:
        print("графической сессии нет: служба работает в фоне, без окна и без иконки")
        return 0
    print("Nightfall работает: иконка рядом с часами. "
          "Клик по ней откроет окно, `nightfall --window` — сразу с окном.")
    return 0


def run_window(client: DaemonClient) -> int:
    if not client.ensure_daemon():
        print("не удалось запустить фоновую службу — окно откроется в режиме «нет связи»",
              file=sys.stderr)
    try:
        from .ui import run_gui
    except ImportError as error:  # pragma: no cover
        print("Не удалось загрузить tkinter:", error, file=sys.stderr)
        print("Установи пакет: sudo apt install python3-tk", file=sys.stderr)
        print("Либо запусти без окна: python3 nightfall.py --service", file=sys.stderr)
        return 1
    try:
        return run_gui(client)
    except Exception as error:  # pragma: no cover
        print("Ошибка графического интерфейса:", error, file=sys.stderr)
        return 1


def run_logs(count: int = 20) -> int:
    lines = notify.tail_log(count)
    if not lines:
        print(f"журнала пока нет: {notify.log_path()}")
        return 0
    print(f"--- {notify.log_path()} (последние {len(lines)}) ---")
    for line in lines:
        print(line)
    return 0


def run_doctor() -> int:
    """Что видно про дисплей, трей и службу. С этого стоит начинать разбор."""
    from . import tray

    print(BANNER)
    print("=" * len(BANNER))
    # сначала запоминаем, что досталось по наследству: discover_display()
    # дописывает найденный дисплей в окружение
    inherited = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    display = session.discover_display()
    if display is None:
        print("дисплей            : нет — программа работает в фоне, без окна и иконки")
    else:
        source = "из окружения" if inherited else "найден сам (автозапуск не успел)"
        print(f"дисплей            : {display} ({source})")
    try:
        import tkinter  # noqa: F401,PLC0415

        print("tkinter            : есть — окно и напоминания возможны")
    except Exception as error:  # noqa: BLE001
        print(f"tkinter            : нет ({error}) — окно и карточки недоступны")
    interpreters = session.tkinter_map()
    if len(interpreters) > 1:
        pairs = ", ".join(f"{path} ({'есть' if ok else 'нет'})"
                          for path, ok in interpreters.items())
        print(f"tkinter в python   : {pairs}")
    print(f"окно запускается   : {session.gui_interpreter()}")
    found = session.detect_all()
    if found:
        pairs = ", ".join(f"{name} ({path})" for name, path in found.items())
        print(f"бэкенды трея       : {pairs}")
    else:
        print("бэкенды трея       : ни одного (нужен python3-gi + gir1.2-gtk-3.0 или yad)")
    print(f"иконка в трее      : {'работает' if session.tray_running() else 'нет'}")
    print(f"сокет иконки       : {session.tray_socket()} "
          f"({'живой' if ipc.socket_is_live(session.tray_socket()) else 'нет'})")
    daemon_pid = ipc.daemon_pid()
    print(f"служба             : {'pid ' + str(daemon_pid) if daemon_pid else 'не запущена'}")
    print(f"файл состояния     : {session.state_path() or 'по умолчанию'}")
    print(f"журнал             : {notify.log_path()}")
    lines = notify.tail_log(5)
    if lines:
        print("последние записи   :")
        for line in lines:
            print(f"  {line}")
    return 0


def run_popup_process(payload: dict, socket: Optional[Path]) -> int:
    from .ui import run_popup

    client = DaemonClient(socket_path=socket)
    return run_popup(payload, client)


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # окно, трей и служба должны смотреть в один и тот же state.json/сокет
    session.configure(args.state, args.socket)
    store = Store(args.state) if args.state else Store()
    client = DaemonClient(socket_path=args.socket, state_path=args.state)

    if args.popup_json:
        try:
            payload = json.loads(args.popup_json)
        except json.JSONDecodeError as error:
            print(f"плохой JSON: {error}", file=sys.stderr)
            return 2
        return run_popup_process(payload, args.socket)

    if args.doctor:
        return run_doctor()

    if args.logs is not None:
        return run_logs(args.logs)

    if args.tray_backend in ("detect", "probe"):
        from . import tray

        found = tray.available_backends()
        if args.tray_backend == "detect":  # лучший бэкенд с учётом всех интерпретаторов
            backend, _interpreter = session.detect_tray()
            print(backend or "none")
        else:  # probe: что есть в этом интерпретаторе (его читает session.detect_all)
            print(" ".join(found))
        return 0

    if args.tray:
        from . import tray

        return tray.run(args.tray_backend)

    if args.show:
        ok = session.show_window()
        print("окно показано" if ok else "не удалось открыть окно", file=sys.stderr)
        return 0 if ok else 1

    if args.toggle:
        ok = session.toggle_window()
        print("окно переключено" if ok else "не удалось переключить окно", file=sys.stderr)
        return 0 if ok else 1

    if args.hide:
        ok = session.hide_window()
        print("окно спрятано в трей" if ok else "окно не запущено", file=sys.stderr)
        return 0 if ok else 1

    if args.quit:
        session.quit_everything(client)
        print("окно, трей и служба остановлены")
        return 0

    if args.service:
        return run_service(store, args.socket)

    if args.status or args.plan:
        return command_status(store, client)

    if args.real or args.dry_run:
        value = bool(args.dry_run)  # при обоих флагах побеждает имитация
        try:
            client.set_dry_run(value)
        except ipc.IpcError:
            controller, settings = _offline_settings(store)
            settings.dry_run = value
            controller.apply_settings(settings)
        print(f"режим: {'имитация' if value else 'реальное выключение'}")
        return 0

    if args.reset:
        return command_flag(store, client, "reset")
    if args.postpone is not None:
        return command_flag(store, client, "postpone", minutes=args.postpone or None)
    if args.undo_postpone:
        return command_flag(store, client, "undo")
    if args.shift is not None:
        return command_flag(store, client, "shift", minutes=args.shift)
    if args.shutdown_now:
        return command_flag(store, client, "shutdown-now")
    if args.stop:
        return command_flag(store, client, "stop")

    return run_startup(client, with_window=args.window)
