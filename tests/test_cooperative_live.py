import csv
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
import cooperative_live as agent
import solo_live as solo_agent
from project_sekai.cooperative_live import PAGE_AREAS, CooperativeLive, MissingCooperativeChart, RoomDisbanded, RoomMatchingTimeout
from project_sekai.maa_device import MaaDevice
from project_sekai.live_end import LIFE_HUD_AREA
from project_sekai.life_monitor import LifeDepleted, LifeGuard, ZeroLifeTemplate
from project_sekai.ocr import Reading
from project_sekai.solo_live import LiveResult
from project_sekai.song_identity import COOPERATIVE_DIFFICULTIES, cooperative_difficulty_selected
from project_sekai.song_identity import SongMatcher


ROOT = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self):
        self.now = 1.0

    def advance(self, seconds):
        self.now += seconds


def identity(song_id=520):
    return SimpleNamespace(song_id=song_id, difficulty="easy", title="测试歌曲",
                           to_dict=lambda: {"song_id": song_id, "title": "测试歌曲", "difficulty": "easy"})


class CooperativeTests(unittest.TestCase):
    def setUp(self):
        agent._SETTINGS.clear()

    def configuration(self, identifier=41):
        context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
        pipeline = json.loads((ROOT / "resource/pipeline/cooperative_chart_live.json").read_text(encoding="utf-8"))
        for name, node in pipeline.items():
            if name.endswith("Config"):
                argv = SimpleNamespace(task_detail=SimpleNamespace(task_id=identifier),
                                       custom_action_param=json.dumps(node["custom_action_param"]))
                self.assertTrue(agent.ProjectSekaiCooperativeLiveConfig().run(context, argv))
        return context, SimpleNamespace(task_detail=SimpleNamespace(task_id=identifier))

    def test_pipeline_exposes_two_vote_modes_five_verified_difficulties_and_is_default_off(self):
        interface = json.loads((ROOT / "interface.json").read_text(encoding="utf-8"))
        task = next(item for item in interface["task"] if item["name"] == "CooperativeChartLive")
        self.assertFalse(task["default_check"])
        cases = interface["option"]["CooperativeChartLiveSongMode"]["cases"]
        self.assertEqual([case["name"] for case in cases], ["Current", "Random"])
        self.assertEqual([case["name"] for case in interface["option"]["CooperativeChartLiveDifficulty"]["cases"]],
                         ["EASY", "NORMAL", "HARD", "EXPERT", "MASTER"])
        context, argv = self.configuration()
        self.assertEqual(agent._SETTINGS[41], {"room": "free", "song_mode": "current", "difficulty": "easy",
                                             "count": 1, "recovery_mode": "off", "recovery_count": 1})
        pipeline = json.loads((ROOT / "resource/pipeline/cooperative_chart_live.json").read_text(encoding="utf-8"))
        for option in task["option"]:
            specification = interface["option"][option]
            overrides = ([case["pipeline_override"] for case in specification["cases"]]
                         if "cases" in specification else [specification["pipeline_override"]])
            for override in overrides:
                self.assertTrue(set(override) <= pipeline.keys())

    def test_task_configuration_does_not_leak_into_solo_or_other_task(self):
        self.configuration(41)
        self.configuration(42)
        agent._SETTINGS[41]["count"] = 2
        self.assertEqual(agent._SETTINGS[42]["count"], 1)
        self.assertNotIn(41, solo_agent._SETTINGS)
        context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
        argv = SimpleNamespace(task_detail=SimpleNamespace(task_id=41), custom_action_param='{"count":0}')
        self.assertFalse(agent.ProjectSekaiCooperativeLiveConfig().run(context, argv))
        self.assertNotIn(41, agent._SETTINGS)
        self.assertIn(42, agent._SETTINGS)

    def test_cancelled_configuration_and_run_discard_task_state(self):
        context, argv = self.configuration()
        context.tasker.stopping = True
        self.assertFalse(agent.ProjectSekaiCooperativeLive().run(context, argv))
        self.assertNotIn(41, agent._SETTINGS)
        agent._SETTINGS[41] = {"room": "free"}
        argv.custom_action_param = '{"count":1}'
        self.assertFalse(agent.ProjectSekaiCooperativeLiveConfig().run(context, argv))
        self.assertNotIn(41, agent._SETTINGS)

    def test_cooperative_rejects_solo_calibration_before_device_operations(self):
        context, argv = self.configuration()
        with patch.object(agent, "load_performance", return_value=SimpleNamespace(engine="native", use_calibration_profile=True,
                cooperative_game_timing_feedback=False)), \
                patch.object(agent, "create_workflow") as create, patch.object(agent, "visible_log"):
            self.assertFalse(agent.ProjectSekaiCooperativeLive().run(context, argv))
        create.assert_not_called()

    def test_agent_uses_saved_engine_manual_offset_and_stamina_without_modifying_settings(self):
        context, argv = self.configuration()
        performance = SimpleNamespace(engine="native", use_calibration_profile=False, touch_offset_ms=-51, bonus_consumption=5,
                                      cooperative_game_timing_feedback=False)
        workflow = Mock()
        with patch.object(agent, "load_performance", return_value=performance), \
                patch.object(agent, "create_workflow", return_value=workflow), patch.object(agent, "visible_log") as log:
            self.assertTrue(agent.ProjectSekaiCooperativeLive().run(context, argv))
        workflow.run.assert_called_once_with(1, "easy", "current", -51, room="free", bonus_consumption=5,
                                            recovery_mode="off", recovery_count=1, engine="native", game_timing_feedback=False)
        self.assertEqual(performance.bonus_consumption, 5)
        self.assertIn("FAST / LATE 微调（试验）：关", log.call_args.args[1])

    def test_agent_initial_log_ipc_failure_does_not_abort_workflow(self):
        context, argv = self.configuration()
        performance = SimpleNamespace(engine='native', use_calibration_profile=False,
                                      touch_offset_ms=-51, bonus_consumption=5, cooperative_game_timing_feedback=False)
        workflow = Mock()
        with patch.object(agent, 'load_performance', return_value=performance), patch.object(agent, 'create_workflow', return_value=workflow), patch.object(agent, 'visible_log', side_effect=OSError('日志IPC断开')):
            self.assertTrue(agent.ProjectSekaiCooperativeLive().run(context, argv))
        workflow.run.assert_called_once()

    def test_agent_forwards_enabled_game_timing_feedback_and_logs_user_switch(self):
        for engine in ("native", "legacy"):
            with self.subTest(engine=engine):
                context, argv = self.configuration()
                performance = SimpleNamespace(engine=engine, use_calibration_profile=False, touch_offset_ms=-51,
                                              bonus_consumption=5, cooperative_game_timing_feedback=True)
                workflow = Mock()
                with patch.object(agent, "load_performance", return_value=performance), \
                        patch.object(agent, "create_workflow", return_value=workflow), \
                        patch.object(agent, "visible_log") as log:
                    self.assertTrue(agent.ProjectSekaiCooperativeLive().run(context, argv))
                self.assertIs(workflow.run.call_args.kwargs["game_timing_feedback"], True)
                self.assertEqual(workflow.run.call_args.kwargs["engine"], engine)
                self.assertEqual(performance.touch_offset_ms, -51)
                self.assertIn("FAST / LATE 微调（试验）：开", log.call_args.args[1])

    def test_agent_rejects_non_boolean_game_timing_switch_before_device_operations(self):
        context, argv = self.configuration()
        performance = SimpleNamespace(engine="native", use_calibration_profile=False, cooperative_game_timing_feedback="true")
        with patch.object(agent, "load_performance", return_value=performance), \
                patch.object(agent, "create_workflow") as create, patch.object(agent, "visible_log"):
            self.assertFalse(agent.ProjectSekaiCooperativeLive().run(context, argv))
        create.assert_not_called()

    def test_selected_difficulty_uses_local_filled_circle_not_another_player_card(self):
        for difficulty, (point, hues) in COOPERATIVE_DIFFICULTIES.items():
            frame = np.zeros((720, 1280, 3), np.uint8)
            color = cv2.cvtColor(np.uint8([[[(hues[0] + hues[1]) // 2, 240, 220]]]), cv2.COLOR_HSV2BGR)[0, 0]
            cv2.circle(frame, point, 29, tuple(int(value) for value in color), -1)
            self.assertTrue(cooperative_difficulty_selected(frame, difficulty))
            for other in set(COOPERATIVE_DIFFICULTIES) - {difficulty}:
                self.assertFalse(cooperative_difficulty_selected(frame, other))
        self.assertFalse(cooperative_difficulty_selected(frame, "append"))

    def test_android_home_checks_controller_ack_and_uses_home_key(self):
        controller = Mock()
        controller.post_click_key.return_value.wait.return_value.succeeded = True
        MaaDevice(controller).home()
        controller.post_click_key.assert_called_once_with(3)
        controller.post_click_key.return_value.wait.return_value.succeeded = False
        with self.assertRaisesRegex(RuntimeError, "HOME"):
            MaaDevice(controller).home()

    def ui_workflow(self, states):
        workflow = CooperativeLive.__new__(CooperativeLive)
        clock = Clock()
        frames = {name: np.full((720, 1280, 3), index + 40, np.uint8) for index, name in enumerate(set(states))}
        tags = {"room": "cooperative_room", "matching": "cooperative_matching", "select": "cooperative_select",
                "matching_decided": "cooperative_matching", "matching_full": "cooperative_matching", "matching_locked": "cooperative_matching",
                "matching_disabled": "cooperative_matching",
                "shuffle": "cooperative_shuffle", "ready": "cooperative_ready", "cancel": "cooperative_cancel", "disbanded": "cooperative_disbanded",
                "personal": "cooperative_personal_result", "playing": "playing", "home": "home", "clear": "live_clear",
                "failed":"live_failed"}
        for state in {"matching", "matching_full", "matching_decided"} & frames.keys():
            cv2.circle(frames[state], (43, 42), 25, (255, 255, 255), -1)
        if "black" in frames:
            frames["black"][:] = 0
        sequence = iter(states)
        current = states[0]

        def screenshot():
            nonlocal current
            current = next(sequence, current)
            clock.advance(.04)
            return frames[current]

        def match(frame, page, *args):
            matched = any(frame is image and (tags.get(state) == page
                          or state == "playing" and page == "life_hud"
                          or state in {"matching", "matching_disabled"} and page == "cooperative_member_waiting"
                          or state == "matching_decided" and page == "cooperative_member_decided"
                          or state in {"dialog", "dialog_no_ok"} and page == "cooperative_disbanded_dialog"
                          or state == "dialog" and page == "cooperative_disbanded_dialog_ok")
                          for state, image in frames.items())
            return (1.0 if matched else 0.0), (0, 0)

        workflow.device = Mock()
        workflow.device.screenshot.return_value = np.zeros((720, 1280, 3), np.uint8)
        workflow.device.shell.side_effect = ["com.android.launcher3/.Launcher\n",
                                            "mCurrentFocus=Window{123 u0 com.android.launcher3/.Launcher}\n"]
        workflow.navigator = Mock(threshold=.83, templates={name:None for name in PAGE_AREAS})
        workflow.navigator.templates.update(life_hud=None,home=None,title_screen=None,live_failed=None,live_clear=None)
        workflow.navigator.match.side_effect = match
        workflow.stop_requested = lambda: False
        workflow.screenshot = screenshot
        workflow.pause = clock.advance
        workflow.matched = False
        workflow.log = Mock()
        workflow.ocr = Mock()
        workflow.matcher = Mock()
        workflow.current_report = None
        workflow.life_guard = LifeGuard(bar_area=(1008, 48, 1177, 56))
        return workflow, clock, frames

    def test_matching_shuffle_and_waiting_pages_send_no_inputs_and_can_be_cancelled(self):
        workflow, clock, _ = self.ui_workflow(["matching", "shuffle", "waiting", "ready"])
        with patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now):
            workflow.wait_page("cooperative_ready", 5)
        workflow.device.tap.assert_not_called()
        workflow.device.back.assert_not_called()
        workflow.navigator.tap.assert_not_called()
        workflow.screenshot = Mock(side_effect=InterruptedError("用户停止"))
        with self.assertRaises(InterruptedError):
            workflow.wait_page("cooperative_ready")

    def test_matching_timeout_enters_room_recovery_without_page_input(self):
        workflow, clock, _ = self.ui_workflow(["matching"])
        with tempfile.TemporaryDirectory() as directory:
            workflow.current_directory = Path(directory)
            with patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now):
                with self.assertRaises(RoomDisbanded):
                    workflow.wait_page("cooperative_select", .1)
        workflow.device.back.assert_not_called()
        workflow.device.home.assert_not_called()
        workflow.device.tap.assert_not_called()

    def test_selection_arriving_at_deadline_continues_without_exit(self):
        workflow, clock, frames = self.ui_workflow(["matching", "select"])
        with patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now):
            self.assertIs(workflow.wait_page("cooperative_select", .01), frames["select"])
        workflow.device.back.assert_not_called()

    def test_other_unknown_wait_timeout_is_not_matching_recovery(self):
        workflow, clock, _ = self.ui_workflow(["unknown"])
        with tempfile.TemporaryDirectory() as directory:
            workflow.current_directory = Path(directory)
            with patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now):
                with self.assertRaises(TimeoutError):
                    workflow.wait_page("cooperative_select", .1)
        workflow.device.back.assert_not_called()

    def test_stalled_matching_arrow_returns_to_confirmed_room_selection(self):
        workflow, clock, _ = self.ui_workflow(["matching", "matching", "room"])
        report = {}
        with tempfile.TemporaryDirectory() as directory, \
                patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now):
            self.assertIsNone(workflow.return_after_matching_timeout(report, Path(directory)))
        workflow.device.tap.assert_called_once_with(43, 43)
        workflow.device.back.assert_not_called()
        workflow.device.home.assert_not_called()
        self.assertTrue(report["room_recovery"]["returned_to_room"])
        self.assertEqual(report["room_recovery"]["exit_clicks"], 1)

    def test_matching_filling_during_log_resumes_selection_without_back(self):
        workflow, clock, frames = self.ui_workflow(["matching", "select"])
        report = {}
        with tempfile.TemporaryDirectory() as directory, \
                patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now):
            self.assertIs(workflow.return_after_matching_timeout(report, Path(directory)), frames["select"])
        workflow.device.back.assert_not_called()
        self.assertTrue(report["room_recovery"]["selection_arrived"])

    def test_matching_becoming_gameplay_before_exit_never_sends_back(self):
        workflow, clock, _ = self.ui_workflow(["matching", "playing"])
        with tempfile.TemporaryDirectory() as directory, \
                patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now):
            with self.assertRaisesRegex(RuntimeError, "演奏"):
                workflow.return_after_matching_timeout({}, Path(directory))
        workflow.device.back.assert_not_called()

    def test_matching_exit_has_a_click_limit_and_requires_actual_room_return(self):
        workflow, clock, _ = self.ui_workflow(["matching"])
        with tempfile.TemporaryDirectory() as directory, \
                patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now):
            with self.assertRaises(TimeoutError):
                workflow.return_after_matching_timeout({}, Path(directory))
        self.assertEqual(workflow.device.tap.call_args_list, [unittest.mock.call(43, 43)] * 3)
        workflow.device.back.assert_not_called()
        workflow.device.home.assert_not_called()

    def test_matching_exit_stop_and_unreleased_input_block_clicks(self):
        for playback in [None, {"sent_actions": 0, "release_confirmed": False},
                         {"sent_actions": 1, "release_confirmed": True}]:
            with self.subTest(playback=playback):
                workflow, clock, _ = self.ui_workflow(["matching"])
                if playback is None:
                    workflow.navigator._check_stop.side_effect = InterruptedError("用户停止")
                    expected = InterruptedError
                else:
                    workflow.current_report = {"playback": playback}
                    expected = RuntimeError
                with tempfile.TemporaryDirectory() as directory, \
                        patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now):
                    with self.assertRaises(expected):
                        workflow.return_after_matching_timeout({}, Path(directory))
                workflow.device.back.assert_not_called()
                workflow.device.tap.assert_not_called()

    def test_matching_exit_does_not_repeat_click_on_an_unknown_page(self):
        workflow, clock, _ = self.ui_workflow(["matching", "matching", "unknown"])
        with tempfile.TemporaryDirectory() as directory, \
                patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now):
            with self.assertRaises(TimeoutError):
                workflow.return_after_matching_timeout({}, Path(directory))
        workflow.device.tap.assert_called_once_with(43, 43)
        workflow.device.back.assert_not_called()

    def test_matching_exit_blocked_log_cannot_click_after_recovery_deadline(self):
        workflow, clock, _ = self.ui_workflow(["matching"])
        workflow.log.side_effect = lambda *_: clock.advance(21)
        with tempfile.TemporaryDirectory() as directory, \
                patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now):
            with self.assertRaises(TimeoutError):
                workflow.return_after_matching_timeout({}, Path(directory))
        workflow.device.back.assert_not_called()
        workflow.device.tap.assert_not_called()

    def test_matching_full_room_waits_for_selection_without_exit(self):
        workflow, clock, frames = self.ui_workflow(["matching_full", "matching_locked", "select"])
        report = {}
        with tempfile.TemporaryDirectory() as directory, \
                patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now):
            self.assertIs(workflow.return_after_matching_timeout(report, Path(directory)), frames["select"])
        workflow.device.tap.assert_not_called()
        workflow.device.back.assert_not_called()
        self.assertTrue(report["room_recovery"]["selection_arrived"])

    def test_matching_filling_before_exit_click_continues_original_room(self):
        workflow, clock, frames = self.ui_workflow(["matching", "matching_full", "matching_locked", "select"])
        with tempfile.TemporaryDirectory() as directory, \
                patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now):
            self.assertIs(workflow.return_after_matching_timeout({}, Path(directory)), frames["select"])
        workflow.device.tap.assert_not_called()
        workflow.device.back.assert_not_called()

    def test_selection_after_exit_request_continues_instead_of_failing(self):
        workflow, clock, frames = self.ui_workflow(["matching", "matching", "select"])
        report = {}
        with tempfile.TemporaryDirectory() as directory, \
                patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now):
            self.assertIs(workflow.return_after_matching_timeout(report, Path(directory)), frames["select"])
        workflow.device.tap.assert_called_once_with(43, 43)
        workflow.device.back.assert_not_called()
        self.assertTrue(report["room_recovery"]["selection_arrived"])
        self.assertFalse(report["room_recovery"]["returned_to_room"])

    def test_matching_disabled_back_with_empty_slot_never_clicks(self):
        workflow, clock, frames = self.ui_workflow(["matching_disabled", "select"])
        with tempfile.TemporaryDirectory() as directory, \
                patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now):
            self.assertIs(workflow.return_after_matching_timeout({}, Path(directory)), frames["select"])
        workflow.device.tap.assert_not_called()
        workflow.device.back.assert_not_called()

    def test_real_choose_continues_after_exit_races_with_song_selection(self):
        workflow, clock, _ = self.ui_workflow(["room", "room", "matching", "matching", "select", "select",
                                             "ready", "ready", "ready", "ready"])
        real_wait = workflow.wait_page

        def wait(page, timeout=30, *, guard=True):
            if page == "cooperative_select":
                workflow.matched = True
                raise RoomMatchingTimeout("成员匹配超时")
            return real_wait(page, timeout, guard=guard)

        workflow.wait_page = Mock(side_effect=wait)
        workflow.ocr.read.return_value = Reading("軽量", .99)
        workflow.matcher.match.return_value = identity()
        report = {"attempts": [{"attempt": 1, "status": "preparing"}]}
        with tempfile.TemporaryDirectory() as directory, \
                patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now):
            workflow.current_directory = Path(directory)
            selected, _ = workflow.choose("free", "easy", "random", report, Path(directory))
        self.assertEqual(selected.song_id, 520)
        self.assertEqual(report["song_vote"]["mode"], "random")
        workflow.device.tap.assert_not_called()
        workflow.device.back.assert_not_called()
        workflow.device.home.assert_not_called()
        self.assertEqual(len(report["attempts"]), 1)

    def test_disbanded_overlay_is_handled_only_before_playback(self):
        workflow, _, frames = self.ui_workflow(["disbanded"])
        with self.assertRaises(RoomDisbanded):
            workflow.guard_room(frames["disbanded"])

    def test_member_room_confirmation_dialog_is_detected(self):
        workflow, _, frames = self.ui_workflow(["dialog"])
        with self.assertRaises(RoomDisbanded):
            workflow.guard_room(frames["dialog"])
        workflow.device.tap.assert_not_called()

    def test_room_dialog_is_confirmed_once_then_return_is_verified(self):
        workflow, clock, _ = self.ui_workflow(["dialog", "dialog", "dialog", "room"])
        report = {}
        with tempfile.TemporaryDirectory() as directory, \
                patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now):
            workflow.return_after_disbanded(report, Path(directory))
        workflow.device.tap.assert_called_once_with(640, 425)
        self.assertEqual(report["room_recovery"], {"dialog_detected": True, "ok_clicked": True, "returned_to_room": True})

    def test_105_notice_waiting_for_ready_must_be_dismissed_before_rematching(self):
        workflow, clock, frames = self.ui_workflow(["disbanded", "room"])
        dismissed = False

        def screenshot():
            clock.advance(.04)
            return frames["room" if dismissed else "disbanded"]

        def dismiss(x, y):
            nonlocal dismissed
            self.assertEqual((x, y), (640, 360))
            dismissed = True

        # 复现实际第三局：横幅一直留在抽选页，只有点击关闭后才回到房间选择页。
        workflow.screenshot = screenshot
        workflow.device.tap.side_effect = dismiss
        report = {}
        with tempfile.TemporaryDirectory() as directory, \
                patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now):
            with self.assertRaises(RoomDisbanded):
                workflow.wait_page("cooperative_ready", 90)
            workflow.return_after_disbanded(report, Path(directory))
        workflow.device.tap.assert_called_once_with(640, 360)
        self.assertTrue(report["room_recovery"]["notice_dismissed"])
        self.assertTrue(report["room_recovery"]["returned_to_room"])
        self.assertFalse(report["room_recovery"]["ok_clicked"])
        workflow.device.home.assert_not_called()

    def test_105_notice_disappearing_before_input_never_clicks_underlying_page(self):
        workflow, clock, _ = self.ui_workflow(["disbanded", "room"])
        report = {}
        with tempfile.TemporaryDirectory() as directory, \
                patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now):
            workflow.return_after_disbanded(report, Path(directory))
        workflow.device.tap.assert_not_called()
        self.assertTrue(report["room_recovery"]["returned_to_room"])

    def test_105_notice_retries_once_when_first_successful_tap_does_not_close_it(self):
        workflow,clock,frames=self.ui_workflow(['disbanded','room'])
        click_times=[]
        def screenshot():
            clock.advance(.04)
            return frames['room' if len(click_times)>=2 else 'disbanded']
        workflow.screenshot=screenshot
        workflow.device.tap.side_effect=lambda *_:click_times.append(clock.now)
        workflow.device.back.side_effect=lambda:click_times.append(clock.now)
        report={}
        with tempfile.TemporaryDirectory() as directory,patch('project_sekai.cooperative_live.time.monotonic',side_effect=lambda:clock.now):
            workflow.return_after_disbanded(report,Path(directory))
        self.assertEqual(len(click_times),2)
        self.assertGreaterEqual(click_times[1]-click_times[0],1.)
        self.assertTrue(report['room_recovery']['notice_click_requested'])
        self.assertTrue(report['room_recovery']['notice_dismissed'])
        self.assertTrue(report['room_recovery']['returned_to_room'])
        workflow.device.tap.assert_called_once_with(640,360)
        workflow.device.back.assert_called_once_with()
        self.assertEqual(report['room_recovery']['notice_dismiss_request_count'],2)

    def test_105_notice_persistent_after_three_taps_is_not_reported_dismissed(self):
        workflow,clock,_=self.ui_workflow(['disbanded'])
        click_times=[];workflow.device.tap.side_effect=lambda *_:click_times.append(clock.now)
        workflow.device.back.side_effect=lambda:click_times.append(clock.now)
        report={}
        with tempfile.TemporaryDirectory() as directory,patch('project_sekai.cooperative_live.time.monotonic',side_effect=lambda:clock.now):
            with self.assertRaises(TimeoutError):workflow.return_after_disbanded(report,Path(directory))
        self.assertEqual(len(click_times),3)
        self.assertTrue(all(after-before>=1 for before,after in zip(click_times,click_times[1:])))
        self.assertFalse(report['room_recovery']['notice_dismissed'])
        self.assertEqual(report['room_recovery']['notice_click_count'],1)
        self.assertEqual(report['room_recovery']['notice_back_count'],2)
        self.assertEqual(report['room_recovery']['notice_dismiss_request_count'],3)
        self.assertLess(clock.now-1.,20.3)
        self.assertEqual(workflow.device.back.call_count,2)

    def test_105_notice_log_or_evidence_delay_rechecks_frame_before_each_input(self):
        for destination in ['final','playing']:
            for blocker in ['log','write']:
                with self.subTest(destination=destination,blocker=blocker):
                    workflow,clock,frames=self.ui_workflow(['disbanded',destination])
                    state='disbanded'
                    def screenshot():
                        clock.advance(.04)
                        return frames[state]
                    def transition(*_):
                        nonlocal state
                        state=destination
                    workflow.screenshot=screenshot
                    if blocker=='log':workflow.log.side_effect=transition
                    report={}
                    with tempfile.TemporaryDirectory() as directory,patch('project_sekai.cooperative_live.time.monotonic',side_effect=lambda:clock.now),patch('project_sekai.cooperative_live.write_image',side_effect=transition if blocker=='write' else None):
                        with self.assertRaises(RuntimeError if destination=='playing' else TimeoutError):
                            workflow.return_after_disbanded(report,Path(directory))
                    workflow.device.tap.assert_not_called()
                    workflow.device.back.assert_not_called()

    def test_105_notice_retry_wait_transition_and_stop_never_send_second_input(self):
        for destination in ['final','playing','stop']:
            with self.subTest(destination=destination):
                workflow,clock,frames=self.ui_workflow(['disbanded','final','playing'])
                workflow.screenshot=lambda: (clock.advance(.04) or frames[
                    destination if destination!='stop' and workflow.device.tap.call_count else 'disbanded'])
                def check_stop():
                    if destination=='stop' and workflow.device.tap.call_count:raise InterruptedError('用户停止')
                workflow.navigator._check_stop.side_effect=check_stop
                report={}
                with tempfile.TemporaryDirectory() as directory,patch('project_sekai.cooperative_live.time.monotonic',side_effect=lambda:clock.now):
                    with self.assertRaises(InterruptedError if destination=='stop' else RuntimeError if destination=='playing' else TimeoutError):
                        workflow.return_after_disbanded(report,Path(directory))
                workflow.device.tap.assert_called_once_with(640,360)

    def test_105_notice_blank_transition_after_tap_does_not_disable_retry(self):
        for value in [0,255]:
            with self.subTest(value=value):
                workflow,clock,frames=self.ui_workflow(['disbanded','room'])
                blank=np.full_like(frames['disbanded'],value);blank_seen=False
                def screenshot():
                    nonlocal blank_seen
                    clock.advance(.04)
                    requests=workflow.device.tap.call_count+workflow.device.back.call_count
                    if requests>=2:return frames['room']
                    if requests==1 and not blank_seen:
                        blank_seen=True
                        return blank
                    return frames['disbanded']
                workflow.screenshot=screenshot;report={}
                with tempfile.TemporaryDirectory() as directory,patch('project_sekai.cooperative_live.time.monotonic',side_effect=lambda:clock.now):
                    workflow.return_after_disbanded(report,Path(directory))
                self.assertEqual(workflow.device.tap.call_count,1)
                self.assertEqual(workflow.device.back.call_count,1)
                self.assertTrue(report['room_recovery']['notice_dismissed'])
                self.assertTrue(report['room_recovery']['returned_to_room'])

    def test_real_disbanded_retry_recovery_continues_round_without_extra_completed_count(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow=self.run_workflow(Path(directory))
            ui,clock,frames=self.ui_workflow(['disbanded','room'])
            workflow.navigator=ui.navigator;workflow.device=ui.device;workflow.pause=ui.pause
            workflow.screenshot=lambda: (clock.advance(.04) or frames['room' if workflow.device.tap.call_count+workflow.device.back.call_count>=2 else 'disbanded'])
            workflow.return_after_disbanded=CooperativeLive.return_after_disbanded.__get__(workflow)
            workflow.choose.side_effect=[RoomDisbanded('成员退出'),(identity(),frames['room'])]
            workflow.device.tap.side_effect=lambda *_:self.assertEqual(workflow.completed_rounds,0)
            workflow.device.back.side_effect=lambda:self.assertEqual(workflow.completed_rounds,0)
            with patch('project_sekai.cooperative_live.time.monotonic',side_effect=lambda:clock.now):
                reports=self.execute(workflow)
            self.assertEqual(workflow.device.tap.call_count,1)
            self.assertEqual(workflow.device.back.call_count,1)
            self.assertEqual(workflow.choose.call_count,2)
            self.assertEqual(workflow.completed_rounds,1)
            self.assertEqual(len(reports[0]['attempts']),2)

    def test_disbanded_ok_evidence_encoding_transition_never_clicks_underlying_page(self):
        for destination in ['final','playing']:
            with self.subTest(destination=destination):
                workflow,clock,frames=self.ui_workflow(['dialog',destination])
                state='dialog'
                def screenshot():
                    clock.advance(.04)
                    return frames[state]
                def transition(*_):
                    nonlocal state
                    state=destination
                workflow.screenshot=screenshot
                with tempfile.TemporaryDirectory() as directory,patch('project_sekai.cooperative_live.time.monotonic',side_effect=lambda:clock.now),patch('project_sekai.cooperative_live.write_image',side_effect=transition):
                    with self.assertRaises(RuntimeError if destination=='playing' else TimeoutError):
                        workflow.return_after_disbanded({},Path(directory))
                workflow.device.tap.assert_not_called()
                workflow.device.back.assert_not_called()

    def test_105_notice_over_room_card_is_closed_before_room_return_is_accepted(self):
        workflow, clock, frames = self.ui_workflow(["disbanded", "room"])
        original_match = workflow.navigator.match.side_effect
        dismissed = False

        def match(frame, page, *args):
            if frame is frames["disbanded"] and page == "cooperative_room":
                return 1.0, (0, 0)
            return original_match(frame, page, *args)

        def screenshot():
            clock.advance(.04)
            return frames["room" if dismissed else "disbanded"]

        def dismiss(*_):
            nonlocal dismissed
            dismissed = True

        # 房间卡片可能先在横幅后方出现，不能把底层卡片当作恢复完成。
        workflow.navigator.match.side_effect = match
        workflow.screenshot = screenshot
        workflow.device.tap.side_effect = dismiss
        report = {}
        with tempfile.TemporaryDirectory() as directory, \
                patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now):
            workflow.return_after_disbanded(report, Path(directory))
        workflow.device.tap.assert_called_once_with(640, 360)
        self.assertTrue(report["room_recovery"]["returned_to_room"])

    def test_105_notice_changing_to_playing_before_input_is_not_dismissed(self):
        workflow, clock, _ = self.ui_workflow(["disbanded", "playing"])
        with tempfile.TemporaryDirectory() as directory, \
                patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now), \
                self.assertRaisesRegex(RuntimeError, "进入演奏"):
            workflow.return_after_disbanded({}, Path(directory))
        workflow.device.tap.assert_not_called()

    def test_stopped_105_notice_does_not_send_dismissal(self):
        workflow, clock, _ = self.ui_workflow(["disbanded", "disbanded"])
        workflow.navigator._check_stop.side_effect = InterruptedError("用户停止")
        with tempfile.TemporaryDirectory() as directory, \
                patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now), \
                self.assertRaises(InterruptedError):
            workflow.return_after_disbanded({}, Path(directory))
        workflow.device.tap.assert_not_called()

    def test_105_notice_is_guarded_in_song_vote_ready_and_loading_phases(self):
        workflow, _, frames = self.ui_workflow(["disbanded"])
        for page in ("cooperative_select", "cooperative_ready", "cooperative_cancel"):
            with self.subTest(page=page), self.assertRaises(RoomDisbanded):
                workflow.wait_page(page, 5)
        with self.assertRaises(RoomDisbanded):
            workflow.tap_page("cooperative_select", 908, 536, "随机选曲")
        with self.assertRaises(RoomDisbanded):
            workflow.submit_ready(None, {})
        with patch("project_sekai.cooperative_live.SoloLive.start") as start:
            workflow.start(None, None, {}, Path("unused"))
        with self.assertRaises(RoomDisbanded):
            start.call_args.kwargs["frame_guard"](frames["disbanded"])
        workflow.device.tap.assert_not_called()
        workflow.navigator.tap.assert_not_called()

    def test_room_dialog_disappearing_during_log_never_clicks_underlying_page(self):
        workflow, clock, _ = self.ui_workflow(["dialog", "room"])
        report = {}
        with tempfile.TemporaryDirectory() as directory, \
                patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now):
            workflow.return_after_disbanded(report, Path(directory))
        workflow.device.tap.assert_not_called()
        self.assertTrue(report["room_recovery"]["returned_to_room"])

    def test_room_dialog_without_confirmed_ok_times_out_without_input(self):
        workflow, clock, _ = self.ui_workflow(["dialog_no_ok"])
        with tempfile.TemporaryDirectory() as directory, \
                patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now), \
                self.assertRaisesRegex(TimeoutError, "未返回"):
            workflow.return_after_disbanded({}, Path(directory))
        self.assertLess(clock.now, 21.3)
        workflow.device.tap.assert_not_called()

    def test_stopped_room_dialog_does_not_send_confirmation(self):
        workflow, clock, _ = self.ui_workflow(["dialog", "dialog"])
        workflow.navigator._check_stop.side_effect = InterruptedError("用户停止")
        with tempfile.TemporaryDirectory() as directory, \
                patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now), \
                self.assertRaises(InterruptedError):
            workflow.return_after_disbanded({}, Path(directory))
        workflow.device.tap.assert_not_called()
        workflow.device.tap.assert_not_called()
        workflow.navigator.tap.assert_not_called()

    def test_stale_page_prevents_voting_or_ready_input(self):
        workflow, _, _ = self.ui_workflow(["waiting"])
        with self.assertRaisesRegex(RuntimeError, "过期"):
            workflow.tap_page("cooperative_select", 1070, 543, "当前歌曲")
        workflow.navigator.tap.assert_not_called()
        workflow.matcher.match.assert_not_called()

    def test_two_song_modes_only_submit_once_and_actual_song_comes_from_prepare(self):
        for mode, vote in [("current", (1070, 543)), ("random", (908, 536))]:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                workflow, clock, _ = self.ui_workflow(["room", "room", "select", "select", "ready", "ready", "ready", "ready"])
                workflow.current_directory = Path(directory)
                workflow.ocr.read.return_value = Reading("軽量", .99)
                actual = identity(520)
                workflow.matcher.match.return_value = actual
                with patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now):
                    result, _ = workflow.choose("free", "easy", mode, {}, Path(directory))
                self.assertEqual(result.song_id, 520)
                points = [call.args[:2] for call in workflow.navigator.tap.call_args_list]
                self.assertEqual(points.count(vote), 1)
                self.assertNotIn((1013, 563), points)

    def test_wrong_song_at_last_ready_check_prevents_submitting(self):
        workflow, _, _ = self.ui_workflow(["ready"])
        workflow.matcher.match.return_value = identity(90)
        with self.assertRaisesRegex(RuntimeError, "歌曲已改变"):
            workflow.submit_ready(identity(520), {})
        workflow.navigator.tap.assert_not_called()

    def test_ready_disappearance_does_not_retry_on_cancel_button(self):
        workflow, _, _ = self.ui_workflow(["cancel"])
        with self.assertRaisesRegex(RuntimeError, "重复"):
            workflow.submit_ready(identity(), {})
        workflow.navigator.tap.assert_not_called()

    def test_unknown_title_missing_from_library_requires_two_readings_before_home_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, clock, _ = self.ui_workflow(["room", "room", "select", "select", "ready", "ready", "ready", "ready"])
            workflow.current_directory = Path(directory)
            workflow.ocr.read.side_effect = [Reading("軽量", .99), Reading("ラビットホール", .99), Reading("ラビットホール", .99)]
            workflow.repository = SimpleNamespace(songs={520: {"title": "透明エレジー"}})
            workflow.matcher.match.side_effect = ValueError("歌曲封面身份不明确：最佳=0.5")
            report = {}
            with patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now):
                selected, _ = workflow.choose("free", "easy", "current", report, Path(directory))
            self.assertIsNone(selected)
            workflow.device.home.assert_not_called()
            workflow.device.controller.post_touch_down.assert_not_called()

    def test_cooperative_title_is_an_independent_opportunity_when_cover_is_ambiguous(self):
        matcher = SongMatcher.__new__(SongMatcher)
        matcher.ids = [520, 90]
        matcher.features = np.array([[.514, 0], [.325, 0]])
        matcher.repository = SimpleNamespace(songs={
            520: {"title": "透明エレジー", "charts": {"easy": {"play_level": 6}}},
            90: {"title": "限りなく灰色へ", "charts": {"easy": {"play_level": 5}}}})
        matcher.ocr = Mock()
        matcher.ocr.read.return_value = Reading("透明エレジー", .99)
        frame = np.zeros((720, 1280, 3), np.uint8)
        cv2.putText(frame, "TITLE", (844, 370), cv2.FONT_HERSHEY_SIMPLEX, .7, (255, 255, 255), 2)
        with patch("project_sekai.song_identity.thumbnail", return_value=np.array([1, 0])), \
                patch("project_sekai.song_identity.cooperative_difficulty_selected", return_value=True):
            result = matcher.match(frame, "easy", "cooperative_prepare")
        self.assertEqual(result.song_id, 520)
        matcher.ocr.read.assert_called()

    def test_unconfirmed_preparation_can_submit_once_then_use_opening_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = self.run_workflow(Path(directory))
            workflow.choose.return_value = (None, np.zeros((720, 1280, 3), np.uint8))

            def final_stage(chart, prepared, report, directory, *, on_final_identity):
                self.assertIsNone(chart)
                self.assertIsNone(prepared)
                resolved = on_final_identity(identity())
                self.assertEqual(resolved.duration, 1)
                return 0

            workflow.start.side_effect = final_stage
            self.execute(workflow)
            self.assertEqual(workflow.last_report["resolved_identity"]["song_id"], 520)
            workflow.repository.load_chart.assert_called_once_with(520, "easy")
            workflow.play.assert_called_once()
            self.assertEqual(workflow.completed_rounds, 1)

    def test_unconfirmed_ready_does_not_block_the_second_identity_stage(self):
        workflow, _, _ = self.ui_workflow(["ready"])
        workflow.matcher.match.side_effect = ValueError("封面与标题均未确认")
        report = {"requested_difficulty": "easy"}
        with patch("project_sekai.cooperative_live.cooperative_difficulty_selected", return_value=True):
            workflow.submit_ready(None, report)
        workflow.navigator.tap.assert_called_once_with(1027, 591, "协力准备完成")
        workflow.device.controller.post_touch_down.assert_not_called()

    def test_real_two_stage_flow_preserves_both_channels_until_the_opening_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            states = ["room", "room", "select", "select"] + ["ready"] * 5 + ["final", "playing"]
            workflow, clock, _ = self.ui_workflow(states)
            workflow.current_directory = Path(directory)
            workflow.ocr.read.return_value = Reading("軽量", .99)

            def match(frame, difficulty, phase):
                workflow.matcher.last_evidence = {"phase": phase, "cover": {"status": "ambiguous"},
                                                 "title": {"status": "unconfirmed"}}
                if phase == "cooperative_prepare":
                    raise ValueError("封面与标题均未确认")
                workflow.matcher.last_evidence["title"]["status"] = "matched"
                return identity()

            workflow.matcher.match.side_effect = match
            report = {"requested_difficulty": "easy"}
            chart = SimpleNamespace(first=SimpleNamespace(start=1), gestures=[])
            prepare = Mock(return_value=chart)
            with patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now), \
                    patch("project_sekai.cooperative_live.cooperative_difficulty_selected", return_value=True), \
                    patch("project_sekai.solo_live.StartAnchor") as anchor:
                anchor.return_value.observe.return_value = 123
                anchor.return_value.samples = []
                anchor.return_value.fit = {}
                selected, _ = workflow.choose("free", "easy", "current", report, Path(directory))
                self.assertIsNone(selected)
                self.assertEqual(workflow.start(None, selected, report, Path(directory), on_final_identity=prepare), 123)
            for stage in ["preparation", "opening"]:
                self.assertIn("cover", report["identity_checks"][stage][-1])
                self.assertIn("title", report["identity_checks"][stage][-1])
            self.assertEqual(report["final_identity"]["song_id"], 520)
            prepare.assert_called_once()
            self.assertEqual([call.args[:2] for call in workflow.navigator.tap.call_args_list].count((1027, 591)), 1)

    def test_both_identity_stages_fail_then_home_without_loading_or_playing_a_chart(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = self.run_workflow(Path(directory))
            ui, _, _ = self.ui_workflow(["ready", "final", "playing"])
            workflow.screenshot, workflow.pause, workflow.navigator, workflow.matcher = ui.screenshot, ui.pause, ui.navigator, ui.matcher
            evidence = {"cover": {"status": "ambiguous"}, "title": {"status": "unconfirmed"}}
            workflow.matcher.last_evidence = evidence
            workflow.matcher.match.side_effect = ValueError("封面与标题均未确认")

            def choose(room, difficulty, mode, report, directory):
                workflow.matched = True
                report["identity_checks"] = {"preparation": [evidence]}
                return None, np.zeros((720, 1280, 3), np.uint8)

            workflow.choose.side_effect = choose
            workflow.start = lambda *args, **kwargs: CooperativeLive.start(workflow, *args, **kwargs)
            with patch("project_sekai.cooperative_live.cooperative_difficulty_selected", return_value=True), \
                    self.assertRaises(InterruptedError):
                self.execute(workflow)
            workflow.device.home.assert_not_called()
            workflow.repository.load_chart.assert_not_called()
            workflow.play.assert_not_called()
            self.assertIn("两个识别阶段", workflow.last_report["error"])
            self.assertIn("opening", workflow.last_report["identity_checks"])

    def test_second_stage_missing_chart_is_not_swallowed_as_an_ocr_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, _, _ = self.ui_workflow(["ready", "final"])
            workflow.matcher.match.side_effect = [ValueError("第一阶段未确认"), identity()]
            prepare = Mock(side_effect=MissingCooperativeChart("SHA256 校验失败"))
            with patch("project_sekai.cooperative_live.cooperative_difficulty_selected", return_value=True), \
                    self.assertRaises(MissingCooperativeChart):
                workflow.start(None, None, {"requested_difficulty": "easy"}, Path(directory), on_final_identity=prepare)
            prepare.assert_called_once()
            workflow.device.controller.post_touch_down.assert_not_called()

    def test_loading_disband_and_wrong_final_song_fail_before_gameplay(self):
        with tempfile.TemporaryDirectory() as directory:
            for destination in ["disbanded", "final"]:
                workflow, clock, _ = self.ui_workflow(["ready", destination])
                workflow.matcher.match.side_effect = [identity(), identity(90)]
                chart = SimpleNamespace(first=SimpleNamespace(start=1.0), gestures=[])
                expected = RoomDisbanded if destination == "disbanded" else RuntimeError
                with self.assertRaises(expected):
                    workflow.start(chart, identity(), {}, Path(directory))
                self.assertEqual(workflow.navigator.tap.call_count, 1)
                self.assertEqual(workflow.navigator.tap.call_args.args[:2], (1027, 591))
                workflow.device.controller.post_touch_down.assert_not_called()

    def test_life_zero_requires_two_gameplay_frames_with_empty_bar_and_read_zero(self):
        workflow, _, _ = self.ui_workflow(["playing", "playing"])
        workflow.life_guard, workflow.life_frames, workflow.life_epoch = LifeGuard(), [], 0
        workflow.last_report = {}
        workflow.read_optional_number = Mock(return_value=0)
        with tempfile.TemporaryDirectory() as directory, patch(
                'project_sekai.cooperative_live.time.perf_counter', side_effect=[0,2]):
            workflow.observe_play_state(Path(directory))
            self.assertFalse(workflow.life_guard.zero_confirmed)
            with self.assertRaises(LifeDepleted):
                workflow.observe_play_state(Path(directory))
        self.assertTrue(workflow.life_guard.zero_confirmed)

    def observed_workflow(self, states):
        workflow, clock, frames = self.ui_workflow(states)
        original = workflow.navigator.match.side_effect
        workflow.navigator.templates['life_hud'] = np.ones((2,2,3),np.uint8)
        def match(frame, name, *args):
            if name == 'life_hud' and frame is frames.get('life_only'):
                return .986, (0,0)
            if name == 'playing' and frame is frames.get('life_only'):
                return .812, (0,0)
            return original(frame,name,*args)
        workflow.navigator.match.side_effect = match
        workflow.life_guard,workflow.life_frames,workflow.life_epoch = LifeGuard(),[],0
        workflow.read_optional_number = Mock(return_value=1000)
        workflow.current_report = workflow.last_report = {}
        return workflow,clock,frames

    def test_life_label_keeps_playing_when_pause_score_is_below_threshold(self):
        workflow, _, _ = self.observed_workflow(['life_only'])
        with tempfile.TemporaryDirectory() as directory:
            workflow.observe_play_state(Path(directory))
        self.assertEqual(workflow.life_guard.samples,1)
        self.assertTrue(workflow.life_guard.hud_seen)
        workflow.device.back.assert_not_called()
        workflow.device.home.assert_not_called()

    def test_low_frequency_samples_keep_middle_song_evidence_without_writing_during_playback(self):
        workflow, _, _ = self.observed_workflow(['playing'])
        with tempfile.TemporaryDirectory() as directory, patch(
                'project_sekai.cooperative_live.time.perf_counter', side_effect=range(0,26,2)), patch(
                'project_sekai.cooperative_live.write_image') as write:
            for _ in range(13):
                workflow.observe_play_state(Path(directory))
            write.assert_not_called()
            self.assertEqual(workflow.life_guard.samples,13)
            workflow.save_evidence(workflow.last_report,Path(directory))
        samples = workflow.last_report['life_samples']
        self.assertEqual(len(samples),13)
        self.assertEqual(samples[-1]['elapsed_s'],24)
        recent = workflow.last_report['life_recent_frames']
        self.assertEqual(len(recent),8)
        self.assertEqual([frame['elapsed_s'] for frame in recent],list(range(10,26,2)))
        self.assertFalse(workflow.life_recent_frames)
        self.assertFalse(workflow.life_sample_images)

    def test_late_zero_saves_both_confirmations_and_their_chart_times(self):
        workflow, _, _ = self.observed_workflow(['playing','playing'])
        workflow.read_optional_number.return_value = 0
        with tempfile.TemporaryDirectory() as directory, patch(
                'project_sekai.cooperative_live.time.perf_counter', side_effect=[100,102]), patch(
                'project_sekai.cooperative_live.write_image') as write:
            workflow.observe_play_state(Path(directory))
            with self.assertRaises(LifeDepleted):
                workflow.observe_play_state(Path(directory))
            write.assert_not_called()
            workflow.save_evidence(workflow.last_report,Path(directory))
        self.assertEqual(workflow.last_report['life_zero_elapsed_s'],102)
        self.assertEqual([frame['elapsed_s'] for frame in workflow.last_report['life_recent_frames']],[100,102])
        self.assertEqual([sample['zero_streak'] for sample in workflow.last_report['life_samples']],[1,2])

    def test_one_missing_frame_then_life_label_does_not_interrupt_playing(self):
        workflow, _, _ = self.observed_workflow(['unknown','life_only','unknown','life_only'])
        with tempfile.TemporaryDirectory() as directory, patch(
                'project_sekai.cooperative_live.time.perf_counter', side_effect=[0,2,4,6]):
            for _ in range(4):
                workflow.observe_play_state(Path(directory))
        self.assertEqual(workflow.life_guard.samples,2)

    def test_repeated_unknown_frames_do_not_prove_that_cooperative_playing_has_ended(self):
        workflow, _, _ = self.observed_workflow(['unknown','unknown','unknown','life_only'])
        with tempfile.TemporaryDirectory() as directory, patch(
                'project_sekai.cooperative_live.time.perf_counter', side_effect=[0,2,4,6]):
            for _ in range(4):
                workflow.observe_play_state(Path(directory))
        self.assertFalse(workflow.life_guard.zero_confirmed)
        workflow.device.home.assert_not_called()

    def test_cooperative_sampling_does_not_capture_in_a_high_frequency_burst(self):
        workflow, _, _ = self.observed_workflow(['life_only'])
        workflow.screenshot = Mock(wraps=workflow.screenshot)
        with tempfile.TemporaryDirectory() as directory, patch(
                'project_sekai.cooperative_live.time.perf_counter', side_effect=[index / 10 for index in range(10)]):
            for _ in range(10):
                workflow.observe_play_state(Path(directory))
        self.assertEqual(workflow.screenshot.call_count,1)

    def test_a_pause_button_without_a_life_label_does_not_validate_zero_life_ocr(self):
        workflow, _, _ = self.observed_workflow(['unknown'])
        workflow.navigator.match.side_effect = lambda frame,name,*args: ((1.0,(0,0)) if name == 'playing' else (0.0,(0,0)))
        workflow.read_optional_number.return_value = 0
        with tempfile.TemporaryDirectory() as directory, patch(
                'project_sekai.cooperative_live.time.perf_counter', side_effect=[0,2]):
            for _ in range(2):
                workflow.observe_play_state(Path(directory))
        self.assertFalse(workflow.life_guard.zero_confirmed)
        workflow.read_optional_number.assert_not_called()

    def test_different_non_gameplay_pages_are_not_a_stable_abnormal_confirmation(self):
        workflow, _, _ = self.observed_workflow(['room','personal','life_only'])
        with tempfile.TemporaryDirectory() as directory, patch(
                'project_sekai.cooperative_live.time.perf_counter', side_effect=[0,2,4]):
            for _ in range(3):
                workflow.observe_play_state(Path(directory))
        self.assertFalse(workflow.life_guard.zero_confirmed)

    def test_same_explicit_non_gameplay_page_requires_two_spaced_observations(self):
        workflow, _, _ = self.observed_workflow(['room'])
        with tempfile.TemporaryDirectory() as directory, patch(
                'project_sekai.cooperative_live.time.perf_counter', side_effect=[0,1,2]):
            workflow.observe_play_state(Path(directory))
            workflow.observe_play_state(Path(directory))
            with self.assertRaisesRegex(RuntimeError,'协力演奏期间连续确认'):
                workflow.observe_play_state(Path(directory))
        self.assertFalse(workflow.life_guard.zero_confirmed)

    def test_black_transition_breaks_the_missing_frame_streak(self):
        workflow, _, _ = self.observed_workflow(['unknown','black','unknown','life_only'])
        with tempfile.TemporaryDirectory() as directory, patch(
                'project_sekai.cooperative_live.time.perf_counter', side_effect=[0,2,4,6]):
            for _ in range(4):
                workflow.observe_play_state(Path(directory))
        self.assertEqual(workflow.life_guard.samples,1)

    def test_zero_life_still_requires_two_confirmations_with_pause_mismatch(self):
        workflow, _, _ = self.observed_workflow(['life_only','life_only'])
        workflow.read_optional_number.return_value = 0
        with tempfile.TemporaryDirectory() as directory, patch(
                'project_sekai.cooperative_live.time.perf_counter', side_effect=[0,2]):
            workflow.observe_play_state(Path(directory))
            with self.assertRaises(LifeDepleted):
                workflow.observe_play_state(Path(directory))
        self.assertTrue(workflow.life_guard.zero_confirmed)

    def test_zero_life_template_uses_two_spaced_frames_without_numeric_ocr(self):
        workflow,_,frames = self.observed_workflow(['playing'])
        value = np.zeros((29,97,3),np.uint8)
        cv2.putText(value,'0',(78,23),cv2.FONT_HERSHEY_SIMPLEX,.7,(255,255,255),2)
        workflow.navigator.zero_life_template = ZeroLifeTemplate(value)
        frames['playing'][16:45,1090:1187] = value
        workflow.read_optional_number.side_effect = AssertionError('零模板可用时禁止调用 OCR')
        with tempfile.TemporaryDirectory() as directory, patch(
                'project_sekai.cooperative_live.time.perf_counter',side_effect=[0,2]):
            workflow.observe_play_state(Path(directory))
            with self.assertRaises(LifeDepleted):
                workflow.observe_play_state(Path(directory))
        workflow.read_optional_number.assert_not_called()

    def test_template_rejects_a_trailing_zero_even_if_ocr_would_misread_it(self):
        workflow,_,frames = self.observed_workflow(['playing'])
        value = np.zeros((29,97,3),np.uint8)
        cv2.putText(value,'0',(78,23),cv2.FONT_HERSHEY_SIMPLEX,.7,(255,255,255),2)
        workflow.navigator.zero_life_template = ZeroLifeTemplate(value)
        value[:] = 0
        cv2.putText(value,'1000',(26,23),cv2.FONT_HERSHEY_SIMPLEX,.7,(255,255,255),2)
        frames['playing'][16:45,1090:1187] = value
        workflow.read_optional_number.return_value = 0
        with tempfile.TemporaryDirectory() as directory, patch(
                'project_sekai.cooperative_live.time.perf_counter',side_effect=[0,2]):
            for _ in range(2):
                workflow.observe_play_state(Path(directory))
        self.assertFalse(workflow.life_guard.zero_confirmed)
        workflow.read_optional_number.assert_not_called()

    def test_solo_observer_also_accepts_life_label_when_pause_score_drops(self):
        from project_sekai.solo_live import SoloLive
        workflow, _, _ = self.observed_workflow(['life_only'])
        with tempfile.TemporaryDirectory() as directory:
            SoloLive.observe_play_state(workflow,Path(directory))
        self.assertEqual(workflow.life_guard.samples,1)
        self.assertTrue(workflow.life_guard.hud_seen)

    def test_solo_confirmed_live_failed_still_stops_on_the_first_missing_frame(self):
        from project_sekai.solo_live import SoloLive
        workflow, _, _ = self.observed_workflow(['unknown'])
        original = workflow.navigator.match.side_effect
        workflow.navigator.match.side_effect = lambda frame,name,*args: (
            (1.0,(0,0)) if name == 'live_failed' else original(frame,name,*args))
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(LifeDepleted):
            SoloLive.observe_play_state(workflow,Path(directory))
        self.assertTrue(workflow.life_guard.zero_confirmed)

    def test_partial_result_preserves_available_numbers_without_filling_missing_zero(self):
        workflow, _, frames = self.ui_workflow(["personal"])
        workflow.current_report = {"requested_difficulty": "easy", "preparation_identity": {"song_id": 520}}
        workflow.matcher.match.return_value = identity()
        workflow.read_judgement_number = Mock(side_effect=[278, 11, ValueError("未读出"), 0, 0])
        self.assertIsNone(workflow.read_result(frames["personal"]))
        partial = workflow.current_report["partial_judgements"]
        self.assertEqual(partial["perfect"], 278)
        self.assertIsNone(partial["good"])
        self.assertFalse(partial["complete"])

    def test_team_score_page_is_never_read_as_personal_result(self):
        workflow, _, frames = self.ui_workflow(["team"])
        workflow.read_judgement_number = Mock()
        self.assertIsNone(workflow.read_result(frames["team"]))
        workflow.read_judgement_number.assert_not_called()

    def test_personal_result_for_another_song_is_not_read(self):
        workflow, _, frames = self.ui_workflow(["personal"])
        workflow.current_report = {"requested_difficulty": "easy", "preparation_identity": {"song_id": 520}}
        workflow.matcher.match.return_value = identity(90)
        workflow.read_judgement_number = Mock()
        self.assertIsNone(workflow.read_result(frames["personal"]))
        workflow.read_judgement_number.assert_not_called()
        self.assertIn("不一致", workflow.current_report["result_identity_error"])

    def collection(self, expected=290):
        workflow, clock, _ = self.ui_workflow(["clear", "personal", "personal", "home", "home"])
        workflow.read_result = Mock(side_effect=[LiveResult(278, 11, 1, 0, 0, late=7, fast=5),
                                               LiveResult(278, 11, 1, 0, 0, late=8, fast=5)])
        report = {"chart": {"total_note_count": expected}, "completed": False,
                  "playback":{"planned_actions":2,"sent_actions":2,"release_confirmed":True}}
        return workflow, clock, report

    def test_two_full_readings_are_saved_before_back_and_optional_conflicts_are_empty(self):
        workflow, clock, report = self.collection()
        with tempfile.TemporaryDirectory() as directory, \
                patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now):
            workflow.collect(report, Path(directory))
            saved = json.loads((Path(directory) / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["judgements"]["total"], 290)
            self.assertIsNone(saved["judgements"]["late"])
            self.assertEqual(saved["judgements"]["fast"], 5)
        self.assertTrue(report["returned_home"])
        workflow.device.back.assert_not_called()

    def test_wrong_note_total_keeps_real_numbers_but_does_not_complete(self):
        workflow, clock, report = self.collection(291)
        with tempfile.TemporaryDirectory() as directory, \
                patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now):
            workflow.collect(report, Path(directory))
        self.assertEqual(report["judgements"]["total"], 290)
        self.assertFalse(report["judgements"]["total_matches_chart"])
        self.assertFalse(report["completed"])
        self.assertTrue(report["returned_home"])
        self.assertIn("音数", report["settlement_warning"])

    def test_missing_clear_does_not_press_back_on_unknown_or_room_page(self):
        workflow, clock, _ = self.ui_workflow(["room"])
        with tempfile.TemporaryDirectory() as directory, \
                patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now), \
                self.assertRaises(TimeoutError):
            workflow.collect({"chart": {"total_note_count": 290}}, Path(directory))
        workflow.device.back.assert_not_called()
        workflow.device.tap.assert_not_called()

    def test_full_input_and_repeated_unknown_frames_still_do_not_allow_settlement_back(self):
        workflow,clock,_ = self.ui_workflow(['unknown'])
        workflow.life_guard = SimpleNamespace(hud_seen=True)
        report = {'chart':{'total_note_count':290},
                  'playback':{'planned_actions':2,'sent_actions':2,'release_confirmed':True}}
        with tempfile.TemporaryDirectory() as directory, patch(
                'project_sekai.cooperative_live.time.monotonic',side_effect=lambda:clock.now), self.assertRaises(TimeoutError):
            workflow.collect(report,Path(directory))
        workflow.device.back.assert_not_called()
        workflow.device.tap.assert_not_called()
        self.assertNotIn('end_detection',report)

    def test_single_failed_match_cannot_stop_a_completed_round_with_real_personal_results(self):
        workflow,clock,_ = self.ui_workflow(['failed','personal','personal','home','home'])
        workflow.read_result = Mock(return_value=LiveResult(278,11,1,0,0))
        report = {'chart':{'total_note_count':290},
                  'playback':{'planned_actions':2,'sent_actions':2,'release_confirmed':True}}
        with tempfile.TemporaryDirectory() as directory, patch(
                'project_sekai.cooperative_live.time.monotonic',side_effect=lambda:clock.now):
            workflow.collect(report,Path(directory))
        self.assertTrue(report['completed'])
        self.assertEqual(report['live_status'],'ended')
        workflow.device.home.assert_not_called()

    def test_actual_failed_page_requires_two_frames_before_failure_exit(self):
        workflow,clock,_ = self.ui_workflow(['failed','failed'])
        report = {'chart':{'total_note_count':290},
                  'playback':{'planned_actions':2,'sent_actions':2,'release_confirmed':True}}
        with tempfile.TemporaryDirectory() as directory, patch(
                'project_sekai.cooperative_live.time.monotonic',side_effect=lambda:clock.now), self.assertRaises(LifeDepleted):
            workflow.collect(report,Path(directory))
        self.assertEqual(report['live_status'],'failed')
        workflow.device.back.assert_not_called()

    def test_unreadable_personal_page_advances_after_bounded_wait_and_keeps_partial_report(self):
        workflow, clock, _ = self.ui_workflow(["clear"] + ["personal"] * 40 + ["home", "home"])
        report = {"chart": {"total_note_count": 290}, "partial_judgements": {"perfect": 278, "good": None},
                  "playback":{"planned_actions":2,"sent_actions":2,"release_confirmed":True}}
        workflow.read_result = Mock(return_value=None)
        with tempfile.TemporaryDirectory() as directory, \
                patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now):
            workflow.collect(report, Path(directory))
        self.assertLess(clock.now, 20)
        self.assertTrue(report["returned_home"])
        self.assertFalse(report["completed"])
        self.assertEqual(report["result_status"], "incomplete")
        self.assertEqual(report["partial_judgements"]["perfect"], 278)
        self.assertGreater(workflow.device.back.call_count, 0)

    def test_finished_input_waits_through_black_and_requires_a_real_settlement_page(self):
        workflow, clock, _ = self.ui_workflow(["black", "black", "playing", "settlement", "personal",
                                             "personal", "home", "home"])
        workflow.navigator.templates = {"life_hud": None}
        workflow.life_guard = SimpleNamespace(hud_seen=True)
        workflow.read_result = Mock(return_value=LiveResult(278, 11, 1, 0, 0))
        report = {"engine": "legacy", "chart": {"total_note_count": 290}, "completed": False,
                  "playback": {"planned_actions": 2, "sent_actions": 2, "release_confirmed": True}}
        with tempfile.TemporaryDirectory() as directory, \
                patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now):
            workflow.collect(report, Path(directory))
        self.assertTrue(report["returned_home"])
        self.assertTrue(report["completed"])
        self.assertEqual(report["live_status"], "ended")
        self.assertEqual(report["end_detection"]["method"], "cooperative_settlement_confirmed")
        self.assertTrue(report["judgements"]["total_matches_chart"])

    def test_incomplete_result_keeps_report_and_continues_the_next_round(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = self.run_workflow(Path(directory))

            def collect(report, _):
                report.update(returned_home=True, live_status="ended", completed=report["round"] != 1)
                if report["round"] == 1:
                    report.update(result_status="incomplete", settlement_warning="个人判定未完整读取")
                else:
                    report.update(result_status="recorded",
                                  judgements={**LiveResult(278, 11, 1, 0, 0).to_dict(), "total_matches_chart": True})

            workflow.collect.side_effect = collect
            reports = self.execute(workflow, count=2)
            self.assertEqual(workflow.play.call_count, 2)
            self.assertEqual(workflow.completed_rounds, 1)
            self.assertFalse(reports[0]["completed"])
            self.assertEqual(reports[0]["result_status"], "incomplete")
            self.assertTrue(reports[1]["completed"])
            with (Path(directory) / "results.csv").open(encoding="utf-8-sig") as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 2)

    def run_workflow(self, root):
        workflow = CooperativeLive.__new__(CooperativeLive)
        workflow.report_root = root
        workflow.stop_requested = lambda: False
        workflow.device = Mock()
        workflow.device.screenshot.return_value = np.zeros((720, 1280, 3), np.uint8)
        workflow.device.shell.side_effect = ["com.android.launcher3/.Launcher\n",
                                            "mCurrentFocus=Window{123 u0 com.android.launcher3/.Launcher}\n"]
        workflow.navigator = Mock(threshold=.83)
        workflow.completed_rounds = 0
        workflow.log = Mock()
        workflow.open_rooms = Mock()
        workflow.recover_runtime_failure = Mock(side_effect=InterruptedError("测试取消恢复等待"))
        workflow.pause = Mock(side_effect=InterruptedError("测试取消等待"))
        workflow.prepare_bonus = Mock(side_effect=lambda requested, report, *args, **kwargs: report.update(bonus={
            'requested_consumption': requested, 'consumption': 5, 'confirmed': True, 'original_consumption': 5,
            'readings': [{'text': '5'}, {'text': '5'}]}))
        workflow.ensure_bonus_available = Mock()
        workflow.choose = Mock(return_value=(identity(), np.zeros((720, 1280, 3), np.uint8)))
        workflow.wait_page = Mock()
        workflow.return_after_disbanded = Mock()
        workflow.start = Mock(return_value=0)
        workflow.repository = Mock()
        workflow.repository.songs = {520: {"charts": {"easy": {"music_id": 520, "difficulty": "easy", "sha256": "verified",
                                                            "path": "verified.sus", "total_note_count": 290}}}}

        def play(events, epoch, offset, report, directory):
            report["playback"] = {"release_confirmed": True, "sent_actions": 2, "planned_actions": 2}

        def collect(report, directory):
            report.update(returned_home=True, live_status="cleared", result_status="recorded",
                          judgements={**LiveResult(278, 11, 1, 0, 0).to_dict(), "total_matches_chart": True})

        workflow.play = Mock(side_effect=play)
        workflow.collect = Mock(side_effect=collect)
        return workflow

    def execute(self, workflow, count=1, **kwargs):
        with patch("project_sekai.cooperative_live.parse_sus", return_value=SimpleNamespace(duration=1)), \
                patch("project_sekai.cooperative_live.compile_touches", return_value=(1, 2)):
            return workflow.run(count, "easy", "current", **kwargs)

    def test_native_rounds_continue_through_pause_mismatch_with_complete_execution_and_release(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = self.run_workflow(Path(directory))
            observed, _, _ = self.observed_workflow(['life_only'])
            workflow.navigator = observed.navigator
            workflow.screenshot = observed.screenshot
            workflow.read_optional_number = observed.read_optional_number
            players = []
            def native(controller,events,run_directory,stop_requested,*,idle_observer,observation_interval,observation_budget):
                self.assertEqual(observation_interval,2.0)
                self.assertEqual(observation_budget,.30)
                player = Mock()
                player.report = {'planned_actions':2,'sent_actions':0,'executed_actions':0,'release_confirmed':False}
                def play(epoch,offset):
                    idle_observer()
                    player.report.update(sent_actions=2,executed_actions=2)
                def close():
                    player.report.update(release_confirmed=True,release={
                        'reset_executed':True,'release_proof':'current-reset-jlog-and-cleanup'})
                player.play.side_effect = play
                player.close.side_effect = close
                players.append(player)
                return player
            with patch('project_sekai.native_player.NativePlayer',side_effect=native):
                reports = self.execute(workflow,count=2,engine='native')
        self.assertEqual(workflow.completed_rounds,2)
        self.assertEqual(workflow.collect.call_count,2)
        for report, player in zip(reports,players):
            self.assertTrue(report['completed'])
            self.assertNotIn('playback_error',report)
            self.assertEqual(report['playback']['executed_actions'],2)
            self.assertEqual(report['playfield_monitor']['pause_fallback_frames'],1)
            player.close.assert_called_once()
        workflow.device.home.assert_not_called()

    def stalled_run_workflow(self, root, *, first_only):
        workflow = self.run_workflow(root)
        ui, clock, frames = self.ui_workflow(["room", "matching"])
        state = {"page": "room", "joins": 0}
        workflow.navigator = ui.navigator
        workflow.pause = clock.advance

        def screenshot():
            clock.advance(.04)
            return frames[state["page"]]

        def join(*_):
            state.update(page="matching", joins=state["joins"] + 1)

        def choose(*arguments):
            if first_only and state["joins"]:
                return identity(), frames["room"]
            return CooperativeLive.choose(workflow, *arguments)

        workflow.screenshot = screenshot
        workflow.wait_page = CooperativeLive.wait_page.__get__(workflow)
        workflow.return_after_disbanded = CooperativeLive.return_after_disbanded.__get__(workflow)
        workflow.navigator.tap.side_effect = join
        workflow.device.back.side_effect = lambda *_: state.update(page="room")
        workflow.device.screenshot.side_effect = screenshot
        workflow.recover_runtime_failure = CooperativeLive.recover_runtime_failure.__get__(workflow)
        workflow.stop_requested = lambda: clock.now > 1000.
        workflow.choose = Mock(side_effect=choose)
        return workflow, clock

    def test_stalled_member_room_recovers_through_real_choose_and_completes_next_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, clock = self.stalled_run_workflow(Path(directory), first_only=True)
            with patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now):
                self.execute(workflow)
            self.assertEqual(workflow.choose.call_count, 2)
            workflow.device.tap.assert_not_called()
            workflow.device.back.assert_called_once()
            workflow.device.home.assert_not_called()
            self.assertEqual(workflow.completed_rounds, 1)
            self.assertEqual([item["status"] for item in workflow.last_report["attempts"]], ["cleared"])
            with (Path(directory) / "results.csv").open(encoding="utf-8-sig") as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 2)

    def test_repeated_stalled_matching_shares_three_rematches_and_never_counts_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, clock = self.stalled_run_workflow(Path(directory), first_only=False)
            with patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now):
                with self.assertRaises(InterruptedError):
                    self.execute(workflow)
            self.assertEqual(workflow.choose.call_count, 4)
            workflow.device.tap.assert_not_called()
            self.assertEqual(workflow.device.back.call_count, 4)
            self.assertEqual(workflow.completed_rounds, 0)
            workflow.play.assert_not_called()
            workflow.repository.load_chart.assert_not_called()
            workflow.device.home.assert_not_called()

    def test_actual_selected_chart_is_loaded_and_json_csv_precede_completed_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = self.run_workflow(root)

            def log(message):
                if "已完成 1" in message:
                    self.assertTrue(workflow.last_report["completed"])
                    self.assertTrue(Path(workflow.last_report["report_path"]).is_file())
                    with (root / "results.csv").open(encoding="utf-8-sig") as stream:
                        self.assertEqual(len(list(csv.DictReader(stream))), 1)

            workflow.log.side_effect = log
            self.execute(workflow)
            workflow.repository.load_chart.assert_called_once_with(520, "easy")
            self.assertEqual(workflow.completed_rounds, 1)

    def test_runtime_failures_keep_requested_round_pending_until_two_performances_finish(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow=self.run_workflow(Path(directory))
            workflow.open_rooms.side_effect=[TimeoutError('首页点击未生效'),None,None,None]
            workflow.start.side_effect=[0,RuntimeError('首音候选未确认运动'),0]
            ui,_,frames=self.ui_workflow(['home'])
            workflow.navigator=ui.navigator
            workflow.device.screenshot.return_value=frames['home']
            workflow.recover_runtime_failure=CooperativeLive.recover_runtime_failure.__get__(workflow)
            reports=self.execute(workflow,count=2)
            self.assertEqual(workflow.completed_rounds,2)
            self.assertEqual(workflow.play.call_count,2)
            self.assertEqual([report['round'] for report in reports],[1,1,2,2])
            self.assertEqual([report['runtime_recovery']['state'] for report in reports if 'runtime_recovery' in report],['ready','ready'])
            with (Path(directory)/'results.csv').open(encoding='utf-8-sig') as stream:
                self.assertEqual(len(list(csv.DictReader(stream))),4)

    def test_real_run_resumes_matching_selection_without_joining_a_new_room(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = self.run_workflow(Path(directory))
            ui, clock, frames = self.ui_workflow(['matching_decided'] * 3 + ['select'] * 4 + ['ready'] * 12)
            workflow.screenshot, workflow.pause, workflow.navigator = ui.screenshot, ui.pause, ui.navigator
            workflow.stop_requested = lambda: clock.now > 30.
            workflow.device.screenshot.side_effect = ui.screenshot
            workflow.ocr = Mock()
            workflow.ocr.read.return_value = Reading('軽量', 1.)
            workflow.matcher = Mock()
            workflow.matcher.match.return_value = identity()
            workflow.recover_runtime_failure = lambda *args: CooperativeLive.recover_runtime_failure(workflow, *args)
            workflow.wait_page = lambda *args, **kwargs: CooperativeLive.wait_page(workflow, *args, **kwargs)
            calls = 0
            def choose(*args):
                nonlocal calls
                calls += 1
                if calls == 1:
                    workflow.matched = True
                    args[3]['attempts'][-1]['room_recovery'] = {'reason': 'member_matching_timeout'}
                    args[3]['attempts'][-1]['member_wait_deadline'] = clock.now - 1.
                    raise TimeoutError('匹配退出确认时连接暂失')
                return CooperativeLive.choose(workflow, *args)
            workflow.choose.side_effect = choose
            with patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock.now):
                try:
                    self.execute(workflow)
                except InterruptedError:
                    self.fail(str(workflow.last_report))
            self.assertEqual(workflow.performed_rounds, 1)
            self.assertEqual(calls, 2)
            self.assertEqual(workflow.round_rematches, 0)
            self.assertEqual(workflow.prepare_bonus.call_count, 1)
            self.assertEqual(workflow.ensure_bonus_available.call_count, 1)
            self.assertFalse(any(call.args[:2] == (920, 245) for call in workflow.navigator.tap.call_args_list))

    def test_decided_matching_positive_label_allows_bounded_exit_but_missing_template_does_not(self):
        workflow, clock, frames = self.ui_workflow(['matching_decided', 'matching_full'])
        self.assertTrue(workflow.member_room_can_leave(frames['matching_decided']))
        self.assertFalse(workflow.member_room_can_leave(frames['matching_full']))
        workflow.navigator.templates.pop('cooperative_member_decided')
        self.assertFalse(workflow.member_room_can_leave(frames['matching_decided']))

    def test_real_recovery_run_resumes_shuffle_ready_and_cancel_without_entry_timeout(self):
        for state in ('shuffle', 'ready', 'cancel'):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                workflow = self.run_workflow(Path(directory))
                states = ([state] * 4 + ['ready'] * 12) if state == 'shuffle' else [state] * 16
                ui, clock, frames = self.ui_workflow(states)
                workflow.screenshot, workflow.pause, workflow.navigator = ui.screenshot, ui.pause, ui.navigator
                workflow.device.screenshot.side_effect = ui.screenshot
                workflow.stop_requested = lambda: clock.now > 80.
                workflow.ocr, workflow.matcher = Mock(), Mock()
                workflow.ocr.read.return_value = Reading('軽量', 1.)
                workflow.matcher.match.return_value = identity()
                entry_calls = 0
                def open_rooms():
                    nonlocal entry_calls
                    entry_calls += 1
                    if entry_calls > 1:
                        return CooperativeLive.open_rooms(workflow)
                workflow.open_rooms = open_rooms
                workflow.recover_runtime_failure = lambda *args: CooperativeLive.recover_runtime_failure(workflow, *args)
                workflow.wait_page = lambda *args, **kwargs: CooperativeLive.wait_page(workflow, *args, **kwargs)
                calls = 0
                def choose(*args):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        workflow.matched = True
                        args[3]['joined_room'] = True
                        raise OSError('房内连接回执失败')
                    return CooperativeLive.choose(workflow, *args)
                workflow.choose.side_effect = choose
                def start(chart, actual, report, target, *, on_final_identity):
                    if actual is None:
                        on_final_identity(identity())
                    report['final_identity'] = identity().to_dict()
                    return 0.
                workflow.start.side_effect = start
                with patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock.now):
                    reports = self.execute(workflow)
                self.assertEqual(workflow.performed_rounds, 1)
                self.assertEqual(calls, 2)
                self.assertEqual(entry_calls, 2)
                self.assertEqual(workflow.round_rematches, 0)
                self.assertLess(clock.now, 60.)
                self.assertEqual(workflow.prepare_bonus.call_count, 1)
                self.assertEqual(reports[-1]['bonus']['confirmation_source'], 'task_snapshot')
                self.assertEqual(reports[-1]['bonus']['source_report'], reports[0]['report_path'])
                self.assertNotIn('readings', reports[-1]['bonus'])
                self.assertFalse(any(call.args[:2] in ((920, 245), (908, 536), (1070, 543))
                                     for call in workflow.navigator.tap.call_args_list))
                self.assertTrue(reports[-1]['final_identity'])

    def test_unknown_resume_stage_cannot_skip_room_entry(self):
        workflow, clock, _ = self.ui_workflow(['black'])
        workflow.resume_stage = 'untrusted-stage'
        workflow.entry_deadline = clock.now - 1.
        with patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock.now), \
                self.assertRaisesRegex(TimeoutError, '60 秒'):
            workflow.open_rooms()
        workflow.navigator.tap.assert_not_called()

    def test_cooperative_completed_rounds_reuse_consumption_but_check_available_each_round(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = self.run_workflow(Path(directory))
            reports = self.execute(workflow, count=2)
            self.assertEqual(workflow.prepare_bonus.call_count, 1)
            self.assertEqual(workflow.ensure_bonus_available.call_count, 2)
            self.assertEqual(reports[1]['bonus']['confirmation_source'], 'task_snapshot')
            self.assertNotIn('readings', reports[1]['bonus'])
            self.execute(workflow)
            self.assertEqual(workflow.prepare_bonus.call_count, 2)

    def test_runtime_matching_exit_reaches_room_and_consumes_only_one_rematch(self):
        workflow, clock, frames = self.ui_workflow(['matching_decided', 'matching_decided', 'matching_decided', 'room', 'room'])
        workflow.device.screenshot.side_effect = workflow.screenshot
        workflow.round_rematches = 0
        workflow.max_rematches = 3
        workflow.persist_runtime_report = Mock()
        report = {'joined_room': True, 'attempts': [{'member_wait_deadline': clock.now - 1.}]}
        workflow.current_report = report
        workflow.last_report = report
        workflow.life_guard = LifeGuard(bar_area=(1008, 48, 1177, 56))
        with tempfile.TemporaryDirectory() as directory, patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock.now):
            workflow.recover_runtime_failure(report, Path(directory), TimeoutError('匹配等待已超时'))
        workflow.device.tap.assert_not_called()
        workflow.device.back.assert_called_once()
        self.assertEqual(workflow.round_rematches, 1)
        self.assertTrue(report['rematch_budget_consumed'])
        self.assertEqual(report['task_state'], 'ready')

    def test_matching_recovery_selection_resumes_same_room_without_new_join(self):
        workflow, clock, frames = self.ui_workflow(['matching_decided', 'select'])
        workflow.device.screenshot.side_effect = workflow.screenshot
        workflow.round_rematches = 1
        report = {'attempts': [{'room_recovery': {'reason': 'member_matching_timeout'}}]}
        workflow.current_report = report
        with tempfile.TemporaryDirectory() as directory, patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock.now):
            workflow.recover_runtime_failure(report, Path(directory), TimeoutError('退出前转选歌'))
        self.assertTrue(workflow.resume_selection)
        self.assertEqual(workflow.round_rematches, 1)
        workflow.device.tap.assert_not_called()
        workflow.device.back.assert_not_called()

    def test_real_run_input_failure_after_join_recovers_home_with_new_entry_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = self.run_workflow(Path(directory))
            ui, clock, frames = self.ui_workflow(['room', 'home', 'home', 'home', 'home', 'menu', 'menu', 'room', 'room'])
            original = ui.navigator.match.side_effect
            ui.navigator.match.side_effect = lambda frame, name, *args: ((1., (0, 0)) if frame is frames['menu'] and name == 'live_menu' else original(frame, name, *args))
            workflow.screenshot, workflow.pause, workflow.navigator = ui.screenshot, ui.pause, ui.navigator
            workflow.device.screenshot.side_effect = ui.screenshot
            workflow.stop_requested = lambda: clock.now > 260.
            workflow.open_rooms = lambda: CooperativeLive.open_rooms(workflow)
            workflow.recover_runtime_failure = lambda *args: CooperativeLive.recover_runtime_failure(workflow, *args)
            def choose(*args):
                workflow.matched = True
                return identity(), frames['room']
            workflow.choose.side_effect = choose
            def play(events, epoch, offset, report, target):
                report['playback'] = {'release_confirmed': True, 'planned_actions': 2, 'sent_actions': 2, 'executed_actions': 2}
                if workflow.play.call_count == 1:
                    clock.advance(200.)
                    raise OSError('输入回执连接异常')
            workflow.play.side_effect = play
            with patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock.now):
                reports = self.execute(workflow)
            self.assertEqual(workflow.performed_rounds, 1)
            self.assertEqual(len(reports), 2)
            self.assertTrue(reports[0]['rematch_budget_consumed'])
            self.assertIn('输入回执', reports[0]['error'])
            self.assertEqual(workflow.play.call_count, 2)
            workflow.device.home.assert_not_called()

    def test_resume_selection_provenance_survives_new_report_blank_failure(self):
        workflow, clock, frames = self.ui_workflow(['select'])
        workflow.resume_selection = True
        workflow.entry_deadline = -1.
        workflow.entry_request_count, workflow.menu_request_count = 2, 2
        workflow.device.screenshot.side_effect = workflow.screenshot
        report = {'attempts': [{}], 'playback': None}
        with tempfile.TemporaryDirectory() as directory:
            workflow.recover_runtime_failure(report, Path(directory), RuntimeError('原房间选歌复查遇到转场'))
        self.assertTrue(workflow.resume_selection)
        self.assertIsNone(workflow.entry_deadline)
        self.assertEqual((workflow.entry_request_count, workflow.menu_request_count), (2, 2))
        for method in ('tap', 'back', 'home'): getattr(workflow.device, method).assert_not_called()

    def test_stop_during_ordinary_recovery_backoff_updates_cancelled_json(self):
        workflow, clock, frames = self.ui_workflow(['home'])
        workflow.wait_runtime_destination = Mock(side_effect=OSError('恢复连接失败'))
        workflow.check_runtime_stop = Mock(side_effect=InterruptedError('退避前用户停止'))
        report = {'error': 'TimeoutError: 原房间失败', 'cancelled': False}
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            with self.assertRaises(InterruptedError):
                workflow.recover_runtime_failure(report, target, RuntimeError('原房间失败'))
            saved = json.loads((target / 'report.json').read_text(encoding='utf-8'))
            self.assertTrue(saved['cancelled'])
            self.assertIn('stopped_at', saved)
            self.assertEqual(saved['error'], 'TimeoutError: 原房间失败')
        self.assertTrue(report['cancelled'])
        self.assertEqual(report['error'], 'TimeoutError: 原房间失败')
        self.assertIn('stopped_at', report)
        for method in ('tap', 'back', 'home'): getattr(workflow.device, method).assert_not_called()

    def test_runtime_fresh_matching_confirms_join_after_failed_ack_and_counts_rematch(self):
        workflow, clock, frames = self.ui_workflow(['matching', 'matching', 'matching', 'room', 'room'])
        workflow.device.screenshot.side_effect = workflow.screenshot
        workflow.life_guard = LifeGuard(bar_area=(1008, 48, 1177, 56))
        workflow.persist_runtime_report = Mock()
        workflow.current_report = workflow.last_report = report = {'attempts': [{}]}
        workflow.room_progress = {'stage': 'matching', 'deadline': 0.}
        workflow.round_rematches, workflow.max_rematches = 0, 3
        with tempfile.TemporaryDirectory() as directory, patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock.now):
            workflow.recover_runtime_failure(report, Path(directory), OSError('入房请求回执失败'))
        self.assertTrue(workflow.matched)
        self.assertTrue(report['joined_room'])
        self.assertIn('join_confirmed_at', report['attempts'][0])
        self.assertEqual(workflow.round_rematches, 1)
        workflow.device.back.assert_called_once()

    def test_room_with_disband_notice_is_not_a_successful_timeout_destination(self):
        workflow, clock, frames = self.ui_workflow(['room'])
        original = workflow.navigator.match.side_effect
        workflow.navigator.match.side_effect = lambda frame, name, *args: ((1., (0, 0)) if name == 'cooperative_disbanded' else original(frame, name, *args))
        workflow.current_report = {}
        workflow.room_progress = {'stage': 'matching', 'deadline': 0.}
        with tempfile.TemporaryDirectory() as directory, patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock.now), self.assertRaises(TimeoutError):
            workflow.return_after_room_timeout({}, Path(directory))
        for method in ('tap', 'back', 'home'): getattr(workflow.device, method).assert_not_called()

    def test_runtime_room_overlay_cannot_enter_safe_destination_branch(self):
        workflow, clock, frames = self.ui_workflow(['room'])
        workflow.device.screenshot.side_effect = workflow.screenshot
        original = workflow.navigator.match.side_effect
        workflow.navigator.match.side_effect = lambda frame, name, *args: ((1., (0, 0)) if name == 'cooperative_disbanded_dialog' else original(frame, name, *args))
        workflow.stop_requested = lambda: clock.now >= 5.
        report = {}
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(InterruptedError):
            workflow.recover_runtime_failure(report, Path(directory), RuntimeError('解散覆盖房间页'))
        workflow.device.preflight.assert_not_called()
        for method in ('tap', 'back', 'home'): getattr(workflow.device, method).assert_not_called()

    def test_cooperative_preplay_guard_uses_lower_bar_and_two_zero_source_frames(self):
        workflow, clock, frames = self.ui_workflow(['playing'])
        frame = np.full((720, 1280, 3), 50, np.uint8)
        zero = np.zeros((29, 97, 3), np.uint8)
        zero[8:20, 43:49] = 255
        frame[16:45, 1090:1187] = zero
        workflow.life_guard = LifeGuard(bar_area=(1008, 48, 1177, 56))
        workflow.navigator.zero_life_template = ZeroLifeTemplate(zero)
        workflow.navigator.match.side_effect = None
        workflow.navigator.match.return_value = (1., (0, 0))
        frame[48:56, 1008:1177] = (0, 255, 0)
        report = {}
        workflow.observe_preplay_life(frame, 10., report)
        self.assertEqual(workflow.life_guard.zero_streak, 0)
        frame[48:56, 1008:1177] = 50
        workflow.observe_preplay_life(frame, 12., report)
        with self.assertRaises(LifeDepleted): workflow.observe_preplay_life(frame, 14., report)
        self.assertTrue(report['preplay_death_confirmed'])
        self.assertIn('death_confirmed_at', report)
        self.assertEqual([sample['zero_streak'] for sample in report['preplay_life_monitor']['samples']], [0, 1, 2])
        for method in ('tap', 'back', 'home', 'screenshot'): getattr(workflow.device, method).assert_not_called()

    def test_native_preplay_death_releases_zero_input_player_before_cooperative_home(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = self.run_workflow(Path(directory))
            workflow.matched = True
            frame = np.full((720, 1280, 3), 50, np.uint8)
            zero = np.zeros((29, 97, 3), np.uint8)
            zero[8:20, 43:49] = 255
            frame[16:45, 1090:1187] = zero
            workflow.navigator.templates = {'life_hud': frame}
            workflow.navigator.zero_life_template = ZeroLifeTemplate(zero)
            workflow.navigator.match.side_effect = lambda image, name, *args: (float(name == 'life_hud'), (0, 0))
            workflow.repository.load_chart.return_value = '#BPM01:120\n#00008:01\n#00012:11\n#00113:11'
            workflow.submit_ready = Mock(return_value=identity())
            workflow.matcher = Mock()
            workflow.matcher.match.side_effect = ValueError('最终身份尚未确认')
            clock = Clock()
            def capture():
                clock.advance(1.)
                return frame
            workflow.screenshot = Mock(side_effect=capture)
            workflow.pause = lambda _: None
            workflow.start = lambda *args, **kwargs: CooperativeLive.start(workflow, *args, **kwargs)
            player = Mock()
            player.report = {'engine': 'native', 'planned_actions': 2, 'sent_actions': 0, 'executed_actions': 0, 'release_confirmed': False}
            order = []
            def close():
                order.append('release')
                player.report.update(release_confirmed=True, release={'release_proof': 'no-touch-possible-and-cleanup'})
            player.close.side_effect = close
            workflow.device.home.side_effect = lambda: order.append('home')
            with patch('project_sekai.native_player.NativePlayer', return_value=player), patch('project_sekai.solo_live.time.monotonic', side_effect=lambda: clock.now), patch('project_sekai.solo_live.time.perf_counter', side_effect=lambda: clock.now), self.assertRaises(LifeDepleted):
                try:
                    workflow.run(1, 'easy', 'current', engine='native')
                except InterruptedError:
                    self.fail(str(workflow.last_report.get('error')) + str(workflow.last_report.get('preplay_life_monitor')))
            self.assertEqual(order, ['release', 'home'])
            player.play.assert_not_called()
            workflow.device.back.assert_not_called()
            workflow.collect.assert_not_called()
            self.assertEqual(workflow.performed_rounds, 0)
            saved = json.loads(Path(workflow.last_report['report_path']).read_text(encoding='utf-8'))
            self.assertTrue(saved['life_monitor']['zero_confirmed'])
            self.assertTrue(saved['preplay_death_confirmed'])
            self.assertEqual(saved['playback']['sent_actions'], 0)

    def test_room_idle_back_ack_failure_consumes_all_three_requests_without_reset(self):
        workflow, clock, frames = self.ui_workflow(['ready'])
        workflow.current_report = {}
        workflow.room_progress = {'stage': 'ready', 'deadline': 0.}
        workflow.device.back.side_effect = OSError('ESC回执失败')
        attempt = {}
        with tempfile.TemporaryDirectory() as directory, patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock.now):
            for count in range(1, 4):
                with self.assertRaises(OSError):
                    workflow.return_after_room_timeout(attempt, Path(directory))
                self.assertEqual(attempt['room_recovery']['exit_requests'], count)
            with self.assertRaises(TimeoutError):
                workflow.return_after_room_timeout(attempt, Path(directory))
        self.assertEqual(workflow.device.back.call_count, 3)
        workflow.device.tap.assert_not_called()

    def test_prepared_native_without_current_release_proof_cannot_use_room_exit(self):
        workflow, clock, frames = self.ui_workflow(['cancel'])
        workflow.current_report = {'playback': {'engine': 'native', 'planned_actions': 2, 'sent_actions': 0,
                                  'executed_actions': 0, 'release_confirmed': True}}
        workflow.room_progress = {'stage': 'cancel', 'deadline': 0.}
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(RuntimeError):
            workflow.return_after_room_timeout({}, Path(directory))
        for method in ('tap', 'back', 'home'): getattr(workflow.device, method).assert_not_called()

    def test_room_progress_advances_only_forward_and_recovery_keeps_deadline(self):
        workflow, clock, frames = self.ui_workflow(['matching', 'select', 'shuffle', 'ready', 'cancel', 'black'])
        with patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock.now):
            workflow.observe_room_progress(frames['matching'])
            original = workflow.room_progress['deadline']
            clock.advance(100.)
            workflow.observe_room_progress(frames['matching'])
            workflow.observe_room_progress(frames['black'])
            self.assertEqual(workflow.room_progress['deadline'], original)
            workflow.observe_room_progress(frames['select'])
            advanced = workflow.room_progress['deadline']
            self.assertGreater(advanced, original)
            clock.advance(20.)
            workflow.observe_room_progress(frames['matching'])
            self.assertEqual(workflow.room_progress['deadline'], advanced)
            workflow.observe_room_progress(frames['shuffle'])
            workflow.observe_room_progress(frames['ready'])
            workflow.observe_room_progress(frames['cancel'])
            self.assertEqual(workflow.room_progress['stage'], 'cancel')

    def test_room_idle_exit_uses_back_even_with_gray_arrow_and_never_taps(self):
        workflow, clock, frames = self.ui_workflow(['matching_disabled', 'matching_disabled', 'room'])
        workflow.current_report = {}
        workflow.room_progress = {'stage': 'matching', 'deadline': 0.}
        with tempfile.TemporaryDirectory() as directory, patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock.now):
            result = workflow.return_after_room_timeout({}, Path(directory))
        self.assertIsNone(result)
        workflow.device.back.assert_called_once()
        workflow.device.tap.assert_not_called()

    def test_room_idle_exit_rechecks_progress_after_encoding_before_back(self):
        workflow, clock, frames = self.ui_workflow(['select', 'ready'])
        workflow.current_report = {}
        workflow.room_progress = {'stage': 'select', 'deadline': 0.}
        with tempfile.TemporaryDirectory() as directory, patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock.now):
            result = workflow.return_after_room_timeout({}, Path(directory))
        self.assertEqual(result[1], 'ready')
        workflow.device.back.assert_not_called()
        workflow.device.tap.assert_not_called()

    def test_room_idle_exit_rejects_unknown_loading_final_or_playing_and_preserves_budget(self):
        for state in ('black', 'final', 'shuffle_unknown', 'playing'):
            workflow, clock, frames = self.ui_workflow([state])
            workflow.current_report = {}
            workflow.room_progress = {'stage': 'ready', 'deadline': 0.}
            attempt = {}
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory, patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock.now), self.assertRaises(TimeoutError):
                workflow.return_after_room_timeout(attempt, Path(directory))
            self.assertEqual(attempt['room_recovery']['exit_requests'], 0)
            for method in ('tap', 'back', 'home'): getattr(workflow.device, method).assert_not_called()

    def test_resume_prepared_cancel_start_does_not_send_ready_or_vote(self):
        from project_sekai.solo_live import SoloLive
        workflow, clock, frames = self.ui_workflow(['cancel'])
        workflow.resume_stage = 'cancel'
        workflow.submit_ready = Mock()
        report = {}
        def parent(*args, **kwargs):
            kwargs['ready_action']()
            return 12.
        with tempfile.TemporaryDirectory() as directory, patch.object(SoloLive, 'start', side_effect=parent):
            self.assertEqual(workflow.start(None, None, report, Path(directory)), 12.)
        workflow.submit_ready.assert_not_called()
        self.assertTrue(report['ready_confirmed'])
        self.assertEqual(report.get('final_identity'), None)
        workflow.navigator.tap.assert_not_called()

    def test_actual_join_conflicting_pages_cannot_cancel_sixty_second_deadline(self):
        workflow, clock, frames = self.ui_workflow(['matching'])
        original = workflow.navigator.match.side_effect
        workflow.navigator.match.side_effect = lambda frame, name, *args: ((1., (0, 0)) if name == 'cooperative_ready' else original(frame, name, *args))
        with tempfile.TemporaryDirectory() as directory, patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock.now), self.assertRaises(TimeoutError):
            workflow.confirm_room_join({})
        self.assertIsNotNone(workflow.join_deadline)
        self.assertFalse(workflow.matched)

    def test_room_button_ack_without_actual_join_times_out_in_sixty_seconds(self):
        workflow, clock, frames = self.ui_workflow(['room'])
        report = {'attempts': [{}]}
        with tempfile.TemporaryDirectory() as directory, patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock.now), self.assertRaises(TimeoutError):
            workflow.current_directory = Path(directory)
            workflow.choose('free', 'easy', 'random', report, Path(directory))
        self.assertGreaterEqual(clock.now, 61.)
        self.assertLess(clock.now, 62.)
        self.assertNotIn('member_wait_deadline', report['attempts'][0])

    def test_cooperative_start_uses_one_hundred_eighty_seconds_without_changing_solo_default(self):
        from project_sekai.solo_live import SoloLive
        workflow = CooperativeLive.__new__(CooperativeLive)
        with tempfile.TemporaryDirectory() as directory, patch.object(SoloLive, 'start', return_value=12.) as start:
            self.assertEqual(workflow.start(None, None, {}, Path(directory)), 12.)
        self.assertEqual(start.call_args.kwargs.get('opening_timeout'), 180)

    def test_cold_matching_without_deadline_starts_full_wait_without_exit_input(self):
        workflow, clock, frames = self.ui_workflow(['matching_decided'])
        workflow.device.screenshot.side_effect = workflow.screenshot
        workflow.stop_requested = lambda: clock.now >= 20.
        report = {}
        workflow.current_report = report
        with tempfile.TemporaryDirectory() as directory, patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock.now), self.assertRaises(InterruptedError):
            workflow.recover_runtime_failure(report, Path(directory), TimeoutError('进入菜单时连接异常'))
        self.assertIn('member_wait_deadline', report['runtime_attempt'])
        self.assertGreater(report['runtime_attempt']['member_wait_deadline'], clock.now)
        self.assertGreaterEqual(report['runtime_attempt']['member_wait_deadline'], 181.)
        workflow.device.tap.assert_not_called()

    def test_actual_room_ends_entry_deadline_without_resetting_button_budget(self):
        workflow, clock, frames = self.ui_workflow(['room'])
        workflow.entry_deadline = -1.
        workflow.entry_request_count, workflow.menu_request_count = 2, 1
        with patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock.now):
            workflow.open_rooms()
        self.assertIsNone(workflow.entry_deadline)
        self.assertEqual((workflow.entry_request_count, workflow.menu_request_count), (2, 1))

    def test_expired_entry_without_actual_room_keeps_deadline_and_waits(self):
        workflow, clock, frames = self.ui_workflow(['home'])
        workflow.entry_deadline = -1.
        workflow.entry_request_count, workflow.menu_request_count = 1, 1
        workflow.device.screenshot.side_effect = workflow.screenshot
        workflow.stop_requested = lambda: clock.now >= 5.
        report = {}
        with tempfile.TemporaryDirectory() as directory, patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock.now), self.assertRaises(InterruptedError):
            workflow.recover_runtime_failure(report, Path(directory), TimeoutError('未进入房间的入口超时'))
        self.assertEqual(workflow.entry_deadline, -1.)
        self.assertEqual((workflow.entry_request_count, workflow.menu_request_count), (1, 1))
        for method in ('tap', 'back', 'home'): getattr(workflow.device, method).assert_not_called()

    def test_member_exit_rejects_conflicting_known_preparation_pages(self):
        workflow, clock, frames = self.ui_workflow(['matching_decided'])
        original = workflow.navigator.match.side_effect
        for conflict in ('cooperative_select', 'cooperative_ready', 'cooperative_cancel', 'cooperative_disbanded_dialog'):
            with self.subTest(conflict=conflict):
                workflow.navigator.match.side_effect = lambda frame, name, *args: ((1., (0, 0)) if name == conflict else original(frame, name, *args))
                self.assertFalse(workflow.member_room_can_leave(frames['matching_decided']))

    def test_runtime_matching_before_original_deadline_waits_and_resumes_selection(self):
        workflow, clock, frames = self.ui_workflow(['matching_decided', 'select'])
        workflow.device.screenshot.side_effect = workflow.screenshot
        report = {'attempts': [{'member_wait_deadline': 121.}]}
        workflow.current_report = report
        with tempfile.TemporaryDirectory() as directory, patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock.now):
            workflow.recover_runtime_failure(report, Path(directory), OSError('加入后短暂连接异常'))
        self.assertTrue(workflow.resume_selection)
        workflow.device.tap.assert_not_called()

    def test_matching_exit_deadline_and_request_budget_never_reset_on_reentry(self):
        workflow, clock, frames = self.ui_workflow(['matching_decided'])
        workflow.current_report = {}
        report = {'room_recovery': {'exit_clicks': 3, 'exit_deadline': 1.}}
        with tempfile.TemporaryDirectory() as directory, patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock.now), self.assertRaises(TimeoutError):
            workflow.return_after_matching_timeout(report, Path(directory))
        self.assertEqual(report['room_recovery']['exit_clicks'], 3)
        self.assertEqual(report['room_recovery']['exit_deadline'], 1.)
        workflow.device.tap.assert_not_called()

    def test_recovery_cancel_updates_report_and_preserves_original_failure(self):
        workflow, clock, frames = self.ui_workflow(['black'])
        workflow.device.screenshot.side_effect = workflow.screenshot
        workflow.stop_requested = lambda: clock.now >= 5.
        report = {'error': 'TimeoutError: 原匹配失败', 'completed': False}
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(InterruptedError):
            workflow.recover_runtime_failure(report, Path(directory), TimeoutError('原匹配失败'))
        self.assertTrue(report['cancelled'])
        self.assertIn('stopped_at', report)
        self.assertEqual(report['error'], 'TimeoutError: 原匹配失败')
        for method in ('tap', 'back', 'home'): getattr(workflow.device, method).assert_not_called()

    def test_front_entry_menu_retries_share_original_total_deadline(self):
        workflow, clock, frames = self.ui_workflow(['home', 'menu', 'room'])
        original = workflow.navigator.match.side_effect
        workflow.navigator.match.side_effect = lambda frame, name, *args: ((1., (0, 0)) if frame is frames['menu'] and name == 'live_menu' else original(frame, name, *args))
        workflow.screenshot = lambda: (clock.advance(.04) or frames['home'] if clock.now < 25 else clock.advance(.04) or frames['menu'])
        workflow.tap_page = Mock(return_value=None)
        with tempfile.TemporaryDirectory() as directory, patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock.now), self.assertRaises(TimeoutError):
            workflow.current_directory = Path(directory)
            workflow.open_rooms()
        self.assertLess(clock.now, 61.5)
        self.assertLessEqual(workflow.tap_page.call_count, 6)

    def test_matching_exit_failed_ack_consumes_persistent_request_budget(self):
        workflow, clock, frames = self.ui_workflow(['matching'])
        workflow.current_report = {}
        workflow.device.tap.side_effect = OSError('可能已发出，回执失败')
        report = {}
        with tempfile.TemporaryDirectory() as directory, patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock.now):
            for index in range(3):
                with self.assertRaises(OSError):
                    workflow.return_after_matching_timeout(report, Path(directory))
                self.assertEqual(report['room_recovery']['exit_clicks'], index + 1)
        self.assertEqual(workflow.device.tap.call_count, 3)

    def test_home_live_entry_retries_only_on_fresh_confirmed_home(self):
        workflow,clock,frames=self.ui_workflow(['home','live_menu','room'])
        state='home';home_clicks=0
        original_match=workflow.navigator.match.side_effect
        workflow.navigator.match.side_effect=lambda frame,page,*args: (1.,(0,0)) if frame is frames['live_menu'] and page=='live_menu' else original_match(frame,page,*args)
        def screenshot():
            clock.advance(.04)
            return frames[state]
        def tap(x,y,*_):
            nonlocal state,home_clicks
            if (x,y)==(1194,649):
                home_clicks+=1
                if home_clicks==2:state='live_menu'
            elif (x,y)==(907,235):state='room'
        workflow.screenshot=screenshot;workflow.navigator.tap.side_effect=tap
        workflow.current_directory=Path('.')
        with patch('project_sekai.cooperative_live.time.monotonic',side_effect=lambda:clock.now):
            self.assertIs(workflow.open_rooms(),frames['room'])
        self.assertEqual(home_clicks,2)
        workflow.navigator.return_to_home.assert_not_called()

    def test_death_survives_failed_native_close_then_cleanup_finishes_before_home(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow=self.run_workflow(Path(directory));order=[]
            player=Mock();player.report={'engine':'native','planned_actions':2,'sent_actions':1,
                'executed_actions':1,'release_confirmed':False}
            player.play.side_effect=LifeDepleted('实际生命归零')
            def close():
                order.append('close')
                if player.close.call_count==1:raise RuntimeError('cleanup暂不可用')
                player.report.update(release_confirmed=True,release={'reset_executed':True,
                    'release_proof':'current-reset-jlog-and-cleanup'})
            def pause(_):
                self.assertTrue(workflow.last_report['death_confirmed'])
                self.assertIs(workflow.pending_native_player,player)
                workflow.device.home.assert_not_called();workflow.device.back.assert_not_called()
            player.close.side_effect=close;workflow.pause=pause
            workflow.device.home.side_effect=lambda:order.append('home')
            with patch('project_sekai.native_player.NativePlayer',return_value=player) as factory,self.assertRaises(LifeDepleted):
                self.execute(workflow,count=2,engine='native')
            self.assertEqual(order,['close','close','home'])
            self.assertEqual(factory.call_count,1)
            self.assertEqual(workflow.choose.call_count,1)
            self.assertIsNone(workflow.pending_native_player)

    def test_waiting_release_keeps_same_player_and_sends_no_page_input_until_cancel(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow=self.run_workflow(Path(directory));player=Mock()
            player.report={'engine':'native','planned_actions':2,'sent_actions':2,'executed_actions':2,'release_confirmed':False}
            player.close.side_effect=OSError('未取得当前reset回执')
            workflow.pause=lambda _:None
            workflow.stop_requested=lambda:player.close.call_count>=3
            with patch('project_sekai.native_player.NativePlayer',return_value=player) as factory,self.assertRaises(InterruptedError):
                self.execute(workflow,engine='native')
            self.assertEqual(factory.call_count,1)
            self.assertIs(workflow.pending_native_player,player)
            self.assertIn('release_error',workflow.last_report)
            for name in ['home','back','tap']:getattr(workflow.device,name).assert_not_called()
            workflow.collect.assert_not_called()

    def test_connection_wait_is_cancellable_and_never_counts_as_release_proof(self):
        workflow,clock,_=self.ui_workflow(['home'])
        workflow.device.screenshot.side_effect=OSError('ADB断开')
        workflow.stop_requested=lambda:clock.now>=7
        report={'playback':{'release_confirmed':True},'engine':'legacy'}
        with tempfile.TemporaryDirectory() as directory,self.assertRaises(InterruptedError):
            workflow.recover_runtime_failure(report,Path(directory),OSError('断开'))
        self.assertEqual(report['task_state'],'waiting_connection')
        for name in ['home','back','tap']:getattr(workflow.device,name).assert_not_called()

    def test_recovery_waits_for_environment_before_ready(self):
        workflow, clock, frames = self.ui_workflow(['home'])
        workflow.device.screenshot.side_effect = workflow.screenshot
        workflow.device.preflight.side_effect = [ValueError('DPI不匹配'), None]
        report = {}
        with tempfile.TemporaryDirectory() as directory:
            workflow.recover_runtime_failure(report, Path(directory), TimeoutError('入口超时'))
        self.assertEqual(workflow.device.preflight.call_count, 2)
        self.assertGreaterEqual(clock.now, 2.)
        self.assertEqual(report['task_state'], 'ready')
        workflow.device.back.assert_not_called()

    def test_recovery_second_settlement_frame_disconnect_waits_without_back(self):
        workflow, clock, frames = self.ui_workflow(['personal'])
        workflow.device.screenshot.side_effect = [frames['personal'], OSError('第二帧断开')]
        workflow.stop_requested = lambda: clock.now >= 4.
        report = {}
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(InterruptedError):
            workflow.recover_runtime_failure(report, Path(directory), RuntimeError('结算故障'))
        workflow.device.back.assert_not_called()
        self.assertEqual(report['task_state'], 'waiting_connection')

    def test_recovery_failed_back_ack_consumes_budget_without_blind_replay(self):
        workflow, clock, frames = self.ui_workflow(['personal'])
        workflow.device.screenshot.side_effect = workflow.screenshot
        workflow.device.back.side_effect = OSError('回执丢失')
        workflow.stop_requested = lambda: clock.now >= 15.
        report = {}
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(InterruptedError):
            workflow.recover_runtime_failure(report, Path(directory), RuntimeError('结算故障'))
        self.assertEqual(workflow.device.back.call_count, 3)
        self.assertEqual(report['runtime_recovery']['safe_backs'], 3)
        for call in workflow.navigator.match.call_args_list:
            if call.args[1] == 'life_hud':
                self.assertEqual(call.args[2], LIFE_HUD_AREA)

    def test_recovery_exhausted_rematches_waits_on_home_without_resetting_budget(self):
        workflow, clock, frames = self.ui_workflow(['home'])
        workflow.device.screenshot.side_effect = workflow.screenshot
        workflow.stop_requested = lambda: clock.now >= 5.
        report = {'rematch_limit_reached': True}
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(InterruptedError):
            workflow.recover_runtime_failure(report, Path(directory), RuntimeError('三次重匹配耗尽'))
        self.assertTrue(report['rematch_limit_reached'])
        self.assertEqual(report['task_state'], 'waiting_room')
        for method in ('tap', 'back', 'home'): getattr(workflow.device, method).assert_not_called()

    def test_recovery_insufficient_resource_waits_without_another_batch(self):
        workflow, clock, frames = self.ui_workflow(['home'])
        workflow.device.screenshot.side_effect = workflow.screenshot
        workflow.read_available_bonus = Mock(return_value=(3, None))
        workflow.stop_requested = lambda: clock.now >= 5.
        report = {'bonus': {'consumption': 5, 'availability': 'insufficient'},
                  'recovery': {'status': 'started', 'confirmed_count': 1}}
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(InterruptedError):
            workflow.recover_runtime_failure(report, Path(directory), RuntimeError('体力不足'))
        self.assertEqual(report['recovery']['confirmed_count'], 1)
        self.assertEqual(report['task_state'], 'waiting_resource')
        for method in ('tap', 'back', 'home'): getattr(workflow.device, method).assert_not_called()

    def test_recovery_exhausted_entry_requests_do_not_retry_same_home(self):
        workflow, clock, frames = self.ui_workflow(['home'])
        workflow.entry_request_count = 3
        workflow.device.screenshot.side_effect = workflow.screenshot
        workflow.stop_requested = lambda: clock.now >= 5.
        report = {}
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(InterruptedError):
            workflow.recover_runtime_failure(report, Path(directory), TimeoutError('Live入口失效'))
        self.assertEqual(workflow.entry_request_count, 3)
        self.assertEqual(report['task_state'], 'waiting_page')
        workflow.device.preflight.assert_not_called()
        for method in ('tap', 'back', 'home'): getattr(workflow.device, method).assert_not_called()

    def test_recovery_confirms_death_in_two_normal_samples_before_exit(self):
        for state in ['playing', 'failed']:
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                workflow, clock, frames = self.ui_workflow([state])
                workflow.device.screenshot.side_effect = workflow.screenshot
                workflow.life_guard = LifeGuard(bar_area=(1008, 48, 1177, 56))
                workflow.navigator.zero_life_template = None
                workflow.read_optional_number = Mock(return_value=0)
                report = {'joined_room': True, 'playback': {'release_confirmed': True}, 'engine': 'legacy'}
                workflow.last_report = report
                workflow.finish_death = Mock()
                workflow.persist_runtime_report = Mock()
                with patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock.now), self.assertRaises(LifeDepleted):
                    workflow.recover_runtime_failure(report, Path(directory), RuntimeError('原输入失败'))
                self.assertGreaterEqual(clock.now, 2.)
                self.assertEqual(workflow.device.screenshot.call_count, 2)
                self.assertTrue(report['death_confirmed'])
                workflow.finish_death.assert_called_once()
                for method in ('tap', 'back', 'home'): getattr(workflow.device, method).assert_not_called()

    def test_recovery_confirmed_death_stays_terminal_after_persistence_failure(self):
        workflow, clock, frames = self.ui_workflow(['failed', 'failed', 'home'])
        workflow.device.screenshot.side_effect = workflow.screenshot
        workflow.life_guard = LifeGuard(bar_area=(1008, 48, 1177, 56))
        report = {'joined_room': True}
        workflow.last_report = report
        workflow.finish_death = Mock()
        workflow.persist_runtime_report = Mock(side_effect=[OSError('死亡JSON暂不可写'), None])
        with tempfile.TemporaryDirectory() as directory, patch('project_sekai.cooperative_live.time.monotonic', side_effect=lambda: clock.now), self.assertRaises(LifeDepleted):
            workflow.recover_runtime_failure(report, Path(directory), RuntimeError('原输入失败'))
        self.assertTrue(report['death_confirmed'])
        self.assertEqual(workflow.device.screenshot.call_count, 2)
        self.assertEqual(workflow.persist_runtime_report.call_count, 2)
        workflow.device.preflight.assert_not_called()
        for method in ('tap', 'back', 'home'): getattr(workflow.device, method).assert_not_called()

    def test_corrupted_csv_waits_for_repair_before_counting_performance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / 'results.csv'
            csv_path.write_bytes(b'\xff\xfeinvalid')
            workflow = self.run_workflow(root)
            waits = []
            def repair(_):
                waits.append(1)
                self.assertEqual(workflow.completed_rounds, 0)
                self.assertEqual(csv_path.read_bytes(), b'\xff\xfeinvalid')
                csv_path.unlink()
            workflow.pause = repair
            self.execute(workflow)
            self.assertEqual(len(waits), 1)
            self.assertEqual(workflow.completed_rounds, 1)
            self.assertTrue(workflow.last_report['persistence_confirmed'])
            with csv_path.open(encoding='utf-8-sig') as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 1)

    def test_csv_shape_error_waits_without_replacing_original_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / 'results.csv'
            original = 'run,unexpected_column\nold,kept\n'
            csv_path.write_text(original, encoding='utf-8')
            workflow = self.run_workflow(root)
            with self.assertRaises(InterruptedError):
                self.execute(workflow)
            self.assertEqual(csv_path.read_text(encoding='utf-8'), original)
            self.assertEqual(workflow.completed_rounds, 0)
            self.assertEqual(workflow.last_report['task_state'], 'waiting_persistence')

    def test_corrupted_csv_persistence_wait_is_cancellable_without_replacing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / 'results.csv'
            csv_path.write_bytes(b'\xff\xfeinvalid')
            workflow = self.run_workflow(root)
            with self.assertRaises(InterruptedError):
                self.execute(workflow)
            self.assertEqual(csv_path.read_bytes(), b'\xff\xfeinvalid')
            self.assertEqual(workflow.completed_rounds, 0)
            self.assertEqual(workflow.last_report['task_state'], 'waiting_persistence')

    def test_recovery_zero_samples_save_both_source_frames_and_final_guard(self):
        workflow, clock, frames = self.ui_workflow(['playing'])
        workflow.life_guard = LifeGuard(bar_area=(1008, 48, 1177, 56))
        workflow.navigator.zero_life_template = None
        workflow.read_optional_number = Mock(return_value=0)
        report = {'playback': {'release_confirmed': True}, 'engine': 'legacy'}
        workflow.last_report = report
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertFalse(workflow.observe_recovery_death(frames['playing'], root, captured_at=10.))
            self.assertTrue(workflow.observe_recovery_death(frames['playing'], root, captured_at=12.))
            workflow.save_recovery_evidence(report, root)
            self.assertTrue(report['life_monitor']['zero_confirmed'])
            samples = report['recovery_observation']['samples']
            self.assertEqual([sample['captured_at'] for sample in samples], [10., 12.])
            self.assertEqual([sample['zero_streak'] for sample in samples], [1, 2])
            replay = LifeGuard(bar_area=(1008, 48, 1177, 56))
            for sample in samples:
                frame = cv2.imread(str(root / sample['path']))
                self.assertEqual(sample['zero_method'], 'ocr')
                self.assertEqual(sample['zero_value'], 0)
                self.assertEqual(sample['filled_pixels'], 0)
                replay.observe(frame, lambda image: sample['zero_value'] == 0)
            self.assertTrue(replay.zero_confirmed)

    def test_recovery_blank_sample_does_not_inherit_failed_page_evidence(self):
        workflow, clock, frames = self.ui_workflow(['failed', 'black'])
        workflow.life_guard = LifeGuard(bar_area=(1008, 48, 1177, 56))
        workflow.last_report = {}
        with tempfile.TemporaryDirectory() as directory:
            for when, name in [(10., 'failed'), (12., 'black'), (14., 'failed')]:
                self.assertFalse(workflow.observe_recovery_death(frames[name], Path(directory), captured_at=when))
        samples = workflow.last_report['recovery_observation']['samples']
        self.assertEqual(samples[0]['confirmation_frames'], 1)
        self.assertTrue(samples[1]['blank_transition'])
        self.assertIsNone(samples[1]['pause_score'])
        self.assertIsNone(samples[1]['life_hud_score'])
        self.assertIsNone(samples[1]['abnormal_page'])
        self.assertEqual(samples[1]['confirmation_frames'], 0)
        self.assertFalse(samples[1]['bar_observed'])
        self.assertEqual(samples[2]['confirmation_frames'], 1)
        self.assertFalse(workflow.life_guard.zero_confirmed)

    def test_recovery_observation_is_bounded_and_unknown_zero_never_confirms(self):
        for mode in ('healthy', 'unknown_zero', 'pause_only'):
            workflow, clock, frames = self.ui_workflow(['playing'])
            workflow.life_guard = LifeGuard(bar_area=(1008, 48, 1177, 56))
            workflow.navigator.zero_life_template = None
            workflow.read_optional_number = Mock(side_effect=ValueError('数字未知'))
            frame = frames['playing']
            if mode == 'healthy': frame[48:56, 1008:1177] = (0, 255, 0)
            if mode == 'pause_only':
                original = workflow.navigator.match.side_effect
                workflow.navigator.match.side_effect = lambda image, name, *args: ((0., (0, 0)) if name == 'life_hud' else original(image, name, *args))
            workflow.last_report = {}
            with tempfile.TemporaryDirectory() as directory:
                for index in range(12):
                    self.assertFalse(workflow.observe_recovery_death(frame, Path(directory), captured_at=index * 2.))
            self.assertEqual(len(workflow.recovery_sample_frames), 8)
            self.assertFalse(workflow.life_guard.zero_confirmed)
            if mode in ('healthy', 'pause_only'): workflow.read_optional_number.assert_not_called()

    def test_persistence_retry_updates_final_json_and_indexes_run_only_once(self):
        from project_sekai.cooperative_live import append_cooperative_result_index as real_index
        with tempfile.TemporaryDirectory() as directory:
            workflow=self.run_workflow(Path(directory));calls=0
            workflow.pause=lambda _:None
            def index(*args):
                nonlocal calls
                calls+=1
                if calls==1:raise OSError('CSV暂时锁定')
                return real_index(*args)
            with patch('project_sekai.cooperative_live.append_cooperative_result_index',side_effect=index):
                self.execute(workflow)
            saved=json.loads(Path(workflow.last_report['report_path']).read_text())
            self.assertTrue(saved['persistence_confirmed'])
            self.assertEqual(saved['runtime_recovery']['state'],'persisted')
            self.assertEqual(workflow.completed_rounds,1)
            real_index(workflow.report_root,Path(saved['report_path']).parent,saved)
            with (Path(directory)/'results.csv').open(encoding='utf-8-sig') as stream:
                self.assertEqual(len(list(csv.DictReader(stream))),1)

    def test_pre_match_stamina_failure_keeps_original_reason_and_report(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = self.run_workflow(Path(directory))
            workflow.prepare_bonus.side_effect = RuntimeError("体力不足且用药关闭")
            with self.assertRaises(InterruptedError):
                self.execute(workflow)
            saved = json.loads(Path(workflow.last_report["report_path"]).read_text(encoding="utf-8"))
            self.assertIn("体力不足", saved["error"])
            workflow.choose.assert_not_called()
            workflow.device.home.assert_not_called()

    def test_joined_room_generic_identity_failure_goes_home_before_task_ends(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = self.run_workflow(Path(directory))
            def fail_after_join(*args):
                workflow.matched = True
                raise RuntimeError("实际抽选歌曲、标题或难度未稳定确认")
            workflow.choose.side_effect = fail_after_join
            with self.assertRaises(InterruptedError):
                self.execute(workflow)
            workflow.device.home.assert_not_called()
            workflow.play.assert_not_called()
            self.assertIn("未稳定确认", workflow.last_report["error"])
            self.assertEqual(workflow.completed_rounds, 0)

    def test_missing_or_corrupted_chart_never_submits_ready_or_sends_gameplay(self):
        for error in [KeyError("缺谱"), ValueError("SHA256 校验失败")]:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                workflow = self.run_workflow(Path(directory))
                workflow.repository.load_chart.side_effect = error
                with self.assertRaises(InterruptedError):
                    self.execute(workflow)
                workflow.start.assert_not_called()
                workflow.play.assert_not_called()
                self.assertEqual(workflow.completed_rounds, 0)
                self.assertFalse(workflow.last_report["completed"])
                workflow.device.home.assert_not_called()
                self.assertIn("MissingCooperativeChart", workflow.last_report["error"])

    def test_disbanded_room_retries_have_a_bound_and_do_not_increase_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = self.run_workflow(Path(directory))
            workflow.choose.side_effect = RoomDisbanded("解散")
            with self.assertRaises(InterruptedError):
                self.execute(workflow)
            self.assertEqual(workflow.choose.call_count, 4)
            self.assertEqual(workflow.completed_rounds, 0)
            workflow.play.assert_not_called()
            self.assertEqual(len(workflow.last_report["attempts"]), 4)

    def test_rematch_after_native_prepare_confirms_release_before_joining_again(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = self.run_workflow(Path(directory))
            workflow.start.side_effect = [RoomDisbanded("加载时解散"), 0]
            player = Mock()
            player.report = {"release_confirmed": True, "planned_actions": 2, "sent_actions": 0, "executed_actions": 0,
                             "release":{"reset_executed":True,"release_proof":"current-reset-jlog-and-cleanup"}}

            def play(*args):
                player.report.update(sent_actions=2, executed_actions=2)

            player.play.side_effect = play
            workflow.return_after_disbanded.side_effect = lambda *_: self.assertEqual(player.close.call_count, 1)
            with patch("project_sekai.native_player.NativePlayer", return_value=player):
                self.execute(workflow, engine="native")
            self.assertEqual(player.close.call_count, 2)
            self.assertEqual(workflow.choose.call_count, 2)
            self.assertEqual(workflow.completed_rounds, 1)

    def test_native_cancelled_during_loading_releases_and_stops_without_settlement_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = self.run_workflow(Path(directory))
            workflow.start.side_effect = InterruptedError("用户停止")
            player = Mock()
            player.report = {"release_confirmed": True, "planned_actions": 2, "sent_actions": 0}
            with patch("project_sekai.native_player.NativePlayer", return_value=player), self.assertRaises(InterruptedError):
                self.execute(workflow, engine="native")
            player.close.assert_called_once()
            player.play.assert_not_called()
            workflow.collect.assert_not_called()
            self.assertEqual(workflow.completed_rounds, 0)

    def test_native_release_failure_blocks_all_navigation_and_keeps_failure_report(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = self.run_workflow(Path(directory))
            player = Mock()
            player.report = {"release_confirmed": False, "planned_actions": 2, "sent_actions": 2, "executed_actions": 2}
            player.close.side_effect = RuntimeError("reset 未确认")
            with patch("project_sekai.native_player.NativePlayer", return_value=player), self.assertRaises(InterruptedError):
                self.execute(workflow, engine="native")
            workflow.collect.assert_not_called()
            self.assertEqual(workflow.completed_rounds, 0)
            self.assertIn("release_error", workflow.last_report)

    def test_cooperative_life_zero_releases_then_homes_without_result_or_solo_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = self.run_workflow(Path(directory))
            workflow.exit_depleted_live = Mock()

            def play(events, epoch, offset, report, directory):
                report["playback"] = {"release_confirmed": True, "planned_actions": 2, "sent_actions": 1}
                raise LifeDepleted("协力生命零")

            workflow.play.side_effect = play
            with self.assertRaises(LifeDepleted):
                self.execute(workflow)
            workflow.collect.assert_not_called()
            workflow.exit_depleted_live.assert_not_called()
            workflow.device.home.assert_called_once()
            self.assertEqual(workflow.completed_rounds, 0)
            self.assertFalse(workflow.last_report["completed"])
            self.assertEqual(workflow.last_report["background_exit"]["reason"], "life_zero")

    def test_background_exit_requires_release_and_actual_launcher_focus(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, clock, _ = self.ui_workflow(["playing"])
            report = {"playback": {"release_confirmed": False}}
            with self.assertRaisesRegex(RuntimeError, "释放"):
                workflow.background_game(report, Path(directory), "life_zero")
            workflow.device.home.assert_not_called()
            report["playback"]["release_confirmed"] = True
            workflow.device.shell.side_effect = ["com.android.launcher3/.Launcher\n"] + [
                "mCurrentFocus=Window{123 u0 com.sega.pjsekai/.MainActivity}\n"] * 100
            with patch("project_sekai.cooperative_live.time.monotonic", side_effect=lambda: clock.now), \
                    patch("project_sekai.cooperative_live.time.sleep", side_effect=clock.advance), \
                    self.assertRaisesRegex(RuntimeError, "焦点"):
                workflow.background_game(report, Path(directory), "life_zero")
            workflow.device.home.assert_called_once()
            self.assertFalse(report["background_exit"]["confirmed"])

    def test_native_life_zero_receives_release_before_home_and_skips_all_settlement(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = self.run_workflow(Path(directory))
            player = Mock()
            player.report = {"release_confirmed": False, "planned_actions": 2, "sent_actions": 1, "executed_actions": 1}
            player.play.side_effect = LifeDepleted("生命零")
            order = []

            def close():
                order.append("release")
                player.report.update(release_confirmed=True,release={
                    'reset_executed':True,'release_proof':'current-reset-jlog-and-cleanup'})

            player.close.side_effect = close
            workflow.device.home.side_effect = lambda: order.append("home")
            with patch("project_sekai.native_player.NativePlayer", return_value=player), self.assertRaises(LifeDepleted):
                self.execute(workflow, engine="native")
            self.assertEqual(order, ["release", "home"])
            workflow.collect.assert_not_called()
            workflow.device.back.assert_not_called()

    def test_native_playback_error_releases_then_homes_without_waiting_for_song_or_next_round(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = self.run_workflow(Path(directory))
            player = Mock()
            player.report = {'engine':'native','release_confirmed':False,'planned_actions':2,
                             'sent_actions':1,'executed_actions':1}
            player.play.side_effect = RuntimeError('设备执行回执异常')
            order = []
            def close():
                order.append('release')
                player.report.update(release_confirmed=True,release={
                    'reset_executed':True,'release_proof':'current-reset-jlog-and-cleanup'})
            player.close.side_effect = close
            workflow.device.home.side_effect = lambda: order.append('home')
            with patch('project_sekai.native_player.NativePlayer',return_value=player), self.assertRaises(InterruptedError):
                self.execute(workflow,count=2,engine='native')
            self.assertEqual(order,['release'])
            player.close.assert_called_once()
            workflow.collect.assert_not_called()
            self.assertEqual(workflow.choose.call_count,1)
            self.assertEqual(workflow.completed_rounds,0)
            self.assertTrue(workflow.last_report['skipped_after_input'])
            self.assertIn('设备执行',workflow.last_report['error'])

    def test_incomplete_dispatch_fails_before_any_settlement_navigation(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = self.run_workflow(Path(directory))
            def play(events,epoch,offset,report,directory):
                report['playback'] = {'release_confirmed':True,'planned_actions':2,'sent_actions':1}
            workflow.play.side_effect = play
            with self.assertRaises(InterruptedError):
                self.execute(workflow)
            workflow.collect.assert_not_called()
            workflow.device.home.assert_not_called()

    def test_native_release_flag_without_current_reset_proof_cannot_enter_settlement_or_home(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = self.run_workflow(Path(directory))
            player = Mock()
            player.report = {'engine':'native','release_confirmed':True,'planned_actions':2,
                             'sent_actions':2,'executed_actions':2}
            with patch('project_sekai.native_player.NativePlayer',return_value=player), self.assertRaises(InterruptedError):
                self.execute(workflow,engine='native')
            workflow.collect.assert_not_called()
            workflow.device.home.assert_not_called()

    def test_partial_dispatch_and_index_write_failure_never_increase_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = self.run_workflow(Path(directory))
            with patch("project_sekai.cooperative_live.append_cooperative_result_index", side_effect=OSError("CSV 锁定")), \
                    self.assertRaises(OSError):
                self.execute(workflow)
            self.assertEqual(workflow.completed_rounds, 0)
            self.assertFalse(workflow.last_report["completed"])
            self.assertIn("persistence_error", workflow.last_report)
        for key in ["sent_actions", "executed_actions"]:
            report = {"engine": "native", "playback": {"release_confirmed": True, "planned_actions": 2,
                                                       "sent_actions": 2, "executed_actions": 2}}
            report["playback"][key] = 1
            with self.assertRaises(RuntimeError):
                CooperativeLive.verify_playback(report)


if __name__ == "__main__":
    unittest.main()
