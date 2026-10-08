from datetime import datetime
import os
from pathlib import Path
import re
import time
from uuid import uuid4

import cv2
import numpy as np

from .chart_catalog import _write_json
from .maa_device import MaaDevice
from .navigator import Navigator
from .ocr import LineOcr


def title_menu_ready(frame):
    hsv = cv2.cvtColor(frame[27:62, 1217:1259], cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (0, 0, 0), (179, 95, 130))
    lines = []
    for contour in cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]:
        x, y, width, height = cv2.boundingRect(contour)
        if 12 <= width <= 30 and 1 <= height <= 5 and width >= height * 3:
            lines.append((x + width / 2, y + height / 2))
    lines.sort(key=lambda point: point[1])
    return (len(lines) == 3 and max(x for x, _ in lines) - min(x for x, _ in lines) <= 4
            and all(6 <= lines[index + 1][1] - lines[index][1] <= 15 for index in range(2)))


class GameLogin:
    def __init__(self, device, navigator, report_root, *, stop_requested=None, log_message=None,
                 clock=None, sleeper=None, ocr_root=None, ocr=None):
        self.device, self.navigator = device, navigator
        self.report_root = Path(report_root)
        self.stop_requested = stop_requested or (lambda: False)
        self.log = log_message or (lambda message: print(message, flush=True))
        self.clock, self.sleeper = clock or time.monotonic, sleeper or time.sleep
        self.ocr_root, self.ocr = ocr_root, ocr

    def check_stop(self):
        if self.stop_requested():
            raise InterruptedError("用户已停止游戏登录")

    def safe_log(self, message):
        try:
            self.log(message)
        except Exception:
            pass

    def pause(self, duration):
        deadline = self.clock() + duration
        while self.clock() < deadline:
            self.check_stop()
            self.sleeper(min(.1, deadline - self.clock()))
        self.check_stop()

    def foreground(self):
        self.check_stop()
        focus = self.device.shell("dumpsys window")
        return "mCurrentFocus=" in focus and "com.sega.pjsekai" in focus.split("mCurrentFocus=", 1)[1].splitlines()[0]

    def matches(self, frame, name):
        return name in self.navigator.templates and self.navigator.match(frame, name)[0] >= self.navigator.threshold

    def known_notice(self, frame):
        if self.matches(frame, "life_hud") or self.matches(frame, "playing"):
            return False
        if self.ocr is None:
            if self.ocr_root is None:
                return False
            self.ocr = LineOcr(self.ocr_root)
        for box, expected in (((174, 29, 299, 59), "お知らせ"), ((491, 33, 550, 60), "一覧"),
                              ((729, 33, 791, 60), "不具合"), ((930, 34, 1054, 61), "関連サイト")):
            reading = self.ocr.read(frame, box)
            if reading.confidence < .85 or re.sub(r"\s+", "", reading.text) != expected:
                return False
        return True

    def run(self, *, wait_for_home=False, resume=False):
        if resume and hasattr(self, '_session'):
            directory, report = self._session
        else:
            directory = self.report_root / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:8])
            directory.mkdir(parents=True)
            report = {"schema_version": 1, "task": "game_login", "completed": False,
                      "started_at": datetime.now().astimezone().isoformat(), "launch_requests": 0,
                      "title_requests": 0, "notice_requests": 0}
            self._session = directory, report
            self._deadline = self.clock() + 60.
            self._last_title = self._last_notice = float('-inf')
            self._managed_login = wait_for_home
        self.report = report
        persist = lambda: _write_json(directory / "report.json", report)
        try:
            self.check_stop()
            persist()
            density = self.device.shell("wm density")
            if not re.search(r"(?:Physical|Override) density: 240\b", density):
                raise RuntimeError("游戏登录要求当前设备 DPI 为 240")
            if not {"title_screen", "home"} <= self.navigator.templates.keys():
                raise RuntimeError("登录模板缺失，无法安全确认标题或主页")
            deadline = self._deadline
            stable_home = 0
            if resume and self.clock() >= deadline:
                # 输入期限耗尽后只读接纳用户已经返回的真实主页；不重置登录预算或重新点标题。
                for _ in range(2):
                    self.check_stop()
                    frame = self.device.screenshot()
                    if (frame.shape[:2] != (720, 1280) or not self.foreground()
                            or not self.matches(frame, 'home') or self.matches(frame, 'title_screen')
                            or self.matches(frame, 'life_hud') or self.matches(frame, 'playing')
                            or self.known_notice(frame)):
                        raise TimeoutError("登录输入期限已耗尽，等待用户返回实际主页")
                    self.pause(.25)
                self.device.preflight()
                report.update(completed=True, phase='home_confirmed_after_deadline')
                persist()
                return report
            while self.clock() < deadline:
                self.check_stop()
                frame = self.device.screenshot()
                self.check_stop()
                if frame.shape[:2] != (720, 1280):
                    raise RuntimeError("游戏登录截图不是 1280×720")
                foreground = self.foreground()
                if self.clock() >= deadline:
                    break
                title = foreground and self.matches(frame, "title_screen")
                if title:
                    self._managed_login = True
                if foreground and not self._managed_login:
                    # 已运行的非标题页交给各任务原有安全策略，包括准备、房内、演奏和未知页。
                    self.device.preflight()
                    report.update(completed=True, phase="already_running")
                    persist()
                    return report
                if foreground and self._managed_login and self.known_notice(frame):
                    stable_home = 0
                    if report['notice_requests'] < 3 and self.clock() - self._last_notice >= 1.:
                        fresh = self.device.screenshot()
                        self.check_stop()
                        if not self.known_notice(fresh) or not self.foreground():
                            continue
                        report['notice_requests'] += 1
                        self._last_notice = self.clock()
                        persist()
                        self.check_stop()
                        self.safe_log("登录后已确认お知らせ公告，使用 BACK 关闭并等待实际主页")
                        self.device.back()
                    self.pause(.25)
                    continue
                if foreground and self.matches(frame, "home") and not self.matches(frame, "title_screen"):
                    stable_home += 1
                    if stable_home >= 2:
                        self.device.preflight()
                        report.update(completed=True, phase="home_confirmed")
                        persist()
                        self.safe_log("日服游戏登录已确认实际主页，继续当前任务")
                        return report
                else:
                    stable_home = 0
                if not foreground and report["launch_requests"] == 0:
                    self._managed_login = True
                    report["launch_requests"] += 1
                    persist()
                    self.check_stop()
                    self.safe_log("当前日服未在前台，启动游戏并等待标题登录")
                    # 启动指定日服包，不强停用户的其他前台应用。
                    self.device.shell("monkey -p com.sega.pjsekai -c android.intent.category.LAUNCHER 1")
                elif (title and title_menu_ready(frame) and report["title_requests"] < 3
                      and self.clock() - self._last_title >= 5.):
                    # shell 检查可能经历页面切换，点击前必须重新取帧并再次确认标题。
                    fresh = self.device.screenshot()
                    self.check_stop()
                    if (not self.matches(fresh, "title_screen") or not title_menu_ready(fresh)
                            or not self.foreground()):
                        continue
                    report["title_requests"] += 1
                    self._last_title = self.clock()
                    persist()
                    self.check_stop()
                    self.safe_log("已确认标题与右上三横菜单就绪，点击 TAP TO START 登录")
                    self.device.tap(640, 620)
                self.pause(.25)
            raise TimeoutError("游戏启动与登录超过 60 秒，未确认实际主页")
        except Exception as error:
            report.update(error=f"{type(error).__name__}: {error}", cancelled=self.stop_requested())
            raise
        finally:
            report["finished_at"] = datetime.now().astimezone().isoformat()
            persist()


def login_at_task_start(context, root, log_message=None):
    root = Path(root)
    device = MaaDevice(context.tasker.controller)
    config = Path(os.environ.get("MAAPJSK_TEMPLATE_CONFIG", root / "config/maapjsk-templates.json"))
    navigator = Navigator(device, config, stop_requested=lambda: bool(context.tasker.stopping))
    return GameLogin(device, navigator, root / "debug/game-login-runs",
                     stop_requested=lambda: bool(context.tasker.stopping), log_message=log_message,
                     ocr_root=root / "resource/models/song_title_ocr").run()
