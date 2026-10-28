import tempfile
import unittest
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np

from project_sekai.cooperative_live import CooperativeLive


class RoomSearchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.now = 60.
        self.state = 'search'
        self.stopped = False
        self.foreground = True
        self.workflow = w = CooperativeLive.__new__(CooperativeLive)
        w.room_search_guard = True
        w.entry_notice_guard = True
        w.entry_notice_disqualified = True
        w.join_request_count = 1
        w.join_deadline = 60.
        w.max_rematches = 3
        w.round_rematches = 0
        w.matched = False
        w.room_progress = {}
        w.current_directory = w.report_root = self.directory
        w.log = Mock()
        w.stop_requested = lambda: self.stopped or self.now >= 600
        w.pause = lambda seconds: setattr(self, 'now', self.now + seconds)
        w.device = Mock()
        w.device.screenshot.side_effect = self.screenshot
        w.screenshot = w.device.screenshot
        w.device.shell.side_effect = lambda *_: 'mCurrentFocus=Window{ ' + ('com.sega.pjsekai/Main' if self.foreground else 'launcher') + ' }'
        w.device.back.side_effect = lambda: setattr(self, 'state', 'room')
        w.navigator = Mock(threshold=.83)
        w.navigator.templates = {'life_hud', 'playing', 'cooperative_matching', 'cooperative_select', 'cooperative_ready', 'cooperative_cancel'}
        w.navigator._check_stop.side_effect = lambda: None
        w.score = self.score
        w.navigator.match.side_effect = lambda frame, page, *_: (self.score(frame, page), (0, 0))
        w.ocr = Mock()
        self.labels = {(638, 95, 840, 136): 'SELECT ROOM', (508, 223, 775, 263): 'ルームを探しています…',
                       (579, 436, 704, 477): 'キャンセル'}
        w.ocr.read.side_effect = lambda frame, box: SimpleNamespace(text=self.labels.get(box, ''), confidence=.99)
        w.settlement_page = Mock(return_value=None)
        w.observe_recovery_death = Mock(return_value=False)
        w.save_evidence = Mock()
        self.report = {'attempts': [{}], 'error': 'TimeoutError: 原入房60秒超时', 'failure_reason': 'TimeoutError: 原入房60秒超时',
                       'completed': False, 'engine': 'native', 'started_at': 'now', 'room': 'veteran', 'requested_difficulty': 'expert'}
        w.current_report = w.last_report = self.report
        self.frames = {}
        self.patchers = [patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: self.now),
                         patch('project_sekai.cooperative_live.write_image'),
                         patch('project_sekai.cooperative_live._write_json'),
                         patch('project_sekai.cooperative_live.append_cooperative_result_index')]
        self.clock, self.images, self.json, self.index = [p.start() for p in self.patchers]
        for p in self.patchers:
            self.addCleanup(p.stop)

    def screenshot(self):
        if self.state not in self.frames:
            frame = np.full((720, 1280, 3), 80, np.uint8)
            if self.state == 'search':
                cv2.rectangle(frame, (560, 430), (720, 482), (255, 255, 255), -1)
                cv2.circle(frame, (560, 456), 26, (255, 255, 255), -1)
                cv2.circle(frame, (720, 456), 26, (255, 255, 255), -1)
            if self.state == 'black':
                frame[:] = 0
            self.frames[self.state] = frame
        return self.frames[self.state]

    def score(self, frame, page):
        state = next(key for key, value in self.frames.items() if value is frame)
        return float((state == 'room' and page == 'cooperative_room')
                     or (state == 'matching' and page == 'cooperative_matching')
                     or (state == 'select' and page == 'cooperative_select')
                     or (state == 'hud' and page in {'life_hud', 'playing'}))

    def recover(self):
        return self.workflow.recover_runtime_failure(self.report, self.directory, TimeoutError('原入房60秒超时'))

    def assert_terminal(self, text=None):
        with self.assertRaises(RuntimeError) as raised:
            self.recover()
        self.assertEqual(type(raised.exception).__name__, 'RuntimeRecoveryExhausted')
        if text:
            self.assertIn(text, str(raised.exception))
        self.assertEqual(self.report['error'], 'TimeoutError: 原入房60秒超时')

    def test_search_is_independent_from_joined_room_and_does_not_refresh_join_clock(self):
        frame = self.screenshot()
        self.assertTrue(self.workflow.room_search_visible(frame))
        self.assertIsNone(self.workflow.room_stage(frame))
        self.workflow.stop_requested = lambda: False
        with self.assertRaises(TimeoutError):
            self.workflow.confirm_room_join(self.report['attempts'][0])
        self.assertEqual(self.workflow.join_deadline, 60.)
        self.assertFalse(self.workflow.matched)

    def test_expired_search_cancel_requires_two_room_frames_then_keeps_total_join_budget(self):
        self.recover()
        state = self.report['room_search_recovery']
        self.assertTrue(state['search_cancel_confirmed'])
        self.assertEqual(state['room_confirmation_frames'], 2)
        self.workflow.device.back.assert_called_once_with()
        self.assertIsNone(self.workflow.join_deadline)
        self.assertEqual(self.workflow.join_request_count, 1)
        self.assertEqual(self.workflow.round_rematches, 0)
        self.assertFalse(self.report.get('joined_room', False))
        self.assertFalse(self.report['completed'])

    def test_three_ack_failures_consume_budget_then_terminal_without_reset(self):
        times = []
        def back():
            times.append(self.now)
            raise OSError('ACK')
        self.workflow.device.back.side_effect = back
        self.assert_terminal('三次')
        self.assertEqual(len(times), 3)
        self.assertTrue(all(b - a >= 1 for a, b in zip(times, times[1:])))
        self.assertLess(self.now, 85)

    def test_unknown_and_screenshot_error_terminate_in_120_seconds_without_input(self):
        for state in ('unknown', 'screenshot_error'):
            with self.subTest(state=state):
                self.now = 60.
                self.state = 'unknown'
                self.workflow.runtime_recovery_episode = None
                if state == 'screenshot_error':
                    self.workflow.device.screenshot.side_effect = OSError('截图失败')
                self.assert_terminal('120')
                self.assertLessEqual(self.now, 182)
        self.workflow.device.back.assert_not_called()

    def test_strict_labels_and_geometry_reject_missing_fake_or_plain_white(self):
        frame = self.screenshot()
        for box in self.labels:
            old = self.labels[box]
            self.labels[box] = old + '假'
            self.assertFalse(self.workflow.room_search_visible(frame))
            self.labels[box] = old
        self.labels[(508, 223, 775, 263)] = 'ルームを探しています...'
        self.assertTrue(self.workflow.room_search_visible(frame))
        frame[412:501, 505:775] = 80
        cv2.rectangle(frame, (534, 430), (746, 482), (255, 255, 255), -1)
        self.assertFalse(self.workflow.room_search_visible(frame))

    def test_pre_input_recheck_foreground_stop_deadline_and_transition(self):
        for change in ('foreground', 'stop', 'deadline', 'matching', 'room', 'hud'):
            with self.subTest(change=change):
                self.now, self.state, self.stopped, self.foreground = 60., 'search', False, True
                self.workflow.room_search_recovery = None
                def log(*_):
                    if change == 'foreground': self.foreground = False
                    elif change == 'stop': self.stopped = True
                    elif change == 'deadline': self.now += 21
                    else: self.state = change
                self.workflow.log.side_effect = log
                if change == 'stop':
                    with self.assertRaises(InterruptedError):
                        self.workflow.handle_room_search(self.screenshot(), self.report, self.directory)
                else:
                    self.workflow.handle_room_search(self.screenshot(), self.report, self.directory)
                self.workflow.device.back.assert_not_called()

    def test_late_matching_on_deadline_uses_original_room_progress_without_back(self):
        self.state = 'matching'
        self.workflow.pause = lambda seconds: (setattr(self, 'now', self.now + seconds), setattr(self, 'state', 'select'))
        self.recover()
        self.assertTrue(self.report['joined_room'])
        self.assertTrue(self.workflow.resume_selection)
        self.workflow.device.back.assert_not_called()

    def test_search_scope_rejects_input_pending_release_death_and_room_history(self):
        for attrs, fields in (({'matched': True}, {}), ({'room_progress': {'stage': 'matching'}}, {}),
                              ({'pending_native_player': object()}, {}), ({}, {'death_confirmed': True}),
                              ({}, {'ready_confirmed': True}), ({}, {'playback': {'sent_actions': 1}}),
                              ({}, {'playback': {'sent_actions': 0, 'release_confirmed': False}})):
            with self.subTest(attrs=attrs, fields=fields):
                saved = {key: getattr(self.workflow, key, None) for key in attrs}
                for key, value in attrs.items(): setattr(self.workflow, key, value)
                report = dict(self.report, **fields)
                self.assertFalse(self.workflow.room_search_scope(report))
                for key, value in saved.items(): setattr(self.workflow, key, value)

    def test_disabled_diagnostics_has_no_writes_and_retains_terminal_in_memory(self):
        self.state = 'unknown'
        with patch('project_sekai.cooperative_live.enabled', return_value=False):
            self.assert_terminal('120')
        self.images.assert_not_called()
        self.json.assert_not_called()
        self.index.assert_not_called()

    def test_real_choose_run_retries_new_reports_but_never_sends_fifth_join(self):
        w = self.workflow
        self.state = 'search'
        self.now = 0.
        w.prepare_task_bonus = Mock()
        w.ensure_bonus_available = Mock()
        w.begin_performance_trace = Mock()
        w.prepare_game_timing_feedback = lambda report, *_: report.update(game_timing_feedback={})
        w.make_report_directory = Mock()
        def reports_bounded(*_):
            if w.make_report_directory.call_count > 5:
                self.fail(f"恢复反复重建报告：{w.last_report.get('error')}；clock={self.now}")
            _[0].mkdir(parents=True, exist_ok=True)
        w.make_report_directory.side_effect = reports_bounded
        order = []
        def join(*_):
            order.append(('join', w.join_request_count))
            self.state = 'search'
        def cancel():
            order.append(('back', w.join_request_count))
            self.state = 'room'
        w.navigator.tap.side_effect = join
        w.device.back.side_effect = cancel
        with self.assertRaisesRegex(RuntimeError, '入房请求总预算') as raised:
            w.run(15, 'expert', 'random', room='veteran', engine='native')
        self.assertEqual(type(raised.exception).__name__, 'RuntimeRecoveryExhausted')
        self.assertEqual(w.navigator.tap.call_count, 4)
        self.assertEqual(w.device.back.call_count, 5)
        self.assertEqual(order[:2], [('back', 0), ('join', 1)])
        self.assertEqual(w.make_report_directory.call_count, 4)
        self.assertEqual((w.join_request_count, w.round_rematches, w.performed_rounds, w.completed_rounds), (4, 0, 0, 0))
        self.assertIn('点击房间后 60 秒', w.last_report['error'])
        self.assertIn('点击房间后 60 秒', w.last_report['failure_reason'])
        self.assertTrue(w.last_report['room_search_recovery']['search_cancel_confirmed'])
        self.assertFalse(w.last_report.get('joined_room', False))
        self.assertEqual(w.failure_retry_budget.failure_count, 0)

    def test_same_ready_stage_failure_across_new_reports_cannot_renew_120_second_budget(self):
        w = self.workflow
        self.state = 'select'
        self.recover()
        deadline = w.runtime_recovery_episode['deadline']
        for index in range(3):
            self.now += 35
            self.report = dict(self.report, attempts=[{}])
            w.current_report = self.report
            self.recover()
            self.assertEqual(w.runtime_recovery_episode['deadline'], deadline)
        self.now = deadline + 1
        self.report = dict(self.report, attempts=[{}])
        w.device.preflight.side_effect = OSError('同阶段准备仍失败')
        self.assert_terminal('120')
        self.assertEqual(w.runtime_recovery_episode['deadline'], deadline)

    def test_pending_release_resource_wait_and_unconfirmed_drink_do_not_use_common_terminal(self):
        w = self.workflow
        for attrs, fields in (({'pending_native_player': object()}, {}),
                              ({}, {'playback': {'sent_actions': 0, 'release_confirmed': False}}),
                              ({}, {'bonus': {'availability': 'insufficient'}}),
                              ({}, {'recovery': {'status': 'started'}})):
            with self.subTest(attrs=attrs, fields=fields):
                for key, value in attrs.items(): setattr(w, key, value)
                w.runtime_recovery_episode = {'join_request': 1, 'stage_rank': -1, 'deadline': 0.}
                report = dict(self.report, **fields)
                self.assertFalse(w.room_search_scope(report))
                w.check_runtime_recovery_deadline(report, RuntimeError('原错误'))
                self.assertNotIn('runtime_recovery_error', report)
                self.assertIn('suspended_at', w.runtime_recovery_episode)
                for key in attrs: setattr(w, key, None)

    def test_deadline_room_return_wins_and_matching_keeps_180_second_clock(self):
        w = self.workflow
        self.state = 'matching'
        w.runtime_recovery_episode = {'join_request': 1, 'stage_rank': 0, 'deadline': 59.}
        w.room_progress = {'stage': 'matching', 'deadline': 240.}
        w.pause = lambda seconds: (setattr(self, 'now', self.now + seconds), setattr(self, 'state', 'select'))
        self.recover()
        self.assertEqual(self.report['attempts'][0]['member_wait_deadline'], 240.)
        w.device.back.assert_not_called()
        self.state = 'room'
        w.matched, w.room_progress, w.join_deadline = False, {}, None
        self.now = 400.
        self.recover()
        self.assertNotIn('runtime_recovery_error', self.report)

    def test_slow_persistence_failure_has_finite_terminal_and_original_error(self):
        self.json.side_effect = OSError('磁盘拒绝')
        with self.assertRaisesRegex(RuntimeError, '120') as raised:
            self.workflow.persist_runtime_report(self.report, self.directory)
        self.assertEqual(type(raised.exception).__name__, 'RuntimeRecoveryExhausted')
        self.assertLessEqual(self.now, 181)
        self.assertEqual(self.report['task_state'], 'runtime_recovery_exhausted')
        self.assertIn('原入房60秒超时', self.report['error'])
        self.assertIn('磁盘拒绝', self.report['persistence_error'])

    def test_search_false_room_template_never_allows_repeated_join(self):
        frame = self.screenshot()
        old_score = self.workflow.score
        self.workflow.score = lambda image, page: 1. if page == 'cooperative_room' else old_score(image, page)
        self.assertTrue(self.workflow.room_search_visible(frame))
        self.assertFalse(self.workflow.room_destination(frame))
        with self.assertRaisesRegex(RuntimeError, '入房前搜索'):
            self.workflow.tap_page('cooperative_room', 920, 431, '匹配资深房间')
        self.workflow.navigator.tap.assert_not_called()

    def test_search_ocr_exception_consumes_common_budget_without_back(self):
        self.workflow.ocr.read.side_effect = ValueError('OCR失败')
        self.assert_terminal('120')
        self.workflow.device.back.assert_not_called()

    def test_twenty_second_search_deadline_has_last_frame_progress_priority(self):
        state = self.workflow.room_search_state(self.report)
        state.update(requests=2, deadline=60.)
        self.state = 'matching'
        self.workflow.pause = lambda seconds: (setattr(self, 'now', self.now + seconds), setattr(self, 'state', 'select'))
        self.recover()
        self.assertTrue(self.report['joined_room'])
        self.workflow.device.back.assert_not_called()

    def test_search_ack_loss_after_real_cancel_still_confirms_two_room_frames(self):
        def back():
            self.state = 'room'
            raise OSError('ACK丢失')
        self.workflow.device.back.side_effect = back
        self.recover()
        self.assertTrue(self.report['room_search_recovery']['search_cancel_confirmed'])
        self.assertEqual(self.report['room_search_recovery']['room_confirmation_frames'], 2)
        self.workflow.device.back.assert_called_once()

    def test_twenty_seconds_of_no_request_does_not_reopen_search_cancel_budget(self):
        self.foreground = False
        self.assert_terminal('20')
        self.assertEqual(self.report['room_search_recovery']['requests'], 0)
        self.workflow.device.back.assert_not_called()

    def test_foreground_loss_during_refresh_or_ocr_prevents_search_back(self):
        w = self.workflow
        for phase in ('screenshot', 'ocr'):
            with self.subTest(phase=phase):
                self.foreground = True
                w.room_search_recovery = None
                count = [0]
                original_read = lambda frame, box: SimpleNamespace(text=self.labels[box], confidence=.99)
                def screenshot():
                    self.foreground = False
                    return self.screenshot()
                def read(frame, box):
                    count[0] += 1
                    if count[0] > 3: self.foreground = False
                    return original_read(frame, box)
                w.device.screenshot.side_effect = screenshot if phase == 'screenshot' else self.screenshot
                w.ocr.read.side_effect = read if phase == 'ocr' else original_read
                self.assertFalse(w.handle_room_search(self.screenshot(), self.report, self.directory))
                w.device.back.assert_not_called()

    def test_report_rebuild_ack_exception_preserves_search_requests_deadline(self):
        self.workflow.device.back.side_effect = OSError('ACK')
        with self.assertRaises(OSError):
            self.workflow.handle_room_search(self.screenshot(), self.report, self.directory)
        state = self.workflow.room_search_recovery
        deadline = state['deadline']
        self.now += 1
        other = dict(self.report, attempts=[{}])
        with self.assertRaises(OSError):
            self.workflow.handle_room_search(self.screenshot(), other, self.directory)
        self.assertIs(other['room_search_recovery'], state)
        self.assertEqual((state['requests'], state['deadline']), (2, deadline))

    def test_candidate_defaults_on_and_is_frozen_with_explicit_rollback(self):
        key = 'MAAPJSK_COOPERATIVE_ROOM_SEARCH_GUARD'
        for value in (None, '0', '1'):
            with self.subTest(value=value), patch.dict(os.environ), \
                    patch('project_sekai.cooperative_live.SoloLive.__init__', return_value=None), \
                    patch('project_sekai.cooperative_live.CooperativeNavigator'):
                if value is None: os.environ.pop(key, None)
                else: os.environ[key] = value
                workflow = CooperativeLive(Mock(), self.directory / 'base.json', self.directory / 'coop.json',
                                           self.directory, self.directory, self.directory)
                self.assertEqual(workflow.room_search_guard, value != '0')
                os.environ[key] = '0' if value != '0' else '1'
                self.assertEqual(workflow.room_search_guard, value != '0')

    def test_actual_matching_uses_full_180_then_bounded_exit_terminal(self):
        self.state = 'matching'
        self.workflow.device.back.side_effect = None
        self.assert_terminal('20')
        self.assertGreaterEqual(self.now, 260)
        self.assertLess(self.now, 261)
        self.assertEqual(self.workflow.device.back.call_count, 3)
        self.assertEqual(self.report['attempts'][0]['member_wait_deadline'], 240.)
        self.assertTrue(self.report['joined_room'])

    def test_running_settlement_login_keeps_its_original_60_second_budget(self):
        w = self.workflow
        w.runtime_recovery_episode = {'join_request': 1, 'stage_rank': -1, 'deadline': 59.}
        w.settlement_login = SimpleNamespace(_deadline=120.)
        w.settlement_login_completed = False
        for now in (60., 90., 119.):
            self.now = now
            w.check_runtime_recovery_deadline(self.report, TimeoutError('原始错误'))
            self.assertNotIn('runtime_recovery_error', self.report)
        self.now = 120.
        with self.assertRaisesRegex(RuntimeError, '120'):
            w.check_runtime_recovery_deadline(self.report, TimeoutError('原始错误'))

    def test_new_task_inherited_search_cancels_before_first_real_join(self):
        w = self.workflow
        self.now = 0.
        w.join_request_count, w.join_deadline, w.entry_notice_disqualified = 0, None, False
        self.report['attempts'] = []
        w.open_rooms()
        inherited = w.room_search_recovery
        self.assertTrue(inherited['inherited_search'])
        self.assertTrue(inherited['observed_on_start'])
        self.assertTrue(inherited['search_cancel_confirmed'])
        self.assertEqual(w.join_request_count, 0)
        self.assertLess(self.now, 2)
        self.assertFalse(self.report.get('joined_room', False))
        w.device.back.assert_called_once()
        w.navigator.tap.side_effect = lambda *_: setattr(self, 'state', 'search')
        with self.assertRaises(TimeoutError):
            w.choose('veteran', 'expert', 'random', self.report, self.directory)
        self.assertEqual(w.join_request_count, 1)
        w.navigator.tap.assert_called_once()
        self.assertIsNot(w.room_search_recovery, inherited)

    def test_observed_search_can_naturally_return_without_back_and_still_clear_deadline(self):
        self.workflow.room_search_state(self.report)['observed_at'] = 50.
        self.state = 'room'
        self.recover()
        self.assertIsNone(self.workflow.join_deadline)
        self.assertTrue(self.report['room_search_recovery']['search_cancel_confirmed'])
        self.assertEqual(self.report['room_search_recovery']['requests'], 0)
        self.workflow.device.back.assert_not_called()

    def test_resource_now_available_does_not_hide_later_preflight_failure_forever(self):
        self.state = 'room'
        self.workflow.join_deadline = None
        self.report['bonus'] = {'availability': 'insufficient', 'consumption': 5}
        self.workflow.read_available_bonus = Mock(return_value=(5, None))
        self.workflow.device.preflight.side_effect = OSError('环境持续错误')
        self.assert_terminal('120')
        self.assertTrue(self.report['runtime_resource_ready'])
        self.assertEqual(self.report['bonus']['availability'], 'insufficient')

    def test_resource_available_then_preflight_failure_then_insufficient_restores_resource_wait(self):
        self.state = 'room'
        w = self.workflow
        w.join_deadline = None
        self.report['bonus'] = {'availability': 'insufficient', 'consumption': 5}
        w.read_available_bonus = Mock(side_effect=lambda *_: (5 if w.device.preflight.call_count == 0 else 4, None))
        w.device.preflight.side_effect = OSError('环境检查失败')
        w.stop_requested = lambda: self.now >= 250.
        with self.assertRaises(InterruptedError):
            self.recover()
        w.device.preflight.assert_called_once()
        self.assertGreaterEqual(self.now, 250.)
        self.assertNotIn('runtime_resource_ready', self.report)
        self.assertNotIn('runtime_recovery_error', self.report)
        self.assertEqual(self.report['runtime_recovery']['state'], 'waiting_resource')
        self.assertIn('suspended_at', w.runtime_recovery_episode)
        self.assertEqual(self.report['error'], 'TimeoutError: 原入房60秒超时')


if __name__ == '__main__':
    unittest.main()
