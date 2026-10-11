import tempfile
import json
import sys
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import numpy as np
from project_sekai.cooperative_live import CooperativeLive
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agent'))
import cooperative_live as agent
from project_sekai.game_login import GameLogin


class SettlementTitleRecoveryTests(unittest.TestCase):
    diagnostics_disabled = False

    def setUp(self):
        policy = patch('project_sekai.diagnostics._disabled', self.diagnostics_disabled)
        policy.start()
        self.addCleanup(policy.stop)

    def test_agent_login_factory_shares_device_and_keeps_startup_service_separate(self):
        from types import SimpleNamespace
        context = SimpleNamespace(tasker=SimpleNamespace(controller=object(), stopping=False))
        workflow = Mock()
        startup, settlement = Mock(), Mock()
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / 'config').mkdir()
            (root / 'config/chart-sync.json').write_text(json.dumps({'output_root': 'charts'}))
            with patch.object(agent, 'CooperativeLive', return_value=workflow), \
                    patch.object(agent, 'MaaDevice') as device, \
                    patch.object(agent, 'GameLogin', side_effect=[startup, settlement]) as create:
                actual = agent.create_workflow(context, root)
                self.assertEqual(create.call_count, 1)
                actual.startup_login()
                self.assertIs(actual.settlement_login_factory(), settlement)
                startup.run.assert_called_once_with(resume=True)
                self.assertTrue(all(call.args[:2] == (workflow.device, workflow.navigator)
                                    for call in create.call_args_list))
                device.assert_called_once_with(context.tasker.controller)

    def setup_workflow(self, states, *, login_error=None):
        workflow = CooperativeLive.__new__(CooperativeLive)
        clock = [0.]
        frames = {name: np.full((720, 1280, 3), 60 + i, np.uint8) for i, name in enumerate(set(states))}
        sequence = iter(states)
        current = [states[0]]
        def screenshot():
            current[0] = next(sequence, current[0])
            return frames[current[0]]
        def score(frame, page):
            name = next(name for name, image in frames.items() if image is frame)
            return float(page == name or name == 'conflict' and page in ('title_screen', 'playing')
                         or name == 'life_conflict' and page in ('title_screen', 'life_hud'))
        workflow.device = Mock()
        workflow.device.screenshot.side_effect = screenshot
        workflow.navigator = Mock(threshold=.83)
        workflow.navigator.match.side_effect = lambda frame, page, *args: (score(frame, page), (0, 0))
        workflow.score = score
        workflow.stop_requested = lambda: clock[0] > 12
        workflow.pause = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
        workflow.log = Mock()
        workflow.recovery_state = Mock()
        workflow.observe_recovery_death = Mock(return_value=False)
        workflow.persist_runtime_report = Mock()
        workflow.settlement_page = Mock(return_value=None)
        service = Mock()
        service.report = {'title_requests': 1}
        def login(**kwargs):
            if login_error:
                raise login_error
            current[0] = 'home'
            workflow.device.screenshot.side_effect = lambda: frames['home']
            return {'completed': True}
        service.run.side_effect = login
        workflow.settlement_login_factory = Mock(return_value=service)
        report = {'phase': 'settlement', 'error': '原结算失败', 'completed': False, 'joined_room': True,
                  'playback': {'planned_actions': 2, 'sent_actions': 2, 'release_confirmed': True}}
        return workflow, clock, report, service

    def test_completed_settlement_title_calls_login_then_existing_home_gate(self):
        workflow, clock, report, service = self.setup_workflow(['title_screen', 'title_screen', 'home'])
        report['engine'] = 'native'
        report['playback'].update(executed_actions=2, release={
            'reset_executed': True, 'release_proof': 'current-reset-jlog-and-cleanup'})
        with tempfile.TemporaryDirectory() as root, patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock[0]):
            workflow.wait_runtime_destination(report, Path(root), RuntimeError('原結算失败'))
        service.run.assert_called_once_with(wait_for_home=True, resume=True)
        workflow.device.preflight.assert_called_once()
        self.assertEqual(workflow.round_rematches, 1)
        self.assertFalse(report['completed'])
        self.assertEqual(report['error'], '原结算失败')
        self.assertEqual(report['settlement_login']['source_report'],
                         None if self.diagnostics_disabled else str(Path(root) / 'report.json'))

    def test_completed_episode_does_not_restart_but_next_run_can_login(self):
        workflow, clock, report, service = self.setup_workflow(['title_screen', 'title_screen', 'home'])
        report['run_id'] = 'first-run'
        title = np.full((720, 1280, 3), 60, np.uint8)
        with tempfile.TemporaryDirectory() as root, patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock[0]):
            directory = Path(root) / 'memory-only-run'
            workflow.wait_runtime_destination(report, directory, RuntimeError('原失败'))
            workflow.device.screenshot.side_effect = lambda: title
            workflow.score = lambda frame, page: float(page == 'title_screen')
            workflow.navigator.match.side_effect = lambda frame, page, *args: (workflow.score(frame, page), (0, 0))
            with self.assertRaises(InterruptedError):
                workflow.wait_runtime_destination(dict(report), directory, RuntimeError('原失败'))
            workflow.settlement_login_factory.assert_called_once()
            next_report = dict(report, run_id='second-run')
            clock[0] = 0.
            next_service = Mock(report={})
            def login(**kwargs):
                workflow.device.screenshot.side_effect = lambda: title
                workflow.score = lambda frame, page: float(page == 'home')
                return {'completed': True}
            next_service.run.side_effect = login
            workflow.settlement_login_factory.return_value = next_service
            workflow.wait_runtime_destination(next_report, directory, RuntimeError('下一局失败'))
            self.assertEqual(workflow.settlement_login_factory.call_count, 2)
            next_service.run.assert_called_once_with(wait_for_home=True, resume=True)
            self.assertFalse(next_report['completed'])
            self.assertEqual(next_report['error'], '原结算失败')
            if self.diagnostics_disabled:
                self.assertEqual(list(Path(root).iterdir()), [])

    def test_distinct_directories_without_run_id_start_distinct_completed_episodes(self):
        workflow, clock, report, service = self.setup_workflow(['title_screen', 'title_screen', 'home'])
        with tempfile.TemporaryDirectory() as root, patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock[0]):
            workflow.wait_runtime_destination(report, Path(root) / 'first', RuntimeError('原失败'))
            title = np.full((720, 1280, 3), 60, np.uint8)
            workflow.device.screenshot.side_effect = lambda: title
            workflow.score = lambda frame, page: float(page == 'title_screen')
            workflow.navigator.match.side_effect = lambda frame, page, *args: (workflow.score(frame, page), (0, 0))
            def login(**kwargs):
                workflow.score = lambda frame, page: float(page == 'home')
                return {'completed': True}
            service.run.side_effect = login
            workflow.wait_runtime_destination(dict(report), Path(root) / 'second', RuntimeError('下一局失败'))
            self.assertEqual(workflow.settlement_login_factory.call_count, 2)
            self.assertEqual(service.run.call_count, 2)

    def test_real_login_keeps_deadline_and_three_requests_across_recovery_reports(self):
        workflow, clock, report, _ = self.setup_workflow(['title_screen'])
        workflow.stop_requested = lambda: clock[0] > 75.
        workflow.device.shell.side_effect = lambda command: ('Physical density: 240' if command == 'wm density'
                                                            else 'mCurrentFocus=com.sega.pjsekai/.Main')
        workflow.device.tap.side_effect = OSError('点击回执连接失败')
        workflow.navigator.templates = {page: None for page in ('title_screen', 'home', 'playing', 'life_hud')}
        workflow.recovery_state = CooperativeLive.recovery_state.__get__(workflow)
        with tempfile.TemporaryDirectory() as root, patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock[0]), \
                patch('project_sekai.game_login.title_menu_ready', return_value=True):
            directory = Path(root) / 'first-report'
            service = GameLogin(workflow.device, workflow.navigator, Path(root) / 'login',
                                stop_requested=lambda: workflow.stop_requested(), log_message=Mock(),
                                clock=lambda: clock[0], sleeper=workflow.pause)
            service.known_notice = Mock(return_value=False)
            workflow.settlement_login_factory.return_value = service
            with self.assertRaises(InterruptedError):
                workflow.wait_runtime_destination(report, directory, RuntimeError('原失败'))
            self.assertEqual(service.report['title_requests'], 3)
            deadline, session = service._deadline, service._session
            self.assertEqual(deadline, 62.)
            self.assertEqual(workflow.device.tap.call_count, 3)
            next_report = dict(report)
            workflow.stop_requested = lambda: clock[0] > 82.
            with self.assertRaises(InterruptedError):
                workflow.wait_runtime_destination(next_report, Path(root) / 'rebuilt-report', RuntimeError('原失败'))
            self.assertEqual(workflow.device.tap.call_count, 3)
            self.assertEqual(service._deadline, deadline)
            self.assertIs(service._session, session)
            home = np.full((720, 1280, 3), 80, np.uint8)
            workflow.device.screenshot.side_effect = lambda: home
            workflow.score = lambda frame, page: float(page == 'home')
            workflow.navigator.match.side_effect = lambda frame, page, *args: (workflow.score(frame, page), (0, 0))
            workflow.stop_requested = lambda: clock[0] > 95.
            workflow.wait_runtime_destination(next_report, Path(root) / 'rebuilt-report', RuntimeError('原失败'))
            self.assertEqual(service.report['phase'], 'home_confirmed_after_deadline')
            workflow.score = lambda frame, page: float(page == 'title_screen')
            with self.assertRaises(InterruptedError):
                workflow.wait_runtime_destination(dict(next_report), Path(root) / 'rebuilt-report', RuntimeError('原失败'))
            workflow.settlement_login_factory.assert_called_once()
            self.assertEqual(workflow.device.tap.call_count, 3)
            self.assertFalse(next_report['completed'])
            self.assertEqual(next_report['error'], '原结算失败')
            self.assertEqual(next_report['settlement_login']['source_report'],
                             None if self.diagnostics_disabled else str(directory / 'report.json'))
            if self.diagnostics_disabled:
                self.assertEqual(list(Path(root).iterdir()), [])

    def test_unsafe_or_nonsettlement_reports_never_create_service(self):
        for variant in ('playing', 'incomplete', 'release', 'conflict', 'life_conflict', 'death', 'one_frame', 'unknown'):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as root:
                states = [variant] if variant in ('conflict', 'life_conflict') else ['title_screen']
                if variant == 'one_frame':
                    states = ['title_screen', 'unknown']
                if variant == 'unknown':
                    states = ['unknown']
                workflow, clock, report, _ = self.setup_workflow(states, login_error=TimeoutError('期限耗尽'))
                if variant == 'playing':
                    report['phase'] = 'playing'
                elif variant == 'incomplete':
                    report['playback']['sent_actions'] = 1
                elif variant == 'release':
                    report['playback']['release_confirmed'] = False
                elif variant == 'death':
                    report['death_confirmed'] = True
                with patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock[0]):
                    with self.assertRaises(InterruptedError):
                        workflow.wait_runtime_destination(report, Path(root), RuntimeError('原失败'))
                workflow.settlement_login_factory.assert_not_called()

    def test_failed_service_and_reentered_recovery_reuse_one_budget(self):
        with tempfile.TemporaryDirectory() as root:
            workflow, clock, report, service = self.setup_workflow(['title_screen'], login_error=TimeoutError('期限耗尽'))
            directory = Path(root)
            with patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock[0]):
                with self.assertRaises(InterruptedError):
                    workflow.wait_runtime_destination(report, directory, RuntimeError('原失败'))
                first_calls = service.run.call_count
                clock[0] = 0.
                with self.assertRaises(InterruptedError):
                    workflow.wait_runtime_destination(report, directory, RuntimeError('原失败'))
            workflow.settlement_login_factory.assert_called_once()
            self.assertGreater(service.run.call_count, first_calls)
            self.assertTrue(all(call.kwargs == {'wait_for_home': True, 'resume': True}
                                for call in service.run.call_args_list))
            self.assertFalse(report['completed'])
            self.assertEqual(report['error'], '原结算失败')
            self.assertFalse(hasattr(workflow, 'round_rematches'))

    def test_stop_before_login_never_creates_service(self):
        workflow, clock, report, service = self.setup_workflow(['title_screen'])
        workflow.stop_requested = lambda: True
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(InterruptedError):
                workflow.wait_runtime_destination(report, Path(root), RuntimeError('原失败'))
        workflow.settlement_login_factory.assert_not_called()

    def test_recovery_death_precedes_title_login(self):
        from project_sekai.life_monitor import LifeDepleted
        workflow, clock, report, _ = self.setup_workflow(['title_screen'])
        workflow.observe_recovery_death.return_value = True
        workflow.finish_death = Mock()
        with tempfile.TemporaryDirectory() as root, patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(LifeDepleted):
                workflow.wait_runtime_destination(report, Path(root), RuntimeError('原失败'))
        self.assertTrue(report['death_confirmed'])
        workflow.finish_death.assert_called_once()
        workflow.settlement_login_factory.assert_not_called()

    def test_native_current_release_and_execution_proof_required(self):
        for variant in ('missing_reset', 'old_cleanup', 'missing_execution'):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as root:
                workflow, clock, report, _ = self.setup_workflow(['title_screen'])
                report['engine'] = 'native'
                report['playback'].update(executed_actions=2, release={
                    'reset_executed': True, 'release_proof': 'current-reset-jlog-and-cleanup'})
                if variant == 'missing_reset':
                    report['playback']['release']['reset_executed'] = False
                elif variant == 'old_cleanup':
                    report['playback']['release']['release_proof'] = 'old-reset'
                else:
                    report['playback']['executed_actions'] = 1
                with patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock[0]):
                    with self.assertRaises(InterruptedError):
                        workflow.wait_runtime_destination(report, Path(root), RuntimeError('原失败'))
                workflow.settlement_login_factory.assert_not_called()

    def test_original_entry_budget_is_not_reset_by_login(self):
        workflow, clock, report, service = self.setup_workflow(['title_screen', 'title_screen', 'home'])
        workflow.entry_request_count = 3
        with tempfile.TemporaryDirectory() as root, patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(InterruptedError):
                workflow.wait_runtime_destination(report, Path(root), RuntimeError('原失败'))
        service.run.assert_called_once()
        self.assertEqual(workflow.entry_request_count, 3)
        workflow.device.preflight.assert_not_called()

    def test_logged_in_home_still_waits_for_existing_resource_gate(self):
        workflow, clock, report, service = self.setup_workflow(['title_screen', 'title_screen', 'home'])
        report['bonus'] = {'availability': 'insufficient', 'consumption': 5}
        workflow.read_available_bonus = Mock(return_value=(1, {}))
        with tempfile.TemporaryDirectory() as root, patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(InterruptedError):
                workflow.wait_runtime_destination(report, Path(root), RuntimeError('原失败'))
        service.run.assert_called_once()
        workflow.read_available_bonus.assert_called()
        workflow.device.preflight.assert_not_called()
        self.assertFalse(report.get('rematch_budget_consumed', False))


class NoLogSettlementTitleRecoveryTests(SettlementTitleRecoveryTests):
    diagnostics_disabled = True


if __name__ == '__main__':
    unittest.main()
