from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np

from project_sekai.cooperative_live import CooperativeLive


class AnchorEvidencePersistenceTests(unittest.TestCase):
    def workflow(self):
        workflow = CooperativeLive.__new__(CooperativeLive)
        for name in ('save_opening_evidence', 'save_preplay_life_evidence',
                     'save_recovery_evidence', 'finish_performance_trace'):
            setattr(workflow, name, Mock())
        workflow.life_frames = []
        workflow.anchor_frames = [(12.5, np.zeros((8, 8, 3), np.uint8))]
        return workflow

    def report(self):
        return {'playback': {'release_confirmed': True}, 'error': '原开场失败'}

    def test_cooperative_repeated_save_preserves_frame_index_and_timestamps(self):
        workflow, report = self.workflow(), self.report()
        with tempfile.TemporaryDirectory() as directory, patch('project_sekai.solo_live.enabled', return_value=True):
            workflow.save_evidence(report, Path(directory))
            saved = report['anchor_frames']
            workflow.save_evidence(report, Path(directory))
            self.assertIs(report['anchor_frames'], saved)
            self.assertEqual(saved, [{'captured_at': 12.5, 'path': 'anchor-frame-00.png'}])
            self.assertTrue((Path(directory) / saved[0]['path']).is_file())
            fresh_report = self.report()
            workflow.save_evidence(fresh_report, Path(directory))
            self.assertNotIn('anchor_frames', fresh_report)

    def test_failed_write_preserves_existing_index_and_original_error(self):
        workflow, report = self.workflow(), self.report()
        saved = report['anchor_frames'] = [{'captured_at': 1.5, 'path': 'previous.png'}]
        workflow.anchor_frames *= 2
        with patch('project_sekai.solo_live.enabled', return_value=True), patch(
                'project_sekai.solo_live.write_image', side_effect=[None, OSError('磁盘失败')]):
            workflow.save_evidence(report, Path('unused'))
        self.assertIs(report['anchor_frames'], saved)
        self.assertEqual(report['error'], '原开场失败')
        self.assertIn('磁盘失败', report['anchor_evidence_warning'])

    def test_disabled_logging_discards_memory_without_writing_or_changing_index(self):
        workflow, report = self.workflow(), self.report()
        saved = report['anchor_frames'] = [{'captured_at': 1.5, 'path': 'previous.png'}]
        with patch('project_sekai.solo_live.enabled', return_value=False), patch(
                'project_sekai.solo_live.write_image') as write:
            workflow.save_evidence(report, Path('unused'))
        write.assert_not_called()
        self.assertEqual(workflow.anchor_frames, [])
        self.assertIs(report['anchor_frames'], saved)

    def test_missing_release_keeps_memory_and_existing_index(self):
        workflow, report = self.workflow(), self.report()
        report['playback']['release_confirmed'] = False
        saved = report['anchor_frames'] = [{'captured_at': 1.5, 'path': 'previous.png'}]
        with patch('project_sekai.solo_live.enabled', return_value=True), patch(
                'project_sekai.solo_live.write_image') as write:
            workflow.save_evidence(report, Path('unused'))
        write.assert_not_called()
        self.assertEqual(len(workflow.anchor_frames), 1)
        self.assertIs(report['anchor_frames'], saved)

    def test_new_batch_does_not_overwrite_files_referenced_by_existing_index(self):
        workflow, report = self.workflow(), self.report()
        report['anchor_frames'] = [{'captured_at': 1.5, 'path': 'anchor-frame-00.png'},
                                   {'captured_at': 2.5, 'path': 'anchor-frame-02.png'}]
        workflow.anchor_frames *= 2
        with patch('project_sekai.solo_live.enabled', return_value=True), patch(
                'project_sekai.solo_live.write_image') as write:
            workflow.save_evidence(report, Path('unused'))
        self.assertEqual([call.args[0].name for call in write.call_args_list],
                         ['anchor-frame-01.png', 'anchor-frame-03.png'])
        self.assertEqual([item['path'] for item in report['anchor_frames']],
                         ['anchor-frame-01.png', 'anchor-frame-03.png'])

    def test_relocated_index_keeps_original_png_bytes_when_next_batch_fails(self):
        workflow, report = self.workflow(), self.report()
        with tempfile.TemporaryDirectory() as directory:
            attempt = Path(directory) / 'attempt-1'
            attempt.mkdir()
            original = attempt / 'anchor-frame-00.png'
            original.write_bytes(b'original-released-evidence')
            saved = report['anchor_frames'] = [
                {'captured_at': 1.5, 'path': 'attempt-1/anchor-frame-00.png'}]
            workflow.anchor_frames *= 2

            def write(path, frame):
                if path.name == 'anchor-frame-02.png':
                    raise OSError('第二张写入失败')
                path.write_bytes(b'new-evidence')

            with patch('project_sekai.solo_live.enabled', return_value=True), patch(
                    'project_sekai.solo_live.write_image', side_effect=write):
                workflow.save_evidence(report, attempt)
            self.assertEqual(original.read_bytes(), b'original-released-evidence')
            self.assertIs(report['anchor_frames'], saved)
            self.assertEqual(saved[0]['captured_at'], 1.5)
            self.assertEqual(report['error'], '原开场失败')
            self.assertIn('第二张写入失败', report['anchor_evidence_warning'])


if __name__ == '__main__':
    unittest.main()
