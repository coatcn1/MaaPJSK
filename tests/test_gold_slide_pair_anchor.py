from dataclasses import replace
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from project_sekai.chart_player import FirstHeadOutsideWindow, StartAnchor, first_note_context, first_note_y
from project_sekai.sus_chart import Chart, Gesture, Point, parse_sus


class GoldSlidePairAnchorTests(unittest.TestCase):
    def chart(self, flick=1):
        return Chart(tuple(Gesture((Point(0., lane, 6, kind=1), Point(1., lane, 6, kind=5),
                                    Point(2., lane, 6, kind=2, direction=flick)), "slide", flick=flick,
                                   critical=True) for lane in (2, 8)), 480, ((0., 120.),))

    def frame(self, *, y=150, height=20, separated=False, body=False, color=(25, 200, 240)):
        hsv = np.zeros((720, 1280, 3), np.uint8)
        center = y + height / 2
        width = round(1000 * center / 570)
        left = round(640 - width / 2)
        hsv[y:y + height, left:left + width] = color
        if separated:
            hsv[y:y + height, 638:642] = 0
        if body:
            # 长条的暗色 body 向上延伸，但首头仍是完整亮横条，不套用 TAP 的顶部裁边规则。
            hsv[:y, 610:614] = (25, 200, 110)
        return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)

    def detect(self, frame, *, chart=None, context=None, guard=True, baseline=None, **kwargs):
        chart = chart or self.chart()
        if context is None:
            context = first_note_context(chart)
        return first_note_y(frame, chart.first, baseline, simultaneous_gestures=context,
                            gold_slide_pair_guard=guard, **kwargs)

    def anchor(self, chart=None, *, guard=True):
        chart = chart or self.chart()
        with patch.dict(os.environ, {"MAAPJSK_GOLD_SLIDE_PAIR_GUARD": "1" if guard else "0"}):
            return StartAnchor(chart.first, minimum_samples=4, simultaneous_gestures=first_note_context(chart))

    def test_merged_pair_preserves_context_through_real_anchor_chain(self):
        chart = self.chart()
        self.assertEqual(first_note_context(chart), chart.gestures)
        self.assertIsNone(first_note_y(self.frame(), chart.first, simultaneous_gestures=chart.gestures))
        self.assertEqual(self.detect(self.frame()), 160.)
        anchor = self.anchor(chart)
        anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
        anchor.observe(self.frame(), 10., capture_seconds=.02)
        self.assertEqual(anchor.simultaneous_gestures, chart.gestures)
        self.assertEqual(anchor.samples, [(10., 160.)])

    def test_guard_defaults_on_only_explicit_zero_disables_and_freezes(self):
        for setting in (None, "", "0", "1", "true", "false", "other"):
            with self.subTest(setting=setting):
                with patch.dict(os.environ, {}):
                    os.environ.pop("MAAPJSK_GOLD_SLIDE_PAIR_GUARD", None)
                    if setting is not None:
                        os.environ["MAAPJSK_GOLD_SLIDE_PAIR_GUARD"] = setting
                    chart = self.chart()
                    anchor = StartAnchor(chart.first, minimum_samples=4, simultaneous_gestures=first_note_context(chart))
                with patch.dict(os.environ, {"MAAPJSK_GOLD_SLIDE_PAIR_GUARD": "1" if setting == "0" else "0"}):
                    anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
                    anchor.observe(self.frame(), 10.)
                self.assertEqual(anchor.gold_slide_pair_guard, setting != "0")
                self.assertEqual(anchor.samples, [] if setting == "0" else [(10., 160.)])

    def test_tail_flick_does_not_change_slide_head_color_or_onset(self):
        for flick in range(7):
            with self.subTest(flick=flick):
                chart = self.chart(flick=flick)
                self.assertEqual(first_note_context(chart), chart.gestures)
                self.assertEqual(self.detect(self.frame(), chart=chart), 160.)

    def test_separated_real_sources_keep_original_detection(self):
        chart = self.chart()
        frame = self.frame(separated=True)
        original = first_note_y(frame, chart.first)
        self.assertEqual(original, 160.)
        self.assertEqual(self.detect(frame), original)

    def test_invalid_context_cannot_supply_merged_first_head(self):
        chart = self.chart()
        first, right = chart.gestures
        invalid = [(), (right,), (first, first), (first, right, right), (first, None),
                   (first, replace(right, critical=False)), (first, replace(right, kind="trace")),
                   (first, replace(right, flick=True)), (first, replace(right, flick=1.)),
                   (first, replace(right, flick=7)),
                   (first, replace(right, points=(Point(.01, 8, 6), *right.points[1:]))),
                   (first, replace(right, points=(Point(float("nan"), 8, 6), *right.points[1:]))),
                   (first, replace(right, points=(Point(0., 9, 5), *right.points[1:]))),
                   (first, replace(right, points=(Point(0., 7, 6), *right.points[1:]))),
                   (first, replace(right, points=(Point(0., 8, 7), *right.points[1:]))),
                   (first, replace(right, points=(Point(0., 8., 6), *right.points[1:]))),
                   (first, replace(right, points=(Point(0., 8, 6, kind=2), *right.points[1:]))),
                   (first, replace(right, points=(right.points[0],))),
                   (first, replace(right, points=(right.points[0], Point(0., 8, 6, kind=2)))),
                   (first, replace(right, points=(right.points[0], Point(float("inf"), 8, 6, kind=2)))),
                   (first, replace(right, points=(right.points[0], Point(1., 8, 6, kind=9), right.points[-1]))),
                   (first, replace(right, points=(right.points[0], Point(3., 8, 6, kind=5), right.points[-1])))]
        for context in invalid:
            with self.subTest(context=context):
                self.assertIsNone(self.detect(self.frame(), context=context))
                with patch.dict(os.environ, {"MAAPJSK_GOLD_SLIDE_PAIR_GUARD": "1"}):
                    self.assertEqual(StartAnchor(first, simultaneous_gestures=context).simultaneous_gestures, ())
        other_first = replace(first, points=(Point(.1, 2, 6), *first.points[1:]))
        self.assertIsNone(self.detect(self.frame(), chart=Chart((other_first,), 480, ((0., 120.),)),
                                     context=(first, right)))
        # bool / 浮点字段与整数比较可能相等，仍必须校验实际首手势的类型，不能借合法组通过。
        for selected in (replace(first, flick=True), replace(first, critical=1),
                         replace(first, points=(Point(0., 2., 6), *first.points[1:]))):
            with self.subTest(selected=selected):
                self.assertIsNone(self.detect(self.frame(), chart=replace(chart, gestures=(selected,)),
                                             context=chart.gestures))

    def test_extra_actual_onset_of_other_color_invalidates_context(self):
        chart = self.chart()
        extra = Gesture((Point(0., 4, 2),), "tap")
        self.assertEqual(first_note_context(replace(chart, gestures=(*chart.gestures, extra))), ())

    def test_wrong_color_location_width_and_static_source_are_rejected(self):
        frame = self.frame()
        self.assertIsNone(self.detect(self.frame(color=(80, 200, 240))))
        self.assertIsNone(self.detect(frame, baseline=frame))
        moved = np.roll(frame, 220, axis=1)
        self.assertIsNone(self.detect(moved))
        narrow = np.zeros_like(frame)
        narrow[:, 630:650] = frame[:, 630:650]
        self.assertIsNone(self.detect(narrow))

    def test_sparse_group_bbox_cannot_invent_a_local_head(self):
        hsv = np.zeros((720, 1280, 3), np.uint8)
        # 全组框合法，但首轨内仅细斜线；只测真实分量，不能借预期宽度补成局部横头。
        cv2.line(hsv, (500, 150), (780, 169), (25, 200, 240), 1)
        self.assertIsNone(self.detect(cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)))

    def test_upper_body_keeps_original_single_head_and_clean_merged_head(self):
        original = self.frame(y=42, height=5)
        self.assertEqual(self.detect(original), 44.5)
        # 不把暗色正常 body 当作浅亮芯或应用 TAP 上裁边保护。
        self.assertEqual(self.detect(self.frame(body=True)), 160.)
        hsv = np.zeros((720, 1280, 3), np.uint8)
        hsv[:47, 602:678] = (25, 200, 240)
        # 完整金色来源上越界且高于 TAP 原高度门槛，窗口内的真实局部首头仍沿用长条规则。
        self.assertEqual(self.detect(cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)), 43.5)

    def test_group_bottom_crop_has_priority_over_same_frame_successor(self):
        for with_successor in (False, True):
            frame = self.frame(y=535, height=65)
            if with_successor:
                successor = self.frame()
                frame[np.any(successor != 0, axis=2)] = successor[np.any(successor != 0, axis=2)]
            with self.subTest(with_successor=with_successor):
                self.assertIsNone(self.detect(frame))
                with self.assertRaises(FirstHeadOutsideWindow):
                    self.detect(frame, reject_passed_slide=True)
                anchor = self.anchor()
                anchor.observe(np.zeros_like(frame), 9.)
                with self.assertRaises(FirstHeadOutsideWindow):
                    anchor.observe(frame, 10.)
                self.assertEqual(anchor.failure_reason, "first_head_outside_window")
                self.assertEqual(anchor.samples, [])

    def test_one_pixel_group_bottom_remnant_cannot_allow_successor_to_take_over(self):
        frame = self.frame(y=539, height=65)
        successor = self.frame()
        foreground = np.any(successor != 0, axis=2)
        frame[foreground] = successor[foreground]
        # 仅剩一行的真实合并首头仍是下越界证据，不能因不足普通首头高度而忽略它。
        self.assertIsNone(self.detect(frame))
        with self.assertRaises(FirstHeadOutsideWindow):
            self.detect(frame, reject_passed_slide=True)
        anchor = self.anchor()
        anchor.observe(np.zeros_like(frame), 9.)
        with self.assertRaises(FirstHeadOutsideWindow):
            anchor.observe(frame, 10.)
        self.assertEqual(anchor.failure_reason, "first_head_outside_window")
        self.assertEqual(anchor.samples, [])

    def test_one_pixel_source_without_bottom_crossing_keeps_normal_height_gate(self):
        frame = self.frame(y=539, height=1)
        self.assertIsNone(self.detect(frame, reject_passed_slide=True))
        successor = self.frame()
        foreground = np.any(successor != 0, axis=2)
        frame[foreground] = successor[foreground]
        # 实际来源未下越界的一行不能当首头，也不能冒充拒绝整帧的下越界证据。
        self.assertEqual(self.detect(frame, reject_passed_slide=True), 160.)

    def test_private_original_frame_sequence(self):
        # 私有帧和谱面仅通过临时路径启用，不复制进测试或发行资源。
        probe = os.environ.get("MAAPJSK_GOLD_SLIDE_PAIR_REPLAY_NPZ")
        chart_path = os.environ.get("MAAPJSK_GOLD_SLIDE_PAIR_REPLAY_CHART")
        if not probe or not chart_path:
            self.skipTest("未提供私有双金色 Slide 开场帧与谱面")
        chart = parse_sus(Path(chart_path).read_text(encoding="utf-8"))
        with np.load(probe, allow_pickle=False) as data:
            frames, times, costs = data["frames"], data["captured_at"], data["capture_seconds"]
        self.assertEqual(len(first_note_context(chart)), 2)
        anchor = self.anchor(chart)
        epoch = None
        for index, frame in enumerate(frames):
            epoch = anchor.observe(frame, float(times[index]), capture_seconds=float(costs[index]))
            if epoch is not None:
                break
        self.assertIsNotNone(epoch)
        self.assertGreaterEqual(len(anchor.samples), 4)
        self.assertLessEqual(anchor.fit["residual_pixels"], 12)
        self.assertGreater(anchor.fit["first_hit"] - times[index], costs[index] + .080)
        self.assertTrue(anchor.fit["gold_slide_pair_guard"])
        with self.assertRaises(FirstHeadOutsideWindow):
            self.detect(frames[16], chart=chart, baseline=frames[0], reject_passed_slide=True)
        self.assertIsNone(self.detect(frames[16], chart=chart, baseline=frames[0]))
        original = self.anchor(chart, guard=False)
        with self.assertRaises(RuntimeError):
            for index, frame in enumerate(frames):
                self.assertIsNone(original.observe(frame, float(times[index]), capture_seconds=float(costs[index])))
        self.assertIsNone(original.fit)


if __name__ == "__main__":
    unittest.main()
