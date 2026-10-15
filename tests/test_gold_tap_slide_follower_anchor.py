from dataclasses import replace
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from project_sekai import chart_player
from project_sekai.chart_catalog import ChartRepository
from project_sekai.sus_chart import Chart, Gesture, Point, parse_sus


class GoldTapSlideFollowerAnchorTests(unittest.TestCase):
    first = Gesture((Point(1., 2, 4, 2),), 'tap', critical=True)
    slide = Gesture((Point(1.7, 2, 4, 1), Point(2.4, 8, 4, 2, 3)),
                    'slide', flick=3, critical=True)

    def chart(self, *extra):
        return Chart((self.first, self.slide, *extra), 480, ((0., 120.),))

    def context(self, chart=None):
        return chart_player.gold_tap_slide_following_context(chart or self.chart())

    def head(self, y, height=16):
        hsv = np.zeros((720, 1280, 3), np.uint8)
        center = y + height / 2
        width = round(4 * (1000 / 12) * center / 570)
        x = round(640 + (self.first.points[0].x - 640) * center / 570 - width / 2)
        hsv[y:y + height, x:x + width] = (25, 200, 240)
        hsv[y + 2:y + height - 2, x + 2:x + width - 2] = (25, 40, 250)
        return hsv

    def frame(self, *ys, height=16):
        hsv = np.zeros((720, 1280, 3), np.uint8)
        for y in ys:
            head = self.head(y, height)
            foreground = head[:, :, 2] > 0
            hsv[foreground] = head[foreground]
        return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)

    def detect(self, frame, *, context=None, guard=True, **kwargs):
        return chart_player.first_note_y(frame, self.first, np.zeros_like(frame),
            gold_tap_head_guard=True,
            gold_tap_slide_following_gestures=self.context() if context is None else context,
            gold_tap_slide_follower_guard=guard, **kwargs)

    def test_two_distinct_complete_heads_choose_front_only_with_real_slide_context(self):
        frame = self.frame(72, 139)
        self.assertEqual(self.detect(frame), 147.)
        self.assertIsNone(self.detect(frame, context=()))
        self.assertIsNone(self.detect(frame, guard=False))
        self.assertEqual(chart_player.gold_tap_following_context(self.chart()), ())

    def test_context_accepts_separate_blue_overlap_but_rejects_gold_conflicts(self):
        blue = Gesture((Point(1.3, 5, 3, 1),), 'tap')
        self.assertEqual(self.context(self.chart(blue)), (self.slide,))
        conflicts = (replace(blue, critical=True),
                     Gesture((Point(1.3, 2, 4, 2),), 'tap', critical=True),
                     Gesture((Point(1.3, 2, 4, 6),), 'trace', critical=True),
                     replace(self.slide, points=(Point(1.5, 2, 4, 1), Point(2., 2, 4, 2))))
        for conflict in conflicts:
            with self.subTest(conflict=conflict):
                self.assertEqual(self.context(self.chart(conflict)), ())

    def test_slide_validation_rejects_illegal_types_positions_times_and_start_flick(self):
        bad = [(), (None,), (self.slide, self.slide), (self.first,),
               (replace(self.slide, kind='trace'),), (replace(self.slide, critical=False),),
               (replace(self.slide, flick=True),)]
        for first_point in (Point(1., 2, 4, 1), Point(2.01, 2, 4, 1),
                            Point(1.7, 3, 4, 1), Point(1.7, 2, 3, 1),
                            Point(1.7, 2, 4, 2), Point(1.7, 2, 4, 1, 7),
                            Point(float('nan'), 2, 4, 1)):
            bad.append((replace(self.slide, points=(first_point, self.slide.points[-1])),))
        for last_point in (Point(1.6, 2, 4, 2), Point(2.4, 13, 2, 2),
                           Point(2.4, 8, 4, 1), Point(float('inf'), 8, 4, 2)):
            bad.append((replace(self.slide, points=(self.slide.points[0], last_point)),))
        for values in bad:
            with self.subTest(values=values):
                self.assertEqual(chart_player.validated_gold_tap_slide_follower(self.first, values), ())
                self.assertIsNone(self.detect(self.frame(72, 139), context=values))

    def test_near_depth_and_connected_sources_remain_unknown(self):
        self.assertIsNone(self.detect(self.frame(130, 149, height=6)))
        hsv = np.zeros((720, 1280, 3), np.uint8)
        # 两个完整但同深度的半芯共用外框，不能用谱面上下文替代实际独立来源证明。
        hsv[150:170, 484:609] = (25, 200, 240)
        hsv[152:168, 486:607] = (25, 40, 250)
        hsv[152:168, 546:548] = (25, 200, 240)
        for left, right in ((486, 546), (548, 607)):
            single = hsv.copy()
            single[152:168, left:right] = (25, 200, 240)
            self.assertEqual(self.detect(cv2.cvtColor(single, cv2.COLOR_HSV2BGR)), 160.)
        self.assertIsNone(self.detect(cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)))

    def test_unconfirmed_front_source_blocks_later_valid_slide_head(self):
        hsv = cv2.cvtColor(self.frame(139, 530), cv2.COLOR_BGR2HSV)
        pixels = np.where(hsv[530, :, 2] > 0)[0]
        # 独立单像素只污染首头来源唯一性；不能忽略它并改用后继合法芯。
        hsv[527, pixels[0] + 10] = (25, 200, 240)
        self.assertIsNone(self.detect(cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR), reject_passed_slide=True))
        with self.assertRaises(chart_player.FirstHeadOutsideWindow):
            self.detect(self.frame(139, 530), reject_passed_slide=True)

    def test_default_on_and_initialization_freezes_explicit_zero_rollback(self):
        for initial in (None, '', '0', '1', 'true', 'other'):
            with self.subTest(initial=initial), patch.dict(os.environ, {}):
                os.environ.pop('MAAPJSK_GOLD_TAP_SLIDE_FOLLOWER_GUARD', None)
                if initial is not None:
                    os.environ['MAAPJSK_GOLD_TAP_SLIDE_FOLLOWER_GUARD'] = initial
                anchor = chart_player.StartAnchor(self.first, minimum_samples=4,
                    gold_tap_slide_following_gestures=self.context())
                os.environ['MAAPJSK_GOLD_TAP_SLIDE_FOLLOWER_GUARD'] = '0' if initial != '0' else '1'
                anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
                anchor.observe(self.frame(72, 139), 10.)
                self.assertEqual(anchor.gold_tap_slide_follower_guard, initial != '0')
                self.assertEqual(anchor.samples, [(10., 147.)] if initial != '0' else [])

    def test_head_guard_rollback_keeps_legacy_fitting_without_pair_requirement(self):
        # 原首头主保护关闭时，不得被依赖其双芯证据的新保护再次阻断旧拟合。
        with patch.dict(os.environ, {'MAAPJSK_GOLD_TAP_HEAD_GUARD': '0',
                                     'MAAPJSK_GOLD_TAP_SLIDE_FOLLOWER_GUARD': '1'}):
            anchor = chart_player.StartAnchor(self.first, minimum_samples=4,
                gold_tap_slide_following_gestures=self.context())
        self.assertFalse(anchor.gold_tap_slide_follower_guard)
        frame = np.zeros((720, 1280, 3), np.uint8)
        anchor.observe(frame, 9.)
        epoch = None
        with patch.object(chart_player, 'first_note_y', side_effect=(54., 75., 104.5, 141.5, 184., 243.5)):
            for index in range(6):
                epoch = anchor.observe(frame, 10. + index * .175, capture_seconds=.16)
                if epoch is not None:
                    break
        self.assertIsNotNone(epoch)
        self.assertFalse(anchor.gold_tap_slide_pair_confirmed)

    def test_single_late_slide_without_first_head_proof_cannot_establish_epoch(self):
        with patch.dict(os.environ, {'MAAPJSK_GOLD_TAP_SLIDE_FOLLOWER_GUARD': '1'}):
            anchor = chart_player.StartAnchor(self.first, minimum_samples=4,
                gold_tap_slide_following_gestures=self.context())
        anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
        for index, y in enumerate((52, 73, 102, 139, 182, 241, 324, 421)):
            self.assertIsNone(anchor.observe(self.frame(y), 10. + index * .18, capture_seconds=.15))
        self.assertIsNone(anchor.fit)

    def test_proven_first_trajectory_cannot_continue_with_a_single_later_head(self):
        with patch.dict(os.environ, {'MAAPJSK_GOLD_TAP_SLIDE_FOLLOWER_GUARD': '1'}):
            anchor = chart_player.StartAnchor(self.first, minimum_samples=4,
                gold_tap_slide_following_gestures=self.context())
        anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
        anchor.observe(self.frame(72, 139), 10.)
        self.assertTrue(anchor.gold_tap_slide_pair_confirmed)
        before = list(anchor.samples)
        self.assertIsNone(anchor.observe(self.frame(193), 10.2, capture_seconds=.15))
        self.assertEqual(anchor.samples, before)
        self.assertIsNone(anchor.fit)

    def test_private_song774_replay_preserves_first_identity_and_startup_margin(self):
        # 私有复现帧及本地曲库仅由显式路径启用，不复制到源码或发行资产。
        probe = os.environ.get('MAAPJSK_GOLD_TAP_SLIDE_REPLAY_NPZ')
        chart_root = os.environ.get('MAAPJSK_GOLD_TAP_SLIDE_REPLAY_CHART_ROOT')
        if not probe or not chart_root:
            self.skipTest('未提供私有同轨金色 SLIDE 首头复现帧及曲库')
        chart = parse_sus(ChartRepository(Path(chart_root)).load_chart(774, 'expert'))
        context = self.context(chart)
        self.assertEqual(len(context), 1)
        with np.load(probe, allow_pickle=False) as data:
            frames, captured_at, capture_seconds = data['frames'], data['captured_at'], data['capture_seconds']
        for index, expected in ((8, 54.), (9, 75.), (10, 104.5), (11, 141.5),
                                (12, 184.), (13, 243.5), (14, 326.), (15, 423.5), (16, None)):
            with self.subTest(index=index):
                actual = chart_player.first_note_y(frames[index], chart.first, frames[0],
                    gold_tap_head_guard=True, gold_tap_slide_follower_guard=True,
                    gold_tap_slide_following_gestures=context, reject_passed_slide=True)
                self.assertEqual(actual, expected)
        with self.assertRaises(chart_player.FirstHeadOutsideWindow):
            chart_player.first_note_y(frames[17], chart.first, frames[0], gold_tap_head_guard=True,
                gold_tap_slide_follower_guard=True, gold_tap_slide_following_gestures=context,
                reject_passed_slide=True)
        with patch.dict(os.environ, {'MAAPJSK_GOLD_TAP_SLIDE_FOLLOWER_GUARD': '1'}):
            anchor = chart_player.StartAnchor(chart.first, minimum_samples=4,
                gold_tap_slide_following_gestures=context)
        epoch = None
        for index, frame in enumerate(frames):
            epoch = anchor.observe(frame, float(captured_at[index]), capture_seconds=float(capture_seconds[index]))
            if epoch is not None:
                break
        self.assertIsNotNone(epoch)
        self.assertLessEqual(index, 14)
        self.assertTrue(anchor.fit['gold_tap_slide_follower_guard'])
        self.assertIs(anchor.fit['gold_tap_slide_pair_confirmed'], True)
        self.assertLess(anchor.fit['residual_pixels'], 12)
        first_touch = chart_player.compile_touches(chart)[0].time
        self.assertGreater(epoch + first_touch - .051 - float(captured_at[index]),
                           float(capture_seconds[index]) + .080)


if __name__ == '__main__':
    unittest.main()
