import math
import unittest
from unittest.mock import patch

import numpy as np

from project_sekai.chart_player import StartAnchor
from project_sekai.sus_chart import Gesture, Point


# 实际 EARLY 局的首音轨迹，仅保留相对时间与像素；尾部五帧会把起点提前约 60 ms。
EARLY_TRAJECTORY = (
    (0.0, 44.0), (.0627959, 49.5), (.0929299, 53.0), (.1263409, 57.0),
    (.1554397, 60.5), (.1869278, 65.5), (.2186862, 69.5), (.2503611, 73.5),
    (.2825508, 78.0), (.3440513, 85.0), (.3759274, 89.5), (.4077863, 95.5),
    (.4369402, 101.5), (.4703528, 106.5), (.5013843, 113.0), (.5304523, 116.0),
    (.5622719, 123.0), (.5940791, 130.0), (.6258061, 139.0), (.6571688, 145.5),
    (.6872413, 152.5), (.7188601, 161.5), (.7494170, 172.0), (.7825716, 179.0),
    (.8132626, 183.5), (.8427714, 192.5), (.8753527, 202.5), (.9067609, 213.0),
    (.9384600, 227.5), (.9699459, 236.0),
)


class AnchorStabilityTests(unittest.TestCase):
    def test_single_candidate_timeout_still_rejects_without_claiming_first_note_passed(self):
        anchor=StartAnchor(Gesture((Point(0,8,3),),'tap'),minimum_samples=4)
        anchor.baseline=np.zeros((1,1,3),np.uint8)
        anchor.samples=[(10.,220.)]
        with patch('project_sekai.chart_player.first_note_y') as detect:
            with self.assertRaisesRegex(RuntimeError,'候选未确认运动'):
                anchor.observe(anchor.baseline,12.01)
        detect.assert_not_called()
        self.assertEqual(anchor.samples,[(10.,220.)])

    def test_motion_trajectory_timeout_remains_rejected(self):
        anchor=StartAnchor(Gesture((Point(0,8,3),),'tap'),minimum_samples=4)
        anchor.baseline=np.zeros((1,1,3),np.uint8)
        anchor.samples=[(10.,100.),(10.1,120.)]
        with self.assertRaisesRegex(RuntimeError,'轨迹未通过同步门槛'):
            anchor.observe(anchor.baseline,12.01)
        self.assertEqual(anchor.failure_reason,'trajectory_gate_timeout')

    def replay(self, samples, *, capture_seconds=.022, minimum_samples=4):
        anchor = StartAnchor(Gesture((Point(0, 8, 3),), "tap"), minimum_samples=minimum_samples)
        frame = np.zeros((1, 1, 3), np.uint8)
        anchor.observe(frame, 9)
        epoch = None
        with patch("project_sekai.chart_player.first_note_y", side_effect=[y for _, y in samples]):
            for when, _ in samples:
                epoch = anchor.observe(frame, 10 + when, capture_seconds=capture_seconds)
                if epoch is not None:
                    break
        return anchor, epoch

    def test_saved_early_trajectory_uses_available_motion_instead_of_noisy_tail(self):
        anchor, epoch = self.replay(EARLY_TRAJECTORY)
        self.assertIsNotNone(epoch)
        self.assertAlmostEqual(epoch, 11.588971, places=5)
        self.assertEqual(anchor.fit["model"], "perspective")
        self.assertEqual(anchor.fit["sample_count"], 30)
        self.assertLess(anchor.fit["residual_pixels"], 4)
        self.assertEqual(len(anchor.samples), 30)

    def test_clean_fast_trajectory_keeps_its_true_phase(self):
        rate = 1.35
        samples = [(index * .032, 74 * math.exp(rate * index * .032) - 30) for index in range(32)]
        anchor, epoch = self.replay(samples)
        self.assertAlmostEqual(epoch, 10 + math.log(600 / 74) / rate, places=8)
        self.assertEqual(anchor.fit["model"], "perspective")

    def test_slow_link_keeps_existing_four_sample_model_and_phase(self):
        anchor, epoch = self.replay([(0, 97), (.156, 146.5), (.304, 210), (.460, 299)], capture_seconds=.14)
        self.assertIsNotNone(epoch)
        self.assertEqual(anchor.fit["sample_count"], 4)
        self.assertFalse(anchor.fit["short_window"])
        self.assertLess(anchor.fit["residual_pixels"], 4)

    def test_fast_four_samples_do_not_wait_for_wide_window(self):
        anchor, epoch = self.replay([(0, 97), (.05, 146.5), (.10, 210), (.15, 299)])
        self.assertIsNotNone(epoch)
        self.assertEqual(anchor.fit["sample_count"], 4)
        self.assertEqual(anchor.fit["fit_window"], "recent")

    def test_three_samples_wait_when_next_capture_still_fits(self):
        anchor, epoch = self.replay([(0, 56.5), (.282, 124.5), (.562, 243.5)], capture_seconds=.02)
        self.assertIsNone(epoch)
        self.assertIsNone(anchor.fit)

    def test_three_samples_reject_when_capture_has_consumed_start_margin(self):
        anchor, epoch = self.replay([(0, 56.5), (.282, 124.5), (.562, 243.5)], capture_seconds=.45)
        self.assertIsNone(epoch)
        self.assertIsNone(anchor.fit)

    def test_native_does_not_use_two_sample_fallback(self):
        anchor, epoch = self.replay([(0, 72), (.311, 165)], capture_seconds=.3)
        self.assertIsNone(epoch)
        self.assertIsNone(anchor.fit)

    def test_inconsistent_wide_trajectory_does_not_fall_back_to_clean_tail(self):
        samples = [(index * .032, 74 * math.exp(1.35 * index * .032) - 30) for index in range(32)]
        samples = [(t, y + (35 if 10 <= index <= 20 else 0)) for index, (t, y) in enumerate(samples)]
        anchor, epoch = self.replay(samples)
        self.assertIsNone(epoch)
        self.assertIsNone(anchor.fit)

    def test_wide_fit_is_bounded_by_sample_count(self):
        anchor = StartAnchor(Gesture((Point(0, 8, 3),), "tap"), minimum_samples=4)
        frame = np.zeros((1, 1, 3), np.uint8)
        anchor.observe(frame, 9)
        samples = [(10 + index * .024, 80 * math.exp(1.4 * index * .024) - 30) for index in range(45)]
        anchor.samples = samples[:-1]
        with patch("project_sekai.chart_player.first_note_y", return_value=samples[-1][1]):
            epoch = anchor.observe(frame, samples[-1][0], capture_seconds=.02)
        self.assertIsNotNone(epoch)
        self.assertEqual(anchor.fit["sample_count"], 32)
        self.assertAlmostEqual(anchor.fit["window_seconds"], 31 * .024)

    def test_wide_fit_is_bounded_by_time_span(self):
        anchor = StartAnchor(Gesture((Point(0, 8, 3),), "tap"), minimum_samples=4)
        frame = np.zeros((1, 1, 3), np.uint8)
        anchor.observe(frame, 9)
        samples = [(10 + index * .05, 60 * math.exp(1.3 * index * .05) - 30) for index in range(30)]
        anchor.samples = samples[:-1]
        with patch("project_sekai.chart_player.first_note_y", return_value=samples[-1][1]):
            epoch = anchor.observe(frame, samples[-1][0], capture_seconds=.02)
        self.assertIsNotNone(epoch)
        self.assertEqual(anchor.fit["sample_count"], 26)
        self.assertLessEqual(anchor.fit["window_seconds"], 1.25)

    def test_anchor_still_refuses_to_start_after_first_note_window(self):
        anchor = StartAnchor(Gesture((Point(0, 8, 3),), "tap"), minimum_samples=4)
        frame = np.zeros((1, 1, 3), np.uint8)
        anchor.observe(frame, 9)
        with patch("project_sekai.chart_player.first_note_y", return_value=50):
            anchor.observe(frame, 10)
            with self.assertRaisesRegex(RuntimeError, "拒绝从歌曲中途开始"):
                anchor.observe(frame, 12.001)


if __name__ == "__main__":
    unittest.main()
