"""Ядро расписания Nightfall.

Чистая логика без ввода-вывода: только даты, времена и правила сдвига.

Правила
-------
* Каждая новая ночь начинается на 30 минут раньше фактического выключения
  предыдущей ночи (фактическое = план + все переносы).
* Расписание никогда не уходит раньше целевого времени (по умолчанию 23:00):
  как только цель достигнута, время просто держится на ней.
* Если ночь была пропущена (компьютер был выключен, программа не работала),
  шаг всё равно применяется один раз — расписание не «прыгает» вперёд.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Iterable, Optional

DAY = timedelta(days=1)


# --------------------------------------------------------------------------- #
#  Сериализация примитивов
# --------------------------------------------------------------------------- #
def parse_time(value: str) -> time:
    """'23:00' -> datetime.time(23, 0)."""
    hours, _, minutes = value.strip().partition(":")
    return time(int(hours), int(minutes or 0))


def fmt_time(value: time) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


def parse_dt(value: str) -> datetime:
    """ISO без секунд: '2026-11-11T00:30'."""
    return datetime.strptime(value.strip(), "%Y-%m-%dT%H:%M")


def fmt_dt(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M")


def fmt_hm(value: datetime) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


def human_delta(delta: timedelta) -> str:
    """timedelta -> '1 ч 05 мин' / '12 мин'."""
    total = int(round(delta.total_seconds() // 60))
    sign = "-" if total < 0 else ""
    total = abs(total)
    hours, minutes = divmod(total, 60)
    if hours and minutes:
        return f"{sign}{hours} ч {minutes:02d} мин"
    if hours:
        return f"{sign}{hours} ч"
    return f"{sign}{minutes} мин"


# --------------------------------------------------------------------------- #
#  Настройки
# --------------------------------------------------------------------------- #
@dataclass
class Settings:
    """Пользовательские настройки (сериализуются в JSON)."""

    goal_time: time = time(23, 0)          # идеальное время засыпания
    step_minutes: int = 30                 # на сколько раньше каждый день
    initial_time: time = time(1, 0)        # с чего начинаем в самую первую ночь
    warn_lead_minutes: int = 50            # за сколько минут до выключения начинаем
    warn_interval_minutes: int = 5         # как часто напоминать
    postpone_minutes: int = 15             # перенос по умолчанию
    shutdown_command: tuple[str, ...] = ("systemctl", "poweroff")
    dry_run: bool = True                   # True = только имитация выключения
    grace_minutes: int = 90                # сколько ждём «опоздавшее» выключение
    shutdown_delay_seconds: int = 8        # пауза перед реальным выключением

    # -- сериализация ------------------------------------------------------- #
    def to_dict(self) -> dict:
        return {
            "goal_time": fmt_time(self.goal_time),
            "step_minutes": self.step_minutes,
            "initial_time": fmt_time(self.initial_time),
            "warn_lead_minutes": self.warn_lead_minutes,
            "warn_interval_minutes": self.warn_interval_minutes,
            "postpone_minutes": self.postpone_minutes,
            "shutdown_command": list(self.shutdown_command),
            "dry_run": self.dry_run,
            "grace_minutes": self.grace_minutes,
            "shutdown_delay_seconds": self.shutdown_delay_seconds,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Settings":
        base = cls()
        if not isinstance(raw, dict):
            return base
        try:
            return cls(
                goal_time=parse_time(raw.get("goal_time", fmt_time(base.goal_time))),
                step_minutes=max(1, int(raw.get("step_minutes", base.step_minutes))),
                initial_time=parse_time(raw.get("initial_time", fmt_time(base.initial_time))),
                warn_lead_minutes=max(0, int(raw.get("warn_lead_minutes", base.warn_lead_minutes))),
                warn_interval_minutes=max(
                    1, int(raw.get("warn_interval_minutes", base.warn_interval_minutes))
                ),
                postpone_minutes=max(
                    1, int(raw.get("postpone_minutes", base.postpone_minutes))
                ),
                shutdown_command=tuple(
                    raw.get("shutdown_command") or base.shutdown_command
                ),
                dry_run=bool(raw.get("dry_run", True)),
                grace_minutes=max(1, int(raw.get("grace_minutes", base.grace_minutes))),
                shutdown_delay_seconds=max(
                    0, int(raw.get("shutdown_delay_seconds", base.shutdown_delay_seconds))
                ),
            )
        except (TypeError, ValueError):
            return base

    def warning_levels(self) -> list[int]:
        """Минуты до выключения, в которые нужно напоминать: 50, 45, ..., 5."""
        lead = self.warn_lead_minutes
        step = self.warn_interval_minutes
        levels = list(range(lead, 0, -step))
        return levels or [lead]

    def clean(self) -> "Settings":
        settings = replace(self)
        settings.step_minutes = max(1, settings.step_minutes)
        settings.warn_interval_minutes = max(1, settings.warn_interval_minutes)
        settings.warn_lead_minutes = max(1, settings.warn_lead_minutes)
        settings.postpone_minutes = max(1, settings.postpone_minutes)
        settings.grace_minutes = max(1, settings.grace_minutes)
        settings.shutdown_delay_seconds = max(0, settings.shutdown_delay_seconds)
        command = [part for part in settings.shutdown_command if part.strip()]
        settings.shutdown_command = tuple(command) or ("systemctl", "poweroff")
        return settings


# --------------------------------------------------------------------------- #
#  Ночь
# --------------------------------------------------------------------------- #
@dataclass
class Night:
    """Одна ночь: план на вечер, переносы и фактическое выключение."""

    evening: date                              # дата вечера, к которому относится ночь
    target: datetime                           # плановое выключение (без переносов)
    postponements: list[dict] = field(default_factory=list)
    actual: Optional[datetime] = None          # фактическое выключение
    skipped: bool = False                      # ночь прошла без нас
    fired: list[int] = field(default_factory=list)  # уже отданные напоминания

    # -- вычисляемые свойства ----------------------------------------------- #
    @property
    def postponed_minutes(self) -> int:
        return sum(int(item.get("minutes", 0)) for item in self.postponements)

    @property
    def shutdown_at(self) -> datetime:
        """Когда выключение произойдёт с учётом переносов."""
        return self.target + timedelta(minutes=self.postponed_minutes)

    @property
    def settled(self) -> bool:
        return self.actual is not None or self.skipped

    @property
    def outcome(self) -> datetime:
        """Время, от которого считается следующая ночь."""
        return self.actual or self.shutdown_at

    # -- сериализация ------------------------------------------------------- #
    def to_dict(self) -> dict:
        return {
            "evening": self.evening.isoformat(),
            "target": fmt_dt(self.target),
            "postponements": [dict(item) for item in self.postponements],
            "actual": fmt_dt(self.actual) if self.actual else None,
            "skipped": self.skipped,
            "fired": list(self.fired),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Night":
        return cls(
            evening=date.fromisoformat(str(raw["evening"])),
            target=parse_dt(str(raw["target"])),
            postponements=[
                {"at": str(item.get("at", "")), "minutes": int(item.get("minutes", 0))}
                for item in raw.get("postponements", [])
            ],
            actual=parse_dt(raw["actual"]) if raw.get("actual") else None,
            skipped=bool(raw.get("skipped", False)),
            fired=[int(value) for value in raw.get("fired", [])],
        )


# --------------------------------------------------------------------------- #
#  Планирование
# --------------------------------------------------------------------------- #
def goal_anchor(evening: date, settings: Settings) -> datetime:
    """Целевая отметка (например 23:00) для конкретного вечера."""
    return datetime.combine(evening, settings.goal_time)


def evening_of(moment: datetime, settings: Settings) -> date:
    """К какому вечеру относится момент.

    00:30 12 ноября при цели 23:00 -> вечер 11 ноября.
    """
    if moment.time() >= settings.goal_time:
        return moment.date()
    return moment.date() - DAY


def is_after_midnight(value: time, goal: time) -> bool:
    """Время «уже после полуночи» (позже цели), а не «раньше цели».

    01:00 при цели 23:00 — это +2 часа, значит ночь следующего дня.
    21:30 при цели 23:00 — это −1,5 часа, то есть раньше цели.
    """
    goal_minutes = goal.hour * 60 + goal.minute
    value_minutes = value.hour * 60 + value.minute
    return (goal_minutes - value_minutes) % 1440 > 720


def _normalise(evening: date, target: datetime, settings: Settings) -> Night:
    """Не даём плану уехать раньше целевого времени."""
    floor = goal_anchor(evening, settings)
    if target < floor:
        target = floor
    return Night(evening=evening, target=target)


def first_night(now: datetime, settings: Settings) -> Night:
    """Самая первая ночь — от стартового времени из настроек."""
    candidate = datetime.combine(now.date(), settings.initial_time)
    if candidate <= now:
        candidate += DAY
    if not is_after_midnight(settings.initial_time, settings.goal_time):
        # стартовое время уже раньше цели — сразу начинаем с целевого
        candidate = datetime.combine(candidate.date(), settings.goal_time)
        return Night(evening=candidate.date(), target=candidate)
    return _normalise(evening_of(candidate, settings), candidate, settings)


def next_night(previous: Night, settings: Settings) -> Night:
    """Следующая ночь: на один шаг раньше фактического выключения."""
    evening = previous.evening + DAY
    offset = previous.outcome - goal_anchor(previous.evening, settings)
    offset -= timedelta(minutes=settings.step_minutes)
    if offset < timedelta(0):
        offset = timedelta(0)
    return Night(evening=evening, target=goal_anchor(evening, settings) + offset)


def roll_forward(night: Night, now: datetime, settings: Settings) -> Night:
    """Сдвинуть ночь на ближайший будущий вечер, сохранив время суток."""
    guard = 0
    while night.shutdown_at <= now - timedelta(minutes=settings.grace_minutes):
        guard += 1
        if guard > 400:  # защита от бесконечного цикла
            break
        offset = night.target - goal_anchor(night.evening, settings)
        evening = night.evening + DAY
        night = Night(evening=evening, target=goal_anchor(evening, settings) + offset)
    return night


def current_night(nights: list[Night], settings: Settings, now: datetime) -> Night:
    """Актуальная ночь для момента ``now``.

    Возвращает существующую запись (она может быть изменена: помечена как
    пропущенная) либо создаёт новую — её нужно будет добавить в список.
    """
    if not nights:
        return first_night(now, settings)

    last = nights[-1]
    if not last.settled:
        # Ещё «наша» ночь? Даём запас времени на опоздавшее выключение.
        if now < last.shutdown_at + timedelta(minutes=settings.grace_minutes):
            return last
        last.skipped = True  # нас не было — ночь прошла мимо

    candidate = next_night(last, settings)
    return roll_forward(candidate, now, settings)


# --------------------------------------------------------------------------- #
#  Переносы
# --------------------------------------------------------------------------- #
def postpone(night: Night, minutes: int, now: datetime) -> Night:
    """Отложить выключение и заново «взвести» напоминания."""
    minutes = int(minutes)
    if minutes <= 0:
        return night
    night.postponements.append({"at": fmt_dt(now), "minutes": minutes})
    night.fired = []
    return night


def undo_last_postpone(night: Night) -> Night:
    if night.postponements:
        night.postponements.pop()
        night.fired = []
    return night


# --------------------------------------------------------------------------- #
#  Напоминания и выключение
# --------------------------------------------------------------------------- #
@dataclass
class Reminder:
    level: int              # сколько минут оставалось по расписанию напоминаний
    remaining: timedelta    # реально осталось
    shutdown_at: datetime

    @property
    def remaining_minutes(self) -> int:
        seconds = max(0.0, self.remaining.total_seconds())
        return int(seconds // 60) + (1 if seconds % 60 else 0)


def pending_reminder(night: Night, settings: Settings, now: datetime) -> Optional[Reminder]:
    """Ближайшее неотданное напоминание, если его время пришло.

    Если программа «спала» и накопилось несколько напоминаний, отдаётся только
    самое свежее, а устаревшие тихо помечаются как отданные — без спама.
    """
    if night.settled:
        return None
    due = [
        level
        for level in settings.warning_levels()
        if level not in night.fired
        and now >= night.shutdown_at - timedelta(minutes=level)
    ]
    if not due:
        return None
    level = min(due)  # самое «горячее» из просроченных
    for stale in due:
        if stale not in night.fired:
            night.fired.append(stale)
    return Reminder(
        level=level,
        remaining=night.shutdown_at - now,
        shutdown_at=night.shutdown_at,
    )


def should_shutdown(night: Night, now: datetime) -> bool:
    return not night.settled and now >= night.shutdown_at


def mark_shutdown(night: Night, now: datetime) -> Night:
    """Зафиксировать фактическое время выключения (с точностью до минуты)."""
    night.actual = now.replace(second=0, microsecond=0)
    night.skipped = False
    return night


def preview_tomorrow(night: Night, settings: Settings) -> datetime:
    """Каким было бы выключение следующей ночью с учётом переносов сегодня."""
    return next_night(night, settings).shutdown_at


def history_offsets(nights: Iterable[Night], settings: Settings) -> list[tuple[Night, int]]:
    """[(ночь, смещение в минутах относительно целевого времени)]."""
    rows = []
    for night in nights:
        offset = int((night.outcome - goal_anchor(night.evening, settings)).total_seconds() // 60)
        rows.append((night, offset))
    return rows
