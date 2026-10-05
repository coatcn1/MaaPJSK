from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from project_sekai.navigator import Navigator


class FakeDevice:
    def __init__(self, frames: list[np.ndarray]) -> None:
        self.frames = frames
        self.index = 0
        self.taps: list[tuple[int, int]] = []
        self.backs = 0

    def screenshot(self) -> np.ndarray:
        frame = self.frames[min(self.index, len(self.frames) - 1)]
        self.index += 1
        return frame

    def tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))

    def back(self) -> None:
        self.backs += 1


class ResultNavigationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        pattern = np.random.default_rng(7).integers(0, 256, (20, 20, 3), dtype=np.uint8)
        self.assertTrue(cv2.imwrite(str(root / "home.png"), pattern))
        (root / "config.json").write_text(json.dumps({"threshold": 0.95, "templates": {"home": "home.png"}}), encoding="utf-8")
        self.config = root / "config.json"
        self.unknown = np.zeros((80, 120, 3), dtype=np.uint8)
        self.home = self.unknown.copy()
        self.home[30:50, 40:60] = pattern

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_uses_back_and_safe_pixel_until_home(self) -> None:
        device = FakeDevice([self.unknown, self.unknown, self.home, self.home])
        navigator = Navigator(device, self.config)
        with patch("project_sekai.navigator.time.sleep"):
            navigator.collect_with_back(timeout=1)
        self.assertEqual(device.backs, 1)
        self.assertEqual(device.taps, [(1279, 719), (1279, 719)])

    def test_never_sends_back_after_home_appears(self) -> None:
        device = FakeDevice([self.unknown, self.home, self.home, self.home])
        navigator = Navigator(device, self.config)
        with patch("project_sekai.navigator.time.sleep"):
            navigator.collect_with_back(timeout=1)
        self.assertEqual(device.backs, 0)
        self.assertEqual(device.taps, [(1279, 719)])

    def test_home_fallback_returns_with_back_until_home_is_stable(self) -> None:
        device = FakeDevice([self.unknown, self.home, self.home])
        navigator = Navigator(device, self.config)
        with patch("project_sekai.navigator.time.sleep"):
            navigator.return_to_home(max_backs=2)
        self.assertEqual(device.backs, 1)
        self.assertEqual(device.taps, [])

    def test_home_fallback_is_bounded(self) -> None:
        device = FakeDevice([self.unknown])
        navigator = Navigator(device, self.config)
        with patch("project_sekai.navigator.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "ESC 返回次数=2"):
                navigator.return_to_home(max_backs=2)
        self.assertEqual(device.backs, 2)

    def test_home_fallback_does_not_interrupt_live(self) -> None:
        device = FakeDevice([self.unknown])
        navigator = Navigator(device, self.config)
        navigator.templates["playing"] = self.unknown[0:20, 0:20]
        navigator.match = lambda _frame, name, _area=None: (1.0 if name == "playing" else 0.0, (0, 0))
        with self.assertRaisesRegex(RuntimeError, "仍在演奏中"):
            navigator.return_to_home()
        self.assertEqual(device.backs, 0)

    def test_home_fallback_stops_before_input_when_cancelled(self) -> None:
        device = FakeDevice([self.unknown])
        navigator = Navigator(device, self.config, stop_requested=lambda: True)
        with self.assertRaises(InterruptedError):
            navigator.return_to_home()
        self.assertEqual(device.backs, 0)
        self.assertEqual(device.index, 0)

    def _title_navigator(self, device: FakeDevice) -> Navigator:
        navigator = Navigator(device, self.config)
        navigator.templates["title_screen"] = self.unknown[0:20, 0:20]
        navigator.match = lambda _frame, name, _area=None: (1.0 if name == "title_screen" else 0.0, (0, 0))
        return navigator

    def test_title_during_settlement_stops_without_back_or_counting(self) -> None:
        device = FakeDevice([self.unknown])
        navigator = self._title_navigator(device)
        with patch("project_sekai.navigator.time.sleep"), patch("project_sekai.navigator.time.monotonic", side_effect=[0, 0, 2]):
            with self.assertRaisesRegex(RuntimeError, "标题页"):
                navigator.collect_with_back(timeout=0.05)
        self.assertEqual(device.backs, 0)
        self.assertEqual(device.taps, [])
        self.assertEqual(navigator.completed_rounds, 0)

    def test_title_after_animation_tap_stops_before_back(self) -> None:
        device = FakeDevice([self.unknown, self.home])
        navigator = self._title_navigator(device)
        navigator.match = lambda frame, name, _area=None: (1.0 if name == "title_screen" and frame is self.home else 0.0, (0, 0))
        with patch("project_sekai.navigator.time.sleep"), patch("project_sekai.navigator.time.monotonic", side_effect=[0, 0, 2]):
            with self.assertRaisesRegex(RuntimeError, "标题页"):
                navigator.collect_with_back(timeout=0.05)
        self.assertEqual(device.backs, 0)
        self.assertEqual(device.taps, [(1279, 719)])

    def test_title_before_navigation_stops_without_input(self) -> None:
        device = FakeDevice([self.unknown])
        navigator = self._title_navigator(device)
        with patch("project_sekai.navigator.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "标题页"):
                navigator.return_to_home(max_backs=2)
        self.assertEqual(device.backs, 0)
        self.assertEqual(device.taps, [])


if __name__ == "__main__":
    unittest.main()
