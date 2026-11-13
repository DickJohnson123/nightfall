"""Интеграционные тесты: неделя жизни программы с ускоренным временем."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from nightfall import schedule
from nightfall.controller import Controller
from nightfall.schedule import fmt_hm
from nightfall.storage import State, Store


def dt(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M")


class SimulationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        self.store = Store(self.path)
        self.now = dt("2026-11-11 19:00")
        # часы подменяем: тесты про неделю не должны зависеть от того, какое
        # сегодня число на машине, где их гоняют
        self.controller = Controller(
            self.store, desktop_notifications=False, clock=lambda: self.now
        )

    def tearDown(self):
        self.tmp.cleanup()

    def run_until(self, moment: datetime, step_minutes: int = 5, on_tick=None):
        while self.now < moment:
            view = self.controller.tick(self.now)
            if on_tick:
                on_tick(self.controller, view)
            self.now += timedelta(minutes=step_minutes)
        self.controller.tick(self.now)

    def test_week_with_one_postponement(self):
        postponed = set()

        def maybe_postpone(ctrl, view):
            night = view.night
            # во вторую ночь один раз переносим на 20 минут
            if (
                view.phase == "evening"
                and night.evening.isoformat() == "2026-11-12"
                and night.evening not in postponed
            ):
                ctrl.postpone(20)
                postponed.add(night.evening)

        self.run_until(dt("2026-11-17 12:00"), on_tick=maybe_postpone)

        nights = self.controller.state.nights
        scheduled = [(n.evening.isoformat(), fmt_hm(n.target), fmt_hm(n.shutdown_at), n.actual is not None)
                     for n in nights]
        self.assertEqual(
            scheduled[:5],
            [
                ("2026-11-11", "01:00", "01:00", True),
                ("2026-11-12", "00:30", "00:50", True),   # +20 мин переноса
                ("2026-11-13", "00:20", "00:20", True),   # 00:50 − 30 мин
                ("2026-11-14", "23:50", "23:50", True),
                ("2026-11-15", "23:20", "23:20", True),
            ],
        )
        self.assertTrue(postponed)

    def test_actual_time_is_stored_and_reloaded(self):
        self.run_until(dt("2026-11-13 02:00"))
        first = self.controller.state.nights[0]
        self.assertIsNotNone(first.actual)
        self.assertEqual(first.actual.strftime("%H:%M"), "01:00")

        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["version"], 1)
        self.assertEqual(raw["nights"][0]["actual"], "2026-11-12T01:00")

        reloaded = Controller(Store(self.path), desktop_notifications=False)
        self.assertEqual(
            [n.to_dict() for n in reloaded.state.nights],
            [n.to_dict() for n in self.controller.state.nights],
        )

    def test_reminders_fire_ten_times(self):
        self.controller.tick(self.now)
        fired = []

        def collect(ctrl, view):
            fired.append(len(view.night.fired))

        self.run_until(dt("2026-11-12 01:00"), step_minutes=1, on_tick=collect)
        night = self.controller.state.nights[0]
        self.assertEqual(sorted(night.fired), [5, 10, 15, 20, 25, 30, 35, 40, 45, 50])

    def test_dry_run_does_not_call_poweroff(self):
        calls = []
        import nightfall.notify as notify_module
        original = notify_module.run_command
        notify_module.run_command = lambda command, dry_run=True: calls.append(command) or (True, "ok")
        try:
            settings = self.controller.state.settings
            settings.dry_run = True
            self.controller.apply_settings(settings)
            self.run_until(dt("2026-11-12 01:05"))
        finally:
            notify_module.run_command = original
        self.assertEqual(calls, [])
        self.assertEqual(self.controller.state.nights[0].actual.strftime("%H:%M"), "01:00")

    def test_postpone_after_shutdown_is_ignored(self):
        self.run_until(dt("2026-11-12 01:10"))
        night = self.controller.state.nights[0]
        before = night.postponed_minutes
        self.controller.postpone(30, dt("2026-11-12 01:10"))
        self.assertEqual(night.postponed_minutes, before)

    def test_settings_change_keeps_history(self):
        self.run_until(dt("2026-11-13 03:00"))
        history_before = [n.to_dict() for n in self.controller.state.nights if n.settled]
        settings = self.controller.state.settings
        settings.step_minutes = 10
        settings.goal_time = schedule.parse_time("22:30")
        self.controller.apply_settings(settings, dt("2026-11-13 12:00"))
        history_after = [n.to_dict() for n in self.controller.state.nights if n.settled]
        self.assertEqual(history_before, history_after)
        plan = self.controller.state.nights[-1]
        # предыдущая ночь закрылась в 00:30 → минус новый шаг 10 мин = 00:20
        self.assertEqual(plan.target.strftime("%H:%M"), "00:20")
        offset = plan.target - schedule.goal_anchor(plan.evening, settings)
        self.assertEqual(int(offset.total_seconds() // 60), 110)


class StorageTest(unittest.TestCase):
    def test_broken_file_is_tolerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{ это не json", encoding="utf-8")
            self.assertEqual(Store(path).load(), State())

    def test_reset_creates_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = Store(path)
            controller = Controller(store, desktop_notifications=False)
            controller.tick(dt("2026-11-11 19:00"))
            store.reset()
            self.assertEqual(Store(path).load().nights, [])
            self.assertTrue(list(Path(tmp).glob("state-*.json.bak")))


if __name__ == "__main__":
    unittest.main()
