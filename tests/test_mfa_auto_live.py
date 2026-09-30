from __future__ import annotations

import json
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
        self.assertEqual([task["name"] for task in interface["task"]], ["AutoLive"])
        self.assertEqual(
            interface["task"][0]["option"],
            ["AutoLiveSongMode", "AutoLiveCount", "AutoLiveRecoveryMode", "AutoLiveRecoveryLimit"],
        )
        self.assertEqual(interface["option"]["AutoLiveSongMode"]["default_case"], "Current")
        self.assertEqual(
            [case["label"] for case in interface["option"]["AutoLiveSongMode"]["cases"]],
            ["当前歌曲", "随机选取"],
        )

    def test_mode_limit_and_rounds_reach_navigator(self) -> None:
        task_id = 731
        context = SimpleNamespace(tasker=SimpleNamespace(controller=object(), stopping=False))
        self.assertTrue(auto_live.ProjectSekaiRecoveryModeConfig().run(context, argument(task_id, {"mode": "small"})))
        self.assertTrue(auto_live.ProjectSekaiRecoveryLimitConfig().run(context, argument(task_id, {"limit": 3})))
        self.assertTrue(auto_live.ProjectSekaiSongModeConfig().run(context, argument(task_id, {"mode": "random"})))
        with patch.object(auto_live, "MaaDevice"), patch.object(auto_live, "Navigator") as navigator_type:
            self.assertTrue(auto_live.ProjectSekaiAutoLive().run(context, argument(task_id, {"count": 5})))
            navigator_type.return_value.auto_live_loop.assert_called_once_with(
                5, song_mode="random", recovery_mode="small", recovery_limit=3
            )
        self.assertNotIn(task_id, auto_live._SETTINGS)

    def test_invalid_limit_does_not_start_auto_live(self) -> None:
        task_id = 732
        context = SimpleNamespace(tasker=SimpleNamespace(controller=object(), stopping=False))
        self.assertTrue(auto_live.ProjectSekaiRecoveryModeConfig().run(context, argument(task_id, {"mode": "large"})))
        self.assertFalse(auto_live.ProjectSekaiRecoveryLimitConfig().run(context, argument(task_id, {"limit": 0})))
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
    def test_disabled_recovery_never_selects_item(self) -> None:
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        device = SimpleNamespace(screenshot=lambda: frame)
        navigator = Navigator.__new__(Navigator)
        navigator.device = device
        navigator.stop_requested = lambda: False
        navigator.threshold = 0.83
        navigator.match = lambda _frame, name, _area=None: (1.0 if name == "recovery_dialog" else 0.0, (0, 0))
        navigator.tap = lambda *_args: self.fail("关闭恢复时不应点击道具或确认")
        for mode, remaining in (("off", 1), ("small", 0)):
            with self.subTest(mode=mode, remaining=remaining):
                with self.assertRaisesRegex(RuntimeError, "未获准使用恢复道具或次数已达上限"):
                    navigator.wait_for_play_or_recovery(frame, mode, remaining)

    def test_existing_item_selection_is_rejected_before_click(self) -> None:
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        navigator = Navigator.__new__(Navigator)
        navigator.threshold = 0.83
        navigator.wait = lambda _name: frame
        navigator.match = lambda _frame, name, _area=None: (
            0.0 if name == "recovery_large_zero" else 1.0, (0, 0)
        )
        navigator._save_failure = lambda *_args: None
        taps: list[tuple[int, int]] = []
        navigator.tap = lambda x, y, _reason: taps.append((x, y))
        with self.assertRaisesRegex(RuntimeError, "初始选择数量不是零"):
            navigator.recover_bonus_from_dialog("small")
        self.assertEqual(taps, [(461, 104)])


class ProgressTests(unittest.TestCase):
    def test_failed_round_does_not_increment_completed_count(self) -> None:
        navigator = Navigator.__new__(Navigator)
        navigator.device = SimpleNamespace(preflight=lambda: None)
        navigator.stop_requested = lambda: False
        navigator.dry_run = False
        navigator.navigate_to_prepare = Mock()
        navigator.ensure_auto = Mock()
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
