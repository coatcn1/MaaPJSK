import os
from pathlib import Path
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from project_sekai.chart_player import FirstHeadOutsideWindow, StartAnchor, first_note_y
from project_sekai.sus_chart import Gesture, Point, parse_sus


class GoldTapHeadAnchorTests(unittest.TestCase):
    gesture = Gesture((Point(0., 10, 4, kind=2),), "tap", critical=True)

    def anchor(self, guard=True):
        with patch.dict(os.environ, {"MAAPJSK_GOLD_TAP_HEAD_GUARD": "1" if guard else "0"}):
            return StartAnchor(self.gesture, minimum_samples=4)

    def frame(self, *, y=150, wrong_lane=False, connection="above", core_hue=25, core_saturation=40):
        hsv = np.zeros((720, 1280, 3), np.uint8)
        center = y + 10
        width = round(4 * (1000 / 12) * center / 570)
        x = round(640 + (self.gesture.points[0].x - 640) * center / 570 - width / 2) + (250 if wrong_lane else 0)
        # 后继金色轨迹只模拟真实连通来源；其时序不进入首音上下文。
        if connection == "above":
            hsv[60:y, x + width // 2 + 10:x + width // 2 + 18] = (25, 200, 170)
        elif connection == "below":
            hsv[y + 20:y + 90, x + width // 2:x + width // 2 + 8] = (25, 200, 170)
        hsv[y:y + 20, x:x + width] = (25, 200, 240)
        hsv[y + 2:y + 18, x + 2:x + width - 2] = (core_hue, core_saturation, 250)
        return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)

    def detect(self, frame, baseline=None, **kwargs):
        return first_note_y(frame, self.gesture, baseline, gold_tap_head_guard=True, **kwargs)

    def test_connected_gold_tap_head_uses_complete_core_without_dense_context(self):
        anchor = self.anchor()
        anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
        self.assertIsNone(anchor.observe(self.frame(), 10., capture_seconds=.02))
        self.assertEqual(anchor.dense_following_gestures, ())
        self.assertEqual(anchor.samples, [(10., 160.)])

    def test_gold_guard_defaults_on_and_only_explicit_zero_disables(self):
        for value in (None, "", "0", "1", "true", "false", "other"):
            with self.subTest(value=value):
                with patch.dict(os.environ, {}):
                    os.environ.pop("MAAPJSK_GOLD_TAP_HEAD_GUARD", None)
                    if value is not None:
                        os.environ["MAAPJSK_GOLD_TAP_HEAD_GUARD"] = value
                    anchor = StartAnchor(self.gesture, minimum_samples=4)
                anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
                anchor.observe(self.frame(), 10.)
                self.assertEqual(anchor.gold_tap_head_guard, value != "0")
                self.assertEqual(anchor.samples, [] if value == "0" else [(10., 160.)])

    def test_initialization_freezes_explicit_guard(self):
        for initial in (False, True):
            with self.subTest(initial=initial):
                anchor = self.anchor(initial)
                with patch.dict(os.environ, {"MAAPJSK_GOLD_TAP_HEAD_GUARD": "0" if initial else "1"}):
                    anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
                    anchor.observe(self.frame(), 10.)
                self.assertEqual(anchor.gold_tap_head_guard, initial)
                self.assertEqual(anchor.samples, [(10., 160.)] if initial else [])

    def test_clean_isolated_gold_tap_keeps_original_center(self):
        frame = self.frame(connection="none")
        self.assertEqual(first_note_y(frame, self.gesture), 160.)
        self.assertEqual(self.detect(frame), 160.)

    def test_full_core_baseline_validation_rejects_static_and_half_changed_heads(self):
        frame = self.frame()
        self.assertIsNone(self.detect(frame, frame))
        baseline = np.zeros_like(frame)
        baseline[152:160] = frame[152:160]
        self.assertIsNone(self.detect(frame, baseline))
        baseline[155:160] = 0
        self.assertEqual(self.detect(frame, baseline), 160.)

    def test_wrong_lane_white_core_and_missing_source_cannot_supply_first_head(self):
        for options in ({"wrong_lane": True}, {"core_hue": 0}):
            with self.subTest(options=options):
                self.assertIsNone(self.detect(self.frame(**options)))
        hsv = cv2.cvtColor(self.frame(), cv2.COLOR_BGR2HSV)
        hsv[60:170] = (25, 20, 250)
        self.assertIsNone(self.detect(cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)))

    def test_source_extending_below_core_is_not_first_tap_front(self):
        self.assertIsNone(self.detect(self.frame(connection="below")))

    def test_disconnected_gold_sources_do_not_form_one_head(self):
        hsv = cv2.cvtColor(self.frame(), cv2.COLOR_BGR2HSV)
        hsv[150:152, 730:735] = 0
        hsv[168:170, 730:735] = 0
        self.assertIsNone(self.detect(cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)))

    def test_two_rectangular_half_cores_cannot_be_chosen_as_complete_head(self):
        hsv = np.zeros((720, 1280, 3), np.uint8)
        hsv[60:150, 746:754] = (25, 200, 170)
        hsv[150:170, 671:797] = (25, 200, 240)
        hsv[152:168, 673:795] = (25, 40, 250)
        # 两片各自能通过位置、宽度和来源门槛，仍不能把任一半芯包装成唯一完整首头。
        hsv[152:168, 733:735] = (25, 200, 240)
        self.assertIsNone(self.detect(cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)))

    def test_upper_clipped_head_cannot_supply_complete_core(self):
        self.assertIsNone(self.detect(self.frame(y=35)))

    def test_bottom_passed_head_rejects_before_a_later_trace_can_replace_it(self):
        frame = self.frame(y=530)
        self.assertIsNone(self.detect(frame))
        with self.assertRaisesRegex(FirstHeadOutsideWindow, "首金色 TAP 头已越过"):
            self.detect(frame, reject_passed_slide=True)
        anchor = self.anchor()
        anchor.baseline = np.zeros_like(frame)
        anchor.samples = [(10., 160.)]
        with self.assertRaises(FirstHeadOutsideWindow):
            anchor.observe(frame, 10.5)
        self.assertEqual(anchor.failure_reason, "first_head_outside_window")
        self.assertEqual(anchor.samples, [(10., 160.)])

    def test_passed_head_with_downward_source_has_frame_priority_over_valid_follower(self):
        connected = cv2.connectedComponentsWithStats
        for first_y in (510, 530):
            # 510 的芯还在窗口内但来源已下越界；530 的芯和来源都已越界。
            hsv = cv2.cvtColor(self.frame(y=first_y, connection="below"), cv2.COLOR_BGR2HSV)
            follower = cv2.cvtColor(self.frame(connection="none"), cv2.COLOR_BGR2HSV)
            foreground = follower[:, :, 2] > 0
            hsv[foreground] = follower[foreground]
            frame = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
            self.assertEqual(self.detect(self.frame(connection="none")), 160.)
            for reverse in (False, True):
                def reordered(mask, *args, **kwargs):
                    count, labels, stats, centroids = connected(mask, *args, **kwargs)
                    if not reverse:
                        return count, labels, stats, centroids
                    # 只改变遍历顺序，标签、统计和真实像素一并重排，防止测试依赖 OpenCV 枚举顺序。
                    order = np.array([0, *range(count - 1, 0, -1)])
                    mapping = np.empty(count, np.int32)
                    mapping[order] = np.arange(count)
                    return count, mapping[labels], stats[order], centroids[order]
                for reject in (False, True):
                    with self.subTest(first_y=first_y, reverse=reverse, reject=reject):
                        with patch("project_sekai.chart_player.cv2.connectedComponentsWithStats", side_effect=reordered):
                            if reject:
                                with self.assertRaisesRegex(FirstHeadOutsideWindow, "首金色 TAP 头已越过"):
                                    self.detect(frame, np.zeros_like(frame), reject_passed_slide=True)
                            else:
                                self.assertIsNone(self.detect(frame, np.zeros_like(frame)))

    def test_following_trace_without_plain_tap_core_does_not_restart_anchor(self):
        hsv = np.zeros((720, 1280, 3), np.uint8)
        hsv[100:120, 670:735] = (25, 170, 230)
        cv2.fillConvexPoly(hsv, np.array([[690, 105], [702, 90], [714, 105]]), (0, 0, 255))
        frame = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
        self.assertIsNone(self.detect(frame))
        anchor = self.anchor()
        anchor.baseline = np.zeros_like(frame)
        anchor.samples = [(10., 160.), (10.1, 260.)]
        self.assertIsNone(anchor.observe(frame, 10.3))
        self.assertEqual(anchor.samples, [(10., 160.), (10.1, 260.)])

    def test_new_core_path_does_not_expand_flick_trace_or_slide(self):
        frame = self.frame()
        for gesture in (Gesture(self.gesture.points, "tap", flick=1, critical=True),
                        Gesture((Point(0., 10, 4, kind=6),), "trace", critical=True),
                        Gesture((Point(0., 10, 4, kind=2), Point(1., 10, 4)), "slide", critical=True)):
            with self.subTest(kind=gesture.kind, flick=gesture.flick):
                self.assertEqual(first_note_y(frame, gesture, gold_tap_head_guard=True), first_note_y(frame, gesture))

    def test_private_gold_tap_frame_replay(self):
        # 私有原始帧只由本次临时路径启用，不复制到测试或发行资产。
        probe = os.environ.get("MAAPJSK_GOLD_TAP_REPLAY_NPZ")
        chart_path = os.environ.get("MAAPJSK_GOLD_TAP_REPLAY_CHART")
        if not probe or not chart_path:
            self.skipTest("未提供私有金色首 TAP 的帧与谱面路径")
        chart = parse_sus(Path(chart_path).read_text(encoding="utf-8"))
        with np.load(probe, allow_pickle=False) as data:
            frames, captured_at, capture_seconds = data["frames"], data["captured_at"], data["capture_seconds"]
        with patch.dict(os.environ, {"MAAPJSK_GOLD_TAP_HEAD_GUARD": "1"}):
            anchor = StartAnchor(chart.first, minimum_samples=4)
        epoch = None
        for index, frame in enumerate(frames):
            epoch = anchor.observe(frame, float(captured_at[index]), capture_seconds=float(capture_seconds[index]))
            if epoch is not None:
                break
        self.assertIsNotNone(epoch)
        self.assertEqual(index, 9)
        self.assertEqual([y for _, y in anchor.samples], [68., 101.5, 154.5, 210.5, 290.])
        self.assertEqual(anchor.fit["model"], "perspective")
        self.assertLess(anchor.fit["residual_pixels"], 4)
        self.assertGreater(anchor.fit["first_hit"] - float(captured_at[index]), float(capture_seconds[index]) + .080)
        self.assertTrue(anchor.fit["gold_tap_head_guard"])
        self.assertIsNone(self.detect(frames[13], frames[0]))
        with self.assertRaises(FirstHeadOutsideWindow):
            self.detect(frames[11], frames[0], reject_passed_slide=True)
        with patch.dict(os.environ, {"MAAPJSK_GOLD_TAP_HEAD_GUARD": "0"}):
            original = StartAnchor(chart.first, minimum_samples=4)
        with self.assertRaisesRegex(RuntimeError, "轨迹未通过同步门槛且等待超时"):
            for index, frame in enumerate(frames):
                original.observe(frame, float(captured_at[index]), capture_seconds=float(capture_seconds[index]))


if __name__ == "__main__":
    unittest.main()
