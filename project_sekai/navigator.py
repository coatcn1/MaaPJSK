from __future__ import annotations

import json
from pathlib import Path
import time
from collections.abc import Callable

import cv2
import numpy as np

from .device import AdbDevice


class Navigator:
    def __init__(self, device: AdbDevice, config_path: str | Path, *, dry_run: bool = False,
                 stop_requested: Callable[[], bool] | None = None,
                 log_message: Callable[[str], None] | None = None) -> None:
        self.device = device
        self.config_path = Path(config_path).resolve()
        self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.dry_run = dry_run
        self.stop_requested = stop_requested or (lambda: False)
        self.log_message = log_message or (lambda message: print(message, flush=True))
        self.completed_rounds = 0
        self.templates: dict[str, np.ndarray] = {}
        for name, relative_path in self.config["templates"].items():
            path = (self.config_path.parent / relative_path).resolve()
            image = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError(f"模板无法读取：{path}")
            self.templates[name] = image
        self.threshold = float(self.config.get("threshold", 0.83))

    def match(self, frame: np.ndarray, name: str, area: tuple[int, int, int, int] | None = None) -> tuple[float, tuple[int, int]]:
        template = self.templates[name]
        if area is None:
            area = (0, 0, frame.shape[1], frame.shape[0])
        x1, y1, x2, y2 = area
        crop = frame[y1:y2, x1:x2]
        if crop.shape[0] < template.shape[0] or crop.shape[1] < template.shape[1]:
            return 0.0, (0, 0)
        result = cv2.matchTemplate(crop, template, cv2.TM_CCOEFF_NORMED)
        _, score, _, location = cv2.minMaxLoc(result)
        return float(score), (location[0] + x1, location[1] + y1)

    def wait(self, name: str, timeout: float = 20) -> np.ndarray:
        deadline = time.monotonic() + timeout
        best = 0.0
        frame = None
        while time.monotonic() < deadline:
            self._check_stop()
            frame = self.device.screenshot()
            score, _ = self.match(frame, name)
            best = max(best, score)
            if score >= self.threshold:
                return frame
            time.sleep(0.8)
        self._save_failure(frame, name)
        raise TimeoutError(f"等待 {name} 超时，最高匹配分数 {best:.3f}")

    def _check_stop(self) -> None:
        if self.stop_requested():
            raise InterruptedError("用户已停止任务")

    def _save_failure(self, frame: np.ndarray, name: str) -> None:
        destination = self.config_path.parent / f"failure-{name}.png"
        cv2.imwrite(str(destination), frame)

    def tap(self, x: int, y: int, reason: str) -> None:
        self._check_stop()
        print(f"点击 {reason}: ({x}, {y})" + (" [预演]" if self.dry_run else ""), flush=True)
        if not self.dry_run:
            self.device.tap(x, y)

    def return_to_home(self, *, max_backs: int = 12) -> None:
        # 每次输入前确认页面；演奏场仍在运行时停止返回，避免 ESC 暂停或放弃当前演出。
        for backs in range(max_backs + 1):
            self._check_stop()
            frame = self.device.screenshot()
            if self.match(frame, "home")[0] >= self.threshold:
                time.sleep(0.4)
                self._check_stop()
                if self.match(self.device.screenshot(), "home")[0] >= self.threshold:
                    return
            if "playing" in self.templates and self.match(frame, "playing")[0] >= self.threshold:
                raise RuntimeError("当前仍在演奏中，停止主页返回，请待演出结束后再启动")
            if self.dry_run or backs >= max_backs:
                self._save_failure(frame, "return_home")
                raise RuntimeError(f"未识别到主页，ESC 返回次数={backs}")
            self._check_stop()
            self.log_message(f"未识别到主页，ESC 返回 {backs + 1}/{max_backs}")
            self.device.back()
            time.sleep(1.0)

    def _select_random_song(self, before: np.ndarray) -> None:
        # 随机选曲仍停留在选曲页；用右侧封面变化确认按钮生效，避免沿用上一局的歌曲。
        x1, y1, x2, y2 = (908, 80, 1161, 338)
        previous_cover = before[y1:y2, x1:x2].astype("float32")
        for _ in range(3):
            self.tap(943, 652, "随机选曲")
            time.sleep(0.6)
            frame = self.wait("song_select")
            current_cover = frame[y1:y2, x1:x2].astype("float32")
            difference = float(np.abs(previous_cover - current_cover).mean())
            if difference >= 8.0:
                print(f"随机歌曲已切换，封面变化分数={difference:.1f}", flush=True)
                return
        self._save_failure(frame, "random_song")
        raise RuntimeError("点击随机选曲后未确认歌曲变化")

    def navigate_to_prepare(self, song_mode: str = "current") -> None:
        if song_mode not in {"current", "random"}:
            raise ValueError("选曲方式无效")
        self.return_to_home()
        self.tap(1194, 649, "主页 Live")
        if self.dry_run:
            return
        self.wait("live_menu")
        self.tap(722, 235, "单人 Live")
        frame = self.wait("song_select")
        if song_mode == "random":
            self._select_random_song(frame)
        self.tap(1175, 493, "Master 难度")
        frame = self.wait("song_select")
        if self.match(frame, "master_selected")[0] < self.threshold:
            self._save_failure(frame, "master_selected")
            raise RuntimeError("Master 难度未确认")
        self.tap(1007, 590, "选曲确认")
        self.wait("prepare")

    def ensure_auto(self, enabled: bool) -> None:
        frame = self.wait("prepare")
        wanted = "auto_on" if enabled else "auto_off"
        if self.match(frame, wanted)[0] >= self.threshold:
            return
        opposite = "auto_off" if enabled else "auto_on"
        if self.match(frame, opposite)[0] < self.threshold:
            self._save_failure(frame, "auto_state")
            raise RuntimeError("无法确认自动演奏开关状态")
        self.tap(565, 669, "自动演奏开关")
        if not self.dry_run:
            time.sleep(0.5)
            frame = self.wait("prepare")
            if self.match(frame, wanted)[0] < self.threshold:
                raise RuntimeError("AUTO LIVE 未启用；请检查 Live Bonus 是否至少设置为 1")

    def recover_bonus_from_dialog(self, mode: str) -> None:
        # 只允许已确认的道具页和一瓶饮料；水晶页不在自动恢复路径中。
        if mode not in {"small", "large"}:
            raise ValueError("体力恢复仅支持 small 或 large 道具")
        self.wait("recovery_dialog")
        self.tap(461, 104, "道具恢复页")
        frame = self.wait("recovery_item_tab")
        row = f"recovery_{mode}_row"
        if self.match(frame, row)[0] < self.threshold:
            self._save_failure(frame, "recovery_item")
            raise RuntimeError("恢复道具页面未确认")
        small_area = (828, 215, 942, 284)
        large_area = (828, 350, 942, 424)
        if (self.match(frame, "recovery_small_zero", small_area)[0] < self.threshold or
                self.match(frame, "recovery_large_zero", large_area)[0] < self.threshold):
            self._save_failure(frame, "recovery_initial_count")
            raise RuntimeError("恢复页初始选择数量不是零，停止以避免消耗多瓶道具")
        plus_y = 247 if mode == "small" else 386
        self.tap(794, plus_y, "选择一瓶恢复饮料")
        selected = f"recovery_{mode}_selected"
        area = small_area if mode == "small" else large_area
        other = "large" if mode == "small" else "small"
        other_area = large_area if mode == "small" else small_area
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self._check_stop()
            frame = self.device.screenshot()
            if self.match(frame, selected, area)[0] >= self.threshold:
                break
            time.sleep(0.3)
        else:
            self._save_failure(frame, "recovery_selection")
            raise RuntimeError("未确认已选择一瓶恢复饮料")
        if self.match(frame, f"recovery_{other}_zero", other_area)[0] < self.threshold:
            self._save_failure(frame, "recovery_other_count")
            raise RuntimeError("另一种恢复饮料的选择数量不是零")
        if self.match(frame, "recovery_confirm_enabled", (625, 612, 890, 694))[0] < self.threshold:
            raise RuntimeError("恢复确认按钮未启用")
        self.tap(756, 655, "确认消耗一瓶恢复饮料")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            self._check_stop()
            frame = self.device.screenshot()
            if self.match(frame, "recovery_dialog")[0] < self.threshold:
                return
            time.sleep(0.5)
        self._save_failure(frame, "recovery_confirmation")
        raise TimeoutError("确认恢复后窗口仍未关闭")

    @staticmethod
    def _verify_bonus_changed(before: np.ndarray, after: np.ndarray) -> None:
        before_bonus = before[25:60, 1045:1120].astype("float32")
        after_bonus = after[25:60, 1045:1120].astype("float32")
        if float(np.abs(before_bonus - after_bonus).mean()) < 2.0:
            raise RuntimeError("恢复窗口已关闭，但体力显示没有变化")

    def wait_for_play_or_recovery(self, before: np.ndarray, mode: str, remaining: int) -> int:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            self._check_stop()
            frame = self.device.screenshot()
            if self.match(frame, "playing")[0] >= self.threshold:
                return remaining
            if self.match(frame, "recovery_dialog")[0] >= self.threshold:
                if mode == "off" or remaining <= 0:
                    raise RuntimeError("游戏请求恢复体力；MFA 未获准使用恢复道具或次数已达上限")
                self.recover_bonus_from_dialog(mode)
                after = self.wait("prepare", timeout=20)
                self._verify_bonus_changed(before, after)
                self.tap(1010, 558, "恢复体力后重新开始演出")
                before = self.device.screenshot()
                remaining -= 1
                deadline = time.monotonic() + 45
            time.sleep(0.5)
        self._save_failure(frame, "start_or_recovery")
        raise TimeoutError("开演后未出现演奏场或已知体力恢复页")

    def collect_with_back(self, *, timeout: float = 180) -> None:
        # 结算中只检查主页终点。右下角像素用于加速动画，BACK 负责推进页面。
        deadline = time.monotonic() + timeout
        backs = 0
        while time.monotonic() < deadline:
            self._check_stop()
            frame = self.device.screenshot()
            if self.match(frame, "home")[0] >= self.threshold:
                time.sleep(0.5)
                if self.match(self.device.screenshot(), "home")[0] >= self.threshold:
                    print(f"已回到主页，BACK 次数={backs}", flush=True)
                    return
            self.tap(1279, 719, "结算动画安全像素")
            time.sleep(0.25)
            frame = self.device.screenshot()
            if self.match(frame, "home")[0] >= self.threshold:
                continue
            print("结算 Android BACK（ESC）", flush=True)
            self.device.back()
            backs += 1
            self.tap(1279, 719, "BACK 后安全像素")
            time.sleep(0.85)
        self._save_failure(frame, "result_to_home")
        raise TimeoutError(f"结算 {timeout:.0f} 秒后仍未确认主页，BACK 次数={backs}")

    def start_and_collect(self, *, max_song_seconds: float = 300,
                          recovery_mode: str = "off", recovery_remaining: int = 0) -> int:
        before = self.wait("prepare")
        self.tap(1010, 558, "开始演出")
        if self.dry_run:
            return recovery_remaining
        # 开演后必须在短时间内看到演奏场，体力不足或弹窗不会误耗完整曲目超时。
        recovery_remaining = self.wait_for_play_or_recovery(before, recovery_mode, recovery_remaining)
        self.wait("live_clear", timeout=max_song_seconds)
        self.collect_with_back()
        return recovery_remaining

    def auto_live_loop(self, rounds: int, *, song_mode: str = "current",
                       recovery_mode: str = "off", recovery_limit: int = 0) -> None:
        if rounds < 1:
            raise ValueError("rounds 必须至少为 1")
        if song_mode not in {"current", "random"}:
            raise ValueError("选曲方式无效")
        if recovery_mode not in {"off", "small", "large"} or not 0 <= recovery_limit <= 99:
            raise ValueError("恢复配置无效")
        self.device.preflight()
        self.completed_rounds = 0
        self.log_message(f"自动演出：已完成 0 / 总数 {rounds}")
        for index in range(rounds):
            self._check_stop()
            print(f"第 {index + 1}/{rounds} 局", flush=True)
            self.navigate_to_prepare(song_mode)
            if self.dry_run:
                return
            self.ensure_auto(True)
            recovery_limit = self.start_and_collect(
                recovery_mode=recovery_mode,
                recovery_remaining=recovery_limit,
            )
            self.completed_rounds += 1
            self.log_message(f"自动演出：已完成 {self.completed_rounds} / 总数 {rounds}")
