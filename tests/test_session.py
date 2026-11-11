"""Тесты на то, как окно, трей и служба находят друг друга."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
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


class EnsureTrayTest(unittest.TestCase):
    def test_no_backend_means_no_tray(self) -> None:
        with mock.patch.object(session, "tray_running", return_value=False), \
                mock.patch.dict("os.environ", {"NIGHTFALL_TRAY_BACKEND": ""}), \
                mock.patch.object(session, "detect_tray", return_value=(None, None)), \
                mock.patch.object(session, "spawn") as spawn:
            self.assertFalse(session.ensure_tray())
        spawn.assert_not_called()

    def test_existing_icon_is_reused(self) -> None:
        with mock.patch.object(session, "tray_running", return_value=True), \
                mock.patch.object(session, "spawn") as spawn:
            self.assertTrue(session.ensure_tray())
        spawn.assert_not_called()

    def test_forced_backend_uses_interpreter_with_gtk(self) -> None:
        with mock.patch.object(session, "tray_running", side_effect=[False, True]), \
                mock.patch.dict("os.environ", {"NIGHTFALL_TRAY_BACKEND": "statusicon"}), \
                mock.patch.object(session, "interpreter_for", return_value="/usr/bin/python3"), \
                mock.patch.object(session, "spawn", return_value=True) as spawn:
            self.assertTrue(session.ensure_tray())
        self.assertEqual(spawn.call_args[0][0], ["--tray", "--tray-backend", "statusicon"])
        self.assertEqual(spawn.call_args[1]["interpreter"], "/usr/bin/python3")

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


if __name__ == "__main__":
    unittest.main()


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
        env = {key: value for key, value in os.environ.items()
               if key not in ("DISPLAY", "WAYLAND_DISPLAY")}
        with mock.patch.dict("os.environ", env, clear=True):
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
