from __future__ import annotations

from dataclasses import asdict, dataclass
from collections import deque
from datetime import datetime
import csv
import json
import os
from pathlib import Path
import re
import time
from uuid import uuid4

import cv2
import numpy as np

from .chart_catalog import ChartRepository, _write_json
from .chart_player import ChartPlayer, StartAnchor, compile_touches, first_note_context, dense_first_note_context
from .calibration import verify_profile_anchor
from .life_monitor import LifeDepleted, LifeGuard, ZeroLifeTemplate
from .live_end import LIFE_HUD_AREA, LiveEndGuard, blank_transition, release_finished
from .navigator import Navigator, read_available_bonus
from .ocr import LineOcr, Reading
from .performance_trace import PerformanceTrace
from .song_identity import SongMatcher, normalize_title, write_image
from .sus_chart import parse_sus


DIFFICULTY_POINTS = {"easy": (854, 488), "normal": (934, 492), "hard": (1016, 497),
                     "expert": (1094, 501), "master": (1175, 504)}
SEARCH_QUERY_BOX = (151, 24, 580, 62)
SEARCH_QUERY_BOXES = (SEARCH_QUERY_BOX, (151, 20, 580, 65), (151, 27, 580, 57))
SEARCH_NATIVE_QUERY_BOX = (12, 642, 460, 696)


def numeric(reading: Reading) -> int:
    # 引号等字形边缘噪声不参与数值；多组数字或低置信度不能当作已确认结果。
    groups = re.findall(r"\d+", reading.text.replace(",", ""))
    if len(groups) != 1 or reading.confidence < .75:
        raise ValueError(f"数字未稳定识别：{reading.text!r}，置信度={reading.confidence:.3f}")
    return int(groups[0])


def is_light_mode(reading: Reading) -> bool:
    # 本机字体的「軽」会被多语 OCR 解码为繁体「輕」，只接受完整的已知字形变体。
    return reading.confidence >= .7 and normalize_title(reading.text) in {"軽量", "輕量", "轻量"}


def append_result_index(root: Path, directory: Path, report: dict):
    if "judgements" not in report:
        return
    identity = report.get("preparation_identity", {})
    values = report["judgements"]
    row = {"run": directory.name, "started_at": report["started_at"], "song_id": identity.get("song_id"),
           "title": identity.get("title"), "difficulty": report["requested_difficulty"],
           "completed": report.get("completed", False), "live_status": report.get("live_status", "unknown"),
           **{key: values.get(key) for key in ("perfect", "great", "good", "bad", "miss", "total", "perfect_rate", "hit_rate",
                                             "combo", "score", "late", "fast", "flick", "total_matches_chart")}}
    path = root / "results.csv"
    existing = path.is_file() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8-sig" if not existing else "utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(row))
        if not existing:
            writer.writeheader()
        writer.writerow(row)


@dataclass(frozen=True)
class LiveResult:
    perfect: int
    great: int
    good: int
    bad: int
    miss: int
    late: int | None = None
    fast: int | None = None
    flick: int | None = None
    combo: int | None = None
    score: int | None = None

    def to_dict(self) -> dict:
        total = self.perfect + self.great + self.good + self.bad + self.miss
        if total <= 0 or min(self.perfect, self.great, self.good, self.bad, self.miss) < 0:
            raise ValueError("判定数据无效")
        return {**asdict(self), "total": total, "perfect_rate": self.perfect / total,
                "hit_rate": (total - self.miss) / total}


class SoloLive:
    def __init__(self, device, template_config: Path, chart_root: Path, model_root: Path, report_root: Path,
                 *, stop_requested=lambda: False, log_message=print) -> None:
        self.device, self.stop_requested, self.log = device, stop_requested, log_message
        self.navigator = Navigator(device, template_config, stop_requested=stop_requested, log_message=log_message,
                                   bonus_reader=self.read_available_bonus)
        self.repository = ChartRepository(chart_root)
        self.ocr = LineOcr(model_root)
        self.matcher = SongMatcher(self.repository, self.ocr)
        self.report_root = report_root
        self.completed_rounds = 0

    def screenshot(self):
        self.navigator._check_stop()
        frame = self.device.screenshot()
        self.navigator._check_title_screen(frame)
        if (self.navigator.match(frame, "playing")[0] < self.navigator.threshold
                and self.navigator.match(frame, "home")[0] < self.navigator.threshold):
            # 标题文字会淡入，模板可能被透明度干扰；在未知页面用文字复核一次。
            title = self.ocr.read(frame, (529, 599, 752, 637))
            if title.confidence >= .8 and normalize_title(title.text) == "taptostart":
                raise RuntimeError("游戏已返回 TAP TO START 标题页，停止结算返回")
        return frame

    def pause(self, seconds: float):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.navigator._check_stop()
            time.sleep(min(.04, max(0, end - time.monotonic())))

    def resolve_specified_song(self, song_name: str, difficulty: str) -> int:
        if not isinstance(song_name, str) or not song_name.strip():
            raise ValueError("指定歌曲名不能为空")
        matches = [song_id for song_id, song in self.repository.songs.items()
                   if normalize_title(song['title']) == normalize_title(song_name)]
        if len(matches) != 1:
            raise ValueError("指定歌曲须与本地目录的唯一完整曲名匹配")
        song_id = matches[0]
        if difficulty not in self.repository.songs[song_id]['charts']:
            raise ValueError("指定歌曲的请求难度未收录本地谱面")
        # 搜索前校验实际文件，不能为了找歌先输入，之后才发现本地谱面损坏。
        self.repository.load_chart(song_id, difficulty)
        return song_id

    def checked_search_page(self):
        self.navigator._check_stop()
        frame = self.screenshot()
        if (blank_transition(frame) or self.navigator.match(frame, 'playing')[0] >= self.navigator.threshold
                or self.navigator.match(frame, 'life_hud', LIFE_HUD_AREA)[0] >= self.navigator.threshold):
            raise RuntimeError("指定搜索未确认无演奏 HUD 的单人选曲页")
        if self.navigator.match(frame, 'song_select')[0] < self.navigator.threshold:
            # 无结果的决定按钮变灰，但搜索页固定双标题仍可提供独立正证据。
            layout = self.ocr.read(frame, (680, 25, 757, 59))
            order = self.ocr.read(frame, (995, 25, 1142, 59))
            if (layout.confidence < .85 or normalize_title(layout.text) != normalize_title('リスト')
                    or order.confidence < .85 or normalize_title(order.text) != normalize_title('配信順')):
                raise RuntimeError("指定搜索页面模板与固定双标题均未确认")
        return frame

    def select_all_search_category(self):
        # 分类列表可能滚动到中段；只在已确认页面内有界向下滑动，找到实际文字才点击。
        for attempt in range(4):
            frame = self.checked_search_page()
            matches = []
            for y in range(85, 636, 50):
                reading = self.ocr.read(frame, (35, y, 114, y + 50))
                if reading.confidence >= .85 and normalize_title(reading.text) == normalize_title('すべて'):
                    matches.append((74, y + 25))
            if len(matches) == 1:
                fresh = self.checked_search_page()
                x, y = matches[0]
                verified = self.ocr.read(fresh, (35, y - 25, 114, y + 25))
                if verified.confidence < .85 or normalize_title(verified.text) != normalize_title('すべて'):
                    raise RuntimeError("搜索分类在操作前已变化")
                self.navigator._check_stop()
                self.navigator.tap(*matches[0], '搜索分类：すべて')
                self.pause(.3)
                for _ in range(3):
                    fresh = self.checked_search_page()
                    selected = self.ocr.read(fresh, (35, y - 25, 114, y + 25))
                    hsv = cv2.cvtColor(fresh[y - 25:y + 25, 35:114], cv2.COLOR_BGR2HSV)
                    active = cv2.countNonZero(cv2.inRange(hsv, (75, 60, 120), (100, 255, 255))) >= 80
                    # 分类选中由青色文字确认；点击回执和白色すべて不能证明已切换。
                    if selected.confidence >= .85 and normalize_title(selected.text) == normalize_title('すべて') and active:
                        return
                    self.pause(.2)
                raise RuntimeError("すべて分类点击后未确认选中，拒绝搜索")
            if matches or attempt == 3:
                raise RuntimeError("未唯一确认搜索分类すべて，拒绝在局部分类搜索")
            self.navigator._check_stop()
            self.device.swipe(70, 180, 70, 550)
            self.pause(.3)

    @staticmethod
    def native_search_text_present(frame) -> bool:
        x1, y1, x2, y2 = SEARCH_NATIVE_QUERY_BOX
        gray = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
        if float(np.mean(gray)) < 220:
            return False
        _, _, stats, _ = cv2.connectedComponentsWithStats(np.uint8(gray < 190), connectivity=8)
        # 原生白色文本行只测有无字形；忽略细光标与右边界被 toast 裁入的分量，不解释曲名。
        return any(width >= 3 and height >= 3 and area >= 12 and x + width < gray.shape[1]
                   for x, y, width, height, area in stats[1:])

    def submit_song_search(self, title: str):
        query = title.replace(' ', '')
        report = getattr(self, 'last_report', None)
        if isinstance(report, dict):
            report['search_query'] = query
        frame = self.checked_search_page()
        if self.search_clear_visible(frame):
            # 新搜索前清除正向确认的旧查询；提交后绝不再次点击 X。
            self.navigator._check_stop()
            self.navigator.tap(610, 42, '搜索前清除旧查询')
            self.pause(.3)
            frame = self.checked_search_page()
            if self.search_clear_visible(frame):
                raise RuntimeError("搜索前清除后查询 X 仍在")
        self.navigator.tap(350, 42, '打开歌曲搜索输入')
        self.pause(.2)
        frame = self.checked_search_page()
        confirm = self.ocr.read(frame, (1180, 644, 1254, 693))
        if confirm.confidence < .85 or normalize_title(confirm.text) != normalize_title('确定'):
            raise RuntimeError("未确认原生搜索输入框的确定按钮")
        if self.native_search_text_present(frame):
            raise RuntimeError("原生搜索编辑框并非空白，拒绝重复输入")
        self.navigator._check_stop()
        if not self.device.controller.post_input_text(query).wait().succeeded:
            raise RuntimeError("歌曲搜索文本输入失败")
        self.wait_search_submission(title)
        self.navigator._check_stop()
        self.navigator.tap(1215, 667, '提交歌曲搜索')
        self.pause(.4)

    def search_query_readings(self, frame):
        # 固定有限几何仅保留诊断读数，不作为输入或身份的语义门槛。
        return [{'source': 'top', 'box': list(box), **asdict(self.ocr.read(frame, box))}
                for box in SEARCH_QUERY_BOXES]

    def wait_search_submission(self, title: str):
        deadline = time.monotonic() + 2
        consecutive = 0
        while True:
            frame = self.checked_search_page()
            readings = self.search_query_readings(frame)
            confirm = self.ocr.read(frame, (1180, 644, 1254, 693))
            confirm_valid = confirm.confidence >= .85 and normalize_title(confirm.text) == normalize_title('确定')
            if confirm_valid:
                # 原生左文本行是独立黑白通道，避开右侧粘贴 toast；只有真实编辑框成立才读取。
                readings.append({'source': 'native', 'box': list(SEARCH_NATIVE_QUERY_BOX),
                                 **asdict(self.ocr.read(frame, SEARCH_NATIVE_QUERY_BOX))})
            present = self.native_search_text_present(frame)
            chosen = readings[0]
            evidence = {'query_box': chosen['box'], 'typed': {'text': chosen['text'], 'confidence': chosen['confidence']},
                        'confirm': asdict(confirm), 'query_readings': readings, 'native_text_present': present}
            report = getattr(self, 'last_report', None)
            if isinstance(report, dict):
                report['search_submission_readings'] = evidence
            valid = present and confirm_valid
            consecutive = consecutive + 1 if valid else 0
            if consecutive >= 2:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"原生搜索编辑状态未确认，拒绝提交：{evidence}")
            # 文本粘贴回执不等于画面刷新；限时只读新帧，不重复粘贴、清除或提交。
            self.pause(min(.15, remaining))

    @staticmethod
    def search_clear_visible(frame) -> bool:
        # 固定搜索框内的深色双对角 X；放大镜只有单侧柄，不能作为清除按钮证据。
        gray = cv2.cvtColor(frame[30:54, 598:622], cv2.COLOR_BGR2GRAY)
        dark = np.uint8(gray < 115) * 255
        lines = cv2.HoughLinesP(dark, 1, np.pi / 180, 9, minLineLength=12, maxLineGap=2)
        slopes = set()
        if lines is not None:
            for x1, y1, x2, y2 in lines[:, 0]:
                dx, dy = int(x2 - x1), int(y2 - y1)
                if abs(dx) >= 10 and .7 <= abs(dy / dx) <= 1.3:
                    slopes.add(1 if dx * dy > 0 else -1)
        return slopes == {-1, 1}

    def search_specified_song(self, song_id: int, difficulty: str):
        title = self.repository.songs[song_id]['title']
        self.select_all_search_category()
        self.submit_song_search(title)
        selected = None
        # 完整曲名查询通常自动选中唯一结果；只接受真实选中身份，不猜第一张卡的位置。
        for index in range(2):
            frame = self.checked_search_page()
            readings = self.search_query_readings(frame)
            confirm = self.ocr.read(frame, (1180, 644, 1254, 693))
            native_open = confirm.confidence >= .85 and normalize_title(confirm.text) == normalize_title('确定')
            committed = not native_open and self.search_clear_visible(frame)
            report = getattr(self, 'last_report', None)
            if isinstance(report, dict):
                report['search_result_query_readings'] = readings
                report['search_result_query_verification'] = {
                    'method': 'actual_target_after_commit' if committed else 'unconfirmed',
                    'native_open': native_open, 'query_x_visible': committed, 'target_confirmed': False}
            if not committed:
                raise RuntimeError("提交后的指定歌曲查询词或提交状态未确认")
            identity = self.matcher.match(frame, difficulty, 'song_select')
            if isinstance(report, dict):
                report['search_result_query_verification']['actual_song_id'] = identity.song_id
            if identity.song_id != song_id:
                raise RuntimeError("搜索结果未选中指定歌曲，拒绝演出其他歌曲")
            if isinstance(report, dict):
                report['search_result_query_verification'].update(target_confirmed=True, target_confirmation_frames=index + 1)
            selected = identity
            self.pause(.2)
        return selected

    @staticmethod
    def selected_card_padlock(frame) -> bool:
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        white = cv2.inRange(hsv, (0, 0, 235), (179, 40, 255))
        selected = []
        for contour in cv2.findContours(white[170:550, 170:710], cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]:
            x, y, width, height = cv2.boundingRect(contour)
            if 165 <= width <= 190 and 165 <= height <= 190 and cv2.contourArea(contour) > width * height * .8:
                selected.append((x + 170, y + 170, width, height))
        if len(selected) != 1:
            return False
        x, y, width, height = selected[0]
        cx, cy = x + width // 2, y + height // 2
        crop = frame[cy - 30:cy + 30, cx - 26:cx + 26]
        mask = cv2.inRange(cv2.cvtColor(crop, cv2.COLOR_BGR2HSV), (0, 0, 235), (179, 40, 255))
        for contour in cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]:
            lx, ly, lw, lh = cv2.boundingRect(contour)
            if not (30 <= lw <= 42 and 36 <= lh <= 48):
                continue
            part = mask[ly:ly + lh, lx:lx + lw] > 0
            dark = cv2.cvtColor(crop[ly:ly + lh, lx:lx + lw], cv2.COLOR_BGR2GRAY) < 110
            # 白锁须同时有实心底座、暗色锁环内孔与钥匙孔，不能把白色文字当作锁。
            if (np.mean(part[round(lh * .4):]) >= .85
                    and np.mean(dark[round(lh * .12):round(lh * .33), round(lw * .27):round(lw * .73)]) >= .65
                    and np.mean(dark[round(lh * .48):round(lh * .86), round(lw * .35):round(lw * .65)]) >= .2):
                return True
        return False

    def specified_decide_state(self, frame) -> str:
        reading = self.ocr.read(frame, (976, 578, 1042, 608))
        if reading.confidence < .7 or normalize_title(reading.text) not in {normalize_title('決定'), normalize_title('决定')}:
            return 'unknown'
        value = float(np.median(cv2.cvtColor(frame[573:606, 955:1060], cv2.COLOR_BGR2HSV)[:, :, 2]))
        locked = self.selected_card_padlock(frame)
        if locked and 50 <= value <= 100:
            return 'locked'
        if not locked and value >= 115:
            return 'ready'
        return 'unknown'

    def select(self, difficulty: str, song_mode: str, *, song_name: str | None = None):
        expected_song_id = self.resolve_specified_song(song_name, difficulty) if song_mode == 'specified' else None
        self.navigator.return_to_home()
        self.navigator.tap(1194, 649, "主页 Live")
        self.navigator.wait("live_menu")
        self.navigator.tap(722, 235, "单人 Live")
        frame = self.navigator.wait("song_select")
        selected = self.matcher.match(frame, difficulty, "song_select") if song_mode == "current" else None
        group = self.ocr.read(frame, (971, 529, 1059, 560))
        if group.confidence < .7 or normalize_title(group.text) not in {"hard", "append"}:
            raise RuntimeError(f"选曲页难度组未确认：{group.text!r}")
        wanted_group = "append" if difficulty == "append" else "hard"
        if normalize_title(group.text) != wanted_group:
            self.navigator.tap(*( (1224, 505) if difficulty == "append" else (803, 485)), "切换谱面难度组")
            self.pause(.4)
            frame = self.navigator.wait("song_select")
            group = self.ocr.read(frame, (971, 529, 1059, 560))
            if group.confidence < .7 or normalize_title(group.text) != wanted_group:
                raise RuntimeError("选曲页未切换到请求的难度组")
        if song_mode == "random":
            self.navigator._select_random_song(frame)
        elif song_mode == 'specified':
            selected = self.search_specified_song(expected_song_id, difficulty)
        if difficulty == "append":
            # Append 组只显示中央一个难度按钮，与普通五难度组的坐标不同。
            self.navigator.tap(1015, 500, "Append 难度")
        else:
            self.navigator.tap(*DIFFICULTY_POINTS[difficulty], f"{difficulty.upper()} 难度")
        self.pause(.4)
        if expected_song_id is not None:
            frame = self.checked_search_page()
            actual = self.matcher.match(frame, difficulty, 'song_select')
            if actual.song_id != expected_song_id:
                raise RuntimeError("指定歌曲在切换难度后发生变化")
            state = self.specified_decide_state(frame)
            if state == 'locked':
                raise RuntimeError("指定歌曲尚未解锁，请先在游戏音乐商店解锁")
            if state != 'ready':
                raise RuntimeError("指定歌曲的决定按钮未确认可用，拒绝点击")
        self.navigator.tap(1007, 590, "选曲确认")
        frame = self.navigator.wait("prepare")
        prepared = self.matcher.match(frame, difficulty, "prepare")
        if selected is not None and selected.song_id != prepared.song_id:
            raise RuntimeError("切换难度后当前歌曲发生变化，拒绝演出其他歌曲")
        if expected_song_id is not None and prepared.song_id != expected_song_id:
            raise RuntimeError("指定歌曲与准备页实际歌曲不符，拒绝演出")
        return prepared, frame

    def prepare_bonus(self, consumption: str | int, report: dict, directory: Path, *, setup_playback=True, return_page="prepare"):
        if setup_playback:
            self.navigator.ensure_auto(False)
        self.navigator.tap(1150, 42, "打开体力消耗设置")
        self.pause(.4)
        # 游戏会记住上次的回复标签；两个标签标题始终可见，不能只凭标题确认消耗页。
        self.navigator.tap(470, 42, "选择体力消耗标签")
        self.pause(.35)
        for _ in range(6):
            frame = self.screenshot()
            heading = self.ocr.read(frame, (365, 27, 650, 66))
            description = self.ocr.read(frame, (436, 150, 845, 198))
            if (heading.confidence >= .7 and description.confidence >= .7
                    and "消費" in heading.text and "消費量" in description.text):
                break
            # 标签切换的淡入帧会短暂误读汉字，先等待文字稳定，不重复点标签或操作滑条。
            self.pause(.2)
        else:
            raise RuntimeError(f"未到达体力消耗页：{heading.text!r}，{description.text!r}")
        original = None
        for _ in range(12):
            try:
                original = self.read_bonus(frame)[0]
                break
            except ValueError:
                # 标题和正文就绪不代表消耗数字的动画已结束；读数不清时等待，不调整数量。
                self.pause(.2)
                frame = self.screenshot()
        if original is None:
            raise RuntimeError("每局体力消耗数字未稳定确认，拒绝调整和开演")
        target = original if consumption == "current" else consumption
        # 沿用模式不调整滑条；指定数量只按用户设置调整，不因体力不足改成零或更小的数。
        if original != target:
            self.adjust_bonus_count(target, frame)
        readings = []
        for _ in range(2):
            frame = self.screenshot()
            value, reading = self.read_bonus(frame)
            if value != target:
                raise RuntimeError(f"未确认用户指定的 {target} 体力消耗，拒绝开演")
            readings.append(asdict(reading))
            self.pause(.12)
        write_image(directory / "bonus.png", frame)
        report["bonus"] = {"requested_consumption": consumption, "consumption": target,
                           "confirmed": True, "readings": readings, "original_consumption": original}
        self.navigator.tap(762, 655, f"保存 {target} 体力消耗")
        frame = self.navigator.wait(return_page)
        if not setup_playback:
            return
        self.prepare_playback(report, frame=frame)

    def prepare_task_bonus(self, consumption, report, directory, snapshot, *, return_page="prepare"):
        if snapshot:
            # 后续局沿用本任务已保存的确认，不把历史读数包装成当前两帧检测。
            report["bonus"] = dict(snapshot, confirmation_source="task_snapshot")
            return
        self.prepare_bonus(consumption, report, directory, setup_playback=False, return_page=return_page)
        bonus = report["bonus"]
        if bonus.get("confirmed") is not True:
            raise RuntimeError("任务体力消耗尚未确认，拒绝缓存和开演")
        bonus.update(confirmation_source="observed", source_report=report["report_path"])
        # 只有菜单保存及返回成功才缓存；每局可用体力和回复证据不属于设置快照。
        snapshot.update({key: bonus[key] for key in ("requested_consumption", "consumption", "confirmed",
                                                   "original_consumption", "source_report")})

    def prepare_playback(self, report, *, frame=None):
        self.navigator.ensure_auto(False)
        if frame is None:
            frame = self.navigator.wait("prepare")
        # 轻量背景提供固定开场锚点，不修改用户的流速或游戏判定偏移。
        for _ in range(5):
            mode = self.ocr.read(frame, (43, 647, 153, 685))
            if is_light_mode(mode):
                report["background"] = asdict(mode)
                break
            self.navigator.tap(94, 665, "切换到轻量演出")
            self.pause(.3)
            frame = self.screenshot()
        else:
            raise RuntimeError("未确认轻量演出，拒绝使用未知背景开演")
        self.navigator.ensure_auto(False)
        self.log("本局演奏准备已确认：AUTO 关闭、轻量背景")

    def restore_calibration_bonus(self, consumption: int, directory: Path):
        frame = self.screenshot()
        if self.navigator.match(frame, "home")[0] < self.navigator.threshold:
            raise RuntimeError("校准尚未返回主页，无法安全恢复原体力消耗")
        restoration = {}
        self.prepare_bonus(consumption, restoration, directory, setup_playback=False, return_page="home")
        return restoration["bonus"]

    def read_bonus(self, frame):
        # 10 的第二位超出单数字矩形；保留完整两位，瓶子图标由粉色掩码排除。
        hsv = cv2.cvtColor(frame[247:289, 630:710], cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, (140, 75, 140), (179, 255, 255))
        digits = cv2.cvtColor(255 - mask, cv2.COLOR_GRAY2BGR)
        digits = cv2.copyMakeBorder(digits, 4, 4, 4, 4, cv2.BORDER_CONSTANT, value=(255, 255, 255))
        reading = self.ocr.read(digits, (0, 0, digits.shape[1], digits.shape[0]))
        if reading.confidence < .75:
            # 原图补读排除瓶子图标；保留完整两位数字，不降低低置信度门槛。
            reading = self.ocr.read(frame, (642, 247, 710, 289))
        if not re.fullmatch(r"\d{1,2}", reading.text.strip().strip("'\"")):
            raise ValueError(f"体力消耗数字不完整：{reading.text!r}")
        value = numeric(reading)
        if not 0 <= value <= 10:
            raise ValueError("体力消耗读数超出 0 到 10")
        return value, reading

    def adjust_bonus_count(self, target: int, frame: np.ndarray) -> int:
        if isinstance(target, bool) or not isinstance(target, int) or not 0 <= target <= 10:
            raise ValueError("每局体力消耗必须为 0 到 10 的整数")
        current, _ = self.read_bonus(frame)
        for _ in range(25):
            if current == target:
                return current
            self.navigator.tap(837 if current < target else 442, 320, f"调整为 {target} 体力消耗")
            # 加减按钮会去抖，逐次读取后再操作，不能按预定次数连续连点。
            self.pause(.3)
            updated, _ = self.read_bonus(self.screenshot())
            if abs(updated - current) > 1 or not 0 <= updated <= 10:
                raise RuntimeError("调整体力消耗时读数异常，停止调整")
            current = updated
        raise RuntimeError("用户指定的体力消耗数量未确认，停止调整")

    def read_available_bonus(self, frame: np.ndarray) -> tuple[int, Reading]:
        return read_available_bonus(self.ocr, frame)

    def wait_available_bonus(self) -> tuple[np.ndarray, int, list[dict]]:
        deadline = time.monotonic() + 8
        last_value = None
        readings = []
        while time.monotonic() < deadline:
            frame = self.screenshot()
            if self.navigator.match(frame, "prepare")[0] >= self.navigator.threshold:
                try:
                    value, reading = self.read_available_bonus(frame)
                except ValueError:
                    last_value, readings = None, []
                else:
                    if value == last_value:
                        return frame, value, readings + [asdict(reading)]
                    last_value, readings = value, [asdict(reading)]
            else:
                last_value, readings = None, []
            self.pause(.2)
        raise RuntimeError("当前体力未稳定确认，停止开演和自动用药")

    def ensure_bonus_available(self, mode: str, count: int, report: dict, directory: Path):
        consumption = report["bonus"]["consumption"]
        if consumption == 0:
            report["bonus"]["availability"] = "not_required"
            return
        before, available, readings = self.wait_available_bonus()
        report["bonus"].update(available_before=available, availability_readings=readings)
        if available >= consumption:
            report["bonus"]["availability"] = "sufficient"
            return
        report["bonus"]["availability"] = "insufficient"
        if mode == "off":
            raise RuntimeError(f"当前体力 {available}，每局需要 {consumption}；自动用药已关闭，请补充体力或修改任务设置")
        previous = getattr(self.navigator, "recovery_evidence", None)
        if (isinstance(previous, dict) and previous.get("ok_requested")
                and previous.get("status") == "started"):
            report["recovery"] = dict(previous, inherited_pending_batch=True,
                                      current_available=available)
            _write_json(directory / "report.json", report)
            raise RuntimeError("本任务先前已请求批量用药但到账未确认，请用户处理；不追加或重复整批")
        report["recovery"] = {"mode": mode, "requested_bottles": count, "completed_bottles": 0, "available_before": available,
                              "status": "started"}
        _write_json(directory / "report.json", report)
        self.log(f"体力不足：当前 {available} / 每局需要 {consumption}，按用户设置自动回复")
        self.navigator.tap(1086, 42, "打开体力回复")
        def record_batch(completed, frame):
            value, reading = self.read_available_bonus(frame)
            # 整批足额到账后先落盘，关闭提示失败也保留已用数量，不能把本批重新执行。
            report["recovery"].update(completed_bottles=completed, available_after=value,
                                      batch_reading=asdict(reading), status="credited",
                                      expected_increase=count * (1 if mode == "small" else 10))
            _write_json(directory / "report.json", report)

        try:
            self.navigator._recover_and_verify(before, mode, count, on_recovered=record_batch)
        except Exception:
            evidence = getattr(self.navigator, "recovery_evidence", None)
            if isinstance(evidence, dict):
                report["recovery"].update(evidence)
            _write_json(directory / "report.json", report)
            raise
        frame, after, readings = self.wait_available_bonus()
        write_image(directory / "recovery-after.png", frame)
        report["recovery"].update(available_after=after, completed_bottles=count, readings=readings, status="confirmed")
        report["bonus"]["available_after"] = after
        if after < consumption:
            # 每次只执行用户规定的一批瓶数；不足时停止，不追加瓶数，也不降低每局消耗。
            raise RuntimeError(f"已按设置使用 {count} 瓶饮料，当前体力 {after} 仍不足 {consumption}；请调整每次回复瓶数")
        report["bonus"]["availability"] = "recovered"
        self.navigator.ensure_auto(False)
        self.log(f"体力回复已确认：当前 {after}，继续按每局 {consumption} 体力开演")

    def record_identity_check(self, report, stage, *, captured_at=None, error=None):
        evidence = getattr(getattr(self, "matcher", None), "last_evidence", None)
        if not isinstance(evidence, dict):
            return
        check = dict(evidence, captured_at=captured_at, error=error)
        checks = report.setdefault("identity_checks", {}).setdefault(stage, [])
        checks.append(check)
        if len(checks) > 24:
            del checks[0]

    def observe_preplay_life(self, frame, captured_at, report):
        guard = getattr(self, "life_guard", None)
        if not isinstance(guard, LifeGuard) or captured_at - guard.last_sample_at < 2.:
            return
        guard.last_sample_at = captured_at
        if not hasattr(self, "preplay_life_frames"):
            self.preplay_life_frames = deque(maxlen=8)
        sample = {"captured_at": captured_at, "phase": "anchor" if report.get("final_identity") else "waiting_final",
                  "life_hud_score": None, "bar_observed": False, "filled_pixels": None, "total_pixels": None,
                  "zero_template_score": None, "status": "unknown"}
        depleted = False
        try:
            if blank_transition(frame):
                guard.zero_streak = 0
                sample["status"] = "blank_transition"
                return
            templates = getattr(self.navigator, "templates", {})
            if not isinstance(templates, dict) or "life_hud" not in templates:
                guard.zero_streak = 0
                return
            score = self.navigator.match(frame, "life_hud", LIFE_HUD_AREA)[0]
            sample["life_hud_score"] = float(score)
            if score < self.navigator.threshold:
                guard.zero_streak = 0
                return
            guard.hud_seen = True
            template = getattr(self.navigator, "zero_life_template", None)
            if not isinstance(template, ZeroLifeTemplate) and isinstance(templates.get("life_zero_value"), np.ndarray):
                template = ZeroLifeTemplate(templates["life_zero_value"])
                self.navigator.zero_life_template = template
            area = (1090, 16, 1187, 45) if guard.bar_area[1] == 48 else (1090, 8, 1187, 37)
            def read_zero(image):
                if not isinstance(template, ZeroLifeTemplate):
                    sample["status"] = "zero_template_unavailable"
                    return False
                sample["zero_template_score"] = float(template.score(image, area))
                return sample["zero_template_score"] >= .90
            sample["bar_observed"] = True
            sample["status"] = "observed"
            depleted = guard.observe(frame, read_zero)
            sample.update(filled_pixels=guard.bar_fill_pixels, total_pixels=guard.bar_total_pixels)
        except (ValueError, cv2.error) as error:
            guard.zero_streak = 0
            sample.update(status="detection_unknown", error=str(error))
        finally:
            sample.update(zero_streak=guard.zero_streak, death_confirmed=depleted)
            # 沿用开场现有帧和两秒节奏；不截图、不 OCR、不编码，释放后才保存证据。
            self.preplay_life_frames.append((sample, frame.copy()))
            report["preplay_life_monitor"] = {"capacity": 8, "sample_interval_s": 2., "capture_clock": "request_start",
                                               "samples": [item[0] for item in self.preplay_life_frames]}
            report["life_monitor"] = {"samples": guard.samples, "zero_confirmed": guard.zero_confirmed}
        if depleted:
            report["preplay_death_confirmed"] = report["death_confirmed"] = True
            report["death_confirmed_at"] = datetime.now().astimezone().isoformat()
            raise LifeDepleted("开场／首音同步中连续两帧确认生命零，停止启动并先清理触点")

    def recheck_failed_preplay_life(self, report):
        guard = self.life_guard
        samples = report.get("preplay_life_monitor", {}).get("samples", [])
        if (report.get("start_anchor") or report.get("preplay_death_confirmed")
                or not samples or samples[-1].get("zero_streak") != 1 or guard.zero_streak != 1
                or not release_finished(report)):
            return False
        # 首音拒绝不改成宽松启动；清理后只补一次正常间隔复查，单帧零不直接退出。
        self.pause(max(0., guard.last_sample_at + 2. - time.perf_counter()))
        captured_at = time.perf_counter()
        frame = self.screenshot()
        try:
            self.observe_preplay_life(frame, captured_at, report)
        except LifeDepleted as death:
            report["preplay_followup_death_reason"] = str(death)
            return True
        return False

    def save_preplay_life_evidence(self, report, directory):
        playback = report.get("playback")
        if playback and not release_finished(report):
            return
        for index, (sample, frame) in enumerate(getattr(self, "preplay_life_frames", ())):
            name = f"preplay-life-{index:02d}.png"
            write_image(directory / name, frame)
            sample["path"] = name

    def start(self, chart, identity, report: dict, directory: Path, *, ready_action=None, frame_guard=None,
              identity_phase="final", on_final_identity=None, opening_timeout=120, wait_for_opening=False,
              opening_deadline_provider=None) -> float:
        self.device.preflight()
        self.preplay_life_frames = deque(maxlen=8)
        if ready_action is None:
            self.navigator.tap(1013, 563, "开始单人谱面演出")
        else:
            # 准备页不足以确认时仍保留开场机会；确认之前只能操作准备按钮，不能派发谱面触控。
            ready_identity = ready_action()
            if identity is None and ready_identity is not None:
                identity = ready_identity
        clicked_at = time.perf_counter()
        deadline = time.monotonic() + opening_timeout
        final = None
        # 优先使用四帧；慢截图来不及取得第四帧时，只允许三帧低残差拟合且留足启动余量。
        minimum_samples = 4 if report.get("engine") == "native" else 2
        anchor = (StartAnchor(chart.first, minimum_samples=minimum_samples,
                              simultaneous_gestures=first_note_context(chart) if getattr(chart, "gestures", None) else (),
                              dense_following_gestures=dense_first_note_context(chart) if getattr(chart, "gestures", None) else ())
                  if chart is not None else None)
        loading_saved = False
        last_error = ""
        problem_priority = (-1, -1.0)
        samples = []
        captured_frames = []
        sampled_frames = {}

        def retained_anchor_frames():
            retained = {when: screenshot for when, screenshot in captured_frames}
            retained.update(sampled_frames)
            return sorted(retained.items())

        def update_anchor_attempt(*, failed=False, accepted=False):
            nonlocal samples, sampled_frames
            current = list(anchor.samples)
            wanted = {when for when, _ in current[-32:]}
            if current:
                wanted.add(current[0][0])
            available = {when: screenshot for when, screenshot in captured_frames}
            available.update(sampled_frames)
            # 引用现有帧，保留首点与最新 32 个采纳样本；候选替换时释放失效引用。
            sampled_frames = {when: available[when] for when in wanted if when in available}
            samples = current
            state = ("accepted" if accepted else "no_candidate" if not samples
                     else "candidate_unconfirmed" if len(samples) == 1 else "trajectory_pending")
            attempt = {"samples": samples, "sample_count": len(samples), "sample_state": state}
            if failed:
                reason = getattr(anchor, "failure_reason", None)
                attempt["failure_class"] = (reason if isinstance(reason, str)
                                            else "candidate_motion_unconfirmed" if len(samples) == 1
                                            else "trajectory_unaccepted" if samples else "no_candidate")
            attempt["sample_frame_capacity"] = 33
            attempt["sample_frames_retained"] = len(sampled_frames)
            attempt["sample_frames_truncated"] = len(current) > len(sampled_frames)
            attempt["frame_capacity"] = 65
            report["anchor_attempt"] = attempt

        def save_anchor_frames():
            report["anchor_frames"] = []
            for index, (when, screenshot) in enumerate(retained_anchor_frames()):
                name = f"anchor-frame-{index:02d}.png"
                write_image(directory / name, screenshot)
                report["anchor_frames"].append({"captured_at": when, "path": name})

        frame = None
        while time.monotonic() < (opening_deadline_provider() if final is None and opening_deadline_provider is not None else deadline):
            before = time.perf_counter()
            frame = self.screenshot()
            self.observe_preplay_life(frame, before, report)
            if frame_guard is not None:
                frame_guard(frame)
            # 帧在设备开始 screencap 时生成，gzip 传输及 Agent 图像解码不是拍摄时间。
            captured_at = before
            if not loading_saved and np.mean(frame[:560].min(axis=2) > 230) > .94:
                self.loading_frame = frame.copy()
                loading_saved = True
            if final is None:
                try:
                    difficulty = identity.difficulty if identity is not None else report["requested_difficulty"]
                    candidate = self.matcher.match(frame, difficulty, identity_phase)
                except ValueError as error:
                    last_error = str(error)
                    self.record_identity_check(report, "opening", captured_at=captured_at, error=last_error)
                    attempts = report.setdefault("final_identity_attempts", [])
                    if len(attempts) < 24:
                        attempts.append({"captured_at": captured_at, "error": last_error})
                    # 保留最接近封面的失败帧，避免只保存超时后的暂停页而丢失实际开场证据。
                    score = re.search(r"(?:最佳|封面)=([\d.]+)", last_error)
                    candidate_score = float(score[1]) if score else None
                    priority = (0, candidate_score) if score else (1, 0.0)
                    if priority > problem_priority:
                        problem_priority = priority
                        report["unconfirmed_identity"] = {"captured_at": captured_at, "error": last_error,
                                                          "cover_score": candidate_score}
                        self.identity_problem_frame = frame.copy()
                else:
                    self.record_identity_check(report, "opening", captured_at=captured_at)
                    if identity is not None and candidate.song_id != identity.song_id:
                        raise RuntimeError("准备页与最终封面的歌曲不同，拒绝触控")
                    report["final_identity"] = candidate.to_dict()
                    self.final_frame = frame.copy()
                    # 资源及 Native 初始化异常必须向外传播，不能作为动画中的 OCR 失败重试。
                    if on_final_identity is not None:
                        chart = on_final_identity(candidate)
                        if anchor is None:
                            anchor = StartAnchor(chart.first, minimum_samples=minimum_samples,
                                                 simultaneous_gestures=first_note_context(chart) if getattr(chart, "gestures", None) else (),
                                                 dense_following_gestures=dense_first_note_context(chart) if getattr(chart, "gestures", None) else ())
                    final = candidate
                    if opening_deadline_provider is not None:
                        # 房间等待可由调用方按正向进展计时；最终身份确认后固定窗口，不再延展首音。
                        deadline = time.monotonic() + 120
                    if wait_for_opening:
                        # 一键任务的五分钟只限制身份等待；确认后仍须取得完整首音轨迹，不能抢在中途启动。
                        deadline = time.monotonic() + 120
                    report["opening_detection"] = {"seconds_after_start": captured_at - clicked_at,
                                                   "captured_at": captured_at}
                    # 开场窗口短，只要求一次明确身份；关键时段不执行嵌套日志或编码图片。
                    print(f"开场身份已确认：{candidate.song_id} {candidate.title} {candidate.difficulty.upper()}", flush=True)
                if final is None:
                    if not wait_for_opening and self.navigator.match(frame, "playing")[0] >= self.navigator.threshold:
                        raise RuntimeError(f"两个识别阶段均未确认歌曲，未派发谱面输入：{last_error}")
                    self.pause(.02)
                    continue
            if self.navigator.match(frame, "playing")[0] >= self.navigator.threshold:
                captured_frames.append((captured_at, frame.copy()))
                if len(captured_frames) > 32:
                    # 长前奏会占满最初的空帧；保留原始基线和最近轨迹，确保失败后仍能回放首音。
                    del captured_frames[1]
                try:
                    epoch = anchor.observe(frame, captured_at, capture_seconds=time.perf_counter() - before)
                except Exception:
                    update_anchor_attempt(failed=True)
                    save_anchor_frames()
                    raise
                if list(anchor.samples) != samples:
                    update_anchor_attempt()
                if epoch is not None:
                    update_anchor_attempt(accepted=True)
                    report["start_anchor"] = {"epoch": epoch, "first_note_time": chart.first.start,
                                              "samples": samples, "capture_seconds": time.perf_counter() - before,
                                              "capture_clock": "request_start", "fit": anchor.fit}
                    if len(samples) >= 2:
                        origin = samples[0][0]
                        report["start_anchor"]["trajectory_rate"] = float(np.polyfit(
                            np.array([when - origin for when, _ in samples]),
                            np.log(np.array([y + 30 for _, y in samples])), 1)[0])
                    # MFA 日志的嵌套任务会阻塞调度；开场临界区只写进程日志和报告。
                    print(f"第一音锚点已建立，按本地谱面派发 {len(chart.gestures)} 个手势", flush=True)
                    # 锚点建立后不在触控前编码截图，避免错过第一音；结算时再保存证据。
                    self.anchor_frames = retained_anchor_frames()
                    return epoch
            self.pause(.002)
        if frame is not None:
            write_image(directory / "start-failure.png", frame)
        if anchor is not None:
            update_anchor_attempt(failed=True)
        save_anchor_frames()
        if wait_for_opening and final is None:
            raise TimeoutError(f"等待最终歌曲封面超过 {opening_timeout} 秒，未派发谱面输入：{last_error}")
        raise RuntimeError(f"开场封面或第一音锚点未确认，未派发谱面输入：{last_error}")

    def read_result(self, frame: np.ndarray) -> LiveResult | None:
        label = self.ocr.read(frame, (151, 393, 290, 434))
        if "PERFECT" not in label.text.upper():
            return None
        boxes = {"perfect": (302, 391, 384, 434), "great": (302, 435, 384, 475),
                 "good": (302, 479, 384, 519), "bad": (302, 522, 384, 562), "miss": (302, 565, 384, 605)}
        try:
            values = {key: self.read_judgement_number(frame, box) for key, box in boxes.items()}
            for key, box in {"late": (448, 514, 554, 542), "fast": (556, 514, 638, 542),
                             "flick": (609, 557, 643, 590), "combo": (536, 390, 668, 435),
                             "score": (282, 205, 670, 274)}.items():
                try:
                    values[key] = self.read_optional_number(frame, box)
                except ValueError:
                    values[key] = None
            return LiveResult(**values)
        except ValueError:
            return None

    def read_judgement_number(self, frame: np.ndarray, box: tuple[int, int, int, int]) -> int:
        raw = self.ocr.read(frame, box)
        x1, y1, x2, y2 = box
        crop = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
        # 灰色前导零在深色判定面板上对比不足；统一转成黑字白底后再读数。
        digits = cv2.cvtColor(255 - cv2.threshold(crop, 145, 255, cv2.THRESH_BINARY)[1], cv2.COLOR_GRAY2BGR)
        digits = cv2.copyMakeBorder(digits, 4, 4, 4, 4, cv2.BORDER_CONSTANT, value=(255, 255, 255))
        processed = self.ocr.read(digits, (0, 0, digits.shape[1], digits.shape[0]))
        values = []
        for reading in (raw, processed):
            try:
                values.append(numeric(reading))
            except ValueError:
                continue
        if not values or len(set(values)) != 1:
            raise ValueError(f"判定数字无法确认或两种读法冲突：{raw}，{processed}")
        return values[0]

    def read_optional_number(self, frame: np.ndarray, box: tuple[int, int, int, int]) -> int:
        raw = self.ocr.read(frame, box)
        try:
            return numeric(raw)
        except ValueError:
            x1, y1, x2, y2 = box
            hsv = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2HSV)
            # FAST/LATE 的蓝色条会干扰浅色单个零；只保留白色数字后重读。
            mask = cv2.inRange(hsv, (0, 0, 200), (179, 60, 255))
            points = cv2.findNonZero(mask)
            if points is None:
                raise ValueError("可选结算数字为空")
            x, y, width, height = cv2.boundingRect(points)
            mask = mask[y:y + height, x:x + width]
            digits = cv2.cvtColor(255 - mask, cv2.COLOR_GRAY2BGR)
            # 单个零需要足够横向留白，过窄的二值区域会被多语模型误读为字母 O。
            # 窄笔画的 1 需要更紧的横向留白；保留置信度门槛，不能将低置信度强行确认为数字。
            vertical, horizontal = (4, 4) if width <= 6 else (6, 12)
            digits = cv2.copyMakeBorder(digits, vertical, vertical, horizontal, horizontal,
                                       cv2.BORDER_CONSTANT, value=(255, 255, 255))
            return numeric(self.ocr.read(digits, (0, 0, digits.shape[1], digits.shape[0])))

    def observe_playfield(self, frame: np.ndarray, directory: Path, interrupted_message: str) -> bool:
        guard = self.life_guard
        if blank_transition(frame):
            guard.playfield_missing_frames = guard.zero_streak = 0
            return False
        pause_score = self.navigator.match(frame, "playing", (1190, 10, 1256, 80))[0]
        life_score = (self.navigator.match(frame, "life_hud", LIFE_HUD_AREA)[0]
                      if "life_hud" in self.navigator.templates else None)
        life_visible = life_score is not None and life_score >= self.navigator.threshold
        visible = pause_score >= self.navigator.threshold or life_visible
        if visible:
            guard.playfield_missing_frames = 0
            guard.hud_seen |= life_visible or life_score is None
            if pause_score < self.navigator.threshold:
                guard.pause_fallback_frames += 1
        else:
            guard.playfield_missing_frames += 1
            guard.zero_streak = 0
        report = getattr(self, "last_report", None)
        if isinstance(report, dict):
            report["playfield_monitor"] = {"pause_score": pause_score, "life_hud_score": life_score,
                                           "consecutive_missing": guard.playfield_missing_frames,
                                           "pause_fallback_frames": guard.pause_fallback_frames}
        # 暂停按钮会被特效干扰；生命标签仍在就继续，一张缺失帧不能中断已确认歌曲的输入。
        if guard.playfield_missing_frames >= 2:
            write_image(directory / "playback-interrupted.png", frame)
            raise RuntimeError(interrupted_message)
        return visible

    def begin_performance_trace(self):
        self.performance_trace = PerformanceTrace()
        self.performance_trace_error = None

    def set_performance_trace_plan(self, events):
        trace = getattr(self, "performance_trace", None)
        if trace is not None:
            trace.set_plan(events)

    def record_performance_trace(self, elapsed, previous_samples, *, zero_template_score=None):
        trace = getattr(self, "performance_trace", None)
        if trace is None or getattr(self, "performance_trace_error", None):
            return
        try:
            playback = None
            player = getattr(self, "active_chart_player", None)
            if player is not None:
                playback = {"sent_actions": player.sent_actions, "active_contacts": sorted(player.active)}
            trace.observe(elapsed, self.last_report, self.life_guard,
                          life_sampled=self.life_guard.samples > previous_samples,
                          zero_template_score=zero_template_score, playback=playback)
        except Exception as error:
            # 诊断不能反压派发或中断演出；失败信息等输入清理后再写入正式报告。
            self.performance_trace_error = f"{type(error).__name__}: {error}"

    def finish_performance_trace(self, report, directory):
        trace = getattr(self, "performance_trace", None)
        if trace is None:
            return
        self.performance_trace = None
        try:
            if getattr(self, "performance_trace_error", None):
                raise RuntimeError(self.performance_trace_error)
            summary = trace.save(directory, report)
        except Exception as error:
            summary = {"status": "failed", "error": f"{type(error).__name__}: {error}"}
        report["performance_trace"] = summary
        report.setdefault("performance_traces", []).append(summary)

    def observe_play_state(self, directory: Path):
        requested_at = time.perf_counter()
        frame = self.screenshot()
        # 诊断仅保留已有采样，不增加截图或改变输入；整局保存也限定在 180 秒内。
        capture_limit = 180 if os.environ.get("MAAPJSK_DIAGNOSTIC_FULL_LIFE_FRAMES") == "1" else 8
        if requested_at - self.life_epoch < capture_limit:
            self.life_frames.append((requested_at - self.life_epoch, frame))
        previous_samples = self.life_guard.samples
        try:
            if not self.observe_playfield(frame, directory, "演奏场提前消失，停止谱面输入"):
                if self.navigator.match(frame, "live_failed")[0] >= self.navigator.threshold:
                    write_image(directory / "playback-interrupted.png", frame)
                    self.life_guard.zero_confirmed = True
                    raise LifeDepleted("游戏已确认 LIVE FAILED，停止输入并退出演出")
                return
            def read_zero(image):
                try:
                    return self.read_optional_number(image, (1090, 8, 1187, 37)) == 0
                except ValueError:
                    return False
            if self.life_guard.observe(frame, read_zero):
                write_image(directory / "life-zero.png", frame)
                raise LifeDepleted("连续两帧确认生命归零，停止输入并退出演出")
        finally:
            self.record_performance_trace(requested_at - self.life_epoch, previous_samples)

    def dialog_action(self, frame, labels):
        # 只在已经暂停或退出确认的场景定位白色按钮，避免把游戏场景文字当成操作入口。
        mask = cv2.inRange(frame, (245, 245, 245), (255, 255, 255))
        mask |= cv2.inRange(cv2.cvtColor(frame, cv2.COLOR_BGR2HSV), (75, 55, 150), (95, 255, 255))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidates = []
        for contour in contours:
            x, y, width, height = cv2.boundingRect(contour)
            if (200 <= x <= 1080 and 250 <= y <= 650 and 80 <= width <= 420 and 28 <= height <= 110
                    and cv2.contourArea(contour) > width * height * .65):
                candidates.append((x, y, width, height))
        for x, y, width, height in sorted(candidates, key=lambda item: item[1], reverse=True)[:16]:
            reading = self.ocr.read(frame, (x, y, x + width, y + height))
            if reading.confidence >= .75 and normalize_title(reading.text) in labels:
                return x + width // 2, y + height // 2
        return None

    def exit_depleted_live(self, report: dict, directory: Path):
        self.navigator._check_stop()
        report["live_status"] = "life_depleted"
        report["completed"] = False
        self.log("生命归零：触点已释放，暂停并退出本局；本局不计入完成次数")
        frame = self.screenshot()
        if (self.dialog_action(frame, {"リタイア"}) is None
                and self.navigator.match(frame, "playing", (1190, 10, 1256, 80))[0] >= self.navigator.threshold):
            self.navigator.tap(1223, 44, "生命归零后暂停演出")
            self.pause(.4)
        deadline = time.monotonic() + 25
        for attempt in range(48):
            if time.monotonic() >= deadline:
                break
            frame = self.screenshot()
            write_image(directory / f"life-exit-{attempt:02d}.png", frame)
            if self.navigator.match(frame, "home")[0] >= self.navigator.threshold:
                report["life_exit"] = {"confirmed": True, "page": "home"}
                return
            if any(self.navigator.match(frame, page)[0] >= self.navigator.threshold
                   for page in ("song_select", "prepare", "live_menu")):
                self.navigator.return_to_home()
                report["life_exit"] = {"confirmed": True, "page": "home"}
                return
            point = self.dialog_action(frame, {"リタイア", "あきらめる", "終了", "はい", "ok", "確認"})
            if point is not None:
                self.navigator.tap(*point, "暂停页退出或退出确认")
            elif self.navigator.match(frame, "live_failed")[0] >= self.navigator.threshold:
                self.device.back()
            self.pause(.6)
        raise RuntimeError("生命归零已停止触控，但退出页面未确认；保留截图并停止任务")

    def play(self, events, epoch: float, offset_ms: int, report: dict, directory: Path):
        def observe_state():
            self.observe_play_state(directory)

        player = ChartPlayer(self.device.controller, self.stop_requested, idle_observer=observe_state)
        self.active_chart_player = player
        trace = getattr(self, "performance_trace", None)
        if trace is not None:
            trace.set_legacy_feedback(player.lateness)
        try:
            report["playback"] = player.play(events, epoch, offset_ms)
        finally:
            self.active_chart_player = None
            if "playback" not in report:
                report["playback"] = {"release_confirmed": not player.active,
                                      "active_contacts": sorted(player.active), "sent_actions": player.sent_actions,
                                      "planned_actions": len(events),
                                      "lateness_ms_max": max(player.lateness, default=0) * 1000}

    def date_update_notice(self, frame):
        if blank_transition(frame) or 'life_hud' not in self.navigator.templates:
            return None
        hsv = cv2.cvtColor(frame[220:500, 220:1100], cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, (0, 0, 225), (179, 25, 245))
        _, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        modal = next((row for row in stats[1:]
                      if abs(row[0] - 31) <= 8 and abs(row[1] - 28) <= 8
                      and abs(row[2] - 778) <= 16 and abs(row[3] - 224) <= 16
                      and row[4] >= row[2] * row[3] * .86), None)
        if modal is None:
            return None
        if (self.navigator.match(frame, 'playing')[0] >= self.navigator.threshold
                or self.navigator.match(frame, 'life_hud', LIFE_HUD_AREA)[0] >= self.navigator.threshold):
            return None
        # 只在结算的已知中部弹窗几何成立后读固定两行；普通结算与开场不追加 OCR。
        updated = self.ocr.read(frame, (510, 306, 778, 337))
        title = self.ocr.read(frame, (520, 335, 774, 365))
        if (updated.confidence < .85 or title.confidence < .85
                or normalize_title(updated.text) != normalize_title('日付が更新されました。')
                or normalize_title(title.text) != normalize_title('タイトルに戻ります。')):
            return None
        return {'updated': asdict(updated), 'return_to_title': asdict(title),
                'modal_box': [int(modal[0] + 220), int(modal[1] + 220), int(modal[2]), int(modal[3])]}

    def collect(self, report: dict, directory: Path):
        deadline = time.monotonic() + 120
        completed = False
        failed = False
        result = None
        last_candidate = None
        stable = 0
        settlement_index = 0
        date_frames = []
        end_guard = LiveEndGuard(self.navigator, report, life_seen=getattr(getattr(self, "life_guard", None), "hud_seen", False))

        def inspect_date_update(frame):
            if not release_finished({**report, 'playback': report.get('playback') or {}}):
                date_frames.clear()
                return False
            notice = self.date_update_notice(frame)
            if notice is None:
                date_frames.clear()
                return False
            date_frames.append(frame)
            if len(date_frames) < 2:
                self.pause(.3)
                return True
            report.update(requires_relogin=True, requires_relogin_reason='game_date_updated_return_to_title',
                          requires_relogin_at=datetime.now().astimezone().isoformat(),
                          date_update_frame='date-update-confirmed.png')
            report['date_update_notice'] = {**notice, 'confirmation_frames': 2,
                                            'frames': ['date-update-first.png', 'date-update-confirmed.png']}
            report['result_status'] = 'recorded' if report.get('judgements') else 'unreadable'
            # 当前释放已成立才写证据；日更必须人工重登，不能用结算安全像素或 BACK 关闭。
            write_image(directory / 'date-update-first.png', date_frames[0])
            write_image(directory / 'date-update-confirmed.png', date_frames[1])
            raise RuntimeError('游戏日期已更新，需要重新登录；停止结算页面输入')

        def require_current_release():
            if not release_finished({**report, 'playback': report.get('playback') or {}}):
                report['completed'] = False
                raise RuntimeError('结算尚未取得本轮严格触点释放证明，禁止页面输入或记为完成')

        def inspect_settlement(frame):
            nonlocal stable, last_candidate, result, completed, failed
            end_guard.observe(frame)
            clear_score = self.navigator.match(frame, "live_clear")[0]
            failed_score = self.navigator.match(frame, "live_failed")[0] if "live_failed" in self.navigator.templates else 0
            if clear_score >= self.navigator.threshold and clear_score > failed_score + .04:
                completed = True
                report["live_status"] = "cleared"
                write_image(directory / "live-clear.png", frame)
            if failed_score >= self.navigator.threshold and failed_score > clear_score + .04:
                failed = True
                report["live_status"] = "failed"
                write_image(directory / "live-failed.png", frame)
            candidate = self.read_result(frame)
            if candidate is not None:
                stable = stable + 1 if candidate == last_candidate else 1
                last_candidate = candidate
                if stable >= 2:
                    result = candidate.to_dict()
                    expected_total = report.get("chart", {}).get("total_note_count")
                    result["expected_total"] = expected_total
                    result["total_matches_chart"] = result["total"] == expected_total if expected_total else None
                    report["judgements"] = result
                    write_image(directory / "result.png", frame)
            return candidate

        while time.monotonic() < deadline:
            frame = self.screenshot()
            if inspect_date_update(frame):
                continue
            if blank_transition(frame):
                end_guard.observe(frame)
                self.pause(.3)
                continue
            candidate = inspect_settlement(frame)
            if self.navigator.match(frame, "home")[0] >= self.navigator.threshold:
                require_current_release()
                if failed:
                    report["result_status"] = "recorded" if result else "unreadable"
                    raise RuntimeError("游戏判定演出失败，本局未计入完成次数")
                if not (completed or end_guard.ended):
                    self.pause(.2)
                    continue
                report["returned_home"] = True
                report["performance_completed"] = True
                report["completed"] = result is not None and result.get("total_matches_chart") is True
                if result is None:
                    report["result_status"] = "unreadable"
                    report["settlement_warning"] = "判定数字未完整读取，保留证据并继续下一曲"
                elif result.get("total_matches_chart") is False:
                    report["result_status"] = "note_count_mismatch"
                    report["settlement_warning"] = "结算音数未匹配本地谱面，保留实际数字并继续下一曲"
                else:
                    report["result_status"] = "recorded"
                return
            write_image(directory / "settlement-last.png", frame)
            if self.navigator.match(frame, "playing")[0] >= self.navigator.threshold and not completed:
                self.pause(.4)
                continue
            if not (completed or failed or end_guard.ended):
                self.pause(.2)
                continue
            if settlement_index < 40:
                settlement_index += 1
                write_image(directory / f"settlement-{settlement_index:02d}.png", frame)
            # 判定页先采集两帧；其余结算只用安全像素加速与 BACK 推进。
            if candidate is not None and stable < 2:
                self.pause(.3)
                continue
            require_current_release()
            self.device.tap(1279, 719)
            # 奖励退出动画会先显示模糊主页；留出切换时间后再读，避免旧帧诱发多余 BACK。
            self.pause(1.2)
            confirmation = self.screenshot()
            if inspect_date_update(confirmation):
                continue
            if blank_transition(confirmation):
                end_guard.observe(confirmation)
                self.pause(.3)
                continue
            # 判定页也可能在这张复查帧才出现，先取得两帧数字再返回。
            confirmation_candidate = inspect_settlement(confirmation)
            if confirmation_candidate is not None and stable < 2:
                self.pause(.3)
                continue
            # 动画加速点击可能已切换到主页；必须重新检查后才能发送 BACK。
            if self.navigator.match(confirmation, "home")[0] >= self.navigator.threshold:
                continue
            if self.navigator.match(confirmation, "playing")[0] >= self.navigator.threshold:
                continue
            if np.mean(confirmation.max(axis=2) < 15) > .95:
                self.pause(.6)
                continue
            require_current_release()
            self.device.back()
            self.pause(1.0)
        raise TimeoutError("结算返回超时")

    def run(self, count: int, difficulty: str, song_mode: str, offset_ms: int = 0, *,
            bonus_consumption: str | int = "current", recovery_mode: str = "off", recovery_count: int = 1,
            engine: str = "legacy", latency_offsets: dict | None = None, calibration_profile: dict | None = None,
            song_name: str | None = None, _bonus_snapshot: dict | None = None):
        if difficulty not in {*DIFFICULTY_POINTS, "append"} or song_mode not in {"current", "random", "specified"}:
            raise ValueError("单人谱面任务选项无效")
        expected_song_id = self.resolve_specified_song(song_name, difficulty) if song_mode == "specified" else None
        if (bonus_consumption != "current" and (isinstance(bonus_consumption, bool)
                or not isinstance(bonus_consumption, int) or not 0 <= bonus_consumption <= 10)):
            raise ValueError("每局体力消耗只能沿用游戏设置或指定 0 到 10")
        if (recovery_mode not in {"off", "small", "large"} or isinstance(recovery_count, bool)
                or not isinstance(recovery_count, int) or not 1 <= recovery_count <= 99):
            raise ValueError("自动用药配置无效")
        self.device.preflight()
        if engine not in {"legacy", "native"}:
            raise ValueError("演奏引擎无效")
        self.completed_rounds = 0
        # 普通 run 每次都是新任务；校准的排练与验证显式共用同一个任务快照。
        bonus_snapshot = {} if _bonus_snapshot is None else _bonus_snapshot
        reports = []
        consumption_label = "沿用游戏设置" if bonus_consumption == "current" else f"每局 {bonus_consumption} 体力"
        recovery_label = "自动用药关闭" if recovery_mode == "off" else f"不足时使用 {recovery_count} 瓶{'小' if recovery_mode == 'small' else '大'}饮料"
        self.log(f"单人谱面演出：已完成 0 / 总数 {count}；{difficulty.upper()}；{consumption_label}；{recovery_label}")
        for round_index in range(1, count + 1):
            native_player = None
            self.life_guard = LifeGuard()
            self.life_frames = []
            directory = self.report_root / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:8])
            directory.mkdir(parents=True)
            report = {"schema_version": 1, "started_at": datetime.now().astimezone().isoformat(),
                      "requested_difficulty": difficulty, "song_mode": song_mode, "round": round_index,
                      "total_rounds": count, "completed": False, "timing_offset_ms": offset_ms,
                      "requested_bonus_consumption": bonus_consumption,
                      "recovery_settings": {"mode": recovery_mode, "count": recovery_count}}
            report["engine"] = engine
            if expected_song_id is not None:
                report.update(requested_song_name=song_name, expected_song_id=expected_song_id)
            self.last_report = report
            self.begin_performance_trace()
            report["report_path"] = str(directory / "report.json")
            try:
                identity, frame = self.select(difficulty, song_mode,
                                              **({"song_name": song_name} if song_mode == "specified" else {}))
                report["preparation_identity"] = identity.to_dict()
                self.record_identity_check(report, "preparation")
                write_image(directory / "prepare.png", frame)
                entry = self.repository.songs[identity.song_id]["charts"][difficulty]
                report["chart"] = {key: entry[key] for key in ("music_id", "difficulty", "sha256", "path", "total_note_count")}
                chart = parse_sus(self.repository.load_chart(identity.song_id, difficulty))
                events = compile_touches(chart)
                self.set_performance_trace_plan(events)
                report["chart"]["duration"] = chart.duration
                report["chart"]["planned_actions"] = len(events)
                self.prepare_task_bonus(bonus_consumption, report, directory, bonus_snapshot)
                self.prepare_playback(report)
                self.ensure_bonus_available(recovery_mode, recovery_count, report, directory)
                if engine == "native":
                    from .native_player import NativePlayer
                    native_player = NativePlayer(self.device.controller, events, directory, self.stop_requested,
                                                 latency_offsets=latency_offsets,
                                                 idle_observer=lambda: self.observe_play_state(directory))
                    # 开场死亡也须清理同一个已准备对象，并保留零派发的释放证明。
                    report["playback"] = native_player.report
                    native_player.prepare()
                self.log("点击开始后持续等待加载与开场封面；确认歌曲、标题和难度后同步首音")
                epoch = self.start(chart, identity, report, directory)
                self.life_epoch = epoch
                if calibration_profile is not None:
                    verify_profile_anchor(calibration_profile, report)
                _write_json(directory / "report.json", report)
                playback_error = None
                try:
                    if native_player is not None:
                        report["playback"] = native_player.report
                        try:
                            native_player.play(epoch, offset_ms)
                        finally:
                            native_player.close()
                            native_player = None
                    else:
                        self.play(events, epoch, offset_ms, report, directory)
                except Exception as error:
                    if self.stop_requested():
                        raise
                    playback_error = error
                    report["playback_error"] = f"{type(error).__name__}: {error}"
                report["life_monitor"] = {"samples": self.life_guard.samples,
                                           "zero_confirmed": self.life_guard.zero_confirmed}
                if isinstance(playback_error, LifeDepleted):
                    if not report.get("playback", {}).get("release_confirmed"):
                        raise RuntimeError("生命归零后的触点释放未确认，禁止继续点击退出")
                    self.exit_depleted_live(report, directory)
                    raise playback_error
                report["phase"] = "settlement"
                _write_json(directory / "report.json", report)
                self.collect(report, directory)
                if playback_error is not None:
                    report["completed"] = False
                    raise playback_error
            except Exception as error:
                report["completed"] = False
                report["error"] = f"{type(error).__name__}: {error}"
                report["cancelled"] = self.stop_requested()
                if not self.stop_requested() and not report.get("preplay_death_confirmed"):
                    try:
                        write_image(directory / "failure.png", self.device.screenshot())
                    except Exception:
                        pass
                raise
            finally:
                if native_player is not None:
                    report["playback"] = native_player.report
                    try:
                        native_player.close()
                    except Exception as release_error:
                        report["completed"] = False
                        report["release_error"] = f"{type(release_error).__name__}: {release_error}"
                        self.finish_performance_trace(report, directory)
                        _write_json(directory / "report.json", report)
                        if not report.get("preplay_death_confirmed"):
                            raise
                followup_death = False
                if not report.get("start_anchor") and not report.get("playback") and engine == "legacy":
                    report["playback"] = {"engine": "legacy", "sent_actions": 0, "executed_actions": 0,
                                          "release_confirmed": True, "release": {"release_proof": "no-gameplay-started"}}
                if not self.stop_requested():
                    try:
                        followup_death = self.recheck_failed_preplay_life(report)
                    except InterruptedError:
                        report["cancelled"] = True
                    except Exception as followup_error:
                        report["preplay_followup_error"] = f"{type(followup_error).__name__}: {followup_error}"
                if report.get("preplay_death_confirmed") and not self.stop_requested():
                    if not report.get("playback") and engine == "legacy":
                        report["playback"] = {"engine": "legacy", "sent_actions": 0, "executed_actions": 0,
                                              "release_confirmed": True, "release": {"release_proof": "no-gameplay-started"}}
                    if release_finished(report):
                        try:
                            self.exit_depleted_live(report, directory)
                        except Exception as exit_error:
                            if self.stop_requested():
                                report["cancelled"] = True
                            report["exit_error"] = f"{type(exit_error).__name__}: {exit_error}"
                    else:
                        report.setdefault("release_error", "开场死亡的当前触点释放未确认，禁止页面退出")
                self.save_preplay_life_evidence(report, directory)
                self.finish_performance_trace(report, directory)
                for attribute, name in (("loading_frame", "loading.png"), ("final_frame", "final-cover.png"),
                                        ("identity_problem_frame", "identity-unconfirmed.png")):
                    frame = getattr(self, attribute, None)
                    if frame is not None:
                        write_image(directory / name, frame)
                        delattr(self, attribute)
                for index, (when, frame) in enumerate(getattr(self, "anchor_frames", [])):
                    name = f"anchor-frame-{index:02d}.png"
                    write_image(directory / name, frame)
                    report.setdefault("anchor_frames", []).append({"captured_at": when, "path": name})
                self.anchor_frames = []
                for index, (when, frame) in enumerate(self.life_frames):
                    name = f"life-frame-{index:02d}.png"
                    write_image(directory / name, frame)
                    report.setdefault("life_frames", []).append({"elapsed_s": when, "path": name})
                self.life_frames = []
                report["finished_at"] = datetime.now().astimezone().isoformat()
                try:
                    _write_json(directory / "report.json", report)
                    append_result_index(self.report_root, directory, report)
                except Exception as error:
                    report["completed"] = False
                    report["persistence_error"] = f"{type(error).__name__}: {error}"
                    _write_json(directory / "report.json", report)
                    raise
                if followup_death:
                    raise LifeDepleted(report["preplay_followup_death_reason"])
            if report.get("completed"):
                self.completed_rounds += 1
            self.log(f"单人谱面演出：已完成 {self.completed_rounds} / 总数 {count}；报告 {directory.name}")
            if report.get("settlement_warning"):
                self.log(f"{report['settlement_warning']}；已演出 {round_index}/{count}，完整记录 {self.completed_rounds}")
            if report.get("judgements"):
                values = report["judgements"]
                self.log(f"PERFECT {values['perfect']}，GREAT {values['great']}，GOOD {values['good']}，BAD {values['bad']}，MISS {values['miss']}；PERFECT 占比 {values['perfect_rate']:.2%}")
            reports.append(report)
        return reports
