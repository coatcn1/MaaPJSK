from __future__ import annotations

import unittest
from collections import Counter
from dataclasses import asdict, replace
import os
from pathlib import Path
from unittest.mock import patch

from project_sekai import chart_player
from project_sekai import sus_chart
from project_sekai.sus_chart import Chart, Gesture, Point


def short(time, lane=7, width=2, *, trace=False, flick=0, critical=False):
    return Gesture((Point(time, lane, width, 6 if critical and trace else 5 if trace else 2 if critical else 1),),
                   'trace' if trace else 'tap', flick, critical)


class TraceContactChainTests(unittest.TestCase):
    def compile(self, *gestures, enabled=True, sample_slide_nodes=False):
        chart = Chart(tuple(gestures), 480, ((0., 120.),))
        with patch.object(chart_player, 'TRACE_CONTACT_CHAIN_GUARD', enabled, create=True):
            events = chart_player.compile_touches(chart, sample_slide_nodes=sample_slide_nodes)
        chart_player.validate_touches(events)
        return events

    def test_recorded_slide_tail_trace_does_not_create_early_down_for_next_tap(self):
        slide = Gesture((Point(41.375, 9, 5, 1, 6), Point(41.5, 4, 4, 5, 2),
                         Point(41.624479, 9, 5, 2)), 'slide')
        trace = short(41.625, 9, 5, trace=True)
        tap = short(41.6875, 7, 5)
        events = self.compile(slide, trace, tap)
        self.assertEqual(len([e for e in events if e.kind == 'down']), 2)
        self.assertFalse(any(e.kind == 'down' and 41.60 <= e.time < 41.625 for e in events))
        self.assertTrue(any(e.kind == 'down' and e.time == tap.start for e in events))

    def test_recorded_near_simultaneous_tap_trace_flick_keeps_tap_down_first(self):
        tap = short(101.090962, 9, 3, critical=True)
        trace = short(101.091482, 9, 3, trace=True, flick=1)
        events = self.compile(tap, trace)
        downs = [e for e in events if e.kind == 'down']
        self.assertEqual(len(downs), 1)
        self.assertEqual(downs[0].time, tap.start)
        moves = [e for e in events if e.kind == 'move' and e.y != 570]
        self.assertEqual(len(moves), 3)
        self.assertGreaterEqual(moves[0].time, tap.start + .024)
        self.assertGreater(next(e.time for e in events if e.kind == 'up'), moves[-1].time)

    def test_dense_omnidirectional_trace_flicks_keep_one_contact_and_every_flick(self):
        notes = [short(6.0 + i * .0625, 8, 6, trace=True, flick=1) for i in range(8)]
        events = self.compile(*notes)
        self.assertEqual(len([e for e in events if e.kind == 'down']), 1)
        self.assertEqual(len([e for e in events if e.kind == 'up']), 1)
        moves = [e for e in events if e.kind == 'move']
        self.assertEqual(len(moves), 3 * len(notes))
        for index, note in enumerate(notes):
            motion = moves[index * 3:index * 3 + 3]
            self.assertGreaterEqual(motion[0].time, note.start)
            self.assertLessEqual(motion[-1].time, note.start + .05)
        self.assertTrue(all(486 <= e.y <= 570 for e in events if e.kind != 'up'))

    def test_plain_trace_run_holds_without_repeated_downs(self):
        notes = [short(1 + i * .0625, 7, 4, trace=True) for i in range(6)]
        events = self.compile(*notes)
        self.assertEqual(len([e for e in events if e.kind == 'down']), 1)
        self.assertEqual(len([e for e in events if e.kind == 'up']), 1)
        self.assertLessEqual(events[0].time, notes[0].start)
        self.assertGreater(events[-1].time, notes[-1].end)

    def test_independent_trace_does_not_prepress_the_next_tap(self):
        events = self.compile(short(1., trace=True), short(1.02))
        self.assertEqual([e.time for e in events if e.kind == 'down'], [1., 1.02])

    def test_real_repeated_taps_always_keep_independent_downs(self):
        taps = [short(t) for t in (0., .125, .25)]
        events = self.compile(taps[0], short(.0625, trace=True), taps[1], short(.1875, trace=True), taps[2])
        self.assertEqual([e.time for e in events if e.kind == 'down'], [g.start for g in taps])

    def test_slide_with_flick_tail_is_not_extended_into_trace(self):
        slide = Gesture((Point(1., 7, 2, 1), Point(1.25, 7, 2, 2)), 'slide', 1)
        events = self.compile(slide, short(1.251, trace=True))
        self.assertEqual(len([e for e in events if e.kind == 'down']), 2)

    def test_directional_flick_traces_keep_separate_contacts(self):
        for direction in (3, 4):
            with self.subTest(direction=direction):
                events = self.compile(short(1., trace=True, flick=direction),
                                      short(1.0625, trace=True, flick=direction))
                self.assertEqual(len([e for e in events if e.kind == 'down']), 2)

    def test_simultaneous_or_long_gap_trace_notes_are_not_collapsed(self):
        for second in (short(1., trace=True), short(1.5, trace=True)):
            with self.subTest(second=second):
                events = self.compile(short(1., trace=True), second)
                self.assertEqual(len([e for e in events if e.kind == 'down']), 2)

    def test_cross_lane_plain_trace_moves_held_finger_without_early_down(self):
        previous = short(111.080145, 9, 3, trace=True, critical=True)
        trace = short(111.120145, 12, 2, trace=True, critical=True)
        tap = short(111.280145, 11, 3, critical=True)
        events = self.compile(previous, trace, tap)
        self.assertEqual(len([e for e in events if e.kind == 'down']), 2)
        self.assertTrue(any(e.kind == 'move' and e.time == trace.start and e.x == round(trace.points[0].x)
                            for e in events))

    def test_recorded_cross_lane_trace_flicks_keep_established_contact(self):
        notes = [short(48.8125, 9, 2), short(48.875, 5, 2),
                 short(48.916667, 9, 3, trace=True, flick=1, critical=True),
                 short(48.958333, 7, 2, trace=True, flick=1, critical=True),
                 short(49., 4, 3, trace=True, flick=1, critical=True)]
        events = self.compile(*notes)
        self.assertEqual(len([e for e in events if e.kind == 'down']), 2)
        self.assertEqual(len([e for e in events if e.kind == 'move']), 9)

    def test_fresh_trace_flick_establishes_contact_before_motion(self):
        events = self.compile(short(1., trace=True, flick=1))
        down = next(e for e in events if e.kind == 'down')
        motion = next(e for e in events if e.kind == 'move')
        self.assertGreaterEqual(motion.time - down.time, .024 - 1e-9)

    def test_cross_lane_flick_does_not_interrupt_previous_flick_motion(self):
        events = self.compile(short(1., 7, 2, trace=True, flick=1),
                              short(1.03125, 2, 2, trace=True, flick=1))
        self.assertEqual(len([e for e in events if e.kind == 'down']), 2)

    def test_native_compiler_retains_contact_settle_wait_after_compensation(self):
        from project_sekai import native_engine
        if not native_engine.available():
            self.skipTest('需要已构建的Native扩展')
        module = native_engine.module()
        events = self.compile(short(1., trace=True, flick=1))

        def emitted_wait(plan, wait_cost):
            compiler = module.ScriptCompiler()
            offsets = module.TouchLatencyOffsets()
            offsets.down_ms = offsets.move_ms = offsets.up_ms = .02
            offsets.interval_ms, offsets.wait_ms = .01, wait_cost
            compiler.set_offsets(offsets)
            timeline = module.Timeline([asdict(e) for e in plan])
            timeline.start(9, 10, 0)
            result = compiler.compile(timeline.next(10), 720, 1280, 1)
            lines = list(result['lines'])
            down = next(i for i, line in enumerate(lines) if line.startswith('d '))
            move = next(i for i, line in enumerate(lines[down + 1:], down + 1) if line.startswith('m '))
            return sum(int(line.split()[1]) for line in lines[down + 1:move] if line.startswith('w '))

        # 同一真实编译器复现旧1ms间隔被吞掉，并确认候选保留完整的接触建立窗口。
        old = tuple(replace(e, time=e.time - .023) if e.kind != 'down' else e for e in events)
        for cost in (0., .62, 1.8):
            with self.subTest(wait_cost=cost):
                self.assertLessEqual(emitted_wait(old, cost), 1)
                self.assertGreaterEqual(emitted_wait(events, cost), 18)

    def test_disabled_candidate_retains_recorded_duplicate_down(self):
        events = self.compile(short(101.090962, 9, 3, critical=True),
                              short(101.091482, 9, 3, trace=True, flick=1), enabled=False)
        self.assertEqual(len([e for e in events if e.kind == 'down']), 2)

    def test_dense_flick_chain_never_sorts_two_movements_at_same_timestamp(self):
        events = self.compile(*(short(1 + i * .025, 7, 4, trace=True, flick=1) for i in range(8)))
        moves = [e for e in events if e.kind == 'move']
        self.assertEqual(len(moves), 24)
        self.assertTrue(all(a.time < b.time for a, b in zip(moves, moves[1:])))

    def test_legacy_direction_codes_keep_their_motion_when_easing_guard_is_off(self):
        for direction, expected_x in ((2, 640), (5, 535), (6, 745)):
            with self.subTest(direction=direction):
                events = self.compile(short(1, 7, 2, flick=direction))
                moves = [e for e in events if e.kind == 'move']
                self.assertEqual((moves[-1].x, moves[-1].y), (expected_x, 654))

    def test_slide_grid_and_explicit_path_nodes_survive_trace_attachment(self):
        slide = Gesture((Point(1., 7, 2, 1), Point(1.025, 8, 2, 5), Point(1.05, 7, 2, 2)), 'slide')
        for sample in (False, True):
            with self.subTest(sample=sample):
                original = self.compile(slide, enabled=False, sample_slide_nodes=sample)
                updated = self.compile(slide, short(1.051, trace=True), sample_slide_nodes=sample)
                expected = {(e.time, e.x, e.y) for e in original if e.kind == 'move'}
                actual = {(e.time, e.x, e.y) for e in updated if e.kind == 'move'}
                self.assertTrue(expected <= actual)

    def test_real_chart_preserves_tap_downs_and_covers_every_trace_when_available(self):
        from project_sekai.chart_catalog import ChartRepository
        root = Path(os.environ.get('MAAPJSK_TEST_CHART_ROOT', Path(__file__).resolve().parents[1] / 'resource/charts'))
        if not (root / 'manifest.json').is_file():
            self.skipTest('本地真实谱面不可用')
        repo = ChartRepository(root)
        song = repo.songs.get(783, {})
        entry = song.get('charts', {}).get('master', {})
        if entry.get('sha256') != 'c579e01d219b8b46f6547bcd64016842099c2f7325d8621e04c2b71650bf7489':
            self.skipTest('真实谱面版本不匹配')
        with patch.object(sus_chart, 'SUS_EASING_DIRECTION_GUARD', True), \
                patch.object(sus_chart, 'NONJUDGEMENT_SLIDE_GUARD', True):
            chart = sus_chart.parse_sus(repo.load_chart(783, 'master'))
        events = self.compile(*chart.gestures)
        expected = Counter((round(g.start - (.018 if g.flick and g.kind == 'tap' else 0), 9),
                            round(g.points[0].x)) for g in chart.gestures if g.kind != 'trace')
        actual = Counter((round(e.time, 9), e.x) for e in events if e.kind == 'down')
        self.assertFalse(expected - actual)
        active, cursor = {}, 0
        for note in sorted((g for g in chart.gestures if g.kind == 'trace'), key=lambda g: g.start):
            while cursor < len(events) and events[cursor].time <= note.start:
                event = events[cursor]
                if event.kind == 'up':
                    active.pop(event.contact)
                else:
                    active[event.contact] = event.x
                cursor += 1
            point = note.points[0]
            self.assertTrue(any(abs(x - point.x) <= point.width * 1000 / 24 + .51 for x in active.values()),
                            f'TRACE 未覆盖：{note.start}')
        self.assertFalse(any(e.kind == 'down' and 41.60 <= e.time < 41.625 for e in events))
        self.assertFalse(any(e.kind == 'down' and 101.07 <= e.time < 101.09 for e in events))


if __name__ == '__main__':
    unittest.main()
