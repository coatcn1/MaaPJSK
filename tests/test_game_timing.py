import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from project_sekai.game_timing import (COMBO_AREA, JUDGEMENT_AREA, TIMING_AREA,
                                       GameTimingGuard, GameTimingTemplates)
from project_sekai.song_identity import write_image


def glyph(text):
    mask = np.zeros((33, 160 if text == 'PERFECT' else 145 if text == 'GREAT' else 75), np.uint8)
    cv2.putText(mask, text, (3, 25), cv2.FONT_HERSHEY_SIMPLEX, .65, 255, 2, cv2.LINE_8)
    return mask


def evidence(direction='fast', signature='1', judgement='great', reason='confirmed'):
    return {'direction': direction, 'judgement': judgement,
            'combo_signature': signature, 'reason': reason}


class GameTimingGuardTests(unittest.TestCase):
    def test_three_normal_intervals_adjust_only_future_phase_with_pjsk_sign(self):
        for direction, expected in [('fast', 5), ('late', -5)]:
            guard = GameTimingGuard()
            self.assertIsNone(guard.observe(evidence(direction, '1'), 1))
            self.assertIsNone(guard.observe(evidence(direction, '2'), 3))
            adjustment = guard.observe(evidence(direction, '3'), 5)
            self.assertEqual(guard.correction_ms, expected)
            self.assertEqual((adjustment['old_ms'], adjustment['new_ms'], adjustment['evidence_count']),
                             (0, expected, 3))
            self.assertEqual(guard.streak, 0)
            self.assertEqual(guard.report['correction_ms'], expected)

    def test_mixed_direction_starts_a_new_three_sample_streak(self):
        guard = GameTimingGuard()
        for index, direction in enumerate(['fast', 'fast', 'late', 'fast', 'fast']):
            self.assertIsNone(guard.observe(evidence(direction, str(index)), index * 2))
        self.assertEqual(guard.correction_ms, 0)
        self.assertIsNotNone(guard.observe(evidence('fast', 'last'), 10))
        self.assertEqual(guard.correction_ms, 5)

    def test_perfect_conflicts_and_unknown_life_clear_prior_window(self):
        invalid = [evidence(None, '3', 'perfect', 'perfect_confirmed'),
                   evidence(None, '3', 'great', 'direction_conflict'),
                   evidence(None, '3', None, 'judgement_conflict'),
                   evidence(None, None, None, 'life_hud_unknown'),
                   evidence(None, None, None, 'life_zero')]
        for rejected in invalid:
            guard = GameTimingGuard()
            guard.observe(evidence('fast', '1'), 0)
            guard.observe(evidence('fast', '2'), 2)
            self.assertIsNone(guard.observe(rejected, 4))
            self.assertEqual(guard.streak, 0)
            guard.observe(evidence('fast', '4'), 6)
            self.assertIsNone(guard.observe(evidence('fast', '5'), 8))
            self.assertEqual(guard.correction_ms, 0)
            guard.observe(evidence('fast', '6'), 10)
            self.assertEqual(guard.correction_ms, 5)

    def test_disappeared_text_and_unconfirmed_combo_do_not_vote_but_preserve_window(self):
        unknowns = [evidence(None, None, None, 'great_unconfirmed'),
                    evidence(None, None, 'great', 'direction_unconfirmed'),
                    evidence(None, None, 'great', 'combo_unconfirmed')]
        for unknown in unknowns:
            guard = GameTimingGuard()
            guard.observe(evidence('fast', '1'), 0)
            self.assertIsNone(guard.observe(unknown, 2))
            self.assertEqual(guard.streak, 1)
            guard.observe(evidence('fast', '2'), 4)
            self.assertIsNone(guard.observe(unknown, 6))
            self.assertEqual(guard.streak, 2)
            adjustment = guard.observe(evidence('fast', '3'), 8)
            self.assertEqual(guard.correction_ms, 5)
            self.assertEqual([row['elapsed_s'] for row in adjustment['evidence']], [0, 4, 8])

    def test_window_expires_old_votes_despite_regular_unknown_observations(self):
        guard = GameTimingGuard()
        guard.observe(evidence('fast', 'old-1'), 0)
        guard.observe(evidence('fast', 'old-2'), 2)
        for elapsed in range(4, 16, 2):
            guard.observe(evidence(None, None, None, 'great_unconfirmed'), elapsed)
        self.assertIsNone(guard.observe(evidence('fast', 'new-1'), 16))
        self.assertEqual(guard.streak, 1)
        self.assertEqual(guard.correction_ms, 0)
        guard.observe(evidence('fast', 'new-2'), 18)
        adjustment = guard.observe(evidence('fast', 'new-3'), 20)
        self.assertEqual([row['combo_signature'] for row in adjustment['evidence']], ['new-1', 'new-2', 'new-3'])

    def test_twelve_second_boundary_is_inclusive(self):
        guard = GameTimingGuard()
        for elapsed in range(0, 14, 2):
            item = evidence('fast', str(elapsed)) if elapsed in (0, 6, 12) else evidence(None, None, None, 'great_unconfirmed')
            adjustment = guard.observe(item, elapsed)
        self.assertEqual(guard.correction_ms, 5)
        self.assertEqual([row['elapsed_s'] for row in adjustment['evidence']], [0, 6, 12])

    def test_adjustments_consume_votes_and_respect_six_second_cooldown(self):
        guard = GameTimingGuard()
        for index in range(3):
            first = guard.observe(evidence('fast', f'first-{index}'), index * 2)
        self.assertEqual(first['elapsed_s'], 4)
        self.assertEqual(guard.correction_ms, 5)
        self.assertIsNone(guard.observe(evidence('fast', 'second-1'), 6))
        self.assertIsNone(guard.observe(evidence('fast', 'second-2'), 8))
        self.assertEqual(guard.correction_ms, 5)
        second = guard.observe(evidence('fast', 'second-3'), 10)
        self.assertEqual(second['elapsed_s'] - first['elapsed_s'], 6)
        self.assertEqual(guard.correction_ms, 10)
        first_signatures = {row['combo_signature'] for row in first['evidence']}
        second_signatures = {row['combo_signature'] for row in second['evidence']}
        self.assertTrue(first_signatures.isdisjoint(second_signatures))
        self.assertEqual([row['direction'] for row in second['evidence']], ['fast'] * 3)

    def test_opposite_valid_direction_discards_prior_window_across_text_gaps(self):
        guard = GameTimingGuard()
        guard.observe(evidence('fast', 'fast-1'), 0)
        guard.observe(evidence('fast', 'fast-2'), 2)
        guard.observe(evidence('late', 'late-1'), 4)
        self.assertEqual(guard.streak, 1)
        guard.observe(evidence(None, None, None, 'direction_unconfirmed'), 6)
        self.assertIsNone(guard.observe(evidence('late', 'late-2'), 8))
        adjustment = guard.observe(evidence('late', 'late-3'), 10)
        self.assertEqual(guard.correction_ms, -5)
        self.assertEqual([row['combo_signature'] for row in adjustment['evidence']], ['late-1', 'late-2', 'late-3'])
        self.assertEqual([row['direction'] for row in adjustment['evidence']], ['late'] * 3)

    def test_same_frame_short_interval_and_backwards_time_do_not_accumulate(self):
        guard = GameTimingGuard()
        guard.observe(evidence('fast', '1'), 10)
        for elapsed in (10, 11, 9, float('nan'), float('inf')):
            self.assertIsNone(guard.observe(evidence('fast', 'different'), elapsed))
        self.assertEqual((guard.streak, len(guard.report['observations'])), (1, 1))
        guard.observe(evidence('fast', '2'), 12)
        self.assertEqual(guard.streak, 2)
        self.assertEqual(guard.correction_ms, 0)

    def test_unchanged_combo_cannot_accumulate_even_across_normal_intervals(self):
        guard = GameTimingGuard()
        for elapsed in range(0, 12, 2):
            self.assertIsNone(guard.observe(evidence('fast', 'same-combo'), elapsed))
        self.assertEqual(guard.correction_ms, 0)
        self.assertEqual(guard.report['latest_observation']['reason'], 'unchanged_combo')

    def test_unknown_frame_does_not_make_old_combo_fresh_again(self):
        guard = GameTimingGuard()
        guard.observe(evidence('fast', 'old'), 0)
        guard.observe(evidence(None, None, None, 'great_unconfirmed'), 2)
        guard.observe(evidence('fast', 'old'), 4)
        self.assertEqual(guard.streak, 0)
        self.assertEqual(guard.report['latest_observation']['reason'], 'unchanged_combo')
        guard.observe(evidence('fast', 'new-1'), 6)
        self.assertIsNone(guard.observe(evidence('fast', 'new-2'), 8))
        self.assertEqual(guard.correction_ms, 0)
        guard.observe(evidence('fast', 'new-3'), 10)
        self.assertEqual(guard.correction_ms, 5)

    def test_long_gap_requires_three_fresh_normal_samples(self):
        guard = GameTimingGuard()
        guard.observe(evidence('fast', '1'), 0)
        guard.observe(evidence('fast', '2'), 2)
        self.assertIsNone(guard.observe(evidence('fast', '3'), 6))
        self.assertEqual(guard.streak, 1)
        self.assertIsNone(guard.observe(evidence('fast', '4'), 8))
        self.assertEqual(guard.correction_ms, 0)
        guard.observe(evidence('fast', '5'), 10)
        self.assertEqual(guard.correction_ms, 5)

    def test_cumulative_limit_reverse_recovery_and_new_round_are_independent(self):
        for direction, expected, reverse in [('fast', 60, 'late'), ('late', -60, 'fast')]:
            guard = GameTimingGuard()
            for index in range(42):
                guard.observe(evidence(direction, str(index)), index * 2)
                self.assertLessEqual(abs(guard.correction_ms), 60)
            self.assertEqual(guard.correction_ms, expected)
            self.assertEqual(len(guard.report['adjustments']), 12)
            self.assertTrue(all(abs(row['new_ms'] - row['old_ms']) == 5 for row in guard.report['adjustments']))
            for index in range(42, 45):
                guard.observe(evidence(reverse, str(index)), index * 2)
            self.assertEqual(guard.correction_ms, expected - (5 if expected > 0 else -5))
            new_round = GameTimingGuard()
            self.assertEqual(new_round.correction_ms, 0)
            self.assertEqual(new_round.report['adjustments'], [])
            self.assertEqual(new_round.report['observations'], [])

    def test_long_round_observation_storage_is_bounded_without_changing_feedback(self):
        guard = GameTimingGuard()
        for index in range(4100):
            guard.observe(evidence('fast', str(index)), index * 2)
        self.assertEqual(len(guard.report['observations']), 4096)
        self.assertEqual(guard.report['dropped_observations'], 4)
        self.assertEqual(guard.correction_ms, 60)
        self.assertEqual(guard.report['latest_observation']['elapsed_s'], 8198)


class GameTimingTemplateTests(unittest.TestCase):
    def setUp(self):
        self.masks = {name: glyph(name.upper()) for name in ('fast', 'late', 'great', 'perfect')}
        self.detector = GameTimingTemplates(self.masks)

    def frame(self, direction='fast', *, judgement='great', combo='123'):
        frame = np.zeros((720, 1280, 3), np.uint8)
        for name, area, color in [(direction, TIMING_AREA, (255, 255, 255)),
                                  (judgement, JUDGEMENT_AREA, (230, 220, 255) if judgement == 'perfect' else (255, 80, 255))]:
            if name in self.masks:
                x, y, _, _ = area
                mask = self.masks[name]
                crop = frame[y + 2:y + 2 + mask.shape[0], x + 4:x + 4 + mask.shape[1]]
                crop[mask > 0] = color
        if combo is not None:
            x, y, _, _ = COMBO_AREA
            cv2.putText(frame, combo, (x + 10, y + 70), cv2.FONT_HERSHEY_SIMPLEX, 2,
                        (255, 255, 255), 4, cv2.LINE_8)
        return frame

    def test_synthetic_fast_late_with_great_and_combo_pass_fixed_hud_match(self):
        for direction in ('fast', 'late'):
            observed = self.detector.observe(self.frame(direction))
            self.assertEqual(observed['direction'], direction)
            self.assertEqual(observed['judgement'], 'great')
            self.assertEqual(observed['reason'], 'confirmed')
            self.assertIsNotNone(observed['combo_signature'])

    def test_direction_or_great_alone_and_missing_combo_cannot_change_phase(self):
        cases = [self.frame(judgement=None), self.frame(direction=None), self.frame(combo=None)]
        for frame in cases:
            guard = GameTimingGuard()
            for index in range(3):
                observed = self.detector.observe(frame)
                self.assertIsNone(observed['direction'])
                guard.observe(observed, index * 2)
            self.assertEqual(guard.correction_ms, 0)

    def test_both_directions_above_threshold_conflict_even_with_large_score_margin(self):
        scores = iter([1., .91, 1., 0.])
        with patch.object(self.detector, '_score', side_effect=lambda *_: next(scores)):
            observed = self.detector.observe(self.frame())
        self.assertIsNone(observed['direction'])
        self.assertNotEqual(observed['reason'], 'confirmed')

    def test_perfect_template_confirms_without_direction_and_interrupts_prior_votes(self):
        observed = self.detector.observe(self.frame(direction=None, judgement='perfect'))
        self.assertIsNone(observed['direction'])
        self.assertEqual(observed['judgement'], 'perfect')
        self.assertEqual(observed['reason'], 'perfect_confirmed')
        guard = GameTimingGuard()
        guard.observe(evidence('fast', '1'), 0)
        guard.observe(evidence('fast', '2'), 2)
        guard.observe(observed, 4)
        self.assertEqual(guard.streak, 0)
        self.assertEqual(guard.correction_ms, 0)

    def test_great_perfect_both_above_threshold_are_conflicting_judgements(self):
        scores = iter([1., 0., 1., .95])
        with patch.object(self.detector, '_score', side_effect=lambda *_: next(scores)):
            observed = self.detector.observe(self.frame())
        self.assertIsNone(observed['direction'])
        self.assertIsNone(observed['judgement'])
        self.assertEqual(observed['reason'], 'judgement_conflict')

    def test_same_hud_combo_signature_is_stable_but_changed_number_is_distinct(self):
        first = self.detector.observe(self.frame(combo='123'))
        same = self.detector.observe(self.frame(combo='123'))
        different = self.detector.observe(self.frame(combo='124'))
        self.assertEqual(first['combo_signature'], same['combo_signature'])
        self.assertNotEqual(first['combo_signature'], different['combo_signature'])

    def test_unsupported_size_and_ambiguous_direction_scores_fail_closed(self):
        observed = self.detector.observe(np.zeros((360, 640, 3), np.uint8))
        self.assertIsNone(observed['direction'])
        for score_values in ([.89, .1, 1., 0.], [.95, .91, 1., 0.], [1., 0., .89, 0.]):
            scores = iter(score_values)
            with patch.object(self.detector, '_score', side_effect=lambda *_: next(scores)):
                observed = self.detector.observe(self.frame())
            self.assertIsNone(observed['direction'])

    def test_missing_degenerate_nonbinary_and_bad_dimensions_are_rejected(self):
        with self.assertRaises(ValueError):
            GameTimingTemplates({'fast': self.masks['fast']})
        with self.assertRaises(ValueError):
            GameTimingTemplates({name: mask for name, mask in self.masks.items() if name != 'perfect'})
        for invalid in [np.zeros((10, 10), np.uint8), np.full((10, 10), 255, np.uint8),
                        np.full((10, 10), 127, np.uint8), np.ones((2, 40), np.uint8) * 255,
                        np.zeros((100, 100), np.uint8), np.zeros((10, 10, 3), np.uint8)]:
            with self.assertRaises(ValueError):
                GameTimingTemplates({**self.masks, 'fast': invalid})
        for threshold, margin in [(.89, .08), (1.1, .08), (.9, .07), (float('nan'), .08)]:
            with self.assertRaises(ValueError):
                GameTimingTemplates(self.masks, threshold, margin)

    def test_non_uint8_template_is_rejected_before_opencv_matching(self):
        with self.assertRaises(ValueError):
            GameTimingTemplates({**self.masks, 'fast': self.masks['fast'].astype(np.float32)})

    def test_config_load_roundtrip_missing_image_and_bad_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, mask in self.masks.items():
                write_image(root / f'{name}.png', mask)
            config = {'schema_version': 1, 'templates': {name: f'{name}.png' for name in self.masks}}
            path = root / 'config.json'
            path.write_text(json.dumps(config))
            loaded = GameTimingTemplates.load(path)
            self.assertEqual(loaded.observe(self.frame())['direction'], 'fast')
            config['templates']['late'] = 'missing.png'
            path.write_text(json.dumps(config))
            with self.assertRaises(FileNotFoundError):
                GameTimingTemplates.load(path)
            config['schema_version'] = 99
            path.write_text(json.dumps(config))
            with self.assertRaises(ValueError):
                GameTimingTemplates.load(path)

    def test_config_rejects_colored_image_that_only_blue_channel_is_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, mask in self.masks.items():
                image = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
                if name == 'fast':
                    image[:, :, 1] = 80
                write_image(root / f'{name}.png', image)
            path = root / 'config.json'
            path.write_text(json.dumps({'schema_version': 1,
                                        'templates': {name: f'{name}.png' for name in self.masks}}))
            with self.assertRaises(ValueError):
                GameTimingTemplates.load(path)


if __name__ == '__main__':
    unittest.main()
