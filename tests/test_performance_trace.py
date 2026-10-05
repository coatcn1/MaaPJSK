import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from project_sekai.chart_player import Touch
from project_sekai.life_monitor import LifeDepleted, LifeGuard
from project_sekai import native_engine
from project_sekai.native_player import NativePlayer
from project_sekai.solo_live import SoloLive
from project_sekai.one_shot_live import OneShotLive
from project_sekai.performance_trace import PerformanceTrace
import test_cooperative_live
import test_native_playback


class PerformanceTraceTests(unittest.TestCase):
    def workflow(self, states):
        workflow, _, _ = test_cooperative_live.CooperativeTests().observed_workflow(states)
        workflow.begin_performance_trace()
        workflow.set_performance_trace_plan((Touch(.1, 2, 0, "down", 500), Touch(600, 0, 0, "up")))
        return workflow

    def test_whole_song_trace_keeps_samples_after_eight_minutes_without_hot_path_io(self):
        workflow = self.workflow(["playing"])
        with tempfile.TemporaryDirectory() as directory, patch(
                "project_sekai.cooperative_live.time.perf_counter", side_effect=range(0, 604, 2)), patch(
                "project_sekai.cooperative_live.write_image"), patch(
                "project_sekai.performance_trace.json.dumps") as serialise, patch.object(
                workflow, "screenshot", wraps=workflow.screenshot) as capture:
            for _ in range(302):
                workflow.observe_play_state(Path(directory))
            serialise.assert_not_called()
            self.assertFalse((Path(directory) / "trace.jsonl").exists())
            self.assertEqual(capture.call_count, 302)
        with tempfile.TemporaryDirectory() as directory:
            workflow.finish_performance_trace(workflow.last_report, Path(directory))
            rows = [json.loads(line) for line in (Path(directory) / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
            samples = [row for row in rows if row["kind"] == "observation"]
            self.assertEqual(len(samples), 302)
            self.assertEqual(samples[-1]["elapsed_s"], 602)
            self.assertEqual(rows[-1]["kind"], "outcome")
            plan = [json.loads(line) for line in (Path(directory) / "touch-plan.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual((plan[0]["kind"], plan[-1]["time"]), ("down", 600))

    def test_zero_confirmations_and_stop_reason_survive_the_protection_exception(self):
        workflow = self.workflow(["playing", "playing"])
        workflow.read_optional_number.return_value = 0
        with tempfile.TemporaryDirectory() as directory, patch(
                "project_sekai.cooperative_live.time.perf_counter", side_effect=[100, 102]), patch(
                "project_sekai.cooperative_live.write_image"):
            workflow.observe_play_state(Path(directory))
            with self.assertRaises(LifeDepleted):
                workflow.observe_play_state(Path(directory))
            workflow.last_report.update(completed=False, error="LifeDepleted: life zero")
            workflow.finish_performance_trace(workflow.last_report, Path(directory))
            rows = [json.loads(line) for line in (Path(directory) / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["life"]["zero_streak"] for row in rows if row["kind"] == "observation"], [1, 2])
        self.assertEqual(rows[-1]["error"], "LifeDepleted: life zero")
        self.assertFalse(rows[-1]["completed"])

    def test_unknown_page_does_not_reuse_an_old_bar_measurement(self):
        workflow = self.workflow(["playing", "unknown"])
        with tempfile.TemporaryDirectory() as directory, patch(
                "project_sekai.cooperative_live.time.perf_counter", side_effect=[0, 2]):
            workflow.observe_play_state(Path(directory))
            workflow.observe_play_state(Path(directory))
        samples = workflow.performance_trace.samples
        self.assertTrue(samples[0]["life"]["sampled"])
        self.assertFalse(samples[1]["life"]["sampled"])
        self.assertIsNone(samples[1]["life"]["bar_fill_pixels"])
        self.assertIsNone(samples[1]["life"]["value"])

    def test_solo_and_one_shot_observers_reuse_the_same_trace_without_extra_capture(self):
        for entry in (SoloLive, OneShotLive):
            workflow = self.workflow(["playing", "playing"])
            with self.subTest(entry=entry.__name__), tempfile.TemporaryDirectory() as directory, patch(
                    "project_sekai.solo_live.time.perf_counter", side_effect=[0, 2]), patch.object(
                    workflow, "screenshot", wraps=workflow.screenshot) as capture, patch(
                    "project_sekai.solo_live.write_image") as write:
                entry.observe_play_state(workflow, Path(directory))
                entry.observe_play_state(workflow, Path(directory))
                self.assertEqual(capture.call_count, 2)
                write.assert_not_called()
                self.assertEqual([row["elapsed_s"] for row in workflow.performance_trace.samples], [0, 2])
                self.assertTrue(all(row["life"]["sampled"] for row in workflow.performance_trace.samples))

    def test_recording_or_export_failure_never_changes_performance_success(self):
        workflow = self.workflow(["playing"])
        with tempfile.TemporaryDirectory() as directory, patch.object(
                PerformanceTrace, "observe", side_effect=RuntimeError("diagnostic failure")):
            workflow.observe_play_state(Path(directory))
            workflow.last_report.update(completed=True)
            workflow.finish_performance_trace(workflow.last_report, Path(directory))
        self.assertTrue(workflow.last_report["completed"])
        self.assertEqual(workflow.last_report["performance_trace"]["status"], "failed")
        workflow.begin_performance_trace()
        workflow.last_report.update(completed=True)
        with tempfile.TemporaryDirectory() as directory, patch.object(
                PerformanceTrace, "save", side_effect=OSError("disk full")):
            workflow.finish_performance_trace(workflow.last_report, Path(directory))
        self.assertTrue(workflow.last_report["completed"])
        self.assertIn("disk full", workflow.last_report["performance_trace"]["error"])

    def test_round_and_attempt_reset_do_not_mix_samples_or_input_plans(self):
        workflow = self.workflow(["playing"])
        with tempfile.TemporaryDirectory() as directory:
            workflow.observe_play_state(Path(directory))
        first = workflow.performance_trace
        workflow.begin_performance_trace()
        self.assertEqual(len(first.samples), 1)
        self.assertFalse(workflow.performance_trace.samples)
        self.assertFalse(workflow.performance_trace.events)

    def test_progress_and_command_cost_snapshots_are_frozen_when_future_windows_change(self):
        trace = PerformanceTrace()
        report = {"playback": {"sent_actions": 10, "executed_actions": 8, "latency_offsets": {"down_ms": .5}}}
        trace.observe(2, report, LifeGuard(), life_sampled=False)
        report["playback"]["sent_actions"] = 20
        report["playback"]["latency_offsets"]["down_ms"] = 1
        self.assertEqual(trace.samples[0]["playback"]["sent_actions"], 10)
        self.assertEqual(trace.samples[0]["latency_offsets"]["down_ms"], .5)

    def test_legacy_reuses_existing_dispatch_delays_without_marking_failed_input_successful(self):
        trace = PerformanceTrace()
        trace.set_plan((Touch(.1, 2, 0, "down", 500), Touch(.2, 0, 0, "up")))
        trace.set_legacy_feedback([.001, .003])
        with tempfile.TemporaryDirectory() as directory:
            trace.save(Path(directory), {"playback": {"sent_actions": 1}})
            rows = [json.loads(line) for line in (Path(directory) / "touch-plan.jsonl").read_text().splitlines()]
        self.assertEqual([row["legacy_dispatch_lateness_ms"] for row in rows], [1, 3])
        self.assertEqual([row["legacy_input_succeeded"] for row in rows], [True, False])

    @unittest.skipUnless(native_engine.available(), "需要已构建的 Native 扩展")
    def test_whole_native_song_with_trace_has_identical_commands_and_no_extra_underflows(self):
        events = tuple(Touch(1 + index * .12 + delay, order, 0, kind, 500)
                       for index in range(1000) for delay, order, kind in ((0, 2, "down"), (.024, 0, "up")))
        outputs = []
        for recording in (False, True):
            clock = test_native_playback.Clock()
            device = test_native_playback.ScriptDevice(clock)
            trace = PerformanceTrace()
            guard = LifeGuard()
            report = {}
            def observe():
                if recording:
                    trace.observe(clock() - 1000, report, guard, life_sampled=False)
                clock.sleep(.20)
            player = NativePlayer(None, events, Path("."), lambda: False, device=device, clock=clock,
                                  sleeper=clock.sleep, idle_observer=observe,
                                  observation_interval=2.0, observation_budget=.30)
            report["playback"] = player.report
            player.prepare()
            player.play(1000, -51)
            player.close()
            outputs.append((device.commands, player.report))
            if recording:
                self.assertGreater(trace.samples[-1]["elapsed_s"], 116)
                self.assertAlmostEqual(player.report["device_clock_offset_s"], 900, delta=.001)
                self.assertEqual(player.report["playback_epoch_s"], 1000)
        self.assertEqual(outputs[0][0], outputs[1][0])
        for _, report in outputs:
            self.assertEqual(report["queue_underflows"], 0)
            self.assertEqual(report["sent_actions"], 2000)
            self.assertEqual(report["executed_actions"], 2000)
            self.assertTrue(report["release_confirmed"])


if __name__ == "__main__":
    unittest.main()
