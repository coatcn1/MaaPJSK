import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from project_sekai.game_timing import GameTimingGuard
from project_sekai.performance_trace import PerformanceTrace
from tests import test_cooperative_live as cooperative_test_helpers


class GameTimingIntegrationTests(unittest.TestCase):
    def workflow(self):
        workflow, _, _ = cooperative_test_helpers.CooperativeTests().observed_workflow(["playing"])
        workflow.game_timing_guard = GameTimingGuard()
        workflow.game_timing_templates = Mock()
        workflow.last_report["game_timing_feedback"] = workflow.game_timing_guard.report
        workflow.begin_performance_trace()
        return workflow

    def test_same_protection_frames_drive_feedback_without_extra_capture_or_hot_path_write(self):
        workflow = self.workflow()
        workflow.game_timing_templates.observe.side_effect = [
            {"direction": "fast", "judgement": "great", "combo_signature": str(index), "reason": "confirmed"}
            for index in range(3)]
        with tempfile.TemporaryDirectory() as directory, patch(
                "project_sekai.cooperative_live.time.perf_counter", side_effect=[0, 0, .001, 2, 2, 2.001, 4, 4, 4.001]), \
                patch.object(workflow, "screenshot", wraps=workflow.screenshot) as capture, \
                patch("project_sekai.performance_trace.json.dumps") as serialise:
            for _ in range(3):
                workflow.observe_play_state(Path(directory))
            self.assertEqual(capture.call_count, 3)
            serialise.assert_not_called()
        self.assertEqual(workflow.game_timing_guard.correction_ms, 5)
        observations = workflow.performance_trace.samples
        self.assertEqual(len(observations), 3)
        self.assertEqual(observations[-1]["game_timing"]["requested_correction_ms"], 5)
        self.assertEqual(len(workflow.game_timing_guard.report["adjustments"][0]["evidence"]), 3)

    def test_recognition_failure_freezes_phase_and_keeps_protection_running(self):
        workflow = self.workflow()
        workflow.game_timing_guard.correction_ms = 5
        workflow.game_timing_templates.observe.side_effect = ValueError("模板损坏")
        with tempfile.TemporaryDirectory() as directory, patch(
                "project_sekai.cooperative_live.time.perf_counter", side_effect=[0, 0, .001, 2]):
            workflow.observe_play_state(Path(directory))
            workflow.observe_play_state(Path(directory))
        self.assertEqual(workflow.game_timing_guard.correction_ms, 5)
        self.assertEqual(workflow.game_timing_guard.report["status"], "recognition_failed")
        self.assertEqual(workflow.life_guard.samples, 2)
        self.assertEqual(workflow.game_timing_templates.observe.call_count, 1)

    def test_unconfirmed_hud_clears_old_feedback_and_never_classifies_frame(self):
        workflow = self.workflow()
        guard = workflow.game_timing_guard
        guard.observe({"direction": "fast", "judgement": "great", "combo_signature": "old", "reason": "confirmed"}, 0)
        workflow.observe_game_timing_feedback(None, 2, {"playfield_visible": False})
        self.assertEqual(guard.streak, 0)
        workflow.game_timing_templates.observe.assert_not_called()

    def test_per_attempt_guard_is_new_and_disabled_or_missing_templates_do_not_stop(self):
        workflow = self.workflow()
        first_report, second_report = {}, {}
        first = workflow.game_timing_guard
        first.correction_ms = 25
        workflow.prepare_game_timing_feedback(first_report, True, "native")
        self.assertIsNot(first, workflow.game_timing_guard)
        self.assertEqual(workflow.game_timing_guard.correction_ms, 0)
        workflow.prepare_game_timing_feedback(second_report, True, "legacy")
        self.assertIsNone(workflow.game_timing_guard)
        self.assertEqual(second_report["game_timing_feedback"]["status"], "unsupported_engine")
        workflow.game_timing_templates = None
        workflow.game_timing_config = Path("missing-game-timing-templates.json")
        workflow.prepare_game_timing_feedback({}, False, "native")
        self.assertIsNone(workflow.game_timing_guard)
        report = {}
        workflow.prepare_game_timing_feedback(report, True, "native")
        self.assertIsNone(workflow.game_timing_guard)
        self.assertEqual(report["game_timing_feedback"]["status"], "templates_unavailable")

    def test_trace_keeps_requested_phase_separate_from_published_phase_and_receipt_path(self):
        workflow = self.workflow()
        workflow.last_report["playback"] = {"game_phase_correction_ms": 5}
        guard = workflow.game_timing_guard
        guard.observe({"direction": "fast", "judgement": "great", "combo_signature": "new", "reason": "confirmed"}, 0)
        guard.report["correction_ms"] = 10
        trace = PerformanceTrace()
        trace.observe(0, workflow.last_report, workflow.life_guard, life_sampled=False)
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            (directory / "native-touch-receipts.jsonl").write_text('{"index":0}\n', encoding="utf-8")
            trace.save(directory, workflow.last_report)
            rows = [json.loads(line) for line in (directory / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rows[0]["native_touch_receipts"], "native-touch-receipts.jsonl")
        self.assertEqual(rows[1]["game_timing"]["requested_correction_ms"], 10)
        self.assertEqual(rows[1]["game_timing"]["published_correction_ms"], 5)
        self.assertEqual(rows[-1]["game_timing_feedback"]["latest_observation"]["combo_signature"], "new")

    def test_native_callback_reads_current_game_phase_and_new_round_starts_at_zero(self):
        helpers = cooperative_test_helpers.CooperativeTests()
        callbacks, initial_phases = [], []
        with tempfile.TemporaryDirectory() as directory:
            workflow = helpers.run_workflow(Path(directory))
            workflow.game_timing_templates = Mock()

            def native(*args, **options):
                callback = options["game_timing_correction"]
                callbacks.append(callback)
                initial_phases.append(callback())
                player = Mock()
                player.report = {"planned_actions": 2, "sent_actions": 0, "executed_actions": 0,
                                 "release_confirmed": False}

                def play(epoch, offset):
                    self.assertEqual(offset, 0)
                    workflow.game_timing_guard.correction_ms = 5
                    self.assertEqual(callback(), 5)
                    player.report.update(sent_actions=2, executed_actions=2)

                def close():
                    player.report.update(release_confirmed=True, release={
                        "reset_executed": True, "release_proof": "current-reset-jlog-and-cleanup"})

                player.play.side_effect = play
                player.close.side_effect = close
                return player

            with patch("project_sekai.native_player.NativePlayer", side_effect=native):
                reports = helpers.execute(workflow, count=2, engine="native", game_timing_feedback=True)
        self.assertEqual(initial_phases, [0, 0])
        self.assertEqual(len(callbacks), 2)
        self.assertTrue(all(report["game_timing_feedback"]["effective"] for report in reports))
        workflow.game_timing_guard.correction_ms = 20
        self.assertEqual(callbacks[0](), 5)
        self.assertEqual(callbacks[1](), 20)
        self.assertEqual(workflow.completed_rounds, 2)
        workflow.device.home.assert_not_called()
