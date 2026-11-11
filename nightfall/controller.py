"""Контроллер: связывает расписание, хранилище и уведомления.

Никакого Tk — этим модулем пользуются и графический интерфейс, и headless-режим.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Callable, Deque, Optional

from . import notify, schedule
from .schedule import Night, Settings, fmt_hm
from .storage import State, Store


@dataclass
class Event:
    at: datetime
    kind: str      # reminder | shutdown | postpone | info | warning
    text: str


@dataclass
class View:
    """Всё, что нужно интерфейсу, одним снимком."""

    now: datetime
    night: Night
    settings: Settings
    tomorrow: datetime
    remaining: timedelta
    next_reminder_at: Optional[datetime]
    at_goal: bool
    history: list[Night] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    state_path: str = ""
    poweroff_scheduled: bool = False

    @property
    def postponed(self) -> int:
        return self.night.postponed_minutes

    @property
    def phase(self) -> str:
        """idle | evening | urgent | done."""
        if self.night.settled:
            return "done"
        seconds = self.remaining.total_seconds()
        if seconds <= 0:
            return "done"
        if seconds <= 10 * 60:
            return "urgent"
        if seconds <= self.settings.warn_lead_minutes * 60:
            return "evening"
        return "idle"


def view_to_dict(view: View) -> dict:
    """Снимок состояния в JSON-совместимый вид (для демона и его клиентов)."""
    night = view.night
    return {
        "now": view.now.isoformat(timespec="seconds"),
        "phase": view.phase,
        "evening": night.evening.isoformat(),
        "target": night.target.isoformat(timespec="minutes"),
        "shutdown_at": night.shutdown_at.isoformat(timespec="minutes"),
        "actual": night.actual.isoformat(timespec="minutes") if night.actual else None,
        "skipped": night.skipped,
        "settled": night.settled,
        "postponed": view.postponed,
        "remaining_seconds": int(view.remaining.total_seconds()),
        "next_reminder_at": (
            view.next_reminder_at.isoformat(timespec="minutes") if view.next_reminder_at else None
        ),
        "at_goal": view.at_goal,
        "tomorrow": view.tomorrow.isoformat(timespec="minutes"),
        "settings": view.settings.to_dict(),
        "state_path": view.state_path,
        "history": [
            {
                "evening": item.evening.isoformat(),
                "target": item.target.isoformat(timespec="minutes"),
                "outcome": item.outcome.isoformat(timespec="minutes"),
                "actual": item.actual.isoformat(timespec="minutes") if item.actual else None,
                "skipped": item.skipped,
            }
            for item in view.history
        ],
        "events": [
            {"at": event.at.isoformat(timespec="seconds"), "kind": event.kind, "text": event.text}
            for event in view.events
        ],
    }


class Controller:
    def __init__(
        self,
        store: Store | None = None,
        *,
        clock: Callable[[], datetime] = datetime.now,
        on_reminder: Optional[Callable[[Night, schedule.Reminder], None]] = None,
        on_shutdown: Optional[Callable[[Night, bool, str], None]] = None,
        on_new_night: Optional[Callable[[Night], None]] = None,
        desktop_notifications: bool = True,
    ) -> None:
        self.store = store or Store()
        self.clock = clock
        self.on_reminder = on_reminder
        self.on_shutdown = on_shutdown
        self.on_new_night = on_new_night
        self.desktop_notifications = desktop_notifications

        self.state: State = self.store.load()
        self.events: Deque[Event] = deque(maxlen=60)
        self.poweroff_scheduled = False
        self._poweroff_thread: Optional[threading.Thread] = None
        # Первый такт без уведомлений: окно ещё не успело подписаться на события,
        # иначе стартовое напоминание потерялось бы.
        self.tick(emit=False)

    # ------------------------------------------------------------------ #
    #  Служебное
    # ------------------------------------------------------------------ #
    def save(self) -> None:
        try:
            self.store.save(self.state)
        except OSError as error:
            self.add_event("warning", f"не удалось сохранить состояние: {error}")

    def add_event(self, kind: str, text: str, at: datetime | None = None) -> None:
        self.events.append(Event(at=at or self.clock(), kind=kind, text=text))

    def _notify(self, title: str, body: str, urgency: str = "normal") -> None:
        if self.desktop_notifications:
            notify.desktop_notify(title, body, urgency=urgency)

    # ------------------------------------------------------------------ #
    #  Основной такт
    # ------------------------------------------------------------------ #
    def tick(self, now: datetime | None = None, *, emit: bool = True) -> View:
        now = (now or self.clock()).replace(microsecond=0)
        state = self.state
        settings = state.settings

        previous = state.nights[-1] if state.nights else None
        night = schedule.current_night(state.nights, settings, now)
        if night is not previous:
            state.nights.append(night)
            self.poweroff_scheduled = False  # новая ночь — новый цикл выключения
            self.add_event(
                "info",
                f"ночь {night.evening:%d.%m}: план {fmt_hm(night.target)}"
                + ("" if not night.postponed_minutes else f" (перенос +{night.postponed_minutes} мин)"),
                at=now,
            )
            self.save()
            if self.on_new_night:
                self.on_new_night(night)
        elif previous is not None and previous.skipped and not night.settled:
            self.add_event(
                "warning",
                f"ночь {night.evening:%d.%m} прошла без выключения — шаг сохранён",
                at=now,
            )
            self.save()

        # --- напоминания ------------------------------------------------ #
        reminder = schedule.pending_reminder(night, settings, now) if emit else None
        if reminder is not None:
            minutes = reminder.remaining_minutes
            time_text = fmt_hm(reminder.shutdown_at)
            self.add_event("reminder", f"напоминание: осталось {minutes} мин (в {time_text})", at=now)
            self.save()
            self._notify(
                f"Осталось {minutes} мин до сна",
                f"Выключение в {time_text}. Не успеваешь? Отложи на {settings.postpone_minutes} мин.",
                urgency="normal" if minutes > 10 else "critical",
            )
            if self.on_reminder:
                self.on_reminder(night, reminder)

        # --- выключение -------------------------------------------------- #
        if emit and schedule.should_shutdown(night, now) and not self.poweroff_scheduled:
            self.fire_shutdown(night, now, trigger="расписание")

        return self.view(now)

    # ------------------------------------------------------------------ #
    #  Действия пользователя
    # ------------------------------------------------------------------ #
    def postpone(self, minutes: int | None = None, now: datetime | None = None) -> None:
        now = (now or self.clock()).replace(microsecond=0)
        settings = self.state.settings
        minutes = settings.postpone_minutes if minutes is None else int(minutes)
        night = self.state.nights[-1] if self.state.nights else schedule.current_night(
            self.state.nights, settings, now
        )
        if night.settled:
            self.add_event("warning", "эта ночь уже закрыта — перенос недоступен", at=now)
            return
        self.poweroff_scheduled = False
        schedule.postpone(night, minutes, now)
        self.add_event(
            "postpone",
            f"перенос +{minutes} мин → {fmt_hm(night.shutdown_at)}",
            at=now,
        )
        self.save()

    def undo_postpone(self, now: datetime | None = None) -> None:
        now = (now or self.clock()).replace(microsecond=0)
        night = self.state.nights[-1] if self.state.nights else None
        if not night:
            return
        minutes = night.postponed_minutes
        schedule.undo_last_postpone(night)
        self.poweroff_scheduled = False
        self.add_event("postpone", f"последний перенос отменён (−{minutes} мин до {fmt_hm(night.shutdown_at)})", at=now)
        self.save()

    def shutdown_now(self, now: datetime | None = None) -> None:
        now = (now or self.clock()).replace(microsecond=0)
        if not self.state.nights:
            return
        night = self.state.nights[-1]
        if night.settled:
            return
        self.fire_shutdown(night, now, trigger="вручную")

    def fire_shutdown(self, night: Night, now: datetime, *, trigger: str = "расписание") -> None:
        if self.poweroff_scheduled or night.settled:
            return
        settings = self.state.settings
        schedule.mark_shutdown(night, now)
        self.poweroff_scheduled = True
        self.save()  # фиксируем факт ДО выключения

        target_text = fmt_hm(night.actual or now)
        extra = f" (+{night.postponed_minutes} мин переноса)" if night.postponed_minutes else ""
        self.add_event(
            "shutdown",
            f"выключение {trigger} в {target_text}{extra}",
            at=now,
        )
        self._notify(
            "Пора спать",
            f"Выключаем компьютер ({target_text}){extra}. Спокойной ночи!",
            urgency="critical",
        )
        if settings.dry_run:
            self.save()  # без таймера: просто сохраняем факт
            message = "имитация: компьютер остался включён"
            self.add_event("info", message, at=now)
            if self.on_shutdown:
                self.on_shutdown(night, True, message)
            return

        self._poweroff_thread = threading.Thread(
            target=self._delayed_poweroff, args=(settings.shutdown_command,), daemon=True
        )
        self._poweroff_thread.start()
        if self.on_shutdown:
            self.on_shutdown(night, True, "выключение запланировано")

    def _delayed_poweroff(self, command: tuple[str, ...]) -> None:
        delay = max(0, int(self.state.settings.shutdown_delay_seconds))
        if delay:
            threading.Event().wait(delay)
        ok, message = notify.run_command(list(command), dry_run=False)
        self.add_event("shutdown" if ok else "warning", f"poweroff: {message}")
        if self.on_shutdown:
            try:
                self.on_shutdown(self.state.nights[-1], ok, message)
            except Exception:  # pragma: no cover - UI уже мог закрыться
                pass

    # ------------------------------------------------------------------ #
    #  Настройки
    # ------------------------------------------------------------------ #
    def apply_settings(self, settings: Settings, now: datetime | None = None) -> None:
        now = (now or self.clock()).replace(microsecond=0)
        settings = settings.clean()
        self.state.settings = settings

        # Незакрытую ночь пересобираем по новым правилам, но переносы сохраняем:
        # история фактических выключений остаётся нетронутой.
        settled = [night for night in self.state.nights if night.settled]
        tail = [night for night in self.state.nights if not night.settled]
        self.state.nights = settled
        if tail:
            fresh = (
                schedule.next_night(settled[-1], settings)
                if settled
                else schedule.first_night(now, settings)
            )
            fresh.postponements = [dict(item) for item in tail[-1].postponements]
            self.state.nights.append(schedule.roll_forward(fresh, now, settings))

        self.poweroff_scheduled = False
        self.add_event("info", "настройки обновлены", at=now)
        self.save()
        self.tick(now)

    def reset_progress(self, now: datetime | None = None) -> None:
        nights = self.state.nights
        self.store.backup(self.state)
        self.state.nights = []
        self.poweroff_scheduled = False
        self.add_event("warning", "прогресс сброшен, начинаем со стартового времени", at=now)
        self.save()
        self.tick(now)

    # ------------------------------------------------------------------ #
    #  Снимок для интерфейса
    # ------------------------------------------------------------------ #
    def view(self, now: datetime | None = None) -> View:
        now = (now or self.clock()).replace(microsecond=0)
        settings = self.state.settings
        if not self.state.nights:
            self.state.nights.append(schedule.current_night([], settings, now))
        night = self.state.nights[-1]
        remaining = night.shutdown_at - now

        next_at: Optional[datetime] = None
        for level in sorted(settings.warning_levels(), reverse=True):
            if level in night.fired:
                continue
            moment = night.shutdown_at - timedelta(minutes=level)
            if moment > now:
                next_at = moment
                break

        floor = schedule.goal_anchor(night.evening, settings)
        at_goal = night.target <= floor
        tomorrow = schedule.preview_tomorrow(night, settings)

        return View(
            now=now,
            night=night,
            settings=settings,
            tomorrow=tomorrow,
            remaining=remaining,
            next_reminder_at=next_at,
            at_goal=at_goal,
            history=self.state.nights[-12:],
            events=list(self.events)[-6:],
            state_path=str(self.store.path),
            poweroff_scheduled=self.poweroff_scheduled,
        )
