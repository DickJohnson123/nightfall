"""Графический интерфейс Nightfall (tkinter, только стандартная библиотека).

Вся отрисовка идёт на Canvas: скруглённые карточки, кольцо-таймер, график
сдвига и собственные кнопки — поэтому интерфейс выглядит одинаково вне
зависимости от системной темы.
"""

from __future__ import annotations

import math
import shlex
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime, timedelta
from tkinter import font as tkfont, messagebox
from typing import Callable, Optional

from . import ipc, schedule
from .client import DaemonClient, RemoteView
from .schedule import Settings, fmt_hm, human_delta

# --------------------------------------------------------------------------- #
#  Палитра
# --------------------------------------------------------------------------- #
C = {
    "bg0": "#06080F",
    "bg1": "#0C1428",
    "bg2": "#0A1020",
    "card": "#111A30",
    "card2": "#172140",
    "card3": "#1D294B",
    "edge": "#222F52",
    "edge2": "#33456F",
    "text": "#EDF1FB",
    "muted": "#8D99BA",
    "faint": "#5A6787",
    "violet": "#8B5CF6",
    "violet2": "#A78BFA",
    "violet3": "#C4B5FD",
    "cyan": "#22D3EE",
    "amber": "#FBBF24",
    "red": "#FB7185",
    "green": "#34D399",
}

DESIGN_W, DESIGN_H = 1020, 768


# --------------------------------------------------------------------------- #
#  Цвета
# --------------------------------------------------------------------------- #
def _rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def mix(color_a: str, color_b: str, t: float) -> str:
    """Смешать цвета: t=0 -> a, t=1 -> b."""
    t = max(0.0, min(1.0, t))
    a, b = _rgb(color_a), _rgb(color_b)
    return "#%02x%02x%02x" % tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


# --------------------------------------------------------------------------- #
#  Отрисовщик (координаты — в «дизайнерских» единицах)
# --------------------------------------------------------------------------- #
class Painter:
    def __init__(self, canvas: tk.Canvas, scale: float = 1.0) -> None:
        self.c = canvas
        self.k = scale

    def u(self, value: float) -> float:
        return value * self.k

    def rrect(self, x1, y1, x2, y2, r=16, fill="", outline="", width=1) -> int:
        x1, y1, x2, y2, r = (self.u(v) for v in (x1, y1, x2, y2, r))
        r = max(0.0, min(r, abs(x2 - x1) / 2, abs(y2 - y1) / 2))
        points = [
            x1 + r, y1, x2 - r, y1, x2, y1,
            x2, y1 + r, x2, y2 - r, x2, y2,
            x2 - r, y2, x1 + r, y2, x1, y2,
            x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        return self.c.create_polygon(
            points, smooth=True, splinesteps=20,
            fill=fill or "", outline=outline or "",
            width=self.u(width) if outline else 0,
        )

    def rect(self, x1, y1, x2, y2, fill="", outline="", width=1) -> int:
        return self.c.create_rectangle(
            self.u(x1), self.u(y1), self.u(x2), self.u(y2),
            fill=fill or "", outline=outline or "", width=self.u(width) if outline else 0,
        )

    def oval(self, x1, y1, x2, y2, fill="", outline="", width=1) -> int:
        return self.c.create_oval(
            self.u(x1), self.u(y1), self.u(x2), self.u(y2),
            fill=fill or "", outline=outline or "", width=self.u(width) if outline else 0,
        )

    def line(self, x1, y1, x2, y2, fill=C["edge"], width=1, dash=None) -> int:
        return self.c.create_line(
            self.u(x1), self.u(y1), self.u(x2), self.u(y2),
            fill=fill, width=self.u(width), capstyle="round", dash=dash,
        )

    def arc(self, x1, y1, x2, y2, start, extent, fill, width=10) -> int:
        return self.c.create_arc(
            self.u(x1), self.u(y1), self.u(x2), self.u(y2),
            start=start, extent=extent, style="arc", outline=fill, width=self.u(width),
        )

    def text(self, x, y, value, font, fill=C["text"], anchor="nw", **kw) -> int:
        return self.c.create_text(
            self.u(x), self.u(y), text=value, font=font, fill=fill, anchor=anchor, **kw
        )


class Fonts:
    SANS = ["Inter", "SF Pro Display", "Ubuntu", "Cantarell", "Segoe UI", "Noto Sans", "DejaVu Sans"]
    MONO = ["JetBrains Mono", "SF Mono", "Ubuntu Mono", "Noto Sans Mono", "DejaVu Sans Mono", "Liberation Mono"]

    def __init__(self, root: tk.Misc, scale: float = 1.0) -> None:
        available = set(tkfont.families(root))
        self.sans = self._pick(available, self.SANS)
        self.mono = self._pick(available, self.MONO)
        k = scale

        def f(size: int, weight: str = "normal") -> tkfont.Font:
            return tkfont.Font(root=root, family=self.sans, size=-int(size * k), weight=weight)

        def m(size: int, weight: str = "normal") -> tkfont.Font:
            return tkfont.Font(root=root, family=self.mono, size=-int(size * k), weight=weight)

        self.title = f(22, "bold")
        self.subtitle = f(11)
        self.h2 = f(16, "bold")
        self.h3 = f(13, "bold")
        self.label = f(12)
        self.label_sm = f(11)
        self.small = f(10)
        self.tiny = f(9)
        self.chip = f(10, "bold")
        self.btn = f(12, "bold")
        self.btn_sm = f(11, "bold")
        self.value = m(14, "bold")
        self.value_sm = m(11)
        self.mono_sm = m(10)
        self.mono_xs = m(9)
        self.big = m(58, "bold")
        self.clock = m(13)

    @staticmethod
    def _pick(available: set[str], candidates: list[str]) -> str:
        for name in candidates:
            if name in available:
                return name
        return "DejaVu Sans"


# --------------------------------------------------------------------------- #
#  Поверхность с собственными кнопками
# --------------------------------------------------------------------------- #
@dataclass
class HitButton:
    x1: float
    y1: float
    x2: float
    y2: float
    command: Optional[Callable[[], None]] = None
    enabled: bool = True

    def contains(self, x: float, y: float) -> bool:
        return self.x1 <= x <= self.x2 and self.y1 <= y <= self.y2


class CanvasSurface(tk.Frame):
    def __init__(self, master, width: int, height: int, scale: float = 1.0, bg: str = C["bg0"]):
        super().__init__(master, bg=bg, highlightthickness=0, bd=0)
        self.scale = scale
        self.canvas = tk.Canvas(
            self, width=int(width * scale), height=int(height * scale),
            bg=bg, highlightthickness=0, bd=0,
        )
        self.canvas.pack(fill="both", expand=True)
        self.p = Painter(self.canvas, scale)
        self.buttons: list[HitButton] = []
        self.pointer: Optional[tuple[float, float]] = None
        self._hover_key: Optional[tuple] = None
        self._static_ids: frozenset[int] = frozenset()
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)

    # -- события --------------------------------------------------------- #
    def _hit(self, x: float, y: float) -> Optional[HitButton]:
        for button in reversed(self.buttons):
            if button.enabled and button.contains(x, y):
                return button
        return None

    def _on_click(self, event) -> None:
        button = self._hit(event.x / self.scale, event.y / self.scale)
        if button and button.command:
            button.command()

    def _on_motion(self, event) -> None:
        self.pointer = (event.x / self.scale, event.y / self.scale)
        button = self._hit(*self.pointer)
        self.canvas.configure(cursor="hand2" if button else "")
        # Перерисовываем только когда поменялась кнопка под курсором:
        # движение мыши по пустому месту не должно стоить кадра.
        key = (button.x1, button.y1, button.x2, button.y2) if button else None
        if key != self._hover_key:
            self._hover_key = key
            self.redraw()

    def _on_leave(self, _event) -> None:
        self.pointer = None
        self.canvas.configure(cursor="")
        if self._hover_key is not None:
            self._hover_key = None
            self.redraw()

    def redraw(self) -> None:
        pass

    # -- рисование -------------------------------------------------------- #
    def begin(self, keep_static: bool = False) -> None:
        if keep_static and self._static_ids:
            for item in self.canvas.find_all():
                if item not in self._static_ids:
                    self.canvas.delete(item)
        else:
            self.canvas.delete("all")
            self._static_ids = frozenset()
        self.buttons.clear()

    def protect_static(self) -> None:
        """Запомнить текущие элементы как неподвижный фон (градиент, свечение)."""
        self._static_ids = frozenset(self.canvas.find_all())

    def button(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        text: str,
        font,
        command: Optional[Callable[[], None]] = None,
        *,
        kind: str = "ghost",
        radius: float = 12,
        enabled: bool = True,
    ) -> HitButton:
        palette = {
            "primary": (C["violet"], mix(C["violet"], "#FFFFFF", 0.18), C["text"], ""),
            "ghost": (C["card2"], C["card3"], C["text"], C["edge"]),
            "danger": (C["card2"], mix(C["card2"], C["red"], 0.25), C["red"], C["edge"]),
            "success": (C["card2"], mix(C["card2"], C["green"], 0.25), C["green"], C["edge"]),
            "quiet": (C["card"], C["card2"], C["muted"], ""),
        }[kind]
        base, hover_fill, fg, edge = palette
        if not enabled:
            base = hover_fill = C["bg2"]
            fg, edge = C["faint"], ""
        hovered = self.pointer is not None and x1 <= self.pointer[0] <= x2 and y1 <= self.pointer[1] <= y2
        self.p.rrect(x1, y1 + 2, x2, y2 + 3, radius, fill="#05070F")
        self.p.rrect(x1, y1, x2, y2, radius, fill=hover_fill if hovered else base, outline=edge)
        self.p.text((x1 + x2) / 2, (y1 + y2) / 2, text, font, fill=fg, anchor="center")
        button = HitButton(x1, y1, x2, y2, command if enabled else None, enabled)
        self.buttons.append(button)
        return button


def font_measure(surface: CanvasSurface, text: str, font) -> float:
    return font.measure(text) / surface.scale


# --------------------------------------------------------------------------- #
#  Главное окно
# --------------------------------------------------------------------------- #
class NightfallWindow:
    """Окно — всего лишь клиент демона: закрытие окна ничего не останавливает."""

    def __init__(self, client: DaemonClient) -> None:
        self.ctrl = client
        self.root = tk.Tk()
        self.root.title("Nightfall — плавное засыпание")
        self.root.configure(bg=C["bg0"])
        self.scale = self._detect_scale()
        self.fonts = Fonts(self.root, self.scale)
        self.surface = CanvasSurface(self.root, DESIGN_W, DESIGN_H, self.scale, C["bg0"])
        self.surface.redraw = self.render
        # центр вместо растягивания: тайловые WM могут развернуть окно на весь экран
        self.surface.place(relx=0.5, rely=0.5, anchor="center")
        self.root.resizable(False, False)
        self._center_window()

        self.online = client.is_running()
        self.view: Optional[RemoteView] = client.last_view
        if self.view is None:
            try:
                self.view = client.view()
            except ipc.IpcError:
                self.view = None
        self.settings_dialog: Optional["SettingsDialog"] = None

        self.root.bind("<Control-p>", lambda _e: self.ctrl.postpone())
        self.root.bind("<Control-P>", lambda _e: self.ctrl.postpone(30))
        self.root.bind("<Control-comma>", lambda _e: self.open_settings())
        self.root.bind("<Control-q>", lambda _e: self.close())
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        # после разворачивания окна кадр мог устареть — рисуем заново
        self.root.bind("<Map>", lambda _e: self.render())

        self.render()
        self.root.after(500, self.tick)

    # ------------------------------------------------------------------ #
    def _detect_scale(self) -> float:
        try:
            dpi = self.root.winfo_fpixels("1i")
            return max(1.0, min(1.6, round(dpi / 96.0, 2)))
        except tk.TclError:  # pragma: no cover
            return 1.0

    def _center_window(self) -> None:
        width = int(DESIGN_W * self.scale)
        height = int(DESIGN_H * self.scale)
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = max(0, (self.root.winfo_screenheight() - height) // 2 - 24)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def close(self) -> None:
        """Закрыть окно. Демон продолжает работать — расписание не останавливается."""
        self.root.destroy()

    def restart_daemon(self) -> None:
        if self.ctrl.ensure_daemon():
            self.online = True
            self.view = self.ctrl.view()
        self.render()

    def run(self) -> None:
        self.root.mainloop()

    # ------------------------------------------------------------------ #
    def tick(self) -> None:
        try:
            self.view = self.ctrl.tick()
            self.online = True
        except ipc.IpcError:
            # демон не отвечает: показываем последнее известное состояние
            if self.online or self.view is None:
                self.online = False
                self._frame_key = None
        except Exception as error:  # pragma: no cover
            print("nightfall: ошибка кадра:", error)
        try:
            if self.view is not None and self._needs_render():
                self.render()
        except Exception as error:  # pragma: no cover
            print("nightfall: ошибка отрисовки:", error)
        self.root.after(1000, self.tick)

    def _needs_render(self) -> bool:
        """Кадр нужен, только если изменилось то, что видно на экране.

        Пока до выключения больше часа, счётчик показывает часы и минуты —
        значит, достаточно перерисовывать окно раз в минуту, а не каждую секунду.
        """
        try:
            if self.root.state() == "iconic":  # свернуто — не рисуем вообще
                return False
        except tk.TclError:  # pragma: no cover
            pass
        v = self.view
        if v is None:
            return True
        seconds = max(0.0, v.remaining.total_seconds())
        quantum = 1 if seconds < 3600 else 60
        key = (
            int(seconds // quantum),
            quantum,
            v.phase,
            v.postponed,
            v.night.target,
            v.night.actual,
            v.night.skipped,
            v.now.strftime("%H:%M"),
            v.next_reminder_at,
            v.at_goal,
            len(v.events),
            len(v.history),
            v.settings,
            v.settings.dry_run,
            self.online,
            self.ctrl.pid,
        )
        if key == getattr(self, "_frame_key", None):
            return False
        self._frame_key = key
        return True

    # ------------------------------------------------------------------ #
    #  Действия
    # ------------------------------------------------------------------ #
    def postpone_menu(self) -> None:
        menu = tk.Menu(
            self.root, tearoff=0, bg=C["card"], fg=C["text"],
            activebackground=C["violet"], activeforeground=C["text"],
            bd=0, font=(self.fonts.sans, -int(11 * self.scale)),
        )
        default = self.view.settings.postpone_minutes
        for minutes in [5, 10, 15, 30, 45, 60]:
            label = f"+{minutes} мин" + ("  (по умолчанию)" if minutes == default else "")
            menu.add_command(label=label, command=lambda m=minutes: self.ctrl.postpone(m))
        menu.add_separator()
        menu.add_command(label="Отменить последний перенос", command=self.ctrl.undo_postpone)
        try:
            x = self.root.winfo_pointerx()
            y = self.root.winfo_pointery()
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def open_settings(self) -> None:
        if self.settings_dialog is not None and self.settings_dialog.alive:
            self.settings_dialog.lift()
            return
        self.settings_dialog = SettingsDialog(self)

    # ------------------------------------------------------------------ #
    #  Всплывающие окна
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    #  Отрисовка
    # ------------------------------------------------------------------ #
    def render(self) -> None:
        s = self.surface
        first_paint = not s._static_ids
        s.begin(keep_static=not first_paint)
        if first_paint:
            self._background()
            s.protect_static()  # градиент и свечение больше не перерисовываем
        if self.view is None:
            self._offline_screen()
            return
        self._header()
        self._hero()
        self._chart()
        self._footer()

    def _offline_screen(self) -> None:
        p, f = self.surface.p, self.fonts
        self._card(32, 96, 988, 478)
        p.text(510, 190, "Нет связи с фоновой службой", f.title, C["text"], anchor="center")
        p.text(
            510, 226,
            "Расписание сейчас не работает: напоминаний и выключения не будет.",
            f.label, C["muted"], anchor="center",
        )
        p.text(510, 252, "Запустите фон — он поднимется отдельным процессом и продолжит "
                         "работать даже после закрытия окна.", f.small, C["faint"], anchor="center")
        self.surface.button(390, 300, 630, 348, "Запустить фон", f.btn,
                            self.restart_daemon, kind="primary", radius=14)
        p.text(510, 380, "или из терминала:  python3 nightfall.py --service", f.small,
               C["faint"], anchor="center")

    def _glow(self, cx, cy, radius, color, strength) -> None:
        p = self.surface.p
        rings = 34
        for index in range(rings, 0, -1):
            k = index / rings
            current = radius * k
            p.oval(
                cx - current, cy - current * 0.7, cx + current, cy + current * 0.7,
                fill=mix(C["bg1"], color, strength * (1 - k ** 2) * 0.55 + 0.015),
            )

    def _background(self) -> None:
        p = self.surface.p
        bands = 80
        for index in range(bands):
            t = index / (bands - 1)
            p.rect(
                0, DESIGN_H * index / bands - 0.5,
                DESIGN_W, DESIGN_H * (index + 1) / bands + 0.5,
                fill=mix(C["bg0"], C["bg1"], t ** 0.9),
            )
        self._glow(180, 60, 360, C["violet"], 0.18)
        self._glow(900, 700, 400, C["cyan"], 0.10)
        self._glow(540, 300, 420, C["violet"], 0.07)

    def _header(self) -> None:
        p, f, v = self.surface.p, self.fonts, self.view
        p.oval(30, 26, 64, 60, fill=mix(C["bg1"], C["violet"], 0.30))
        p.oval(35, 31, 61, 57, fill=C["violet2"])
        p.oval(44, 25, 65, 46, fill=mix(C["bg0"], C["bg1"], 0.35))
        p.text(78, 42, "Nightfall", f.title, C["text"], anchor="w")
        p.text(79, 64, f"каждую ночь на {v.settings.step_minutes} минут раньше", f.subtitle, C["muted"], anchor="w")

        # режим работы
        text = "ИМИТАЦИЯ" if v.settings.dry_run else "БОЕВОЙ РЕЖИМ"
        color = C["amber"] if v.settings.dry_run else C["green"]
        width = font_measure(self.surface, text, f.chip) + 42
        x2 = DESIGN_W - 32
        x1 = x2 - width
        p.rrect(x1, 29, x2, 57, 14, fill=mix(C["card"], color, 0.15), outline=mix(C["card"], color, 0.45))
        p.oval(x1 + 14, 39, x1 + 22, 47, fill=color)
        p.text(x1 + 30, 43, text, f.chip, color, anchor="w")

        # состояние фоновой службы
        if self.online:
            daemon_text = f"ФОН · {self.ctrl.pid or '—'}"
            daemon_color = C["cyan"]
        else:
            daemon_text = "ФОН ОСТАНОВЛЕН"
            daemon_color = C["red"]
        daemon_width = font_measure(self.surface, daemon_text, f.chip) + 42
        dx2 = x1 - 10
        dx1 = dx2 - daemon_width
        p.rrect(dx1, 29, dx2, 57, 14, fill=mix(C["card"], daemon_color, 0.15),
                outline=mix(C["card"], daemon_color, 0.45))
        p.oval(dx1 + 14, 39, dx1 + 22, 47, fill=daemon_color)
        p.text(dx1 + 30, 43, daemon_text, f.chip, daemon_color, anchor="w")

        p.text(dx1 - 16, 43, v.now.strftime("%H:%M"), f.clock, C["muted"], anchor="e")

    # -- карточки --------------------------------------------------------- #
    def _card(self, x1, y1, x2, y2, fill=None) -> None:
        p = self.surface.p
        p.rrect(x1, y1 + 6, x2, y2 + 9, 24, fill="#03050B")
        p.rrect(x1, y1, x2, y2, 24, fill=fill or C["card"], outline=C["edge"])

    def _tag(self, x, y, text, color, anchor="w") -> None:
        p = self.surface.p
        width = font_measure(self.surface, text, self.fonts.chip) + 24
        x1 = x if anchor == "w" else x - width
        p.rrect(x1, y - 11, x1 + width, y + 11, 11,
                fill=mix(C["card"], color, 0.16), outline=mix(C["card"], color, 0.42))
        p.text(x1 + width / 2, y, text, self.fonts.chip, color, anchor="center")

    def _hero(self) -> None:
        self._card(32, 96, 520, 478)
        self._card(540, 96, 988, 478)
        self._hero_dial()
        self._hero_plan()

    def _hero_dial(self) -> None:
        p, f, v = self.surface.p, self.fonts, self.view
        p.text(62, 122, "СЕГОДНЯШНЯЯ НОЧЬ", f.tiny, C["faint"], anchor="w")

        phase = v.phase
        status = {
            "idle": ("по плану", C["cyan"]),
            "evening": ("финальные минуты", C["violet3"]),
            "urgent": ("пора спать", C["red"]),
            "done": ("ночь закрыта", C["green"]),
        }[phase]
        if not self.online:
            status = ("фон остановлен", C["red"])
        self._tag(490, 122, status[0], status[1], anchor="e")

        seconds = v.remaining.total_seconds()
        if seconds > 3600:
            big = f"{int(seconds // 3600)}:{int(seconds % 3600 // 60):02d}"
        elif seconds > 0:
            big = f"{int(seconds // 60):02d}:{int(seconds % 60):02d}"
        else:
            big = "00:00"

        lead_seconds = max(1, v.settings.warn_lead_minutes) * 60
        progress = max(0.0, min(1.0, seconds / lead_seconds))
        accent = C["violet"]
        if phase == "done":
            accent = C["green"]
        elif seconds <= 10 * 60:
            accent = C["red"]
        elif seconds <= 30 * 60:
            accent = C["amber"]
        elif phase == "evening":
            accent = C["violet2"]

        cx, cy, radius = 276, 268, 104
        # свечение под кольцом
        p.oval(cx - radius - 20, cy - radius - 20, cx + radius + 20, cy + radius + 20,
               fill=mix(C["card"], accent, 0.06))
        p.oval(cx - radius - 9, cy - radius - 9, cx + radius + 9, cy + radius + 9,
               fill=mix(C["card"], accent, 0.11))
        # тики
        for index in range(60):
            angle = math.radians(index * 6 - 90)
            major = index % 5 == 0
            outer = radius + 15
            inner = outer - (7 if major else 3.5)
            p.line(
                cx + math.cos(angle) * inner, cy + math.sin(angle) * inner,
                cx + math.cos(angle) * outer, cy + math.sin(angle) * outer,
                fill=C["edge2"] if major else mix(C["card"], C["edge"], 0.7),
                width=1.6 if major else 1,
            )
        box = (cx - radius, cy - radius, cx + radius, cy + radius)
        p.arc(*box, start=0, extent=359.9, fill=mix(C["card"], C["edge2"], 0.6), width=12)
        if progress > 0:
            extent = -359.9 * progress
            p.arc(*box, start=90, extent=extent, fill=accent, width=12)
            angle = math.radians(90 + extent)
            dot_x = cx + math.cos(angle) * radius
            dot_y = cy - math.sin(angle) * radius
            p.oval(dot_x - 6.5, dot_y - 6.5, dot_x + 6.5, dot_y + 6.5, fill=mix(accent, "#FFFFFF", 0.4))

        p.text(cx, cy - 24, big, f.big, C["text"], anchor="center")
        p.text(cx, cy + 20, "до выключения", f.label_sm, C["muted"], anchor="center")
        if seconds <= 0:
            hint = "время вышло"
        else:
            hint = f"{human_delta(v.remaining)} · в {fmt_hm(v.night.shutdown_at)}"
            if v.postponed:
                hint += f" (+{v.postponed})"
        p.text(cx, cy + 42, hint, f.small, mix(C["muted"], C["text"], 0.3), anchor="center")

        line = f"план {fmt_hm(v.night.target)}"
        if v.postponed:
            line += f"   ·   перенос +{v.postponed} мин  →  {fmt_hm(v.night.shutdown_at)}"
        p.text(276, 432, line, f.label_sm, C["muted"], anchor="center")
        p.text(
            276, 454,
            f"цель {fmt_hm(v.settings.goal_time)}  ·  шаг {v.settings.step_minutes} мин",
            f.small, C["faint"], anchor="center",
        )

    def _hero_plan(self) -> None:
        p, f, v = self.surface.p, self.fonts, self.view
        left, right = 568, 960
        p.text(left, 122, "ПЛАН", f.tiny, C["faint"], anchor="w")
        p.text(left, 146, "Расписание на сегодня", f.h2, C["text"], anchor="w")

        lead = v.settings.warn_lead_minutes
        interval = v.settings.warn_interval_minutes
        warn_start = v.night.shutdown_at - timedelta(minutes=lead)
        next_night = (
            f"{fmt_hm(v.tomorrow)}  ·  цель достигнута"
            if v.at_goal
            else f"{fmt_hm(v.tomorrow)}  ·  −{v.settings.step_minutes} мин"
        )
        rows = [
            ("Плановая остановка", fmt_hm(v.night.target), C["text"]),
            ("Перенесено", f"+{v.postponed} мин" if v.postponed else "нет",
             C["violet2"] if v.postponed else C["muted"]),
            ("Фактическое выключение", fmt_hm(v.night.shutdown_at), C["text"]),
            ("Напоминания", f"с {fmt_hm(warn_start)} каждые {interval} мин", C["muted"]),
            ("Следующая ночь", next_night, C["cyan"]),
        ]
        y = 188
        for label, value, color in rows:
            p.text(left, y, label, f.label_sm, C["muted"], anchor="w")
            p.text(right, y, value, f.value_sm, color, anchor="e")
            p.line(left, y + 16, right, y + 16, fill=mix(C["card"], C["edge"], 0.55), width=1)
            y += 33

        # кнопки
        y1, y2 = 372, 414
        gap = 10
        width = (right - left - gap * 2) / 3
        self.surface.button(left, y1, left + width, y2, f"+{v.settings.postpone_minutes} мин", f.btn_sm,
                            lambda: self.ctrl.postpone(), kind="primary")
        self.surface.button(left + width + gap, y1, left + width * 2 + gap, y2, "+30 мин", f.btn_sm,
                            lambda: self.ctrl.postpone(30), kind="ghost")
        self.surface.button(left + (width + gap) * 2, y1, right, y2, "Отложить…", f.btn_sm,
                            self.postpone_menu, kind="ghost")

        y1, y2 = 422, 462
        half = (right - left - gap) / 2
        if self.online:
            self.surface.button(left, y1, left + half, y2, "Выключить сейчас", f.btn_sm,
                                self.ctrl.shutdown_now, kind="danger", enabled=not v.night.settled)
        else:
            self.surface.button(left, y1, left + half, y2, "Запустить фон", f.btn_sm,
                                self.restart_daemon, kind="primary")
        self.surface.button(left + half + gap, y1, right, y2, "Настройки", f.btn_sm,
                            self.open_settings, kind="ghost")

    # -- график ----------------------------------------------------------- #
    def _chart(self) -> None:
        p, f, v = self.surface.p, self.fonts, self.view
        self._card(32, 494, 988, 718)
        p.text(62, 514, "ДИНАМИКА", f.tiny, C["faint"], anchor="w")
        p.text(62, 532, "Как сдвигается время выключения", f.h3, C["text"], anchor="w")
        p.text(960, 538, "последние ночи", f.small, C["faint"], anchor="e")

        nights = v.history[-12:]
        if not nights:
            p.text(510, 600, "данных пока нет", f.label, C["faint"], anchor="center")
            return

        plot_left, plot_right = 132, 900
        plot_top, plot_bottom = 560, 676
        goal = v.settings.goal_time

        offsets = [
            max(0, int((night.outcome - schedule.goal_anchor(night.evening, v.settings)).total_seconds() // 60))
            for night in nights
        ]
        max_offset = max(60, min(300, max(offsets) + 15))
        step_grid = 60

        # сетка
        level = 0
        while level <= max_offset:
            y = plot_bottom - (level / max_offset) * (plot_bottom - plot_top)
            color = mix(C["card"], C["edge"], 0.9) if level else mix(C["card"], C["cyan"], 0.4)
            p.line(plot_left, y, plot_right, y, fill=color, width=1, dash=None if level else (5, 4))
            hour = (goal.hour + level // 60) % 24
            p.text(plot_left - 12, y, f"{hour:02d}:{goal.minute:02d}", f.mono_xs, C["faint"], anchor="e")
            level += step_grid

        count = len(nights)
        spacing = (plot_right - plot_left) / max(1, count - 1) if count > 1 else 0
        points = []
        for index, (night, offset) in enumerate(zip(nights, offsets)):
            x = plot_left + spacing * index if count > 1 else (plot_left + plot_right) / 2
            ratio = min(1.0, offset / max_offset)
            y = plot_bottom - ratio * (plot_bottom - plot_top)
            points.append((x, y, night))

        # линия (прямые отрезки: расписание — это ступеньки, а не кривая)
        if len(points) > 1:
            flat = [coord for point in points for coord in point[:2]]
            for width, color in ((9, mix(C["card"], C["violet"], 0.35)), (2.4, C["violet2"])):
                self.surface.canvas.create_line(
                    [p.u(value) for value in flat],
                    fill=color, width=p.u(width),
                    capstyle="round", joinstyle="round",
                )

        for index, (x, y, night) in enumerate(points):
            planned = night.actual is None and not night.skipped
            color = C["cyan"] if planned else (C["faint"] if night.skipped else C["violet3"])
            if planned:
                p.oval(x - 7, y - 7, x + 7, y + 7, fill=mix(C["card"], color, 0.25), outline=color, width=2)
                p.oval(x - 2.5, y - 2.5, x + 2.5, y + 2.5, fill=color)
            else:
                p.oval(x - 6, y - 6, x + 6, y + 6, fill=C["card"], outline=color, width=2)
                p.oval(x - 3, y - 3, x + 3, y + 3, fill=color)
            p.text(x, y - 14, fmt_hm(night.outcome), f.mono_xs,
                   C["text"] if index == len(points) - 1 else C["muted"], anchor="s")
            p.text(x, plot_bottom + 10, f"{night.evening:%d.%m}", f.mono_xs, C["faint"], anchor="n")

        # подпись целевой линии
        p.text(plot_left + 4, plot_bottom - 6, "цель", f.tiny, C["cyan"], anchor="sw")

    def _footer(self) -> None:
        p, f, v = self.surface.p, self.fonts, self.view
        event = v.events[-1].text if v.events else "готово"
        if len(event) > 78:
            event = event[:75] + "…"
        p.text(32, 744, f"журнал: {event}", f.small, C["faint"], anchor="w")
        p.text(
            DESIGN_W - 32, 744,
            "Ctrl+P — +15 мин  ·  Ctrl+Shift+P — +30 мин  ·  Ctrl+, — настройки",
            f.small, C["faint"], anchor="e",
        )


# --------------------------------------------------------------------------- #
#  Всплывающее окно
# --------------------------------------------------------------------------- #
class Popup(tk.Toplevel):
    """Короткоживущее окно-напоминание. Живёт отдельным процессом."""

    W, H = 468, 208

    def __init__(self, root: tk.Misc, fonts: "Fonts", scale: float, title: str,
                 lines: list[str], buttons: list[tuple[str, str, Callable[[], None]]],
                 accent: str, autoclose: int = 30) -> None:
        super().__init__(root)
        self.fonts = fonts
        self.title_text = title
        self.lines = lines
        self.buttons = buttons
        self.accent = accent
        self.autoclose = autoclose
        self.total = autoclose
        self.scale = scale
        self.overrideredirect(True)
        try:
            self.attributes("-topmost", True)
        except tk.TclError:  # pragma: no cover
            pass
        self.configure(bg=C["bg0"])
        self.surface = CanvasSurface(self, self.W, self.H, self.scale, C["bg0"])
        self.surface.redraw = self.render
        self.surface.pack()
        self._place()
        self.render()
        self.after(1000, self._tick)

    def _place(self) -> None:
        self.update_idletasks()
        width, height = self.W * self.scale, self.H * self.scale
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        x = screen_w - width - int(34 * self.scale)
        y = screen_h - height - int(64 * self.scale)
        self.geometry(f"{int(width)}x{int(height)}+{int(x)}+{int(y)}")

    def close(self) -> None:
        try:
            self.destroy()
        except tk.TclError:
            pass

    def _tick(self) -> None:
        self.autoclose -= 1
        if self.autoclose <= 0:
            self.close()
            return
        self.render()
        self.after(1000, self._tick)

    def render(self) -> None:
        p, f = self.surface.p, self.fonts
        s = self.surface
        s.begin()
        p.rrect(0, 0, self.W, self.H, 20, fill=C["card2"], outline=mix(C["card2"], self.accent, 0.55), width=1.5)
        # акцентная полоса слева, ровно по высоте текстового блока
        p.rrect(26, 28, 32, 100, 3, fill=self.accent)

        p.text(48, 32, self.title_text, f.h2, C["text"], anchor="w")
        p.text(48, 62, self.lines[0] if self.lines else "", f.label_sm, C["muted"], anchor="w")
        if len(self.lines) > 1:
            p.text(48, 84, self.lines[1], f.small, C["faint"], anchor="w")

        # мини-циферблат справа
        gx, gy, gr = self.W - 62, 62, 26
        p.oval(gx - gr, gy - gr, gx + gr, gy + gr, fill=mix(C["card2"], self.accent, 0.12))
        p.oval(gx - gr + 3, gy - gr + 3, gx + gr - 3, gy + gr - 3, fill="", outline=mix(self.accent, C["text"], 0.15), width=2)
        p.line(gx, gy, gx, gy - gr + 9, fill=self.accent, width=2.5)
        p.line(gx, gy, gx + gr - 11, gy + 4, fill=self.accent, width=2.5)
        p.oval(gx - 3, gy - 3, gx + 3, gy + 3, fill=self.accent)

        y = self.H - 58
        x = 48
        widths = [112, 96]
        for index, (label, kind, command) in enumerate(self.buttons):
            width = widths[min(index, len(widths) - 1)]
            s.button(x, y, x + width, y + 38, label, f.btn_sm, command, kind=kind, radius=11)
            x += width + 10

        if self.autoclose is not None:
            ratio = max(0.0, self.autoclose / max(1, self.total))
            p.line(22, self.H - 12, 22 + (self.W - 44) * ratio, self.H - 12,
                   fill=mix(C["card2"], self.accent, 0.7), width=2.5)


# --------------------------------------------------------------------------- #
#  Настройки
# --------------------------------------------------------------------------- #
class SettingsDialog(tk.Toplevel):
    def __init__(self, app: NightfallWindow) -> None:
        super().__init__(app.root)
        self.app = app
        self.controller = app.ctrl
        self.alive = True
        scale = app.scale
        self.scale = scale
        self.bg = mix(C["bg0"], C["bg1"], 0.75)
        self.configure(bg=self.bg)
        self.title("Настройки Nightfall")
        self.resizable(False, False)
        self.transient(app.root)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.font = (app.fonts.sans, -int(12 * scale))
        self.font_small = (app.fonts.sans, -int(10.5 * scale))
        self.font_label = (app.fonts.sans, -int(10.5 * scale))
        self.font_mono = (app.fonts.mono, -int(12 * scale))

        settings = app.view.settings
        pad = int(24 * scale)
        outer = tk.Frame(self, bg=self.bg, padx=pad, pady=pad)
        outer.pack(fill="both", expand=True)

        tk.Label(outer, text="Настройки", bg=self.bg, fg=C["text"],
                 font=(app.fonts.sans, -int(17 * scale), "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w")
        tk.Label(outer, text="Изменения влияют на текущую и будущие ночи — история фактических "
                             "выключений сохраняется.",
                 bg=self.bg, fg=C["faint"], font=self.font_small, justify="left",
                 wraplength=int(430 * scale)).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 16))

        self.entries: dict[str, tk.Entry] = {}
        self.vars: dict[str, tk.BooleanVar] = {}

        row = 2
        fields = [
            ("goal", "Целевое время (идеал)", schedule.fmt_time(settings.goal_time), "например 23:00"),
            ("initial", "Стартовое время (1-я ночь)", schedule.fmt_time(settings.initial_time), "например 01:00"),
            ("step", "Шаг сдвига, мин", str(settings.step_minutes), "30"),
            ("lead", "Напоминать за, мин", str(settings.warn_lead_minutes), "50"),
            ("interval", "Интервал напоминаний, мин", str(settings.warn_interval_minutes), "5"),
            ("postpone", "Перенос по умолчанию, мин", str(settings.postpone_minutes), "15"),
            ("delay", "Пауза перед выключением, с", str(settings.shutdown_delay_seconds), "8"),
            ("command", "Команда выключения", " ".join(settings.shutdown_command), "systemctl poweroff"),
        ]
        for key, label, value, hint in fields:
            tk.Label(outer, text=label, bg=self.bg, fg=C["muted"], font=self.font_label).grid(
                row=row, column=0, sticky="w", pady=6)
            entry = tk.Entry(
                outer, width=22, bg=C["card"], fg=C["text"], insertbackground=C["text"],
                relief="flat", highlightthickness=1, highlightbackground=C["edge"],
                highlightcolor=C["violet"], font=self.font_mono,
            )
            entry.insert(0, value)
            entry.grid(row=row, column=1, sticky="e", ipady=int(4 * scale), pady=6)
            self.entries[key] = entry
            row += 1

        check_row = row
        tk.Label(outer, text="Только имитация (не выключать компьютер)", bg=self.bg,
                 fg=C["muted"], font=self.font_label).grid(row=check_row, column=0, sticky="w", pady=(14, 4))
        var = tk.BooleanVar(value=settings.dry_run)
        self.vars["dry_run"] = var
        check = tk.Checkbutton(
            outer, variable=var, bg=self.bg, activebackground=C["bg0"],
            selectcolor=C["card"], fg=C["text"], bd=0, highlightthickness=0,
        )
        check.grid(row=check_row, column=1, sticky="e", pady=(14, 4))
        row += 1

        tk.Label(outer, text=f"Файл состояния: {app.view.state_path}", bg=self.bg, fg=C["faint"],
                 font=(app.fonts.mono, -int(9 * scale)), wraplength=int(430 * scale),
                 justify="left").grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 14))
        row += 1

        actions = tk.Frame(outer, bg=self.bg)
        actions.grid(row=row, column=0, columnspan=2, sticky="ew")
        self._button(actions, "Сохранить", self.save, "primary").pack(side="right")
        self._button(actions, "Отмена", self.close, "ghost").pack(side="right", padx=(0, 8))
        self._button(actions, "Сбросить прогресс", self.reset, "danger").pack(side="left")
        self._button(actions, "Журнал в буфер", self.copy_log, "ghost").pack(side="left", padx=(8, 0))
        self._button(actions, "Остановить фон", self.stop_daemon, "ghost").pack(side="left", padx=(8, 0))

        self.update_idletasks()
        width, height = self.winfo_width(), self.winfo_height()
        x = app.root.winfo_x() + (app.root.winfo_width() - width) // 2
        y = app.root.winfo_y() + 80
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.grab_set()
        self.focus_set()

    # ------------------------------------------------------------------ #
    def _button(self, parent, text, command, kind) -> tk.Button:
        palette = {
            "primary": (C["violet"], C["text"]),
            "ghost": (C["card2"], C["text"]),
            "danger": (C["card2"], C["red"]),
        }[kind]
        return tk.Button(
            parent, text=text, command=command, bg=palette[0], fg=palette[1],
            activebackground=mix(palette[0], "#FFFFFF", 0.1), activeforeground=palette[1],
            relief="flat", bd=0, font=self.app.fonts.btn_sm, padx=14, pady=6, cursor="hand2",
        )

    # ------------------------------------------------------------------ #
    def save(self) -> None:
        try:
            settings = Settings(
                goal_time=schedule.parse_time(self.entries["goal"].get()),
                initial_time=schedule.parse_time(self.entries["initial"].get()),
                step_minutes=int(self.entries["step"].get()),
                warn_lead_minutes=int(self.entries["lead"].get()),
                warn_interval_minutes=int(self.entries["interval"].get()),
                postpone_minutes=int(self.entries["postpone"].get()),
                shutdown_delay_seconds=int(self.entries["delay"].get()),
                shutdown_command=tuple(shlex.split(self.entries["command"].get() or "")),
                dry_run=self.vars["dry_run"].get(),
            )
        except ValueError as error:
            messagebox.showerror("Nightfall", f"Проверь значения: {error}", parent=self)
            return
        if settings.step_minutes <= 0:
            messagebox.showerror("Nightfall", "Шаг должен быть больше нуля.", parent=self)
            return
        self.controller.apply_settings(settings)
        self.close()

    def reset(self) -> None:
        if messagebox.askyesno(
            "Nightfall",
            "Сбросить прогресс?\n\nВся история ночей обнулится, и расписание снова "
            "начнётся со стартового времени. Настройки сохранятся, резервная копия "
            "файла состояния останется рядом.",
            parent=self,
        ):
            self.controller.reset_progress()
            self.close()

    def stop_daemon(self) -> None:
        if not messagebox.askyesno(
            "Nightfall",
            "Остановить фоновую службу?\n\nРасписание и напоминания перестанут "
            "работать, пока вы снова не запустите Nightfall.",
            parent=self,
        ):
            return
        try:
            self.controller.quit_daemon()
        except ipc.IpcError:
            pass
        self.app.online = False
        self.close()

    def copy_log(self) -> None:
        try:
            text = self.controller.export_history()
        except ipc.IpcError as error:
            messagebox.showerror("Nightfall", f"Фон не отвечает: {error}", parent=self)
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        messagebox.showinfo("Nightfall", "История скопирована в буфер обмена.", parent=self)

    def close(self) -> None:
        self.alive = False
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()


# --------------------------------------------------------------------------- #
def run_popup(payload: dict, client: DaemonClient, scale: Optional[float] = None) -> int:
    """Показать напоминание отдельным процессом и выйти (его зовёт демон)."""
    root = tk.Tk()
    root.withdraw()
    root.configure(bg=C["bg0"])
    if scale is None:
        try:
            scale = max(1.0, min(1.6, round(root.winfo_fpixels("1i") / 96.0, 2)))
        except tk.TclError:  # pragma: no cover
            scale = 1.0
    fonts = Fonts(root, scale)

    def postpone() -> None:
        try:
            client.postpone(payload.get("postpone"))
        except ipc.IpcError:
            pass
        root.destroy()

    def dismiss() -> None:
        root.destroy()

    kind = payload.get("kind", "reminder")
    if kind == "reminder":
        buttons = [
            (f"+{payload.get('postpone', 15)} мин", "primary", postpone),
            ("Понятно", "ghost", dismiss),
        ]
    else:
        buttons = [("Хорошо", "ghost", dismiss)]

    lines = [payload.get("line1", ""), payload.get("line2", "")]
    popup = Popup(
        root, fonts, scale,
        str(payload.get("title", "Nightfall")),
        [line for line in lines if line],
        buttons,
        str(payload.get("accent", C["violet2"])),
        autoclose=int(payload.get("autoclose", 30)),
    )
    popup.focus_force()
    root.mainloop()
    return 0


def run_gui(client: DaemonClient) -> int:
    window = NightfallWindow(client)
    window.run()
    return 0
