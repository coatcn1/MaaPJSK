import json
import os
from pathlib import Path
import unittest

import cv2
import numpy as np

from project_sekai.chart_player import StartAnchor, first_note_y
from project_sekai.sus_chart import Gesture, Point, parse_sus


class BlueHeadAnchorTests(unittest.TestCase):
    gesture = Gesture((Point(2.0, 11, 3),), 'tap')

    def frame(self, *, core_hue=110, wrong_lane=False, clipped=False):
        hsv = np.zeros((720, 1280, 3), np.uint8)
        x = 900 if wrong_lane else 710
        y = 35 if clipped else 150
        # 背景蓝带接到首音外框，浅亮横芯仍保持完整。
        hsv[60:280, x+68:x+80] = (110, 200, 170)
        hsv[y:y+20, x:x+80] = (110, 200, 240)
        hsv[y+2:y+18, x+2:x+78] = (core_hue, 80 if core_hue else 0, 250)
        # 同色后继音满足旧门槛；回退必须与它比较，而不能只处理 None。
        hsv[99:110, 660:717] = (110, 200, 250)
        return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)

    def test_contaminated_blue_head_wins_over_valid_follower(self):
        self.assertEqual(first_note_y(self.frame(), self.gesture), 160.)

    def test_start_anchor_callsite_uses_head_candidate(self):
        anchor = StartAnchor(self.gesture, minimum_samples=4)
        anchor.observe(np.zeros((720, 1280, 3), np.uint8), 10.)
        anchor.observe(self.frame(), 10.1)
        self.assertEqual(anchor.samples, [(10.1, 160.)])

    def test_static_head_is_removed_by_baseline(self):
        frame = self.frame()
        self.assertIsNone(first_note_y(frame, self.gesture, frame))

    def test_baseline_half_core_cannot_shift_full_head_center(self):
        frame = self.frame()
        baseline = np.zeros_like(frame)
        baseline[152:160, 712:788] = frame[152:160, 712:788]
        self.assertEqual(first_note_y(frame, self.gesture, baseline), 104.5)

    def test_small_baseline_overlap_keeps_raw_complete_core_center(self):
        frame = self.frame()
        baseline = np.zeros_like(frame)
        baseline[152:155, 712:788] = frame[152:155, 712:788]
        self.assertEqual(first_note_y(frame, self.gesture, baseline), 160.)

    def test_white_wrong_lane_and_clipped_core_do_not_override_follower(self):
        for options in ({'core_hue': 0}, {'wrong_lane': True}, {'clipped': True}):
            with self.subTest(options=options):
                self.assertEqual(first_note_y(self.frame(**options), self.gesture), 104.5)

    def test_non_blue_gestures_do_not_use_core(self):
        for gesture in (Gesture((Point(2., 11, 3),), 'tap', flick=1),
                        Gesture((Point(2., 11, 3, kind=2),), 'tap', critical=True),
                        Gesture((Point(2., 11, 3), Point(3.,11,3)), 'slide')):
            with self.subTest(gesture=gesture):
                self.assertIsNone(first_note_y(self.frame(), gesture))

    def test_no_blue_source_and_sparse_core_are_rejected(self):
        for mode in ('no_source', 'sparse'):
            hsv = cv2.cvtColor(self.frame(), cv2.COLOR_BGR2HSV)
            if mode == 'no_source':
                hsv[150:170, 710:790] = (110, 20, 250)
            else:
                hsv[152:168, 712:788] = (110, 200, 170)
                hsv[152:168:3, 712:788] = (110, 80, 250)
                hsv[152:168, 712] = (110, 80, 250)
            with self.subTest(mode=mode):
                self.assertEqual(first_note_y(cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR), self.gesture), 104.5)

    def test_bottom_clipped_core_cannot_become_complete_head(self):
        hsv = np.zeros((720, 1280, 3), np.uint8)
        hsv[400:600, 1000:1020] = (110, 200, 170)
        hsv[520:552, 885:1110] = (110, 200, 240)
        hsv[522:550, 887:1108] = (110, 80, 250)
        self.assertIsNone(first_note_y(cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR), self.gesture))

    def test_later_contaminated_head_cannot_reset_established_trajectory(self):
        anchor = StartAnchor(self.gesture, minimum_samples=4)
        anchor.baseline = np.zeros((720, 1280, 3), np.uint8)
        anchor.samples = [(10., 400.), (10.1, 500.)]
        self.assertIsNone(anchor.observe(self.frame(), 10.2))
        self.assertEqual(anchor.samples, [(10., 400.), (10.1, 500.)])

    def test_private_original_frame_replay(self):
        # 私有实机帧只通过显式路径参与回放，测试与发行不携带截图。
        setting = os.environ.get('MAAPJSK_BLUE_HEAD_REPLAY_REPORT')
        if not setting:
            self.skipTest('未提供私有首蓝音故障报告')
        report_path = Path(setting)
        report = json.loads(report_path.read_text(encoding='utf-8'))
        chart_root = Path(__file__).resolve().parents[1] / 'resource' / 'charts'
        chart = parse_sus((chart_root / report['chart']['path']).read_text(encoding='utf-8'))
        frames = report['anchor_frames']
        images = [cv2.imread(str(report_path.parent / 'attempt-1' / row['path'])) for row in frames]
        self.assertTrue(all(image is not None for image in images))
        self.assertAlmostEqual(first_note_y(images[5], chart.first, images[0]), 157.5)
        anchor = StartAnchor(chart.first, minimum_samples=4)
        epoch = None
        for image, row in zip(images, frames):
            epoch = anchor.observe(image, row['captured_at'], capture_seconds=.03)
            if epoch is not None:
                break
        self.assertIsNotNone(epoch)
        self.assertEqual(row['path'], 'anchor-frame-13.png')
        self.assertLessEqual(anchor.fit['residual_pixels'], 12)
        self.assertGreater(anchor.fit['first_hit'] - row['captured_at'], .04)


if __name__ == '__main__':
    unittest.main()
