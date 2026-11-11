"""Тесты ядра расписания (только стандартная библиотека)."""

import unittest
from datetime import date, datetime, time, timedelta

from nightfall import schedule
from nightfall.schedule import Night, Settings, fmt_hm


def dt(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M")


class FirstNightTest(unittest.TestCase):
    def test_default_starts_tonight(self):
        settings = Settings()  # старт 01:00, цель 23:00
        night = schedule.first_night(dt("2026-11-11 19:00"), settings)
        self.assertEqual(night.target, dt("2026-11-12 01:00"))
        self.assertEqual(night.evening, date(2026, 11, 11))

    def test_after_midnight_keeps_coming_night(self):
        settings = Settings()
        night = schedule.first_night(dt("2026-11-12 00:30"), settings)
        self.assertEqual(night.target, dt("2026-11-12 01:00"))
        self.assertEqual(night.evening, date(2026, 11, 11))

    def test_initial_earlier_than_goal_is_clamped(self):
        settings = Settings(initial_time=time(21, 30))
        night = schedule.first_night(dt("2026-11-11 19:00"), settings)
        self.assertEqual(fmt_hm(night.target), "23:00")


class StepTest(unittest.TestCase):
    def test_each_night_is_thirty_minutes_earlier(self):
        settings = Settings(goal_time=time(23, 0), step_minutes=30)
        night = schedule.first_night(dt("2026-11-11 19:00"), settings)
        expected = ["01:00", "00:30", "00:00", "23:30", "23:00", "23:00"]
        actual = []
        for _ in range(6):
            actual.append(fmt_hm(night.target))
            schedule.mark_shutdown(night, night.shutdown_at)  # выключились точно по плану
            night = schedule.next_night(night, settings)
        self.assertEqual(actual, expected)

    def test_goal_is_never_overshot(self):
        settings = Settings(step_minutes=90)
        night = schedule.first_night(dt("2026-11-11 19:00"), settings)
        for _ in range(4):
            schedule.mark_shutdown(night, night.shutdown_at)
            night = schedule.next_night(night, settings)
        self.assertEqual(fmt_hm(night.target), "23:00")
        self.assertEqual(night.evening, date(2026, 11, 15))


class PostponeTest(unittest.TestCase):
    def test_postponement_is_remembered_in_next_night(self):
        settings = Settings()
        night = schedule.first_night(dt("2026-11-11 19:00"), settings)
        # отложили на 30 минут и выключились в 01:30
        schedule.postpone(night, 30, dt("2026-11-12 01:00"))
        self.assertEqual(night.shutdown_at, dt("2026-11-12 01:30"))
        schedule.mark_shutdown(night, night.shutdown_at)
        following = schedule.next_night(night, settings)
        # 01:30 - 30 мин = 01:00, то есть прогресса не случилось
        self.assertEqual(fmt_hm(following.target), "01:00")

    def test_tiny_postponement_still_steps_back(self):
        settings = Settings()
        night = schedule.first_night(dt("2026-11-11 19:00"), settings)
        schedule.postpone(night, 15, dt("2026-11-12 01:00"))
        schedule.mark_shutdown(night, night.shutdown_at)
        self.assertEqual(fmt_hm(schedule.next_night(night, settings).target), "00:45")

    def test_undo_postponement(self):
        night = schedule.first_night(dt("2026-11-11 19:00"), Settings())
        schedule.postpone(night, 15, dt("2026-11-12 00:50"))
        schedule.postpone(night, 15, dt("2026-11-12 01:05"))
        self.assertEqual(night.postponed_minutes, 30)
        schedule.undo_last_postpone(night)
        self.assertEqual(night.postponed_minutes, 15)


class ReminderTest(unittest.TestCase):
    def test_sequence_is_50_to_5_every_5_minutes(self):
        settings = Settings()
        night = schedule.first_night(dt("2026-11-11 19:00"), settings)
        fired = []
        moment = night.shutdown_at - timedelta(minutes=60)
        while moment < night.shutdown_at:
            reminder = schedule.pending_reminder(night, settings, moment)
            if reminder:
                fired.append(reminder.level)
            moment += timedelta(minutes=1)
        self.assertEqual(fired, [50, 45, 40, 35, 30, 25, 20, 15, 10, 5])

    def test_no_spam_after_long_sleep(self):
        settings = Settings()
        night = schedule.first_night(dt("2026-11-11 19:00"), settings)
        reminder = schedule.pending_reminder(
            night, settings, night.shutdown_at - timedelta(minutes=7)
        )
        self.assertIsNotNone(reminder)
        self.assertEqual(reminder.level, 10)  # самое свежее из просроченных
        self.assertEqual(reminder.remaining_minutes, 7)
        # повторно то же напоминание не выдаётся
        self.assertIsNone(schedule.pending_reminder(
            night, settings, night.shutdown_at - timedelta(minutes=6)
        ))

    def test_postponement_rearms_reminders(self):
        settings = Settings()
        night = schedule.first_night(dt("2026-11-11 19:00"), settings)
        schedule.pending_reminder(night, settings, night.shutdown_at - timedelta(minutes=20))
        self.assertTrue(night.fired)
        schedule.postpone(night, 15, dt("2026-11-12 00:40"))
        self.assertEqual(night.fired, [])
        reminder = schedule.pending_reminder(night, settings, night.shutdown_at - timedelta(minutes=20))
        self.assertIsNotNone(reminder)


class MissedNightTest(unittest.TestCase):
    def test_missed_night_advances_only_one_step(self):
        settings = Settings()
        nights = [schedule.first_night(dt("2026-11-11 19:00"), settings)]
        # компьютер не включался четыре дня
        night = schedule.current_night(nights, settings, dt("2026-11-16 20:00"))
        self.assertTrue(nights[0].skipped)
        self.assertEqual(fmt_hm(night.target), "00:30")
        self.assertEqual(night.evening, date(2026, 11, 16))  # дата подтянута к будущему
        self.assertGreater(night.shutdown_at, dt("2026-11-16 20:00"))

    def test_same_night_is_returned_until_shutdown(self):
        settings = Settings()
        nights = [schedule.first_night(dt("2026-11-11 19:00"), settings)]
        night = schedule.current_night(nights, settings, dt("2026-11-11 23:10"))
        self.assertIs(night, nights[0])

    def test_late_launch_still_allows_shutdown(self):
        settings = Settings(grace_minutes=90)
        nights = [schedule.first_night(dt("2026-11-11 19:00"), settings)]
        night = schedule.current_night(nights, settings, dt("2026-11-12 01:30"))
        self.assertIs(night, nights[0])
        self.assertTrue(schedule.should_shutdown(night, dt("2026-11-12 01:30")))


class SerialisationTest(unittest.TestCase):
    def test_round_trip(self):
        settings = Settings(goal_time=time(22, 45), step_minutes=20, dry_run=False)
        night = schedule.first_night(dt("2026-11-11 19:00"), Settings())
        schedule.postpone(night, 15, dt("2026-11-12 00:50"))
        schedule.mark_shutdown(night, dt("2026-11-12 01:15"))
        restored = Night.from_dict(night.to_dict())
        self.assertEqual(restored.to_dict(), night.to_dict())
        self.assertEqual(Settings.from_dict(settings.to_dict()).to_dict(), settings.to_dict())

    def test_broken_settings_fall_back_to_defaults(self):
        self.assertEqual(Settings.from_dict({"goal_time": "ой"}).goal_time, time(23, 0))


if __name__ == "__main__":
    unittest.main()
