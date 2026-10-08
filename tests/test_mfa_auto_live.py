from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from agent import auto_live
from project_sekai.navigator import Navigator


def argument(task_id: int, parameters: dict) -> SimpleNamespace:
    return SimpleNamespace(
        task_detail=SimpleNamespace(task_id=task_id),
        custom_action_param=json.dumps(parameters),
    )


class AutoLiveConfigurationTests(unittest.TestCase):
    def tearDown(self) -> None:
        auto_live._SETTINGS.clear()

    def test_recovery_options_are_part_of_auto_live_task(self) -> None:
        interface_path = Path(__file__).resolve().parents[1] / "interface.json"
        interface = json.loads(interface_path.read_text(encoding="utf-8"))
        self.assertEqual([task["name"] for task in interface["task"]],
                         ["AutoLive", "SoloChartLive", "CooperativeChartLive", "OneShotChartLive", "SoloChartCalibration", "AdRewards"])
        self.assertEqual(
            next(task for task in interface["task"] if task["name"] == "AutoLive")["option"],
            ["AutoLiveSongMode", "AutoLiveCount", "AutoLiveRecoveryMode", "AutoLiveRecoveryCount"],
        )
        self.assertEqual(interface["option"]["AutoLiveSongMode"]["default_case"], "Current")
        self.assertEqual(
            [case["label"] for case in interface["option"]["AutoLiveSongMode"]["cases"]],
            ["当前歌曲", "随机选取"],
        )

    def test_mode_quantity_and_rounds_reach_navigator(self) -> None:
        task_id = 731
        context = SimpleNamespace(tasker=SimpleNamespace(controller=object(), stopping=False))
        self.assertTrue(auto_live.ProjectSekaiRecoveryModeConfig().run(context, argument(task_id, {"mode": "small"})))
        self.assertTrue(auto_live.ProjectSekaiRecoveryCountConfig().run(context, argument(task_id, {"count": 2})))
        self.assertTrue(auto_live.ProjectSekaiSongModeConfig().run(context, argument(task_id, {"mode": "random"})))
        with patch.object(auto_live, "MaaDevice"), patch.object(auto_live, "Navigator") as navigator_type, \
                patch.object(auto_live, "login_at_task_start") as login:
            self.assertTrue(auto_live.ProjectSekaiAutoLive().run(context, argument(task_id, {"count": 5})))
            login.assert_called_once()
            navigator_type.return_value.auto_live_loop.assert_called_once_with(
                5, song_mode="random", recovery_mode="small", recovery_count=2
            )
        self.assertNotIn(task_id, auto_live._SETTINGS)

    def test_invalid_quantity_is_rejected(self) -> None:
        task_id = 732
        context = SimpleNamespace(tasker=SimpleNamespace(controller=object(), stopping=False))
        self.assertTrue(auto_live.ProjectSekaiRecoveryModeConfig().run(context, argument(task_id, {"mode": "large"})))
        self.assertFalse(auto_live.ProjectSekaiRecoveryCountConfig().run(context, argument(task_id, {"count": 0})))
        self.assertEqual(auto_live._SETTINGS[task_id], {"mode": "large"})

    def test_invalid_song_mode_is_rejected(self) -> None:
        task_id = 733
        context = SimpleNamespace(tasker=SimpleNamespace(controller=object(), stopping=False))
        self.assertFalse(auto_live.ProjectSekaiSongModeConfig().run(context, argument(task_id, {"mode": "other"})))
        self.assertNotIn(task_id, auto_live._SETTINGS)


class SongSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.before = np.zeros((720, 1280, 3), dtype=np.uint8)
        self.after = self.before.copy()
        self.after[80:338, 908:1161] = 80
        self.navigator = Navigator.__new__(Navigator)
        self.navigator.dry_run = False
        self.navigator.threshold = 0.83
        self.navigator.stop_requested = lambda: False
        self.navigator.return_to_home = lambda: None
        self.taps: list[tuple[int, int]] = []
        self.navigator.tap = lambda x, y, _reason: self.taps.append((x, y))

    def test_current_song_does_not_click_shuffle_or_search_cover(self) -> None:
        self.navigator.wait = lambda _name: self.before
        self.navigator.match = lambda _frame, name, _area=None: (1.0 if name == "master_selected" else 0.0, (0, 0))
        self.navigator.navigate_to_prepare("current")
        self.assertEqual(self.taps, [(1194, 649), (722, 235), (1175, 493), (1007, 590)])

    def test_random_button_runs_before_master_and_confirm(self) -> None:
        song_select_reads = iter((self.before, self.after, self.after))
        self.navigator.wait = lambda name: next(song_select_reads) if name == "song_select" else self.after
        self.navigator.match = lambda _frame, name, _area=None: (1.0 if name == "master_selected" else 0.0, (0, 0))
        with patch("project_sekai.navigator.time.sleep"):
            self.navigator.navigate_to_prepare("random")
        self.assertEqual(
            self.taps,
            [(1194, 649), (722, 235), (943, 652), (1175, 493), (1007, 590)],
        )

    def test_random_button_stops_if_song_does_not_change(self) -> None:
        self.navigator.wait = lambda _name: self.before
        self.navigator._save_failure = lambda *_args: None
        with patch("project_sekai.navigator.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "未确认歌曲变化"):
                self.navigator._select_random_song(self.before)
        self.assertEqual(self.taps, [(943, 652)] * 3)


class RecoveryGuardTests(unittest.TestCase):
    def test_remembered_consumption_tab_is_switched_before_selecting_drinks(self) -> None:
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        navigator = Navigator.__new__(Navigator)
        navigator.threshold = 0.83
        state = {"page": "consumption"}
        taps = []
        navigator.wait = lambda _name: frame
        navigator._save_failure = lambda *_args: None
        navigator.match = lambda _frame, name, _area=None: (
            1.0 if name == "recovery_dialog" or state["page"] == "item" else 0.0, (0, 0)
        )

        def tap(x, y, _reason):
            taps.append((x, y))
            if (x, y) == (802, 42):
                state["page"] = "item"

        navigator.tap = tap
        # 在真正选择饮料前停止，专门验证同一弹窗两个顶部标签的入口切换。
        navigator._check_stop = lambda: (_ for _ in ()).throw(InterruptedError("入口已确认"))
        with patch("project_sekai.navigator.time.sleep"), self.assertRaisesRegex(InterruptedError, "入口已确认"):
            navigator.recover_bonus_from_dialog("large")
        self.assertEqual(taps, [(802, 42)])

    def test_disabled_recovery_never_selects_item(self) -> None:
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        device = SimpleNamespace(screenshot=lambda: frame)
        navigator = Navigator.__new__(Navigator)
        navigator.device = device
        navigator.stop_requested = lambda: False
        navigator.templates = {}
        navigator.threshold = 0.83
        navigator.match = lambda _frame, name, _area=None: (1.0 if name == "recovery_dialog" else 0.0, (0, 0))
        navigator.tap = lambda *_args: self.fail("关闭恢复时不应点击道具或确认")
        with self.assertRaisesRegex(RuntimeError, "自动回复体力已关闭"):
            navigator.wait_for_play_or_recovery(frame, "off")

    def test_unrecognized_item_row_is_rejected_before_click(self) -> None:
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        navigator = Navigator.__new__(Navigator)
        navigator.threshold = 0.83
        navigator.wait = lambda _name: frame
        navigator.match = lambda _frame, name, _area=None: (
            0.0 if name == "recovery_small_row" else 1.0, (0, 0)
        )
        navigator._save_failure = lambda *_args: None
        taps: list[tuple[int, int]] = []
        navigator.tap = lambda x, y, _reason: taps.append((x, y))
        with self.assertRaisesRegex(RuntimeError, "恢复道具页面未确认"):
            navigator.recover_bonus_from_dialog("small")
        self.assertEqual(taps, [])


class ZeroBonusPreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.empty = np.zeros((720, 1280, 3), dtype=np.uint8)
        self.filled = self.empty.copy()
        self.filled[25:60, 1045:1120] = 100
        self.navigator = Navigator.__new__(Navigator)
        self.navigator.device = SimpleNamespace(preflight=lambda: None, screenshot=lambda: self.frame)
        self.navigator.device.back = Mock()
        self.navigator.threshold = 0.83
        self.navigator.stop_requested = lambda: False
        self.navigator.dry_run = False
        self.frame = self.empty
        self.navigator.bonus_reader = lambda frame: (int(frame[25,1045,0]), None)
        self.auto_on = False
        self.taps: list[tuple[int, int]] = []
        self.messages: list[str] = []
        self.navigator.log_message = self.messages.append
        self.navigator._save_failure = Mock()
        self.navigator.navigate_to_prepare = Mock()
        self.navigator.wait = lambda _name, **_kwargs: self.frame
        self.navigator.match = self.match
        self.navigator.tap = self.tap
        self.navigator.recover_bonus_from_dialog = Mock(side_effect=self.recover)
        self.navigator.start_and_collect = Mock(side_effect=self.start)

    def match(self, frame: np.ndarray, name: str, _area=None) -> tuple[float, tuple[int, int]]:
        matches = {"prepare": True, "auto_on": self.auto_on,
                   "auto_off": not self.auto_on}
        return (1.0 if matches.get(name, False) else 0.0), (0, 0)

    def tap(self, x: int, y: int, _reason: str) -> None:
        self.taps.append((x, y))
        if (x, y) == (565, 669) and self.frame is self.filled:
            self.auto_on = True

    def recover(self, _mode: str, count: int = 1) -> None:
        self.frame = self.filled

    def start(self, **kwargs) -> None:
        self.assertTrue(self.auto_on, "补充体力并确认 AUTO 后才能开演")

    def test_zero_bonus_is_recovered_before_auto_and_start(self) -> None:
        with patch("project_sekai.navigator.time.sleep"):
            self.navigator.auto_live_loop(1, recovery_mode="large")
        self.navigator.recover_bonus_from_dialog.assert_called_once_with("large", 1)
        self.assertEqual(self.taps, [(565, 669), (1086, 42), (565, 669)])
        self.assertEqual(self.navigator.device.back.call_count, 2)
        self.navigator.start_and_collect.assert_called_once_with(
            recovery_mode="large", recovery_count=1)
        self.assertEqual(self.navigator.completed_rounds, 1)

    def test_disabled_recovery_does_not_open_popup_or_start(self) -> None:
        with patch("project_sekai.navigator.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "自动回复体力已关闭"):
                self.navigator.auto_live_loop(1, recovery_mode="off")
        self.assertNotIn((1086, 42), self.taps)
        self.navigator.recover_bonus_from_dialog.assert_not_called()
        self.navigator.start_and_collect.assert_not_called()

    def test_nonzero_bonus_does_not_consume_drink(self) -> None:
        self.frame = self.filled
        with patch("project_sekai.navigator.time.sleep"):
            self.navigator.auto_live_loop(1, recovery_mode="large")
        self.navigator.recover_bonus_from_dialog.assert_not_called()
        self.navigator.start_and_collect.assert_called_once_with(
            recovery_mode="large", recovery_count=1)

    def test_recovery_without_bonus_change_never_starts(self) -> None:
        self.navigator.recover_bonus_from_dialog.side_effect = lambda _mode, _count: None
        with patch("project_sekai.navigator.time.sleep"), patch(
                "project_sekai.navigator.time.monotonic", side_effect=range(30)):
            with self.assertRaisesRegex(RuntimeError, "体力未确认足额变化"):
                self.navigator.auto_live_loop(1, recovery_mode="large")
        self.assertEqual(self.taps, [(565, 669), (1086, 42)])
        self.navigator.device.back.assert_called_once()
        self.navigator.start_and_collect.assert_not_called()

    def test_bonus_update_can_arrive_after_ok_dialog_closes(self) -> None:
        readings = iter((self.empty, self.empty, self.filled, self.filled))
        self.navigator.device.screenshot = lambda: next(readings)
        with patch("project_sekai.navigator.time.sleep"):
            after = self.navigator._wait_for_bonus_change(self.empty)
        self.assertIs(after, self.filled)
        self.navigator.recover_bonus_from_dialog.assert_not_called()

    def test_small_bonus_digit_change_is_confirmed_numerically_in_two_frames(self) -> None:
        before = self.filled
        after = before.copy()
        after[35:40, 1070:1075] = 255
        self.navigator.bonus_reader = lambda frame: (5 if frame is before else 6, None)
        self.navigator.device.screenshot = Mock(return_value=after)
        self.assertLess(float(np.abs(before.astype(float) - after).mean()), 2)
        with patch("project_sekai.navigator.time.sleep"):
            result = self.navigator._wait_for_bonus_change(before)
        self.assertIs(result, after)
        self.assertEqual(self.navigator.device.screenshot.call_count, 2)
        self.navigator.recover_bonus_from_dialog.assert_not_called()

    def test_background_change_without_numeric_increase_cannot_confirm_a_drink(self) -> None:
        self.navigator.bonus_reader = lambda frame: (5, None)
        self.navigator.device.screenshot = Mock(return_value=self.filled)
        with patch("project_sekai.navigator.time.sleep"), \
                patch("project_sekai.navigator.time.monotonic", side_effect=range(30)):
            with self.assertRaisesRegex(RuntimeError, "体力未确认足额变化"):
                self.navigator._wait_for_bonus_change(self.empty)
        self.navigator.recover_bonus_from_dialog.assert_not_called()

    def test_bonus_update_waits_for_reliable_equal_increased_readings(self) -> None:
        before = self.filled
        frames = [before.copy() for _ in range(4)]
        for frame in frames:
            frame[35:40, 1070:1075] = 255
        values = iter((ValueError("读数冲突"), 6, 7, 7))
        def read(frame):
            if frame is before:
                return 5, None
            value = next(values)
            if isinstance(value, Exception):
                raise value
            return value, None
        self.navigator.bonus_reader = read
        self.navigator.device.screenshot = Mock(side_effect=frames)
        with patch("project_sekai.navigator.time.sleep"):
            result = self.navigator._wait_for_bonus_change(before)
        self.assertIs(result, frames[-1])
        self.assertEqual(self.navigator.device.screenshot.call_count, 4)

    def test_unreadable_before_value_cannot_consume_another_drink(self) -> None:
        self.navigator.bonus_reader = Mock(side_effect=ValueError("用药前读数冲突"))
        with self.assertRaisesRegex(RuntimeError, "用药前"):
            self.navigator._recover_and_verify(self.filled, "small", 5)
        self.navigator.recover_bonus_from_dialog.assert_not_called()

    def test_five_small_drinks_are_confirmed_once_even_when_stamina_becomes_sufficient_early(self) -> None:
        frames = {value:self.filled.copy() for value in range(2,8)}
        for value, frame in frames.items():
            frame[0,0,0] = value
        state = {'value':2}
        self.navigator.bonus_reader = lambda frame: (int(frame[0,0,0]),None)
        self.navigator.device.screenshot = Mock(side_effect=lambda: frames[state['value']])
        self.navigator.wait = lambda *args,**kwargs: frames[state['value']]
        def recover(mode, count):
            self.assertEqual((mode,count),('small',5))
            state['value'] += count
        self.navigator.recover_bonus_from_dialog = Mock(side_effect=recover)
        progress = []
        with patch('project_sekai.navigator.time.sleep'):
            after = self.navigator._recover_and_verify(frames[2],'small',5,
                on_recovered=lambda count,frame: progress.append((count,int(frame[0,0,0]))))
        self.assertIs(after,frames[7])
        self.assertEqual(progress,[(5,7)])
        self.navigator.recover_bonus_from_dialog.assert_called_once_with("small",5)
        self.assertEqual(self.navigator.device.back.call_count,1)

    def test_confirmed_batch_progress_survives_notice_dismissal_failure(self) -> None:
        before = self.empty
        self.navigator.bonus_reader = lambda frame: (2 if frame is before else 7,None)
        self.navigator._dismiss_bonus_notice = Mock(side_effect=RuntimeError('通知未关闭'))
        progress = []
        with patch('project_sekai.navigator.time.sleep'), self.assertRaisesRegex(RuntimeError,'通知未关闭'):
            self.navigator._recover_and_verify(before,'small',5,
                on_recovered=lambda count,frame: progress.append((count,int(self.navigator.bonus_reader(frame)[0]))))
        self.assertEqual(progress,[(5,7)])
        self.navigator.recover_bonus_from_dialog.assert_called_once_with('small',5)

    def test_partial_increase_never_confirms_the_requested_batch(self) -> None:
        self.navigator.bonus_reader = lambda frame: (2 if frame is self.empty else 3, None)
        progress = Mock()
        with patch("project_sekai.navigator.time.sleep"), patch(
                "project_sekai.navigator.time.monotonic", side_effect=range(30)):
            with self.assertRaisesRegex(RuntimeError, "需增加 5"):
                self.navigator._recover_and_verify(self.empty, "small", 5, on_recovered=progress)
        progress.assert_not_called()
        self.navigator.recover_bonus_from_dialog.assert_called_once_with("small", 5)
        self.assertEqual(self.navigator.recovery_evidence["available_after"], 3)
        self.navigator.device.back.assert_not_called()

    def test_auto_batch_evidence_is_persisted_before_dismissing_notice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.navigator.config_path = Path(directory) / 'templates.json'
            def dismiss(reason):
                saved = json.loads((Path(directory)/'recovery-last.json').read_text(encoding='utf-8'))
                self.assertEqual(saved['completed_bottles'],5)
                self.assertEqual(saved['status'],'credited')
                self.assertEqual(saved['available_before'],0)
                self.assertEqual(saved['available_after'],100)
                raise RuntimeError('关闭失败')
            self.navigator._dismiss_bonus_notice = Mock(side_effect=dismiss)
            with patch('project_sekai.navigator.time.sleep'), self.assertRaisesRegex(RuntimeError,'关闭失败'):
                self.navigator._recover_and_verify(self.empty,'small',5)
        self.navigator.recover_bonus_from_dialog.assert_called_once_with('small',5)

    def test_unconfirmed_ok_batch_blocks_retry_in_same_navigator(self) -> None:
        self.navigator.recovery_evidence = {'status':'started','ok_requested':True,
                                            'requested_bottles':5,'available_before':2,'available_after':5}
        with self.assertRaisesRegex(RuntimeError,'已有 OK 请求'):
            self.navigator._recover_and_verify(self.empty,'small',5)
        self.navigator.recover_bonus_from_dialog.assert_not_called()
        self.assertEqual(self.navigator.recovery_evidence['available_after'],5)

    def test_unreadable_new_batch_does_not_reuse_previous_credited_evidence(self) -> None:
        self.navigator.recovery_evidence = {'status':'credited','ok_requested':True,
            'mode':'large','requested_bottles':2,'completed_bottles':2,'available_after':23}
        self.navigator.bonus_reader = Mock(side_effect=ValueError('读数异常'))
        with self.assertRaisesRegex(RuntimeError,'用药前'):
            self.navigator._recover_and_verify(self.empty,'small',5)
        self.assertEqual(self.navigator.recovery_evidence['mode'],'small')
        self.assertEqual(self.navigator.recovery_evidence['requested_bottles'],5)
        self.assertEqual(self.navigator.recovery_evidence['completed_bottles'],0)
        self.assertNotIn('available_after',self.navigator.recovery_evidence)
        self.navigator.recover_bonus_from_dialog.assert_not_called()

    def test_missing_auto_ocr_model_rejects_before_any_drink_selection(self) -> None:
        self.navigator.bonus_reader = None
        with patch('project_sekai.navigator.LineOcr',side_effect=FileNotFoundError('缺少离线模型')):
            with self.assertRaisesRegex(FileNotFoundError,'缺少离线模型'):
                self.navigator._recover_and_verify(self.empty,'small',5)
        self.navigator.recover_bonus_from_dialog.assert_not_called()
        self.navigator.device.back.assert_not_called()

    def test_auto_without_drink_use_never_loads_ocr_model(self) -> None:
        self.frame = self.filled
        self.navigator.bonus_reader = None
        with patch('project_sekai.navigator.LineOcr') as model, patch('project_sekai.navigator.time.sleep'):
            self.navigator.auto_live_loop(1,recovery_mode='off')
        model.assert_not_called()

    def test_recovery_runs_again_when_a_later_round_needs_bonus(self) -> None:
        def next_round(_mode: str) -> None:
            self.frame = self.empty
            self.auto_on = False
        self.navigator.navigate_to_prepare.side_effect = next_round
        with patch("project_sekai.navigator.time.sleep"):
            self.navigator.auto_live_loop(2, recovery_mode="large")
        self.assertEqual(self.navigator.recover_bonus_from_dialog.call_count, 2)
        self.assertEqual(self.navigator.start_and_collect.call_count, 2)
        self.assertEqual(self.navigator.completed_rounds, 2)

    def test_specified_quantity_is_consumed_before_auto_retry(self) -> None:
        def recover_one(_mode: str, count: int) -> None:
            self.filled = self.frame.copy()
            self.filled[25:60, 1045:1120] += 60
            self.frame = self.filled
        self.navigator.recover_bonus_from_dialog.side_effect = recover_one
        with patch("project_sekai.navigator.time.sleep"):
            self.navigator.auto_live_loop(1, recovery_mode="small", recovery_count=3)
        self.navigator.recover_bonus_from_dialog.assert_called_once_with("small",3)
        self.navigator.start_and_collect.assert_called_once_with(
            recovery_mode="small", recovery_count=3)
        self.assertEqual(self.taps.count((1086, 42)), 1)
        self.assertEqual(self.taps[-1], (565, 669))
        self.assertEqual(self.navigator.device.back.call_count, 2)

    def test_rejected_again_after_recovery_does_not_consume_more(self) -> None:
        self.navigator.tap = lambda x, y, _reason: self.taps.append((x, y))
        with patch("project_sekai.navigator.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "点击 AUTO 后未开启"):
                self.navigator.auto_live_loop(1, recovery_mode="large")
        self.navigator.recover_bonus_from_dialog.assert_called_once_with("large", 1)
        self.navigator.start_and_collect.assert_not_called()

    def test_modal_auto_prompt_is_closed_with_back(self) -> None:
        modal = [False]
        def tap(x: int, y: int, reason: str) -> None:
            self.tap(x, y, reason)
            if (x, y) == (565, 669) and self.frame is self.empty:
                modal[0] = True
        original_match = self.match
        self.navigator.tap = tap
        self.navigator.match = lambda frame, name, area=None: (
            (0.0, (0, 0)) if name == "prepare" and modal[0] else original_match(frame, name, area))
        self.navigator.device.back = Mock(side_effect=lambda: modal.__setitem__(0, False))
        with patch("project_sekai.navigator.time.sleep"):
            self.navigator.auto_live_loop(1, recovery_mode="large")
        self.assertEqual(self.navigator.device.back.call_count, 2)
        self.assertNotIn((1279, 719), self.taps)

    def test_stop_before_notice_dismissal_never_sends_back(self) -> None:
        self.navigator.stop_requested = lambda: True
        with self.assertRaises(InterruptedError):
            self.navigator._dismiss_bonus_notice("关闭体力回复完成提示")
        self.navigator.device.back.assert_not_called()

    def test_recovery_notice_is_dismissed_before_retrying_auto(self) -> None:
        events = []
        self.navigator.device.back.side_effect = lambda: events.append("esc")
        def recover(mode, count):
            events.append("recover")
            self.recover(mode, count)
        def tap(x, y, reason):
            if (x, y) == (565, 669):
                events.append("auto")
            self.tap(x, y, reason)
        self.navigator.recover_bonus_from_dialog.side_effect = recover
        self.navigator.tap = tap
        with patch("project_sekai.navigator.time.sleep"):
            self.navigator.auto_live_loop(1, recovery_mode="large")
        self.assertEqual(events, ["auto", "esc", "recover", "esc", "auto"])


class ProgressTests(unittest.TestCase):
    def test_failed_round_does_not_increment_completed_count(self) -> None:
        navigator = Navigator.__new__(Navigator)
        navigator.device = SimpleNamespace(preflight=lambda: None)
        navigator.stop_requested = lambda: False
        navigator.dry_run = False
        navigator.navigate_to_prepare = Mock()
        navigator.prepare_auto = Mock()
        navigator.start_and_collect = Mock(side_effect=[0, RuntimeError("第二局失败")])
        messages: list[str] = []
        navigator.log_message = messages.append
        with self.assertRaisesRegex(RuntimeError, "第二局失败"):
            navigator.auto_live_loop(3)
        self.assertEqual(navigator.completed_rounds, 1)
        self.assertEqual(messages, ["自动演出：已完成 0 / 总数 3", "自动演出：已完成 1 / 总数 3"])

    def test_progress_is_sent_to_mfa_log_without_toast(self) -> None:
        context = SimpleNamespace(tasker=SimpleNamespace(stopping=False), run_task=Mock(
            return_value=SimpleNamespace(status=SimpleNamespace(succeeded=True))))
        auto_live._visible_log(context, "自动演出：已完成 2 / 总数 3")
        entry, override = context.run_task.call_args.args
        self.assertEqual(entry, "AutoLiveLog")
        focus = override[entry]["focus"]["Node.Action.Succeeded"]
        self.assertEqual(focus["display"], ["log"])
        self.assertIn("已完成 2 / 总数 3", focus["content"])


if __name__ == "__main__":
    unittest.main()
