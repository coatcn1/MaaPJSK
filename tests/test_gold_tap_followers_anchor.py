from dataclasses import replace
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from project_sekai import chart_player
from project_sekai.sus_chart import Gesture, Point, parse_sus


class GoldTapFollowersAnchorTests(unittest.TestCase):
    def chart(self):
        # 同刻另一个轨道的首音不改变目标轨道后继顺序；只用可解析的真实 SUS 元数据。
        return parse_sus('#BPM01:120\n#00008:01\n#0001b:0000000023232323\n#00012:0000000023000000\n')

    def head(self, y, *, lane=11, width=3, height=16):
        hsv = np.zeros((720, 1280, 3), np.uint8)
        center = y + height / 2
        span = round(width * (1000 / 12) * center / 570)
        point = Point(1., lane, width, 2)
        x = round(640 + (point.x - 640) * center / 570 - span / 2)
        hsv[y:y + height, x:x + span] = (25, 200, 240)
        hsv[y + 2:y + height - 2, x + 2:x + span - 2] = (25, 40, 250)
        return hsv

    def frame(self, *ys):
        hsv = np.zeros((720, 1280, 3), np.uint8)
        for y in ys:
            head = self.head(y)
            foreground = head[:, :, 2] > 0
            hsv[foreground] = head[foreground]
        return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)

    def context(self):
        return chart_player.gold_tap_following_context(self.chart())

    def detect(self, frame, *, context=None, guard=True, **kwargs):
        return chart_player.first_note_y(frame, self.chart().first, np.zeros_like(frame),
            gold_tap_head_guard=True, gold_tap_followers_guard=guard,
            gold_tap_following_gestures=self.context() if context is None else context, **kwargs)

    def test_two_independent_complete_heads_track_front_only_with_real_sus_context(self):
        chart = self.chart()
        self.assertEqual(chart.first.points[0].lane, 11)
        self.assertEqual([value.start for value in self.context()], [1.25, 1.5, 1.75])
        frame = self.frame(72, 139)
        self.assertIsNone(chart_player.first_note_y(frame, chart.first, gold_tap_head_guard=True))
        self.assertEqual(self.detect(frame), 147.)
        self.assertIsNone(self.detect(frame, context=()))
        self.assertIsNone(self.detect(frame, guard=False))

    def test_context_rejects_wrong_type_time_lane_width_and_nonfinite_values(self):
        chart = self.chart()
        followers = self.context()
        first, later = chart.first, followers[0]
        bad = [(), (None,), (first,), (later, later)]
        for field, value in (("kind", "trace"), ("critical", False), ("flick", 1), ("flick", False)):
            bad.append((replace(later, **{field: value}),))
        for point in (Point(1., 11, 3, 2), Point(.5, 11, 3, 2), Point(2.01, 11, 3, 2),
                      Point(float("nan"), 11, 3, 2), Point(float("inf"), 11, 3, 2),
                      Point(1.5, 10, 3, 2), Point(1.5, 11, 2, 2), Point(1.5, 11, 3, 1),
                      Point(1.5, 11, 3, 2, 1), Point(1.5, True, 3, 2)):
            bad.append((replace(later, points=(point,)),))
        for values in bad:
            with self.subTest(values=values):
                self.assertEqual(chart_player.validated_gold_tap_followers(first, values), ())
                self.assertIsNone(self.detect(self.frame(72, 139), context=values))
        for invalid in (replace(first, critical=False), replace(first, kind="trace"),
                        replace(first, points=(Point(float("nan"), 11, 3, 2),))):
            self.assertEqual(chart_player.validated_gold_tap_followers(invalid, followers), ())

    def test_context_is_bound_to_actual_first_and_rejects_intervening_conflicting_notes(self):
        chart = self.chart()
        conflict = Gesture((Point(1.1, 11, 2, 1),), "tap")
        self.assertEqual(chart_player.gold_tap_following_context(
            replace(chart, gestures=(*chart.gestures, conflict))), ())
        earlier = Gesture((Point(.5, 2, 3, 1),), "tap")
        self.assertEqual(chart_player.gold_tap_following_context(
            replace(chart, gestures=(*chart.gestures, earlier))), ())
        invalid = replace(self.context()[0], points=(Point(float("nan"), 11, 3, 2),))
        self.assertEqual(chart_player.gold_tap_following_context(
            replace(chart, gestures=(*chart.gestures, invalid))), ())

    def test_same_source_double_core_stays_ambiguous_even_when_each_half_is_eligible(self):
        hsv = np.zeros((720, 1280, 3), np.uint8)
        hsv[60:150, 746:754] = (25, 200, 170)
        hsv[150:170, 671:797] = (25, 200, 240)
        hsv[152:168, 673:795] = (25, 40, 250)
        hsv[152:168, 733:735] = (25, 200, 240)
        first = Gesture((Point(1., 10, 4, 2),), "tap", critical=True)
        follower = replace(first, points=(Point(1.5, 10, 4, 2),))
        for left, right in ((673, 733), (735, 795)):
            single = hsv.copy()
            single[152:168, left:right] = (25, 200, 240)
            self.assertEqual(chart_player.first_note_y(cv2.cvtColor(single, cv2.COLOR_HSV2BGR),
                first, gold_tap_head_guard=True), 160.)
        self.assertIsNone(chart_player.first_note_y(cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR),
            first, gold_tap_head_guard=True, gold_tap_followers_guard=True,
            gold_tap_following_gestures=(follower,)))

    def test_near_depth_distinct_sources_and_excess_heads_remain_ambiguous(self):
        rear, front = self.head(130, height=6), self.head(149, height=6)
        for single, expected in ((rear, 133.), (front, 152.)):
            self.assertEqual(self.detect(cv2.cvtColor(single, cv2.COLOR_HSV2BGR)), expected)
        self.assertIsNone(self.detect(cv2.cvtColor(np.maximum(rear, front), cv2.COLOR_HSV2BGR)))
        self.assertIsNone(self.detect(self.frame(72, 139, 220), context=self.context()[:1]))

    def test_half_changed_core_cannot_join_valid_followers(self):
        frame = self.frame(72, 139)
        baseline = np.zeros_like(frame)
        baseline[141:147] = frame[141:147]
        # 不让后继合法芯掩盖首头基线变化不足；原首芯门槛保持，退回单芯结果仍不伪造前沿。
        self.assertNotEqual(chart_player.first_note_y(frame, self.chart().first, baseline,
            gold_tap_head_guard=True, gold_tap_followers_guard=True,
            gold_tap_following_gestures=self.context()), 147.)

    def test_component_enumeration_cannot_change_selected_front_or_passed_head_rejection(self):
        connected = cv2.connectedComponentsWithStats
        for reverse in (False, True):
            def reordered(mask, *args, **kwargs):
                count, labels, stats, centroids = connected(mask, *args, **kwargs)
                if not reverse:
                    return count, labels, stats, centroids
                order = np.array([0, *range(count - 1, 0, -1)])
                mapping = np.empty(count, np.int32)
                mapping[order] = np.arange(count)
                return count, mapping[labels], stats[order], centroids[order]
            with self.subTest(reverse=reverse), patch(
                "project_sekai.chart_player.cv2.connectedComponentsWithStats", side_effect=reordered):
                self.assertEqual(self.detect(self.frame(72, 139)), 147.)
                for frame in (self.frame(139, 530), self.downward_source_frame()):
                    with self.assertRaises(chart_player.FirstHeadOutsideWindow):
                        self.detect(frame, reject_passed_slide=True)
                    self.assertIsNone(self.detect(frame))

    def downward_source_frame(self):
        hsv = cv2.cvtColor(self.frame(139, 510), cv2.COLOR_BGR2HSV)
        xx = np.where(hsv[525, :, 2] > 0)[0]
        hsv[526:600, xx[len(xx) // 2]:xx[len(xx) // 2] + 5] = (25, 200, 240)
        return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)

    def test_guard_is_enabled_by_default_and_frozen_at_initialization(self):
        for initial in (None, "", "0", "1", "true", "other"):
            with self.subTest(initial=initial), patch.dict(os.environ, {}):
                os.environ.pop("MAAPJSK_GOLD_TAP_FOLLOWERS_GUARD", None)
                if initial is not None:
                    os.environ["MAAPJSK_GOLD_TAP_FOLLOWERS_GUARD"] = initial
                anchor = chart_player.StartAnchor(self.chart().first, minimum_samples=4,
                    gold_tap_following_gestures=self.context())
                os.environ["MAAPJSK_GOLD_TAP_FOLLOWERS_GUARD"] = "1" if initial == "0" else "0"
                anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
                anchor.observe(self.frame(72, 139), 10.)
                self.assertEqual(anchor.gold_tap_followers_guard, initial != "0")
                self.assertEqual(anchor.samples, [(10., 147.)] if initial != "0" else [])

    def test_private_song186_middle_frames_replay_with_original_baseline(self):
        # 本机原始截图仅作为可选私有回归输入，不复制或提交资源。
        directory = os.environ.get("MAAPJSK_GOLD_TAP_FOLLOWERS_REPLAY_DIR")
        chart_path = os.environ.get("MAAPJSK_GOLD_TAP_FOLLOWERS_REPLAY_CHART")
        if not directory or not chart_path:
            self.skipTest("未提供私有后继金 TAP 帧及谱面")
        chart = parse_sus(Path(chart_path).read_text(encoding="utf-8"))
        root = Path(directory)
        baseline = cv2.imread(str(root / "MaaPJSK-gold186-failure-0-baseline.png"))
        context = chart_player.gold_tap_following_context(chart)
        self.assertEqual([value.start for value in context], [1.5, 2.])
        for index, expected_y in ((0, 147.), (1, 155.)):
            with self.subTest(index=index):
                frame = cv2.imread(str(root / f"MaaPJSK-gold186-middle-{index}.png"))
                self.assertIsNone(chart_player.first_note_y(frame, chart.first, baseline, gold_tap_head_guard=True))
                self.assertEqual(chart_player.first_note_y(frame, chart.first, baseline,
                    gold_tap_head_guard=True, gold_tap_followers_guard=True,
                    gold_tap_following_gestures=context), expected_y)


if __name__ == "__main__":
    unittest.main()
