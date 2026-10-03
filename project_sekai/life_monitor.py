from __future__ import annotations

import cv2
import numpy as np


class LifeDepleted(RuntimeError):
    """演奏场已连续确认生命为零，触控清理后由任务负责退出。"""


class LifeGuard:
    def __init__(self, confirm_frames: int = 2):
        self.confirm_frames = confirm_frames
        self.zero_streak = 0
        self.samples = 0
        self.zero_confirmed = False

    def observe(self, frame: np.ndarray, read_zero) -> bool:
        self.samples += 1
        # 只检查条带内部，排除左侧心形；低生命的黄色和红色也必须计为存活。
        bar = cv2.cvtColor(frame[40:48, 1008:1177], cv2.COLOR_BGR2HSV)
        fill = cv2.inRange(bar, (17, 55, 150), (100, 255, 255))
        fill |= cv2.inRange(bar, (0, 100, 205), (12, 255, 255))
        fill |= cv2.inRange(bar, (170, 100, 205), (179, 255, 255))
        if np.count_nonzero(fill) >= 6:
            self.zero_streak = 0
            return False
        # 空条还可能来自淡入或技能特效；必须同时实读零，不能把 OCR 缺失当作死亡。
        self.zero_streak = self.zero_streak + 1 if read_zero(frame) else 0
        self.zero_confirmed = self.zero_streak >= self.confirm_frames
        return self.zero_confirmed
