from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Callable

import cv2
import numpy as np

from .sus_chart import Chart, Gesture, slide_x

TOUCH_PLAN_VERSION = 3
START_ANCHOR_VERSION = 3


def touch_chains(chart: Chart) -> list[list[Gesture]]:
    gestures = sorted(chart.gestures, key=lambda value: value.start)
    chains: list[list[Gesture]] = []
    for index, gesture in enumerate(gestures):
        previous = None
        if gesture.kind == "trace":
            x = gesture.points[0].x
            conflict = False
            for future in gestures[index + 1:]:
                if future.start - gesture.start > .125:
                    break
                point = future.points[0]
                if (future.kind != "trace" and point.x - point.width * 1000 / 24 <= x
                        <= point.x + point.width * 1000 / 24):
                    conflict = True
                    break
            if conflict:
                # 轨迹不能用新 DOWN 提前击中下一颗蓝键；延续已经按下的前一触点再移动过去。
                candidates = [chain for chain in chains if chain[-1].kind in {"tap", "trace"}
                              and not chain[-1].flick and .035 < gesture.start - chain[-1].end <= .35]
                if candidates:
                    previous = min(candidates, key=lambda chain: gesture.start - chain[-1].end
                                   + abs(x - chain[-1].points[-1].x) / 5000)
        if previous is None:
            chains.append([gesture])
        else:
            previous.append(gesture)
    return chains


@dataclass(frozen=True, order=True)
class Touch:
    time: float
    order: int
    contact: int
    kind: str
    x: int = 0
    y: int = 570


def compile_touches(chart: Chart) -> tuple[Touch, ...]:
    events: list[Touch] = []
    free_after = [-math.inf] * 10
    for chain in sorted(touch_chains(chart), key=lambda values: (values[0].start - (.018 if values[0].flick and values[0].kind != "slide" else 0))):
        gesture = chain[0]
        last = chain[-1]
        start = gesture.start - (.018 if gesture.flick and gesture.kind != "slide" else 0)
        end = last.end + (.050 if last.flick else .030 if last.kind == "trace" else .024)
        if gesture.kind == "trace":
            start = gesture.start - .020
        possible = [index for index, released in enumerate(free_after) if released < start - .004]
        if not possible:
            raise ValueError("谱面需要超过十个并发触点，拒绝开演")
        contact = min(possible, key=lambda index: free_after[index])
        free_after[contact] = end
        events.append(Touch(start, 2, contact, "down", round(gesture.points[0].x)))
        if gesture.kind == "slide":
            count = max(1, math.ceil((gesture.end - gesture.start) / .020))
            for index in range(1, count + 1):
                when = gesture.start + (gesture.end - gesture.start) * index / count
                events.append(Touch(when, 1, contact, "move", round(slide_x(gesture.points, when))))
        for trace in chain[1:]:
            events.append(Touch(trace.start - .020, 1, contact, "move", round(trace.points[0].x)))
        if last.flick:
            point = last.points[-1]
            direction = -1 if last.flick in {3, 5} else 1 if last.flick in {4, 6} else 0
            vertical = 1 if last.flick in {2, 5, 6} else -1
            for step in range(1, 4):
                x = max(130, min(1150, round(point.x + direction * 35 * step)))
                y = 570 + vertical * 28 * step
                events.append(Touch(last.end + .012 * (step - 1), 1, contact, "move", x, y))
        events.append(Touch(end, 0, contact, "up"))
    result = tuple(sorted(events))
    validate_touches(result)
    return result


def validate_touches(events: tuple[Touch, ...]) -> None:
    active = set()
    for event in events:
        if not math.isfinite(event.time) or not 0 <= event.contact <= 9:
            raise ValueError("触控时间或编号无效")
        if event.kind == "down":
            if event.contact in active:
                raise ValueError("触点生命周期重叠")
            active.add(event.contact)
        elif event.kind in {"move", "up"}:
            if event.contact not in active:
                raise ValueError("移动或抬起了未按下的触点")
            if event.kind == "up":
                active.remove(event.contact)
        else:
            raise ValueError("未知触控动作")
    if active:
        raise ValueError("谱面结束仍有触点未释放")


def first_note_y(frame: np.ndarray, gesture: Gesture, baseline: np.ndarray | None = None) -> float | None:
    """只追踪谱面第一音的开场锚点；演奏开始后不再识别音符。"""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    if gesture.critical or gesture.points[0].kind in {2, 6}:
        mask = cv2.inRange(hsv, (15, 55, 135), (45, 255, 255))
    elif gesture.flick:
        mask = cv2.inRange(hsv, (140, 65, 135), (179, 255, 255))
    elif gesture.kind in {"trace", "slide"}:
        # 轨迹底带较暗；只用亮色横条，避免与底带连通而丢失首音。
        mask = cv2.inRange(hsv, (50, 55, 205), (95, 255, 255))
    else:
        mask = cv2.inRange(hsv, (95, 55, 135), (135, 255, 255))
    if baseline is not None:
        original = cv2.cvtColor(baseline, cv2.COLOR_BGR2HSV).astype(np.int16)
        current = hsv.astype(np.int16)
        hue_delta = np.abs(current[:, :, 0] - original[:, :, 0])
        hue_delta = np.minimum(hue_delta, 180 - hue_delta)
        saturation_delta = np.abs(current[:, :, 1] - original[:, :, 1])
        changed = (hue_delta > 6) | (saturation_delta > 35)
        # 开场淡入只改变底图亮度，不能因此把底图文字当作首音。
        mask[~changed] = 0
    mask[:40] = 0
    mask[540:] = 0
    point = gesture.points[0]
    candidates = []
    for contour in cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]:
        x, y, width, height = cv2.boundingRect(contour)
        center_y = y + height / 2
        projected_x = 640 + (point.x - 640) * center_y / 570
        expected_width = point.width * (1000 / 12) * center_y / 570
        # 音符随透视靠近判定线会变厚；固定高度上限会过滤首音并误取后面的音符。
        if (width > 14 and 2 <= height <= max(36, center_y * .12) and width > height * 2
                and abs(x + width / 2 - projected_x) < max(25, expected_width * .35)
                and expected_width * .5 < width < expected_width * 1.5 + 12):
            candidates.append(center_y)
    return max(candidates) if candidates else None


class StartAnchor:
    def __init__(self, gesture: Gesture, *, minimum_samples: int = 2) -> None:
        self.gesture = gesture
        self.minimum_samples = minimum_samples
        self.samples: list[tuple[float, float]] = []
        self.baseline: np.ndarray | None = None
        self.fit: dict | None = None

    def observe(self, frame: np.ndarray, captured_at: float, *, capture_seconds: float = 0.0) -> float | None:
        if self.baseline is None:
            self.baseline = frame.copy()
            return None
        if self.samples and captured_at - self.samples[0][0] > 2:
            raise RuntimeError("第一音通过后仍未建立锚点，拒绝从歌曲中途开始")
        y = first_note_y(frame, self.gesture, self.baseline)
        if y is None:
            return None
        if self.samples:
            previous_time, previous_y = self.samples[-1]
            if captured_at <= previous_time:
                return None
            if y < previous_y - 10:
                # 单个候选尚未证明运动；首音从顶端出现时可替换淡入背景噪声，已建立的轨迹不重置。
                if len(self.samples) == 1 and y < 130:
                    self.samples[0] = (captured_at, y)
                return None
            # 静止判定线或背景不应累积成时间锚点。
            if abs(y - previous_y) < 3:
                if len(self.samples) == 1:
                    self.samples[0] = (captured_at, y)
                return None
        self.samples.append((captured_at, y))
        recent = self.samples[-5:]
        if len(recent) < 2 or recent[-1][1] - recent[0][1] < 25:
            return None
        short_window = len(recent) < self.minimum_samples
        if short_window and len(recent) != 3:
            return None
        if len(recent) == 2:
            # 慢截图链路会让第三帧晚于首音；只允许已实机校验的透视模型提前同步。
            if not (recent[0][1] < 130 and 130 <= y <= 280):
                return None
            elapsed = recent[1][0] - recent[0][0]
            rate = math.log((y + 30) / (recent[0][1] + 30)) / elapsed
            first_hit = captured_at + math.log(600 / (y + 30)) / rate
            if not (.06 <= elapsed <= .5 and 1 <= rate <= 10 and .35 <= first_hit - captured_at <= .8):
                return None
            self.fit = {"model": "calibrated_perspective", "residual_pixels": None, "first_hit": first_hit}
            return first_hit - self.gesture.start
        if y < 230:
            return None
        origin = recent[0][0]
        offsets = np.array([sample[0] - origin for sample in recent])
        heights = np.array([sample[1] for sample in recent])
        candidates = []
        # 实际开场帧的下落带透视加速；比较模型残差，不能用直线外推强行同步。
        for name, shift in (("linear", 0), ("exponential", 0), ("perspective", 30)):
            transformed = heights if name == "linear" else np.log(heights + shift)
            velocity, intercept = np.polyfit(offsets, transformed, 1)
            if velocity <= 0:
                continue
            if name == "linear":
                predicted = velocity * offsets + intercept
                first_hit = origin + (570 - intercept) / velocity
                current_velocity = velocity
            else:
                predicted = np.exp(velocity * offsets + intercept) - shift
                first_hit = origin + (math.log(570 + shift) - intercept) / velocity
                current_velocity = velocity * (heights[-1] + shift)
            residual = float(np.max(np.abs(heights - predicted)))
            if (60 <= current_velocity <= 9000 and residual <= (4 if short_window else 12)
                    and captured_at + .040 <= first_hit <= captured_at + .8):
                candidates.append((residual, name, first_hit))
        if not candidates:
            return None
        residual, name, first_hit = min(candidates)
        if short_window:
            cadence = float(np.median(np.diff([when for when, _ in recent])))
            remaining = first_hit - captured_at
            # 仍有时间就等待第四帧；仅在下一帧来不及时用三帧低残差拟合，并保留启动余量。
            if (capture_seconds <= 0 or remaining <= capture_seconds + .080
                    or remaining > capture_seconds + cadence + .080):
                return None
        self.fit = {"model": name, "residual_pixels": residual, "first_hit": first_hit,
                    "sample_count": len(recent), "short_window": short_window}
        return first_hit - self.gesture.start


class ChartPlayer:
    def __init__(self, controller, stop_requested: Callable[[], bool], *, clock=time.perf_counter, sleeper=time.sleep,
                 idle_observer: Callable[[], None] | None = None) -> None:
        self.controller = controller
        self.stop_requested = stop_requested
        self.clock, self.sleeper = clock, sleeper
        self.active: set[int] = set()
        self.lateness: list[float] = []
        self.idle_observer = idle_observer
        self.last_observation = -math.inf
        self.sent_actions = 0

    def release(self) -> None:
        failures = []
        for contact in sorted(self.active):
            try:
                if not self.controller.post_touch_up(contact).wait().succeeded:
                    failures.append(contact)
            except Exception:
                failures.append(contact)
        self.active = set(failures)
        if failures:
            raise RuntimeError(f"触点释放未确认：{failures}")

    def play(self, events: tuple[Touch, ...], epoch: float, offset_ms: int = 0) -> dict:
        validate_touches(events)
        sent = 0
        try:
            for event in events:
                target = epoch + event.time + offset_ms / 1000
                while self.clock() < target:
                    if self.stop_requested():
                        raise InterruptedError("用户已停止谱面演出")
                    remaining = target - self.clock()
                    # Agent 反向 RPC 不可并发等待；截图只在无触点且有足够空档时串行执行。
                    if (self.idle_observer is not None and not self.active and remaining > .85
                            and self.clock() - self.last_observation > 1.0):
                        self.idle_observer()
                        self.last_observation = self.clock()
                        continue
                    self.sleeper(min(.004, max(.0001, remaining)))
                if self.stop_requested():
                    raise InterruptedError("用户已停止谱面演出")
                delay = self.clock() - target
                self.lateness.append(delay)
                if delay > .180:
                    raise RuntimeError(f"触控派发落后 {delay * 1000:.0f} ms，停止输入")
                if event.kind == "down":
                    # 即使回执失败也可能已经按下，先纳入本轮清理责任。
                    self.active.add(event.contact)
                    job = self.controller.post_touch_down(event.x, event.y, event.contact, 1)
                elif event.kind == "move":
                    job = self.controller.post_touch_move(event.x, event.y, event.contact, 1)
                else:
                    job = self.controller.post_touch_up(event.contact)
                if not job.wait().succeeded:
                    raise RuntimeError(f"MFA 多点触控失败：{event.kind}，触点 {event.contact}")
                if event.kind == "up":
                    self.active.discard(event.contact)
                sent += 1
                self.sent_actions = sent
            return {"sent_actions": sent, "planned_actions": len(events), "release_confirmed": not self.active,
                    "lateness_ms_p50": float(np.percentile(self.lateness, 50) * 1000),
                    "lateness_ms_p95": float(np.percentile(self.lateness, 95) * 1000),
                    "lateness_ms_max": max(self.lateness) * 1000}
        finally:
            self.release()
