import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np

from project_sekai import diagnostics
from project_sekai.live_exit import background_game


LAUNCHER = "com.android.launcher3/.Launcher\n"
LAUNCHER_FOCUS = "mCurrentFocus=Window{123 u0 com.android.launcher3/.Launcher}\n"
GAME_FOCUS = "mCurrentFocus=Window{123 u0 com.sega.pjsekai/.MainActivity}\n"


class BackgroundGameTests(unittest.TestCase):
    def tearDown(self):
        diagnostics.configure(False, framework=False)

    def device(self):
        device = Mock()
        device.shell.side_effect = [LAUNCHER, LAUNCHER_FOCUS]
        device.screenshot.return_value = np.zeros((5, 5, 3), np.uint8)
        return device

    def released_report(self):
        return {"engine": "native", "playback": {
            "sent_actions": 3, "release_confirmed": True,
            "release": {"reset_executed": True,
                        "release_proof": "current-reset-jlog-and-cleanup"}}}

    def test_disabled_logging_keeps_home_confirmation_in_memory_without_files(self):
        diagnostics.configure(True, framework=False)
        for report in ({}, self.released_report()):
            with self.subTest(report=report), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                device = self.device()
                background_game(device, report, root / "run", "playback_failed", log=Mock())
                device.home.assert_called_once()
                self.assertTrue(report["background_exit"]["home_request_succeeded"])
                self.assertTrue(report["background_exit"]["confirmed"])
                self.assertEqual(report["background_exit"]["foreground_package"], "com.android.launcher3")
                self.assertEqual(list(root.rglob("*")), [])

    def test_enabled_logging_writes_initial_report_before_home_and_confirms_in_memory(self):
        diagnostics.configure(False, framework=False)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = self.released_report()
            device = self.device()

            def home():
                saved = json.loads((root / "report.json").read_text(encoding="utf-8"))
                self.assertFalse(saved["background_exit"]["confirmed"])
                self.assertEqual(saved["background_exit"]["touch_cleanup"], "confirmed")
                self.assertNotIn("home_request_succeeded", saved["background_exit"])

            device.home.side_effect = home
            background_game(device, report, root, "life_zero", log=Mock())
            device.home.assert_called_once()
            self.assertTrue(report["background_exit"]["confirmed"])
            self.assertTrue((root / "emulator-home.png").is_file())

    def test_disabled_logging_does_not_relax_current_native_release_gate(self):
        diagnostics.configure(True, framework=False)
        cases = [
            {"release_confirmed": False},
            {"release_confirmed": True},
            {"release_confirmed": True, "release": {"reset_executed": False,
                "release_proof": "current-reset-jlog-and-cleanup"}},
            {"release_confirmed": True, "release": {"reset_executed": True,
                "release_proof": "old-reset-jlog-and-cleanup"}},
        ]
        for playback in cases:
            with self.subTest(playback=playback), tempfile.TemporaryDirectory() as tmp:
                device = self.device()
                report = {"engine": "native", "playback": playback}
                with self.assertRaisesRegex(RuntimeError, "释放未确认"):
                    background_game(device, report, Path(tmp), "life_zero", log=Mock())
                device.home.assert_not_called()
                device.shell.assert_not_called()
                self.assertNotIn("background_exit", report)
                self.assertEqual(list(Path(tmp).rglob("*")), [])

    def test_disabled_logging_requires_launcher_focus_and_retry_does_not_repeat_home(self):
        diagnostics.configure(True, framework=False)
        with tempfile.TemporaryDirectory() as tmp:
            report = self.released_report()
            device = self.device()
            device.shell.side_effect = [LAUNCHER, GAME_FOCUS]
            with patch("project_sekai.live_exit.time.monotonic", side_effect=[0, 0, 9]), \
                 patch("project_sekai.live_exit.time.sleep"), \
                 self.assertRaisesRegex(RuntimeError, "未确认模拟器主页焦点"):
                background_game(device, report, Path(tmp), "life_zero", log=Mock())
            self.assertFalse(report["background_exit"]["confirmed"])
            device.screenshot.assert_not_called()
            device.shell.side_effect = [LAUNCHER, LAUNCHER_FOCUS]
            background_game(device, report, Path(tmp), "life_zero", log=Mock())
            device.home.assert_called_once()
            self.assertTrue(report["background_exit"]["confirmed"])
            self.assertEqual(list(Path(tmp).rglob("*")), [])

    def test_disabled_logging_stop_prevents_home(self):
        diagnostics.configure(True, framework=False)
        with tempfile.TemporaryDirectory() as tmp:
            device = self.device()
            report = self.released_report()
            with self.assertRaises(InterruptedError):
                background_game(device, report, Path(tmp), "life_zero", log=Mock(),
                                stop_requested=lambda: True)
            device.home.assert_not_called()
            self.assertFalse(report["background_exit"]["confirmed"])
            self.assertEqual(list(Path(tmp).rglob("*")), [])

    def test_enabled_report_write_failure_still_prevents_home(self):
        diagnostics.configure(False, framework=False)
        device = self.device()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "report.json").mkdir()
            with self.assertRaises(OSError):
                background_game(device, self.released_report(), root, "life_zero", log=Mock())
        device.home.assert_not_called()
        device.shell.assert_not_called()
