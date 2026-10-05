from __future__ import annotations

import numpy as np


# 协力生命栏比单人低约 8 像素，区域覆盖两种布局，模板只含固定标签。
LIFE_HUD_AREA = (990, 8, 1080, 68)


def blank_transition(frame):
    sample = frame[::12, ::12]
    return (np.mean(sample.max(axis=2) < 15) > .95
            or np.mean(sample.min(axis=2) > 245) > .95)


def release_finished(report):
    playback = report.get("playback", {})
    if not playback.get("release_confirmed"):
        return False
    if report.get("engine", playback.get("engine")) == "native":
        release = playback.get("release", {})
        if (playback.get("sent_actions") == 0
                and release.get("release_proof") == "no-touch-possible-and-cleanup"):
            return True
        return (release.get("reset_executed") is True
                and release.get("release_proof") == "current-reset-jlog-and-cleanup")
    return True


def input_finished(report):
    playback = report.get("playback", {})
    planned = playback.get("planned_actions", 0)
    if (planned <= 0 or planned != playback.get("sent_actions")
            or not release_finished(report) or report.get("playback_error")):
        return False
    if report.get("engine", playback.get("engine")) == "native":
        release = playback.get("release", {})
        return (planned == playback.get("executed_actions") and release.get("reset_executed") is True
                and release.get("release_proof") == "current-reset-jlog-and-cleanup")
    return True


class LiveEndGuard:
    def __init__(self, navigator, report, *, life_seen=False, require_settlement=False):
        self.navigator = navigator
        self.report = report
        self.life_seen = life_seen
        self.absent_frames = 0
        self.ended = False
        self.require_settlement = require_settlement
        self.candidate_page = None
        self.confirmed_page = None

    def visible(self, frame):
        # LIFE 标签与数值、条带填充无关；空条和零生命仍是演奏画面，不能当作结束。
        if "life_hud" in self.navigator.templates:
            return self.navigator.match(frame, "life_hud", LIFE_HUD_AREA)[0] >= self.navigator.threshold
        # 旧模板包仍可运行，使用同属演奏界面的暂停按钮；重新生成模板后改用 LIFE 标签。
        return self.navigator.match(frame, "playing")[0] >= self.navigator.threshold

    def observe(self, frame, *, settlement_page=None):
        if blank_transition(frame):
            self.absent_frames = 0
            self.ended = False
            self.candidate_page = None
            return False
        if self.visible(frame):
            self.life_seen = True
            self.absent_frames = 0
            self.ended = False
            self.candidate_page = self.confirmed_page = None
            return False
        if (self.navigator.match(frame, "playing")[0] >= self.navigator.threshold
                or (not self.life_seen and not self.require_settlement) or not input_finished(self.report)):
            self.absent_frames = 0
            self.ended = False
            self.candidate_page = self.confirmed_page = None
            return False
        if self.require_settlement:
            # 协力先取得连续两帧明确结算证据；确认后的奖励动画不必重复识别按钮。
            if self.confirmed_page is not None:
                self.ended = True
                return True
            if settlement_page is None:
                self.absent_frames = 0
                self.candidate_page = None
                return False
            if self.candidate_page != settlement_page:
                self.absent_frames = 0
                self.candidate_page = settlement_page
        self.absent_frames = min(2, self.absent_frames + 1)
        if self.absent_frames >= 2:
            self.ended = True
            self.report["end_detection"] = {
                "method": "cooperative_settlement_confirmed" if self.require_settlement else "life_hud_disappeared",
                "confirmation_frames": self.absent_frames,
                "life_seen_during_playback": self.life_seen, "input_and_release_complete": True,
                "hud_detector": "life_label" if "life_hud" in self.navigator.templates else "pause_button",
            }
            if self.require_settlement:
                self.confirmed_page = settlement_page
                self.report["end_detection"]["settlement_page"] = settlement_page
            self.report.setdefault("live_status", "ended")
        return self.ended
