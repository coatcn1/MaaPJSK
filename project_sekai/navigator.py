from __future__ import annotations

import json
import re
from pathlib import Path
import time
from collections.abc import Callable

import cv2
import numpy as np

from .device import AdbDevice
from .chart_catalog import _write_json
from .ocr import LineOcr, Reading


def read_available_bonus(ocr: LineOcr, frame: np.ndarray) -> tuple[int, Reading]:
    box = (1057, 27, 1128, 59)
    raw = ocr.read(frame, box)
    x1, y1, x2, y2 = box
    hsv = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (0, 0, 190), (179, 95, 255))
    digits = cv2.cvtColor(255 - mask, cv2.COLOR_GRAY2BGR)
    digits = cv2.copyMakeBorder(digits, 4, 4, 4, 4, cv2.BORDER_CONSTANT, value=(255, 255, 255))
    processed = ocr.read(digits, (0, 0, digits.shape[1], digits.shape[0]))
    values = []
    for reading in (raw, processed):
        match = re.fullmatch(r"(\d{1,4})[/／](\d{1,4})", re.sub(r"\s+", "", reading.text))
        if reading.confidence >= .75 and match and int(match[2]) > 0:
            values.append((int(match[1]), reading))
    # 只读取顶部实际体力，不识别饮料库存或已选瓶数；冲突读数不能触发用药。
    if not values or len({value for value, _ in values}) != 1:
        raise ValueError(f"当前体力无法确认：{raw}，{processed}")
    return values[0]


class AutoEnableRejected(RuntimeError):
    """点击 AUTO 后未进入开启状态，需要关闭提示并处理体力。"""


class Navigator:
    def __init__(self, device: AdbDevice, config_path: str | Path, *, dry_run: bool = False,
                 stop_requested: Callable[[], bool] | None = None,
                 log_message: Callable[[str], None] | None = None,
                 bonus_reader: Callable[[np.ndarray], tuple[int, object]] | None = None) -> None:
        self.device = device
        self.config_path = Path(config_path).resolve()
        self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.dry_run = dry_run
        self.stop_requested = stop_requested or (lambda: False)
        self.log_message = log_message or (lambda message: print(message, flush=True))
        self.bonus_reader = bonus_reader
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
            self._check_title_screen(frame)
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

    def _check_title_screen(self, frame: np.ndarray) -> None:
        # 返回标题页后无法确认本局结果，继续 ESC 既不能登录，也不能可靠地增加完成次数。
        if "title_screen" in self.templates and self.match(frame, "title_screen")[0] >= self.threshold:
            self._save_failure(frame, "unexpected_title")
            raise RuntimeError("游戏已返回 TAP TO START 标题页，无法确认演出结果；停止 ESC 返回，完成次数保持不变")

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
            self._check_title_screen(frame)
            if self.match(frame, "home")[0] >= self.threshold:
                time.sleep(0.4)
                self._check_stop()
                confirmation = self.device.screenshot()
                self._check_title_screen(confirmation)
                if self.match(confirmation, "home")[0] >= self.threshold:
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
            frame = self.device.screenshot()
            if (self.match(frame, "prepare")[0] < self.threshold or
                    self.match(frame, wanted)[0] < self.threshold):
                if enabled:
                    raise AutoEnableRejected("点击 AUTO 后未开启")
                raise RuntimeError("AUTO 开关未进入指定状态")
            self.log_message("AUTO 已开启" if enabled else "AUTO 已关闭")

    def _dismiss_auto_prompt(self) -> None:
        self._dismiss_bonus_notice("关闭 AUTO 体力不足提示")

    def _dismiss_bonus_notice(self, reason: str) -> np.ndarray:
        # 这两类通知均由 BACK 关闭；只发一次，随后确认准备页，避免连续返回。
        self._check_stop()
        self.log_message(f"{reason}：ESC/BACK")
        self._check_stop()
        self.device.back()
        time.sleep(0.5)
        return self.wait("prepare")

    def prepare_auto(self, mode: str, count: int = 1) -> None:
        before = self.wait("prepare")
        try:
            self.ensure_auto(True)
            return
        except AutoEnableRejected:
            self.log_message("AUTO 未开启，关闭提示后按设置回复体力")
            self._dismiss_auto_prompt()
            if mode == "off":
                raise RuntimeError("AUTO 未开启；自动回复体力已关闭")
            self.tap(1086, 42, "打开体力回复")
            self._recover_and_verify(before, mode, count)
        # 本次规定数量全部回复后只重试一次，避免其他错误导致无限用药。
        self.ensure_auto(True)

    def recover_bonus_from_dialog(self, mode: str, count: int = 1) -> None:
        # 同页选择整批饮料后只提交一次；不识别库存或已选数量，水晶页不在恢复路径中。
        if mode not in {"small", "large"}:
            raise ValueError("体力恢复仅支持 small 或 large 道具")
        if type(count) is not int or not 1 <= count <= 99:
            raise ValueError("每次回复饮料数量必须为 1 到 99 的整数")
        frame = self.wait("recovery_dialog")
        # 默认已在道具页时不重复切换标签，避免页面刷新吞掉紧接着的加号点击。
        if self.match(frame, "recovery_item_tab")[0] < self.threshold:
            # 消耗与回复标题始终可见，标题匹配不能证明回复页已打开；先切换顶部标签。
            self.tap(802, 42, "选择体力回复标签")
            time.sleep(0.4)
            frame = self.wait("recovery_dialog")
            if self.match(frame, "recovery_item_tab")[0] < self.threshold:
                self.tap(461, 104, "道具恢复页")
        frame = self.wait("recovery_item_tab")
        row = f"recovery_{mode}_row"
        if self.match(frame, row)[0] < self.threshold:
            self._save_failure(frame, "recovery_item")
            raise RuntimeError("恢复道具页面未确认")
        plus_y = 247 if mode == "small" else 386
        # 标题和标签可在动画结束前匹配成功，先等待界面稳定。
        time.sleep(0.5)
        confirm_location = None
        for attempt in range(1, 4):
            self._check_stop()
            frame = self.device.screenshot()
            if (self.match(frame, "recovery_dialog")[0] < self.threshold or
                    self.match(frame, "recovery_item_tab")[0] < self.threshold):
                self._save_failure(frame, "recovery_selection")
                raise RuntimeError("选择饮料时道具回复页已关闭")
            # 每次重试均复位两条滑条，避免第一次输入延迟生效后再次加号叠加用药。
            self.tap(534, 247, "小饮料滑条复位")
            self.tap(534, 386, "大饮料滑条复位")
            time.sleep(0.3)
            self.log_message(f"体力回复：同页选择 {count} 瓶饮料，尝试 {attempt}/3")
            for index in range(count):
                self._check_stop()
                frame = self.device.screenshot()
                if (self.match(frame, "recovery_dialog")[0] < self.threshold or
                        self.match(frame, "recovery_item_tab")[0] < self.threshold):
                    self._save_failure(frame, "recovery_selection")
                    raise RuntimeError("批量选择饮料时道具回复页已关闭")
                self.tap(794, plus_y, f"选择恢复饮料 {index + 1}/{count}")
                # 加号存在去抖；逐次等待输入完成，不把连续请求压成一次选择。
                time.sleep(0.3)
            deadline = time.monotonic() + 2.5
            while time.monotonic() < deadline:
                self._check_stop()
                frame = self.device.screenshot()
                if self.match(frame, "recovery_dialog")[0] < self.threshold:
                    self._save_failure(frame, "recovery_selection")
                    raise RuntimeError("选择饮料时回复页已关闭")
                # 广告回复入口出现时，决定按钮会右移；使用实际匹配位置，不点广告按钮。
                score, location = self.match(frame, "recovery_confirm_enabled", (270, 612, 1000, 695))
                if score >= self.threshold:
                    confirm_location = location
                    break
                time.sleep(0.3)
            if confirm_location is not None:
                break
        else:
            self._save_failure(frame, "recovery_selection")
            raise RuntimeError(f"选择 {count} 瓶饮料后决定按钮未启用")
        height, width = self.templates["recovery_confirm_enabled"].shape[:2]
        self.tap(confirm_location[0] + width // 2, confirm_location[1] + height // 2, f"提交 {count} 瓶恢复饮料选择")
        self._confirm_bonus_recovery()

    def _confirm_bonus_recovery(self) -> None:
        # 决定只打开二次确认；必须识别 Live Bonus 提示和 OK，再确认回到准备页。
        deadline = time.monotonic() + 20
        ok_requested = False
        time.sleep(0.5)
        while time.monotonic() < deadline:
            self._check_stop()
            frame = self.device.screenshot()
            prompt = self.match(frame, "recovery_ok_dialog", (300, 270, 965, 385))[0]
            score, location = self.match(frame, "recovery_ok_button", (640, 389, 882, 460))
            if prompt >= self.threshold and score >= self.threshold:
                if not ok_requested:
                    height, width = self.templates["recovery_ok_button"].shape[:2]
                    # 请求前占用本批唯一确认；回执失败或服务器迟到都不能再次消耗整批。
                    ok_requested = True
                    self.recovery_evidence = dict(getattr(self, "recovery_evidence", {}), ok_requested=True)
                    self._persist_recovery_evidence()
                    self.tap(location[0] + width // 2, location[1] + height // 2, "确认体力回复 OK")
            elif (ok_requested and self.match(frame, "prepare")[0] >= self.threshold
                  and self.match(frame, "recovery_dialog")[0] < self.threshold):
                return
            time.sleep(0.5)
        self._save_failure(frame, "recovery_ok")
        raise TimeoutError("体力回复 OK 确认未完成或未返回准备页")

    def _bonus_reader(self):
        reader = getattr(self, "bonus_reader", None)
        if reader is None:
            # AUTO 与谱面任务复用同一数字门槛；仅实际用药时加载本项目离线模型。
            if not hasattr(self, "bonus_ocr"):
                self.bonus_ocr = LineOcr(Path(__file__).resolve().parent.parent / "resource/models/song_title_ocr")
            reader = lambda frame: read_available_bonus(self.bonus_ocr, frame)
        return reader

    def _persist_recovery_evidence(self) -> None:
        path = getattr(self, "config_path", None)
        if path is not None:
            # AUTO 没有单局谱面报告，同样先原子保存本批用药证据再关闭提示。
            _write_json(path.parent / "recovery-last.json", self.recovery_evidence)

    def _wait_for_bonus_change(self, before: np.ndarray, *, expected_increase: int = 1) -> np.ndarray:
        # OK 弹窗先关闭，服务器回复和顶部体力可能稍后更新；等待结果而不再次用药。
        reader = self._bonus_reader()
        before_value = reader(before)[0]
        previous_value = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self._check_stop()
            after = self.device.screenshot()
            if self.match(after, "prepare")[0] >= self.threshold:
                # 只有足额增量才能把整批记为已用；一瓶变化不能证明设置的 N 瓶到账。
                try:
                    value = reader(after)[0]
                except ValueError:
                    previous_value = None
                else:
                    self.recovery_evidence = dict(getattr(self, "recovery_evidence", {}),
                                                  available_before=before_value, available_after=value,
                                                  expected_increase=expected_increase)
                    if value >= before_value + expected_increase and value == previous_value:
                        return after
                    previous_value = value if value >= before_value + expected_increase else None
            else:
                previous_value = None
            time.sleep(0.3)
        self._save_failure(after, "recovery_bonus_update")
        raise RuntimeError(f"确认 OK 后等待 10 秒，体力未确认足额变化：用药前 {before_value}，"
                           f"需增加 {expected_increase}，最后读数 {getattr(self, 'recovery_evidence', {}).get('available_after')}")

    def _recover_and_verify(self, before: np.ndarray, mode: str, count: int = 1, *,
                            on_recovered: Callable[[int, np.ndarray], None] | None = None) -> np.ndarray:
        if mode == "off":
            raise RuntimeError("游戏请求恢复体力；自动回复体力已关闭")
        if mode not in {"small", "large"} or type(count) is not int or not 1 <= count <= 99:
            raise ValueError("体力回复种类或本批数量无效")
        previous = getattr(self, "recovery_evidence", None)
        if (isinstance(previous, dict) and previous.get("ok_requested")
                and previous.get("status") == "started"):
            # 同一任务的普通恢复会重建单局报告，但可能已消费的未确认批不能因此重新发起。
            raise RuntimeError("本任务已有 OK 请求但整批到账未确认，请用户处理；不重复使用另一批饮料")
        expected = count * (1 if mode == "small" else 10)
        # 本批证据在任何可失败操作前换新，不能把上局已到账证据拼到本局失败报告。
        self.recovery_evidence = {"mode": mode, "requested_bottles": count, "available_before": None,
                                  "expected_increase": expected, "ok_requested": False,
                                  "completed_bottles": 0, "status": "started"}
        label = "小饮料" if mode == "small" else "大饮料"
        self.log_message(f"体力回复：本次使用 {count} 瓶{label}")
        self._check_stop()
        try:
            available, _ = self._bonus_reader()(before)
        except ValueError as error:
            raise RuntimeError("用药前实际体力未确认，停止追加饮料") from error
        self.recovery_evidence["available_before"] = available
        self._persist_recovery_evidence()
        try:
            self.recover_bonus_from_dialog(mode, count)
            after = self._wait_for_bonus_change(before, expected_increase=expected)
        except Exception:
            self._persist_recovery_evidence()
            raise
        # 整批到账先保存，关闭完成提示失败时也不能遗失已用瓶数或重做本批。
        self.recovery_evidence.update(completed_bottles=count, status="credited")
        self._persist_recovery_evidence()
        if on_recovered is not None:
            on_recovered(count, after)
        after = self._dismiss_bonus_notice("关闭体力回复完成提示")
        self.log_message(f"体力回复成功：本批 {count} 瓶{label}")
        return after

    def wait_for_play_or_recovery(self, before: np.ndarray, mode: str, count: int = 1) -> None:
        deadline = time.monotonic() + 45
        recovered = False
        while time.monotonic() < deadline:
            self._check_stop()
            frame = self.device.screenshot()
            self._check_title_screen(frame)
            if self.match(frame, "playing")[0] >= self.threshold:
                return
            if self.match(frame, "recovery_dialog")[0] >= self.threshold:
                if recovered:
                    raise RuntimeError("回复体力后再次出现回复页，停止以免重复消耗")
                before = self._recover_and_verify(before, mode, count)
                recovered = True
                self.ensure_auto(True)
                self.tap(1010, 558, "恢复体力后重新开始演出")
                deadline = time.monotonic() + 45
            time.sleep(0.5)
        self._save_failure(frame, "start_or_recovery")
        raise TimeoutError("开演后未出现演奏场或已知体力恢复页")

    def collect_with_back(self, *, timeout: float = 180) -> None:
        # 主页仍是结算终点；标题页表示结果无法确认，必须停止而不能把重新登录计为完成。
        deadline = time.monotonic() + timeout
        backs = 0
        while time.monotonic() < deadline:
            self._check_stop()
            frame = self.device.screenshot()
            self._check_title_screen(frame)
            if self.match(frame, "home")[0] >= self.threshold:
                time.sleep(0.5)
                self._check_stop()
                confirmation = self.device.screenshot()
                self._check_title_screen(confirmation)
                if self.match(confirmation, "home")[0] >= self.threshold:
                    print(f"已回到主页，BACK 次数={backs}", flush=True)
                    return
            self.tap(1279, 719, "结算动画安全像素")
            time.sleep(0.25)
            frame = self.device.screenshot()
            self._check_title_screen(frame)
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
                          recovery_mode: str = "off", recovery_count: int = 1) -> None:
        before = self.wait("prepare")
        self.tap(1010, 558, "开始演出")
        if self.dry_run:
            return
        # 开演后必须在短时间内看到演奏场，体力不足或弹窗不会误耗完整曲目超时。
        self.wait_for_play_or_recovery(before, recovery_mode, recovery_count)
        self.wait("live_clear", timeout=max_song_seconds)
        self.collect_with_back()

    def auto_live_loop(self, rounds: int, *, song_mode: str = "current",
                       recovery_mode: str = "off",
                       recovery_count: int = 1) -> None:
        if rounds < 1:
            raise ValueError("rounds 必须至少为 1")
        if song_mode not in {"current", "random"}:
            raise ValueError("选曲方式无效")
        if (recovery_mode not in {"off", "small", "large"} or
                isinstance(recovery_count, bool) or not isinstance(recovery_count, int) or not 1 <= recovery_count <= 99):
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
            self.prepare_auto(recovery_mode, recovery_count)
            self.start_and_collect(
                recovery_mode=recovery_mode,
                recovery_count=recovery_count,
            )
            self.completed_rounds += 1
            self.log_message(f"自动演出：已完成 {self.completed_rounds} / 总数 {rounds}")
