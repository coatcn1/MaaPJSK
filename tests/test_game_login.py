from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import numpy as np

from project_sekai.game_login import GameLogin, title_menu_ready
from project_sekai.ocr import Reading


class LoginDevice:
    def __init__(self, state=1, *, foreground=True, destination=2, menu_ready=True):
        self.state, self.is_foreground = state, foreground
        self.destination, self.menu_ready = destination, menu_ready
        self.preflight, self.events = Mock(), []
        self.shell = Mock(side_effect=self._shell)

    def _shell(self, command):
        if command == 'wm density':
            return 'Physical density: 240'
        if command == 'dumpsys window':
            return 'mCurrentFocus=' + ('com.sega.pjsekai/.Main' if self.is_foreground else 'com.other.app/.Main')
        if command.startswith('monkey '):
            self.events.append(('launch', command))
            self.is_foreground, self.state = True, 1
            return 'Events injected: 1'
        raise AssertionError('登录服务不得关闭其他应用')

    def screenshot(self):
        frame = np.full((720, 1280, 3), 255, np.uint8)
        frame[0, 0] = self.state
        if self.menu_ready:
            for y in (32, 43, 54):
                frame[y:y + 3, 1226:1248] = 70
        return frame

    def tap(self, x, y):
        self.events.append(('tap', (x, y)))
        self.state = self.destination

    def back(self):
        self.events.append(('back', ()))
        self.state = 2


class GameLoginTests(unittest.TestCase):
    def workflow(self, root, **options):
        device = LoginDevice(**options)
        clock = SimpleNamespace(now=0.)
        navigator = SimpleNamespace(templates={name: None for name in ('title_screen', 'home', 'playing', 'life_hud')}, threshold=.83)
        def match(frame, name):
            state = int(frame[0, 0, 0])
            return (float((state == 1 and name == 'title_screen') or (state == 2 and name == 'home')
                          or (state == 3 and name in {'playing', 'life_hud'})), (0, 0))
        navigator.match = match
        ocr = Mock()
        titles = {(174, 29, 299, 59): 'お知らせ', (491, 33, 550, 60): '一覧',
                  (729, 33, 791, 60): '不具合', (930, 34, 1054, 61): '関連サイト'}
        ocr.read.side_effect = lambda frame, box: Reading(titles[box] if frame[0, 0, 0] == 4 else '', .99)
        workflow = GameLogin(device, navigator, root, clock=lambda: clock.now,
                             sleeper=lambda seconds: setattr(clock, 'now', clock.now + seconds), ocr=ocr)
        return workflow, device, clock

    def test_title_and_ready_menu_login_once_then_confirm_home(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, device, _ = self.workflow(Path(directory))
            report = workflow.run()
        self.assertTrue(report['completed'])
        self.assertEqual(report['title_requests'], 1)
        self.assertEqual(device.events, [('tap', (640, 620))])

    def test_existing_game_pages_including_playing_are_handed_back_without_login_input(self):
        for state in (0, 2, 3, 4):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                workflow, device, _ = self.workflow(Path(directory), state=state)
                report = workflow.run()
                self.assertEqual(report['phase'], 'already_running')
                self.assertEqual(device.events, [])

    def test_other_foreground_app_only_launches_game_and_is_never_force_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, device, _ = self.workflow(Path(directory), state=0, foreground=False)
            report = workflow.run()
        self.assertEqual(report['launch_requests'], 1)
        self.assertEqual(report['title_requests'], 1)
        self.assertTrue(all('force-stop' not in call.args[0] for call in device.shell.call_args_list))

    def test_unready_title_or_managed_blank_times_out_without_any_click(self):
        for state, menu_ready in ((1, False), (0, True)):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                workflow, device, clock = self.workflow(Path(directory), state=state, menu_ready=menu_ready)
                with self.assertRaisesRegex(TimeoutError, '60 秒'):
                    workflow.run(wait_for_home=True)
                self.assertEqual(device.events, [])
                self.assertGreaterEqual(clock.now, 60.)
                self.assertFalse(workflow.report['completed'])

    def test_stalled_title_has_three_request_budget_and_waits_without_blind_input(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, device, _ = self.workflow(Path(directory), destination=1)
            with self.assertRaises(TimeoutError):
                workflow.run()
        self.assertEqual(workflow.report['title_requests'], 3)
        self.assertEqual(len(device.events), 3)

    def test_user_stop_interrupts_loading_promptly(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, device, clock = self.workflow(Path(directory), state=0)
            workflow.stop_requested = lambda: clock.now >= 2.
            with self.assertRaises(InterruptedError):
                workflow.run(wait_for_home=True)
        self.assertLessEqual(clock.now, 2.1)
        self.assertEqual(device.events, [])
        self.assertTrue(workflow.report['cancelled'])

    def test_failed_title_ack_is_counted_and_never_claims_login_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, device, _ = self.workflow(Path(directory))
            device.tap = Mock(side_effect=OSError('登录点击回执失败'))
            with self.assertRaises(OSError):
                workflow.run()
        self.assertEqual(workflow.report['title_requests'], 1)
        self.assertFalse(workflow.report['completed'])

    def test_known_notice_is_closed_only_in_managed_login_context(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, device, _ = self.workflow(Path(directory), destination=4)
            report = workflow.run()
        self.assertTrue(report['completed'])
        self.assertEqual(report['notice_requests'], 1)
        self.assertEqual(device.events, [('tap', (640, 620)), ('back', ())])

    def test_notice_with_wrong_tab_or_playing_hud_does_not_get_back(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, device, _ = self.workflow(Path(directory), state=4)
            workflow.ocr.read.return_value = Reading('', .99)
            workflow.ocr.read.side_effect = lambda frame, box: Reading('購入', .99)
            with self.assertRaises(TimeoutError):
                workflow.run(wait_for_home=True)
            self.assertEqual(device.events, [])

    def test_menu_shape_alone_never_means_title_or_clicks_menu(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, device, _ = self.workflow(Path(directory), state=0)
            self.assertTrue(title_menu_ready(device.screenshot()))
            report = workflow.run()
        self.assertEqual(report['phase'], 'already_running')
        self.assertEqual(device.events, [])

    def test_resumed_service_keeps_title_request_budget_and_original_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, device, clock = self.workflow(Path(directory))
            original_tap = device.tap
            device.tap = Mock(side_effect=OSError('登录回执连接失败'))
            with self.assertRaises(OSError):
                workflow.run(resume=True)
            first_report = workflow.report
            self.assertEqual(first_report['title_requests'], 1)
            device.tap = original_tap
            clock.now = 6.
            report = workflow.run(resume=True)
            self.assertIs(report, first_report)
            self.assertEqual(report['title_requests'], 2)
            self.assertEqual(len(list(Path(directory).glob('*/report.json'))), 1)

        with tempfile.TemporaryDirectory() as directory:
            workflow, device, clock = self.workflow(Path(directory))
            device.tap = Mock(side_effect=OSError('登录点击失败'))
            with self.assertRaises(OSError):
                workflow.run(resume=True)
            clock.now = 61.
            with self.assertRaises(TimeoutError):
                workflow.run(resume=True)
            self.assertEqual(device.tap.call_count, 1)

    def test_expired_login_can_read_only_accept_user_home_without_new_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, device, clock = self.workflow(Path(directory), state=0)
            with self.assertRaises(TimeoutError):
                workflow.run(wait_for_home=True, resume=True)
            self.assertGreaterEqual(clock.now, 60.)
            device.state = 2
            report = workflow.run(resume=True)
            self.assertEqual(report['phase'], 'home_confirmed_after_deadline')
            self.assertEqual(device.events, [])
            self.assertEqual(report['title_requests'], 0)


if __name__ == '__main__':
    unittest.main()
