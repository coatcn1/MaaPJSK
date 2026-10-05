from __future__ import annotations

import cv2
import numpy as np


class LifeDepleted(RuntimeError):
    """演奏场已连续确认生命为零，触控清理后由任务负责退出。"""


class ZeroLifeTemplate:
    def __init__(self, template: np.ndarray):
        # 包含整段数值留白，不能截取末位 0，否则 10、100、1000 都可能命中。
        self.mask = cv2.inRange(template, (230, 230, 230), (255, 255, 255))
        self.pixels = np.count_nonzero(self.mask)
        if self.pixels < 6:
            raise ValueError("零生命模板缺少实际数字笔画")

    def score(self, frame: np.ndarray, area) -> float:
        x1, y1, x2, y2 = area
        value = cv2.inRange(frame[y1:y2, x1:x2], (230, 230, 230), (255, 255, 255))
        if value.shape != self.mask.shape:
            return 0.0
        # 额外数字和特效亮块应视为未知，不借助单个 0 的局部匹配强行判死。
        pixels = np.count_nonzero(value)
        if not self.pixels * .8 <= pixels <= self.pixels * 1.2:
            return 0.0
        return float(cv2.matchTemplate(value, self.mask, cv2.TM_CCOEFF_NORMED)[0, 0])


class LifeGuard:
    def __init__(self, confirm_frames: int = 2, *, bar_area=(1008, 40, 1177, 48)):
        self.confirm_frames = confirm_frames
        self.bar_area = bar_area
        self.last_sample_at = float('-inf')
        self.zero_streak = 0
        self.samples = 0
        self.zero_confirmed = False
        self.hud_seen = False
        self.playfield_missing_frames = 0
        self.pause_fallback_frames = 0
        self.bar_fill_pixels = self.bar_total_pixels = None

    def observe(self, frame: np.ndarray, read_zero) -> bool:
        self.samples += 1
        # 只检查条带内部，排除左侧心形；低生命的黄色和红色也必须计为存活。
        x1, y1, x2, y2 = self.bar_area
        bar = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2HSV)
        fill = cv2.inRange(bar, (17, 55, 150), (100, 255, 255))
        fill |= cv2.inRange(bar, (0, 100, 205), (12, 255, 255))
        fill |= cv2.inRange(bar, (170, 100, 205), (179, 255, 255))
        self.bar_fill_pixels = int(np.count_nonzero(fill))
        self.bar_total_pixels = int(fill.size)
        if self.bar_fill_pixels >= 6:
            self.zero_streak = 0
            return False
        # 空条还可能来自淡入或技能特效；必须同时实读零，不能把 OCR 缺失当作死亡。
        self.zero_streak = self.zero_streak + 1 if read_zero(frame) else 0
        self.zero_confirmed = self.zero_streak >= self.confirm_frames
        return self.zero_confirmed
