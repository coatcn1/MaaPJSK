import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from agent import solo_live as agent
from project_sekai.life_monitor import LifeDepleted, LifeGuard
from project_sekai.live_end import LIFE_HUD_AREA
from project_sekai.maa_device import MaaDevice
from project_sekai.solo_live import SoloLive


class SoloCaptureRegressionTests(unittest.TestCase):
    def workflow(self, value='1'):
        with (patch.dict(os.environ, {}, clear=True),
              patch('project_sekai.solo_live.Navigator'),
              patch('project_sekai.solo_live.ChartRepository'),
              patch('project_sekai.solo_live.LineOcr'),
              patch('project_sekai.solo_live.SongMatcher')):
            if value is not None:
                os.environ['MAAPJSK_SOLO_NATIVE_HUD_FASTPATH'] = value
            workflow = SoloLive(Mock(), Path('.'), Path('.'), Path('.'), Path('.'), log_message=Mock())
        frame = np.full((720, 1280, 3), 50, np.uint8)
        frame[40:48, 1008:1177] = (0, 255, 0)
        workflow.device.screenshot.return_value = frame
        workflow.navigator.threshold = .83
        workflow.navigator.templates = {'life_hud': frame}
        workflow.navigator.match.return_value = (1., (0, 0))
        workflow.life_guard = LifeGuard()
        workflow.life_frames = []
        workflow.life_epoch = 0.
        workflow.last_report = {}
        workflow.record_performance_trace = Mock()
        workflow.read_optional_number = Mock(return_value=0)
        return workflow, frame

    def test_healthy_hud_reuses_one_frame_without_fullscreen_matching_or_ocr(self):
        workflow, frame = self.workflow()
        workflow.observe_native_play_state(Path('.'), requested_at=2.)
        workflow.device.screenshot.assert_called_once_with()
        workflow.navigator._check_title_screen.assert_not_called()
        workflow.ocr.read.assert_not_called()
        workflow.read_optional_number.assert_not_called()
        for call in workflow.navigator.match.call_args_list:
            self.assertIn(call.args[1:], [('playing', (1190, 10, 1256, 80)), ('life_hud', LIFE_HUD_AREA)])
            self.assertIs(call.args[0], frame)
        self.assertEqual(workflow.life_guard.samples, 1)
        self.assertTrue(workflow.life_guard.hud_seen)
        self.assertEqual(workflow.last_report['native_hud_fastpath'],
                         {'enabled': True, 'accepted_frames': 1, 'fallback_frames': 0})
        workflow.record_performance_trace.assert_called_once_with(2., 0)

    def test_missing_either_hud_blank_or_missing_template_uses_original_checks(self):
        for mode in ('pause', 'life', 'blank', 'white', 'missing_template'):
            with self.subTest(mode=mode):
                workflow, frame = self.workflow()
                if mode == 'blank':
                    frame[:] = 0
                    workflow.navigator.match.return_value = (0., (0, 0))
                elif mode == 'white':
                    frame[:] = 255
                    workflow.navigator.match.return_value = (0., (0, 0))
                elif mode == 'missing_template':
                    workflow.navigator.templates = {}
                else:
                    absent = 'playing' if mode == 'pause' else 'life_hud'
                    workflow.navigator.match.side_effect = lambda image, name, *area: (0. if name == absent else 1., (0, 0))
                workflow.check_observation_frame = Mock(return_value=frame)
                workflow.observe_native_play_state(Path('.'), requested_at=2.)
                workflow.check_observation_frame.assert_called_once_with(frame)
                workflow.device.screenshot.assert_called_once_with()
                self.assertEqual(workflow.last_report['native_hud_fastpath']['fallback_frames'], 1)

    def test_fastpath_defaults_on_explicit_zero_disables_and_freezes_at_initialization(self):
        for value in (None, '', '0', '1', 'true', 'other'):
            with self.subTest(value=value):
                workflow, frame = self.workflow(value)
                workflow.check_observation_frame = Mock(return_value=frame)
                with patch.dict(os.environ, {'MAAPJSK_SOLO_NATIVE_HUD_FASTPATH': '0' if value != '0' else '1'}):
                    workflow.observe_native_play_state(Path('.'), requested_at=2.)
                self.assertEqual(workflow.check_observation_frame.call_count, 0 if value != '0' else 1)

    def test_original_observation_and_async_default_keep_original_checks(self):
        workflow, frame = self.workflow()
        workflow.observe_play_state(Path('.'), frame=frame, requested_at=2.)
        workflow.navigator._check_title_screen.assert_called_once_with(frame)
        workflow.observe_play_state = Mock()
        workflow.observe_native_play_state = Mock()
        with patch.dict(os.environ, {'MAAPJSK_NATIVE_ASYNC_OBSERVATION': '1'}):
            options = workflow.native_observation_options(Path('.'))
            options['captured_frame_observer'](frame, 3.)
            workflow.observe_play_state.assert_called_once_with(Path('.'), frame=frame, requested_at=3.)
            options = workflow.native_observation_options(Path('.'), observer=workflow.observe_native_play_state)
        options['captured_frame_observer'](frame, 4.)
        workflow.observe_native_play_state.assert_called_once_with(Path('.'), frame=frame, requested_at=4.)
        workflow.device.screenshot.assert_not_called()

    def test_failed_capture_cannot_add_life_end_evidence_or_inputs(self):
        workflow, _ = self.workflow()
        job = Mock(succeeded=False)
        job.wait.return_value = job
        controller = Mock()
        controller.post_screencap.return_value = job
        with patch.dict(os.environ, {'MAAPJSK_SCREENSHOT_SUCCESS_GUARD': '1'}):
            workflow.device = MaaDevice(controller)
        workflow.life_guard.zero_streak = 1
        with self.assertRaisesRegex(RuntimeError, '截图作业失败'):
            workflow.observe_native_play_state(Path('.'), requested_at=2.)
        self.assertEqual(workflow.life_guard.samples, 0)
        self.assertFalse(workflow.life_guard.zero_confirmed)
        self.assertEqual(workflow.life_frames, [])
        workflow.record_performance_trace.assert_not_called()
        workflow.navigator.match.assert_not_called()
        job.get.assert_not_called()
        controller.post_click.assert_not_called()
        controller.post_click_key.assert_not_called()

    def test_async_supplied_frame_keeps_request_time_and_does_not_capture_again(self):
        workflow, frame = self.workflow()
        workflow.observe_native_play_state(Path('.'), frame=frame, requested_at=3.)
        workflow.device.screenshot.assert_not_called()
        workflow.navigator._check_title_screen.assert_not_called()
        self.assertEqual(workflow.life_frames[0][0], 3.)
        self.assertIs(workflow.life_frames[0][1], frame)
        workflow.record_performance_trace.assert_called_once_with(3., 0)

    def test_missing_hud_still_rejects_title_before_life_or_trace(self):
        workflow, frame = self.workflow()
        workflow.navigator.match.return_value = (0., (0, 0))
        workflow.navigator._check_title_screen.side_effect = RuntimeError('游戏已返回 TAP TO START 标题页')
        with self.assertRaisesRegex(RuntimeError, 'TAP TO START'):
            workflow.observe_native_play_state(Path('.'), requested_at=2.)
        self.assertEqual(workflow.life_guard.samples, 0)
        self.assertEqual(workflow.life_frames, [])
        workflow.record_performance_trace.assert_not_called()
        workflow.device.screenshot.assert_called_once_with()

    def test_stop_before_or_during_capture_never_adds_evidence(self):
        for during_capture in (False, True):
            with self.subTest(during_capture=during_capture):
                workflow, _ = self.workflow()
                workflow.navigator._check_stop.side_effect = ([None, InterruptedError('用户停止')]
                    if during_capture else InterruptedError('用户停止'))
                with self.assertRaises(InterruptedError):
                    workflow.observe_native_play_state(Path('.'), requested_at=2.)
                self.assertEqual(workflow.device.screenshot.call_count, int(during_capture))
                self.assertEqual(workflow.life_guard.samples, 0)
                workflow.navigator.match.assert_not_called()
                workflow.record_performance_trace.assert_not_called()

    def run_fixture(self, workflow, frame, player, directory):
        workflow.report_root = Path(directory)
        workflow.repository.songs = {74: {'charts': {'master': {'music_id': 74, 'difficulty': 'master',
            'sha256': 'synthetic', 'path': 'synthetic.sus', 'total_note_count': 1}}}}
        workflow.select = Mock(return_value=(SimpleNamespace(song_id=74, to_dict=lambda: {'song_id': 74, 'title': '合成曲名', 'difficulty': 'master'}), frame))
        workflow.prepare_task_bonus = Mock()
        workflow.prepare_playback = Mock()
        workflow.ensure_bonus_available = Mock()
        workflow.start = Mock(return_value=0.)
        chart = SimpleNamespace(duration=1.)
        with (patch('project_sekai.diagnostics._disabled', True),
              patch('project_sekai.solo_live.parse_sus', return_value=chart),
              patch('project_sekai.solo_live.compile_touches', return_value=[]),
              patch('project_sekai.native_player.NativePlayer', return_value=player) as constructor):
            def create_player(*args, **kwargs):
                player.observation_options = kwargs
                return player
            constructor.side_effect = create_player
            workflow.run(1, 'master', 'current', engine='native')
        return constructor

    def player(self):
        player = Mock()
        player.report = {'engine': 'native', 'planned_actions': 3, 'sent_actions': 3, 'executed_actions': 2,
                         'release_confirmed': True, 'release': {'reset_executed': True, 'release_proof': 'current-reset-jlog-and-cleanup'}}
        return player

    def test_real_run_preserves_original_native_error_when_collection_also_fails(self):
        for settlement_error in (RuntimeError('LIVE FAILED'), TimeoutError('结算返回超时')):
            with self.subTest(settlement_error=settlement_error):
                workflow, frame = self.workflow()
                player = self.player()
                original = RuntimeError('Native 队列断粮，2 / 3')
                player.play.side_effect = original
                workflow.collect = Mock(side_effect=settlement_error)
                with tempfile.TemporaryDirectory() as directory:
                    with self.assertRaisesRegex(RuntimeError, 'Native 队列断粮') as raised:
                        self.run_fixture(workflow, frame, player, directory)
                    self.assertEqual(list(Path(directory).iterdir()), [])
                self.assertIs(raised.exception, original)
                self.assertIs(raised.exception.__cause__, settlement_error)
                self.assertIn('Native 队列断粮', workflow.last_report['playback_error'])
                self.assertIn(str(settlement_error), workflow.last_report['settlement_error'])
                self.assertIn('Native 队列断粮', workflow.last_report['error'])
                self.assertIsNone(workflow.last_report['report_path'])
                self.assertFalse(workflow.last_report['completed'])
                self.assertEqual(workflow.completed_rounds, 0)
                player.close.assert_called_once_with()

    def test_real_run_two_normal_zero_frames_release_then_exit(self):
        workflow, frame = self.workflow()
        frame[40:48, 1008:1177] = 50
        player = self.player()
        order = []
        clock = SimpleNamespace(now=.75)
        def observe(*args):
            player.observation_options['idle_observer']()
            self.assertFalse(workflow.life_guard.zero_confirmed)
            clock.now = 1.5
            player.observation_options['idle_observer']()
        player.play.side_effect = observe
        player.close.side_effect = lambda: order.append('release')
        workflow.exit_depleted_live = Mock(side_effect=lambda *args: order.append('exit'))
        workflow.collect = Mock()
        with (tempfile.TemporaryDirectory() as directory,
              patch('project_sekai.solo_live.time.perf_counter', side_effect=lambda: clock.now),
              self.assertRaises(LifeDepleted)):
            self.run_fixture(workflow, frame, player, directory)
        self.assertEqual(order, ['release', 'exit'])
        self.assertEqual(workflow.life_guard.samples, 2)
        workflow.collect.assert_not_called()
        self.assertEqual(workflow.completed_rounds, 0)

    def test_real_run_failed_native_capture_releases_and_retains_capture_error(self):
        workflow, frame = self.workflow()
        job = Mock(succeeded=False)
        job.wait.return_value = job
        controller = Mock()
        controller.post_screencap.return_value = job
        with patch.dict(os.environ, {'MAAPJSK_SCREENSHOT_SUCCESS_GUARD': '1'}):
            workflow.device = MaaDevice(controller)
        workflow.device.preflight = Mock()
        player = self.player()
        order = []
        player.play.side_effect = lambda *args: player.observation_options['idle_observer']()
        player.close.side_effect = lambda: order.append('release')
        workflow.collect = Mock(side_effect=RuntimeError('后续 LIVE FAILED'))
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(RuntimeError, '截图作业失败'):
            self.run_fixture(workflow, frame, player, directory)
        self.assertEqual(order, ['release'])
        job.get.assert_not_called()
        self.assertEqual(workflow.life_guard.samples, 0)
        self.assertEqual(workflow.life_frames, [])
        self.assertFalse(workflow.life_guard.zero_confirmed)
        self.assertFalse(workflow.last_report['completed'])
        self.assertIn('截图作业失败', workflow.last_report['playback_error'])
        self.assertIn('LIVE FAILED', workflow.last_report['settlement_error'])
        self.assertEqual(workflow.completed_rounds, 0)

    def test_agent_abort_shows_identity_receipts_and_original_error_without_report(self):
        context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
        argv = SimpleNamespace(task_detail=SimpleNamespace(task_id=827364))
        settings = {'count': 1, 'difficulty': 'master', 'song_mode': 'current', 'recovery_mode': 'off', 'recovery_count': 1}
        workflow = SimpleNamespace(completed_rounds=0, last_report={'report_path': None,
            'requested_difficulty': 'master', 'preparation_identity': {'title': 'Booo!', 'difficulty': 'master'},
            'playback': {'executed_actions': 2, 'planned_actions': 3}, 'playback_error': 'RuntimeError: 原Native断粮'},
            run=Mock(side_effect=RuntimeError('LIVE FAILED')))
        performance = SimpleNamespace(engine='native', use_calibration_profile=False, touch_offset_ms=-51, bonus_consumption=0)
        with (patch.dict(agent._SETTINGS, {827364: settings}), patch.object(agent, 'create_workflow', return_value=workflow),
              patch.object(agent, 'load_performance', return_value=performance),
              patch.object(agent, 'visible_log') as visible, patch.object(agent.traceback, 'print_exc')):
            self.assertFalse(agent.ProjectSekaiSoloLive().run(context, argv))
        message = visible.call_args.args[1]
        for expected in ('Booo!', 'MASTER', '2 / 3', '原Native断粮', 'LIVE FAILED'):
            self.assertIn(expected, message)


if __name__ == '__main__':
    unittest.main()
