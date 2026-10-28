import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from project_sekai.cooperative_live import CooperativeLive, EntryRecoveryExhausted


class EntryNoticeTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.clock = 0.
        self.state = 'notice'
        self.stopped = False
        self.frames = {}
        self.workflow = workflow = CooperativeLive.__new__(CooperativeLive)
        workflow.entry_notice_guard = True
        workflow.current_directory = workflow.report_root = Path(self.root.name)
        workflow.current_report = {'attempts': [], 'completed': False, 'engine': 'native',
                                   'started_at': 'now', 'room': 'free', 'requested_difficulty': 'expert'}
        workflow.matched = False
        workflow.stop_requested = lambda: self.stopped or self.clock >= 250
        workflow.navigator = Mock(threshold=.83)
        workflow.navigator.templates = {'life_hud', 'playing', 'home', 'title_screen'}
        workflow.device = Mock()
        workflow.device.screenshot.side_effect = self.screenshot
        workflow.device.shell.return_value = 'mCurrentFocus=Window{ com.sega.pjsekai/Main }'
        workflow.device.back.side_effect = lambda: setattr(self, 'state', 'home')
        workflow.score = self.score
        workflow.navigator.match.side_effect = lambda frame, page, *args: (self.score(frame, page), (0, 0))
        workflow.navigator._check_stop.side_effect = self.check_stop
        workflow.pause = lambda seconds: setattr(self, 'clock', self.clock + seconds)
        workflow.log = Mock()
        workflow.ocr = Mock()
        expected = {(174, 29, 299, 59): 'お知らせ', (491, 33, 550, 60): '一覧',
                    (729, 33, 791, 60): '不具合', (930, 34, 1054, 61): '関連サイト'}
        workflow.ocr.read.side_effect = lambda frame, box: SimpleNamespace(
            text=expected[box] if self.frame_state(frame) in {'notice', 'notice_hud', 'notice_conflict'} else '',
            confidence=.99)
        workflow.settlement_page = Mock(return_value=None)
        workflow.persist_runtime_report = Mock()
        workflow.observe_recovery_death = Mock(return_value=False)
        workflow.screenshot = workflow.device.screenshot
        workflow.navigator.tap.side_effect = self.tap
        self.patch_clock = patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: self.clock)
        self.patch_clock.start()
        self.addCleanup(self.patch_clock.stop)
        image_patch = patch('project_sekai.cooperative_live.write_image')
        self.images = image_patch.start()
        self.addCleanup(image_patch.stop)

    def frame_state(self, frame):
        return next(name for name, image in self.frames.items() if image is frame)

    def screenshot(self):
        return self.frames.setdefault(self.state, np.full((72, 128, 3), 70 + len(self.frames), np.uint8))

    def score(self, frame, page):
        state = self.frame_state(frame)
        return float(page == state or state == 'notice_hud' and page == 'playing'
                     or state == 'notice_conflict' and page == 'cooperative_ready')

    def check_stop(self):
        if self.workflow.stop_requested():
            raise InterruptedError('停止')

    def tap(self, x, y, reason):
        self.state = 'live_menu' if self.state == 'home' else 'cooperative_room'

    def recover(self, report=None):
        report = self.workflow.current_report if report is None else report
        report.setdefault('error', 'TimeoutError: 原始入口超时')
        return self.workflow.recover_runtime_failure(report, Path(self.root.name), TimeoutError('原始入口超时'))

    def test_open_rooms_notice_back_then_original_home_menu_room_chain(self):
        self.workflow.open_rooms()
        self.workflow.device.back.assert_called_once_with()
        self.assertEqual(self.state, 'cooperative_room')
        self.assertEqual(self.workflow.entry_request_count, 1)
        self.assertEqual(self.workflow.menu_request_count, 1)
        self.assertFalse(self.workflow.current_report['completed'])

    def test_expired_entry_notice_closure_reopens_window_and_keeps_button_budgets(self):
        self.workflow.entry_deadline = -1
        self.workflow.entry_request_count = 1
        self.workflow.menu_request_count = 1
        self.recover()
        self.assertGreater(self.workflow.entry_deadline, self.clock)
        self.workflow.open_rooms()
        self.assertEqual((self.workflow.entry_request_count, self.workflow.menu_request_count), (2, 2))
        self.assertFalse(self.workflow.current_report['completed'])

    def test_unknown_recovery_is_terminal_with_original_error_without_input(self):
        self.state = 'unknown'
        with self.assertRaisesRegex(RuntimeError, '120') as raised:
            self.recover()
        self.assertEqual(type(raised.exception).__name__, 'EntryRecoveryExhausted')
        self.assertLessEqual(self.clock, 122)
        self.assertEqual(self.workflow.current_report['error'], 'TimeoutError: 原始入口超时')
        self.workflow.device.back.assert_not_called()
        self.workflow.device.tap.assert_not_called()

    def test_run_propagates_terminal_once_without_new_report_or_completed_count(self):
        self.state = 'unknown'
        self.workflow.make_report_directory = Mock()
        with self.assertRaises(EntryRecoveryExhausted):
            self.workflow.run(99, 'expert', 'random', engine='native')
        self.assertEqual((self.workflow.performed_rounds, self.workflow.completed_rounds), (0, 0))
        self.workflow.make_report_directory.assert_called_once()
        self.assertIn('TimeoutError:', self.workflow.last_report['error'])
        self.assertIn('120', self.workflow.last_report['entry_recovery_error'])
        self.assertEqual(self.workflow.failure_retry_budget.failure_count, 0)

    def test_three_ineffective_requests_share_deadline_then_terminal_without_blind_restart(self):
        times = []
        self.workflow.device.back.side_effect = lambda: times.append(self.clock)
        with self.assertRaises(EntryRecoveryExhausted):
            self.recover()
        self.assertEqual(len(times), 3)
        self.assertTrue(all(b - a >= 1 for a, b in zip(times, times[1:])))
        self.assertFalse(self.workflow.entry_notice_recovery['closed'])
        self.assertLessEqual(len(self.images.call_args_list), 3)
        self.assertTrue(all('dumpsys' in call.args[0] for call in self.workflow.device.shell.call_args_list))

    def test_ack_failure_consumes_request_and_shared_budget_across_reports(self):
        self.workflow.device.back.side_effect = OSError('ACK失败')
        report1 = self.workflow.current_report
        with self.assertRaises(OSError):
            self.workflow.handle_entry_notice(self.screenshot(), report1, Path(self.root.name))
        deadline = self.workflow.entry_notice_recovery['deadline']
        self.clock += 1
        report2 = {'attempts': [], 'completed': False}
        with self.assertRaises(OSError):
            self.workflow.handle_entry_notice(self.screenshot(), report2, Path(self.root.name))
        self.assertIs(report1['entry_notice_recovery'], report2['entry_notice_recovery'])
        self.assertEqual((report2['entry_notice_recovery']['requests'], report2['entry_notice_recovery']['deadline']), (2, deadline))
        self.clock = 21
        self.assertFalse(self.workflow.handle_entry_notice(self.screenshot(), report2, Path(self.root.name)))
        self.workflow.device.back.assert_called_with()
        self.assertEqual(self.workflow.device.back.call_count, 2)

    def test_ack_failure_after_actual_back_still_requires_two_home_frames(self):
        def failed_ack():
            self.state = 'home'
            raise OSError('ACK丢失')
        self.workflow.device.back.side_effect = failed_ack
        self.recover()
        self.assertTrue(self.workflow.entry_notice_recovery['closed'])
        self.workflow.device.back.assert_called_once()
        self.assertEqual(self.workflow.entry_notice_recovery['home_confirmation_frames'], 2)

    def test_twenty_second_deadline_after_slow_log_sends_no_back(self):
        self.workflow.log.side_effect = lambda *_: setattr(self, 'clock', self.clock + 21)
        self.assertFalse(self.workflow.handle_entry_notice(self.screenshot(), self.workflow.current_report, Path(self.root.name)))
        self.workflow.device.back.assert_not_called()

    def test_logging_transition_to_hud_blocks_back(self):
        self.workflow.log.side_effect = lambda *_: setattr(self, 'state', 'notice_hud')
        self.assertFalse(self.workflow.handle_entry_notice(self.screenshot(), self.workflow.current_report, Path(self.root.name)))
        self.workflow.device.back.assert_not_called()
        self.assertTrue(self.workflow.entry_notice_disqualified)

    def test_stop_during_evidence_or_log_wins(self):
        for hook in (self.images, self.workflow.log):
            with self.subTest(hook=hook):
                self.stopped = False
                hook.side_effect = lambda *_: setattr(self, 'stopped', True)
                with self.assertRaises(InterruptedError):
                    self.workflow.handle_entry_notice(self.screenshot(), self.workflow.current_report, Path(self.root.name))
                self.workflow.device.back.assert_not_called()
                hook.side_effect = None

    def test_unknown_fake_labels_hud_conflict_or_nonforeground_never_back(self):
        for state in ('unknown', 'home', 'notice_hud', 'notice_conflict', 'title_screen'):
            with self.subTest(state=state):
                self.state = state
                self.workflow.entry_notice_disqualified = False
                self.workflow.handle_entry_notice(self.screenshot(), self.workflow.current_report, Path(self.root.name))
                self.workflow.device.back.assert_not_called()
        self.state = 'notice'
        self.workflow.entry_notice_disqualified = False
        self.workflow.ocr.read.side_effect = lambda *_: SimpleNamespace(text='お知らせ', confidence=.99)
        self.workflow.handle_entry_notice(self.screenshot(), self.workflow.current_report, Path(self.root.name))
        self.workflow.device.back.assert_not_called()

    def test_foreground_changed_during_logging_prevents_input(self):
        self.workflow.log.side_effect = lambda *_: setattr(self.workflow.device.shell, 'return_value', 'mCurrentFocus=Window{ launcher }')
        self.workflow.handle_entry_notice(self.screenshot(), self.workflow.current_report, Path(self.root.name))
        self.workflow.device.back.assert_not_called()

    def test_foreground_change_during_fresh_screenshot_or_notice_ocr_prevents_back(self):
        original_read = self.workflow.ocr.read.side_effect
        for during in ('fresh_screenshot', 'fresh_ocr'):
            with self.subTest(during=during):
                self.workflow.device.shell.return_value = 'mCurrentFocus=Window{ com.sega.pjsekai/Main }'
                self.workflow.entry_notice_recovery = {'requests': 0, 'closed': False}
                reads = [0]
                def fresh_screenshot():
                    if during == 'fresh_screenshot':
                        self.workflow.device.shell.return_value = 'mCurrentFocus=Window{ launcher }'
                    return self.screenshot()
                def ocr_read(frame, box):
                    reads[0] += 1
                    if during == 'fresh_ocr' and reads[0] == 5:
                        self.workflow.device.shell.return_value = 'mCurrentFocus=Window{ launcher }'
                    return original_read(frame, box)
                self.workflow.device.screenshot.side_effect = fresh_screenshot
                self.workflow.ocr.read.side_effect = ocr_read
                self.assertFalse(self.workflow.handle_entry_notice(self.screenshot(), self.workflow.current_report, Path(self.root.name)))
                self.workflow.device.back.assert_not_called()

    def test_lifecycle_boundaries_prevent_notice_input_and_terminal(self):
        cases = [({'join_request_count': 1}, {}), ({'matched': True}, {}),
                 ({'room_progress': {'stage': 'matching'}}, {}), ({'resume_stage': 'ready'}, {}),
                 ({'pending_native_player': object()}, {}), ({}, {'joined_room': True}),
                 ({}, {'attempts': [{'join_confirmed_at': 0}]}),
                 ({}, {'ready_confirmed': True}), ({}, {'death_confirmed': True}),
                 ({}, {'playback': {'sent_actions': 1}}),
                 ({}, {'playback': {'sent_actions': 0, 'release_confirmed': False}}),
                 ({}, {'engine': 'native', 'playback': {'sent_actions': 0, 'release_confirmed': True, 'release': {}}})]
        for attrs, fields in cases:
            with self.subTest(attrs=attrs, fields=fields):
                old = {key: getattr(self.workflow, key, None) for key in attrs}
                for key, value in attrs.items():
                    setattr(self.workflow, key, value)
                report = dict(self.workflow.current_report, **fields)
                self.assertFalse(self.workflow.handle_entry_notice(self.screenshot(), report, Path(self.root.name)))
                self.workflow.check_entry_recovery_deadline(report, RuntimeError('原错'))
                self.workflow.device.back.assert_not_called()
                for key, value in old.items():
                    if value is None:
                        delattr(self.workflow, key)
                    else:
                        setattr(self.workflow, key, value)

    def test_native_zero_input_requires_current_strict_cleanup_proof(self):
        self.workflow.current_report['playback'] = {'sent_actions': 0, 'release_confirmed': True,
            'release': {'release_proof': 'no-touch-possible-and-cleanup'}}
        self.assertTrue(self.workflow.handle_entry_notice(self.screenshot(), self.workflow.current_report, Path(self.root.name)))
        self.workflow.device.back.assert_called_once()

    def test_explicit_rollback_keeps_existing_timeout_and_never_uses_notice_ocr(self):
        self.workflow.entry_notice_guard = False
        with self.assertRaises(TimeoutError):
            self.workflow.open_rooms()
        self.workflow.device.back.assert_not_called()
        self.workflow.ocr.read.assert_not_called()
        with self.assertRaises(InterruptedError):
            self.recover()
        self.assertNotIn('entry_recovery_error', self.workflow.current_report)

    def test_connection_failures_do_not_reset_recovery_120_seconds(self):
        self.workflow.device.screenshot.side_effect = OSError('截图连接异常')
        with self.assertRaises(EntryRecoveryExhausted):
            self.recover()
        self.assertLessEqual(self.clock, 121)
        self.workflow.device.back.assert_not_called()

    def test_notice_ack_failures_do_not_reset_terminal_or_request_budget(self):
        self.workflow.device.back.side_effect = OSError('ACK失败')
        with self.assertRaises(EntryRecoveryExhausted):
            self.recover()
        self.assertEqual(self.workflow.device.back.call_count, 3)
        self.assertLessEqual(self.clock, 122)

    def test_single_home_or_conflicting_home_cannot_confirm_closed(self):
        self.workflow.handle_entry_notice(self.screenshot(), self.workflow.current_report, Path(self.root.name))
        self.workflow.device.screenshot.side_effect = lambda: self.frames.setdefault('unknown', np.full((72, 128, 3), 90, np.uint8))
        self.assertFalse(self.workflow.handle_entry_notice(self.screenshot(), self.workflow.current_report, Path(self.root.name)))
        self.assertFalse(self.workflow.entry_notice_recovery['closed'])
        self.workflow.device.preflight.assert_not_called()

    def test_preflight_failure_does_not_confirm_notice_closed_or_reset_entry_window(self):
        self.workflow.entry_deadline = -1
        self.workflow.handle_entry_notice(self.screenshot(), self.workflow.current_report, Path(self.root.name))
        self.workflow.device.preflight.side_effect = OSError('环境变化')
        with self.assertRaises(OSError):
            self.workflow.handle_entry_notice(self.screenshot(), self.workflow.current_report, Path(self.root.name))
        self.assertFalse(self.workflow.entry_notice_recovery['closed'])
        self.assertEqual(self.workflow.entry_deadline, -1)

    def test_guard_is_default_on_with_explicit_zero_rollback_and_frozen_at_initialization(self):
        for raw, expected in ((None, True), ('0', False), ('1', True)):
            with self.subTest(raw=raw), patch.dict('os.environ', {}, clear=True), \
                    patch('project_sekai.cooperative_live.SoloLive.__init__', return_value=None), \
                    patch('project_sekai.cooperative_live.CooperativeNavigator'):
                import os
                if raw is not None:
                    os.environ['MAAPJSK_COOPERATIVE_ENTRY_NOTICE_GUARD'] = raw
                root = Path(self.root.name)
                workflow = CooperativeLive(Mock(), root / 'base.json', root / 'coop.json', root, root, root)
                os.environ['MAAPJSK_COOPERATIVE_ENTRY_NOTICE_GUARD'] = '0' if expected else '1'
                self.assertIs(workflow.entry_notice_guard, expected)

    def test_failed_persistence_reaches_same_terminal_without_masking_original_error(self):
        self.workflow.save_evidence = Mock(side_effect=OSError('磁盘不可写'))
        self.workflow.current_report.update(error='TimeoutError: 原始入口超时', started_at='now',
                                           room='free', requested_difficulty='expert')
        with patch('project_sekai.cooperative_live._write_json', side_effect=OSError('磁盘不可写')), \
                patch('project_sekai.cooperative_live.append_cooperative_result_index', side_effect=OSError('磁盘不可写')):
            with self.assertRaises(EntryRecoveryExhausted):
                CooperativeLive.persist_runtime_report(self.workflow, self.workflow.current_report, Path(self.root.name))
        self.assertLessEqual(self.clock, 121)
        self.assertEqual(self.workflow.current_report['error'], 'TimeoutError: 原始入口超时')
        self.assertIn('磁盘不可写', self.workflow.current_report['persistence_error'])
        self.assertEqual(self.workflow.current_report['task_state'], 'entry_recovery_exhausted')

    def test_disabled_diagnostics_keeps_terminal_and_does_not_write_notice_images(self):
        with patch('project_sekai.diagnostics._disabled', True):
            self.workflow.handle_entry_notice(self.screenshot(), self.workflow.current_report, Path(self.root.name))
            self.images.assert_not_called()
            self.state = 'unknown'
            with self.assertRaises(EntryRecoveryExhausted):
                self.recover()
            self.assertEqual(self.workflow.current_report['task_state'], 'entry_recovery_exhausted')

    def test_room_or_hud_newly_visible_at_deadline_blocks_terminal_before_screenshot(self):
        for state in ('cooperative_ready', 'notice_hud'):
            with self.subTest(state=state):
                self.clock = 120
                self.state = state
                self.workflow.entry_notice_disqualified = False
                self.workflow.entry_notice_recovery = {'requests': 0, 'closed': False, 'recovery_deadline': 120}
                self.workflow.pause = lambda seconds: setattr(self, 'stopped', True)
                self.stopped = False
                with self.assertRaises(InterruptedError):
                    self.recover()
                self.assertTrue(self.workflow.entry_notice_disqualified)
                self.assertNotIn('entry_recovery_error', self.workflow.current_report)
                self.workflow.current_report.pop('cancelled', None)

    def test_repeated_ocr_failure_still_reaches_terminal(self):
        self.workflow.ocr.read.side_effect = RuntimeError('OCR失效')
        with self.assertRaises(EntryRecoveryExhausted):
            self.recover()
        self.assertLessEqual(self.clock, 122)
        self.workflow.device.back.assert_not_called()

    def test_real_run_room_arrival_then_bonus_failure_keeps_original_resource_wait(self):
        self.state = 'cooperative_room'
        self.workflow.make_report_directory = Mock()
        def bonus_failure(consumption, report, *args, **kwargs):
            report['bonus'] = {'availability': 'insufficient', 'consumption': 5}
            raise ValueError('体力不足，等待用户处理')
        self.workflow.prepare_task_bonus = Mock(side_effect=bonus_failure)
        self.workflow.read_available_bonus = Mock(return_value=(0, []))
        with self.assertRaises(InterruptedError):
            self.workflow.run(99, 'expert', 'random', engine='native')
        self.assertGreaterEqual(self.clock, 120)
        self.assertTrue(self.workflow.entry_notice_disqualified)
        self.assertEqual(self.workflow.last_report['runtime_recovery']['state'], 'waiting_resource')
        self.assertNotIn('entry_recovery_error', self.workflow.last_report)
        self.assertEqual((self.workflow.performed_rounds, self.workflow.completed_rounds), (0, 0))
        self.workflow.device.back.assert_not_called()
        self.workflow.make_report_directory.assert_called_once()

    def test_room_destination_at_recovery_deadline_finishes_entry_episode(self):
        self.state = 'cooperative_room'
        self.clock = 120
        self.workflow.entry_notice_recovery = {'requests': 0, 'closed': False, 'recovery_deadline': 120}
        self.recover()
        self.assertTrue(self.workflow.entry_notice_disqualified)
        self.assertNotIn('entry_recovery_error', self.workflow.current_report)
        replacement = dict(self.workflow.current_report, run_id='new-report', attempts=[])
        self.assertFalse(self.workflow.entry_notice_scope(replacement))

    def test_resource_or_unconfirmed_consumption_never_uses_entry_terminal_even_without_latch(self):
        for fields in ({'bonus': {'availability': 'insufficient'}}, {'recovery': {'status': 'started'}}):
            with self.subTest(fields=fields):
                report = dict(self.workflow.current_report, **fields)
                self.clock = 200
                self.workflow.entry_notice_recovery = {'requests': 0, 'closed': False, 'recovery_deadline': 120}
                self.assertFalse(self.workflow.entry_notice_scope(report))
                self.workflow.check_entry_recovery_deadline(report, ValueError('等待用户处理'))
                self.assertFalse(self.workflow.handle_entry_notice(self.screenshot(), report, Path(self.root.name)))
                self.workflow.device.back.assert_not_called()


if __name__ == '__main__':
    unittest.main()
