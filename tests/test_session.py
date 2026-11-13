"""Тесты на то, как окно, трей и служба находят друг друга."""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nightfall import ipc, session


class PathsTest(unittest.TestCase):
    def tearDown(self) -> None:
        session.configure(None, None)

    def test_defaults_have_no_child_args(self) -> None:
        session.configure(None, None)
        self.assertEqual(session.child_args(), [])
        self.assertIsNone(session.state_path())
        self.assertIsNone(session.socket_path())

    def test_child_args_repeat_explicit_paths(self) -> None:
        session.configure("/tmp/x/state.json", "/tmp/x/n.sock")
        self.assertEqual(
            session.child_args(),
            ["--state", "/tmp/x/state.json", "--socket", "/tmp/x/n.sock"],
        )

    def test_spawn_passes_paths_to_children(self) -> None:
        session.configure("/tmp/y/s.json", "/tmp/y/n.sock")
        with mock.patch.object(subprocess, "Popen") as popen:
            self.assertTrue(session.spawn(["--service"]))
        argv = popen.call_args[0][0]
        self.assertIn("--service", argv)
        self.assertEqual(argv[-4:], ["--state", "/tmp/y/s.json", "--socket", "/tmp/y/n.sock"])

    def test_spawn_gui_asks_for_window_explicitly(self) -> None:
        with mock.patch.object(session, "gui_interpreter", return_value="/usr/bin/python3"), \
                mock.patch.object(subprocess, "Popen") as popen:
            self.assertTrue(session.spawn_gui())
        argv = popen.call_args[0][0]
        # без --window процесс поднимет только иконку и тихо завершится
        self.assertIn("--window", argv)
        self.assertEqual(argv[0], "/usr/bin/python3")

    def test_make_client_uses_same_paths(self) -> None:
        session.configure("/tmp/z/s.json", "/tmp/z/n.sock")
        client = session.make_client()
        self.assertEqual(Path(client.socket_path), Path("/tmp/z/n.sock"))


class WindowTest(unittest.TestCase):
    def test_show_prefers_live_window(self) -> None:
        with mock.patch.object(session, "request_gui", return_value=True) as request, \
                mock.patch.object(session, "spawn_gui") as spawn:
            self.assertTrue(session.show_window())
        request.assert_called_once_with("show")
        spawn.assert_not_called()

    def test_show_starts_window_when_nothing_runs(self) -> None:
        with mock.patch.object(session, "request_gui", return_value=False), \
                mock.patch.object(session, "spawn_gui", return_value=True) as spawn:
            self.assertTrue(session.show_window())
        spawn.assert_called_once_with()

    def test_toggle_asks_live_window(self) -> None:
        with mock.patch.object(session, "request_gui", return_value=True) as request, \
                mock.patch.object(session, "spawn_gui") as spawn:
            self.assertTrue(session.toggle_window())
        request.assert_called_once_with("toggle")
        spawn.assert_not_called()

    def test_toggle_opens_window_when_none_runs(self) -> None:
        with mock.patch.object(session, "request_gui", return_value=False), \
                mock.patch.object(session, "spawn_gui", return_value=True) as spawn:
            self.assertTrue(session.toggle_window())
        spawn.assert_called_once_with()

    def test_gui_visible_reports_visibility(self) -> None:
        answer = {"ok": True, "visible": False}
        with mock.patch.object(ipc, "request", return_value=answer):
            self.assertFalse(session.gui_visible())
        with mock.patch.object(ipc, "request", side_effect=ipc.IpcError("нет окна")):
            self.assertIsNone(session.gui_visible())

    def test_hide_and_quit_never_launch_anything(self) -> None:
        with mock.patch.object(session, "request_gui", return_value=False), \
                mock.patch.object(session, "spawn_gui") as spawn:
            self.assertFalse(session.hide_window())
            self.assertFalse(session.quit_window())
        spawn.assert_not_called()


class TrayDetectTest(unittest.TestCase):
    def _probe(self, mapping: dict[str, str]):
        def fake_run(argv, **_kwargs):
            interpreter = argv[0]
            out = mapping.get(interpreter, "")
            return subprocess.CompletedProcess(argv, 0, out + "\n", "")

        return mock.patch.object(subprocess, "run", side_effect=fake_run)

    def test_no_tray_at_all(self) -> None:
        with self._probe({}):
            self.assertEqual(session.detect_all(), {})
            self.assertEqual(session.detect_tray(), (None, None))

    def test_best_backend_wins_regardless_of_interpreter(self) -> None:
        mapping = {
            "/usr/local/bin/python3": "yad",
            "/usr/bin/python3": "statusicon appindicator",
        }
        with mock.patch.object(session, "_candidate_interpreters",
                               return_value=list(mapping)), self._probe(mapping):
            found = session.detect_all()
            self.assertEqual(found["yad"], "/usr/local/bin/python3")
            self.assertEqual(found["appindicator"], "/usr/bin/python3")
            # классический XEmbed-трей важнее: он отдаёт приложению клик по иконке
            self.assertEqual(session.detect_tray(), ("statusicon", "/usr/bin/python3"))

    def test_statusicon_preferred_over_appindicator(self) -> None:
        mapping = {"/usr/bin/python3": "appindicator statusicon"}
        with mock.patch.object(session, "_candidate_interpreters",
                               return_value=list(mapping)), self._probe(mapping):
            self.assertEqual(session.detect_tray()[0], "statusicon")

    def test_interpreter_for_forced_backend(self) -> None:
        mapping = {"/usr/local/bin/python3": "yad", "/usr/bin/python3": "statusicon"}
        with mock.patch.object(session, "_candidate_interpreters",
                               return_value=list(mapping)), self._probe(mapping):
            self.assertEqual(session.interpreter_for("statusicon"), "/usr/bin/python3")
            self.assertIsNone(session.interpreter_for("appindicator"))


def alive_process():
    """Живой дочерний процесс (Popen-заглушка)."""
    process = mock.Mock()
    process.poll.return_value = None
    return process


def dead_process():
    process = mock.Mock()
    process.poll.return_value = 1
    return process


class EnsureTrayTest(unittest.TestCase):
    def setUp(self) -> None:
        session._forget_backend()

    def tearDown(self) -> None:
        session._forget_backend()

    def test_no_backend_means_no_tray(self) -> None:
        with mock.patch.object(session, "tray_running", return_value=False), \
                mock.patch.dict("os.environ", {"NIGHTFALL_TRAY_BACKEND": ""}), \
                mock.patch.object(session, "detect_all", return_value={}), \
                mock.patch.object(session, "spawn") as spawn:
            self.assertFalse(session.ensure_tray())
        spawn.assert_not_called()

    def test_existing_icon_is_reused(self) -> None:
        with mock.patch.object(session, "tray_running", return_value=True), \
                mock.patch.object(session, "spawn") as spawn:
            self.assertTrue(session.ensure_tray())
        spawn.assert_not_called()

    def test_forced_backend_uses_interpreter_with_gtk(self) -> None:
        with mock.patch.object(session, "tray_running", return_value=False), \
                mock.patch.dict("os.environ", {"NIGHTFALL_TRAY_BACKEND": "statusicon"}), \
                mock.patch.object(session, "interpreter_for", return_value="/usr/bin/python3"), \
                mock.patch.object(session, "spawn", return_value=alive_process()) as spawn, \
                mock.patch.object(session, "_wait_for_tray", return_value=True):
            self.assertTrue(session.ensure_tray())
        self.assertEqual(spawn.call_args[0][0], ["--tray", "--tray-backend", "statusicon"])
        self.assertEqual(spawn.call_args[1]["interpreter"], "/usr/bin/python3")

    def test_dead_backend_falls_back_to_next(self) -> None:
        """Первый бэкенд упал — пробуем следующий, а не сдаёмся сразу."""
        with mock.patch.object(session, "tray_running", return_value=False), \
                mock.patch.dict("os.environ", {"NIGHTFALL_TRAY_BACKEND": ""}), \
                mock.patch.object(session, "_tray_attempts",
                                  return_value=[("statusicon", "/usr/bin/python3"),
                                                ("yad", "/usr/local/bin/python3")]), \
                mock.patch.object(session, "spawn",
                                  side_effect=[dead_process(), alive_process()]) as spawn, \
                mock.patch.object(session, "_wait_for_tray", side_effect=[False, True]):
            self.assertTrue(session.ensure_tray())
        self.assertEqual(spawn.call_count, 2)
        self.assertEqual(spawn.call_args[0][0], ["--tray", "--tray-backend", "yad"])

    def test_failed_start_is_reported_honestly(self) -> None:
        with mock.patch.object(session, "tray_running", return_value=False), \
                mock.patch.object(session, "_tray_attempts",
                                  return_value=[("yad", "/usr/local/bin/python3")]), \
                mock.patch.object(session, "spawn", return_value=dead_process()), \
                mock.patch.object(session, "_wait_for_tray", return_value=False):
            self.assertFalse(session.ensure_tray())

    def test_slow_start_still_counts_as_success(self) -> None:
        """На холодную GTK стартует секундами: подождать дешевле, чем открыть окно."""
        answers = [False, False, False, True]  # до появления сокета и после
        with mock.patch.object(session, "tray_running", side_effect=lambda: answers.pop(0)
                               if answers else True), \
                mock.patch.object(session, "spawn", return_value=alive_process()), \
                mock.patch.object(session, "_tray_attempts",
                                  return_value=[("statusicon", "/usr/bin/python3")]):
            self.assertTrue(session.ensure_tray())

    def test_overall_wait_is_capped(self) -> None:
        """Три бэкенда по 15 секунд — это минута молчания; общий срок короче."""
        with mock.patch.object(session, "tray_running", return_value=False), \
                mock.patch.object(session, "TRAY_TOTAL_SECONDS", 0.0), \
                mock.patch.object(session, "_tray_attempts",
                                  return_value=[("statusicon", "/usr/bin/python3"),
                                                ("yad", "/usr/local/bin/python3")]), \
                mock.patch.object(session, "spawn") as spawn:
            self.assertFalse(session.ensure_tray())
        spawn.assert_not_called()

    def test_working_backend_is_remembered(self) -> None:
        with mock.patch.object(session, "tray_running", return_value=False), \
                mock.patch.object(session, "_tray_attempts",
                                  return_value=[("yad", "/usr/local/bin/python3")]), \
                mock.patch.object(session, "spawn", return_value=alive_process()), \
                mock.patch.object(session, "_wait_for_tray", return_value=True):
            self.assertTrue(session.ensure_tray())
        # следующий запуск начнёт с того, что уже сработало
        self.assertEqual(session._cached_backend()[0], "yad")

    def test_cache_is_dropped_after_failure(self) -> None:
        session._remember_backend("yad", "/usr/local/bin/python3")
        self.assertIsNotNone(session._cached_backend())
        with mock.patch.object(session, "tray_running", return_value=False), \
                mock.patch.object(session, "_tray_attempts",
                                  return_value=[("yad", "/usr/local/bin/python3")]), \
                mock.patch.object(session, "spawn", return_value=dead_process()), \
                mock.patch.object(session, "_wait_for_tray", return_value=False):
            self.assertFalse(session.ensure_tray())
        self.assertIsNone(session._cached_backend())

    def test_quit_everything_stops_window_tray_and_daemon(self) -> None:
        client = mock.Mock()
        with mock.patch.object(session, "quit_window") as quit_window, \
                mock.patch.object(session, "stop_tray") as stop_tray:
            session.quit_everything(client)
        quit_window.assert_called_once_with()
        stop_tray.assert_called_once_with()
        client.quit_daemon.assert_called_once_with()

    def test_quit_everything_survives_dead_daemon(self) -> None:
        client = mock.Mock()
        client.quit_daemon.side_effect = ipc.IpcError("нет связи")
        with mock.patch.object(session, "quit_window"), mock.patch.object(session, "stop_tray"):
            session.quit_everything(client)  # не должно бросить исключение


class StartupTest(unittest.TestCase):
    """По умолчанию программа поднимает только иконку в трее, без окна."""

    def test_default_start_keeps_window_closed(self) -> None:
        from nightfall.cli import startup_plan

        self.assertFalse(startup_plan(with_window=False, tray_available=True))

    def test_explicit_flag_opens_window(self) -> None:
        from nightfall.cli import startup_plan

        self.assertTrue(startup_plan(with_window=True, tray_available=True))
        self.assertTrue(startup_plan(with_window=True, tray_available=False))

    def test_window_is_fallback_when_tray_missing(self) -> None:
        from nightfall.cli import startup_plan

        # без иконки и без окна пользователь не увидел бы вообще ничего
        self.assertTrue(startup_plan(with_window=False, tray_available=False))

    def test_headless_session_does_not_open_window(self) -> None:
        from nightfall.cli import startup_plan

        # нет ни X11, ни Wayland — окно открывать некуда, служба работает молча
        self.assertFalse(startup_plan(with_window=False, tray_available=False,
                                      has_display=False))
        self.assertTrue(startup_plan(with_window=True, tray_available=True,
                                     has_display=True))

    def test_display_detection(self) -> None:
        from nightfall.cli import display_available

        with mock.patch.dict("os.environ", {"DISPLAY": ":0"}, clear=False):
            self.assertTrue(display_available())
        # без дисплея не сдаёмся молча: cli спрашивает общий поиск
        with mock.patch.object(session, "discover_display", return_value=None):
            self.assertFalse(display_available())

    def test_startup_without_tray_opens_window(self) -> None:
        from nightfall import cli

        client = mock.Mock()
        with mock.patch.object(cli, "display_available", return_value=True), \
                mock.patch.object(session, "ensure_tray", return_value=False), \
                mock.patch.object(cli, "run_window", return_value=7) as run_window, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.run_startup(client, with_window=False), 7)
        run_window.assert_called_once_with(client)

    def test_startup_with_tray_leaves_window_closed(self) -> None:
        from nightfall import cli

        client = mock.Mock()
        output = io.StringIO()
        with mock.patch.object(cli, "display_available", return_value=True), \
                mock.patch.object(session, "ensure_tray", return_value=True), \
                mock.patch.object(cli, "run_window") as run_window, \
                contextlib.redirect_stdout(output):
            self.assertEqual(cli.run_startup(client, with_window=False), 0)
        run_window.assert_not_called()
        self.assertIn("иконка", output.getvalue())


class DiscoverDisplayTest(unittest.TestCase):
    """Автозапуск часто срабатывает раньше оконной сессии — дисплей ищем сами."""

    def setUp(self) -> None:
        self.env = mock.patch.dict("os.environ", {}, clear=False)
        self.env.start()
        for name in ("DISPLAY", "WAYLAND_DISPLAY"):
            os.environ.pop(name, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.x11 = Path(self.tmp.name) / "X11"
        self.x11.mkdir()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def _socket(self, path: Path) -> socket.socket:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen(1)
        self.addCleanup(server.close)
        return server

    def test_inherited_display_is_kept(self) -> None:
        os.environ["DISPLAY"] = ":7"
        self.assertEqual(session.discover_display(self.x11), ":7")

    def test_live_x_socket_is_found(self) -> None:
        self._socket(self.x11 / "X0")
        self.assertEqual(session.discover_display(self.x11), ":0")
        self.assertEqual(os.environ["DISPLAY"], ":0")  # детям тоже достанется

    def test_stale_socket_is_ignored(self) -> None:
        (self.x11 / "X0").write_text("")  # файл остался от выключенного сервера
        runtime = Path(self.tmp.name) / "empty"
        runtime.mkdir()
        with mock.patch.object(ipc, "runtime_dir", return_value=runtime):
            self.assertIsNone(session.discover_display(self.x11))

    def test_lowest_display_wins(self) -> None:
        self._socket(self.x11 / "X1")
        self._socket(self.x11 / "X0")
        self.assertEqual(session.discover_display(self.x11), ":0")

    def test_wayland_socket_is_found(self) -> None:
        runtime = Path(self.tmp.name) / "run"
        runtime.mkdir()
        self._socket(runtime / "wayland-0")
        with mock.patch.object(ipc, "runtime_dir", return_value=runtime):
            self.assertEqual(session.discover_display(self.x11), "wayland-0")
        self.assertEqual(os.environ["WAYLAND_DISPLAY"], "wayland-0")

    def test_no_session_at_all(self) -> None:
        with mock.patch.object(ipc, "runtime_dir", return_value=self.x11):
            self.assertIsNone(session.discover_display(self.x11))

    def test_display_available_uses_discovery(self) -> None:
        # не полагаемся на реальный /tmp/.X11-unix машины, где гоняются тесты
        with mock.patch.object(session, "discover_display", return_value=":0"):
            self.assertTrue(session.display_available())
        with mock.patch.object(session, "discover_display", return_value=None):
            self.assertFalse(session.display_available())


class GuiInterpreterTest(unittest.TestCase):
    """Иконка и окно могут запускаться разными Python — проверяем, что нужным."""

    def setUp(self) -> None:
        session._gui_interpreter_cache = None
        self.addCleanup(setattr, session, "_gui_interpreter_cache", None)

    def test_prefers_current_python_when_it_has_tkinter(self) -> None:
        with mock.patch.object(session, "_tkinter_works", return_value=True):
            self.assertEqual(session.gui_interpreter(), session.sys.executable)

    def test_falls_back_to_python_with_tkinter(self) -> None:
        def has_tkinter(interpreter: str) -> bool:
            return interpreter == "/usr/bin/python3"

        with mock.patch.object(session, "_tkinter_works", side_effect=has_tkinter), \
                mock.patch.object(session, "_candidate_interpreters",
                                  return_value=[session.sys.executable, "/usr/bin/python3"]):
            self.assertEqual(session.gui_interpreter(), "/usr/bin/python3")

    def test_remembers_the_choice(self) -> None:
        with mock.patch.object(session, "_tkinter_works", return_value=True) as probe:
            session.gui_interpreter()
            session.gui_interpreter()
        self.assertEqual(probe.call_count, 1)

    def test_tkinter_map_reports_every_candidate(self) -> None:
        with mock.patch.object(session, "_tkinter_works",
                               side_effect=lambda path: path.endswith("python3.13")), \
                mock.patch.object(session, "_candidate_interpreters",
                                  return_value=["/usr/local/bin/python3.13", "/usr/bin/python3"]):
            self.assertEqual(session.tkinter_map(),
                             {"/usr/local/bin/python3.13": True, "/usr/bin/python3": False})


class ChildOutputTest(unittest.TestCase):
    """Вывод детей идёт в журнал: иначе падение окна неотличимо от «не откликается»."""

    def test_child_output_goes_to_journal(self) -> None:
        import tempfile
        from pathlib import Path as PathType

        from nightfall import notify

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {"XDG_STATE_HOME": tmp}):
                stream = session._child_stream()
                self.assertIsNot(stream, subprocess.DEVNULL)
                stream.write("окно упало: нет tkinter\n")
                stream.close()
                self.assertIn("нет tkinter", notify.log_path().read_text(encoding="utf-8"))
                self.assertTrue(PathType(notify.log_path()).exists())


class TrayClickTest(unittest.TestCase):
    """Клик по иконке обязан оставлять след: и в журнале, и на экране."""

    def setUp(self) -> None:
        from nightfall import tray

        self.tray = tray
        tray._last_failure = 0.0

    def test_click_logs_and_opens_window(self) -> None:
        with mock.patch.object(session, "toggle_window", return_value=True), \
                mock.patch.object(self.tray.notify, "log") as log, \
                mock.patch.object(session, "spawn") as spawn:
            self.tray._toggle()
        self.assertIn("клик по иконке", log.call_args[0][0])
        spawn.assert_not_called()

    def test_broken_click_shows_card_instead_of_silence(self) -> None:
        with mock.patch.object(session, "toggle_window", return_value=False), \
                mock.patch.object(self.tray.notify, "log"), \
                mock.patch.object(session, "spawn") as spawn:
            self.tray._toggle()
        argv = spawn.call_args[0][0]
        self.assertEqual(argv[0], "--popup-json")
        # json пишется ASCII-escape'ами: локаль может быть не UTF-8
        self.assertEqual(json.loads(argv[1])["title"], "Окно не открылось")

    def test_failure_card_is_not_repeated_every_click(self) -> None:
        with mock.patch.object(session, "toggle_window", return_value=False), \
                mock.patch.object(self.tray.notify, "log"), \
                mock.patch.object(session, "spawn") as spawn:
            self.tray._toggle()
            self.tray._toggle()
            self.tray._toggle()
        self.assertEqual(spawn.call_count, 1)
