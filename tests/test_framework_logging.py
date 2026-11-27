from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from project_sekai import diagnostics
from project_sekai.performance_settings import PerformanceSettings


class FrameworkLoggingTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        os.environ.pop(diagnostics.FRAMEWORK_LOGGING_ENV, None)
        self.state = patch.object(diagnostics, '_framework_configured', False)
        self.state.start()
        self.addCleanup(self.state.stop)

    def tearDown(self):
        diagnostics.configure(False, framework=False)

    def test_old_settings_default_framework_off_without_rewriting_user_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'settings.json'
            values = asdict(PerformanceSettings(engine='native', touch_offset_ms=-51, bonus_consumption=5))
            values.pop('framework_logging')
            values['future_option'] = {'keep': True}
            path.write_text(json.dumps(values), encoding='utf-8')
            before = path.read_bytes()
            settings = PerformanceSettings.load(path)
            self.assertFalse(settings.framework_logging)
            self.assertFalse(settings.disable_logging)
            self.assertEqual((settings.touch_offset_ms, settings.bonus_consumption), (-51, 5))
            self.assertEqual(path.read_bytes(), before)

    def test_framework_setting_requires_a_real_boolean(self):
        for value in [0, 1, 'true', None, [], {}]:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'MaaFramework'):
                PerformanceSettings(framework_logging=value).validate()

    def test_gui_snapshot_wins_over_later_saved_values_and_invalid_snapshot_disables(self):
        for raw, saved, expected in [('0', True, ''), ('1', False, './debug'), ('true', True, '')]:
            with self.subTest(raw=raw), patch.dict(os.environ, {diagnostics.FRAMEWORK_LOGGING_ENV: raw}), \
                    patch.object(diagnostics, '_framework_configured', False), \
                    patch('maa.library.Library.is_agent_server', return_value=True), \
                    patch('maa.tasker.Tasker.set_log_dir', return_value=True) as log:
                diagnostics.configure(False, framework_logging=saved)
                log.assert_called_once_with(expected)
                self.assertTrue(diagnostics.enabled())

    def test_total_disable_wins_for_a_fresh_independent_start(self):
        with patch('maa.library.Library.is_agent_server', return_value=False), \
                patch('maa.tasker.Tasker.set_log_dir', return_value=True) as log, \
                patch('maa.tasker.Tasker.set_save_draw', return_value=True) as draw, \
                patch('maa.tasker.Tasker.set_save_on_error', return_value=True) as errors:
            diagnostics.configure(True, framework_logging=True)
            log.assert_called_once_with('')
            draw.assert_called_once_with(False)
            errors.assert_called_once_with(False)

    def test_agent_never_hot_switches_log_stream_when_policy_is_reloaded(self):
        with patch('maa.library.Library.is_agent_server', return_value=True), \
                patch('maa.tasker.Tasker.set_log_dir', return_value=True) as log:
            diagnostics.configure(False, framework_logging=False)
            diagnostics.configure(True, framework_logging=True)
            diagnostics.configure(False, framework_logging=True)
            log.assert_called_once_with('')
            self.assertFalse(diagnostics.framework_logging_enabled())
            self.assertTrue(diagnostics.enabled())

    def test_independent_cli_can_ignore_an_inherited_snapshot(self):
        with patch.dict(os.environ, {diagnostics.FRAMEWORK_LOGGING_ENV: '1'}), \
                patch('maa.library.Library.is_agent_server', return_value=True), \
                patch('maa.tasker.Tasker.set_log_dir', return_value=True) as log:
            diagnostics.configure(False, framework_logging=False, inherit_framework_snapshot=False)
            log.assert_called_once_with('')

    def test_log_initialization_failure_does_not_disable_project_evidence_or_retry_init(self):
        with patch('maa.tasker.Tasker.set_log_dir', side_effect=OSError('log fixture')), \
                patch.object(diagnostics, '_warn') as warning:
            diagnostics.configure(False)
            diagnostics.configure(False)
            warning.assert_called_once()
            self.assertTrue(diagnostics.enabled())
