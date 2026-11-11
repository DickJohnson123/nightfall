"""Командная строка Nightfall: `python3 nightfall.py [--gui|--headless|--plan]`."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import schedule
from .controller import Controller
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
            "  python3 nightfall.py                 # открыть окно\n"
            "  python3 nightfall.py --real          # боевой режим (реально выключать)\n"
            "  python3 nightfall.py --headless      # без окна, только уведомления\n"
            "  python3 nightfall.py --plan          # показать план и выйти\n"
            "  python3 nightfall.py --reset         # сбросить прогресс\n"
        ),
    )
    parser.add_argument("--state", type=Path, default=None, help="путь к файлу состояния")
    parser.add_argument("--headless", action="store_true", help="работать без графического окна")
    parser.add_argument("--plan", action="store_true", help="напечатать план и выйти")
    parser.add_argument("--reset", action="store_true", help="сбросить прогресс (история -> .bak)")
    parser.add_argument("--real", action="store_true", help="выключать компьютер по-настоящему")
    parser.add_argument("--dry-run", action="store_true", help="только имитация выключения (по умолчанию)")
    parser.add_argument("--shift", type=int, default=None, metavar="МИН",
                        help="служебное: сдвинуть план текущей ночи на N минут раньше "
                             "(чтобы проверить уведомления, не дожидаясь ночи)")
    return parser


def _print_plan(controller: Controller) -> None:
    view = controller.view()
    night = view.night
    print(BANNER)
    print("=" * len(BANNER))
    print(f"сейчас            : {view.now:%d.%m %H:%M}")
    print(f"ночь (вечер)      : {night.evening:%d.%m.%Y}")
    print(f"план              : {fmt_hm(night.target)}")
    print(f"перенесено        : +{night.postponed_minutes} мин")
    print(f"фактически        : {fmt_hm(night.shutdown_at)}")
    print(f"осталось          : {human_delta(view.remaining)}")
    print(f"цель              : {fmt_hm(view.settings.goal_time)}"
          f" (шаг {view.settings.step_minutes} мин)")
    print(f"следующая ночь    : {fmt_hm(view.tomorrow)}")
    print(f"напоминания       : за {view.settings.warn_lead_minutes} мин, "
          f"каждые {view.settings.warn_interval_minutes} мин")
    print(f"режим             : {'имитация' if view.settings.dry_run else 'реальное выключение'}")
    print(f"состояние         : {view.state_path}")
    print()
    print(export_history(controller.state))


def run_headless(controller: Controller) -> int:
    from . import notify

    def on_reminder(night, reminder):
        notify.log(f"напоминание: осталось {reminder.remaining_minutes} мин "
                   f"до {fmt_hm(reminder.shutdown_at)}")

    def on_shutdown(night, ok, message):
        notify.log(f"выключение: {message}")

    controller.on_reminder = on_reminder
    controller.on_shutdown = on_shutdown
    notify.log("запущен без окна, Ctrl+C для выхода")
    notify.log(f"план: {fmt_hm(controller.view().night.shutdown_at)} "
               f"({'имитация' if controller.state.settings.dry_run else 'реально'})")
    try:
        while True:
            controller.tick()
            time.sleep(15)
    except KeyboardInterrupt:
        notify.log("остановлено")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = Store(args.state) if args.state else Store()

    if args.reset:
        store.reset()
        print(f"Прогресс сброшен. Данные: {store.path}")

    controller = Controller(store)
    settings = controller.state.settings
    changed = False
    if args.real:
        settings.dry_run = False
        changed = True
    if args.dry_run:
        settings.dry_run = True
        changed = True
    if changed:
        controller.apply_settings(settings)

    if args.shift:
        night = controller.state.nights[-1] if controller.state.nights else None
        if night is not None:
            night.target = night.target - timedelta(minutes=args.shift)
            night.fired = []
            controller.save()
            print(f"План сдвинут на {args.shift} мин раньше -> {fmt_hm(night.shutdown_at)}")

    if args.plan:
        _print_plan(controller)
        return 0

    if args.headless:
        return run_headless(controller)

    try:
        from .ui import run_gui
    except ImportError as error:  # pragma: no cover
        print("Не удалось загрузить tkinter:", error, file=sys.stderr)
        print("Установи пакет: sudo apt install python3-tk", file=sys.stderr)
        return 1
    try:
        return run_gui(controller)
    except Exception as error:  # pragma: no cover
        print("Ошибка графического интерфейса:", error, file=sys.stderr)
        print("Запасной вариант: python3 nightfall.py --headless", file=sys.stderr)
        return 1
