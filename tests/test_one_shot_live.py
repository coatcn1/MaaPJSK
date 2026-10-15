import csv
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
import one_shot_live as agent
import solo_live as solo_agent
import cooperative_live as cooperative_agent
from project_sekai.life_monitor import LifeDepleted, LifeGuard
import project_sekai.one_shot_live as one_shot_module
from project_sekai.one_shot_live import MissingOneShotChart, OneShotLive, OPENING_WAIT_SECONDS
from project_sekai.solo_live import SoloLive


ROOT = Path(__file__).resolve().parents[1]


def identity(difficulty="master"):
    return SimpleNamespace(song_id=7, title="测试歌曲", difficulty=difficulty,
                           to_dict=lambda: {"song_id": 7, "title": "测试歌曲", "difficulty": difficulty})


class OneShotTests(unittest.TestCase):
    def setUp(self):
        agent._SETTINGS.clear()

    def configuration(self, identifier=87, difficulty="master"):
        context = SimpleNamespace(tasker=SimpleNamespace(stopping=False, controller=object()))
        argv = SimpleNamespace(task_detail=SimpleNamespace(task_id=identifier),
                               custom_action_param=json.dumps({"difficulty": difficulty}))
        self.assertTrue(agent.ProjectSekaiOneShotLiveConfig().run(context, argv))
        return context, argv

    def workflow(self, root):
        workflow = OneShotLive.__new__(OneShotLive)
        workflow.report_root = root
        workflow.stop_requested = lambda: False
        workflow.log = Mock()
        workflow.device = Mock()
        workflow.device.screenshot.return_value = np.zeros((720, 1280, 3), np.uint8)
        workflow.device.shell.side_effect = ["com.android.launcher3/.Launcher\n",
                                            "mCurrentFocus=Window{123 u0 com.android.launcher3/.Launcher}\n"]
        workflow.navigator = Mock(threshold=.83)
        workflow.repository = Mock()
        workflow.repository.songs = {7: {"charts": {difficulty: {
            "music_id": 7, "difficulty": difficulty, "sha256": "verified", "path": "verified.sus", "total_note_count": 2}
            for difficulty in ("easy", "master")}}}
        workflow.repository.load_chart.return_value = "#BPM01:120\n#00008:01\n#00012:11\n#00113:11"

        def start(chart, prepared, report, directory, **kwargs):
            self.assertIsNone(chart)
            self.assertIsNone(prepared)
            kwargs["ready_action"]()
            final = identity(report["requested_difficulty"])
            report["final_identity"] = final.to_dict()
            kwargs["on_final_identity"](final)
            report["start_anchor"] = {"epoch": 123.0}
            return 123.0

        def play(events, epoch, offset, report, directory):
            report["playback"] = {"planned_actions": len(events), "sent_actions": len(events), "release_confirmed": True}

        workflow.start = Mock(side_effect=start)
        workflow.play = Mock(side_effect=play)
        workflow.wait_for_finish = Mock(side_effect=lambda report, directory: report.update(live_status="cleared"))
        return workflow

    def native(self, order=None):
        player = Mock()
        player.report = {"planned_actions": 4, "sent_actions": 0, "executed_actions": 0,
                         "queue_underflows": 0, "release_confirmed": False}
        player.play.side_effect = lambda *_: player.report.update(sent_actions=4, executed_actions=4)

        def close():
            if order is not None:
                order.append("release")
            player.report.update(release_confirmed=True,
                                 release={"reset_executed": True, "release_proof": "current-reset-jlog-and-cleanup"})

        player.close.side_effect = close
        return player

    def test_default_off_task_has_only_six_difficulties_and_all_overrides_exist(self):
        interface = json.loads((ROOT / "interface.json").read_text(encoding="utf-8"))
        pipeline = json.loads((ROOT / "resource/pipeline/one_shot_chart_live.json").read_text(encoding="utf-8"))
        task = next(item for item in interface["task"] if item["name"] == "OneShotChartLive")
        self.assertFalse(task["default_check"])
        self.assertEqual(task["option"], ["OneShotChartLiveDifficulty"])
        cases = interface["option"]["OneShotChartLiveDifficulty"]["cases"]
        self.assertEqual([case["name"] for case in cases], ["EASY", "NORMAL", "HARD", "EXPERT", "MASTER", "APPEND"])
        for case in cases:
            self.assertTrue(set(case["pipeline_override"]) <= pipeline.keys())
        self.assertEqual(OPENING_WAIT_SECONDS, 300)

    def test_configuration_isolated_and_cancelled_or_invalid_entries_are_discarded(self):
        context, argv = self.configuration()
        self.configuration(88, "easy")
        self.assertNotIn(87, solo_agent._SETTINGS)
        self.assertNotIn(87, cooperative_agent._SETTINGS)
        argv.custom_action_param = '{"difficulty":"other"}'
        self.assertFalse(agent.ProjectSekaiOneShotLiveConfig().run(context, argv))
        self.assertNotIn(87, agent._SETTINGS)
        self.assertIn(88, agent._SETTINGS)
        context.tasker.stopping = True
        argv.task_detail.task_id = 88
        self.assertFalse(agent.ProjectSekaiOneShotLive().run(context, argv))
        self.assertNotIn(88, agent._SETTINGS)

    def test_native_startup_guard_is_independent_and_frozen_at_initialization(self):
        for value in (None, "", "0", "1", "false", "other"):
            for solo_value in ("0", "1"):
                with self.subTest(value=value, solo_value=solo_value), \
                        patch.dict(os.environ, {"MAAPJSK_SOLO_NATIVE_STARTUP_GUARD": solo_value}, clear=True), \
                        patch("project_sekai.solo_live.Navigator"), \
                        patch("project_sekai.solo_live.ChartRepository"), \
                        patch("project_sekai.solo_live.LineOcr"), patch("project_sekai.solo_live.SongMatcher"):
                    if value is not None:
                        os.environ["MAAPJSK_ONE_SHOT_NATIVE_STARTUP_GUARD"] = value
                    workflow = OneShotLive(Mock(), Path("."), Path("."), Path("."), Path("."))
                    os.environ["MAAPJSK_ONE_SHOT_NATIVE_STARTUP_GUARD"] = "1" if value == "0" else "0"
                    self.assertEqual(workflow.one_shot_native_startup_guard, value != "0")
                    self.assertEqual(workflow.native_startup_guard, solo_value != "0")

    def test_native_startup_target_uses_validated_compiled_first_flick_and_manual_offset(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = self.workflow(Path(directory))
            workflow.one_shot_native_startup_guard = True
            workflow.repository.load_chart.return_value += "\n#00052:11"
            player = self.native()
            original = workflow.start.side_effect

            def start(*args, **kwargs):
                target = kwargs["native_start_target"]
                self.assertEqual(target, [])
                self.assertTrue(kwargs["native_start_fastpath"])
                workflow.repository.load_chart.assert_not_called()
                os.environ["MAAPJSK_ONE_SHOT_NATIVE_STARTUP_GUARD"] = "0"
                epoch = original(*args, **kwargs)
                self.assertEqual(len(target), 2)
                self.assertAlmostEqual(target[0], -.018)
                self.assertEqual(target[1], -51)
                return epoch

            workflow.start.side_effect = start
            with patch.dict(os.environ, {}), \
                    patch("project_sekai.one_shot_live.compile_touches", wraps=one_shot_module.compile_touches) as compile_plan, \
                    patch("project_sekai.native_player.NativePlayer", return_value=player) as create_player:
                report = workflow.run("master", -51, engine="native")
            compile_plan.assert_called_once()
            events = create_player.call_args.args[1]
            self.assertEqual(events[0].time, workflow.start.call_args.kwargs["native_start_target"][0])
            workflow.repository.load_chart.assert_called_once_with(7, "master")
            player.play.assert_called_once_with(123.0, -51)
            self.assertTrue(report["one_shot_native_startup_guard"])
            workflow.navigator.tap.assert_not_called()
            workflow.device.tap.assert_not_called()
            workflow.device.back.assert_not_called()
            workflow.device.home.assert_not_called()

    def test_disabled_or_legacy_startup_keeps_original_start_arguments(self):
        for engine, guard in (("native", False), ("legacy", True), ("legacy", False)):
            with self.subTest(engine=engine, guard=guard), tempfile.TemporaryDirectory() as directory:
                workflow = self.workflow(Path(directory))
                workflow.one_shot_native_startup_guard = guard
                with patch("project_sekai.native_player.NativePlayer", return_value=self.native()):
                    report = workflow.run("master", -51, engine=engine)
                self.assertNotIn("native_start_target", workflow.start.call_args.kwargs)
                self.assertNotIn("native_start_fastpath", workflow.start.call_args.kwargs)
                self.assertEqual(report["one_shot_native_startup_guard"], guard)

    def test_native_startup_chart_errors_leave_target_empty_and_never_start_playback(self):
        for stage in ("load", "compile"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                workflow = self.workflow(Path(directory))
                workflow.one_shot_native_startup_guard = True
                if stage == "load":
                    workflow.repository.load_chart.side_effect = ValueError("本地 SHA256 不匹配")
                compile_options = ({"side_effect": ValueError("编译拒绝非法触点")}
                                   if stage == "compile" else {"wraps": one_shot_module.compile_touches})
                with patch("project_sekai.one_shot_live.compile_touches", **compile_options), \
                        patch("project_sekai.native_player.NativePlayer") as create_player, \
                        self.assertRaises(MissingOneShotChart):
                    workflow.run("master", -51, engine="native")
                self.assertEqual(workflow.start.call_args.kwargs["native_start_target"], [])
                create_player.assert_not_called()
                workflow.play.assert_not_called()
                workflow.device.tap.assert_not_called()
                workflow.device.back.assert_not_called()
                workflow.device.home.assert_called_once()

    def test_real_start_consumes_late_target_and_rejects_late_handoff_after_release_without_logs(self):
        for epoch, accepted in ((1001., True), (1000.1, False)):
            with self.subTest(epoch=epoch), tempfile.TemporaryDirectory() as directory:
                workflow = self.workflow(Path(directory))
                workflow.one_shot_native_startup_guard = True
                del workflow.start
                workflow.repository.load_chart.return_value += "\n#00052:11"
                workflow.matcher = Mock()
                workflow.matcher.match.return_value = identity()
                workflow.observe_preplay_life = Mock()
                frame = np.full((720, 1280, 3), 50, np.uint8)
                workflow.device.screenshot.return_value = frame
                workflow.navigator.templates = {"life_hud": frame}
                workflow.navigator.match.side_effect = lambda _frame, name, *area: (
                    float(name in {"playing", "life_hud"}), (0, 0))
                workflow.pause = Mock()
                player = self.native()
                anchor = Mock(samples=[], fit={})
                anchor.observe.side_effect = [None, epoch]
                with patch("project_sekai.diagnostics._disabled", True), \
                        patch("project_sekai.solo_live.time.perf_counter", return_value=1000.), \
                        patch("project_sekai.solo_live.StartAnchor", return_value=anchor), \
                        patch("project_sekai.native_player.NativePlayer", return_value=player):
                    if accepted:
                        report = workflow.run("master", -51, engine="native")
                        self.assertTrue(report["completed"])
                        player.play.assert_called_once_with(epoch, -51)
                        workflow.device.home.assert_not_called()
                    else:
                        with self.assertRaisesRegex(RuntimeError, "实际启动余量不足"):
                            workflow.run("master", -51, engine="native")
                        report = workflow.last_report
                        player.play.assert_not_called()
                        workflow.wait_for_finish.assert_not_called()
                        workflow.device.home.assert_called_once()
                        self.assertTrue(report["background_exit"]["confirmed"])
                    player.close.assert_called_once()
                    self.assertTrue(report["playback"]["release_confirmed"])
                    self.assertEqual(report["startup_handoff"]["accepted"], accepted)
                    self.assertAlmostEqual(report["startup_handoff"]["first_target_s"], epoch - .018 - .051)
                    self.assertEqual(report["native_startup_hud_fastpath"]["accepted_frames"], 1)
                    self.assertEqual(list(Path(directory).iterdir()), [])
                workflow.device.tap.assert_not_called()
                workflow.device.back.assert_not_called()
                workflow.navigator.tap.assert_not_called()

    def test_agent_uses_manual_settings_and_cannot_reuse_a_solo_profile(self):
        context, argv = self.configuration()
        with patch.object(agent, "load_performance", return_value=SimpleNamespace(
                engine="native", touch_offset_ms=-51, use_calibration_profile=False)), \
                patch.object(agent, "create_workflow") as create, patch.object(agent, "visible_log"):
            self.assertTrue(agent.ProjectSekaiOneShotLive().run(context, argv))
            create.return_value.run.assert_called_once_with("master", -51, engine="native")
        context, argv = self.configuration()
        with patch.object(agent, "load_performance", return_value=SimpleNamespace(
                engine="native", use_calibration_profile=True)), \
                patch.object(agent, "create_workflow") as create, patch.object(agent, "visible_log"):
            self.assertFalse(agent.ProjectSekaiOneShotLive().run(context, argv))
            create.assert_not_called()

    def test_one_song_finishes_without_navigation_or_result_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = self.workflow(Path(directory))
            report = workflow.run("master", -51)
            workflow.repository.load_chart.assert_called_once_with(7, "master")
            self.assertTrue(report["completed"])
            self.assertTrue(report["input_completed"])
            self.assertEqual(report["result_status"], "not_collected")
            self.assertNotIn("judgements", report)
            self.assertEqual(workflow.start.call_args.kwargs["opening_timeout"], 300)
            self.assertEqual(workflow.start.call_args.kwargs["identity_phase"], "one_shot_final")
            self.assertTrue(workflow.start.call_args.kwargs["wait_for_opening"])
            workflow.navigator.tap.assert_not_called()
            workflow.navigator.return_to_home.assert_not_called()
            workflow.device.tap.assert_not_called()
            workflow.device.back.assert_not_called()
            with (Path(directory) / "results.csv").open(encoding="utf-8-sig") as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 1)

    def test_five_minute_unrecognized_gameplay_wait_never_loads_or_dispatches(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = self.workflow(Path(directory))
            del workflow.start
            clock = SimpleNamespace(now=1.0)

            def screenshot():
                clock.now += 60
                return workflow.device.screenshot()

            workflow.screenshot = screenshot
            workflow.pause = lambda seconds: setattr(clock, "now", clock.now + seconds)
            workflow.navigator.match.return_value = (1.0, (0, 0))
            workflow.matcher = Mock()
            workflow.matcher.match.side_effect = ValueError("封面与标题均未确认")
            with patch("project_sekai.solo_live.time.monotonic", side_effect=lambda: clock.now), \
                    self.assertRaisesRegex(TimeoutError, "300 秒"):
                workflow.run("master")
            self.assertGreaterEqual(clock.now, 301)
            workflow.repository.load_chart.assert_not_called()
            workflow.play.assert_not_called()
            workflow.navigator.tap.assert_not_called()
            workflow.device.home.assert_not_called()
            self.assertFalse(workflow.last_report["completed"])

    def test_unknown_timeout_or_cancel_does_not_operate_game_pages(self):
        for error in (TimeoutError("300 秒未识别"), InterruptedError("用户停止")):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                workflow = self.workflow(Path(directory))
                workflow.start.side_effect = error
                with self.assertRaises(type(error)):
                    workflow.run("master")
                workflow.play.assert_not_called()
                workflow.device.tap.assert_not_called()
                workflow.device.back.assert_not_called()
                workflow.device.home.assert_not_called()

    def test_missing_or_invalid_chart_homes_before_any_gameplay_input(self):
        for error in (FileNotFoundError("缺谱"), ValueError("哈希错误")):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                workflow = self.workflow(Path(directory))
                workflow.repository.load_chart.side_effect = error
                with patch("project_sekai.native_player.NativePlayer") as player, self.assertRaises(MissingOneShotChart):
                    workflow.run("master", engine="native")
                player.assert_not_called()
                workflow.play.assert_not_called()
                workflow.device.home.assert_called_once()
                self.assertTrue(workflow.last_report["background_exit"]["confirmed"])

    def test_native_execution_and_reset_precede_finish_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = self.workflow(Path(directory))
            player = self.native()
            workflow.wait_for_finish.side_effect = lambda report, _: self.assertTrue(report["playback"]["release_confirmed"])
            with patch("project_sekai.native_player.NativePlayer", return_value=player):
                report = workflow.run("master", engine="native")
            self.assertTrue(report["completed"])
            player.close.assert_called_once()
            workflow.device.home.assert_not_called()

    def test_life_zero_or_anchor_failure_releases_before_home_and_skips_finish(self):
        for failure in ("anchor", "life"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                workflow = self.workflow(Path(directory))
                workflow.one_shot_native_startup_guard = True
                order = []
                player = self.native(order)
                if failure == "life":
                    player.play.side_effect = LifeDepleted("生命零")
                    expected = LifeDepleted
                else:
                    original = workflow.start.side_effect

                    def reject_anchor(*args, **kwargs):
                        original(*args, **kwargs)
                        raise RuntimeError("锚点未确认")

                    workflow.start.side_effect = reject_anchor
                    expected = RuntimeError
                with patch("project_sekai.native_player.NativePlayer", return_value=player), \
                        patch("project_sekai.one_shot_live.background_game", side_effect=lambda *_a, **_k: order.append("home")), \
                        self.assertRaises(expected):
                    workflow.run("master", engine="native")
                self.assertEqual(order, ["release", "home"])
                workflow.wait_for_finish.assert_not_called()
                self.assertFalse(workflow.last_report["completed"])

    def test_stop_during_failure_background_exit_is_reported_as_cancelled(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = self.workflow(Path(directory))
            workflow.wait_for_finish.side_effect = RuntimeError('原结束失败')
            stopped = False
            workflow.stop_requested = lambda: stopped
            def exiting(*args, **kwargs):
                nonlocal stopped
                stopped = True
                self.assertIs(kwargs['stop_requested'], workflow.stop_requested)
                raise InterruptedError('退出期间用户停止')
            with patch('project_sekai.one_shot_live.background_game', side_effect=exiting), self.assertRaises(InterruptedError):
                workflow.run('master')
            self.assertTrue(workflow.last_report['cancelled'])
            self.assertIn('原结束失败', workflow.last_report['error'])
            workflow.device.home.assert_not_called()

    def test_native_cancel_releases_without_home_or_settlement(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = self.workflow(Path(directory))
            workflow.one_shot_native_startup_guard = True
            player = self.native()
            player.play.side_effect = InterruptedError("用户停止")
            with patch("project_sekai.native_player.NativePlayer", return_value=player), self.assertRaises(InterruptedError):
                workflow.run("master", engine="native")
            player.close.assert_called_once()
            workflow.device.home.assert_not_called()
            workflow.wait_for_finish.assert_not_called()

    def test_release_failure_forbids_home_and_cannot_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = self.workflow(Path(directory))
            workflow.one_shot_native_startup_guard = True
            player = self.native()
            player.close.side_effect = RuntimeError("reset 失败")
            with patch("project_sekai.native_player.NativePlayer", return_value=player), self.assertRaisesRegex(RuntimeError, "reset"):
                workflow.run("master", engine="native")
            player.close.assert_called_once()
            workflow.device.home.assert_not_called()
            self.assertIn("释放", workflow.last_report["background_error"])
            self.assertFalse(workflow.last_report["completed"])

    def test_incomplete_native_execution_cannot_become_successful_finish(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = self.workflow(Path(directory))
            player = self.native()
            player.play.side_effect = lambda *_: player.report.update(sent_actions=4, executed_actions=3)
            with patch("project_sekai.native_player.NativePlayer", return_value=player), self.assertRaisesRegex(RuntimeError, "Native"):
                workflow.run("master", engine="native")
            workflow.wait_for_finish.assert_not_called()
            self.assertFalse(workflow.last_report["completed"])

    def test_persistence_failure_is_not_reported_as_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = self.workflow(Path(directory))
            with patch("project_sekai.one_shot_live.append_one_shot_result_index", side_effect=OSError("磁盘写入失败")), \
                    self.assertRaises(OSError):
                workflow.run("master")
            saved = json.loads(Path(workflow.last_report["report_path"]).read_text(encoding="utf-8"))
            self.assertFalse(saved["completed"])
            self.assertIn("persistence_error", saved)

    def test_finish_clear_saves_evidence_without_clicking_rewards_or_back(self):
        workflow = self.workflow(Path("unused"))
        del workflow.wait_for_finish
        workflow.device.screenshot.return_value = np.full((720, 1280, 3), 50, np.uint8)
        workflow.navigator.match.side_effect = lambda _, page, *args: (1.0 if page == "live_clear" else 0.0, (0, 0))
        with tempfile.TemporaryDirectory() as directory:
            report = {}
            workflow.wait_for_finish(report, Path(directory))
            self.assertEqual(report["live_status"], "cleared")
            self.assertTrue((Path(directory) / "live-clear.png").is_file())
        workflow.device.tap.assert_not_called()
        workflow.device.back.assert_not_called()

    def test_finish_uses_life_hud_disappearance_after_black_without_clear_banner(self):
        workflow = self.workflow(Path("unused"))
        del workflow.wait_for_finish
        workflow.life_guard = LifeGuard()
        workflow.life_guard.hud_seen = True
        workflow.navigator.templates = {"life_hud": None}
        workflow.navigator.match.return_value = (0.0, (0, 0))
        black = np.zeros((720, 1280, 3), np.uint8)
        finish = np.full((720, 1280, 3), 50, np.uint8)
        workflow.device.screenshot.side_effect = [black, finish, finish]
        workflow.pause = lambda _: None
        report = {"engine": "legacy", "playback": {
            "planned_actions": 4, "sent_actions": 4, "release_confirmed": True}}
        with tempfile.TemporaryDirectory() as directory:
            workflow.wait_for_finish(report, Path(directory))
        self.assertEqual(report["live_status"], "ended")
        self.assertEqual(report["end_detection"]["method"], "life_hud_disappeared")
        workflow.device.tap.assert_not_called()
        workflow.device.back.assert_not_called()
        workflow.device.home.assert_not_called()

    def test_waiting_screenshot_does_not_interpret_title_screen_or_navigate(self):
        workflow = self.workflow(Path("unused"))
        workflow.ocr = Mock()
        frame = workflow.screenshot()
        self.assertIs(frame, workflow.device.screenshot.return_value)
        workflow.ocr.read.assert_not_called()
        workflow.navigator.match.assert_not_called()
        workflow.navigator.tap.assert_not_called()


if __name__ == "__main__":
    unittest.main()
