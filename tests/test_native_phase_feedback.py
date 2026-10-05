from copy import deepcopy
from dataclasses import asdict
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from project_sekai import native_engine
from project_sekai.chart_player import Touch
from project_sekai.native_player import NativePlayer
from test_native_playback import Clock, ScriptDevice


class DelayedScriptDevice(ScriptDevice):
    def __init__(self, clock):
        super().__init__(clock)
        self.delayed = False

    def publish(self, text):
        for command in text.splitlines():
            super().publish(command)
            if not self.delayed and self.clock() >= 1000.7 and command.startswith('w '):
                end, line, _ = self.pending.pop()
                event = json.loads(line[5:])
                event['et'] += 25
                event['c'] += 25
                self.tail += .025
                self.pending.append((end + .025, 'jlog ' + json.dumps(event), command))
                self.delayed = True


@unittest.skipUnless(native_engine.available(), '先构建 Native 才能验证未来窗口反馈')
class NativePhaseFeedbackTests(unittest.TestCase):
    events = (Touch(.1, 2, 0, 'down', 500), Touch(.6, 1, 0, 'move', 520),
              Touch(.8, 1, 0, 'move', 540), Touch(1.2, 0, 0, 'up'))

    def setUp(self):
        root = Path('.local/chart-settings/native-phase-feedback')
        root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def run_player(self, correction=None, *, stop=None, events=None):
        clock = Clock()
        device = ScriptDevice(clock)
        player = NativePlayer(None, events or self.events, self.directory, stop or (lambda: False),
                              device=device, clock=clock, sleeper=clock.sleep,
                              game_timing_correction=None if correction is None else lambda: correction(clock()))
        player.prepare()
        report = player.play(1000, -51)
        player.close()
        return device, report

    def test_disabled_and_zero_feedback_have_identical_command_bytes(self):
        original, before = self.run_player()
        zero, after = self.run_player(lambda _: 0)
        self.assertEqual('\n'.join(original.commands).encode(), '\n'.join(zero.commands).encode())
        self.assertEqual(before['frozen_timing_offset_ms'], -51)
        self.assertEqual(after['game_phase_correction_ms'], 0)
        self.assertEqual(after['game_phase_applications'], [])
        self.assertNotIn('native_touch_receipts', before)
        metadata = after['native_touch_receipts']
        self.assertEqual(metadata['status'], 'written')
        self.assertEqual(metadata['captured_actions'], len(self.events))

    def test_future_positive_and_negative_phase_leave_published_hold_window_unchanged(self):
        module = native_engine.module()
        for correction in (5, -5):
            timeline = module.Timeline([asdict(event) for event in self.events])
            timeline.start(10, 10, 0)
            compiler = module.ScriptCompiler()
            first = timeline.next(10)
            saved = deepcopy(first)
            first_bytes = '\n'.join(compiler.compile(first, 1280, 720, 0)['lines'])
            timeline.set_future_phase_correction(correction)
            second = timeline.next(10.31)
            self.assertEqual(first, saved)
            self.assertEqual(first_bytes, '\n'.join(module.ScriptCompiler().compile(saved, 1280, 720, 0)['lines']))
            self.assertAlmostEqual(second['events'][0]['time'], 10.6 + correction / 1000)
            self.assertAlmostEqual(second['events'][0]['planned_time'], 10.6)
            receipts = compiler.compile(second, 1280, 720, 0)['receipts']
            self.assertAlmostEqual(receipts[0]['game_phase_correction_ms'], correction)
            last = timeline.next(10.81)
            compiler.compile(last, 1280, 720, 0)
            self.assertEqual(timeline.sent, len(self.events))
            self.assertEqual(timeline.underflows, 0)

    def test_negative_change_at_window_boundary_never_rewinds_or_repeats_actions(self):
        module = native_engine.module()
        events = (Touch(.49, 2, 0, 'down', 500), Touch(.501, 1, 0, 'move', 510),
                  Touch(.501, 2, 1, 'down', 700), Touch(.8, 0, 0, 'up'), Touch(.8, 0, 1, 'up'))
        timeline = module.Timeline([asdict(event) for event in events])
        timeline.start(10, 10, 0)
        first = timeline.next(10)
        timeline.set_future_phase_correction(-5)
        second = timeline.next(10.31)
        self.assertEqual([row['index'] for row in first['events'] + second['events']], list(range(5)))
        self.assertEqual(second['events'][0]['time'], first['end'])
        self.assertEqual(second['events'][1]['time'], first['end'])
        self.assertGreaterEqual(second['end'], first['end'])

    def test_native_rejects_nonfinite_total_limit_and_oversized_steps(self):
        timeline = native_engine.module().Timeline([asdict(event) for event in self.events])
        timeline.start(10, 10, 0)
        for value in (math.nan, math.inf, -math.inf, 61, -61, 6, -6):
            with self.assertRaises(ValueError):
                timeline.set_future_phase_correction(value)
        for value in range(5, 61, 5):
            timeline.set_future_phase_correction(value)
        with self.assertRaises(ValueError):
            timeline.set_future_phase_correction(65)

    def test_feedback_complete_receipts_zero_underflow_and_frozen_reference(self):
        events = tuple([Touch(.1, 2, 0, 'down', 500)] +
                       [Touch(n / 10, 1, 0, 'move', 500 + n) for n in range(2, 60)] +
                       [Touch(6, 0, 0, 'up')])
        for correction in (5, -5):
            device, report = self.run_player(lambda now: correction if now >= 1000.8 else 0, events=events)
            self.assertEqual((report['sent_actions'], report['executed_actions']), (len(events), len(events)))
            self.assertEqual(report['queue_underflows'], 0)
            self.assertTrue(report['release_confirmed'])
            self.assertFalse(device.active)
            self.assertEqual(report['playback_epoch_s'], 1000)
            self.assertEqual(report['frozen_timing_offset_ms'], -51)
            self.assertEqual(report['game_phase_correction_ms'], correction)
            application = report['game_phase_applications'][0]
            self.assertEqual((application['old_ms'], application['new_ms']), (0, correction))
            self.assertGreater(application['first_action_index'], 0)
            self.assertLess(report['execution_drift_ms_max_abs'], 10)
            self.assertIn('planned_execution_drift_ms_p50', report)
            rows = [json.loads(line) for line in Path(report['native_touch_receipts']['path']).read_text().splitlines()]
            self.assertEqual([row['index'] for row in rows], list(range(len(events))))
            self.assertEqual(len(rows), report['executed_actions'])
            for row in rows:
                planned = events[row['index']].time - .051
                self.assertAlmostEqual(row['planned_target_relative_s'], planned)
                self.assertAlmostEqual((row['effective_target_relative_s'] - planned) * 1000,
                                       row['game_phase_correction_ms'])
                self.assertAlmostEqual((row['actual_barrier_relative_s'] - row['effective_target_relative_s']) * 1000,
                                       row['execution_drift_ms'])

    def test_invalid_feedback_is_ignored_and_does_not_modify_script(self):
        original, _ = self.run_player()
        for invalid in (math.nan, 61, -61, 6, True, 'invalid'):
            device, report = self.run_player(lambda _: invalid)
            self.assertEqual(device.commands, original.commands)
            self.assertEqual(report['game_phase_correction_ms'], 0)
            self.assertEqual(len(report['game_phase_rejections']), 1)

    def test_return_to_zero_after_positive_phase_keeps_monotonic_targets(self):
        module = native_engine.module()
        events = (Touch(.1, 2, 0, 'down', 500), Touch(.5, 1, 0, 'move', 510), Touch(.8, 0, 0, 'up'))
        timeline = module.Timeline([asdict(event) for event in events])
        timeline.start(10, 10, 0)
        timeline.set_future_phase_correction(5)
        first = timeline.next(10)
        timeline.set_future_phase_correction(0)
        second = timeline.next(10.31)
        self.assertEqual(second['events'][0]['time'], first['end'])
        self.assertEqual([row['index'] for row in first['events'] + second['events']], [0, 1, 2])

    def test_real_wait_delay_remains_visible_with_game_feedback(self):
        clock = Clock()
        device = DelayedScriptDevice(clock)
        events = tuple([Touch(.1, 2, 0, 'down', 500)] +
                       [Touch(n / 10, 1, 0, 'move', 500 + n) for n in range(2, 40)] +
                       [Touch(4, 0, 0, 'up')])
        player = NativePlayer(None, events, self.directory, lambda: False,
                              device=device, clock=clock, sleeper=clock.sleep,
                              game_timing_correction=lambda: 5 if clock() >= 1000.8 else 0)
        player.prepare()
        report = player.play(1000, -51)
        player.close()
        self.assertTrue(device.delayed)
        self.assertEqual(report['executed_actions'], len(events))
        self.assertEqual(report['queue_underflows'], 0)
        self.assertEqual(report['game_phase_correction_ms'], 5)
        self.assertGreater(report['execution_drift_ms_max_abs'], 20)
        self.assertGreater(report['planned_execution_drift_ms_max_abs'], 20)
        self.assertTrue(report['release_confirmed'])

    def test_cumulative_limits_and_slow_observer_preserve_queue_and_final_release(self):
        events = tuple([Touch(.1, 2, 0, 'down', 500)] +
                       [Touch(n / 10, 1, 0, 'move', 500 + n % 100) for n in range(2, 400)] +
                       [Touch(40, 0, 0, 'up')])
        for direction in (1, -1):
            clock = Clock()
            device = ScriptDevice(clock)
            samples = []
            def observer():
                samples.append(clock())
                clock.sleep(.2)
            player = NativePlayer(None, events, self.directory, lambda: False,
                                  device=device, clock=clock, sleeper=clock.sleep,
                                  idle_observer=observer, observation_interval=2, observation_budget=.30,
                                  game_timing_correction=lambda: direction * min(60, int((clock() - 1000) / 2) * 5))
            player.prepare()
            report = player.play(1000, -51)
            player.close()
            self.assertEqual(report['executed_actions'], len(events))
            self.assertEqual(report['queue_underflows'], 0)
            self.assertEqual(report['game_phase_correction_ms'], direction * 60)
            self.assertTrue(report['release_confirmed'])
            self.assertEqual(len(report['game_phase_applications']), 12)
            self.assertTrue(all(row['status'] == 'applied' for row in report['game_phase_applications']))
            self.assertTrue(all(abs(row['new_ms'] - row['old_ms']) == 5 for row in report['game_phase_applications']))
            self.assertGreater(len(samples), 10)
            self.assertTrue(all(right - left >= 2 for left, right in zip(samples, samples[1:])))

    def test_stop_with_feedback_preserves_partial_evidence_and_release(self):
        clock = Clock()
        device = ScriptDevice(clock)
        events = (Touch(.1, 2, 0, 'down', 500), Touch(3, 0, 0, 'up'))
        player = NativePlayer(None, events, self.directory, lambda: clock() > 1000.8,
                              device=device, clock=clock, sleeper=clock.sleep,
                              game_timing_correction=lambda: 5 if clock() > 1000.4 else 0)
        player.prepare()
        with self.assertRaises(InterruptedError):
            player.play(1000, 0)
        published = list(device.commands)
        player.close()
        self.assertEqual(device.commands, published)
        self.assertTrue(player.report['release_confirmed'])
        self.assertFalse(device.active)
        self.assertEqual(player.report['game_phase_correction_ms'], 5)
        rows = Path(player.report['native_touch_receipts']['path']).read_text().splitlines()
        self.assertEqual(len(rows), player.report['executed_actions'])
        self.assertLess(len(rows), len(events))

    def test_receipt_file_is_only_written_after_confirmed_release_and_write_failure_is_diagnostic(self):
        clock = Clock()
        device = ScriptDevice(clock)
        player = NativePlayer(None, self.events, self.directory, lambda: False,
                              device=device, clock=clock, sleeper=clock.sleep, game_timing_correction=lambda: 0)
        player.prepare()
        report = player.play(1000, 0)
        path = Path(report['native_touch_receipts']['path'])
        self.assertFalse(path.exists())
        with patch.object(Path, 'open', side_effect=OSError('offline write failure')):
            player.close()
        self.assertTrue(report['release_confirmed'])
        self.assertEqual(report['executed_actions'], len(self.events))
        self.assertEqual(report['native_touch_receipts']['status'], 'failed')

    def test_receipt_capacity_marks_dropped_actions_and_unconfirmed_release_does_not_write(self):
        clock = Clock()
        device = ScriptDevice(clock)
        player = NativePlayer(None, self.events, self.directory, lambda: False,
                              device=device, clock=clock, sleeper=clock.sleep, game_timing_correction=lambda: 0)
        player.action_receipt_capacity = 1
        player.report['native_touch_receipts']['capacity'] = 1
        player.prepare()
        report = player.play(1000, 0)
        self.assertTrue(report['native_touch_receipts']['truncated'])
        self.assertEqual(report['native_touch_receipts']['dropped_actions'], len(self.events) - 1)
        device.release_ok = False
        with self.assertRaises(RuntimeError):
            player.close()
        self.assertEqual(report['native_touch_receipts']['status'], 'release-unconfirmed')
        self.assertFalse(Path(report['native_touch_receipts']['path']).exists())
