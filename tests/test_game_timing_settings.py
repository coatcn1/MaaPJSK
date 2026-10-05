from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest

from project_sekai.performance_settings import PerformanceSettings, migrate_settings


class GameTimingSettingsTests(unittest.TestCase):
    def test_new_settings_and_missing_config_keep_feedback_off(self):
        self.assertIs(PerformanceSettings().cooperative_game_timing_feedback, False)
        with tempfile.TemporaryDirectory() as directory:
            self.assertIs(PerformanceSettings.load(Path(directory) / "missing.json").cooperative_game_timing_feedback, False)

    def test_old_schema_one_config_loads_without_changing_user_values_or_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config/performance-settings.json"
            path.parent.mkdir()
            values = asdict(PerformanceSettings(engine="native", touch_offset_ms=-51, bonus_consumption=5,
                                               use_calibration_profile=False))
            del values["cooperative_game_timing_feedback"]
            path.write_text(json.dumps(values), encoding="utf-8")
            before = path.read_bytes()
            settings = PerformanceSettings.load(path)
            self.assertIs(settings.cooperative_game_timing_feedback, False)
            self.assertEqual((settings.engine, settings.touch_offset_ms, settings.bonus_consumption,
                              settings.use_calibration_profile, settings.schema_version), ("native", -51, 5, False, 1))
            migrate_settings(root)
            self.assertEqual(path.read_bytes(), before)

    def test_explicit_boolean_switch_round_trips_without_changing_manual_offset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            for enabled in (False, True):
                with self.subTest(enabled=enabled):
                    settings = PerformanceSettings(engine="native", touch_offset_ms=-51,
                                                   cooperative_game_timing_feedback=enabled)
                    path.write_text(json.dumps(asdict(settings)), encoding="utf-8")
                    loaded = PerformanceSettings.load(path)
                    self.assertEqual(loaded, settings)
                    self.assertIs(loaded.cooperative_game_timing_feedback, enabled)

    def test_feedback_switch_rejects_non_boolean_json_types(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            for value in (0, 1, "true", "false", None, [], {}):
                with self.subTest(value=value):
                    values = asdict(PerformanceSettings())
                    values["cooperative_game_timing_feedback"] = value
                    path.write_text(json.dumps(values), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "开关必须为布尔值"):
                        PerformanceSettings.load(path)

    def test_legacy_engine_preserves_switch_for_a_future_native_task(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps(asdict(PerformanceSettings(cooperative_game_timing_feedback=True))), encoding="utf-8")
            loaded = PerformanceSettings.load(path)
            self.assertEqual(loaded.engine, "legacy")
            self.assertIs(loaded.cooperative_game_timing_feedback, True)


if __name__ == "__main__":
    unittest.main()
