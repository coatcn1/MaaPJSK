import unittest
import json
import shutil
import subprocess
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from project_sekai.life_monitor import LifeDepleted
from project_sekai.solo_live import SoloLive


class FailureRetryBudgetTests(unittest.TestCase):
    def test_one_shared_retry_never_starts_a_third_attempt_after_two_deaths(self):
        from project_sekai.failure_retry import FailureRetryBudget
        changes = []
        budget = FailureRetryBudget(1, on_change=changes.append)
        budget.record_failure('first-attempt')
        self.assertTrue(budget.reserve_retry('first-attempt'))
        self.assertFalse(budget.reserve_retry('first-attempt'))
        budget.record_failure('first-attempt')
        self.assertEqual((budget.failure_count, budget.retries_used), (1, 1))
        budget.record_failure('second-attempt')
        self.assertFalse(budget.reserve_retry('second-attempt'))
        self.assertEqual((budget.failure_count, budget.retries_used), (2, 1))
        self.assertEqual(len(changes), 3)
        self.assertEqual(changes[-1]['failure_count'], 2)

    def test_success_and_new_reports_do_not_reset_the_run_budget(self):
        from project_sekai.failure_retry import FailureRetryBudget
        budget = FailureRetryBudget(2)
        for attempt in ('round1-death', 'round2-death', 'round4-death'):
            budget.record_failure(attempt)
            permitted = budget.reserve_retry(attempt)
            self.assertEqual(permitted, attempt != 'round4-death')
        self.assertEqual((budget.failure_count, budget.retries_used), (3, 2))
        self.assertEqual(FailureRetryBudget(2).failure_count, 0)

    def test_disabled_budget_still_counts_a_real_death_without_retry(self):
        from project_sekai.failure_retry import FailureRetryBudget
        budget = FailureRetryBudget(0)
        budget.record_failure('no-diagnostic-report')
        self.assertFalse(budget.reserve_retry('no-diagnostic-report'))
        self.assertEqual((budget.failure_count, budget.retries_used), (1, 0))


class FailureRetryRunTests(unittest.TestCase):
    def solo(self, root, outcomes):
        live = SoloLive.__new__(SoloLive)
        live.report_root = root
        live.device = Mock()
        live.stop_requested = lambda: False
        live.log = Mock()
        live.repository = Mock()
        live.repository.songs = {1: {'charts': {'easy': {'music_id': 1, 'difficulty': 'easy',
            'sha256': 'offline', 'path': 'offline.sus', 'total_note_count': 1}}}}
        identity = SimpleNamespace(song_id=1, to_dict=lambda: {'song_id': 1})
        live.select = Mock(return_value=(identity, np.zeros((1, 1, 3), np.uint8)))
        live.prepare_bonus = Mock(side_effect=lambda requested, report, *args, **kwargs:
            report.update(bonus={'requested_consumption': requested, 'consumption': 0, 'confirmed': True,
                                 'original_consumption': 5, 'readings': []}))
        live.prepare_playback = Mock()
        live.ensure_bonus_available = Mock()
        live.start = Mock(return_value=1.)
        outcomes = iter(outcomes)
        def play(events, epoch, offset, report, directory):
            report['playback'] = {'engine': 'legacy', 'planned_actions': 1, 'sent_actions': 1,
                                  'release_confirmed': True}
            error = next(outcomes)
            if error:
                raise error
        live.play = Mock(side_effect=play)
        live.collect = Mock(side_effect=lambda report, directory: report.update(
            completed=True, performance_completed=True, returned_home=True))
        live.exit_depleted_live = Mock(side_effect=lambda report, directory: report.update(
            life_exit={'confirmed': True, 'page': 'home'}))
        return live

    def run_solo(self, live, count, limit):
        with patch('project_sekai.solo_live.parse_sus', return_value=SimpleNamespace(duration=1)), \
                patch('project_sekai.solo_live.compile_touches', return_value=[]):
            return live.run(count, 'easy', 'current', bonus_consumption=0, play_failure_retry_count=limit)

    def test_actual_solo_run_keeps_one_budget_across_success_and_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            live = self.solo(Path(directory), [LifeDepleted('first'), None, LifeDepleted('second')])
            with self.assertRaises(LifeDepleted):
                self.run_solo(live, 2, 1)
            self.assertEqual(live.play.call_count, 3)
            self.assertEqual(live.performed_rounds, 1)
            self.assertEqual((live.failure_retry_budget.failure_count, live.failure_retry_budget.retries_used), (2, 1))
            self.assertEqual(live.prepare_bonus.call_count, 1)
            self.assertEqual(live.exit_depleted_live.call_count, 2)

    def test_solo_does_not_retry_without_safe_home_release_or_after_stop(self):
        for condition in ('home', 'release', 'stop', 'ordinary'):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as directory:
                live = self.solo(Path(directory), [RuntimeError('identity') if condition == 'ordinary' else LifeDepleted('zero')])
                if condition == 'home':
                    live.exit_depleted_live.side_effect = lambda report, directory: None
                elif condition == 'release':
                    original = live.play.side_effect
                    def no_release(*args):
                        try:
                            original(*args)
                        finally:
                            args[3]['playback']['release_confirmed'] = False
                    live.play.side_effect = no_release
                elif condition == 'stop':
                    live.exit_depleted_live.side_effect = lambda report, directory: setattr(live, 'stop_requested', lambda: True)
                with self.assertRaises((LifeDepleted, RuntimeError, InterruptedError)):
                    self.run_solo(live, 1, 1)
                self.assertEqual(live.play.call_count, 1)
                self.assertEqual(live.failure_retry_budget.retries_used, 0)

    def test_solo_finally_confirmed_preplay_death_uses_the_same_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            live = self.solo(Path(directory), [None])
            live.start.side_effect = [RuntimeError('original anchor refusal'), 1.]
            def followup(report):
                if live.start.call_count == 1:
                    report.update(preplay_death_confirmed=True, preplay_followup_death_reason='two strict zero frames')
                    return True
                return False
            live.recheck_failed_preplay_life = followup
            reports = self.run_solo(live, 1, 1)
            self.assertEqual((live.start.call_count, live.play.call_count, live.performed_rounds), (2, 1, 1))
            self.assertIn('original anchor refusal', reports[0]['error'])
            self.assertEqual(live.failure_retry_budget.failure_count, 1)

    def test_failure_publishing_waits_until_current_release_before_exit(self):
        from project_sekai.failure_retry import FailureRetryBudget
        changes = []
        budget = FailureRetryBudget(1, on_change=changes.append)
        budget.record_failure('death', publish=False)
        self.assertEqual(changes, [])
        self.assertEqual(budget.failure_count, 1)
        budget.publish()
        self.assertEqual(len(changes), 1)

    def test_cooperative_retry_uses_same_run_and_does_not_decrement_remaining_goal(self):
        from tests.test_cooperative_live import CooperativeTests
        with tempfile.TemporaryDirectory() as directory:
            owner = CooperativeTests()
            live = owner.run_workflow(Path(directory))
            original = live.play.side_effect
            calls = []
            def play(*args):
                original(*args)
                calls.append(args[3]['round'])
                if len(calls) in (1, 3):
                    raise LifeDepleted('strict zero')
            live.play.side_effect = play
            live.finish_death = Mock(side_effect=lambda report, directory: report.update(background_exit={'confirmed': True}))
            live.restart_after_death = Mock()
            with self.assertRaises(LifeDepleted):
                owner.execute(live, count=2, play_failure_retry_count=1)
            self.assertEqual(calls, [1, 1, 2])
            self.assertEqual(live.performed_rounds, 1)
            self.assertEqual((live.failure_retry_budget.failure_count, live.failure_retry_budget.retries_used), (2, 1))
            live.restart_after_death.assert_called_once()

    def test_cooperative_native_close_warning_does_not_override_current_release_proof(self):
        from tests.test_cooperative_live import CooperativeTests
        with tempfile.TemporaryDirectory() as directory:
            owner = CooperativeTests()
            live = owner.run_workflow(Path(directory))
            first, second = Mock(), Mock()
            for player in (first, second):
                player.report = {'engine': 'native', 'planned_actions': 2, 'sent_actions': 2,
                                 'executed_actions': 2, 'release_confirmed': False}
            first.play.side_effect = LifeDepleted('strict zero')
            def close_first():
                if first.close.call_count == 1:
                    raise RuntimeError('historical cleanup warning')
                first.report.update(release_confirmed=True, release={'reset_executed': True,
                    'release_proof': 'current-reset-jlog-and-cleanup'})
            first.close.side_effect = close_first
            second.close.side_effect = lambda: second.report.update(release_confirmed=True,
                release={'reset_executed': True, 'release_proof': 'current-reset-jlog-and-cleanup'})
            live.pause = lambda _: None
            def restart(report, directory):
                self.assertIn('historical cleanup warning', report['release_error'])
                self.assertIsNone(live.pending_native_player)
                self.assertTrue(report['background_exit']['confirmed'])
            live.restart_after_death = Mock(side_effect=restart)
            with patch('project_sekai.native_player.NativePlayer', side_effect=[first, second]):
                owner.execute(live, count=1, engine='native', play_failure_retry_count=1)
            self.assertEqual(first.close.call_count, 2)
            live.restart_after_death.assert_called_once()
            self.assertEqual(live.performed_rounds, 1)

    def test_cooperative_restart_checks_shell_exit_and_new_login_without_repeated_force_stop(self):
        from project_sekai.cooperative_live import CooperativeLive
        from tests.test_cooperative_live import CooperativeTests
        report = {'engine': 'native', 'playback': {'sent_actions': 2, 'release_confirmed': True,
            'release': {'reset_executed': True, 'release_proof': 'current-reset-jlog-and-cleanup'}},
            'background_exit': {'confirmed': True}}
        for failure in (None, 'shell', 'pid', 'probe', 'login', 'stop-after-force', 'stop-after-pid'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                live = CooperativeTests().run_workflow(Path(directory))
                stopped = []
                def shell(command):
                    if failure == 'shell':
                        raise RuntimeError('job failed')
                    if command.startswith('am force-stop'):
                        if failure == 'stop-after-force':
                            stopped.append(True)
                        return ''
                    if failure == 'stop-after-pid':
                        stopped.append(True)
                    return ('MAAPJSK_RUNNING' if failure == 'pid' else
                            'MAAPJSK_PROBE_FAILED' if failure == 'probe' else 'MAAPJSK_STOPPED')
                live.stop_requested = lambda: bool(stopped)
                live.device.checked_shell.side_effect = shell
                login = Mock()
                def login_run(**kwargs):
                    if failure == 'login':
                        raise TimeoutError('login failed')
                    return {'completed': True, 'phase': 'home_confirmed'}
                login.run.side_effect = login_run
                with patch('project_sekai.cooperative_live.GameLogin', return_value=login) as factory:
                    if failure is None:
                        CooperativeLive.restart_after_death(live, dict(report), Path(directory))
                        login.run.assert_called_once_with(wait_for_home=True)
                        self.assertIs(factory.call_args.args[0], live.device)
                        factory.call_args.kwargs['launch_game']()
                        self.assertTrue(live.device.checked_shell.call_args.args[0].startswith('monkey -p com.sega.pjsekai'))
                    else:
                        with self.assertRaises((RuntimeError, TimeoutError, InterruptedError)):
                            CooperativeLive.restart_after_death(live, dict(report), Path(directory))
                    force = [call for call in live.device.checked_shell.call_args_list if call.args[0].startswith('am force-stop')]
                    self.assertEqual(len(force), 1)
                    if failure not in (None, 'login'):
                        login.run.assert_not_called()

    def test_fixed_process_probe_distinguishes_stopped_from_pidof_errors(self):
        from project_sekai.cooperative_live import CooperativeLive
        from tests.test_cooperative_live import CooperativeTests
        shell = shutil.which('sh')
        git = shutil.which('git')
        if shell is None and git:
            bundled = Path(git).parent.parent / 'usr/bin/sh.exe'
            if bundled.is_file():
                shell = str(bundled)
        if shell is None:
            self.skipTest('本机没有可运行固定脚本的离线 sh')
        with tempfile.TemporaryDirectory() as directory:
            live = CooperativeTests().run_workflow(Path(directory))
            live.device.checked_shell.side_effect = ['', 'MAAPJSK_STOPPED']
            report = {'playback': {'release_confirmed': True}, 'background_exit': {'confirmed': True}}
            login = Mock()
            login.run.return_value = {'completed': True, 'phase': 'home_confirmed'}
            with patch('project_sekai.cooperative_live.GameLogin', return_value=login):
                CooperativeLive.restart_after_death(live, report, Path(directory))
            probe = live.device.checked_shell.call_args.args[0]
            for code, expected in ((0, 'MAAPJSK_RUNNING'), (1, 'MAAPJSK_STOPPED'),
                                   (2, 'MAAPJSK_PROBE_FAILED'), (127, 'MAAPJSK_PROBE_FAILED')):
                with self.subTest(code=code):
                    # 函数桩完全替代 pidof，不查询任何真实进程，也不连接设备。
                    result = subprocess.run([shell, '-c', f'pidof() {{ return {code}; }}; ' + probe],
                                            check=True, capture_output=True, text=True)
                    self.assertEqual(result.stdout.strip(), expected)

    def test_actual_cooperative_run_stops_before_login_when_process_probe_fails(self):
        from tests.test_cooperative_live import CooperativeTests
        with tempfile.TemporaryDirectory() as directory:
            owner = CooperativeTests()
            live = owner.run_workflow(Path(directory))
            play = live.play.side_effect
            def depleted(*args):
                play(*args)
                raise LifeDepleted('strict zero')
            live.play.side_effect = depleted
            live.device.checked_shell.side_effect = ['', 'MAAPJSK_PROBE_FAILED']
            with patch('project_sekai.cooperative_live.GameLogin') as login, self.assertRaisesRegex(RuntimeError, '进程未明确关闭'):
                owner.execute(live, count=1, play_failure_retry_count=1)
            login.assert_not_called()
            self.assertEqual(live.play.call_count, 1)
            self.assertEqual(live.device.checked_shell.call_count, 2)

    def test_checked_shell_never_reads_output_from_a_failed_job(self):
        from project_sekai.maa_device import MaaDevice
        device = MaaDevice.__new__(MaaDevice)
        device.controller = Mock()
        job = device.controller.post_shell.return_value.wait.return_value
        job.succeeded = False
        with patch('project_sekai.maa_device._bind_shell_api'), self.assertRaises(RuntimeError):
            device.checked_shell('am force-stop com.sega.pjsekai')
        job.get.assert_not_called()
        job.succeeded = True
        job.get.return_value = 'MAAPJSK_STOPPED'
        with patch('project_sekai.maa_device._bind_shell_api'):
            self.assertEqual(device.checked_shell('fixed probe'), 'MAAPJSK_STOPPED')

    def test_invalid_limits_and_retry_without_confirmed_death_are_rejected(self):
        from project_sekai.failure_retry import FailureRetryBudget
        for value in (-1, 100, True, False, '1', 1., None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, '失败重试'):
                FailureRetryBudget(value)
        with self.assertRaises(ValueError):
            FailureRetryBudget(1).reserve_retry('unconfirmed-failure')

    def test_summary_write_failure_is_visible_before_a_retry_can_be_scheduled(self):
        from project_sekai.failure_retry import FailureRetryBudget
        def fail_write(summary):
            raise OSError('summary unavailable')
        budget = FailureRetryBudget(1, on_change=fail_write)
        with self.assertRaises(OSError):
            budget.record_failure('death')
        self.assertEqual((budget.failure_count, budget.retries_used), (1, 0))


if __name__ == '__main__':
    unittest.main()
