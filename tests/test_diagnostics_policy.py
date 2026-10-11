import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np

from project_sekai import diagnostics
from project_sekai.calibration import CalibrationProfiles
from project_sekai.chart_catalog import _write_json
from project_sekai.cooperative_live import CooperativeLive
from project_sekai.native_minitouch import NativeMinitouchDevice
from project_sekai.performance_settings import PerformanceSettings
from project_sekai.performance_trace import PerformanceTrace
from project_sekai.song_identity import write_image


class DiagnosticsPolicyTests(unittest.TestCase):
    def tearDown(self):
        diagnostics.configure(False, framework=False)

    def test_legacy_config_defaults_to_logging_and_rejects_non_boolean(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            values = PerformanceSettings().__dict__.copy()
            values.pop("disable_logging")
            _write_json(path, values)
            self.assertFalse(PerformanceSettings.load(path).disable_logging)
            values["disable_logging"] = 1
            _write_json(path, values)
            with self.assertRaises(ValueError):
                PerformanceSettings.load(path)

    def test_agent_server_only_sets_supported_log_dir_and_keeps_fail_closed(self):
        with patch("maa.library.Library.is_agent_server", return_value=True), \
             patch("maa.tasker.Tasker.set_log_dir", return_value=True) as log_dir, \
             patch("maa.tasker.Tasker.set_save_draw", side_effect=AssertionError("AgentServer 不支持此选项")), \
             patch("maa.tasker.Tasker.set_save_on_error", side_effect=AssertionError("AgentServer 不支持此选项")):
            diagnostics.configure(True)
            log_dir.assert_called_once_with("")
        with patch("maa.library.Library.is_agent_server", return_value=True), \
             patch("maa.tasker.Tasker.set_log_dir", return_value=False):
            with self.assertRaises(RuntimeError): diagnostics.configure(True)

    def test_real_agent_registration_startup_has_zero_diagnostics_and_enabled_control(self):
        repo = Path(__file__).resolve().parents[1]
        for disabled in (True, False):
            with self.subTest(disabled=disabled), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "config").mkdir()
                (root / "agent").mkdir()
                _write_json(root / "config/performance-settings.json", PerformanceSettings(disable_logging=disabled).__dict__)
                shutil.copyfile(repo / "agent/server.py", root / "agent/server.py")
                code = "import sys,runpy;sys.path.insert(0,sys.argv[1]);runpy.run_path(sys.argv[2],run_name='diagnostics_fixture')"
                result = subprocess.run([sys.executable, "-c", code, str(repo / "agent"), str(root / "agent/server.py")],
                                        cwd=root, env=dict(os.environ, PYTHONPATH=str(repo)), capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                files = list((root / "debug").rglob("*")) if (root / "debug").exists() else []
                if disabled: self.assertEqual(files, [])
                else: self.assertTrue((root / "debug/maafw.log").is_file())

    def test_disabled_files_do_not_change_native_memory_or_reset_cleanup(self):
        diagnostics.configure(True, framework=False)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device = NativeMinitouchDevice("adb", "fixture", jlog_path=root / "native.jsonl")
            self.assertIsNone(device._start_jlog_writer())
            device._record_log_line("reset receipt remains in memory")
            self.assertEqual(device.logs_since(0)[1], ["reset receipt remains in memory"])
            self.assertTrue(device.stop())
            write_image(root / "frame.png", np.zeros((5, 5, 3), np.uint8))
            diagnostics.write_json(root / "report.json", {"completed": True})
            self.assertEqual(PerformanceTrace().save(root, {})["status"], "disabled")
            self.assertEqual(list(root.iterdir()), [])

    def test_one_shot_success_failure_and_stop_keep_release_and_zero_files(self):
        helper = importlib.import_module("test_one_shot_live").OneShotTests()
        diagnostics.configure(True, framework=False)
        for error in (None, RuntimeError("anchor failed"), InterruptedError("stopped")):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workflow = helper.workflow(root)
                if error:
                    workflow.start.side_effect = error
                    with self.assertRaises(type(error)):
                        workflow.run("master", 0)
                else:
                    report = workflow.run("master", 0)
                    self.assertTrue(report["completed"])
                    self.assertTrue(report["input_completed"])
                self.assertEqual(list(root.rglob("*")), [])

    def test_cooperative_completion_is_in_memory_without_persistence_wait(self):
        diagnostics.configure(True, framework=False)
        workflow = CooperativeLive.__new__(CooperativeLive)
        workflow.save_evidence = Mock(side_effect=AssertionError("must not save"))
        report = {"completed": True, "returned_home": True}
        workflow.persist_runtime_report(report, Path("unused"))
        self.assertTrue(report["completed"])
        self.assertFalse(report["persistence_confirmed"])
        self.assertEqual(report["task_state"], "completed_in_memory")

    def test_full_solo_and_cooperative_run_count_only_success_with_zero_files(self):
        diagnostics.configure(True, framework=False)
        module = importlib.import_module("test_cooperative_live")
        helper = module.CooperativeTests()
        for mode in ("solo", "cooperative"):
            for failed in (False, True):
                with self.subTest(mode=mode, failed=failed), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    workflow = helper.run_workflow(root)
                    if mode == "solo":
                        from project_sekai.solo_live import SoloLive
                        workflow.select = workflow.choose
                        workflow.prepare_task_bonus = Mock()
                        workflow.prepare_playback = Mock()
                        collect = workflow.collect.side_effect
                        workflow.collect.side_effect = lambda report, directory: (collect(report, directory), report.update(completed=True))
                        invoke = lambda: SoloLive.run(workflow, 1, "easy", "current")
                    else:
                        invoke = lambda: workflow.run(1, "easy", "current")
                    if failed:
                        workflow.start.side_effect = InterruptedError("fixture stop")
                    with patch("project_sekai.solo_live.parse_sus", return_value=module.SimpleNamespace(duration=1)), \
                         patch("project_sekai.solo_live.compile_touches", return_value=()), \
                         patch("project_sekai.cooperative_live.parse_sus", return_value=module.SimpleNamespace(duration=1)), \
                         patch("project_sekai.cooperative_live.compile_touches", return_value=()):
                        if failed:
                            with self.assertRaises(InterruptedError): invoke()
                            self.assertEqual(workflow.completed_rounds, 0)
                        else:
                            reports = invoke()
                            self.assertEqual(workflow.completed_rounds, 1)
                            self.assertTrue(reports[0]["completed"])
                    self.assertEqual(list(root.rglob("*")), [])

    def test_native_complete_receipts_and_cleanup_remain_enabled_without_files(self):
        from project_sekai import native_engine
        if not native_engine.available(): self.skipTest("Native 扩展未构建")
        from project_sekai.chart_player import Touch
        from project_sekai.native_player import NativePlayer
        fixture = importlib.import_module("test_native_playback")
        diagnostics.configure(True, framework=False)
        with tempfile.TemporaryDirectory() as tmp:
            clock = fixture.Clock()
            device = fixture.ScriptDevice(clock)
            player = NativePlayer(None, (Touch(.6, 2, 0, "down", 190), Touch(.8, 0, 0, "up")),
                                  Path(tmp), lambda: False, device=device, clock=clock, sleeper=clock.sleep)
            player.prepare()
            report = player.play(1000, 0)
            player.close()
            self.assertEqual(report["executed_actions"], 2)
            self.assertTrue(report["release_confirmed"])
            self.assertEqual(report["release"]["release_proof"], "current-reset-jlog-and-cleanup")
            self.assertFalse(device.active)
            self.assertFalse(device.connected)
            self.assertEqual(list(Path(tmp).rglob("*")), [])

    def test_profile_and_business_consumption_state_still_persist(self):
        diagnostics.configure(True, framework=False)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = {"environment": {"fixture": "device"}, "difficulty": "master", "accepted": True,
                       "validation_report": {"completed": True, "live_status": "cleared",
                           "judgements": {"total_matches_chart": True, "fast": 0, "late": 0},
                           "playback": {"release_confirmed": True, "planned_actions": 1,
                                        "sent_actions": 1, "executed_actions": 1}}}
            path = CalibrationProfiles(root / "profiles").save(profile)
            self.assertTrue(path.is_file())
            navigator = importlib.import_module("project_sekai.navigator").Navigator.__new__(
                importlib.import_module("project_sekai.navigator").Navigator)
            navigator.config_path = root / "templates.json"
            navigator.recovery_evidence = {"status": "credited", "count": 2}
            navigator._persist_recovery_evidence()
            self.assertEqual(json.loads((root / "recovery-last.json").read_text())["count"], 2)

    def test_logging_enabled_keeps_write_failure_gate(self):
        diagnostics.configure(False, framework=False)
        with patch("project_sekai.chart_catalog._write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                diagnostics.write_json(Path("report.json"), {})
