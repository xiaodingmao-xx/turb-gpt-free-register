# -*- coding: utf-8 -*-
import unittest

from core.windows_window import calculate_center_position


class WindowsWindowTests(unittest.TestCase):
    def test_center_position_uses_primary_work_area(self):
        self.assertEqual(
            calculate_center_position((0, 0, 1920, 1080), (1000, 800)),
            (460, 140),
        )


if __name__ == "__main__":
    unittest.main()
