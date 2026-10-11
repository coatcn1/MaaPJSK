import math
import os
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

# 2026-10-10 失败报告的完整数值轨迹；没有原始帧，不能证明检测位置或模型的唯一根因。
AMBIGUOUS_TRAJECTORY = (
    (0., 44.), (.0304278, 47.5), (.0787843, 52.5), (.1096976, 56.5),
    (.1569371, 63.5), (.2192610, 69.5), (.2812988, 77.), (.3125652, 88.),
    (.4063928, 98.5), (.4692640, 105.5), (.5015061, 114.5), (.5311021, 122.5),
    (.5782212, 131.5), (.6095437, 137.5), (.6393981, 145.5), (.6721947, 155.5),
    (.7025121, 160.), (.7486206, 171.5), (.7817834, 182.5), (.8287272, 197.),
    (.8597456, 216.), (.9233556, 238.5),
)


class AnchorStabilityTests(unittest.TestCase):
    def test_upward_background_candidate_never_becomes_motion_trajectory(self):
        frame = np.zeros((1, 1, 3), np.uint8)
        anchor = StartAnchor(Gesture((Point(0, 8, 3),), 'tap'), minimum_samples=4)
        anchor.observe(frame, 9.)
        with patch('project_sekai.chart_player.first_note_y', side_effect=[213., 209., *[74 * math.exp(3 * dt) - 30 for dt in (0., .2, .4, .6)]]):
            anchor.observe(frame, 10.)
            anchor.observe(frame, 10.266)
            self.assertEqual(anchor.samples, [(10., 213.)])
            anchor.observe(frame, 10.5)
            self.assertEqual(anchor.samples, [(10.5, 44.)])
            for when in [10.7, 10.9, 11.1]:
                epoch = anchor.observe(frame, when, capture_seconds=.02)
        self.assertIsNotNone(epoch)
        self.assertEqual(len(anchor.samples), 4)

    def test_high_single_candidate_cannot_restart_from_later_note(self):
        frame = np.zeros((1, 1, 3), np.uint8)
        anchor = StartAnchor(Gesture((Point(0, 8, 3),), 'tap'), minimum_samples=4)
        anchor.baseline = frame
        anchor.samples = [(10.1, 300.)]
        with patch('project_sekai.chart_player.first_note_y', side_effect=[200., 260., 340., 440.]):
            self.assertIsNone(anchor.observe(frame, 10.5, capture_seconds=.03))
            self.assertEqual(anchor.samples, [(10.1, 300.)])
            for when in (10.6, 10.7, 10.8):
                self.assertIsNone(anchor.observe(frame, when, capture_seconds=.03))
        with self.assertRaisesRegex(RuntimeError, '轨迹未通过'):
            anchor.observe(frame, 12.11)

    def test_established_downward_motion_cannot_be_reset_by_upward_noise(self):
        frame = np.zeros((1, 1, 3), np.uint8)
        anchor = StartAnchor(Gesture((Point(0, 8, 3),), 'tap'), minimum_samples=4)
        anchor.baseline = frame
        anchor.samples = [(10., 100.), (10.1, 120.)]
        with patch('project_sekai.chart_player.first_note_y', return_value=116.):
            anchor.observe(frame, 10.2)
        self.assertEqual(anchor.samples, [(10., 100.), (10.1, 120.)])
        with self.assertRaisesRegex(RuntimeError, '轨迹未通过'):
            anchor.observe(frame, 12.01)

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

    def replay(self, samples, *, capture_seconds=.022, minimum_samples=4, guard=True, legacy_trial=None):
        with patch.dict(os.environ, {}):
            os.environ.pop("MAAPJSK_START_ANCHOR_AMBIGUITY_GUARD", None)
            os.environ.pop("MAAPJSK_START_ANCHOR_AMBIGUITY_TRIAL", None)
            if guard is not None:
                os.environ["MAAPJSK_START_ANCHOR_AMBIGUITY_GUARD"] = ("1" if guard else "0") if isinstance(guard, bool) else guard
            if legacy_trial is not None:
                os.environ["MAAPJSK_START_ANCHOR_AMBIGUITY_TRIAL"] = legacy_trial
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

    def test_guard_defaults_on_with_explicit_primary_and_legacy_overrides(self):
        cases = (
            (None, None, True), ("", None, True), (None, "", True), ("", "", True),
            ("invalid", "invalid", True), (False, None, False), (True, None, True),
            (False, "1", False), (True, "0", True), (None, "0", False), (None, "1", True),
            ("", "0", False), ("", "1", True),
        )
        for setting, legacy, expected in cases:
            with self.subTest(guard=setting, legacy=legacy):
                anchor, epoch = self.replay(AMBIGUOUS_TRAJECTORY, capture_seconds=.0299101,
                                            guard=setting, legacy_trial=legacy)
                self.assertEqual(anchor.ambiguity_guard, expected)
                if expected:
                    self.assertIsNone(epoch)
                    self.assertIsNotNone(anchor.ambiguity_defer)
                else:
                    self.assertAlmostEqual(epoch, 11.41030696, places=7)
                    self.assertEqual(anchor.fit["model"], "exponential")
                    self.assertIsNone(anchor.ambiguity_defer)

    def test_saved_ambiguous_trajectory_explicitly_disabled_keeps_original_model(self):
        anchor, epoch = self.replay(AMBIGUOUS_TRAJECTORY, capture_seconds=.0299101, guard=False)
        self.assertAlmostEqual(epoch, 11.41030696, places=7)
        self.assertEqual(anchor.fit["model"], "exponential")
        candidates = anchor.fit["candidates"]
        self.assertAlmostEqual(candidates[1]["first_hit"] - candidates[0]["first_hit"], .1292091, places=6)

    def test_saved_ambiguous_trajectory_guard_waits_for_actual_new_samples(self):
        anchor, epoch = self.replay(AMBIGUOUS_TRAJECTORY, capture_seconds=.0299101, guard=True)
        self.assertIsNone(epoch)
        self.assertIsNone(anchor.fit)
        self.assertEqual(len(anchor.samples), 22)
        self.assertEqual(anchor.ambiguity_defer["defer_count"], 1)
        self.assertEqual(anchor.ambiguity_defer["new_sample_count"], 0)
        self.assertAlmostEqual(anchor.ambiguity_defer["max_disagreement_ms"], 129.2091, places=4)

    def test_ambiguous_guard_follows_new_numerical_evidence_for_either_model(self):
        # 后续点是公开数值轨迹的合成延续，分别核验两种可能，不能当作真实截图验收。
        offsets = np.array([t for t, _ in AMBIGUOUS_TRAJECTORY])
        heights = np.array([y for _, y in AMBIGUOUS_TRAJECTORY])
        for model, shift, expected_new in (("perspective", 30, 1), ("exponential", 0, 3)):
            with self.subTest(model=model):
                velocity, intercept = np.polyfit(offsets, np.log(heights + shift), 1)
                expected_hit = 10 + (math.log(570 + shift) - intercept) / velocity
                future = [(offsets[-1] + .032 * i, float(np.exp(velocity * (offsets[-1] + .032 * i) + intercept) - shift))
                          for i in range(1, 5)]
                anchor, epoch = self.replay([*AMBIGUOUS_TRAJECTORY, *future], capture_seconds=.0299101, guard=True)
                self.assertAlmostEqual(epoch, expected_hit, places=8)
                self.assertEqual(anchor.fit["model"], model)
                summary = anchor.fit["ambiguity_guard"]
                self.assertTrue(summary["deferred"])
                self.assertEqual(summary["status"], "resolved")
                self.assertEqual(summary["new_sample_count"], expected_new)
                self.assertGreater(epoch - anchor.samples[-1][0], .0299101 + .080)
                self.assertEqual(anchor.fit["sample_count"], 22 + expected_new)

    def test_ambiguous_guard_rejects_when_next_capture_consumes_margin(self):
        anchor, _ = self.replay(AMBIGUOUS_TRAJECTORY[:-1], capture_seconds=.43, guard=True)
        with patch("project_sekai.chart_player.first_note_y", return_value=AMBIGUOUS_TRAJECTORY[-1][1]):
            with self.assertRaisesRegex(RuntimeError, "模型时刻仍有歧义且启动余量不足"):
                anchor.observe(anchor.baseline, 10 + AMBIGUOUS_TRAJECTORY[-1][0], capture_seconds=.43)
        self.assertEqual(anchor.failure_reason, "trajectory_model_ambiguity_margin")
        self.assertEqual(anchor.ambiguity_defer["status"], "rejected_margin")
        self.assertIsNone(anchor.fit)

    def test_deferred_guard_keeps_early_quality_model_inside_forty_ms(self):
        anchor, _ = self.replay(AMBIGUOUS_TRAJECTORY, capture_seconds=.0299101, guard=True)
        when = AMBIGUOUS_TRAJECTORY[-1][0] + .032
        offsets = np.array([t for t, _ in AMBIGUOUS_TRAJECTORY])
        heights = np.array([y for _, y in AMBIGUOUS_TRAJECTORY])
        velocity, intercept = np.polyfit(offsets, np.log(heights), 1)
        new_y = float(np.exp(velocity * when + intercept))
        extended = [*AMBIGUOUS_TRAJECTORY, (when, new_y)]
        x = np.array([t for t, _ in extended])
        y = np.array([height for _, height in extended])
        captured_at = 10 + when
        projected_logs = []
        for shift, remaining in ((0, .020), (30, .150)):
            velocity, intercept = np.polyfit(x, np.log(y + shift), 1)
            projected_logs.append(intercept + velocity * (when + remaining))

        # 单独注入预测时刻边界，保留真实数值样本的拟合与高残差；不冒充物理轨迹或设备帧。
        # 关闭候选的对照会仅保留迟模型，证明下面的拒绝依赖早模型仍参加歧义检查。
        with patch.dict(os.environ, {"MAAPJSK_START_ANCHOR_AMBIGUITY_GUARD": "0"}):
            control = StartAnchor(anchor.gesture, minimum_samples=4)
        control.baseline = anchor.baseline
        control.samples = list(anchor.samples)
        with patch("project_sekai.chart_player.first_note_y", return_value=new_y):
            with patch("project_sekai.chart_player.math.log", side_effect=projected_logs):
                self.assertAlmostEqual(control.observe(control.baseline, captured_at, capture_seconds=.0299101), captured_at + .150)
            self.assertEqual(control.fit["model"], "perspective")
            self.assertEqual(len(control.fit["candidates"]), 1)
            self.assertGreater(control.fit["residual_pixels"], 4)
            with patch("project_sekai.chart_player.math.log", side_effect=projected_logs):
                with self.assertRaisesRegex(RuntimeError, "模型时刻仍有歧义且启动余量不足"):
                    anchor.observe(anchor.baseline, captured_at, capture_seconds=.0299101)
        self.assertEqual(anchor.failure_reason, "trajectory_model_ambiguity_margin")
        self.assertEqual(anchor.ambiguity_defer["status"], "rejected_margin")
        self.assertAlmostEqual(anchor.ambiguity_defer["earliest_hit"] - captured_at, .020)
        self.assertAlmostEqual(anchor.ambiguity_defer["latest_hit"] - captured_at, .150)
        self.assertIsNone(anchor.fit)

    def test_guard_freezes_environment_at_initialization(self):
        anchor, _ = self.replay(AMBIGUOUS_TRAJECTORY[:-1], guard=None)
        with patch.dict(os.environ, {"MAAPJSK_START_ANCHOR_AMBIGUITY_GUARD": "0"}):
            with patch("project_sekai.chart_player.first_note_y", return_value=AMBIGUOUS_TRAJECTORY[-1][1]):
                self.assertIsNone(anchor.observe(anchor.baseline, 10 + AMBIGUOUS_TRAJECTORY[-1][0], capture_seconds=.0299101))
        self.assertTrue(anchor.ambiguity_guard)
        anchor, _ = self.replay(AMBIGUOUS_TRAJECTORY[:-1], guard=False)
        with patch.dict(os.environ, {"MAAPJSK_START_ANCHOR_AMBIGUITY_GUARD": "1"}):
            with patch("project_sekai.chart_player.first_note_y", return_value=AMBIGUOUS_TRAJECTORY[-1][1]):
                self.assertIsNotNone(anchor.observe(anchor.baseline, 10 + AMBIGUOUS_TRAJECTORY[-1][0], capture_seconds=.0299101))
        self.assertFalse(anchor.ambiguity_guard)

    def test_deferred_guard_keeps_original_two_second_timeout_without_new_detection(self):
        anchor, _ = self.replay(AMBIGUOUS_TRAJECTORY, capture_seconds=.0299101, guard=True)
        with patch("project_sekai.chart_player.first_note_y") as detect:
            with self.assertRaisesRegex(RuntimeError, "轨迹未通过同步门槛且等待超时"):
                anchor.observe(anchor.baseline, 12.001, capture_seconds=.0299101)
        detect.assert_not_called()
        self.assertEqual(anchor.failure_reason, "trajectory_gate_timeout")
        self.assertIsNone(anchor.fit)

    def test_guard_keeps_stable_fast_slow_and_low_sample_paths(self):
        cases = [
            (EARLY_TRAJECTORY, .022, 4),
            ([(index * .032, 74 * math.exp(1.35 * index * .032) - 30) for index in range(32)], .022, 4),
            ([(0, 97), (.156, 146.5), (.304, 210), (.460, 299)], .14, 4),
            ([(0, 97), (.05, 146.5), (.10, 210), (.15, 299)], .022, 4),
            ([(0, 56.5), (.282, 124.5), (.562, 243.5)], .02, 4),
            ([(0, 56.5), (.282, 124.5), (.562, 243.5)], .3, 4),
            ([(0, 72), (.311, 165)], .3, 4),
            ([(0, 72), (.311, 165)], .3, 2),
            ([(index * .032, 74 * math.exp(1.35 * index * .032) - 30 + (35 if 10 <= index <= 20 else 0))
              for index in range(32)], .022, 4),
        ]
        for samples, cost, minimum in cases:
            with self.subTest(samples=len(samples), cost=cost, minimum=minimum):
                baseline, expected = self.replay(samples, capture_seconds=cost, minimum_samples=minimum, guard=False)
                guard, actual = self.replay(samples, capture_seconds=cost, minimum_samples=minimum, guard=True)
                self.assertEqual(expected, actual)
                self.assertEqual(baseline.samples, guard.samples)
                self.assertIsNone(guard.ambiguity_defer)
                if expected is not None:
                    self.assertEqual(baseline.fit["model"], guard.fit["model"])

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
