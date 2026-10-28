from dataclasses import replace
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from project_sekai import chart_player as cp
from project_sekai.sus_chart import Chart, Gesture, Point
import test_chart_bound_head_tracker as tracker_tests


class GoldTapMixedContextTests(unittest.TestCase):
    def chart(self):
        return Chart(tuple(Gesture((Point(t, lane, width, 2),), 'tap', critical=True)
                           for t, lane, width in ((1., 8, 4), (1.2, 8, 4), (1.6, 10, 4))),
                     480, ((0., 120.),))

    def anchor(self, chart=None, **env):
        chart = chart or self.chart()
        values = {'MAAPJSK_GOLD_TAP_MIXED_CONTEXT_GUARD': '1',
                  'MAAPJSK_CHART_BOUND_HEAD_GUARD': '0',
                  'MAAPJSK_GOLD_TAP_HEAD_GUARD': '1', 'MAAPJSK_GOLD_TAP_FOLLOWERS_GUARD': '1'}
        values.update(env)
        with patch.dict(os.environ, values):
            return cp.StartAnchor(chart.first, gold_tap_context_chart=chart)

    def frame(self, *heads):
        return tracker_tests.ChartBoundHeadTrackerTests().frame(*heads)

    def test_metadata_and_default_gate_are_narrow_and_frozen(self):
        chart = self.chart()
        self.assertTrue(cp.gold_tap_mixed_context_eligible(chart, chart.first))
        self.assertFalse(cp.gold_tap_mixed_context_eligible(chart, replace(chart.first)))
        enabled = self.anchor()
        self.assertTrue(enabled.gold_tap_mixed_context_guard)
        self.assertFalse(enabled.chart_bound_head_guard)
        for key in ('MAAPJSK_GOLD_TAP_MIXED_CONTEXT_GUARD', 'MAAPJSK_GOLD_TAP_HEAD_GUARD',
                    'MAAPJSK_GOLD_TAP_FOLLOWERS_GUARD'):
            self.assertFalse(self.anchor(**{key: '0'}).gold_tap_mixed_context_guard)
        with patch.dict(os.environ, {}, clear=True):
            default = cp.StartAnchor(chart.first, gold_tap_context_chart=chart)
            self.assertTrue(default.gold_tap_mixed_context_guard)
            self.assertIsNotNone(default.chart_bound_tracker)
            self.assertTrue(enabled.gold_tap_mixed_context_guard)
        with patch.dict(os.environ, {'MAAPJSK_GOLD_TAP_MIXED_CONTEXT_GUARD': '0'}):
            self.assertTrue(default.gold_tap_mixed_context_guard)
            self.assertIsNone(cp.StartAnchor(chart.first, gold_tap_context_chart=chart).chart_bound_tracker)

    def test_invalid_and_conflicting_metadata_never_qualify(self):
        chart = self.chart()
        bad = [replace(chart, gestures=chart.gestures[:2]),
               replace(chart, gestures=(chart.first, *chart.gestures)),
               replace(chart, gestures=(chart.first, chart.gestures[2])),
               replace(chart, gestures=chart.gestures + (replace(chart.gestures[2], points=(Point(1., 3, 2, 2),)),)),
               replace(chart, gestures=chart.gestures * 20)]
        for field, value in (('critical', 1), ('critical', False), ('flick', True), ('flick', 1), ('kind', 'trace')):
            bad.append(replace(chart, gestures=(*chart.gestures[:2], replace(chart.gestures[2], **{field: value}))))
        for point in (Point(True, 10, 4, 2), Point(float('nan'), 10, 4, 2), Point(float('inf'), 10, 4, 2),
                      Point(1.6, True, 4, 2), Point(1.6, 10, True, 2), Point(1.6, 10, 4, True),
                      Point(1.6, 10, 4, 2, True), Point(1.6, 10, 5, 2)):
            bad.append(replace(chart, gestures=(*chart.gestures[:2], replace(chart.gestures[2], points=(point,)))))
        for value in bad:
            with self.subTest(chart=value):
                self.assertFalse(cp.gold_tap_mixed_context_eligible(value, chart.first))

    def test_nonoverlapping_gold_competitors_also_consume_context_bound(self):
        chart = self.chart()
        extra = tuple(Gesture((Point(1.01 + index * .01, 2, 2, 2),), 'tap', critical=True)
                      for index in range(31))
        self.assertTrue(cp.gold_tap_mixed_context_eligible(
            replace(chart, gestures=(*chart.gestures, *extra[:30])), chart.first))
        self.assertFalse(cp.gold_tap_mixed_context_eligible(
            replace(chart, gestures=(*chart.gestures, *extra)), chart.first))

    def test_legacy_first_head_must_seed_before_tracker_can_continue(self):
        anchor = self.anchor()
        first = anchor.gesture
        baseline = np.zeros((720, 1280, 3), np.uint8)
        anchor.observe(baseline, 9.)
        frame = self.frame((first, 60))
        with patch.object(cp, 'first_note_y', wraps=cp.first_note_y) as legacy:
            anchor.observe(frame, 10.)
            self.assertEqual(legacy.call_count, 1)
            self.assertEqual(len(anchor.samples), 1)
            self.assertTrue(anchor.legacy_first_head_seed)
            anchor.observe(self.frame((first, 70)), 10.06)
            self.assertEqual(legacy.call_count, 1)
            self.assertEqual(len(anchor.samples), 2)

    def test_rejected_or_disagreeing_first_frame_cannot_be_followed_by_reseed(self):
        for legacy_value in (None, 128., 150.):
            with self.subTest(legacy=legacy_value):
                anchor = self.anchor()
                anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
                with patch.object(cp, 'first_note_y', return_value=legacy_value):
                    with self.assertRaises(RuntimeError):
                        anchor.observe(self.frame((anchor.gesture, 60)), 10.)
                with self.assertRaises(RuntimeError):
                    anchor.observe(self.frame((anchor.gesture, 70)), 10.06)
                self.assertEqual(anchor.samples, [])
                self.assertFalse(anchor.legacy_first_head_seed)

    def test_late_head_and_lost_continuity_never_reseed(self):
        anchor = self.anchor()
        anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
        with self.assertRaises(RuntimeError):
            anchor.observe(self.frame((anchor.gesture, 150)), 10.)
        anchor = self.anchor()
        anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
        anchor.observe(self.frame((anchor.gesture, 60)), 10.)
        with self.assertRaises(RuntimeError):
            anchor.observe(self.frame((anchor.gesture, 75)), 10.6)

    def test_actual_multicore_first_rejection_is_terminal(self):
        anchor = self.anchor()
        anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
        frame = self.frame((anchor.gesture, 60), (anchor.gesture, 100))
        self.assertIsNone(cp.first_note_y(frame, anchor.gesture, np.zeros_like(frame),
                                        gold_tap_head_guard=True, gold_tap_followers_guard=True))
        with self.assertRaises(RuntimeError):
            anchor.observe(frame, 10.)
        with self.assertRaises(RuntimeError):
            anchor.observe(self.frame((anchor.gesture, 110)), 10.1)
        self.assertEqual(anchor.samples, [])

    def test_shallow_different_lane_is_kept_as_competing_geometry(self):
        chart = self.chart()
        anchor = self.anchor(chart)
        tracker = anchor.chart_bound_tracker
        frame = self.frame((chart.first, 60))
        tracker.measure(frame, np.zeros_like(frame))
        head = tracker.current_heads[0]
        box = (head['left'], head['y'] - head['height'] / 2,
               head['right'] - head['left'], head['height'])
        self.assertTrue(cp.chart_bound_head_geometry(*box, 10, 4))
        self.assertIn((.6, 10, 4), [(round(t, 2), lane, width) for t, lane, width in tracker.following_geometry])

    def test_repeated_similar_prefix_cannot_publish_ambiguous_motion(self):
        chart = self.chart()
        chart = replace(chart, gestures=(chart.first,
            replace(chart.gestures[1], points=(Point(1.001, 8, 4, 2),)), chart.gestures[2]))
        anchor = self.anchor(chart)
        anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
        anchor.observe(self.frame((chart.first, 60)), 10.)
        anchor.observe(self.frame((chart.first, 70)), 10.1)
        self.assertEqual(len(anchor.samples), 1)
        self.assertEqual(anchor.chart_bound_tracker.status, 'motion_identity_ambiguous')

    def test_missing_foremost_and_source_crop_do_not_replace_seed(self):
        for kind in ('missing', 'crop'):
            with self.subTest(kind=kind):
                anchor = self.anchor()
                anchor.observe(np.zeros((720, 1280, 3), np.uint8), 9.)
                anchor.observe(self.frame((anchor.gesture, 60)), 10.)
                before = list(anchor.samples)
                frame = (self.frame((anchor.gesture, 45)) if kind == 'missing'
                         else self.frame((anchor.gesture, 535)))
                try:
                    anchor.observe(frame, 10.1)
                except RuntimeError:
                    pass
                self.assertEqual(anchor.samples, before)

    def test_global_candidate_and_trace_paths_are_independent(self):
        global_anchor = self.anchor(**{'MAAPJSK_CHART_BOUND_HEAD_GUARD': '1'})
        self.assertTrue(global_anchor.chart_bound_head_guard)
        self.assertFalse(global_anchor.gold_tap_mixed_context_guard)
        chart = self.chart()
        trace = replace(chart.first, kind='trace', points=(Point(1., 8, 4, 6),))
        trace_anchor = self.anchor(replace(chart, gestures=(trace, *chart.gestures[1:])))
        self.assertFalse(trace_anchor.gold_tap_mixed_context_guard)
        self.assertTrue(trace_anchor.trace_opening_head_guard)
        self.assertTrue(trace_anchor.chart_bound_tracker.trace_heads)

    def test_calibration_signature_separates_requested_guard_and_master_override(self):
        from project_sekai.calibration import environment_signature, profile_key
        device = SimpleNamespace(preflight=lambda: None,
            controller=SimpleNamespace(info={'adb_serial': 'offline-test'}),
            shell=lambda _: 'versionName=offline-test')
        results = []
        for flag, master in (('0', '1'), ('1', '1'), ('1', '0'), (None, '1')):
            with patch.dict(os.environ, {'MAAPJSK_GOLD_TAP_MIXED_CONTEXT_GUARD': flag or '1',
                    'MAAPJSK_GOLD_TAP_HEAD_GUARD': master, 'MAAPJSK_GOLD_TAP_FOLLOWERS_GUARD': '1'}), patch(
                    'project_sekai.calibration.native_engine.module',
                    return_value=SimpleNamespace(version=lambda: '1.2.0')):
                if flag is None:
                    os.environ.pop('MAAPJSK_GOLD_TAP_MIXED_CONTEXT_GUARD', None)
                results.append(environment_signature(device))
        self.assertEqual([r['gold_tap_mixed_context_guard'] for r in results], [False, True, False, True])
        self.assertEqual(results[1], results[3])
        self.assertNotEqual(profile_key(results[0], 'expert'), profile_key(results[1], 'expert'))
        self.assertEqual(results[0]['start_anchor_version'], 28)


if __name__ == '__main__':
    unittest.main()
