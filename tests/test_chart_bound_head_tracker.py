from dataclasses import replace
import math
import os
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from project_sekai import chart_player as cp
from project_sekai.sus_chart import Chart, Gesture, Point, parse_sus


class ChartBoundHeadTrackerTests(unittest.TestCase):
    first = Gesture((Point(1., 11, 3, 2),), 'tap', critical=True)

    def chart(self, *gestures):
        return Chart(gestures or (self.first,), 480, ((0., 120.),))

    def frame(self, *heads, noise_front=False, connected=False):
        hsv = np.zeros((720, 1280, 3), np.uint8)
        boxes = []
        for gesture, y in heads:
            point = gesture.points[0]
            height = 12
            center = y + height / 2
            width = round(point.width * (1000 / 12) * center / 570)
            x = round(640 + (point.x - 640) * center / 570 - width / 2)
            hsv[y:y + height, x:x + width] = (25, 200, 240)
            hsv[y + 2:y + height - 2, x + 2:x + width - 2] = (25, 40, 250)
            boxes.append((x, y, width, height))
        if noise_front:
            x, y, width, height = max(boxes, key=lambda box: box[1])
            hsv[y - 2, x + width // 2] = (25, 200, 240)
        if connected and len(boxes) > 1:
            spine = min(box[0] for box in boxes) - 4
            for x, y, width, height in boxes:
                hsv[40:y + 2, spine:spine + 2] = (25, 200, 240)
                hsv[y:y + 2, spine:x + 2] = (25, 200, 240)
        return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)

    def anchor(self, chart=None, *, enabled=True, first=None):
        chart = chart or self.chart()
        with patch.dict(os.environ, {'MAAPJSK_CHART_BOUND_HEAD_GUARD': '1' if enabled else '0'}):
            return cp.StartAnchor(first or chart.first, minimum_samples=4, gold_tap_context_chart=chart,
                                  simultaneous_gestures=cp.first_note_context(chart))

    def test_unknown_foremost_source_blocks_rear_without_replacing_motion_sample(self):
        anchor = self.anchor()
        anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
        anchor.observe(self.frame((self.first, 70)), 10.)
        before = list(anchor.samples)
        anchor.observe(self.frame((self.first, 70), (self.first, 139), noise_front=True), 10.1)
        self.assertEqual(anchor.samples, before)

    def test_four_adjacent_simultaneous_gold_heads_can_establish_true_upper_group(self):
        heads = tuple(Gesture((Point(1., lane, 3, 2),), 'tap', critical=True) for lane in (2, 5, 8, 11))
        anchor = self.anchor(self.chart(*heads))
        anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
        for time, y in ((10., 60), (10.06, 68), (10.12, 77)):
            anchor.observe(self.frame(*((head, y) for head in heads)), time)
        self.assertEqual(len(anchor.samples), 3)
        self.assertGreater(anchor.samples[-1][1], anchor.samples[0][1])

    def test_group_requires_actual_pixels_for_every_member(self):
        heads = tuple(Gesture((Point(1., lane, 3, 2),), 'tap', critical=True) for lane in (2, 5, 8, 11))
        for y in range(44, 117, 4):
            for omitted in range(-1, 4):
                present = heads if omitted < 0 else heads[:omitted] + heads[omitted + 1:]
                with self.subTest(y=y, omitted=omitted):
                    anchor = self.anchor(self.chart(*heads))
                    anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
                    anchor.observe(self.frame(*((head, y) for head in present)), 10.)
                    self.assertEqual(bool(anchor.samples), omitted < 0)

    def test_independent_blue_simultaneous_head_does_not_supply_gold_pixels(self):
        blue = Gesture((Point(1., 2, 4, 1),), 'tap')
        gold = Gesture((Point(1., 10, 4, 2),), 'tap', critical=True)
        chart = self.chart(gold, blue)
        self.assertEqual(cp.chart_bound_head_groups(chart, gold), ((gold,),))
        anchor = self.anchor(chart)
        anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
        anchor.observe(self.frame((gold, 60)), 10.)
        self.assertEqual(len(anchor.samples), 1)
        overlapping = replace(blue, points=(Point(1., 9, 4, 1),))
        self.assertEqual(cp.chart_bound_head_groups(self.chart(gold, overlapping), gold), ())

    def test_single_real_core_cannot_supply_two_unequal_members(self):
        wide = Gesture((Point(1., 5, 6, 2),), 'tap', critical=True)
        narrow = Gesture((Point(1., 11, 3, 2),), 'tap', critical=True)
        for present in ((wide,), (narrow,)):
            with self.subTest(present=present):
                anchor = self.anchor(self.chart(wide, narrow))
                anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
                anchor.observe(self.frame(*((head, 60) for head in present)), 10.)
                self.assertEqual(anchor.samples, [])

    def test_missing_outer_side_and_extra_unknown_head_cannot_seed(self):
        pair = tuple(Gesture((Point(1., lane, 3, 2),), 'tap', critical=True) for lane in (2, 5))
        extra = Gesture((Point(1., 8, 3, 2),), 'tap', critical=True)
        complete = self.frame(*((head, 60) for head in pair))
        hsv = cv2.cvtColor(complete, cv2.COLOR_BGR2HSV)
        gold = cv2.inRange(hsv, (15, 55, 135), (45, 255, 255))
        left = np.where(gold != 0)[1].min()
        missing_side = complete.copy()
        missing_side[62:70, left:left + 2] = 0
        distant = Gesture((Point(1., 11, 3, 2),), 'tap', critical=True)
        for frame in (missing_side, self.frame(*((head, 60) for head in (*pair, extra))),
                      self.frame(*((head, 60) for head in (*pair, distant)))):
            anchor = self.anchor(self.chart(*pair))
            anchor.observe(np.zeros_like(frame), 9.)
            anchor.observe(frame, 10.)
            self.assertEqual(anchor.samples, [])

    def test_late_unseeded_head_and_long_gap_never_seed_or_reseed(self):
        anchor = self.anchor()
        anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
        with self.assertRaisesRegex(RuntimeError, 'unseeded_late_head'):
            anchor.observe(self.frame((self.first, 300)), 10.)
        with self.assertRaisesRegex(RuntimeError, 'unseeded_late_head'):
            anchor.observe(self.frame((self.first, 70)), 10.1)
        self.assertEqual(anchor.samples, [])
        other = self.anchor()
        other.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
        other.observe(self.frame((self.first, 70)), 10.)
        before = list(other.samples)
        with self.assertRaisesRegex(RuntimeError, 'continuity_lost'):
            other.observe(self.frame((self.first, 139)), 11.)
        with self.assertRaisesRegex(RuntimeError, 'continuity_lost'):
            other.observe(self.frame((self.first, 150)), 11.1)
        self.assertEqual(other.samples, before)
        self.assertEqual(other.failure_reason, 'chart_bound_head_continuity_lost')

    def test_unknown_late_complete_head_cannot_be_forgotten_before_upper_follower(self):
        anchor = self.anchor()
        anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
        with self.assertRaisesRegex(RuntimeError, 'unseeded_late_unknown_head'):
            anchor.observe(self.frame((self.first, 300), noise_front=True), 10.)
        with self.assertRaisesRegex(RuntimeError, 'unseeded_late_unknown_head'):
            anchor.observe(self.frame((self.first, 70)), 10.1)
        self.assertEqual(anchor.samples, [])

    def test_later_identical_head_cannot_take_over_after_seed_or_missing_frame(self):
        follower = Gesture((Point(1.1, 11, 3, 2),), 'tap', critical=True)
        for cadence, follower_delay, missing_frame in ((.06, .1, False), (.06, .1, True),
                                                       (.1, .06, False), (.1, .06, True),
                                                       (.08, .06, False), (.08, .06, True)):
            with self.subTest(cadence=cadence, follower_delay=follower_delay, missing_frame=missing_frame):
                anchor = self.anchor(self.chart(self.first, follower))
                blank = np.zeros((720, 1280, 3), np.uint8)
                anchor.observe(blank, 9.)
                accepted = None
                for index in range(20):
                    captured_at = 10. + index * cadence
                    delay = 0. if index < 3 else follower_delay
                    cy = 96 * math.exp(2 * (captured_at - 10. - delay)) - 30
                    y = round(cy - 6)
                    if y > 525:
                        break
                    frame = blank if missing_frame and index == 3 else self.frame((self.first if index < 3 else follower, y))
                    try:
                        accepted = anchor.observe(frame, captured_at, capture_seconds=.01)
                    except RuntimeError:
                        break
                    if accepted is not None:
                        break
                self.assertIsNone(accepted)
                self.assertLessEqual(len(anchor.samples), 3)

    def test_unknown_frame_preserves_prefix_until_same_identity_is_proved(self):
        for seed_samples in (1, 2):
            anchor = self.anchor()
            blank = np.zeros((720, 1280, 3), np.uint8)
            anchor.observe(blank, 9.)
            for index in range(seed_samples):
                y = round(96 * math.exp(2 * index * .06) - 36)
                anchor.observe(self.frame((self.first, y)), 10. + index * .06)
            before = list(anchor.samples)
            anchor.observe(blank, 10. + seed_samples * .06)
            self.assertEqual(anchor.samples, before)
            y = round(96 * math.exp(2 * (seed_samples + 1) * .06) - 36)
            anchor.observe(self.frame((self.first, y)), 10. + (seed_samples + 1) * .06)
            self.assertEqual(len(anchor.samples), seed_samples + 1)

    def test_complete_weakly_changed_front_blocks_rear_without_becoming_a_sample(self):
        weak = self.frame((self.first, 88), (self.first, 50))
        hsv = cv2.cvtColor(weak, cv2.COLOR_BGR2HSV)
        _, _, stats, _ = cv2.connectedComponentsWithStats(cv2.inRange(hsv, (15, 0, 235), (45, 110, 255)), 8)
        x, y, width, height, _ = max(stats[1:], key=lambda value: value[1])
        baseline = np.zeros_like(weak)
        baseline[y:y + height, x:x + round(width * .6)] = weak[y:y + height, x:x + round(width * .6)]
        anchor = self.anchor()
        anchor.observe(baseline, 9.)
        for captured_at, head_y in ((10., 60), (10.06, 68), (10.12, 77)):
            anchor.observe(self.frame((self.first, head_y)), captured_at)
        before = list(anchor.samples)
        anchor.observe(weak, 10.18)
        self.assertEqual(anchor.samples, before)
        self.assertEqual(anchor.chart_bound_tracker.status, 'unknown_front')
        anchor.observe(self.frame((self.first, 99)), 10.24)
        self.assertEqual(len(anchor.samples), len(before) + 1)

    def test_real_sus_slide_easing_direction_is_valid_for_first_and_future_head(self):
        source = '#BPM01:120\n#00008:01\n#000380:14\n#0013b0:23\n#00018:24\n#00058:64\n'
        chart = parse_sus(source)
        self.assertEqual(chart.first.points[0].direction, 6)
        self.assertTrue(cp.chart_bound_head_groups(chart, chart.first))
        anchor = self.anchor(chart)
        anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
        anchor.observe(self.frame((chart.first, 60)), 10.)
        self.assertEqual(len(anchor.samples), 1)
        future = parse_sus('#BPM01:120\n#00008:01\n#000380:00140000\n#0013b0:23\n'
                           '#00018:00240000\n#00058:00640000\n#0001b:23\n')
        self.assertEqual(future.first.kind, 'tap')
        self.assertIsNotNone(cp.chart_bound_following_geometry(future, future.first))

    def test_early_single_seed_does_not_bypass_chart_following_identity_ambiguity(self):
        for cadence in (.06, .1, .16):
            follower = Gesture((Point(1.04, 11, 3, 2),), 'tap', critical=True)
            anchor = self.anchor(self.chart(self.first, follower))
            anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
            anchor.observe(self.frame((self.first, 60)), 10.)
            before = list(anchor.samples)
            for elapsed in (cadence, .2, .4):
                y = round(96 * math.exp(2 * (elapsed - .04)) - 36)
                anchor.observe(self.frame((follower, y)), 10. + elapsed)
                self.assertEqual(anchor.samples, before)
                self.assertEqual(anchor.chart_bound_tracker.motion, before)
                self.assertFalse(anchor.chart_bound_tracker.lost)
                self.assertEqual(anchor.chart_bound_tracker.status, 'motion_identity_ambiguous')
            with self.assertRaisesRegex(RuntimeError, 'continuity_lost'):
                anchor.observe(self.frame((follower, y)), 10.51)
            self.assertEqual(len(anchor.samples), 1)

    def test_geometric_ambiguity_can_clear_without_changing_confirmed_prefix(self):
        follower = Gesture((Point(1.02, 8, 3, 2),), 'tap', critical=True)
        anchor = self.anchor(self.chart(self.first, follower))
        anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
        anchor.observe(self.frame((self.first, 42)), 10.)
        before = list(anchor.samples)
        anchor.observe(self.frame((self.first, round(78 * math.exp(.08) - 36))), 10.04)
        self.assertEqual(anchor.samples, before)
        self.assertEqual(anchor.chart_bound_tracker.status, 'motion_identity_ambiguous')
        self.assertFalse(anchor.chart_bound_tracker.lost)
        anchor.observe(self.frame((self.first, round(78 * math.exp(.24) - 36))), 10.12)
        self.assertEqual(anchor.samples[:1], before)
        self.assertEqual(len(anchor.samples), 2)

    def test_true_first_and_complete_visible_follower_keep_the_original_trajectory(self):
        for delay in (.1, .3):
            follower = Gesture((Point(1. + delay, 11, 3, 2),), 'tap', critical=True)
            anchor = self.anchor(self.chart(self.first, follower))
            anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
            accepted = None
            for index in range(20):
                captured_at = 10. + index * .06
                y = round(96 * math.exp(2 * (captured_at - 10.)) - 36)
                later_y = round(96 * math.exp(2 * (captured_at - 10. - delay)) - 36)
                heads = [(self.first, y)]
                if later_y > 40:
                    heads.append((follower, later_y))
                accepted = anchor.observe(self.frame(*heads), captured_at, capture_seconds=.01)
                if accepted is not None:
                    break
            self.assertIsNotNone(accepted)
            self.assertLess(abs(anchor.fit['first_hit'] - (10. + math.log(600 / 96) / 2)), .02)

    def test_following_aliases_include_slide_and_each_actual_first_group_member(self):
        left = Gesture((Point(1., 2, 3, 2),), 'tap', critical=True)
        slide = Gesture((Point(1.2, 2, 3, 1), Point(1.8, 2, 3, 2, 3)), 'slide', 3, True)
        chart = self.chart(self.first, left, slide)
        anchor = self.anchor(chart)
        self.assertEqual(anchor.chart_bound_tracker.following_offsets, (slide.start - self.first.start,))

    def test_competing_width_lane_and_gold_trace_cannot_take_over(self):
        for follower in (Gesture((Point(1.04, 10, 4, 2),), 'tap', critical=True),
                         Gesture((Point(1.04, 11, 3, 6),), 'trace', critical=True)):
            with self.subTest(follower=follower):
                anchor = self.anchor(self.chart(self.first, follower))
                anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
                epoch = None
                for index in range(20):
                    captured_at = 10. + index * .1
                    y = round(96 * math.exp(2 * (captured_at - 10. - (0 if index < 1 else .04))) - 36)
                    if y > 525:
                        break
                    try:
                        epoch = anchor.observe(self.frame((self.first if index < 1 else follower, y)), captured_at, capture_seconds=.01)
                    except RuntimeError:
                        break
                    if epoch is not None:
                        break
                self.assertIsNone(epoch)

    def test_future_geometry_keeps_spatial_filter_and_actual_connected_spans(self):
        far = Gesture((Point(1.04, 2, 3, 2),), 'tap', critical=True)
        anchor = self.anchor(self.chart(self.first, far))
        anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
        epoch = None
        for index in range(20):
            captured_at = 10. + index * .06
            y = round(96 * math.exp(2 * (captured_at - 10.)) - 36)
            epoch = anchor.observe(self.frame((self.first, y)), captured_at, capture_seconds=.01)
            if epoch is not None:
                break
        self.assertIsNotNone(epoch)
        first = Gesture((Point(1., 8, 6, 2),), 'tap', critical=True)
        followers = tuple(Gesture((Point(1.1, lane, 3, 2),), 'tap', critical=True) for lane in (8, 11))
        geometry = cp.chart_bound_following_geometry(self.chart(first, *followers), first)
        self.assertIn((followers[0].start - first.start, 8, 6), geometry)
        cy = 240.
        width = round(6 * (1000 / 12) * cy / 570)
        x = round(640 + (first.points[0].x - 640) * cy / 570 - width / 2)
        self.assertTrue(cp.chart_bound_head_geometry(x, cy - 6, width, 12, 8, 6))
        self.assertFalse(any(cp.chart_bound_head_geometry(x, cy - 6, width, 12, lane, 3) for lane in (8, 11)))

    def test_invalid_competing_gold_metadata_cannot_be_treated_as_no_followers(self):
        follower = Gesture((Point(1.04, 11, 3, 2),), 'tap', critical=True)
        for invalid in (replace(follower, critical=1), replace(follower, flick=True),
                        replace(follower, points=(Point(1.04, True, 3, 2),))):
            anchor = self.anchor(self.chart(self.first, invalid))
            anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
            with self.assertRaisesRegex(RuntimeError, 'invalid_following_context'):
                anchor.observe(self.frame((self.first, 60)), 10.)

    def test_wrong_chart_identity_and_invalid_metadata_fail_closed(self):
        chart = self.chart()
        wrong = replace(self.first)
        anchor = self.anchor(chart, first=wrong)
        anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
        anchor.observe(self.frame((self.first, 70)), 10.)
        self.assertEqual(anchor.samples, [])

    def test_same_source_different_depth_cores_cannot_seed(self):
        first = Gesture((Point(1., 5, 6, 2),), 'tap', critical=True)
        hsv = np.zeros((720, 1280, 3), np.uint8)
        boxes = []
        for y, height in ((80, 24), (107, 6)):
            cy = y + height / 2
            width = round(first.points[0].width * (1000 / 12) * cy / 570)
            x = round(640 - width / 2)
            hsv[y:y + height, x:x + width] = (25, 200, 240)
            hsv[y + 2:y + height - 2, x + 2:x + width - 2] = (25, 40, 250)
            boxes.append((x, y, width))
        spine = boxes[1][0] - 3
        hsv[80:109, spine:spine + 2] = (25, 200, 240)
        for x, y, width in boxes:
            hsv[y:y + 2, spine:x + 2] = (25, 200, 240)
        anchor = self.anchor(self.chart(first))
        anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
        anchor.observe(cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR), 10.)
        self.assertEqual(anchor.samples, [])

    def test_first_groups_support_disjoint_and_slide_tail_flick_without_bridging_gap(self):
        disjoint = tuple(Gesture((Point(1., lane, 4, 2),), 'tap', critical=True) for lane in (3, 9))
        for heads in (disjoint,
                      (Gesture((Point(1., 2, 6, 1), Point(1.5, 2, 6, 2, 3)), 'slide', 3, True),
                       Gesture((Point(1., 8, 6, 2),), 'tap', 3, True)),
                      tuple(Gesture((Point(1., lane, 3, 1), Point(1.001 if lane < 11 else 2., lane, 3, 2)),
                                    'slide', critical=True) for lane in (2, 5, 8, 11))):
            with self.subTest(heads=heads):
                chart = self.chart(*heads)
                self.assertTrue(cp.chart_bound_head_groups(chart, chart.first))
                anchor = self.anchor(chart)
                anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
                for time, y in ((10., 60), (10.06, 68), (10.12, 77)):
                    anchor.observe(self.frame(*((head, y) for head in heads)), time)
                self.assertEqual(len(anchor.samples), 3)
        anchor = self.anchor(self.chart(*disjoint))
        anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
        anchor.observe(self.frame(*((head, 60) for head in disjoint), connected=True), 10.)
        self.assertEqual(anchor.samples, [])

    def test_invalid_first_metadata_static_frame_and_near_depth_are_not_identity(self):
        for value in (replace(self.first, flick=True), replace(self.first, critical=1),
                      replace(self.first, points=(Point(float('nan'), 11, 3, 2),)),
                      replace(self.first, points=(Point(1., True, 3, 2),)),
                      replace(self.first, points=(Point(1., 11, 3, 2, True),))):
            self.assertEqual(cp.chart_bound_head_groups(self.chart(value), value), ())
        frame = self.frame((self.first, 70))
        anchor = self.anchor()
        anchor.observe(frame, 9.)
        anchor.observe(frame, 10.)
        self.assertEqual(anchor.samples, [])
        anchor = self.anchor()
        anchor.observe(np.zeros_like(frame), 9.)
        anchor.observe(self.frame((self.first, 60), (self.first, 76)), 10.)
        self.assertEqual(anchor.samples, [])

    def test_candidate_is_explicit_and_frozen_and_profile_environment_is_separate(self):
        from types import SimpleNamespace
        from project_sekai.calibration import environment_signature, profile_key
        for initial in (None, '', '0', '1', 'true'):
            with self.subTest(initial=initial), patch.dict(os.environ, {}):
                os.environ.pop('MAAPJSK_CHART_BOUND_HEAD_GUARD', None)
                if initial is not None:
                    os.environ['MAAPJSK_CHART_BOUND_HEAD_GUARD'] = initial
                anchor = cp.StartAnchor(self.first, minimum_samples=4, gold_tap_context_chart=self.chart())
                os.environ['MAAPJSK_CHART_BOUND_HEAD_GUARD'] = '0' if initial == '1' else '1'
                self.assertEqual(anchor.chart_bound_head_guard, initial == '1')
        class Device:
            controller = SimpleNamespace(info={'adb_serial': 'offline-test-device'})
            def preflight(self):
                pass
            def shell(self, command):
                return 'versionName=offline-test-version'
        environments = []
        for flag in ('0', '1'):
            with patch.dict(os.environ, {'MAAPJSK_CHART_BOUND_HEAD_GUARD': flag}), patch(
                    'project_sekai.calibration.native_engine.module', return_value=SimpleNamespace(version=lambda: '1.2.0')):
                environments.append(environment_signature(Device()))
        self.assertNotEqual(profile_key(environments[0], 'expert'), profile_key(environments[1], 'expert'))
        self.assertFalse(environments[0]['chart_bound_head_guard'])
        self.assertTrue(environments[1]['chart_bound_head_guard'])


if __name__ == '__main__':
    unittest.main()
