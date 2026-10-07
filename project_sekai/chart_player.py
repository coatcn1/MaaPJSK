from __future__ import annotations

from dataclasses import dataclass
from bisect import bisect_left
import math
import time
from typing import Callable

import cv2
import numpy as np

from .sus_chart import Chart, Gesture, slide_x

TOUCH_PLAN_VERSION = 4
SLIDE_NODE_TOUCH_PLAN_VERSION = 5
START_ANCHOR_VERSION = 11


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


def compile_touches(chart: Chart, *, sample_slide_nodes: bool = False) -> tuple[Touch, ...]:
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
            sampled_times = []
            for index in range(1, count + 1):
                when = gesture.start + (gesture.end - gesture.start) * index / count
                sampled_times.append(when)
                events.append(Touch(when, 1, contact, "move", round(slide_x(gesture.points, when))))
            if sample_slide_nodes:
                # 离线候选只补真实内部路径节点；保留原网格和边界、控制点及 flick 语义。
                for point in gesture.points:
                    if not (gesture.start < point.time < gesture.end and point.path_node and point.kind != 4):
                        continue
                    position = bisect_left(sampled_times, point.time)
                    neighbors = sampled_times[max(0, position - 1):position + 1]
                    if any(abs(when - point.time) <= 1e-9 for when in neighbors):
                        continue
                    sampled_times.insert(position, point.time)
                    events.append(Touch(point.time, 1, contact, "move", round(slide_x(gesture.points, point.time))))
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


def first_note_color(gesture: Gesture) -> str:
    if gesture.critical or gesture.points[0].kind in {2, 6}:
        return "gold"
    if gesture.kind in {"trace", "slide"}:
        return "green"
    return "pink" if gesture.flick else "blue"


def validated_simultaneous(gesture: Gesture, gestures) -> tuple[Gesture, ...]:
    try:
        group = tuple(sorted(gestures, key=lambda value: value.points[0].lane))
        if len(group) < 2 or gesture not in group:
            return ()
        if any(value.kind != "tap" or not value.flick or first_note_color(value) != "gold" for value in group):
            return validated_mixed_simultaneous(gesture, group)
        if any(not math.isfinite(value.start) or abs(value.start - gesture.start) > 1e-9 or first_note_color(value) != first_note_color(gesture) for value in group):
            return ()
        if any(left.points[0].lane + left.points[0].width != right.points[0].lane for left, right in zip(group, group[1:])):
            return ()
        return group
    except (AttributeError, TypeError, ValueError, IndexError):
        return ()


def validated_mixed_simultaneous(gesture: Gesture, group) -> tuple[Gesture, ...]:
    # 仅允许实际首时刻的一条金色长条头与一个相邻金色 TAP flick 共用横头；不泛化其他混合音。
    try:
        values = tuple(group)
        if (len(values) != 2 or gesture not in values or gesture.kind != "slide"
                or sum(value.kind == "slide" and value.critical and not value.flick for value in values) != 1
                or sum(value.kind == "tap" and value.critical and value.flick in {1, 2, 3} for value in values) != 1):
            return ()
        for value in values:
            if (not isinstance(value, Gesture) or not math.isfinite(value.start)
                    or abs(value.start - gesture.start) > 1e-9 or first_note_color(value) != "gold"
                    or type(value.flick) is not int or type(value.points[0].kind) is not int
                    or value.points[0].kind != (1 if value.kind == "slide" else 2)):
                return ()
            if value.kind == "tap" and len(value.points) != 1:
                return ()
            if value.kind == "slide" and (len(value.points) < 2 or value.end <= value.start):
                return ()
            previous_time = float("-inf")
            for point in value.points:
                if (not math.isfinite(point.time) or point.time < previous_time
                        or type(point.lane) is not int or type(point.width) is not int
                        or not 2 <= point.lane < point.lane + point.width <= 14):
                    return ()
                previous_time = point.time
        ordered = tuple(sorted(values, key=lambda value: value.points[0].lane))
        if ordered[0].points[0].lane + ordered[0].points[0].width != ordered[1].points[0].lane:
            return ()
        return ordered
    except (AttributeError, TypeError, ValueError, IndexError):
        return ()


def first_note_context(chart) -> tuple[Gesture, ...]:
    # 仅从实际谱面首时刻提取已知相邻金色组；非同刻的后继音不能帮助解释首音。
    first = chart.first
    group = [value for value in chart.gestures
             if abs(value.start - first.start) <= 1e-9
             and (first.kind == "slide" or first_note_color(value) == first_note_color(first))]
    return validated_simultaneous(first, group)


def validated_dense_followers(gesture: Gesture, followers) -> tuple[Gesture, ...]:
    try:
        values = tuple(followers)
        point = gesture.points[0]
        if (gesture.kind != "tap" or gesture.flick or not gesture.critical or point.kind != 2
                or len(gesture.points) != 1 or len(values) < 3 or not math.isfinite(gesture.start)):
            return ()
        if not (2 <= point.lane < point.lane + point.width <= 14):
            return ()
        previous = gesture.start
        cadence = None
        for value in values:
            if not isinstance(value, Gesture) or len(value.points) != 1:
                return ()
            trace = value.points[0]
            delta = value.start - previous
            if (value.kind != "trace" or not value.critical or value.flick or trace.kind != 6
                    or not math.isfinite(value.start) or not 0 < delta <= .020
                    or not (point.lane <= trace.lane < trace.lane + trace.width <= point.lane + point.width)
                    or abs(trace.x - point.x) > 1e-9):
                return ()
            if cadence is not None and abs(delta - cadence) > 1e-6:
                return ()
            cadence = delta
            previous = value.start
        return values
    except (AttributeError, TypeError, ValueError, IndexError):
        return ()


def dense_first_note_context(chart) -> tuple[Gesture, ...]:
    # 异时序 TRACE 不能伪装同时首音；仅提取真实谱面最早 TAP 紧接的同色、同中心密集前缀。
    first = chart.first
    ordered = sorted(chart.gestures, key=lambda value: value.start)
    if sum(abs(value.start - first.start) <= 1e-9 for value in ordered) != 1:
        return ()
    prefix = []
    previous = first.start
    for value in ordered[1:33]:
        if (value.kind != "trace" or not value.critical or value.flick or len(value.points) != 1
                or value.points[0].kind != 6 or not 0 < value.start - previous <= .020):
            break
        prefix.append(value)
        previous = value.start
    return validated_dense_followers(first, prefix)


class FirstHeadOutsideWindow(RuntimeError):
    """完整首长条头已越过同步窗口，不能改用后继同色音启动。"""


def first_note_y(frame: np.ndarray, gesture: Gesture, baseline: np.ndarray | None = None, *, simultaneous_gestures=(), dense_following_gestures=(), reject_passed_slide=False) -> float | None:
    """只追踪谱面第一音的开场锚点；演奏开始后不再识别音符。"""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    if gesture.critical or gesture.points[0].kind in {2, 6}:
        mask = cv2.inRange(hsv, (15, 55, 135), (45, 255, 255))
    elif gesture.kind in {"trace", "slide"}:
        # 长条尾部的 flick 不改变绿色起点；只用亮色横条，避免与较暗的底带连通。
        mask = cv2.inRange(hsv, (50, 55, 205), (95, 255, 255))
    elif gesture.flick:
        mask = cv2.inRange(hsv, (140, 65, 135), (179, 255, 255))
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
    gold_tap = gesture.kind == "tap" and first_note_color(gesture) == "gold"
    gold_slide = gesture.kind == "slide" and first_note_color(gesture) == "gold"
    full_mask = mask.copy() if gesture.kind == "tap" and (gesture.flick or gold_tap) or gold_slide or simultaneous_gestures else None
    component_labels, component_stats = None, None
    simultaneous = validated_simultaneous(gesture, simultaneous_gestures)
    mask[:40] = 0
    mask[540:] = 0
    point = gesture.points[0]
    # 金色首音的外框比普通音符更厚；按颜色保留透视高度，避免误取后方同色音符。
    height_ratio = .16 if gesture.critical or point.kind in {2, 6} else .12
    # Trace 的三角箭头外框比普通横条更高，低流速小音符尤其明显；保留已有位置与宽度门槛。
    aspect_ratio = 1.4 if gesture.kind == "trace" else 2.0
    candidates = []
    mixed_candidates = []
    passed_slide_head = False

    def component_at(contour):
        nonlocal component_labels, component_stats
        if component_labels is None:
            _, component_labels, component_stats, _ = cv2.connectedComponentsWithStats(full_mask, connectivity=8)
        sx, sy = contour[0, 0]
        label = component_labels[sy, sx]
        return label, component_stats[label]

    def eligible(x, y, width, height):
        center_y = y + height / 2
        projected_x = 640 + (point.x - 640) * center_y / 570
        expected_width = point.width * (1000 / 12) * center_y / 570
        return (width > 14 and 2 <= height <= max(36, center_y * height_ratio) and width > height * aspect_ratio
                and abs(x + width / 2 - projected_x) < max(25, expected_width * .35)
                and expected_width * .5 < width < expected_width * 1.5 + 12
                and np.any(hsv[y:y + height, x:x + width, 2] >= 205))

    def consider(x, y, width, height, target=candidates):
        if eligible(x, y, width, height):
            target.append(y + height / 2)

    for contour in cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]:
        x, y, width, height = cv2.boundingRect(contour)
        if gold_slide and y + height == 540:
            _, stats = component_at(contour)
            fx, fy, fw, fh = stats[:4]
            # 金色长条头越过下边界后不能把残留薄片当首音；上延长条带仍是正常路径，不检查上裁边。
            if fy + fh > 540:
                passed_slide_head |= eligible(x, y, width, height)
                continue
        if gesture.kind == "tap" and (gesture.flick or gold_tap) and (y == 40 or y + height == 540):
            _, stats = component_at(contour)
            fx, fy, fw, fh = stats[:4]
            # 裁边不能把完整箭头变成横条；合法边界横条仍按原裁剪坐标拟合。
            if not (2 <= fh <= max(36, (fy + fh / 2) * height_ratio) and fw > fh * aspect_ratio):
                continue
        center_y = y + height / 2
        projected_x = 640 + (point.x - 640) * center_y / 570
        expected_width = point.width * (1000 / 12) * center_y / 570
        consider(x, y, width, height)
        if not (simultaneous and width >= expected_width * 1.5 + 12
                and 2 <= height <= max(36, center_y * height_ratio) and width > height * aspect_ratio):
            continue
        first_lane = simultaneous[0].points[0].lane
        span = simultaneous[-1].points[0].lane + simultaneous[-1].points[0].width - first_lane
        span_x = 140 + (first_lane - 2 + span / 2) * (1000 / 12)
        span_center = 640 + (span_x - 640) * center_y / 570
        span_width = span * (1000 / 12) * center_y / 570
        if not (.5 * span_width < width < 1.5 * span_width + 12
                and abs(x + width / 2 - span_center) < max(25, span_width * .35)):
            continue
        label, _ = component_at(contour)
        left, right = max(x, round(projected_x - expected_width / 2)), min(x + width, round(projected_x + expected_width / 2))
        if right <= left:
            continue
        # 只裁真实同一连通分量，再测局部实际前景框；不能把预期窗口宽度当作前景。
        local = np.uint8(component_labels[y:y + height, left:right] == label) * 255
        for part in cv2.findContours(local, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]:
            lx, ly, lw, lh = cv2.boundingRect(part)
            consider(left + lx, y + ly, lw, lh, mixed_candidates if gold_slide else candidates)
    if candidates:
        return max(candidates)
    if passed_slide_head and reject_passed_slide:
        raise FirstHeadOutsideWindow("首长条头已越过同步窗口，拒绝从后继音或歌曲中途启动")
    if mixed_candidates:
        return max(mixed_candidates)
    if not validated_dense_followers(gesture, dense_following_gestures):
        return None
    # 仅密集 critical TRACE 与首 TAP 外框连通时测浅亮横芯；正常检测成功始终优先。
    core = cv2.inRange(hsv, (15, 0, 235), (45, 110, 255))
    if baseline is not None:
        core[~changed] = 0
    _, core_labels, core_stats, _ = cv2.connectedComponentsWithStats(core, connectivity=8)
    if component_labels is None:
        _, component_labels, component_stats, _ = cv2.connectedComponentsWithStats(full_mask, connectivity=8)
    for label, stats in enumerate(core_stats[1:], start=1):
        x, y, width, height, area = stats
        # 完整浅亮横条须未被屏幕窗口裁断；三角 TRACE 或散点不允许虚构 TAP 前沿。
        if y < 40 or y + height > 540 or area < width * height * .75:
            continue
        actual = core_labels[y:y + height, x:x + width] == label
        overlaps = component_labels[y:y + height, x:x + width][actual]
        original_labels = np.unique(overlaps[overlaps != 0])
        if len(original_labels) != 1:
            continue
        fx, fy, fw, fh = component_stats[original_labels[0]][:4]
        # 必须有同一真实金色分量的像素来源，且确因纵向连通而违反原横条几何。
        if 2 <= fh <= max(36, (fy + fh / 2) * height_ratio) and fw > fh * aspect_ratio:
            continue
        consider(x, y, width, height)
    return max(candidates) if candidates else None


class StartAnchor:
    def __init__(self, gesture: Gesture, *, minimum_samples: int = 2, simultaneous_gestures=(), dense_following_gestures=()) -> None:
        self.gesture = gesture
        self.minimum_samples = minimum_samples
        self.simultaneous_gestures = validated_simultaneous(gesture, simultaneous_gestures)
        self.dense_following_gestures = validated_dense_followers(gesture, dense_following_gestures)
        self.samples: list[tuple[float, float]] = []
        self.baseline: np.ndarray | None = None
        self.fit: dict | None = None
        self.failure_reason: str | None = None

    def observe(self, frame: np.ndarray, captured_at: float, *, capture_seconds: float = 0.0) -> float | None:
        if self.baseline is None:
            self.baseline = frame.copy()
            return None
        if self.samples and captured_at - self.samples[0][0] > 2:
            if len(self.samples) == 1:
                self.failure_reason = "candidate_motion_unconfirmed"
                raise RuntimeError("首音候选未确认运动且等待超时，拒绝从歌曲中途开始")
            self.failure_reason = "trajectory_gate_timeout"
            raise RuntimeError("首音轨迹未通过同步门槛且等待超时，拒绝从歌曲中途开始")
        try:
            y = first_note_y(frame, self.gesture, self.baseline, simultaneous_gestures=self.simultaneous_gestures,
                            dense_following_gestures=self.dense_following_gestures, reject_passed_slide=True)
        except FirstHeadOutsideWindow:
            self.failure_reason = "first_head_outside_window"
            raise
        if y is None:
            return None
        if self.samples:
            previous_time, previous_y = self.samples[-1]
            if captured_at <= previous_time:
                return None
            if y < previous_y - 10:
                # 大幅上移仅允许未证明运动的顶部候选替换，不能把后继音当作新的首音。
                if len(self.samples) == 1 and y < 130:
                    self.samples[0] = (captured_at, y)
                return None
            if abs(y - previous_y) < 3:
                if len(self.samples) == 1:
                    self.samples[0] = (captured_at, y)
                return None
            if y < previous_y:
                # 小幅倒退不是向下运动，也不延长候选期限；已建立轨迹始终不能重置。
                return None
        self.samples.append((captured_at, y))
        recent = self.samples[-5:]
        fit_window = "recent"
        # 快截图的五帧可能仅覆盖约 125 ms，局部斜率噪声会放大成整曲相位误差。
        # 已有足够轨迹时直接利用最近 1.25 秒、最多 32 点；不为凑宽窗口等待新帧。
        # 慢链路仍沿用原五帧与低样本门禁，宽窗口不合格时也不能退回噪声短窗口。
        if len(self.samples) >= 8 and recent[-1][0] - recent[0][0] < .250:
            wider = [sample for sample in self.samples[-32:] if captured_at - sample[0] <= 1.25]
            if len(wider) >= 8 and wider[-1][0] - wider[0][0] >= .250:
                recent = wider
                fit_window = "wide_trajectory"
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
        selection = "minimum_residual"
        perspective = next((candidate for candidate in candidates if candidate[1] == "perspective"), None)
        # 顶端短轨迹中不足一像素的残差差异无法区分模型；优先保留实机验证的透视几何，
        # 避免指数模型因微小观测噪声外推出提前几十毫秒的起点。质量与启动余量门槛保持不变。
        if perspective is not None and perspective[0] <= residual + 1.0:
            if name != "perspective":
                selection = "perspective_pixel_tie"
            residual, name, first_hit = perspective
        if short_window:
            cadence = float(np.median(np.diff([when for when, _ in recent])))
            remaining = first_hit - captured_at
            # 仍有时间就等待第四帧；仅在下一帧来不及时用三帧低残差拟合，并保留启动余量。
            if (capture_seconds <= 0 or remaining <= capture_seconds + .080
                    or remaining > capture_seconds + cadence + .080):
                return None
        self.fit = {"model": name, "residual_pixels": residual, "first_hit": first_hit,
                    "sample_count": len(recent), "short_window": short_window, "selection": selection,
                    "fit_window": fit_window, "window_seconds": recent[-1][0] - recent[0][0],
                    "candidates": [{"model": model, "residual_pixels": error, "first_hit": hit}
                                   for error, model, hit in candidates]}
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
