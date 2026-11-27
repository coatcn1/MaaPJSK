from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from project_sekai import sus_chart
from project_sekai.chart_player import compile_touches, validate_touches


HEADER = '#REQUEST "ticks_per_beat 480"\n#00002:4\n#BPM01:120\n#00008:01\n'


def source(*, internal=(), start=100, end=180, head=7, tail=8, head_width=2,
           tail_width=1, tail_lane=9, tail_direction=0, extra=()):
    rows = ['#0001d:11']
    values = {}

    def add(channel, tick, token):
        values.setdefault(channel, {})[tick] = token

    add('320', start, f'1{head_width}')
    add(f'3{tail_lane:x}0', end, f'2{tail_width}')
    add('12', start, f'{head}{head_width}')
    add(f'1{tail_lane:x}', end, f'{tail}{tail_width}')
    for tick, lane, width, kind, marker in internal:
        add(f'3{lane:x}0', tick, f'{kind}{width}')
        if marker is not None:
            add(f'1{lane:x}', tick, f'{marker}{width}')
    if tail_direction:
        add(f'5{tail_lane:x}', end, f'{tail_direction}{tail_width}')
    for channel, items in values.items():
        tokens = ['00'] * 1920
        for tick, token in items.items():
            tokens[tick] = token
        rows.append(f'#000{channel}:' + ''.join(tokens))
    return HEADER + '\n'.join([*rows, *extra])


class SusSemanticGuardTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in [('SUS_EASING_DIRECTION_GUARD', True),
                            ('NONJUDGEMENT_SLIDE_GUARD', True),
                            ('ERASED_SLIDE_GUARD', False), ('HIDDEN_SLIDE_FRAGMENT_GUARD', False)]:
            self.stack.enter_context(patch.object(sus_chart, name, value, create=True))

    def test_easing_modifiers_never_create_tap_or_trace_flicks(self):
        for marker in (1, 2, 5, 6):
            for modifier in (2, 5, 6):
                with self.subTest(marker=marker, modifier=modifier):
                    chart = sus_chart.parse_sus(HEADER + f'#00017:{marker}2\n#00057:{modifier}2')
                    self.assertEqual(chart.first.flick, 0)
                    self.assertFalse(any(e.kind == 'move' for e in compile_touches(chart)))

    def test_real_flicks_and_easing_at_same_position_remain_independent(self):
        for direction in (1, 3, 4):
            with self.subTest(direction=direction):
                chart = sus_chart.parse_sus(HEADER + f'#00017:12\n#00057:{direction}2\n#00057:22')
                self.assertEqual(chart.first.flick, direction)

    def test_slide_tail_easing_does_not_create_flick_and_head_easing_keeps_curve(self):
        rows = '#000320:12\n#001390:22\n#00052:22\n#00159:52'
        chart = sus_chart.parse_sus(HEADER + rows)
        self.assertEqual(chart.first.flick, 0)
        self.assertEqual(chart.first.points[0].direction, 2)
        self.assertAlmostEqual(sus_chart.slide_x(chart.first.points, 1),
                               chart.first.points[0].x + (chart.first.points[-1].x - chart.first.points[0].x) * .25,
                               places=3)

    def test_hidden_chain_can_move_and_change_width_when_all_nodes_are_nonjudgement(self):
        for internal in [(), ((125, 5, 3, 4, None),),
                         ((125, 5, 3, 5, None),), ((125, 5, 3, 5, 3),)]:
            with self.subTest(internal=internal):
                chart = sus_chart.parse_sus(source(internal=internal))
                self.assertEqual(len(chart.gestures), 1)
                self.assertEqual(chart.first.kind, 'tap')
                validate_touches(compile_touches(chart))

    def test_visible_head_tail_or_checkpoint_is_never_erased(self):
        options = [{'head': 1}, {'head': 2}, {'tail': 1}, {'tail': 5}, {'tail_direction': 3}]
        options += [{'internal': ((125, 5, 3, 3, marker),)} for marker in (None, 1, 2, 3, 5, 6, 7, 8)]
        options += [{'internal': ((125, 5, 3, 5, marker),)} for marker in (1, 2, 5, 6)]
        for kwargs in options:
            with self.subTest(kwargs=kwargs):
                self.assertTrue(any(g.kind == 'slide' for g in sus_chart.parse_sus(source(**kwargs)).gestures))

    def test_only_interior_half_beat_ticks_require_hidden_chain_input(self):
        for start, end in [(0, 10), (100, 240), (240, 250)]:
            with self.subTest(start=start, end=end):
                self.assertFalse(any(g.kind == 'slide' for g in sus_chart.parse_sus(source(start=start, end=end)).gestures))
        for start, end in [(239, 241), (100, 700), (0, 241)]:
            with self.subTest(start=start, end=end):
                self.assertTrue(any(g.kind == 'slide' for g in sus_chart.parse_sus(source(start=start, end=end)).gestures))

    def test_conflicting_hidden_marker_cannot_be_erased_even_with_old_candidates_enabled(self):
        self.stack.enter_context(patch.object(sus_chart, 'ERASED_SLIDE_GUARD', True))
        self.stack.enter_context(patch.object(sus_chart, 'HIDDEN_SLIDE_FRAGMENT_GUARD', True))
        tokens = ['00'] * 1920
        tokens[100] = '12'
        rows = source(extra=('#00012:' + ''.join(tokens),))
        self.assertTrue(any(g.kind == 'slide' for g in sus_chart.parse_sus(rows).gestures))

    def test_new_contract_cannot_be_bypassed_by_old_hidden_candidate_for_scored_relay(self):
        self.stack.enter_context(patch.object(sus_chart, 'HIDDEN_SLIDE_FRAGMENT_GUARD', True))
        chart = sus_chart.parse_sus(source(internal=((125, 5, 3, 3, None),)))
        self.assertTrue(any(g.kind == 'slide' for g in chart.gestures))

    def test_disabled_guards_preserve_previous_direction_and_hidden_behavior(self):
        with patch.object(sus_chart, 'SUS_EASING_DIRECTION_GUARD', False), \
                patch.object(sus_chart, 'NONJUDGEMENT_SLIDE_GUARD', False):
            self.assertEqual(sus_chart.parse_sus(HEADER + '#00017:12\n#00057:22').first.flick, 2)
            self.assertTrue(any(g.kind == 'slide' for g in sus_chart.parse_sus(source()).gestures))

    def test_guard_modes_are_explicit_frozen_and_use_separate_touch_versions(self):
        names = ['MAAPJSK_SUS_EASING_DIRECTION_GUARD', 'MAAPJSK_NONJUDGEMENT_SLIDE_GUARD',
                 'MAAPJSK_ERASED_SLIDE_GUARD', 'MAAPJSK_HIDDEN_SLIDE_FRAGMENT_GUARD']
        code = '''
import json, os
from project_sekai import sus_chart as s, chart_player as p
before = [s.SUS_EASING_DIRECTION_GUARD, s.NONJUDGEMENT_SLIDE_GUARD]
os.environ['MAAPJSK_SUS_EASING_DIRECTION_GUARD'] = '1'
os.environ['MAAPJSK_NONJUDGEMENT_SLIDE_GUARD'] = '1'
print(json.dumps([before, [s.SUS_EASING_DIRECTION_GUARD, s.NONJUDGEMENT_SLIDE_GUARD],
                  p.TOUCH_PLAN_VERSION, p.SLIDE_NODE_TOUCH_PLAN_VERSION]))
'''
        cases = [({}, 4), ({names[2]: '1'}, 6), ({names[3]: '1'}, 8),
                 ({names[1]: '1'}, 10), ({names[0]: '1'}, 12),
                 ({names[0]: '1', names[2]: '1'}, 14), ({names[0]: '1', names[3]: '1'}, 16),
                 ({names[0]: '1', names[1]: '1'}, 18),
                 ({name: '1' for name in names}, 18), ({names[0]: 'true', names[1]: '0'}, 4)]
        for values, version in cases:
            env = {key: value for key, value in os.environ.items() if key not in names}
            env.update(values)
            with self.subTest(values=values):
                result = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True, check=True)
                before, after, actual, sampled = json.loads(result.stdout)
                self.assertEqual(before, [values.get(names[0]) == '1', values.get(names[1]) == '1'])
                self.assertEqual(before, after)
                self.assertEqual((actual, sampled), (version, version + 1))

    def test_real_783_master_preserves_uncertain_nodes_and_corrects_known_semantics_when_available(self):
        from project_sekai.chart_catalog import ChartRepository
        root = Path(__file__).resolve().parents[1] / 'resource/charts'
        if not (root / 'manifest.json').is_file():
            self.skipTest('需要本地已校验的真实783 MASTER谱面')
        repository = ChartRepository(root)
        entry = repository.songs.get(783, {}).get('charts', {}).get('master', {})
        if entry.get('sha256') != 'c579e01d219b8b46f6547bcd64016842099c2f7325d8621e04c2b71650bf7489':
            self.skipTest('本地没有本次诊断固定哈希的783 MASTER')
        text = repository.load_chart(783, 'master')
        with patch.object(sus_chart, 'SUS_EASING_DIRECTION_GUARD', False), \
                patch.object(sus_chart, 'NONJUDGEMENT_SLIDE_GUARD', False):
            original = sus_chart.parse_sus(text)
        candidate = sus_chart.parse_sus(text)
        self.assertEqual(sum(g.flick in {2, 5, 6} for g in original.gestures), 8)
        self.assertEqual(sum(g.flick in {2, 5, 6} for g in candidate.gestures), 0)
        self.assertEqual(len(original.gestures) - len(candidate.gestures), 88)
        self.assertEqual(len(compile_touches(original)), 6452)
        self.assertEqual(len(compile_touches(candidate)), 6052)
        retained = original.gestures[1096]
        self.assertIn(retained, candidate.gestures)


if __name__ == '__main__':
    unittest.main()
