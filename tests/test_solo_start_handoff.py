import os
import math
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from project_sekai.chart_player import StartAnchor, Touch
from project_sekai.diagnostics import write_json
from project_sekai.life_monitor import LifeGuard
from project_sekai.native_player import NativePlayer, NativeStartupRejected
from project_sekai.solo_live import SoloLive
from project_sekai.sus_chart import Gesture, Point
from tests.test_native_playback import Clock, ScriptDevice
from tests import test_solo_live as solo_fixtures


class SoloStartHandoffTests(unittest.TestCase):
    def opening(self, *, capture_cost=.02, epoch=1001., fast=False, missing=None):
        identity = SimpleNamespace(song_id=74, difficulty='master', title='合成开场', to_dict=lambda: {'song_id': 74})
        final = np.full((720, 1280, 3), 80, np.uint8)
        playing = np.full((720, 1280, 3), 50, np.uint8)
        playing[40:48, 1008:1177] = (0, 255, 0)
        workflow = solo_fixtures.SoloTests()._opening_workflow([final, playing], [identity])
        clock = SimpleNamespace(now=1000.)
        frames = iter((final, playing))
        def capture():
            clock.now += capture_cost
            return next(frames)
        workflow.screenshot = Mock(side_effect=capture)
        workflow.device.screenshot.side_effect = capture
        workflow.navigator.templates = {'life_hud': playing}
        workflow.navigator.match.side_effect = lambda frame, name, *area: (float(frame is playing and name in {'playing', 'life_hud'} and name != missing), (0, 0))
        workflow.check_observation_frame = Mock(side_effect=lambda frame: frame)
        workflow.observe_preplay_life = Mock()
        workflow.life_guard = LifeGuard()
        workflow.pause = lambda _: None
        chart = SimpleNamespace(first=Gesture((Point(0., 8, 3),), 'tap'), gestures=[])
        anchor = Mock(samples=[], fit={})
        anchor.observe.return_value = epoch
        report = {'engine': 'native', 'requested_difficulty': 'master'}
        return workflow, clock, chart, anchor, report, playing

    def run_opening(self, workflow, clock, chart, anchor, report, directory, **kwargs):
        with (patch('project_sekai.diagnostics._disabled', True),
              patch('project_sekai.solo_live.time.perf_counter', side_effect=lambda: clock.now),
              patch('project_sekai.solo_live.StartAnchor', return_value=anchor)):
            return workflow.start(chart, None, report, Path(directory), **kwargs)

    def test_original_four_samples_reject_actual_first_target_already_220ms_past(self):
        points = [(83222.2180891, 49.5), (83222.5010927, 94.), (83223.0323983, 216.)]
        workflow, clock, chart, _, report, playing = self.opening(capture_cost=.2661142)
        clock.now = 83223.5779326 - .2661142
        anchor = StartAnchor(chart.first, minimum_samples=4)
        anchor.baseline = np.zeros_like(playing)
        anchor.samples = points[:]
        with (tempfile.TemporaryDirectory() as directory,
              patch('project_sekai.chart_player.first_note_y', return_value=479.5),
              self.assertRaisesRegex(RuntimeError, '启动余量不足')):
            self.run_opening(workflow, clock, chart, anchor, report, directory, native_start_target=(-.018, -51))
        self.assertAlmostEqual(report['start_anchor']['epoch'], 83223.693045175, places=6)
        self.assertFalse(report['startup_handoff']['accepted'])
        self.assertAlmostEqual(report['startup_handoff']['first_target_s'], 83223.624045175, places=6)
        self.assertAlmostEqual(report['startup_handoff']['checked_at_s'], 83223.8440468, places=6)
        self.assertAlmostEqual(report['startup_handoff']['remaining_ms'], -220.001625, places=4)

    def test_negative_offset_and_early_flick_use_actual_first_target(self):
        for first_time, offset in ((0., -51), (-.018, -51)):
            with self.subTest(first_time=first_time):
                workflow, clock, chart, anchor, report, _ = self.opening(epoch=1000.150)
                with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(RuntimeError, '启动余量不足'):
                    self.run_opening(workflow, clock, chart, anchor, report, directory, native_start_target=(first_time, offset))
                self.assertAlmostEqual(report['startup_handoff']['first_target_s'], 1000.150 + first_time + offset / 1000)
                self.assertFalse(report['startup_handoff']['accepted'])

    def test_healthy_anchor_with_margin_remains_accepted(self):
        workflow, clock, chart, anchor, report, _ = self.opening()
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_opening(workflow, clock, chart, anchor, report, directory, native_start_target=(-.018, -51))
        self.assertEqual(result, 1001.)
        self.assertTrue(report['startup_handoff']['accepted'])
        self.assertGreaterEqual(report['startup_handoff']['remaining_ms'], 80.)

    def test_healthy_actual_four_sample_fit_keeps_original_model_and_margin(self):
        workflow, clock, chart, _, report, playing = self.opening()
        clock.now = 1001.28
        anchor = StartAnchor(chart.first, minimum_samples=4)
        anchor.baseline = np.zeros_like(playing)
        anchor.samples = [(1000. + value, 80 * math.exp(value) - 30) for value in (0., .2, .5)]
        with (tempfile.TemporaryDirectory() as directory,
              patch('project_sekai.chart_player.first_note_y', return_value=80 * math.exp(1.3) - 30)):
            result = self.run_opening(workflow, clock, chart, anchor, report, directory, native_start_target=(0., -51))
            serialized = json.dumps(report, ensure_ascii=False)
            with patch('project_sekai.diagnostics._disabled', False):
                write_json(Path(directory) / 'report.json', report)
            self.assertEqual(json.loads((Path(directory) / 'report.json').read_text(encoding='utf-8')), json.loads(serialized))
        self.assertAlmostEqual(result, 1000. + math.log(600 / 80), places=6)
        self.assertEqual(report['start_anchor']['fit']['model'], 'perspective')
        self.assertEqual(len(report['start_anchor']['samples']), 4)
        self.assertTrue(report['startup_handoff']['accepted'])
        self.assertIs(type(report['startup_handoff']['first_target_s']), float)
        self.assertIs(type(report['startup_handoff']['remaining_ms']), float)
        self.assertIs(type(report['startup_handoff']['accepted']), bool)

    def test_delay_during_report_retention_is_included_in_handoff_check(self):
        workflow, clock, chart, anchor, report, _ = self.opening(epoch=1000.2)
        original_copy = np.array
        anchor.samples = [(999.7, 100.), (999.9, 180.)]
        def delay(*args, **kwargs):
            clock.now += .05
            return original_copy(*args, **kwargs)
        with (tempfile.TemporaryDirectory() as directory,
              patch('project_sekai.solo_live.np.array', side_effect=delay),
              self.assertRaisesRegex(RuntimeError, '启动余量不足')):
            self.run_opening(workflow, clock, chart, anchor, report, directory, native_start_target=(0., -51))
        self.assertGreater(report['startup_handoff']['checked_at_s'], 1000.04)

    def test_startup_fastpath_only_after_identity_reuses_original_frame(self):
        workflow, clock, chart, anchor, report, playing = self.opening()
        with tempfile.TemporaryDirectory() as directory:
            self.run_opening(workflow, clock, chart, anchor, report, directory,
                             native_start_target=(0., -51), native_start_fastpath=True)
        workflow.screenshot.assert_called_once_with()
        workflow.device.screenshot.assert_called_once_with()
        workflow.check_observation_frame.assert_not_called()
        calls = [call for call in workflow.navigator.match.call_args_list if call.args[0] is playing]
        self.assertTrue(calls)
        self.assertTrue(all(len(call.args) == 3 for call in calls))
        self.assertEqual(workflow.observe_preplay_life.call_count, 2)
        self.assertIs(workflow.observe_preplay_life.call_args.args[0], playing)
        self.assertAlmostEqual(workflow.observe_preplay_life.call_args.args[1], 1000.02)
        self.assertEqual(report['native_startup_hud_fastpath']['accepted_frames'], 1)

    def test_startup_missing_hud_falls_back_on_same_capture(self):
        for missing in ('playing', 'life_hud'):
            with self.subTest(missing=missing):
                workflow, clock, chart, anchor, report, playing = self.opening(missing=missing)
                if missing == 'playing':
                    workflow.navigator.match.side_effect = lambda frame, name, *area: (float(frame is playing and name in {'playing', 'life_hud'} and not (name == 'playing' and area)), (0, 0))
                with tempfile.TemporaryDirectory() as directory:
                    self.run_opening(workflow, clock, chart, anchor, report, directory,
                                     native_start_target=(0., -51), native_start_fastpath=True)
                workflow.check_observation_frame.assert_called_once_with(playing)
                workflow.device.screenshot.assert_called_once_with()
                self.assertEqual(report['native_startup_hud_fastpath']['fallback_frames'], 1)

    def test_startup_black_white_and_missing_label_template_keep_original_checks(self):
        for mode in ('black', 'white', 'missing_template'):
            with self.subTest(mode=mode):
                workflow, clock, chart, anchor, report, playing = self.opening()
                if mode == 'missing_template':
                    workflow.navigator.templates = {}
                else:
                    playing[:] = 0 if mode == 'black' else 255
                with tempfile.TemporaryDirectory() as directory:
                    self.run_opening(workflow, clock, chart, anchor, report, directory,
                                     native_start_target=(0., -51), native_start_fastpath=True)
                workflow.check_observation_frame.assert_called_once_with(playing)
                workflow.device.screenshot.assert_called_once_with()
                self.assertEqual(report['native_startup_hud_fastpath']['accepted_frames'], 0)
                self.assertEqual(report['native_startup_hud_fastpath']['fallback_frames'], 1)

    def test_startup_stop_before_or_after_capture_never_samples_that_frame(self):
        for during_capture in (False, True):
            with self.subTest(during_capture=during_capture):
                workflow, clock, chart, anchor, report, playing = self.opening()
                workflow.navigator._check_stop.side_effect = ([None, InterruptedError('用户停止')]
                    if during_capture else InterruptedError('用户停止'))
                with tempfile.TemporaryDirectory() as directory, self.assertRaises(InterruptedError):
                    self.run_opening(workflow, clock, chart, anchor, report, directory,
                                     native_start_target=(0., -51), native_start_fastpath=True)
                self.assertEqual(workflow.device.screenshot.call_count, int(during_capture))
                self.assertEqual(workflow.observe_preplay_life.call_count, 1)
                anchor.observe.assert_not_called()

    def test_default_start_and_other_modes_keep_original_capture_checks(self):
        workflow, clock, chart, anchor, report, _ = self.opening()
        with tempfile.TemporaryDirectory() as directory:
            self.run_opening(workflow, clock, chart, anchor, report, directory)
        self.assertEqual(workflow.screenshot.call_count, 2)
        workflow.device.screenshot.assert_not_called()
        self.assertNotIn('startup_handoff', report)

    def test_startup_guard_defaults_on_only_explicit_zero_disables_and_freezes_at_initialization(self):
        for value in (None, '', '0', '1', 'true', 'other'):
            with self.subTest(value=value):
                with (patch.dict(os.environ, {}, clear=True), patch('project_sekai.solo_live.Navigator'),
                      patch('project_sekai.solo_live.ChartRepository'), patch('project_sekai.solo_live.LineOcr'),
                      patch('project_sekai.solo_live.SongMatcher')):
                    if value is not None:
                        os.environ['MAAPJSK_SOLO_NATIVE_STARTUP_GUARD'] = value
                    workflow = SoloLive(Mock(), Path('.'), Path('.'), Path('.'), Path('.'))
                    os.environ['MAAPJSK_SOLO_NATIVE_STARTUP_GUARD'] = '0' if value != '0' else '1'
                    self.assertEqual(workflow.native_startup_guard, value != '0')

    def test_actual_native_past_first_target_is_typed_and_never_dispatches(self):
        clock = Clock()
        device = ScriptDevice(clock)
        player = NativePlayer(None, (Touch(0., 2, 0, 'down', 500), Touch(.02, 0, 0, 'up')), Path('.'),
                              lambda: False, device=device, clock=clock, sleeper=clock.sleep)
        player.prepare()
        with self.assertRaisesRegex(RuntimeError, '第一音时刻已过') as raised:
            player.play(clock() - .5, -51)
        self.assertEqual(type(raised.exception).__name__, 'NativeStartupRejected')
        self.assertEqual(player.report['sent_actions'], 0)
        self.assertEqual(player.report['executed_actions'], 0)
        self.assertFalse(any(command.startswith('d ') for command in device.commands))
        player.close()

    def test_actual_native_zero_start_refusal_aborts_after_release_without_collect(self):
        for mode in ('normal', 'missing_release', 'stopped', 'disabled', 'candidate_off'):
            with self.subTest(mode=mode):
                workflow, state, frames, chart, _ = solo_fixtures.SoloTests()._startup_abort_workflow(release='valid')
                workflow.native_startup_guard = mode != 'candidate_off'
                workflow.start = Mock(return_value=9.)
                device = ScriptDevice(lambda: state.now)
                device.release_diagnostics = {'release_proof': 'no-touch-possible-and-cleanup'} if mode != 'missing_release' else {}
                def create(*args, **kwargs):
                    player = NativePlayer(None, args[1], args[2], workflow.stop_requested,
                                          device=device, clock=lambda: state.now, sleeper=lambda duration: setattr(state, 'now', state.now + duration))
                    close = player.close
                    def cleanup():
                        close()
                        state.order.append('release')
                        if mode == 'stopped':
                            state.stopped = True
                    player.close = cleanup
                    return player
                with (tempfile.TemporaryDirectory() as directory, patch('project_sekai.diagnostics._disabled', True),
                      patch.dict(os.environ, {'MAAPJSK_SOLO_STARTUP_ABORT_GUARD': '0' if mode == 'disabled' else '1'}),
                      patch('project_sekai.solo_live.time.perf_counter', side_effect=lambda: state.now),
                      patch('project_sekai.solo_live.time.monotonic', side_effect=lambda: state.now),
                      patch('project_sekai.solo_live.parse_sus', return_value=chart),
                      patch('project_sekai.solo_live.compile_touches', return_value=(Touch(0., 2, 0, 'down', 500), Touch(.02, 0, 0, 'up'))),
                      patch('project_sekai.native_player.NativePlayer', side_effect=create),
                      self.assertRaisesRegex(RuntimeError, '第一音时刻已过')):
                    workflow.report_root = Path(directory)
                    workflow.run(1, 'master', 'current', engine='native')
                if mode == 'candidate_off':
                    workflow.collect.assert_called_once()
                else:
                    workflow.collect.assert_not_called()
                self.assertEqual(workflow.completed_rounds, 0)
                self.assertIn('第一音时刻已过', workflow.last_report['playback_error'] if mode != 'stopped' else workflow.last_report['error'])
                if mode == 'normal':
                    self.assertEqual(state.order, ['release', 'pause', 'retire', 'confirm'])
                    self.assertTrue(workflow.last_report['startup_abort']['confirmed'])
                else:
                    self.assertEqual(state.order, ['release'])

    def test_generic_zero_playback_error_or_any_dispatched_start_error_keep_existing_settlement(self):
        for sent, error in ((0, RuntimeError('原输入故障')), (1, NativeStartupRejected('第一音时刻已过'))):
            with self.subTest(sent=sent):
                workflow, state, frames, chart, player = solo_fixtures.SoloTests()._startup_abort_workflow()
                workflow.native_startup_guard = True
                workflow.start = Mock(return_value=10.)
                player.report['sent_actions'] = sent
                player.play.side_effect = error
                with (tempfile.TemporaryDirectory() as directory,
                      patch('project_sekai.diagnostics._disabled', True),
                      patch('project_sekai.solo_live.parse_sus', return_value=chart),
                      patch('project_sekai.solo_live.compile_touches', return_value=(Touch(0., 2, 0, 'down', 500), Touch(.02, 0, 0, 'up'))),
                      patch('project_sekai.native_player.NativePlayer', return_value=player),
                      self.assertRaises(RuntimeError)):
                    workflow.report_root = Path(directory)
                    workflow.run(1, 'master', 'current', engine='native')
                workflow.collect.assert_called_once()
                self.assertNotIn('startup_abort', workflow.last_report)

    def test_zero_start_refusal_retains_original_when_close_fails_then_retries(self):
        for failed_closes in (1, 2):
            with self.subTest(failed_closes=failed_closes):
                workflow, state, frames, chart, _ = solo_fixtures.SoloTests()._startup_abort_workflow()
                workflow.native_startup_guard = True
                workflow.start = Mock(return_value=9.)
                device = ScriptDevice(lambda: state.now)
                device.release_diagnostics = {'release_proof': 'no-touch-possible-and-cleanup'}
                stop = device.stop
                calls = []
                first_release_error = RuntimeError('首轮清理失败')
                def fail_then_stop():
                    calls.append('release')
                    if len(calls) <= failed_closes:
                        raise first_release_error
                    return stop()
                device.stop = fail_then_stop
                def create(*args, **kwargs):
                    return NativePlayer(None, args[1], args[2], workflow.stop_requested, device=device,
                        clock=lambda: state.now, sleeper=lambda duration: setattr(state, 'now', state.now + duration))
                with (tempfile.TemporaryDirectory() as directory,
                      patch('project_sekai.diagnostics._disabled', True),
                      patch.dict(os.environ, {'MAAPJSK_SOLO_STARTUP_ABORT_GUARD': '1'}),
                      patch('project_sekai.solo_live.parse_sus', return_value=chart),
                      patch('project_sekai.solo_live.compile_touches', return_value=(Touch(0., 2, 0, 'down', 500), Touch(.02, 0, 0, 'up'))),
                      patch('project_sekai.native_player.NativePlayer', side_effect=create),
                      self.assertRaisesRegex(NativeStartupRejected, '第一音时刻已过') as raised):
                    workflow.report_root = Path(directory)
                    workflow.run(1, 'master', 'current', engine='native')
                self.assertIs(raised.exception.__cause__, first_release_error)
                self.assertEqual(calls, ['release', 'release'])
                workflow.collect.assert_not_called()
                workflow.navigator.tap.assert_not_called()
                workflow.device.back.assert_not_called()
                self.assertEqual(workflow.last_report['startup_abort']['status'], 'release_unconfirmed')
                self.assertFalse(workflow.last_report['completed'])
                self.assertIn('第一音时刻已过', workflow.last_report['playback_error'])
                self.assertIn('第一音时刻已过', workflow.last_report['error'])
                self.assertIn('首轮清理失败', workflow.last_report['release_error'])
                self.assertEqual(workflow.last_report['playback']['release_confirmed'], failed_closes == 1)


if __name__ == '__main__':
    unittest.main()
