"""Заголовок окна: у Nightfall должна остаться только кнопка «закрыть».

Рамку рисует оконный менеджер, и «убрать только свернуть» он не умеет:
подсказки Motif он понимает как «всё или ничего». Поэтому рамку WM мы
отключаем целиком (decorations = 0), а свою полоску заголовка рисуем сами —
в ней есть только крестик. Здесь проверяются именно биты подсказок: ошибка
в них не видна глазом, но возвращает лишние кнопки.
"""

from __future__ import annotations

import unittest

from nightfall.ui import motif_hints_values

MWM_HINTS_FUNCTIONS = 0x1
MWM_HINTS_DECORATIONS = 0x2
MWM_FUNC_ALL = 0x1
MWM_FUNC_MAXIMIZE = 0x8
MWM_FUNC_MINIMIZE = 0x10
MWM_FUNC_CLOSE = 0x20
MWM_DECOR_ALL = 0x1
MWM_DECOR_MINIMIZE = 0x20
MWM_DECOR_MAXIMIZE = 0x40
MWM_DECOR_TITLE = 0x8


class MotifHintsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.flags, self.functions, self.decorations = motif_hints_values()

    def test_both_sections_are_described(self) -> None:
        self.assertTrue(self.flags & MWM_HINTS_FUNCTIONS)
        self.assertTrue(self.flags & MWM_HINTS_DECORATIONS)

    def test_all_shortcut_is_not_used(self) -> None:
        # если выставлен *_ALL, оконные менеджеры разрешают вообще всё
        # и «свернуть»/«развернуть» остаются на месте
        self.assertFalse(self.functions & MWM_FUNC_ALL)
        self.assertFalse(self.decorations & MWM_DECOR_ALL)

    def test_minimize_and_maximize_are_forbidden(self) -> None:
        for bit in (MWM_FUNC_MINIMIZE, MWM_FUNC_MAXIMIZE):
            self.assertFalse(self.functions & bit)
        for bit in (MWM_DECOR_MINIMIZE, MWM_DECOR_MAXIMIZE):
            self.assertFalse(self.decorations & bit)

    def test_window_manager_frame_is_off(self) -> None:
        # никакой рамки от WM: заголовок рисует само приложение
        self.assertEqual(self.decorations, 0)

    def test_close_is_allowed(self) -> None:
        self.assertTrue(self.functions & MWM_FUNC_CLOSE)


if __name__ == "__main__":
    unittest.main()
