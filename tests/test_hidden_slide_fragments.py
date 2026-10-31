import hashlib
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from project_sekai.chart_catalog import ChartRepository
from project_sekai.chart_player import compile_touches, validate_touches
from project_sekai.sus_chart import parse_sus


HEADER = '#REQUEST "ticks_per_beat 480"\n#00002:4\n#BPM01:120\n#00008:01\n'


def fragment(*, head=7, tail=8, start=100, end=104, internal=(),
             head_width=1, tail_width=1, head_direction=0, tail_direction=0):
    def body(values):
        tokens = ['00'] * 1920
        for tick, token in values:
            tokens[tick] = token
        return ''.join(tokens)
    nodes = [(start, '11'), (end, '21')]
    markers = [(start, f'{head}{head_width}'), (end, f'{tail}{tail_width}')]
    for tick, kind, marker, width in internal:
        nodes.append((tick, f'{kind}1'))
        if marker is not None:
            markers.append((tick, f'{marker}{width}'))
    directions = [(tick, f'{value}1') for tick, value in
                  ((start, head_direction), (end, tail_direction)) if value]
    return HEADER + '#0001d:11\n#000320:' + body(nodes) + '\n#00012:' + body(markers) + '\n#00052:' + body(directions)


class HiddenSlideFragmentTests(unittest.TestCase):
    def parse(self, source, hidden=True, erased=False):
        with patch('project_sekai.sus_chart.HIDDEN_SLIDE_FRAGMENT_GUARD', hidden, create=True), patch(
                'project_sekai.sus_chart.ERASED_SLIDE_GUARD', erased):
            return parse_sus(source)

    def test_all_hidden_endpoint_pairs_and_nonjudgement_nodes(self):
        for head in (7, 8):
            for tail in (7, 8):
                for erased in (False, True):
                    with self.subTest(head=head, tail=tail, erased=erased):
                        chart = self.parse(fragment(head=head, tail=tail, head_direction=2,
                            internal=((101, 4, 7, 1), (102, 5, 8, 1))), erased=erased)
                        self.assertEqual(len(chart.gestures), 1)
                        validate_touches(compile_touches(chart))

    def test_preserves_judgements_uncertain_geometry_and_tick_boundaries(self):
        retained = [{'head': kind} for kind in (1, 2, 3, 5, 6)]
        retained += [{'tail': kind} for kind in (1, 2, 3, 5, 6)]
        retained += [{'internal': ((101, 5, kind, 1),)} for kind in (1, 2, 3, 5, 6)]
        retained += [{'head_width': 2}, {'tail_width': 2}, {'tail_direction': 3},
                     {'internal': ((101, 3, None, 1),)}, {'internal': ((101, 5, 7, 2),)},
                     {'start': 0}, {'end': 240}, {'start': 240, 'end': 241},
                     {'start': 239, 'end': 241}, {'end': 1000}]
        for options in retained:
            for erased in (False, True):
                with self.subTest(options=options, erased=erased):
                    source = fragment(**options)
                    baseline = self.parse(source, hidden=False)
                    candidate = self.parse(source, erased=erased)
                    self.assertEqual(candidate, baseline)
                    validate_touches(compile_touches(candidate))

    def test_start_direction_is_path_metadata_even_when_chain_retained(self):
        chart = self.parse(fragment(head_direction=2, end=500))
        slide = next(value for value in chart.gestures if value.kind == 'slide')
        self.assertEqual(slide.points[0].direction, 2)
        self.assertEqual(slide.flick, 0)

    def test_conflicting_short_rows_never_prove_hidden_in_either_order(self):
        for tick in (100, 101, 104):
            for conflicting in ('11', '72'):
                source = fragment(head=8, tail=8, internal=((101, 5, 8, 1),))
                tokens = ['00'] * 1920
                tokens[tick] = conflicting
                row = '#00012:' + ''.join(tokens) + '\n'
                for value in (source + '\n' + row, HEADER + row + source[len(HEADER):]):
                    for erased in (False, True):
                        with self.subTest(tick=tick, conflicting=conflicting, erased=erased,
                                          row_first=value.startswith(HEADER + row)):
                            self.assertEqual(self.parse(value, erased=erased), self.parse(value, hidden=False))
        # 重复的相同标记不是冲突，不应改变明确隐藏证据。
        source = fragment()
        row = next(row for row in source.splitlines() if row.startswith('#00012:'))
        self.assertEqual(len(self.parse(source + '\n' + row).gestures), 1)

    def test_disabled_candidate_and_old_guard_outputs_unchanged(self):
        for erased in (False, True):
            for head, expected in ((7, 2), (8, 1 if erased else 2)):
                chart = self.parse(fragment(head=head), hidden=False, erased=erased)
                self.assertEqual(len(chart.gestures), expected)
                validate_touches(compile_touches(chart))

    def test_incomplete_or_invalid_chains_still_rejected(self):
        for rows in ('#000320:00110000', '#000320:00210000', '#000320:00111121'):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.parse(HEADER + '#0001d:11\n' + rows)

    def test_environment_is_explicit_frozen_and_versions_isolate_calibration(self):
        script = '''import os
from unittest.mock import Mock, patch
from project_sekai import sus_chart, chart_player, calibration
device = Mock()
device.controller.info = {'adb_serial': 'synthetic'}
device.shell.return_value = 'versionName=test'
with patch.object(calibration.native_engine, 'module') as native:
    native.return_value.version.return_value = '1.2.0'
    env = calibration.environment_signature(device)
print(sus_chart.HIDDEN_SLIDE_FRAGMENT_GUARD, chart_player.TOUCH_PLAN_VERSION, chart_player.SLIDE_NODE_TOUCH_PLAN_VERSION, env['touch_plan_version'], chart_player.START_ANCHOR_VERSION)
os.environ['MAAPJSK_HIDDEN_SLIDE_FRAGMENT_GUARD'] = '0' if sus_chart.HIDDEN_SLIDE_FRAGMENT_GUARD else '1'
print(sus_chart.HIDDEN_SLIDE_FRAGMENT_GUARD)
'''
        for erased in ('0', '1'):
            for value in (None, '0', '1', 'true'):
                env = dict(os.environ, MAAPJSK_ERASED_SLIDE_GUARD=erased)
                env.pop('MAAPJSK_HIDDEN_SLIDE_FRAGMENT_GUARD', None)
                if value is not None:
                    env['MAAPJSK_HIDDEN_SLIDE_FRAGMENT_GUARD'] = value
                result = subprocess.run([sys.executable, '-B', '-c', script], env=env,
                    cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=True)
                version = 8 if value == '1' else 6 if erased == '1' else 4
                enabled = value == '1'
                self.assertEqual(result.stdout.splitlines(),
                    [f'{enabled} {version} {version + 1} {version} 28', str(enabled)])

    def test_real_767_master_through_strict_repository_loading(self):
        root = Path(os.environ.get('MAAPJSK_TEST_CHART_ROOT', Path(__file__).resolve().parents[1] / 'resource/charts'))
        if not (root / 'manifest.json').is_file():
            self.skipTest('未提供本地曲库，不提交游戏谱面素材')
        repository = ChartRepository(root)
        if 767 not in repository.songs or 'master' not in repository.songs[767]['charts']:
            self.skipTest('本地曲库无767 MASTER')
        source = repository.load_chart(767, 'master')
        if hashlib.sha256(source.encode('utf-8')).hexdigest() != '42944ef6c747d6944defa58f255edf9ce81704534dfa305f7ce61ad8887a5d7a':
            self.skipTest('本地767 MASTER与原失败证据哈希不同')
        baseline = self.parse(source, hidden=False)
        for erased, count in ((False, 817), (True, 814)):
            old = self.parse(source, hidden=False, erased=erased)
            self.assertEqual(len(old.gestures), count)
            with self.assertRaisesRegex(ValueError, '超过十个并发触点'):
                compile_touches(old)
        candidate = self.parse(source)
        self.assertEqual(len(candidate.gestures), 782)
        self.assertEqual(candidate, self.parse(source, erased=True))
        # 可判内部节点、尾flick及真实长条的全部路径节点保持，不能用并发数决定删除。
        for gesture in baseline.gestures:
            if (gesture.kind != 'slide' or gesture.flick or gesture.critical
                    or any(point.kind == 3 for point in gesture.points)
                    or gesture.end - gesture.start >= .25):
                self.assertIn(gesture, candidate.gestures)
        events = compile_touches(candidate)
        self.assertEqual(len(events), 4621)
        active, maximum = set(), 0
        for event in events:
            if event.kind == 'down':
                active.add(event.contact)
            elif event.kind == 'up':
                active.remove(event.contact)
            maximum = max(maximum, len(active))
        self.assertEqual(maximum, 5)
        self.assertFalse(active)
        validate_touches(events)
