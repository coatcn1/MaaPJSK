from dataclasses import replace
import math
import os
import hashlib
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np

from project_sekai.chart_player import StartAnchor
from project_sekai.sus_chart import Chart, Gesture, Point, parse_sus
from project_sekai.opening_head import OpeningEvidence, TracePhaseContext, trace_head_candidates


FLAG = 'MAAPJSK_TRACE_OPENING_HEAD_GUARD'


def trace_frame(heads, *, connected=False, triangle=True):
    hsv = np.zeros((720, 1280, 3), np.uint8)
    boxes = []
    for gesture, center in heads:
        point = gesture.points[0]
        scale = (center + 30) / 600
        width = max(16, round(point.width * 1000 / 12 * scale))
        left = round(640 + (point.x - 640) * scale - width / 2)
        height = max(6, round(width * .2))
        top = round(center - height / 2)
        bottom = top + height
        # 金条有抗锯齿与亮度层次，不能用均匀V250矩形替代真实浅亮横条。
        levels = (211, 231, 248, 234, 241, 223)
        for row in range(top + 1, bottom):
            inset = 1 if row in (top + 1, bottom - 1) else 0
            hsv[row, left + inset:left + width - inset] = (25, 155, levels[(row - top - 1) % len(levels)])
        if triangle:
            half = max(3, round(width * .16))
            tip = top
            cx = left + width // 2
            cv2.fillConvexPoly(hsv, np.array([(cx, tip), (cx - half, bottom - 2),
                                           (cx + half, bottom - 2)]), (0, 0, 255))
            # 斜面仍有金色浅白，中央中性白Hue=0与真实像素一致；不得依赖虚构的金Hue白三角。
            for row in range(top + 1, bottom - 1):
                hsv[row, cx + 1:cx + max(2, round(half * (row - top) / max(1, bottom - 2 - top)))] = (25, 40, 250)
        boxes.append((left, bottom, width))
    if connected and boxes:
        front = max(boxes, key=lambda value: value[1])
        body = np.zeros(hsv.shape[:2], np.uint8)
        for left, bottom, width in boxes:
            cv2.line(body, (left + width // 2, bottom - 5),
                     (front[0] + front[2] // 2, front[1] - 5), 255, 3)
        hsv[(body != 0) & (hsv[:, :, 2] == 0)] = (25, 150, 180)
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)


class TraceOpeningHeadTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {'MAAPJSK_GOLD_TAP_HEAD_GUARD': '1'})
        environment.start()
        self.addCleanup(environment.stop)

    def chart(self):
        # 使用正式 SUS 解析器建立 TRACE 起头与偏轨密集后继，不按曲目编号制造上下文。
        return parse_sus('#BPM01:120\n#00008:01\n#00017:0000620000000000\n'
                         '#00019:0000006200000000\n#0001b:0000000062000000\n')

    def anchor(self, chart=None, enabled='1'):
        chart = chart or self.chart()
        with patch.dict(os.environ, {FLAG: enabled, 'MAAPJSK_CHART_BOUND_HEAD_GUARD': '0'}):
            return StartAnchor(chart.first, minimum_samples=4, gold_tap_context_chart=chart)

    def unique_constellation_chart(self):
        first = Gesture((Point(1., 7, 2, 6),), 'trace', critical=True)
        prefix = (first, *(replace(first, points=(Point(1 + delta, lane, 2, 6),))
                          for delta, lane in ((.0457317, 9), (.0914634, 11))))
        later = tuple(replace(head, points=(replace(head.points[0], time=head.start + 2., lane=lane),))
                      for head, lane in zip(prefix, (7, 5, 3)))
        return Chart((*prefix, *later), 480, ((0., 120.),))

    def constellation_frame(self, chart, y):
        return trace_frame([(head, (y + 30) * math.exp(-2 * (head.start - chart.first.start)) - 30)
                            for head in chart.gestures[:3]], connected=True)

    def test_per_frame_constellation_separates_identity_from_motion_and_fits_epoch(self):
        chart = self.unique_constellation_chart()
        anchor = self.anchor(chart)
        anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
        epoch = None
        for index in range(40):
            time = index * .03
            y = 90 * math.exp(2 * time) - 30
            epoch = anchor.observe(self.constellation_frame(chart, y), 10 + time)
            if epoch is not None:
                break
        self.assertTrue(anchor.chart_bound_tracker.trace_identity_requires_constellation)
        self.assertEqual(anchor.observation['phase_identity'], 'unique_first_constellation')
        self.assertEqual(anchor.observation['identity_contract'], 'per_frame_constellation')
        self.assertGreater(anchor.samples[-1][1], 230)
        self.assertIsNotNone(epoch)
        self.assertGreater(epoch + chart.first.start - (10 + time), .08)

    def test_real_quantized_trajectory_with_positive_identity_ignores_constant_speed(self):
        # 时间及深度来自失败报告，公开回归只保留相对时刻，不携带本机帧或路径。
        samples = [(0.0000000, 53.5),
                   (0.0462304, 59.5),
                   (0.0916894, 65.5),
                   (0.1240985, 70.0),
                   (0.1711786, 74.5),
                   (0.2162667, 82.0),
                   (0.2481555, 87.0),
                   (0.2809319, 92.5),
                   (0.3119595, 98.0),
                   (0.3579693, 107.0),
                   (0.3885928, 110.0),
                   (0.4193538, 116.5),
                   (0.4837057, 128.5),
                   (0.5292014, 136.5),
                   (0.5600734, 142.5),
                   (0.5924980, 148.0),
                   (0.6238922, 154.0),
                   (0.6698787, 160.0),
                   (0.7029940, 166.5),
                   (0.7492474, 176.5),
                   (0.7966666, 187.0),
                   (0.8429994, 198.5),
                   (0.8897739, 210.5),
                   (0.9367839, 218.5)]
        chart = self.unique_constellation_chart()
        tracker = self.anchor(chart).chart_bound_tracker
        blank = np.zeros((720, 1280, 3), np.uint8)
        tracker.measure(self.constellation_frame(chart, 80), blank)
        # 此单元隔离身份合同；真实像素及实际谱面另由私有连续帧完整链路验证。
        for when, y in samples:
            with patch.object(tracker, 'measure', return_value=(y, [])), \
                    patch.object(tracker.trace_phase_context, 'unique', return_value=(True, 'unique_first_constellation')):
                self.assertEqual(tracker.observe(blank, blank, when), y)
        self.assertEqual(len(tracker.motion), 24)
        self.assertTrue(tracker.trace_identity_requires_constellation)
        self.assertFalse(tracker.lost)
        self.assertEqual(tracker.measurement['phase_identity'], 'unique_first_constellation')

    def test_per_frame_identity_unknown_never_falls_back_or_renews_continuity(self):
        chart = self.unique_constellation_chart()
        for reason in ('constellation_incomplete', 'later_constellation_compatible',
                       'first_constellation_inconsistent', 'constellation_observation_limit'):
            with self.subTest(reason=reason):
                anchor = self.anchor(chart)
                blank = np.zeros((720, 1280, 3), np.uint8)
                anchor.observe(blank, 9.)
                anchor.observe(self.constellation_frame(chart, 80), 10.)
                tracker = anchor.chart_bound_tracker
                before = list(tracker.motion)
                with patch.object(tracker.trace_phase_context, 'unique', return_value=(False, reason)):
                    self.assertIsNone(anchor.observe(self.constellation_frame(chart, 90), 10.05))
                    self.assertEqual(tracker.motion, before)
                    self.assertEqual(tracker.status, 'unknown_constellation_identity')
                    self.assertTrue(tracker.trace_identity_requires_constellation)
                    with self.assertRaisesRegex(RuntimeError, 'continuity_lost'):
                        anchor.observe(self.constellation_frame(chart, 100), 10.51)
                self.assertEqual(tracker.motion, before)

    def test_per_frame_proof_recovers_without_reseeding_but_reverse_still_loses(self):
        chart = self.unique_constellation_chart()
        anchor = self.anchor(chart)
        blank = np.zeros((720, 1280, 3), np.uint8)
        anchor.observe(blank, 9.)
        anchor.observe(self.constellation_frame(chart, 80), 10.)
        before = list(anchor.samples)
        anchor.observe(trace_frame([(chart.first, 90)]), 10.05)
        self.assertEqual(anchor.samples, before)
        anchor.observe(self.constellation_frame(chart, 100), 10.1)
        self.assertEqual(anchor.samples[:1], before)
        self.assertEqual(len(anchor.samples), 2)
        self.assertTrue(anchor.chart_bound_tracker.trace_identity_requires_constellation)
        with self.assertRaisesRegex(RuntimeError, 'motion_identity_lost'):
            anchor.observe(self.constellation_frame(chart, 85), 10.15)

    def test_per_frame_identity_missing_first_never_accepts_rear_constellation(self):
        chart = self.unique_constellation_chart()
        anchor = self.anchor(chart)
        anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
        anchor.observe(self.constellation_frame(chart, 80), 10.)
        before = list(anchor.samples)
        frame = trace_frame([(head, (100 + 30) * math.exp(-2 * (head.start - chart.first.start)) - 30)
                             for head in chart.gestures[1:3]], connected=True)
        anchor.observe(frame, 10.05)
        self.assertEqual(anchor.samples, before)
        self.assertTrue(anchor.chart_bound_tracker.trace_identity_requires_constellation)

    def test_per_frame_identity_bars_without_complete_markers_do_not_advance(self):
        chart = self.unique_constellation_chart()
        anchor = self.anchor(chart)
        anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
        anchor.observe(self.constellation_frame(chart, 80), 10.)
        before = list(anchor.samples)
        bars = trace_frame([(head, (100 + 30) * math.exp(-2 * (head.start - chart.first.start)) - 30)
                            for head in chart.gestures[:3]], connected=True, triangle=False)
        anchor.observe(bars, 10.05)
        self.assertEqual(anchor.samples, before)
        self.assertTrue(anchor.chart_bound_tracker.trace_identity_requires_constellation)

    def test_actual_start_anchor_samples_trace_marker_from_connected_source(self):
        chart = self.chart()
        self.assertEqual(chart.first.kind, 'trace')
        anchor = self.anchor(chart)
        anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
        for time, center in ((10., 80), (10.05, 92), (10.1, 106)):
            frame = trace_frame([(chart.first, center), (chart.gestures[1], center - 25)], connected=True)
            anchor.observe(frame, time)
        self.assertEqual(len(anchor.samples), 3)

    def test_guard_explicit_zero_invalid_values_and_initialization_frozen(self):
        for enabled in ('0', '', 'true', '1'):
            with self.subTest(enabled=enabled):
                anchor = self.anchor(enabled=enabled)
                with patch.dict(os.environ, {FLAG: '0' if enabled == '1' else '1'}):
                    self.assertEqual(anchor.trace_opening_head_guard, enabled == '1')

    def test_default_matches_explicit_one_and_master_guard_wins(self):
        with patch.dict(os.environ, {'MAAPJSK_CHART_BOUND_HEAD_GUARD': '0'}):
            os.environ.pop(FLAG, None)
            chart = self.chart()
            default = StartAnchor(chart.first, minimum_samples=4, gold_tap_context_chart=chart)
            explicit = self.anchor(chart)
            self.assertTrue(default.trace_opening_head_guard)
            self.assertTrue(default.chart_bound_tracker.trace_heads)
            self.assertEqual(default.trace_opening_head_guard, explicit.trace_opening_head_guard)
            frame = trace_frame([(chart.first, 80)])
            blank = np.zeros_like(frame)
            self.assertEqual(default.chart_bound_tracker.measure(frame, blank),
                             explicit.chart_bound_tracker.measure(frame, blank))
            with patch.dict(os.environ, {'MAAPJSK_GOLD_TAP_HEAD_GUARD': '0'}):
                disabled = StartAnchor(chart.first, gold_tap_context_chart=chart)
                self.assertFalse(disabled.trace_opening_head_guard)
                self.assertTrue(default.trace_opening_head_guard)

    def test_profile_environment_isolates_trace_candidate_from_explicit_zero(self):
        from project_sekai.calibration import environment_signature, profile_key
        device = SimpleNamespace(preflight=lambda: None,
                                 controller=SimpleNamespace(info={'adb_serial': 'offline-test'}),
                                 shell=lambda _: 'versionName=offline-test')
        environments = []
        for enabled in ('0', '1', None):
            with patch.dict(os.environ, {'MAAPJSK_GOLD_TAP_HEAD_GUARD': '1'}), \
                    patch('project_sekai.calibration.native_engine.module', return_value=SimpleNamespace(version=lambda: '1.2.0')):
                if enabled is None:
                    os.environ.pop(FLAG, None)
                else:
                    os.environ[FLAG] = enabled
                environments.append(environment_signature(device))
        self.assertFalse(environments[0]['trace_opening_head_guard'])
        self.assertTrue(environments[1]['trace_opening_head_guard'])
        self.assertEqual(environments[1], environments[2])
        self.assertNotEqual(profile_key(environments[0], 'master'), profile_key(environments[1], 'master'))

    def test_same_shape_repeated_trace_without_first_identity_never_seeds(self):
        first = Gesture((Point(1., 7, 2, 6),), 'trace', critical=True)
        follower = replace(first, points=(Point(1.05, 7, 2, 6),))
        anchor = self.anchor(Chart((first, follower), 480, ((0., 120.),)))
        anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
        for index in range(6):
            try:
                epoch = anchor.observe(trace_frame([(follower, 65 + index * 12)]), 10 + index * .1)
                self.assertIsNone(epoch)
            except RuntimeError:
                break
        self.assertEqual(anchor.samples, [])

    def test_whole_chart_alias_beyond_one_second_is_not_a_new_first(self):
        first = self.chart().first
        later = replace(first, points=(replace(first.points[0], time=first.start + 1.5),))
        anchor = self.anchor(Chart((first, later), 480, ((0., 120.),)))
        anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
        for index in range(8):
            try:
                anchor.observe(trace_frame([(later, 110 * math.exp(3 * index * .05) - 30)]), 10 + index * .05)
            except RuntimeError:
                break
        self.assertEqual(anchor.samples, [])

    def test_unique_positive_trace_constellation_vs_copied_prefix(self):
        first = Gesture((Point(1., 7, 2, 6),), 'trace', critical=True)
        prefix = (first, *(replace(first, points=(Point(1 + delta, lane, 2, 6),))
                           for delta, lane in ((.0457317, 9), (.0914634, 11))))
        for copied in (False, True):
            later = tuple(replace(head, points=(replace(head.points[0], time=head.start + 2.),)) for head in prefix)
            if not copied:
                later = tuple(replace(head, points=(replace(head.points[0], lane=lane),))
                              for head, lane in zip(later, (7, 5, 3)))
            anchor = self.anchor(Chart((*prefix, *later), 480, ((0., 120.),)))
            anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
            # 复制段与首段图像完全相同；仅首前缀在整谱唯一时才能建立首次身份。
            frame = trace_frame([(head, 110 * math.exp(-2 * (head.start - 1.)) - 30) for head in prefix], connected=True)
            anchor.observe(frame, 10.)
            self.assertEqual(bool(anchor.samples), not copied)

    def test_late_head_never_reseeds_from_upper_follower(self):
        anchor = self.anchor()
        anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
        for center in (300, 80):
            with self.assertRaises(RuntimeError):
                anchor.observe(trace_frame([(anchor.gesture, center)]), 10.)
        self.assertEqual(anchor.samples, [])

    def test_bar_without_triangle_cannot_supply_trace_head(self):
        anchor = self.anchor()
        anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
        anchor.observe(trace_frame([(anchor.gesture, 80)], triangle=False), 10.)
        self.assertEqual(anchor.samples, [])

    def test_dense_offset_trace_sequence_reaches_original_fit(self):
        first = Gesture((Point(1., 7, 2, 6),), 'trace', critical=True)
        following = tuple(replace(first, points=(Point(1. + index * .0457317, lane, 2, 6),))
                          for index, lane in ((1, 9), (2, 11)))
        chart = Chart((first, *following), 480, ((0., 120.),))
        anchor = self.anchor(chart)
        anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
        epoch = None
        for index in range(30):
            now = 10 + index * .025
            center = 110 * math.exp(2 * (now - 10)) - 30
            heads = [(head, (center + 30) * math.exp(-2 * (head.start - first.start)) - 30)
                     for head in chart.gestures]
            epoch = anchor.observe(trace_frame(heads, connected=True), now, capture_seconds=.01)
            if epoch is not None:
                break
        self.assertIsNotNone(epoch)
        self.assertLess(abs(anchor.fit['first_hit'] - (10 + math.log(600 / 110) / 2)), .02)
        self.assertGreater(anchor.fit['first_hit'] - now, .08)

    def test_same_time_members_need_independent_actual_heads(self):
        first = Gesture((Point(1., 5, 2, 6),), 'trace', critical=True)
        for lane in (7, 10):
            other = replace(first, points=(Point(1., lane, 2, 6),))
            chart = Chart((first, other), 480, ((0., 120.),))
            for missing in (False, True):
                with self.subTest(lane=lane, missing=missing):
                    anchor = self.anchor(chart)
                    anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
                    anchor.observe(trace_frame([(first, 100)] + ([] if missing else [(other, 100)])), 10.)
                    self.assertEqual(bool(anchor.samples), not missing)

    def test_group_constellation_binds_merged_members_and_right_hand_first(self):
        for reverse, adjacent in ((False, True), (True, False)):
            left = Gesture((Point(1., 5, 2, 6),), 'trace', critical=True)
            right = replace(left, points=(Point(1., 7 if adjacent else 10, 2, 6),))
            heads = (right, left) if reverse else (left, right)
            successors = tuple(replace(left, points=(Point(when, lane, 2, 6),))
                               for when, lane in ((1.05, 9), (1.1, 11), (3., 5),
                                                  (3., 7 if adjacent else 10), (3.05, 3), (3.1, 2)))
            anchor = self.anchor(Chart((*heads, *successors), 480, ((0., 120.),)))
            anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
            frame = trace_frame([(head, 110 * math.exp(-2 * (head.start - 1.)) - 30)
                                 for head in (*heads, *successors[:2])])
            anchor.observe(frame, 10.)
            self.assertTrue(anchor.samples, anchor.observation)

    def test_constellation_accepts_same_depth_separate_real_followers(self):
        first = Gesture((Point(1., 7, 2, 6),), 'trace', critical=True)
        values = tuple(replace(first, points=(Point(when, lane, 2, 6),)) for when, lane in
                       ((1.05, 3), (1.05, 9), (1.1, 11), (3., 7), (3.05, 5), (3.1, 3)))
        anchor = self.anchor(Chart((first, *values), 480, ((0., 120.),)))
        anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
        frame = trace_frame([(head, 110 * math.exp(-2 * (head.start - 1.)) - 30)
                             for head in (first, *values[:3])])
        anchor.observe(frame, 10.)
        self.assertTrue(anchor.samples, anchor.observation)

    def test_initial_frame_with_existing_notes_rejects_before_baseline_can_hide_them(self):
        anchor = self.anchor()
        with self.assertRaisesRegex(RuntimeError, '首帧'):
            anchor.observe(trace_frame([(anchor.gesture, 100)]), 9.)
        with self.assertRaises(RuntimeError):
            anchor.observe(trace_frame([(anchor.gesture, 80)]), 10.)
        self.assertEqual(anchor.samples, [])

    def test_baseline_white_decoration_requires_one_actual_gold_owner_on_both_sides(self):
        for source in ('none', 'left_only', 'separate_sides'):
            with self.subTest(source=source):
                hsv = np.zeros((720, 1280, 3), np.uint8)
                cv2.fillConvexPoly(hsv, np.array([(640, 90), (635, 96), (645, 96)]), (0, 0, 255))
                hsv[98:102, 624:656] = (0, 0, 255)
                if source != 'none':
                    hsv[90:103, 622:624] = (25, 150, 180)
                if source == 'separate_sides':
                    hsv[90:103, 656:658] = (25, 150, 180)
                anchor = self.anchor()
                self.assertIsNone(anchor.observe(cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR), 9.))
                self.assertFalse(anchor.chart_bound_tracker.lost)
                self.assertEqual(anchor.samples, [])

    def test_invalid_trace_metadata_and_wrong_chart_cannot_provide_context(self):
        chart = self.chart()
        first = chart.first
        variants = [replace(first, flick=1), replace(first, critical=1),
                    replace(first, points=(replace(first.points[0], time=float('nan')),)),
                    replace(first, points=(replace(first.points[0], lane=True),)),
                    replace(first, points=(replace(first.points[0], width=0),)),
                    replace(first, points=(replace(first.points[0], path_node=False),)),
                    replace(first, points=(replace(first.points[0], direction=False),)),
                    replace(first, points=(first.points[0], first.points[0]))]
        for invalid in variants:
            with self.subTest(invalid=invalid):
                changed = replace(chart, gestures=(invalid, *chart.gestures[1:]))
                anchor = self.anchor(changed)
                anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
                anchor.observe(trace_frame([(first, 80)]), 10.)
                self.assertEqual(anchor.samples, [])
        with patch.dict(os.environ, {FLAG: '1'}):
            wrong = StartAnchor(first, gold_tap_context_chart=replace(chart, gestures=(replace(first),)))
        self.assertEqual(wrong.chart_bound_tracker.groups, ())

    def test_stationary_background_and_broken_source_cannot_be_motion(self):
        frame = trace_frame([(self.chart().first, 90)])
        anchor = self.anchor()
        anchor.baseline = frame.copy()
        for now in (10., 10.1, 10.2):
            anchor.observe(frame, now)
        self.assertEqual(anchor.samples, [])

    def test_flick_chevron_above_bar_cannot_substitute_trace_triangle(self):
        anchor = self.anchor()
        frame = trace_frame([(anchor.gesture, 110)], triangle=False)
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        cv2.polylines(hsv, [np.array([(632, 90), (640, 82), (648, 90)])], False, (25, 40, 255), 2)
        anchor.observe(np.zeros_like(frame), 9.)
        anchor.observe(cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR), 10.)
        self.assertEqual(anchor.samples, [])

    def test_white_marker_without_gold_enclosure_cannot_borrow_a_bar_source(self):
        anchor = self.anchor()
        frame = trace_frame([(anchor.gesture, 100)], triangle=False)
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        cv2.fillConvexPoly(hsv, np.array([(640, 91), (635, 96), (645, 96)]), (0, 0, 255))
        anchor.observe(np.zeros_like(frame), 9.)
        anchor.observe(cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR), 10.)
        self.assertEqual(anchor.samples, [])

    def test_antialiased_bar_needs_two_complete_rows_not_spliced_halves(self):
        for kind in ('single_row', 'spliced_halves', 'slanted_body'):
            with self.subTest(kind=kind):
                anchor = self.anchor()
                hsv = cv2.cvtColor(trace_frame([(anchor.gesture, 100)]), cv2.COLOR_BGR2HSV)
                white = cv2.inRange(hsv, (0, 0, 235), (179, 110, 255)) != 0
                gold = cv2.inRange(hsv, (15, 55, 205), (45, 255, 255)) != 0
                yy, xx = np.indices(gold.shape)
                if kind == 'single_row':
                    hsv[gold & (yy != 101)] = 0
                elif kind == 'spliced_halves':
                    hsv[gold & (((yy % 2 == 0) & (xx < 640)) | ((yy % 2 != 0) & (xx >= 640)))] = 0
                else:
                    hsv[gold] = 0
                    cv2.line(hsv, (620, 104), (658, 86), (25, 150, 235), 3)
                # 独立白标记保持，拒绝不能由顺便删除三角产生。
                original = cv2.cvtColor(trace_frame([(anchor.gesture, 100)]), cv2.COLOR_BGR2HSV)
                hsv[white] = original[white]
                frame = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
                anchor.observe(np.zeros_like(frame), 9.)
                anchor.observe(frame, 10.)
                self.assertEqual(anchor.samples, [])

    def test_trace_source_past_bottom_rejects_before_an_upper_follower(self):
        anchor = self.anchor()
        hsv = cv2.cvtColor(trace_frame([(anchor.gesture, 100), (anchor.gesture, 70)]), cv2.COLOR_BGR2HSV)
        cv2.line(hsv, (640, 103), (640, 550), (25, 150, 180), 4)
        anchor.observe(np.zeros_like(hsv), 9.)
        with self.assertRaises(RuntimeError):
            anchor.observe(cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR), 10.)
        self.assertEqual(anchor.samples, [])

    def test_phase_proof_is_trace_typed_one_to_one_and_bounded(self):
        chart = self.chart()
        first = chart.first
        main = (628, 76, 23, 8, 1)
        rear = ((644, 66, 20, 8, 1), (644, 62, 20, 8, 1))
        one_event = TracePhaseContext(((0., 7, 2), (.05, 9, 2)), ((.05, 9, 2, 1),))
        self.assertFalse(one_event.unique(main, (main, *rear))[0], '两深度不能重复占用同一事件')
        untyped = TracePhaseContext(((0., 7, 2), (.05, 9, 2), (.1, 11, 2)))
        self.assertFalse(untyped.unique(main, (main, *rear))[0], '横条元数据不能证明三角首前缀')
        bounded = TracePhaseContext(((0., 7, 2), (.05, 9, 2), (.1, 11, 2)),
                                    ((.05, 9, 2, 1), (.1, 11, 2, 1)))
        bounded.max_comparisons = 0
        self.assertEqual(bounded.unique(main, (main, *rear))[1], 'constellation_comparison_limit')
        self.assertEqual(bounded.unique(main, (main,) * 9)[1], 'constellation_observation_limit')

    def test_early_evidence_ignores_static_intro_then_retains_malformed_head(self):
        first = self.chart().first
        background = replace(first, points=(replace(first.points[0], lane=2),))
        baseline = trace_frame([(background, 90)], triangle=False)
        anchor = self.anchor()
        evidence = OpeningEvidence(True)
        anchor.observe(baseline, 9.)
        for index in range(40):
            anchor.observe(baseline, 9.01 + index * .01)
            evidence.observe(9.01 + index * .01, baseline, anchor.observation)
        self.assertEqual(evidence.frames, [])
        malformed = np.maximum(baseline, trace_frame([(first, 90)], triangle=False))
        anchor.observe(malformed, 10.)
        evidence.observe(10., malformed, anchor.observation)
        self.assertEqual(len(evidence.frames), 1)
        self.assertIn('incomplete', evidence.frames[0][3]['reason'])
        for index in range(300):
            evidence.observe(10 + index * .1, malformed, {'nonempty': True, 'reason': str(index % 5)})
        self.assertLessEqual(len(evidence.frames), 12)
        disabled = OpeningEvidence(False)
        disabled.observe(10., malformed, {'nonempty': True, 'reason': 'no_head'})
        self.assertEqual(disabled.frames, [])

    def test_evidence_requires_current_release_and_io_failure_keeps_original_error(self):
        from project_sekai.solo_live import SoloLive
        workflow = object.__new__(SoloLive)
        image = np.zeros((720, 1280, 3), np.uint8)
        workflow.anchor_frames = [(10., image)]
        workflow.opening_evidence = OpeningEvidence(True)
        workflow.opening_evidence.observe(10., image, {'nonempty': True, 'reason': 'incomplete'})
        report = {'engine': 'native', 'error': '原始同步错误', 'playback': {'release_confirmed': False}}
        with patch('project_sekai.solo_live.write_image') as write:
            workflow.save_opening_evidence(report, Path('unused'))
            workflow.save_anchor_evidence(report, Path('unused'))
            write.assert_not_called()
        self.assertTrue(workflow.anchor_frames)
        report['playback'] = {'release_confirmed': True, 'sent_actions': 0,
                              'release': {'release_proof': 'no-touch-possible-and-cleanup'}}
        with patch('project_sekai.solo_live.write_image', side_effect=OSError('磁盘写入失败')):
            workflow.save_opening_evidence(report, Path('unused'))
            workflow.save_anchor_evidence(report, Path('unused'))
        self.assertEqual(report['error'], '原始同步错误')
        self.assertIn('磁盘写入失败', report['opening_evidence_warning'])
        self.assertIn('磁盘写入失败', report['anchor_evidence_warning'])

    def test_first_eight_geometric_frames_are_contiguous_before_reason_slots(self):
        evidence = OpeningEvidence(True)
        frame = np.zeros((8, 8, 3), np.uint8)
        for index in range(20):
            evidence.observe(10 + index * .03, frame, {'nonempty': True, 'reason': 'incomplete'})
        self.assertEqual([round(value[1] - 10, 3) for value in evidence.frames[:8]],
                         [round(index * .03, 3) for index in range(8)])
        for index in range(10):
            evidence.observe(11 + index * .03, frame, {'nonempty': True, 'reason': f'new_reason_{index}'})
        self.assertEqual(len(evidence.frames), 12)

    def test_report_quantized_prefix_keeps_next_real_head(self):
        # 真实报告中已确认的二十点及紧随其后的拒绝帧；时间与深度保持原值。
        samples = [(0.0000000, 58.), (0.0305244, 62.5),
                   (0.0622191, 66.5), (0.0935080, 70.5),
                   (0.1240112, 75.5), (0.1854029, 83.),
                   (0.2179519, 88.), (0.2488227, 93.5),
                   (0.2801813, 99.5), (0.3115752, 105.),
                   (0.3424160, 111.), (0.3743537, 118.),
                   (0.4055114, 124.), (0.4360618, 127.),
                   (0.4665256, 132.), (0.4976330, 137.5),
                   (0.5458362, 146.5), (0.5778778, 152.),
                   (0.6075412, 158.), (0.6536294, 164.5),
                   (0.6860663, 171.5)]
        tracker = self.anchor().chart_bound_tracker
        blank = np.zeros((720, 1280, 3), np.uint8)
        for captured_at, y in samples:
            with patch.object(tracker, 'measure', return_value=(y, [])):
                self.assertEqual(tracker.observe(blank, blank, captured_at), y)
        self.assertEqual(tracker.motion, samples)
        self.assertFalse(tracker.lost)

    def test_synthetic_trace_full_anchor_obtains_epoch_after_recent_window(self):
        anchor = self.anchor()
        anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
        epoch = None
        for index in range(50):
            captured_at = 10 + index * .03
            y = 110 * math.exp(1.5 * index * .03) - 30
            epoch = anchor.observe(trace_frame([(anchor.gesture, y)]), captured_at)
            if epoch is not None:
                break
        self.assertIsNotNone(epoch)
        self.assertGreater(anchor.samples[-1][1], 230)
        self.assertGreater(len(anchor.samples), 8)
        self.assertGreater(epoch + anchor.gesture.start - captured_at, .08)
        self.assertFalse(anchor.chart_bound_tracker.lost)

    def test_recent_motion_accepts_quantization_and_gradual_speed_change(self):
        blank = np.zeros((720, 1280, 3), np.uint8)
        for acceleration in (0., 8., 12.):
            with self.subTest(acceleration=acceleration):
                tracker = self.anchor().chart_bound_tracker
                for index in range(65):
                    time = index * .03
                    y = round((60 + 170 * time + acceleration * time ** 2) * 2) / 2
                    with patch.object(tracker, 'measure', return_value=(y, [])):
                        self.assertEqual(tracker.observe(blank, blank, 10 + time), y)
                self.assertEqual(len(tracker.motion), 65)

    def test_recent_motion_never_accepts_jump_reverse_or_expired_continuity(self):
        blank = np.zeros((720, 1280, 3), np.uint8)
        for delta_time, delta_y in ((.03, 30), (.03, -10), (.51, 90)):
            with self.subTest(delta_time=delta_time, delta_y=delta_y):
                tracker = self.anchor().chart_bound_tracker
                tracker.motion = [(10 + index * .03, 60 + index * 5) for index in range(20)]
                before = list(tracker.motion)
                with patch.object(tracker, 'measure', return_value=(before[-1][1] + delta_y, [])):
                    self.assertIsNone(tracker.observe(blank, blank, before[-1][0] + delta_time))
                self.assertEqual(tracker.motion, before)
                self.assertTrue(tracker.lost)

    def test_fast_initialization_does_not_freeze_insufficient_recent_span(self):
        tracker = self.anchor().chart_bound_tracker
        blank = np.zeros((720, 1280, 3), np.uint8)
        for index in range(80):
            with patch.object(tracker, 'measure', return_value=(60 + index * 3, [])):
                self.assertEqual(tracker.observe(blank, blank, 10 + index * .005), 60 + index * 3)
        self.assertEqual(len(tracker.motion), 80)
        self.assertEqual(tracker.measurement['motion_envelope']['points'], 32)
        self.assertGreaterEqual(tracker.measurement['motion_envelope']['span_s'], .25)

    def test_recent_window_maturity_and_real_point_cap_boundaries(self):
        blank = np.zeros((720, 1280, 3), np.uint8)
        for count, span, expected in ((32, .25, 32), (33, .25, 32),
                                      (33, .249999, 33), (33, .250001, 32)):
            with self.subTest(count=count, span=span):
                tracker = self.anchor().chart_bound_tracker
                tracker.motion = [(float(time), 60 + 200 * float(time))
                                  for time in np.linspace(0, span, count)]
                before = list(tracker.motion)
                with patch.object(tracker, 'measure', return_value=(64 + 200 * span, [])):
                    self.assertIsNotNone(tracker.observe(blank, blank, span + .02))
                self.assertEqual(tracker.motion[:-1], before)
                measurement = tracker.measurement['motion_envelope']
                self.assertEqual(measurement['points'], expected)
                self.assertAlmostEqual(measurement['span_s'], span)

    def test_recent_motion_retains_all_following_competitors_and_no_reseed(self):
        blank = np.zeros((720, 1280, 3), np.uint8)
        first = self.chart().first
        tracker = self.anchor().chart_bound_tracker
        tracker.motion = [(10 + index * .03, 60 + index * 5) for index in range(20)]
        before = list(tracker.motion)
        # 即使竞争来自较远谱面时间，所有匹配delta也必须参与同一近期模型查询。
        tracker.following_geometry = ((2., first.points[0].lane, first.points[0].width),
                                      (.02, first.points[0].lane, first.points[0].width))
        frame = trace_frame([(first, 160)])
        tracker.measure(frame, blank)
        with patch.object(tracker, 'measure', return_value=(160., [])):
            self.assertIsNone(tracker.observe(frame, blank, 10.6))
        self.assertEqual(tracker.status, 'motion_identity_ambiguous')
        self.assertEqual(tracker.motion, before)
        with patch.object(tracker, 'measure', return_value=(160., [])):
            self.assertIsNone(tracker.observe(frame, blank, 11.1))
        self.assertTrue(tracker.lost)
        self.assertEqual(tracker.status, 'continuity_lost')

    def test_recent_envelope_does_not_replace_independent_full_history_prediction(self):
        tracker = self.anchor().chart_bound_tracker
        blank = np.zeros((720, 1280, 3), np.uint8)
        # 局部可拟合的持续加速最终偏离全前缀预测，仍须拒绝而非无限重学速度。
        for index in range(65):
            time = index * .03
            y = round((60 + 170 * time + 25 * time ** 2) * 2) / 2
            with patch.object(tracker, 'measure', return_value=(y, [])):
                tracker.observe(blank, blank, 10 + time)
            if tracker.lost:
                break
        self.assertTrue(tracker.lost)
        self.assertEqual(tracker.status, 'motion_identity_lost')
        self.assertLess(len(tracker.motion), 65)

    @unittest.skipUnless(os.environ.get('MAAPJSK_TRACE_MOTION_REPORT'), '未提供私有连续帧fixture')
    def test_private_contiguous_report_tracks_last_saved_head_without_claiming_epoch(self):
        from project_sekai.chart_catalog import ChartRepository
        from project_sekai.song_identity import read_image
        path = Path(os.environ['MAAPJSK_TRACE_MOTION_REPORT']).resolve()
        report = json.loads(path.read_text(encoding='utf-8-sig'))
        repo = ChartRepository(Path(__file__).resolve().parents[1] / 'resource/charts')
        info = report['chart']
        self.assertEqual(repo.songs[info['music_id']]['charts'][info['difficulty']]['sha256'], info['sha256'])
        anchor = self.anchor(parse_sus(repo.load_chart(info['music_id'], info['difficulty'])))
        entries = {item['captured_at']: item for item in
                   [*report['anchor_frames'], *report['opening_evidence']['frames']]}
        for captured_at, item in sorted(entries.items()):
            self.assertIsNone(anchor.observe(read_image(path.parent / item['path']), captured_at))
        self.assertEqual(anchor.samples[-1], (max(entries), 171.5))
        self.assertEqual(len(anchor.samples), 21)
        self.assertFalse(anchor.chart_bound_tracker.lost)
        self.assertIsNone(anchor.fit)

    @unittest.skipUnless(os.environ.get('MAAPJSK_TRACE_IDENTITY_REPORT'), '未提供私有逐帧身份fixture')
    def test_private_identity_report_keeps_all_twenty_four_complete_constellations(self):
        from project_sekai.chart_catalog import ChartRepository
        from project_sekai.song_identity import read_image
        path = Path(os.environ['MAAPJSK_TRACE_IDENTITY_REPORT']).resolve()
        report = json.loads(path.read_text(encoding='utf-8-sig'))
        repo = ChartRepository(Path(__file__).resolve().parents[1] / 'resource/charts')
        info = report['chart']
        self.assertEqual(repo.songs[info['music_id']]['charts'][info['difficulty']]['sha256'], info['sha256'])
        anchor = self.anchor(parse_sus(repo.load_chart(info['music_id'], info['difficulty'])))
        for item in report['anchor_frames']:
            self.assertIsNone(anchor.observe(read_image(path.parent / item['path']), item['captured_at']))
        self.assertEqual(len(anchor.samples), 24)
        self.assertEqual(anchor.samples[-1], (report['anchor_frames'][-1]['captured_at'], 218.5))
        self.assertEqual(anchor.observation['phase_identity'], 'unique_first_constellation')
        self.assertFalse(anchor.chart_bound_tracker.lost)
        self.assertIsNone(anchor.fit)

    def test_capture_past_prefix_waits_for_preferred_model_then_matches_holdout(self):
        # 公开相对时间与实测深度只验证拟合；原始像素完整链路另由私有留出回归证明。
        samples = [(0.0000000, 56.5),
                   (0.0221429, 60.5),
                   (0.0717187, 66.5),
                   (0.1223529, 73.5),
                   (0.1707090, 81.0),
                   (0.2402735, 91.0),
                   (0.2613856, 94.0),
                   (0.2828824, 99.5),
                   (0.3054927, 102.5),
                   (0.3266104, 105.5),
                   (0.3490302, 108.5),
                   (0.3728062, 115.0),
                   (0.3996761, 118.0),
                   (0.4233189, 124.5),
                   (0.4689316, 132.5),
                   (0.4971076, 135.5),
                   (0.5218955, 140.5),
                   (0.5435419, 143.5),
                   (0.5680702, 146.5),
                   (0.5933561, 153.0),
                   (0.6156735, 156.0),
                   (0.6404859, 162.0),
                   (0.6641888, 165.5),
                   (0.6889345, 171.5),
                   (0.7126415, 175.5),
                   (0.7405207, 182.0),
                   (0.7643180, 186.0),
                   (0.7891467, 193.0),
                   (0.8126770, 197.0),
                   (0.8351695, 204.5),
                   (0.8586251, 208.5),
                   (0.8817692, 212.5),
                   (0.9059609, 220.5),
                   (0.9319755, 224.5),
                   (0.9563826, 233.0),
                   (0.9795731, 238.0),
                   (1.0033595, 246.5)]
        anchor = self.anchor(self.unique_constellation_chart())
        anchor.baseline = np.zeros((720, 1280, 3), np.uint8)
        anchor.chart_bound_tracker.trace_identity_requires_constellation = True
        epoch = None
        for index, (when, y) in enumerate(samples):
            with patch.object(anchor.chart_bound_tracker, 'observe', return_value=y):
                epoch = anchor.observe(anchor.baseline, when, capture_seconds=.006)
            if index < len(samples) - 1:
                self.assertIsNone(epoch)
        self.assertIsNotNone(epoch)
        self.assertEqual(anchor.fit['model'], 'perspective')
        # 未来穿线两帧仅作断言，未加入以上生产拟合输入。
        self.assertGreaterEqual(anchor.fit['first_hit'], 1.7715357)
        self.assertLessEqual(anchor.fit['first_hit'], 1.7999274)

    def test_trace_fit_window_requires_maturity_and_preserves_real_endpoints(self):
        from project_sekai.chart_player import _trace_fit_window
        self.assertIsNone(_trace_fit_window([(index * .05, 60 + index * 5) for index in range(8)], .5))
        self.assertIsNone(_trace_fit_window([(index * .1, 60 + index * 5) for index in range(6)], .5))
        samples = [(index * .005, 60 + index * 3) for index in range(180)]
        before = list(samples)
        window = _trace_fit_window(samples, .5)
        self.assertEqual(len(window), 32)
        self.assertEqual(window[-1], samples[-1])
        self.assertGreaterEqual(window[-1][0] - window[0][0], .5)
        self.assertTrue(all(point in samples for point in window))
        self.assertEqual(samples, before)

    def test_trace_sixty_millisecond_cadence_extends_short_check_window_to_eight_points(self):
        from project_sekai.chart_player import _trace_fit_window
        samples = [(index * .06, 60 + index * 15) for index in range(20)]
        for seconds in (.250, .500):
            window = _trace_fit_window(samples, seconds)
            self.assertGreaterEqual(len(window), 8)
            self.assertGreaterEqual(window[-1][0] - window[0][0], seconds)
        chart = self.unique_constellation_chart()
        anchor = self.anchor(chart)
        anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
        epoch = None
        for index in range(20):
            time = index * .06
            epoch = anchor.observe(self.constellation_frame(chart, 90 * math.exp(2 * time) - 30), 10 + time)
            if epoch is not None:
                break
        self.assertIsNotNone(epoch)
        self.assertEqual(anchor.fit['fit_window'], 'trace_recent_trajectory')
        self.assertGreaterEqual(anchor.fit['timing_windows']['check_sample_count'], 8)

    def timing_anchor(self, *, immature=False):
        chart = self.unique_constellation_chart()
        anchor = self.anchor(chart)
        anchor.baseline = np.zeros((720, 1280, 3), np.uint8)
        anchor.chart_bound_tracker.trace_identity_requires_constellation = True
        step = .01 if immature else .02
        anchor.samples = [(10 + index * step, 80 + index * 5) for index in range(30)]
        return anchor, 10 + 30 * step

    def test_trace_recent_fit_waits_for_mature_primary_window(self):
        anchor, when = self.timing_anchor(immature=True)
        with patch.object(anchor.chart_bound_tracker, 'observe', return_value=240):
            self.assertIsNone(anchor.observe(anchor.baseline, when))
        self.assertIsNone(anchor.fit)
        self.assertEqual(anchor.observation['timing_windows']['status'], 'waiting_mature_history')

    def test_trace_two_windows_disagreement_waits_and_rejects_expired_margin(self):
        for hits, raises in (((11.2, 11.4), False), ((10.67, 10.77), True)):
            anchor, when = self.timing_anchor()
            main, check = [(1., 'perspective', hits[0])], [(1., 'perspective', hits[1])]
            with patch.object(anchor.chart_bound_tracker, 'observe', return_value=240), \
                    patch('project_sekai.chart_player._anchor_fit_candidates', side_effect=[(main, main), (check, check)]):
                if raises:
                    with self.assertRaisesRegex(RuntimeError, '余量不足'):
                        anchor.observe(anchor.baseline, when)
                    self.assertEqual(anchor.failure_reason, 'trajectory_window_disagreement_margin')
                else:
                    self.assertIsNone(anchor.observe(anchor.baseline, when))
            self.assertIsNone(anchor.fit)

    def test_trace_original_ambiguity_guard_applies_to_both_new_windows(self):
        ordinary = [(1., 'perspective', 11.2)]
        ambiguous = [(5., 'linear', 11.), (6., 'perspective', 11.2)]
        for main, check in ((ambiguous, ordinary), (ordinary, ambiguous)):
            anchor, when = self.timing_anchor()
            with patch.object(anchor.chart_bound_tracker, 'observe', return_value=240), \
                    patch('project_sekai.chart_player._anchor_fit_candidates', side_effect=[(main, main), (check, check)]):
                self.assertIsNone(anchor.observe(anchor.baseline, when))
            self.assertIsNone(anchor.fit)
            self.assertIsNotNone(anchor.ambiguity_defer)
            self.assertEqual(anchor.ambiguity_defer['status'], 'waiting')

    def test_trace_selection_precedes_horizon_filter_and_never_substitutes_earlier_model(self):
        anchor, when = self.timing_anchor()
        models = [(1., 'exponential', 11.3), (1.5, 'perspective', 11.45)]
        with patch.object(anchor.chart_bound_tracker, 'observe', return_value=240), \
                patch('project_sekai.chart_player._anchor_fit_candidates', return_value=(models, models)):
            self.assertIsNone(anchor.observe(anchor.baseline, when))
        self.assertIsNone(anchor.fit)
        self.assertEqual(anchor.observation['timing_windows']['status'], 'waiting_selected_model_horizon')
        self.assertEqual(anchor.observation['timing_windows']['primary_first_hit'], 11.45)

    def test_unmatched_neutral_or_purple_ui_without_any_gold_source_is_not_unknown_head(self):
        chart = self.chart()
        blank = np.zeros((720, 1280, 3), np.uint8)
        for color in ((0, 0, 250), (126, 107, 237)):
            tracker = self.anchor(chart).chart_bound_tracker
            hsv = cv2.cvtColor(trace_frame([(chart.first, 100)]), cv2.COLOR_BGR2HSV)
            hsv[544:549, 540:719] = color
            y, unknown = tracker.measure(cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR), blank)
            self.assertIsNotNone(y)
            self.assertFalse(any(value >= y for value in unknown))

    def test_unmatched_white_core_with_even_one_gold_pixel_remains_unknown(self):
        chart = self.chart()
        blank = np.zeros((720, 1280, 3), np.uint8)
        for pixel in ((560, 546), (539, 546), (719, 546)):
            tracker = self.anchor(chart).chart_bound_tracker
            hsv = cv2.cvtColor(trace_frame([(chart.first, 100)]), cv2.COLOR_BGR2HSV)
            hsv[544:549, 540:719] = (126, 107, 237)
            hsv[pixel[1], pixel[0]] = (25, 150, 220)
            y, unknown = tracker.measure(cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR), blank)
            self.assertIsNotNone(y)
            self.assertIn(546.5, unknown)

    @unittest.skipUnless(os.environ.get('MAAPJSK_TRACE_SOURCE_REPORT'), '未提供私有未知前沿fixture')
    def test_private_source_report_tracks_actual_last_197_without_epoch(self):
        from project_sekai.chart_catalog import ChartRepository
        from project_sekai.song_identity import read_image
        path = Path(os.environ['MAAPJSK_TRACE_SOURCE_REPORT']).resolve()
        report = json.loads(path.read_text(encoding='utf-8-sig'))
        repo = ChartRepository(Path(__file__).resolve().parents[1] / 'resource/charts')
        info = report['chart']
        self.assertEqual(repo.songs[info['music_id']]['charts'][info['difficulty']]['sha256'], info['sha256'])
        anchor = self.anchor(parse_sus(repo.load_chart(info['music_id'], info['difficulty'])))
        entries = {item['captured_at']: item for item in
                   [*report['anchor_frames'], *report['opening_evidence']['frames']]}
        for captured_at, item in sorted(entries.items()):
            self.assertIsNone(anchor.observe(read_image(path.parent / item['path']), captured_at))
        self.assertEqual(anchor.samples[-1], (max(entries), 197.))
        self.assertFalse(anchor.chart_bound_tracker.lost)
        self.assertIsNone(anchor.fit)

    @unittest.skipUnless(os.environ.get('MAAPJSK_TRACE_TIMING_CAPTURE'), '未提供私有穿线留出fixture')
    def test_private_past_only_epoch_matches_future_judge_crossing_holdout(self):
        from project_sekai.chart_catalog import ChartRepository
        from project_sekai.song_identity import read_image
        path = Path(os.environ['MAAPJSK_TRACE_TIMING_CAPTURE']).resolve()
        holdout_path = Path(os.environ['MAAPJSK_TRACE_TIMING_HOLDOUT']).resolve()
        report = json.loads(path.read_text(encoding='utf-8-sig'))
        holdout = json.loads(holdout_path.read_text(encoding='utf-8-sig'))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), holdout['inputs']['report'])
        identity = report['final_identity']
        repo = ChartRepository(Path(__file__).resolve().parents[1] / 'resource/charts')
        chart = parse_sus(repo.load_chart(identity['song_id'], identity['difficulty']))
        anchor = self.anchor(chart)
        epoch = None
        for index, item in enumerate(report['frames']):
            # 未来穿线帧严格留出；拟合只接收实际生产观察到冻结点的过去帧。
            self.assertLess(index, holdout['crossing']['below']['index'])
            self.assertEqual(hashlib.sha256((path.parent / item['path']).read_bytes()).hexdigest(),
                             holdout['inputs']['frames'][item['path']])
            epoch = anchor.observe(read_image(path.parent / item['path']), item['requested_at'],
                                   capture_seconds=item['capture_seconds'])
            if epoch is not None:
                break
        self.assertIsNotNone(epoch)
        low, high = holdout['crossing']['conservative_request_to_received_bracket']
        # 请求时刻不是渲染时刻；保守留出界外另保留30毫秒时钟／采样不确定量。
        self.assertGreaterEqual(anchor.fit['first_hit'], low - .030)
        self.assertLessEqual(anchor.fit['first_hit'], high + .030)
        self.assertEqual(anchor.fit['fit_window'], 'trace_recent_trajectory')
        self.assertGreaterEqual(anchor.fit['window_seconds'], .500)
        self.assertLessEqual(anchor.fit['sample_count'], 32)

    @unittest.skipUnless(os.environ.get('MAAPJSK_TRACE_EARLY_REPORT'), '未提供私有真实早期帧fixture')
    def test_private_early_frames_use_actual_chart_and_form_unique_first_identity(self):
        from project_sekai.chart_catalog import ChartRepository
        from project_sekai.song_identity import read_image
        report_path = Path(os.environ['MAAPJSK_TRACE_EARLY_REPORT']).resolve()
        report = json.loads(report_path.read_text(encoding='utf-8-sig'))
        directory = report_path.parent
        copied = directory / 'chart.sus'
        if copied.is_file():
            body = copied.read_bytes()
            self.assertEqual(hashlib.sha256(body).hexdigest(), report['chart']['sha256'])
            chart = parse_sus(body.decode('utf-8-sig'))
        else:
            repo = ChartRepository(Path(__file__).resolve().parents[1] / 'resource/charts')
            info = report['chart']
            self.assertEqual(repo.songs[info['music_id']]['charts'][info['difficulty']]['sha256'], info['sha256'])
            chart = parse_sus(repo.load_chart(info['music_id'], info['difficulty']))
        anchor = self.anchor(chart)
        baseline = report['anchor_frames'][0]
        anchor.observe(read_image(directory / baseline['path']), baseline['captured_at'])
        early = report['opening_evidence']['frames']
        for index, item in enumerate(early[:2]):
            self.assertIsNone(anchor.observe(read_image(directory / item['path']), item['captured_at']))
            if index == 0:
                self.assertEqual(anchor.observation['phase_identity'], 'unique_first_constellation')
        self.assertEqual([value[1] for value in anchor.samples], [93.5, 99.])
        self.assertGreater(early[2]['captured_at'] - early[1]['captured_at'], .5)
        with self.assertRaisesRegex(RuntimeError, 'continuity_lost'):
            anchor.observe(read_image(directory / early[2]['path']), early[2]['captured_at'])


if __name__ == '__main__':
    unittest.main()
