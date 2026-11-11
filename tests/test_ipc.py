"""Тесты на локальный сокет: второй экземпляр не должен отбирать адрес."""

from __future__ import annotations

import tempfile
import socket
import unittest
from pathlib import Path

from nightfall import ipc


class ServerListenTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="nf-ipc-"))
        self.path = self.dir / "test.sock"

    def tearDown(self) -> None:
        for item in self.dir.iterdir():
            try:
                item.unlink()
            except OSError:
                pass
        try:
            self.dir.rmdir()
        except OSError:
            pass

    def test_live_socket_is_not_taken_over(self) -> None:
        first = ipc.Server(lambda payload: {"ok": True}, path=self.path)
        first.listen()
        try:
            second = ipc.Server(lambda payload: {"ok": True}, path=self.path)
            with self.assertRaises(ipc.AlreadyRunning):
                second.listen()
            # первый продолжает отвечать
            self.assertTrue(ipc.is_running(path=self.path))
        finally:
            first.stop()

    def test_stale_socket_file_is_replaced(self) -> None:
        # файл остался от упавшего процесса: слушателя нет, можно занимать
        self.path.write_text("")
        server = ipc.Server(lambda payload: {"ok": True}, path=self.path)
        server.listen()
        try:
            self.assertTrue(ipc.is_running(path=self.path))
        finally:
            server.stop()

    def test_socket_is_private(self) -> None:
        server = ipc.Server(lambda payload: {"ok": True}, path=self.path)
        server.listen()
        try:
            mode = self.path.stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)
        finally:
            server.stop()

    def test_dead_socket_is_not_live(self) -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.bind(str(self.path))
            # слушателя нет — подключиться некому
        self.assertFalse(ipc.socket_is_live(self.path))


if __name__ == "__main__":
    unittest.main()
