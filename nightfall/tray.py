"""Иконка Nightfall в системном трее (рядом с часами).

Иконка в панели — не часть стандартной библиотеки Python, её рисует система.
Поэтому есть три бэкенда, и подходящий выбирается автоматически:

* ``appindicator`` — современный StatusNotifierItem (KDE, Ubuntu/GNOME с
  расширением AppIndicator, Xfce 4.16+);
* ``statusicon`` — классический XEmbed (Xfce, Cinnamon, MATE, i3+stalonetray);
* ``yad`` — универсальный костыль, если нет PyGObject, но есть ``yad``.

Логика в трее: левый клик — показать окно, правый клик — меню.
Если бэкенд appindicator, система показывает меню по клику (первый пункт —
«Открыть окно»), это ограничение libappindicator, а не наше.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

from . import ipc, session

ASSET = Path(__file__).resolve().parent.parent / "assets" / "nightfall.png"
TITLE = "Nightfall"

ORDER = ("appindicator", "statusicon", "yad")


# --------------------------------------------------------------------------- #
#  Доступность бэкендов (в текущем интерпретаторе)
# --------------------------------------------------------------------------- #
def _gtk():
    import gi  # noqa: PLC0415 - импорт только когда действительно нужен

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk  # noqa: PLC0415

    return gi, Gtk


def _indicator_module():
    import gi

    for namespace in ("AyatanaAppIndicator3", "AppIndicator3"):
        try:
            gi.require_version(namespace, "0.1")
        except ValueError:
            continue
        module = __import__("gi.repository", fromlist=[namespace])
        return getattr(module, namespace)
    raise ImportError("нет AppIndicator")


def _sni_host() -> bool:
    """Есть ли в панели хост для AppIndicator (StatusNotifierItem).

    Без него иконка appindicator существует, но её никто не покажет,
    поэтому лучше сразу взять классический XEmbed-трей.
    """
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio

        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        if bus is None:
            return False
        proxy = Gio.DBusProxy.new_sync(
            bus,
            Gio.DBusProxyFlags.DO_NOT_AUTO_START,
            None,
            "org.kde.StatusNotifierWatcher",
            "/StatusNotifierWatcher",
            "org.kde.StatusNotifierWatcher",
            None,
        )
    except Exception:  # noqa: BLE001 - нет D-Bus/сессии = нет и хоста
        return False
    value = proxy.get_cached_property("IsStatusNotifierHostRegistered")
    return bool(value is not None and value.get_boolean())


def available_backends() -> list[str]:
    found: list[str] = []
    try:
        _indicator_module()
        _gtk()
        if _sni_host():
            found.append("appindicator")
    except Exception:  # noqa: BLE001 - отсутствие gi/Gtk/AppIndicator нормально
        pass
    try:
        _gtk()
        found.append("statusicon")
    except Exception:  # noqa: BLE001
        pass
    if shutil.which("yad"):
        found.append("yad")
    return [name for name in ORDER if name in found]


def icon_path() -> str:
    return str(ASSET) if ASSET.exists() else "night-light-symbolic"


# --------------------------------------------------------------------------- #
#  Общие действия меню
# --------------------------------------------------------------------------- #
def _show() -> None:
    session.show_window()


def _hide() -> None:
    session.hide_window()


def _quit(client, stop_main) -> None:
    session.quit_everything(client)
    stop_main()


def _status_text(client) -> str:
    try:
        view = client.view()
    except ipc.IpcError:
        return "служба не запущена"
    night = view.night
    plan = night.shutdown_at.strftime("%H:%M")
    if night.settled:
        return f"ночь закрыта, следующая в {plan}"
    minutes = int(max(0, view.remaining.total_seconds()) // 60)
    postponed = f" (+{view.postponed})" if view.postponed else ""
    return f"выключение в {plan}{postponed} · через {minutes} мин"


# --------------------------------------------------------------------------- #
#  GTK-бэкенды
# --------------------------------------------------------------------------- #
def _build_menu(Gtk, client, stop_main):
    from gi.repository import GLib  # noqa: PLC0415

    menu = Gtk.Menu()

    open_item = Gtk.MenuItem(label="Открыть окно")
    open_item.connect("activate", lambda *_: _show())
    menu.append(open_item)

    hide_item = Gtk.MenuItem(label="Скрыть окно")
    hide_item.connect("activate", lambda *_: _hide())
    menu.append(hide_item)

    menu.append(Gtk.SeparatorMenuItem())

    status_item = Gtk.MenuItem(label=_status_text(client))
    status_item.set_sensitive(False)
    menu.append(status_item)

    def refresh():
        status_item.set_label(_status_text(client))
        return True  # продолжаем таймер

    GLib.timeout_add_seconds(30, refresh)

    menu.append(Gtk.SeparatorMenuItem())

    quit_item = Gtk.MenuItem(label="Остановить службу и выйти")
    quit_item.connect("activate", lambda *_: _quit(client, stop_main))
    menu.append(quit_item)

    menu.show_all()
    return menu


def _run_appindicator(client) -> int:
    gi, Gtk = _gtk()
    module = _indicator_module()
    from gi.repository import GLib  # noqa: PLC0415

    indicator = module.Indicator.new(
        "nightfall", icon_path(), module.IndicatorCategory.APPLICATION_STATUS
    )
    indicator.set_title(TITLE)
    try:
        indicator.set_icon_full(icon_path(), TITLE)
    except (AttributeError, TypeError):
        pass
    indicator.set_status(module.IndicatorStatus.ACTIVE)

    server = ipc.Server(lambda payload: _handle(payload, Gtk, None), path=session.tray_socket())
    server.listen()

    menu = _build_menu(Gtk, client, Gtk.main_quit)
    indicator.set_menu(menu)
    try:  # средняя/вторичная кнопка открывает окно
        indicator.set_secondary_activate_target(menu.get_children()[0])
    except (AttributeError, TypeError):
        pass

    Gtk.main()
    server.stop()
    return 0


def _run_statusicon(client) -> int:
    _gi, Gtk = _gtk()
    from gi.repository import GLib  # noqa: PLC0415
    icon = Gtk.StatusIcon()
    if ASSET.exists():
        icon.set_from_file(icon_path())
    else:
        icon.set_from_icon_name(icon_path())
    icon.set_tooltip_text(f"{TITLE} — {_status_text(client)}")
    icon.set_visible(True)

    menu = _build_menu(Gtk, client, Gtk.main_quit)
    icon.connect("activate", lambda *_: _show())
    icon.connect(
        "popup-menu",
        lambda widget, button, activate_time: menu.popup(
            None, None, Gtk.StatusIcon.position_menu, widget, button, activate_time
        ),
    )

    server = ipc.Server(lambda payload: _handle(payload, Gtk, None), path=session.tray_socket())
    server.listen()

    state = {"embedded": True}

    def check_embedded():
        # в системе нет XEmbed-трея (например, чистый Wayland) — этот бэкенд
        # бесполезен, пусть launcher попробует следующий
        if not icon.is_embedded():
            state["embedded"] = False
            print("statusicon: иконка не прижилась в трее", file=sys.stderr)
            Gtk.main_quit()
        return False

    GLib.timeout_add(2000, check_embedded)

    Gtk.main()
    server.stop()
    return 0 if state["embedded"] else 3


# --------------------------------------------------------------------------- #
#  yad
# --------------------------------------------------------------------------- #
def _run_yad(client) -> int:
    script = session.launcher()
    yad = shutil.which("yad")
    if script is None or yad is None:
        return 1
    python = sys.executable

    def command(*args: str) -> str:
        # пути из --state/--socket обязательны: иначе клики в меню попали бы
        # в другой экземпляр программы (с путями по умолчанию)
        return " ".join(shlex.quote(part) for part in [python, str(script), *args, *session.child_args()])

    menu = "|".join(
        [
            f"Открыть окно!{command('--show')}",
            f"Скрыть окно!{command('--hide')}",
            "!",
            f"Остановить службу и выйти!{command('--quit')}",
            "",
        ]
    )
    process = subprocess.Popen(
        [
            yad,
            "--notification",
            f"--image={icon_path()}",
            f"--text={TITLE} — {_status_text(client)}",
            f"--command={command('--show')}",
            f"--menu={menu}",
            "--title=Nightfall",
        ],
        stdin=subprocess.DEVNULL,
    )

    stop = threading.Event()

    def handler(payload: dict) -> dict:
        cmd = payload.get("cmd")
        if cmd == "ping":
            return {"ok": True, "pid": __import__("os").getpid(), "backend": "yad"}
        if cmd == "quit":
            stop.set()
            return {"ok": True, "pid": __import__("os").getpid()}
        return {"ok": False, "error": f"неизвестная команда: {cmd}"}

    server = ipc.Server(handler, path=session.tray_socket())
    server.listen()
    try:
        while not stop.is_set() and process.poll() is None:
            stop.wait(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        if process.poll() is None:
            process.terminate()
    return 0


# --------------------------------------------------------------------------- #
def _handle(payload: dict, Gtk, _unused) -> dict:
    cmd = payload.get("cmd")
    import os

    if cmd == "ping":
        return {"ok": True, "pid": os.getpid(), "backend": "gtk"}
    if cmd == "quit":
        Gtk.main_quit()
        return {"ok": True, "pid": os.getpid()}
    if cmd == "show":
        _show()
        return {"ok": True}
    return {"ok": False, "error": f"неизвестная команда: {cmd}"}


def run(backend: Optional[str] = None) -> int:
    if session.tray_running() and backend not in (None, "auto"):
        return 0  # иконка уже есть — вторую не создаём
    client = session.make_client()
    if backend in (None, "auto"):
        found = available_backends()
        backend = found[0] if found else None
    if backend is None:
        print("нет доступного бэкенда трея", file=sys.stderr)
        return 1
    runners = {
        "appindicator": _run_appindicator,
        "statusicon": _run_statusicon,
        "yad": _run_yad,
    }
    if backend not in runners:
        print(f"неизвестный бэкенд трея: {backend}", file=sys.stderr)
        return 2
    # если бэкенд есть в системе, но иконка не прижилась (нет хоста в панели),
    # пробуем следующий — и в конце концов yad
    chain = [backend] + [name for name in available_backends() if name != backend]
    if "yad" not in chain and shutil.which("yad"):
        chain.append("yad")
    for name in chain:
        if session.tray_running():
            return 0  # иконка уже есть
        code = runners[name](client)
        if code != 3:
            return code
    return 3
