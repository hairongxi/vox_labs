from __future__ import annotations

import unittest

from voxhud.platform.win32 import _parse_hotkey


class HotkeyParserTests(unittest.TestCase):
    def test_default_hotkeys(self) -> None:
        self.assertEqual(_parse_hotkey("ctrl+alt+space"), (0x0003, 0x20))
        self.assertEqual(_parse_hotkey("ctrl+alt+x"), (0x0003, ord("X")))

    def test_shift_win_and_function_keys(self) -> None:
        self.assertEqual(_parse_hotkey("shift+win+f8"), (0x000C, 0x77))
        self.assertEqual(_parse_hotkey("F12"), (0, 0x7B))

    def test_invalid(self) -> None:
        self.assertIsNone(_parse_hotkey(""))
        self.assertIsNone(_parse_hotkey("ctrl+alt+notakey"))
        self.assertIsNone(_parse_hotkey("space"))


if __name__ == "__main__":
    unittest.main()
