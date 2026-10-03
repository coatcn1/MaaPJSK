from __future__ import annotations

import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

import numpy as np

from project_sekai.navigator import Navigator
from project_sekai.maa_device import MaaDevice


class ControllerClickTests(unittest.TestCase):
    def test_failed_click_is_reported_instead_of_silently_waiting(self) -> None:
        controller = SimpleNamespace(post_click=Mock())
        controller.post_click.return_value.wait.return_value.succeeded = False
        with self.assertRaisesRegex(RuntimeError, "控制器点击失败"):
            MaaDevice(controller).tap(794, 386)

    def test_successful_click_waits_for_controller_completion(self) -> None:
        controller = SimpleNamespace(post_click=Mock())
        controller.post_click.return_value.wait.return_value.succeeded = True
        MaaDevice(controller).tap(794, 386)
        controller.post_click.assert_called_once_with(794, 386)
        controller.post_click.return_value.wait.assert_called_once_with()


class RecoveryButtonLayoutTests(unittest.TestCase):
    def test_first_plus_click_ignored_is_retried_without_double_selection(self) -> None:
        self._exercise_selection(ignored_clicks=1, expected_clicks=2)

    def test_delayed_decide_enable_does_not_click_plus_again(self) -> None:
        self._exercise_selection(confirm_delay=5, expected_clicks=1)

    def test_unresponsive_plus_stops_without_confirming(self) -> None:
        self._exercise_selection(ignored_clicks=99, expected_clicks=3, fails=True)

    def test_unknown_confirmation_is_not_clicked(self) -> None:
        self._exercise_selection(unknown_prompt=True)

    def test_number_templates_are_not_required(self) -> None:
        self._exercise_selection()

    def test_retries_reset_sliders_before_another_plus(self) -> None:
        self._exercise_selection(confirm_delay=12, expected_clicks=2)

    def _exercise_selection(self, *, ignored_clicks=0, confirm_delay=0, expected_clicks=1,
                            fails=False, unknown_prompt=False):
        navigator = Navigator.__new__(Navigator)
        navigator.threshold = 0.95
        navigator.dry_run = False
        navigator.stop_requested = lambda: False
        navigator._save_failure = lambda *_args: None
        navigator.log_message = lambda *_args: None
        rng = np.random.default_rng(23)
        names = ("recovery_dialog", "recovery_item_tab", "recovery_large_row",
                 "recovery_ok_dialog", "prepare")
        navigator.templates = {name: rng.integers(0, 256, (18, 18, 3), dtype=np.uint8)
                               for name in names}
        navigator.templates["recovery_confirm_enabled"] = rng.integers(
            0, 256, (45, 204, 3), dtype=np.uint8)
        navigator.templates["recovery_ok_button"] = rng.integers(
            0, 256, (35, 202, 3), dtype=np.uint8)
        initial = np.zeros((720, 1280, 3), dtype=np.uint8)
        for name, x, y in (("recovery_dialog", 680, 27), ("recovery_item_tab", 360, 87),
                           ("recovery_large_row", 420, 314)):
            initial[y:y + 18, x:x + 18] = navigator.templates[name]
        selected = initial.copy()
        confirmed = selected.copy()
        confirmed[634:679, 778:982] = navigator.templates["recovery_confirm_enabled"]
        ok_prompt = np.zeros_like(initial)
        ok_prompt[302:320, 389:407] = navigator.templates["recovery_ok_dialog"]
        ok_prompt[405:440, 658:860] = navigator.templates["recovery_ok_button"]
        prepare = np.zeros_like(initial)
        prepare[135:153, 45:63] = navigator.templates["prepare"]
        state = {"frame": initial, "clicks": 0, "reads": 0, "closed": False, "time": 0.0,
                 "quantity": 0, "max_quantity": 0}
        taps = []

        class Device:
            def screenshot(self):
                if state["frame"] is selected:
                    state["reads"] += 1
                    if state["reads"] > confirm_delay:
                        state["frame"] = confirmed
                return state["frame"]

            def tap(self, x, y):
                taps.append((x, y))
                if (x, y) == (534, 386):
                    state["quantity"] = 0
                    state["frame"] = initial
                elif (x, y) == (794, 386):
                    state["clicks"] += 1
                    if state["clicks"] > ignored_clicks:
                        state["quantity"] += 1
                        state["max_quantity"] = max(state["max_quantity"], state["quantity"])
                        state["frame"] = selected
                elif (x, y) == (880, 656):
                    state["frame"] = np.zeros_like(initial) if unknown_prompt else ok_prompt
                elif (x, y) == (759, 422):
                    state["frame"] = prepare
                    state["closed"] = True

        navigator.device = Device()
        def sleep(seconds):
            state["time"] += seconds
        with patch("project_sekai.navigator.time.sleep", side_effect=sleep), patch(
                "project_sekai.navigator.time.monotonic", side_effect=lambda: state["time"]):
            if unknown_prompt:
                with self.assertRaisesRegex(TimeoutError, "OK"):
                    navigator.recover_bonus_from_dialog("large")
            elif fails:
                with self.assertRaisesRegex(RuntimeError, "选择一瓶"):
                    navigator.recover_bonus_from_dialog("large")
            else:
                navigator.recover_bonus_from_dialog("large")
        self.assertEqual(state["clicks"], expected_clicks)
        self.assertEqual(state["closed"], not (fails or unknown_prompt))
        self.assertEqual(taps.count((759, 422)), 0 if (fails or unknown_prompt) else 1)
        self.assertLessEqual(state["max_quantity"], 1)
        self.assertNotIn((640, 656), taps)

    def test_decide_is_located_in_both_two_and_three_button_layouts(self) -> None:
        for button_x in (658, 778):
            with self.subTest(button_x=button_x):
                navigator = Navigator.__new__(Navigator)
                navigator.threshold = 0.95
                navigator.dry_run = False
                navigator.stop_requested = lambda: False
                navigator._save_failure = lambda *_args: None
                navigator.log_message = lambda *_args: None
                rng = np.random.default_rng(23)
                names = ("recovery_dialog", "recovery_item_tab", "recovery_large_row",
                         "recovery_ok_dialog", "prepare")
                navigator.templates = {name: rng.integers(0, 256, (18, 18, 3), dtype=np.uint8)
                                       for name in names}
                navigator.templates["recovery_confirm_enabled"] = rng.integers(
                    0, 256, (45, 204, 3), dtype=np.uint8)
                navigator.templates["recovery_ok_button"] = rng.integers(
                    0, 256, (35, 202, 3), dtype=np.uint8)
                initial = np.zeros((720, 1280, 3), dtype=np.uint8)
                for name, x, y in (("recovery_dialog", 680, 27), ("recovery_item_tab", 360, 87),
                                   ("recovery_large_row", 420, 314)):
                    initial[y:y + 18, x:x + 18] = navigator.templates[name]
                selected = initial.copy()
                selected[634:679, button_x:button_x + 204] = navigator.templates["recovery_confirm_enabled"]
                ok_prompt = np.zeros_like(initial)
                ok_prompt[302:320, 389:407] = navigator.templates["recovery_ok_dialog"]
                ok_prompt[405:440, 658:860] = navigator.templates["recovery_ok_button"]
                prepare = np.zeros_like(initial)
                prepare[135:153, 45:63] = navigator.templates["prepare"]
                frames = [initial]
                taps = []
                class Device:
                    def screenshot(self):
                        return frames[0]
                    def tap(self, x, y):
                        taps.append((x, y))
                        if (x, y) == (794, 386):
                            frames[0] = selected
                        elif (x, y) == (button_x + 102, 656):
                            frames[0] = ok_prompt
                        elif (x, y) == (759, 422):
                            frames[0] = prepare
                navigator.device = Device()
                with patch("project_sekai.navigator.time.sleep"):
                    navigator.recover_bonus_from_dialog("large")
                self.assertEqual(taps, [(534, 247), (534, 386), (794, 386), (button_x + 102, 656), (759, 422)])
                self.assertNotIn((640, 656), taps, "不能点击广告回复按钮")


if __name__ == "__main__":
    unittest.main()
