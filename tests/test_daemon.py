"""Тесты фоновой службы и связи с ней по Unix-сокету."""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from nightfall import ipc
from nightfall.client import DaemonClient, view_from_dict
from nightfall.controller import Controller, view_to_dict
from nightfall.daemon import Daemon
from nightfall.storage import Store


def dt(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M")


class IpcTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.socket = Path(self.tmp.name) / "test.sock"

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip(self):
        def handler(payload):
            return {"ok": True, "echo": payload.get("cmd"), "pid": 4242}

        server = ipc.Server(handler, path=self.socket)
        server.listen()
        try:
            self.assertTrue(ipc.is_running(path=self.socket))
            reply = ipc.request({"cmd": "ping"}, path=self.socket)
            self.assertEqual(reply["echo"], "ping")
            self.assertEqual(ipc.daemon_pid(path=self.socket), 4242)
        finally:
            server.stop()
        self.assertFalse(ipc.is_running(path=self.socket))

    def test_missing_daemon_raises(self):
        with self.assertRaises(ipc.IpcError):
            ipc.request({"cmd": "ping"}, path=self.socket, timeout=0.3)

    def test_broken_handler_reports_error(self):
        def handler(_payload):
            raise ValueError("сломалось")

        server = ipc.Server(handler, path=self.socket)
        server.listen()
        try:
            reply = ipc.request({"cmd": "ping"}, path=self.socket)
            self.assertFalse(reply["ok"])
            self.assertIn("сломалось", reply["error"])
        finally:
            server.stop()


class DaemonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state.json"
        self.socket = Path(self.tmp.name) / "night.sock"
        self.store = Store(self.state)
        self.clock_now = dt("2026-11-11 19:00")
        self.daemon = Daemon(
            self.store, socket=self.socket, popups=False, clock=lambda: self.clock_now
        )
        self.daemon.server.listen()
        self.client = DaemonClient(socket_path=self.socket, state_path=self.state)

    def tearDown(self):
        self.daemon.server.stop()
        self.tmp.cleanup()

    def test_status_matches_controller_view(self):
        view = self.client.view()
        self.assertEqual(view.night.evening.isoformat(), "2026-11-11")
        self.assertEqual(view.night.target, dt("2026-11-12 01:00"))
        self.assertEqual(self.client.pid, __import__("os").getpid())

    def test_postpone_moves_shutdown(self):
        before = self.client.view().night.shutdown_at
        self.client.postpone(15)
        after = self.client.view()
        self.assertEqual(after.night.shutdown_at, before + timedelta(minutes=15))
        self.assertEqual(after.postponed, 15)

    def test_undo_postpone(self):
        self.client.postpone(30)
        self.client.undo_postpone()
        self.assertEqual(self.client.view().postponed, 0)

    def test_settings_change_through_socket(self):
        view = self.client.view()
        settings = view.settings
        settings.step_minutes = 45
        settings.goal_time = dt("2026-11-11 22:00").time()
        self.client.apply_settings(settings)
        self.assertEqual(self.client.view().settings.step_minutes, 45)

    def test_dry_run_flag(self):
        self.client.set_dry_run(False)
        self.assertFalse(self.client.view().settings.dry_run)
        self.client.set_dry_run(True)
        self.assertTrue(self.client.view().settings.dry_run)

    def test_shutdown_now_records_actual(self):
        self.client.shutdown_now()
        # план сразу перескочил на следующую ночь, а факт остался в истории
        nights = self.store.load().nights
        self.assertIsNotNone(nights[0].actual)
        self.assertTrue(nights[0].settled)
        self.assertEqual(self.client.view().night.evening.isoformat(), "2026-11-12")

    def test_export_history(self):
        text = self.client.export_history()
        self.assertIn("дата вечера", text)

    def test_reset(self):
        self.client.reset_progress()
        self.assertIsNone(self.client.view().night.actual)

    def test_unknown_command(self):
        reply = ipc.request({"cmd": "нет-такой"}, path=self.socket)
        self.assertFalse(reply["ok"])

    def test_quit_stops_daemon(self):
        reply = ipc.request({"cmd": "quit"}, path=self.socket)
        self.assertTrue(reply["ok"])
        self.assertTrue(self.daemon._stop.is_set())

    def test_client_survives_snapshot_round_trip(self):
        raw = view_to_dict(self.daemon.controller.view())
        restored = view_from_dict(raw)
        self.assertEqual(restored.night.target, self.daemon.controller.view().night.target)
        self.assertEqual(restored.settings.step_minutes,
                         self.daemon.controller.state.settings.step_minutes)


class OfflineFallbackTest(unittest.TestCase):
    """Без демона окно и CLI должны честно сообщать об этом, а не падать."""

    def test_view_without_daemon(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = DaemonClient(socket_path=Path(tmp) / "nope.sock")
            self.assertFalse(client.is_running())
            with self.assertRaises(ipc.IpcError):
                client.view()

    def test_offline_view_from_file(self):
        from nightfall.cli import _offline_view

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state.json")
            Controller(store, desktop_notifications=False).tick(dt("2026-11-11 19:00"))
            view = _offline_view(store)
            self.assertEqual(view.night.evening.isoformat(), "2026-11-11")


if __name__ == "__main__":
    unittest.main()
