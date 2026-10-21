from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cv2

from project_sekai import chart_player
from project_sekai.chart_catalog import ChartRepository
from project_sekai.sus_chart import Gesture, Point, parse_sus


FLAG = 'MAAPJSK_GOLD_TAP_BLUE_CONTEXT_GUARD'


class GoldTapBlueContextTests(unittest.TestCase):
    def chart(self):
        # 两颗同轨金 TAP 后出现更宽的蓝 TAP，保留可由正式解析器读取的谱面结构。
        return parse_sus('#BPM01:120\n#00008:01\n#0001c:0000222200000000\n#0001b:0000000000130000\n')

    def test_explicit_candidate_keeps_exact_gold_follower_despite_later_blue_overlap(self):
        chart = self.chart()
        self.assertEqual([(g.start, g.points[0].lane, g.points[0].width, g.critical) for g in chart.gestures],
                         [(.5, 12, 2, True), (.75, 12, 2, True), (1.25, 11, 3, False)])
        self.assertEqual(chart_player.gold_tap_following_context(chart), ())
        self.assertEqual(chart_player.gold_tap_following_context(chart, skip_blue_taps=False), ())
        self.assertEqual(chart_player.gold_tap_following_context(chart, skip_blue_taps=True), (chart.gestures[1],))

    def test_candidate_rejects_illegal_blue_and_other_overlapping_conflicts(self):
        chart = self.chart()
        first, later, blue = chart.gestures
        variants = [replace(blue, kind=kind) for kind in ('trace', 'slide', 'unknown')]
        variants.extend(replace(blue, **{key: value}) for key, value in
                        [('critical', True), ('critical', 0), ('flick', 1), ('flick', False)])
        variants.extend(replace(blue, points=(point,)) for point in
                        [Point(.5, 11, 3, 1), Point(.5 + 1e-12, 11, 3, 1),
                         Point(float('nan'), 11, 3, 1), Point(float('inf'), 11, 3, 1),
                         Point(True, 11, 3, 1), Point(1.25, True, 13, 1),
                         Point(1.25, 12, True, 1), Point(1.25, 11., 3, 1),
                         Point(1.25, 11, 4, 1), Point(1.25, 11, 3, True),
                         Point(1.25, 11, 3, 2), Point(1.25, 11, 3, 1, 1),
                         Point(1.25, 11, 3, 1, False)])
        variants.append(replace(blue, points=(blue.points[0], blue.points[0])))
        variants.append(replace(blue, points=(SimpleNamespace(time=1.25, lane=11, width=3, kind=1, direction=0),)))
        for invalid in variants:
            with self.subTest(invalid=invalid):
                changed = replace(chart, gestures=(first, later, invalid))
                self.assertEqual(chart_player.gold_tap_following_context(changed, skip_blue_taps=True), ())

    def test_candidate_preserves_gold_follower_time_lane_width_contract(self):
        chart = self.chart()
        first, later, blue = chart.gestures
        for point in (Point(.5, 12, 2, 2), Point(.75, 11, 2, 2),
                      Point(.75, 12, 1, 2), Point(.75, 12, 2, 6), Point(1.51, 12, 2, 2)):
            with self.subTest(point=point):
                changed = replace(chart, gestures=(first, replace(later, points=(point,)), blue))
                self.assertEqual(chart_player.gold_tap_following_context(changed, skip_blue_taps=True), ())
        self.assertEqual(chart_player.validated_gold_tap_followers(first,
            (replace(later, points=(Point(.4, 12, 2, 2),)),)), ())

    def test_new_flag_is_default_off_requires_exact_one_and_freezes_context(self):
        chart = self.chart()
        for initial in (None, '', '0', '1', 'true', 'other'):
            with self.subTest(initial=initial), patch.dict(os.environ, {}):
                os.environ.pop(FLAG, None)
                if initial is not None:
                    os.environ[FLAG] = initial
                anchor = chart_player.StartAnchor(chart.first, gold_tap_context_chart=chart)
                os.environ[FLAG] = '0' if initial == '1' else '1'
                self.assertEqual(anchor.gold_tap_blue_context_guard, initial == '1')
                self.assertEqual(anchor.gold_tap_following_gestures, (chart.gestures[1],) if initial == '1' else ())

    def test_head_and_follower_rollback_override_candidate_and_wrong_chart_is_rejected(self):
        chart = self.chart()
        for original in ('MAAPJSK_GOLD_TAP_HEAD_GUARD', 'MAAPJSK_GOLD_TAP_FOLLOWERS_GUARD'):
            with self.subTest(original=original), patch.dict(os.environ, {FLAG: '1', original: '0'}):
                anchor = chart_player.StartAnchor(chart.first, gold_tap_context_chart=chart)
                self.assertFalse(anchor.gold_tap_blue_context_guard)
                self.assertEqual(anchor.gold_tap_following_gestures, ())
        with patch.dict(os.environ, {FLAG: '1'}):
            anchor = chart_player.StartAnchor(replace(chart.first), gold_tap_context_chart=chart)
        self.assertEqual(anchor.gold_tap_following_gestures, ())

    def test_private_song443_replay_accepts_original_fit_before_window_expires(self):
        # 私有原始报告、来源帧和曲库只通过显式路径读取，不复制到公开源码或发行资产。
        run = os.environ.get('MAAPJSK_GOLD_TAP_BLUE_REPLAY_RUN')
        chart_root = os.environ.get('MAAPJSK_GOLD_TAP_BLUE_REPLAY_CHART_ROOT')
        if not run or not chart_root:
            self.skipTest('未提供私有重叠蓝 TAP 复现报告及曲库')
        root = Path(run)
        report = json.loads((root / 'report.json').read_text(encoding='utf-8-sig'))
        chart = parse_sus(ChartRepository(Path(chart_root)).load_chart(443, 'expert'))
        evidence = report['anchor_frames']
        with patch.dict(os.environ, {FLAG: '0'}):
            original = chart_player.StartAnchor(chart.first, minimum_samples=4, gold_tap_context_chart=chart)
        with self.assertRaises(chart_player.FirstHeadOutsideWindow):
            for original_index, item in enumerate(evidence):
                original.observe(cv2.imread(str(root / 'attempt-1' / item['path'])), item['captured_at'])
        self.assertEqual(original_index, 32)
        self.assertEqual(len(original.samples), 5)
        with patch.dict(os.environ, {FLAG: '1'}):
            anchor = chart_player.StartAnchor(chart.first, minimum_samples=4, gold_tap_context_chart=chart)
        for index, item in enumerate(evidence):
            frame = cv2.imread(str(root / 'attempt-1' / item['path']))
            self.assertIsNotNone(frame)
            epoch = anchor.observe(frame, item['captured_at'], capture_seconds=item.get('capture_seconds', 0.))
            if epoch is not None:
                break
        self.assertIsNotNone(epoch)
        self.assertEqual(index, 21)
        self.assertAlmostEqual(epoch, 15348.76260707041, places=6)
        self.assertAlmostEqual(anchor.samples[-1][1], 239.)
        self.assertTrue(anchor.fit['gold_tap_blue_context_guard'])
        self.assertLess(anchor.fit['residual_pixels'], 12.)
        first_touch = chart_player.compile_touches(chart)[0].time
        self.assertGreater(epoch + first_touch - .051 - item['captured_at'], item.get('capture_seconds', 0.) + .080)


if __name__ == '__main__':
    unittest.main()
