from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from project_sekai.navigator import Navigator
from project_sekai.ocr import Reading
from project_sekai.solo_live import LiveResult, SoloLive
from project_sekai.title_screen import TitleScreenDetected


class SettlementLoginTests(unittest.TestCase):
    def workflow(self, root, *, initial_title=False, destination='home'):
        clock = SimpleNamespace(now=0.)
        state = SimpleNamespace(captures=0, logged_in=False)

        def frame(page):
            result = np.full((720, 1280, 3), 150, np.uint8)
            cv2.rectangle(result, (10, 10), (180, 35), (0, 0, 0), -1)
            result[0, 0] = {'rewards': 1, 'title': 2, 'home': 3, 'playing': 4}[page]
            if page == 'title':
                for x1, y1, x2, y2 in ((1049, 31, 1053, 49), (1055, 25, 1063, 56), (1070, 20, 1082, 62)):
                    result[y1:y2, x1:x2] = (226, 176, 8)
                result[26:51, 1116:1144] = (170, 95, 34)
            return result

        def capture():
            state.captures += 1
            page = destination if state.logged_in else 'title' if initial_title or state.captures >= 3 else 'rewards'
            return frame(page)

        def match(image, name, *args):
            page = int(image[0, 0, 0])
            # 标题模板始终不匹配，复查固定品牌标识能单独完成整条重登路径。
            return (float((page == 3 and name == 'home') or (page == 4 and name in {'playing', 'life_hud'})), (0, 0))

        workflow = SoloLive.__new__(SoloLive)
        workflow.device = Mock()
        workflow.device.screenshot.side_effect = capture
        workflow.device.shell.side_effect = lambda command: ('Physical density: 240' if command == 'wm density'
                                                              else 'mCurrentFocus=com.sega.pjsekai/.Main')
        workflow.device.tap.side_effect = lambda x, y: setattr(state, 'logged_in', True)
        workflow.device.back.side_effect = AssertionError('标题页不得继续 BACK')
        workflow.navigator = SimpleNamespace(threshold=.83, templates={
            name: None for name in ('home', 'playing', 'life_hud', 'title_screen')}, match=match,
            _check_stop=lambda: None, _save_failure=Mock())
        workflow.navigator._check_title_screen = lambda image: Navigator._check_title_screen(workflow.navigator, image)
        workflow.ocr = Mock()
        workflow.ocr.read.return_value = Reading('', .99)
        workflow.read_result = Mock(return_value=None)
        workflow.stop_requested = lambda: False
        workflow.log = Mock()
        workflow.pause = lambda seconds: setattr(clock, 'now', clock.now + seconds)
        workflow.life_guard = SimpleNamespace(hud_seen=True)
        workflow.report_root = root / 'solo'
        return workflow, clock

    @staticmethod
    def report():
        return {'engine': 'native', 'phase': 'settlement', 'chart': {'total_note_count': 1},
                'playback': {'planned_actions': 4, 'sent_actions': 4, 'executed_actions': 4,
                             'release_confirmed': True, 'release': {'reset_executed': True,
                                 'release_proof': 'current-reset-jlog-and-cleanup'}}}

    def test_real_collection_recovers_branded_title_after_end_and_skips_unreadable_score(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workflow, clock = self.workflow(root)
            report = self.report()
            with patch('time.monotonic', side_effect=lambda: clock.now), \
                    patch('time.sleep', side_effect=workflow.pause):
                workflow.collect(report, root)
            self.assertTrue(report['returned_home'])
            self.assertTrue(report['performance_completed'])
            self.assertEqual(report['result_status'], 'unreadable')
            self.assertFalse(report['completed'])
            self.assertEqual(report['settlement_login']['status'], 'home_confirmed')
            workflow.device.tap.assert_called_once_with(640, 620)
            workflow.device.back.assert_not_called()

    def test_title_before_end_or_with_incomplete_input_never_logs_in(self):
        for defect in ('end_missing', 'input_missing', 'reset_missing', 'playback_error', 'death'):
            with self.subTest(defect=defect), TemporaryDirectory() as directory:
                root = Path(directory)
                workflow, clock = self.workflow(root, initial_title=True)
                report = self.report()
                if defect != 'end_missing':
                    report['end_detection'] = {'confirmation_frames': 2, 'input_and_release_complete': True}
                if defect == 'input_missing':
                    report['playback']['executed_actions'] = 3
                elif defect == 'reset_missing':
                    report['playback']['release']['reset_executed'] = False
                elif defect == 'playback_error':
                    report['playback_error'] = 'original input failure'
                elif defect == 'death':
                    report['death_confirmed'] = True
                with patch('time.monotonic', side_effect=lambda: clock.now), self.assertRaisesRegex(RuntimeError, '标题'):
                    workflow.collect(report, root)
                workflow.device.tap.assert_not_called()
                workflow.device.back.assert_not_called()
                self.assertFalse(report.get('performance_completed', False))

    def test_login_that_reaches_playing_never_claims_home_or_adds_back(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workflow, clock = self.workflow(root, destination='playing')
            report = self.report()
            with patch('time.monotonic', side_effect=lambda: clock.now), \
                    patch('time.sleep', side_effect=workflow.pause), self.assertRaises(TimeoutError):
                workflow.collect(report, root)
            self.assertFalse(report.get('performance_completed', False))
            workflow.device.back.assert_not_called()
            self.assertLessEqual(clock.now, 62.)

    def test_collected_judgements_survive_title_login_without_replaying(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workflow, clock = self.workflow(root)
            workflow.read_result = Mock(return_value=LiveResult(1, 0, 0, 0, 0))
            report = self.report()
            with patch('time.monotonic', side_effect=lambda: clock.now), \
                    patch('time.sleep', side_effect=workflow.pause):
                workflow.collect(report, root)
            self.assertTrue(report['completed'])
            self.assertEqual(report['judgements']['perfect'], 1)
            workflow.device.tap.assert_called_once_with(640, 620)
            workflow.device.back.assert_not_called()

    def test_title_login_cannot_extend_the_original_settlement_deadline(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workflow, clock = self.workflow(root, initial_title=True, destination='title')
            report = self.report()
            report['end_detection'] = {'confirmation_frames': 2, 'input_and_release_complete': True}
            clock.now = 119.
            error = TitleScreenDetected(workflow.device.screenshot())
            with patch('time.monotonic', side_effect=lambda: clock.now), \
                    patch('time.sleep', side_effect=workflow.pause), self.assertRaises(TimeoutError):
                workflow.recover_settlement_title(report, root, error, 120.)
            self.assertLessEqual(clock.now, 120.25)
            self.assertEqual(workflow.settlement_login.report['title_requests'], 1)
            workflow.device.back.assert_not_called()


if __name__ == '__main__':
    unittest.main()
