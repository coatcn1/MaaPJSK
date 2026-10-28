from __future__ import annotations

from dataclasses import dataclass
from bisect import bisect_left
import math
import os
import time
from typing import Callable

import cv2
import numpy as np

from .sus_chart import Chart, Gesture, Point, slide_x, ERASED_SLIDE_GUARD
from .opening_head import (trace_opening_head_guard_requested, trace_head_candidates, trace_head_geometry,
                           trace_source_proof, TracePhaseContext, trace_core_mask, trace_projection_scale)

TOUCH_PLAN_VERSION = 6 if ERASED_SLIDE_GUARD else 4
SLIDE_NODE_TOUCH_PLAN_VERSION = 7 if ERASED_SLIDE_GUARD else 5
START_ANCHOR_VERSION = 27


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


def validated_simultaneous(gesture: Gesture, gestures, *, gold_slide_pair_guard=False) -> tuple[Gesture, ...]:
    try:
        group = tuple(sorted(gestures, key=lambda value: value.points[0].lane))
        if len(group) < 2 or gesture not in group:
            return ()
        if any(value.kind != "tap" or not value.flick or first_note_color(value) != "gold" for value in group):
            if gold_slide_pair_guard:
                pair = validated_gold_slide_pair(gesture, group)
                if pair:
                    return pair
            return validated_mixed_simultaneous(gesture, group)
        if any(not math.isfinite(value.start) or abs(value.start - gesture.start) > 1e-9 or first_note_color(value) != first_note_color(gesture) for value in group):
            return ()
        if any(left.points[0].lane + left.points[0].width != right.points[0].lane for left, right in zip(group, group[1:])):
            return ()
        return group
    except (AttributeError, TypeError, ValueError, IndexError):
        return ()


def validated_gold_slide_pair(gesture: Gesture, group) -> tuple[Gesture, ...]:
    # 只接纳真实同刻、轨道相接的两条 critical SLIDE；尾 flick 不改变普通类型首头。
    try:
        values = tuple(group)
        if len(values) != 2 or gesture not in values:
            return ()
        for value in (*values, gesture):
            if (not isinstance(value, Gesture) or value.kind != "slide" or value.critical is not True
                    or type(value.flick) is not int or value.flick not in range(7) or len(value.points) < 2
                    or not math.isfinite(value.start) or abs(value.start - gesture.start) > 1e-9
                    or value.points[0].kind != 1 or value.points[-1].kind != 2 or value.end <= value.start):
                return ()
            previous_time = float("-inf")
            for index, point in enumerate(value.points):
                kinds = {1} if index == 0 else {2} if index == len(value.points) - 1 else {3, 4, 5}
                if (not math.isfinite(point.time) or point.time < previous_time
                        or type(point.kind) is not int or point.kind not in kinds
                        or type(point.lane) is not int or type(point.width) is not int
                        or not 2 <= point.lane < point.lane + point.width <= 14
                        or type(point.direction) is not int or point.direction not in range(7)):
                    return ()
                previous_time = point.time
        ordered = tuple(sorted(values, key=lambda value: value.points[0].lane))
        if ordered[0].points[0].lane + ordered[0].points[0].width != ordered[1].points[0].lane:
            return ()
        return ordered
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
    # 这里只提取纯谱面元数据；双长条候选是否启用由同步器冻结后在两处重新验证。
    return validated_simultaneous(first, group, gold_slide_pair_guard=True)


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


def validated_gold_tap_followers(gesture: Gesture, followers) -> tuple[Gesture, ...]:
    # 普通金 TAP 后继只解释同轨、同宽的独立首芯，不借用 TRACE 或同刻音的上下文。
    try:
        values = tuple(followers)
        if not 1 <= len(values) <= 32:
            return ()
        for value in (gesture, *values):
            if (not isinstance(value, Gesture) or value.kind != "tap" or value.critical is not True
                    or type(value.flick) is not int or value.flick != 0 or len(value.points) != 1):
                return ()
            point = value.points[0]
            if (not math.isfinite(point.time) or type(point.kind) is not int or point.kind != 2
                    or type(point.lane) is not int or type(point.width) is not int
                    or not 2 <= point.lane < point.lane + point.width <= 14
                    or type(point.direction) is not int or point.direction != 0):
                return ()
        point = gesture.points[0]
        ordered = tuple(sorted(values, key=lambda value: value.start))
        previous = gesture.start
        for value in ordered:
            following = value.points[0]
            if (not previous < value.start <= gesture.start + 1.
                    or following.lane != point.lane or following.width != point.width):
                return ()
            previous = value.start
        return ordered
    except (AttributeError, TypeError, ValueError, IndexError):
        return ()


def gold_tap_following_context(chart, *, skip_blue_taps=False, expected_first=None) -> tuple[Gesture, ...]:
    # 只取首后一秒元数据，默认不跳过重叠音；显式候选仅排除严格较晚的完整普通蓝 TAP。
    try:
        first = chart.first
        if expected_first is not None and first is not expected_first:
            return ()
        point = first.points[0]
        values = []
        for value in chart.gestures:
            if not math.isfinite(value.start):
                return ()
            if value is first or not first.start <= value.start <= first.start + 1.:
                continue
            following = value.points[0]
            if point.lane < following.lane + following.width and following.lane < point.lane + point.width:
                if (skip_blue_taps is True and isinstance(value, Gesture)
                        and value.kind == "tap" and value.critical is False
                        and type(value.flick) is int and value.flick == 0 and len(value.points) == 1
                        and isinstance(following, Point) and type(following.time) in {int, float}
                        and math.isfinite(following.time) and value.start > first.start + 1e-9
                        and type(following.kind) is int and following.kind == 1
                        and type(following.lane) is int and type(following.width) is int
                        and 2 <= following.lane < following.lane + following.width <= 14
                        and type(following.direction) is int and following.direction == 0):
                    # 蓝 TAP 与金色 HSV 分离；同刻或不完整元数据仍是冲突，不能跳过后借后继启动。
                    continue
                values.append(value)
        return validated_gold_tap_followers(first, values)
    except (AttributeError, TypeError, ValueError, IndexError):
        return ()


class FirstHeadOutsideWindow(RuntimeError):
    """完整首头已越过同步窗口，不能改用后继同色音启动。"""


def validated_gold_tap_slide_follower(gesture: Gesture, followers) -> tuple[Gesture, ...]:
    # 普通金 TAP 与同轨后继长条单独校验，不能改变普通 TAP 后继合同或借末尾 flick 解释首头。
    try:
        values = tuple(followers)
        if len(values) != 1 or not isinstance(gesture, Gesture):
            return ()
        if (gesture.kind != "tap" or gesture.critical is not True or type(gesture.flick) is not int
                or gesture.flick != 0 or len(gesture.points) != 1):
            return ()
        first = gesture.points[0]
        if (not math.isfinite(first.time) or type(first.kind) is not int or first.kind != 2
                or type(first.lane) is not int or type(first.width) is not int
                or not 2 <= first.lane < first.lane + first.width <= 14
                or type(first.direction) is not int or first.direction != 0):
            return ()
        slide = values[0]
        if (not isinstance(slide, Gesture) or slide.kind != "slide" or slide.critical is not True
                or type(slide.flick) is not int or slide.flick not in range(7) or len(slide.points) < 2
                or not gesture.start < slide.start <= gesture.start + 1. or slide.end <= slide.start
                or slide.points[0].lane != first.lane or slide.points[0].width != first.width):
            return ()
        previous_time = float("-inf")
        for index, point in enumerate(slide.points):
            # SLIDE 首点 direction 是路径缓动元数据；首头类型由 kind=1 判断，不能当作起点 flick。
            kinds = {1} if index == 0 else {2} if index == len(slide.points) - 1 else {3, 4, 5}
            if (not math.isfinite(point.time) or point.time < previous_time
                    or type(point.kind) is not int or point.kind not in kinds
                    or type(point.lane) is not int or type(point.width) is not int
                    or not 2 <= point.lane < point.lane + point.width <= 14
                    or type(point.direction) is not int or point.direction not in range(7)):
                return ()
            previous_time = point.time
        return values
    except (AttributeError, TypeError, ValueError, IndexError):
        return ()


def gold_tap_slide_following_context(chart) -> tuple[Gesture, ...]:
    # 只解释首后一秒内一个同轨金色长条；可跳过明确蓝 TAP，不能跳过任何重叠金色冲突。
    try:
        first = chart.first
        point = first.points[0]
        values = []
        for value in chart.gestures:
            if not math.isfinite(value.start):
                return ()
            if value is first or not first.start <= value.start <= first.start + 1.:
                continue
            following = value.points[0]
            if not (point.lane < following.lane + following.width
                    and following.lane < point.lane + point.width):
                continue
            if (value.kind == "tap" and value.critical is False and type(value.flick) is int
                    and value.flick == 0 and len(value.points) == 1
                    and type(following.kind) is int and following.kind == 1
                    and type(following.lane) is int and type(following.width) is int
                    and 2 <= following.lane < following.lane + following.width <= 14
                    and type(following.direction) is int and following.direction == 0):
                # 普通蓝 TAP 使用独立 HSV 色带，不能成为此处两个金色芯的解释来源。
                continue
            values.append(value)
        return validated_gold_tap_slide_follower(first, values)
    except (AttributeError, TypeError, ValueError, IndexError):
        return ()


def first_note_y(frame: np.ndarray, gesture: Gesture, baseline: np.ndarray | None = None, *, simultaneous_gestures=(), dense_following_gestures=(), gold_tap_following_gestures=(), gold_tap_slide_following_gestures=(), reject_passed_slide=False, gold_tap_head_guard=False, gold_slide_pair_guard=False, gold_tap_followers_guard=False, gold_tap_slide_follower_guard=False, gold_tap_slide_evidence=None, observation=None) -> float | None:
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
    blue_tap = (gesture.kind == "tap" and not gesture.flick and not gesture.critical
                and len(gesture.points) == 1 and gesture.points[0].kind == 1)
    simultaneous = validated_simultaneous(gesture, simultaneous_gestures, gold_slide_pair_guard=gold_slide_pair_guard)
    gold_slide_pair = bool(simultaneous and all(value.kind == "slide" for value in simultaneous))
    gold_head = (gold_tap_head_guard and gesture.kind == "tap" and not gesture.flick
                 and len(gesture.points) == 1 and gesture.points[0].kind == 2 and not simultaneous)
    # 基线差分会把同一外框拆成碎片；蓝框归属用原颜色分量，横芯自身仍必须通过差分。
    blue_source_mask = mask.copy() if blue_tap else None
    gold_source_mask = mask.copy() if gold_head else None
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
    full_mask = (gold_source_mask if gold_head else blue_source_mask if blue_tap else
                 mask.copy() if gesture.kind == "tap" and (gesture.flick or gold_tap) or gold_slide or simultaneous_gestures else None)
    component_labels, component_stats = None, None
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

    def eligible_group(x, y, width, height, *, minimum_height=2):
        if not simultaneous:
            return False
        center_y = y + height / 2
        expected_width = point.width * (1000 / 12) * center_y / 570
        if not (width >= expected_width * 1.5 + 12
                and minimum_height <= height <= max(36, center_y * height_ratio) and width > height * aspect_ratio):
            return False
        first_lane = simultaneous[0].points[0].lane
        span = simultaneous[-1].points[0].lane + simultaneous[-1].points[0].width - first_lane
        span_x = 140 + (first_lane - 2 + span / 2) * (1000 / 12)
        span_center = 640 + (span_x - 640) * center_y / 570
        span_width = span * (1000 / 12) * center_y / 570
        return (.5 * span_width < width < 1.5 * span_width + 12
                and abs(x + width / 2 - span_center) < max(25, span_width * .35))

    if gold_head:
        head_sources = []
        slide_follower = (validated_gold_tap_slide_follower(gesture, gold_tap_slide_following_gestures)
                          if gold_tap_slide_follower_guard else ())
        unconfirmed_heads = []
        core = cv2.inRange(hsv, (15, 0, 235), (45, 110, 255))
        _, core_labels, core_stats, _ = cv2.connectedComponentsWithStats(core, connectivity=8)
        _, component_labels, component_stats, _ = cv2.connectedComponentsWithStats(full_mask, connectivity=8)
        for label, stats in enumerate(core_stats[1:], start=1):
            x, y, width, height, area = stats
            if y <= 40 or area < width * height * .75 or not eligible(x, y, width, height):
                continue
            actual = core_labels[y:y + height, x:x + width] == label
            # 完整亮芯先测量再验证变化，不能用差分后的半芯替代真实横条。
            if baseline is not None and np.count_nonzero(changed[y:y + height, x:x + width][actual]) < area * .75:
                continue
            padding = max(2, math.ceil(height / 2))
            strip = component_labels[max(0, y - padding):min(frame.shape[0], y + height + padding),
                                     max(0, x - padding):min(frame.shape[1], x + width + padding)]
            sources = strip[strip != 0]
            labels = np.unique(sources)
            # 金色芯低饱和时几乎不在外框掩膜里；用周围完整外框和同高两侧确认足量、唯一来源。
            if len(labels) != 1 or len(sources) < width:
                if slide_follower:
                    unconfirmed_heads.append(float(y + height / 2))
                continue
            source = labels[0]
            left = component_labels[y:y + height, max(0, x - padding):x + 2]
            right = component_labels[y:y + height, x + width - 2:min(frame.shape[1], x + width + padding)]
            if np.count_nonzero(left == source) < height or np.count_nonzero(right == source) < height:
                if slide_follower:
                    unconfirmed_heads.append(float(y + height / 2))
                continue
            fx, fy, fw, fh = component_stats[source][:4]
            bottom = fy + fh
            # 当前完整首芯及来源已下越界的证据优先于前沿门槛，整帧不得由后继芯接管。
            if y + height >= 540 or bottom > 540:
                if reject_passed_slide:
                    raise FirstHeadOutsideWindow("首金色 TAP 头已越过同步窗口，拒绝从后继音或歌曲中途启动")
                return None
            # 后继轨迹向上延伸不改变首 TAP 前沿；未越界但仍向下伸出一个芯高时不是首头。
            if not y + height <= bottom <= y + 2 * height:
                if slide_follower:
                    unconfirmed_heads.append(float(y + height / 2))
                continue
            candidates.append(y + height / 2)
            head_sources.append((y + height / 2, int(source), int(height)))
        # 较前完整芯来源仍未知时保留未知；不能忽略孤立噪点并让后方长条芯接管首 TAP。
        if slide_follower and candidates and any(y >= max(candidates) for y in unconfirmed_heads):
            return None
        if len(candidates) == 1:
            return candidates[0]
        followers = validated_gold_tap_followers(gesture, gold_tap_following_gestures) if gold_tap_followers_guard else ()
        # 多芯必须由真实后继音解释，且每颗来自不同的完整来源；同一外框的半芯仍整组拒绝。
        if ((not followers and not slide_follower)
                or not 2 <= len(head_sources) <= (2 if slide_follower else len(followers) + 1)
                or len({source for _, source, _ in head_sources}) != len(head_sources)):
            return None
        ordered = sorted(head_sources)
        # 25 像素沿用轨迹的明确运动跨度；还须超过两倍芯高，不能按微小高低差拆解同深度歧义。
        if any(front_y - rear_y <= max(25, 2 * max(rear_height, front_height))
               for (rear_y, _, rear_height), (front_y, _, front_height) in zip(ordered, ordered[1:])):
            return None
        if slide_follower and gold_tap_slide_evidence is not None:
            gold_tap_slide_evidence["pair_confirmed"] = True
        return ordered[-1][0]

    for contour in cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]:
        x, y, width, height = cv2.boundingRect(contour)
        if observation is not None and width > 14 and y > 40 and height >= 2:
            observation['nonempty'] = True
            observation['reason'] = 'component_geometry_pending'
            if len(observation.setdefault('components', [])) < 16:
                observation['components'].append([x, y, width, height])
        if gold_slide and y + height == 540:
            _, stats = component_at(contour)
            fx, fy, fw, fh = stats[:4]
            # 金色长条头越过下边界后不能把残留薄片当首音；上延长条带仍是正常路径，不检查上裁边。
            if fy + fh > 540:
                # 合并首头本来就超出单头宽度，不能因此丢掉真实整组下越界的证据。
                # 来源已证明下越界时，一行残留只用于拒绝后继；正常首头仍要求至少两行。
                passed_slide_head |= (eligible(x, y, width, height)
                                      or gold_slide_pair and eligible_group(x, y, width, height, minimum_height=1))
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
        if not eligible_group(x, y, width, height):
            continue
        label, _ = component_at(contour)
        left, right = max(x, round(projected_x - expected_width / 2)), min(x + width, round(projected_x + expected_width / 2))
        if right <= left:
            continue
        # 只裁真实同一连通分量，再测局部实际前景框；不能把预期窗口宽度当作前景。
        local = np.uint8(component_labels[y:y + height, left:right] == label) * 255
        for part in cv2.findContours(local, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]:
            lx, ly, lw, lh = cv2.boundingRect(part)
            if gold_slide_pair and np.count_nonzero(local[ly:ly + lh, lx:lx + lw]) < lw * lh * .25:
                # 金框的浅色芯不在掩膜中，保留实际框线；稀疏连线不能借预期窗口虚构局部横头。
                continue
            consider(left + lx, y + ly, lw, lh, mixed_candidates if gold_slide else candidates)
    if passed_slide_head and (reject_passed_slide or gold_slide_pair):
        # 同帧后继横条不能覆盖首头已越界的证据，否则会从歌曲中途建立 epoch。
        if reject_passed_slide:
            raise FirstHeadOutsideWindow("首长条头已越过同步窗口，拒绝从后继音或歌曲中途启动")
        return None
    if blue_tap:
        # 蓝外框可能与底图纵向连通，原框因高度超限被拒后会误选后继音。
        # 浅亮横芯仅补充这种污染框的测量；位置、尺寸、亮度与后续轨迹门槛均保持原样。
        core = cv2.inRange(hsv, (95, 0, 235), (135, 110, 255))
        _, core_labels, core_stats, _ = cv2.connectedComponentsWithStats(core, connectivity=8)
        if component_labels is None:
            _, component_labels, component_stats, _ = cv2.connectedComponentsWithStats(full_mask, connectivity=8)
        for core_label, stats in enumerate(core_stats[1:], start=1):
            x, y, width, height, area = stats
            if y <= 40 or y + height >= 540 or area < width * height * .75:
                continue
            if not eligible(x, y, width, height):
                continue
            actual = core_labels[y:y + height, x:x + width] == core_label
            # 必须先测完整横芯再验证变化；差分残片不能伪装完整首头或改变其中心。
            if baseline is not None and np.count_nonzero(changed[y:y + height, x:x + width][actual]) < area * .75:
                continue
            overlaps = component_labels[y:y + height, x:x + width][actual]
            sources = overlaps[overlaps != 0]
            original_labels = np.unique(sources)
            # 低饱和横芯不全在蓝掩膜中，但必须有足量、唯一且横芯两侧均可见的真实蓝框来源。
            if len(original_labels) != 1 or len(sources) < area * .10:
                continue
            source = original_labels[0]
            fx, fy, fw, fh = component_stats[source][:4]
            if fh <= max(36, (fy + fh / 2) * height_ratio):
                continue
            left = component_labels[y:y + height, max(0, x - 2):x + 2]
            right = component_labels[y:y + height, x + width - 2:min(frame.shape[1], x + width + 2)]
            if not (np.any(left == source) and np.any(right == source)):
                continue
            # 不在这里重置任何轨迹；首头已越界或候选倒退仍由原同步生命周期拒绝。
            candidates.append(y + height / 2)
    if candidates:
        return max(candidates)
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


def chart_bound_head_guard_requested():
    return os.environ.get('MAAPJSK_CHART_BOUND_HEAD_GUARD') == '1' and os.environ.get('MAAPJSK_GOLD_TAP_HEAD_GUARD') != '0'


def chart_bound_head_groups(chart, expected_first, *, include_trace=False):
    """只按真实首时刻元数据建立相接子组，间隙两侧不得共用虚构的整跨度横头。"""
    try:
        if not isinstance(chart, Chart) or chart.first is not expected_first:
            return ()
        heads = tuple(value for value in chart.gestures if abs(value.start - expected_first.start) <= 1e-9)
        if not 1 <= len(heads) <= 12 or not any(value is expected_first for value in heads):
            return ()
        for value in heads:
            if (not isinstance(value, Gesture) or value.kind not in {'tap', 'slide', 'trace'} or type(value.critical) is not bool
                    or type(value.flick) is not int or value.flick not in range(7)
                    or not value.points
                    or value.kind in {'tap', 'trace'} and len(value.points) != 1
                    or value.kind == 'trace' and value.flick != 0
                    or value.kind == 'slide' and (len(value.points) < 2 or value.end <= value.start)):
                return ()
            previous = -math.inf
            for index, point in enumerate(value.points):
                kinds = ({2} if value.kind == 'tap' and value.critical else {1, 3} if value.kind == 'tap' else
                         {6} if value.kind == 'trace' and value.critical else {5} if value.kind == 'trace' else {1} if index == 0 else
                         {2} if index == len(value.points) - 1 else {3, 4, 5})
                if (not isinstance(point, Point) or isinstance(point.time, bool) or not math.isfinite(point.time)
                        or point.time < previous or type(point.kind) is not int or point.kind not in kinds
                        or type(point.lane) is not int or type(point.width) is not int
                        or not 2 <= point.lane < point.lane + point.width <= 14
                        or type(point.direction) is not int or point.direction not in range(7)
                        or value.kind in {'tap', 'trace'} and point.direction != 0
                        or value.kind == 'slide' and index == len(value.points) - 1 and point.direction != value.flick):
                    return ()
                if include_trace and value.kind == 'trace' and point.path_node is not True:
                    return ()
                previous = point.time
        groups = []
        ordered = sorted(heads, key=lambda value: value.points[0].lane)
        if any(left.points[0].lane + left.points[0].width > right.points[0].lane for left, right in zip(ordered, ordered[1:])):
            return ()
        for value in (value for value in ordered if value.critical and value.kind in ({'tap', 'slide', 'trace'} if include_trace else {'tap', 'slide'})):
            if groups:
                last = groups[-1][-1].points[0]
                if value.points[0].lane < last.lane + last.width:
                    return ()
                if value.points[0].lane == last.lane + last.width:
                    groups[-1].append(value)
                    continue
            groups.append([value])
        if not any(value is expected_first for group in groups for value in group):
            return ()
        return tuple(tuple(group) for group in groups)
    except (AttributeError, TypeError, ValueError, IndexError):
        return ()


def chart_bound_head_geometry(x, y, width, height, lane, span):
    center = y + height / 2
    point_x = 140 + (lane - 2 + span / 2) * (1000 / 12)
    expected = span * (1000 / 12) * center / 570
    return (y > 40 and width > 14 and 2 <= height <= max(36, center * .16) and width > height * 2
            and abs(x + width / 2 - (640 + (point_x - 640) * center / 570)) < max(25, expected * .35)
            and expected * .5 < width < expected * 1.5 + 12)


def chart_bound_following_geometry(chart, first, *, trace_heads=False):
    """冻结可竞争金头及真实同刻相接跨度；非法金色上下文不能静默变成无后继。"""
    try:
        heads = []
        for value in chart.gestures:
            if not isinstance(value, Gesture) or not value.points or not isinstance(value.points[0], Point):
                return None
            if type(value.start) not in {int, float} or not math.isfinite(value.start):
                return None
            if not first.start + 1e-9 < value.start <= (math.inf if trace_heads else first.start + 1.) or first_note_color(value) != 'gold':
                continue
            if (value.critical is not True or value.kind not in {'tap', 'slide', 'trace'}
                    or type(value.flick) is not int or value.flick not in range(7)
                    or value.kind == 'trace' and value.flick != 0 and not trace_heads
                    or value.kind in {'tap', 'trace'} and len(value.points) != 1
                    or value.kind == 'slide' and (len(value.points) < 2 or value.end <= value.start)):
                return None
            previous = -math.inf
            for index, point in enumerate(value.points):
                kind = ({2} if value.kind == 'tap' else {6} if value.kind == 'trace' else {1} if index == 0 else
                        {2} if index == len(value.points) - 1 else {3, 4, 5})
                if (not isinstance(point, Point) or type(point.time) not in {int, float} or not math.isfinite(point.time)
                        or point.time < previous or type(point.kind) is not int or point.kind not in kind
                        or type(point.lane) is not int or type(point.width) is not int
                        or not 2 <= point.lane < point.lane + point.width <= 14
                        or type(point.direction) is not int or point.direction not in range(7)
                        or value.kind in {'tap', 'trace'} and point.direction != 0
                        or value.kind == 'slide' and index == len(value.points) - 1 and point.direction != value.flick):
                    return None
                if trace_heads and value.kind == 'trace' and point.path_node is not True:
                    return None
                previous = point.time
            point = value.points[0]
            heads.append((value.start - first.start, point.lane, point.width))
        geometry = set(heads)
        ordered = sorted(heads)
        groups = []
        for delta, lane, width in ordered:
            if not groups or abs(groups[-1][0][0] - delta) > 1e-9:
                groups.append([])
            groups[-1].append((delta, lane, width))
        for group in groups:
            group.sort(key=lambda value: value[1])
            for left, (delta, lane, width) in enumerate(group):
                end = lane + width
                for _, next_lane, next_width in group[left + 1:]:
                    if next_lane > end:
                        break
                    end = max(end, next_lane + next_width)
                    # 来源外框可能合并同刻相接的几颗后继，竞争范围不能只覆盖单颗理想宽度。
                    geometry.add((delta, lane, end - lane))
        return tuple(sorted(geometry))
    except (AttributeError, TypeError, ValueError, IndexError):
        return None


class ChartBoundHeadTracker:
    """候选只产生真实首头测量；未知不回旧检测器，跨帧不保存source label。"""

    def __init__(self, chart, first, *, trace_heads=False):
        self.trace_heads = trace_heads
        self.groups = chart_bound_head_groups(chart, first, include_trace=trace_heads)
        self.primary = next((index for index, group in enumerate(self.groups)
                             if any(value is first for value in group)), 0)
        self.primary_member = next((index for index, value in enumerate(self.groups[self.primary]) if value is first), 0) if self.groups else 0
        self.motion = []
        self.lost = False
        self.status = 'unseeded' if self.groups else 'invalid_chart_context'
        # 仅首次接受点冻结身份合同；后续不能按当前画面切换到更宽松的证明。
        self.trace_identity_requires_constellation = None
        self.targets = []
        for group_index, group in enumerate(self.groups):
            # 多头可以共享来源，完整芯仍逐成员实测；宽芯不能借整跨度替代多颗首头。
            for index, value in enumerate(group):
                self.targets.append((group_index, (index,), value.points[0].lane, value.points[0].width))
            if trace_heads and len(group) > 1 and all(value.kind == 'trace' for value in group):
                left, right = group[0].points[0], group[-1].points[0]
                self.targets.append((group_index, tuple(range(len(group))), left.lane, right.lane + right.width - left.lane))
        self.following_geometry = chart_bound_following_geometry(chart, first, trace_heads=trace_heads) if self.groups else ()
        trace_events = []
        if trace_heads and self.groups and self.following_geometry is not None:
            for value in chart.gestures:
                if value.kind == 'trace' and value.critical is True:
                    point = value.points[0]
                    trace_events.append((value.start - first.start, point.lane, point.width, 1))
            ordered = sorted(trace_events)
            for index, (when, lane, width, _) in enumerate(ordered):
                end, count = lane + width, 1
                for next_when, next_lane, next_width, _ in ordered[index + 1:]:
                    if next_when != when or next_lane != end:
                        break
                    end, count = next_lane + next_width, count + 1
                    trace_events.append((when, lane, end - lane, count))
        first_geometry = tuple((0., lane, width) for group, members, lane, width in self.targets
                               if group == self.primary and self.primary_member in members)
        self.trace_phase_context = (TracePhaseContext((*first_geometry, *(self.following_geometry or ())), trace_events)
                                    if trace_heads else None)
        self.trace_constellation = ()
        self.following_offsets = tuple(sorted({value[0] for value in self.following_geometry or ()}))
        self.current_heads = ()
        self.trace_constellation = ()
        self.measurement = {}

    def measure(self, frame, baseline):
        self.current_heads = ()
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        original = cv2.cvtColor(baseline, cv2.COLOR_BGR2HSV).astype(np.int16)
        hue = np.abs(hsv[:, :, 0].astype(np.int16) - original[:, :, 0])
        changed = (np.minimum(hue, 180 - hue) > 6) | (np.abs(hsv[:, :, 1].astype(np.int16) - original[:, :, 1]) > 35)
        source_mask = cv2.inRange(hsv, (15, 55, 135), (45, 255, 255))
        core_mask = trace_core_mask(hsv) if self.trace_heads else cv2.inRange(hsv, (15, 0, 235), (45, 110, 255))
        _, sources, source_stats, _ = cv2.connectedComponentsWithStats(source_mask, connectivity=8)
        _, cores, core_stats, _ = cv2.connectedComponentsWithStats(core_mask, connectivity=8)
        self.measurement = {'nonempty': bool(np.any(source_mask[40:540])), 'reason': 'no_complete_head',
                            'gold_sources': int(len(source_stats) - 1), 'bright_cores': int(len(core_stats) - 1)}
        targets = self.targets
        records, unknown, source_depths = [], [], {}

        def eligible(x, y, width, height, target):
            if self.trace_heads and self.groups[target[0]][target[1][0]].kind == 'trace':
                return trace_head_geometry(x, y, width, height, target[2], target[3])
            return chart_bound_head_geometry(x, y, width, height, target[2], target[3])

        def register(x, y, width, height, pixels, matched, *, label=None, trace_markers=()):
            cy = float(y + height / 2)
            if not matched or not np.any(pixels):
                return
            baseline_confirmed = np.count_nonzero(changed[y:y + height, x:x + width][pixels]) >= np.count_nonzero(pixels) * .75
            if not baseline_confirmed and not self.motion:
                return
            actual_targets = []
            for target in matched:
                group_index, members, lane, span = target
                is_trace = self.trace_heads and all(self.groups[group_index][member].kind == 'trace' for member in members)
                scale = trace_projection_scale(cy) if is_trace else cy / 570
                expected_left = 640 + (140 + (lane - 2) * (1000 / 12) - 640) * scale
                expected_right = expected_left + span * (1000 / 12) * scale
                # 位置仍受原整框中心容差约束；同一真实框内的成员使用该帧共同偏移，不改变透视宽度。
                offset = x + width / 2 - (expected_left + expected_right) / 2
                expected_left, expected_right = expected_left + offset, expected_right + offset
                covered = True
                available_markers = set(range(len(trace_markers)))
                for member in members:
                    point = self.groups[group_index][member].points[0]
                    member_scale = trace_projection_scale(cy) if self.trace_heads and self.groups[group_index][member].kind == 'trace' else cy / 570
                    member_left = 640 + (140 + (point.lane - 2) * (1000 / 12) - 640) * member_scale + offset
                    member_width = point.width * (1000 / 12) * member_scale
                    if trace_markers:
                        marker = next((index for index in available_markers if abs(trace_markers[index]
                                      - (member_left + member_width / 2)) <= max(3, member_width * .15)), None)
                        if marker is None:
                            covered = False
                            break
                        available_markers.remove(marker)
                    left, right = max(x, math.ceil(member_left)), min(x + width, math.floor(member_left + member_width))
                    local = pixels[:, max(0, left - x):max(0, right - x)]
                    # 成员归属必须由该成员位置的实际完整芯列证明，不能把整框宽度替代缺失首头。
                    if local.size == 0 or np.count_nonzero(np.count_nonzero(local, axis=0) >= height * .5) < member_width * .65:
                        covered = False
                        break
                if covered:
                    actual_targets.append(target)
            if not actual_targets:
                unknown.append(cy)
                return
            matched = actual_targets
            padding = max(2, math.ceil(height / 2))
            strip = sources[max(0, y - padding):min(frame.shape[0], y + height + padding),
                            max(0, x - padding):min(frame.shape[1], x + width + padding)]
            nonzero = strip[strip != 0]
            labels = np.unique(nonzero)
            # 哪怕第二个来源只有一个像素，完整前芯仍是阻断证据，不能continue后选择后方芯。
            left = sources[y:y + height, max(0, x - padding):x + 2]
            right = sources[y:y + height, x + width - 2:min(frame.shape[1], x + width + padding)]
            owners = [int(value) for value in labels if np.count_nonzero(left == value) >= height
                      and np.count_nonzero(right == value) >= height]
            if len(owners) != 1 or len(nonzero) < width:
                unknown.append(cy)
                return
            source = owners[0]
            for other in labels:
                if int(other) == source:
                    continue
                ox, oy, ow, oh = source_stats[other][:4]
                inner = core_mask[oy + 2:oy + oh - 2, ox + 2:ox + ow - 2] != 0 if ow > 4 and oh > 4 else np.empty(0)
                parts = sorted((tuple(stats[:4]) for stats in core_stats[1:]
                                if ox <= stats[0] and stats[0] + stats[2] <= ox + ow
                                and oy <= stats[1] and stats[1] + stats[3] <= oy + oh
                                and stats[4] >= stats[2] * stats[3] * .75), key=lambda box: box[0])
                adjacent = False
                for group_index in {value[0] for value in matched}:
                    possibilities = {-1}
                    for box in parts:
                        possibilities = {target[1][0] for target in targets if target[0] == group_index
                                         and eligible(*box, target) and abs(box[1] + box[3] / 2 - cy) <= 3
                                         and any(previous < target[1][0] for previous in possibilities)}
                    adjacent |= bool(parts and possibilities)
                trace_adjacent = False
                if self.trace_heads:
                    for tx, ty, tw, th, actual_pixels, marker_centers in trace_boxes:
                        actual = sources[ty:ty + th, tx:tx + tw]
                        same_group = (abs(ty + th / 2 - cy) <= 3 and any(
                            target[0] in {value[0] for value in matched} and eligible(tx, ty, tw, th, target) for target in targets))
                        known_rear = ty + th <= y and trace_source_proof((tx, ty, tw, th, actual_pixels, marker_centers),
                                                                       sources, changed, candidates=trace_boxes)
                        if ((same_group or known_rear) and np.count_nonzero(actual == other) >= tw
                                and np.count_nonzero(changed[ty:ty + th, tx:tx + tw][actual_pixels])
                                    >= np.count_nonzero(actual_pixels) * .75
                                ):
                            trace_adjacent = True
                            break
                # 其它label必须自身是同深度、同一相接子组的完整横头；孤立像素不因此获豁免。
                if not trace_adjacent and (not adjacent or abs(oy + oh / 2 - cy) > 3 or inner.size == 0
                        or np.count_nonzero(inner) < inner.size * .75):
                    unknown.append(cy)
                    return
            bottom = int(source_stats[source][1] + source_stats[source][3])
            if not baseline_confirmed:
                # 已跟踪的完整前沿差分不足时只保留未知，不能采样，也不能让后方完整芯接管。
                if y + height <= bottom <= y + 2 * height:
                    unknown.append(cy)
                return
            if y + height >= 540 or bottom > 540:
                raise FirstHeadOutsideWindow('真实首组完整芯或来源已越过同步窗口，拒绝由后继接管')
            if not y + height <= bottom <= y + 2 * height:
                unknown.append(cy)
                return
            # 来源向下延伸的后方芯尚未取得前沿资格，不能污染同来源多候选的判定。
            source_depths.setdefault(source, []).append((cy, int(height)))
            records.extend({'group': target[0], 'members': set(target[1]), 'y': cy, 'height': int(height),
                            'source': source, 'left': int(x), 'right': int(x + width)} for target in matched)

        trace_targets = [target for target in targets if self.trace_heads and self.groups[target[0]][target[1][0]].kind == 'trace']
        other_targets = [target for target in targets if target not in trace_targets]
        markers = set()
        if trace_targets:
            trace_boxes, markers = trace_head_candidates(hsv, cores, core_stats)
            self.trace_constellation = tuple((*box[:4], len(box[5])) for box in trace_boxes
                                             if trace_source_proof(box, sources, changed, candidates=trace_boxes))
            self.measurement['trace_heads'] = len(trace_boxes)
            self.measurement['nonempty'] = bool(trace_boxes)
            self.measurement['trace_boxes'] = [[int(value) for value in box[:4]] for box in trace_boxes[:16]]
            for x, y, width, height, pixels, trace_markers in trace_boxes:
                matched = [target for target in trace_targets if eligible(x, y, width, height, target)]
                if matched:
                    register(x, y, width, height, pixels, matched, trace_markers=trace_markers)
                else:
                    unknown.append(float(y + height / 2))
            rejected = []
            for label, (x, y, width, height, area) in enumerate(source_stats[1:], 1):
                if not any(eligible(x, y, width, height, target) for target in trace_targets):
                    continue
                pixels = sources[y:y + height, x:x + width] == label
                if np.count_nonzero(changed[y:y + height, x:x + width][pixels]) >= area * .75:
                    rejected.append([int(value) for value in (x, y, width, height)])
                    if not any(abs(y + height / 2 - (box[1] + box[3] / 2)) <= 3 for box in trace_boxes):
                        unknown.append(float(y + height / 2))
            if rejected:
                self.measurement['nonempty'] = True
                self.measurement['rejected_sources'] = rejected[:16]
                self.measurement['reason'] = 'trace_marker_or_source_incomplete'
        for label, (x, y, width, height, area) in enumerate(core_stats[1:], 1):
            if label in markers:
                continue
            matched = [target for target in other_targets if eligible(x, y, width, height, target)]
            if matched and area >= width * height * .75:
                register(x, y, width, height, cores[y:y + height, x:x + width] == label, matched)
            elif not matched and area >= width * height * .75:
                cy = float(y + height / 2)
                pixels = cores[y:y + height, x:x + width] == label
                # 不属首组位置的完整金芯仍是全局未知前沿，不能因没有元数据匹配而被忽略。
                if (y > 40 and width > max(14, .5 * (1000 / 12) * cy / 570)
                        and 2 <= height <= max(36, cy * .16) and width > height * 2
                        and 640 - 500 * cy / 570 - 25 <= x + width / 2 <= 640 + 500 * cy / 570 + 25
                        and np.count_nonzero(changed[y:y + height, x:x + width][pixels]) >= area * .75):
                    if self.trace_heads:
                        # TRACE 中性白芯也可来自界面；完全无金来源才排除，任何孤立金像素仍保守阻断。
                        padding = max(2, math.ceil(height / 2))
                        strip = source_mask[max(0, y - padding):min(frame.shape[0], y + height + padding),
                                            max(0, x - padding):min(frame.shape[1], x + width + padding)]
                        if not np.any(strip):
                            continue
                    unknown.append(cy)
        for label, (x, y, width, height, area) in enumerate(source_stats[1:], 1):
            if any(value['source'] == label and abs(value['y'] - (y + height / 2)) <= 3 for value in records):
                # 同一前沿已有更精确的完整芯证明，不以外框包围盒的厚边重复制造未知候选。
                continue
            matched = [target for target in other_targets if eligible(x, y, width, height, target)]
            if not matched or width <= 4 or height <= 4:
                continue
            # SLIDE正常body可向上接入浅芯；只按真实金色外框的实际完整横头框测内部，不裁预期宽度。
            interior = core_mask[y + 2:y + height - 2, x + 2:x + width - 2] != 0
            if np.count_nonzero(interior) < interior.size * .75:
                continue
            pixels = core_mask[y:y + height, x:x + width] != 0
            register(x, y, width, height, pixels, matched, label=label)
        bad_sources = {source for source, depths in source_depths.items()
                       if any(abs(a[0] - b[0]) > max(3, 2 * max(a[1], b[1])) for a in depths for b in depths)}
        unknown.extend(record['y'] for record in records if record['source'] in bad_sources)
        records = [record for record in records if record['source'] not in bad_sources]
        fronts = []
        for index, group in enumerate(self.groups):
            rows = []
            for record in sorted((value for value in records if value['group'] == index), key=lambda value: value['y']):
                if not rows or abs(rows[-1][0]['y'] - record['y']) > 3:
                    rows.append([])
                rows[-1].append(record)
            complete = []
            for row in rows:
                coverage = {0: (-math.inf, [])}
                for member in range(len(group)):
                    if member not in coverage:
                        continue
                    previous_right, previous = coverage[member]
                    for value in row:
                        if min(value['members']) != member or value['left'] < previous_right - 2:
                            continue
                        following = max(value['members']) + 1
                        # 只用实际不重叠的横框按顺序覆盖各成员；同一芯不能偏移后重复充当缺失头。
                        if following not in coverage or value['right'] < coverage[following][0]:
                            coverage[following] = (value['right'], previous + [value])
                if len(group) in coverage:
                    chosen = coverage[len(group)][1]
                    if any(not any(value['left'] < proof['right'] and value['right'] > proof['left'] for proof in chosen)
                           for value in row):
                        unknown.append(max(value['y'] for value in row))
                        continue
                    complete.append({'y': float(np.median([value['y'] for value in chosen])),
                                     'sources': {value['source'] for value in chosen},
                                     'height': max(value['height'] for value in chosen), 'heads': tuple(chosen)})
            if not complete:
                return None, unknown
            if len(complete) > 1 and complete[-1]['y'] - complete[-2]['y'] <= max(
                    25, 2 * max(complete[-1]['height'], complete[-2]['height'])):
                return None, unknown
            fronts.append(complete[-1])
        if any(abs(value['y'] - fronts[self.primary]['y']) > 3 for value in fronts):
            return None, unknown
        # 有间隙的子组必须来自独立真实分量；不能将跨间隙的连线当成宽横头。
        if any(left['sources'] & right['sources'] for index, left in enumerate(fronts) for right in fronts[index + 1:]):
            return None, unknown
        self.current_heads = tuple(value for front in fronts for value in front['heads'])
        self.measurement['measured_y'] = fronts[self.primary]['y']
        return fronts[self.primary]['y'], unknown

    def observe(self, frame, baseline, captured_at):
        if not self.groups or self.lost:
            return None
        if self.following_geometry is None:
            self.lost, self.status = True, 'invalid_following_context'
            return None
        if self.motion and captured_at - self.motion[-1][0] > .5:
            self.lost, self.status = True, 'continuity_lost'
            return None
        y, unknown = self.measure(frame, baseline)
        if self.trace_heads:
            self.measurement['identity_contract'] = ('unseeded' if self.trace_identity_requires_constellation is None
                else 'per_frame_constellation' if self.trace_identity_requires_constellation else 'causal_motion')
        if not self.motion and any(value >= 130 for value in unknown):
            self.lost, self.status = True, 'unseeded_late_unknown_head'
            return None
        if y is None or any(value >= y for value in unknown):
            self.status = 'unknown_front'
            return None
        if not self.motion:
            if y >= 130:
                self.lost, self.status = True, 'unseeded_late_head'
                return None
            requires_constellation = self.trace_heads and any(trace_head_geometry(head['left'], head['y'] - head['height'] / 2,
                    head['right'] - head['left'], head['height'], lane, span)
                    for _, lane, span in self.following_geometry for head in self.current_heads)
            if requires_constellation:
                # 单帧与真实后继几何相同即无首次身份，不能因处于顶部就偏向第一颗。
                primary = next(head for head in self.current_heads if head['group'] == self.primary
                               and self.primary_member in head['members'])
                box = (primary['left'], primary['y'] - primary['height'] / 2,
                       primary['right'] - primary['left'], primary['height'])
                unique, reason = self.trace_phase_context.unique(box, self.trace_constellation)
                self.measurement['phase_identity'] = reason
                if not unique:
                    self.status = 'unseeded_identity_ambiguous'
                    return None
            self.trace_identity_requires_constellation = bool(requires_constellation)
            if self.trace_heads:
                self.measurement['identity_contract'] = ('per_frame_constellation' if requires_constellation else 'causal_motion')
        else:
            previous_time, previous_y = self.motion[-1]
            if captured_at <= previous_time:
                return None
            if y < previous_y - 3:
                self.lost, self.status = True, 'motion_identity_lost'
                return None
            if y - previous_y < 3:
                return None
            if self.trace_identity_requires_constellation:
                # 同帧完整首前缀是身份正证据；恒速预测不再兼任身份裁决。
                # 任一证明缺失或仍兼容后继都只等待，不采样、不续期，也不能改回运动合同。
                primary = next(head for head in self.current_heads if head['group'] == self.primary
                               and self.primary_member in head['members'])
                box = (primary['left'], primary['y'] - primary['height'] / 2,
                       primary['right'] - primary['left'], primary['height'])
                unique, reason = self.trace_phase_context.unique(box, self.trace_constellation)
                self.measurement['phase_identity'] = reason
                if not unique:
                    self.status = 'unknown_constellation_identity'
                    return None
            elif self.motion:
                origin = self.motion[0][0]
                times = np.array([value[0] - origin for value in self.motion])
                heights = np.array([value[1] for value in self.motion])
                envelope = self.motion
                if self.trace_heads and len(self.motion) > 8 and times[-1] >= .25:
                    # TRACE 的量化及缓变速度不能被开场旧点永久锁成单一模型。
                    # 只取已确认近期点，覆盖原宽轨迹的250毫秒且至少八点；当前点不参与。
                    start = min(len(self.motion) - 8,
                                max(0, int(np.searchsorted(times, times[-1] - .25, side='right')) - 1))
                    envelope = self.motion[start:]
                    if len(envelope) > 32:
                        # 高频时均匀选择真实点并保留两端，不插值、不重置完整轨迹。
                        envelope = [envelope[index] for index in np.linspace(0, len(envelope) - 1, 32, dtype=int)]
                envelope_times = np.array([value[0] - origin for value in envelope])
                envelope_heights = np.array([value[1] for value in envelope])
                left, right = np.triu_indices(len(envelope), 1)
                elapsed = envelope_times[right] - envelope_times[left]
                confirmed = False
                alternative = False
                predictions = []
                offsets = sorted({delta for delta, lane, span in self.following_geometry
                                  if any((trace_head_geometry if self.trace_heads else chart_bound_head_geometry)(
                                         head['left'], head['y'] - head['height'] / 2,
                                         head['right'] - head['left'], head['height'], lane, span) for head in self.current_heads)})
                queries = np.array([captured_at - origin, *(captured_at - origin - delta for delta in offsets)])
                # 只用已确认前缀建立因果预测带；当前候选不能参与拟合后证明自身。
                # 三像素量化带不扩大；独立全历史十二像素预测与全部后继竞争仍保留。
                for model, shift in (('linear', 0), ('exponential', 0), ('perspective', 30)):
                    if len(self.motion) >= 3:
                        transformed = heights if model == 'linear' else np.log(heights + shift)
                        rate, intercept = np.polyfit(times, transformed, 1)
                        if rate > 0:
                            fitted = rate * times + intercept if model == 'linear' else np.exp(rate * times + intercept) - shift
                            prediction = rate * (captured_at - origin) + intercept if model == 'linear' else math.exp(
                                rate * (captured_at - origin) + intercept) - shift
                            predictions.append((float(np.max(np.abs(heights - fitted))), model, prediction))
                    low = envelope_heights - 3 if model == 'linear' else np.log(envelope_heights - 3 + shift)
                    high = envelope_heights + 3 if model == 'linear' else np.log(envelope_heights + 3 + shift)
                    if len(self.motion) == 1:
                        # 第二点之前也检查替代身份；只一帧不能先把可疑后继纳入速度估计。
                        minimum_rate, maximum_rate = (60., 9000.) if model == 'linear' else (1., 10.)
                    else:
                        minimum_rate = max(0., float(np.max((low[right] - high[left]) / elapsed)))
                        maximum_rate = float(np.min((high[right] - low[left]) / elapsed))
                    if not 0 < maximum_rate or minimum_rate > maximum_rate:
                        continue
                    future = queries[:, None] - envelope_times[None, :]
                    lower = np.max(low + np.minimum(minimum_rate * future, maximum_rate * future), axis=1)
                    upper = np.min(high + np.maximum(minimum_rate * future, maximum_rate * future), axis=1)
                    if model != 'linear':
                        lower, upper = np.exp(lower) - shift, np.exp(upper) - shift
                    feasible = (lower - 3 <= y) & (y <= upper + 3) & (lower <= upper)
                    confirmed |= bool(feasible[0])
                    alternative |= bool(np.any(feasible[1:]))
                if self.trace_heads:
                    self.measurement['motion_envelope'] = {
                        'points': len(envelope), 'span_s': float(envelope[-1][0] - envelope[0][0]),
                        'confirmed': bool(confirmed), 'alternative': bool(alternative)}
                if predictions:
                    chosen = min(predictions)
                    perspective = next((value for value in predictions if value[1] == 'perspective'), None)
                    if perspective is not None and perspective[0] <= chosen[0] + 1.:
                        chosen = perspective
                    # 长缺帧时参数可行带可能变宽，仍须通过独立历史最佳模型的原十二像素界。
                    prediction_error = abs(y - chosen[2])
                    if self.trace_heads:
                        self.measurement['motion_envelope']['history_prediction_error_px'] = float(prediction_error)
                    confirmed &= prediction_error <= 12
                if not confirmed:
                    self.lost, self.status = True, 'motion_identity_lost'
                    return None
                if alternative:
                    # 身份暂不唯一只等待新证据；不采样、不续期，持续歧义仍由原连续性期限拒绝。
                    self.status = 'motion_identity_ambiguous'
                    return None
        self.motion.append((captured_at, y))
        self.status = 'tracking'
        return y


def _trace_fit_window(samples, seconds):
    # 两端必须来自已接受真帧；不足成熟跨度或八点时不能退回噪声短窗口。
    if len(samples) < 8 or samples[-1][0] - samples[0][0] < seconds:
        return None
    times = np.array([sample[0] for sample in samples])
    start = min(len(samples) - 8,
                max(0, int(np.searchsorted(times, times[-1] - seconds, side='right')) - 1))
    window = samples[start:]
    if len(window) < 8:
        return None
    if len(window) > 32:
        window = [window[index] for index in np.linspace(0, len(window) - 1, 32, dtype=int)]
    return window


def _anchor_fit_candidates(recent, captured_at, short_window, *, preserve_quality=False):
    origin = recent[0][0]
    offsets = np.array([sample[0] - origin for sample in recent])
    heights = np.array([sample[1] for sample in recent])
    candidates = []
    quality_candidates = []
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
        if 60 <= current_velocity <= 9000 and residual <= (4 if short_window else 12):
            # 已进入歧义等待后，早模型不能仅因剩余时间不足就消失并放行迟模型。
            if preserve_quality or first_hit <= captured_at + .8:
                quality_candidates.append((residual, name, first_hit))
            if preserve_quality or captured_at + .040 <= first_hit <= captured_at + .8:
                candidates.append((residual, name, first_hit))
    return candidates, quality_candidates


class StartAnchor:
    def __init__(self, gesture: Gesture, *, minimum_samples: int = 2, simultaneous_gestures=(), dense_following_gestures=(), gold_tap_following_gestures=(), gold_tap_slide_following_gestures=(), gold_tap_context_chart=None) -> None:
        self.gesture = gesture
        self.minimum_samples = minimum_samples
        # 双金色长条保护默认开启，仅明确 0 回退；本局初始化冻结，纯检测函数不读取环境。
        self.gold_slide_pair_guard = os.environ.get("MAAPJSK_GOLD_SLIDE_PAIR_GUARD") != "0"
        self.simultaneous_gestures = validated_simultaneous(gesture, simultaneous_gestures,
                                                          gold_slide_pair_guard=self.gold_slide_pair_guard)
        self.dense_following_gestures = validated_dense_followers(gesture, dense_following_gestures)
        self.samples: list[tuple[float, float]] = []
        self.baseline: np.ndarray | None = None
        self.fit: dict | None = None
        self.failure_reason: str | None = None
        # 首头保护默认开启并冻结到本局；仅明确 0 回退，非普通金色 TAP 仍走原识别路径。
        self.gold_tap_head_guard = os.environ.get("MAAPJSK_GOLD_TAP_HEAD_GUARD") != "0"
        self.chart_bound_head_guard = (chart_bound_head_guard_requested()
                                       and first_note_color(gesture) == 'gold' and gesture.kind in {'tap', 'slide'})
        self.trace_opening_head_guard = (trace_opening_head_guard_requested()
                                        and first_note_color(gesture) == 'gold' and gesture.kind == 'trace')
        self.chart_bound_tracker = (ChartBoundHeadTracker(gold_tap_context_chart, gesture, trace_heads=self.trace_opening_head_guard)
                                   if self.chart_bound_head_guard or self.trace_opening_head_guard else None)
        self.observation = {}
        # 独立后继 TAP 保护默认开启，仅明确 0 回退；本局冻结，不读取运行中环境变化。
        self.gold_tap_followers_guard = os.environ.get("MAAPJSK_GOLD_TAP_FOLLOWERS_GUARD") != "0"
        # 蓝 TAP 上下文候选默认关闭，只有明确 1 启用；本局冻结并服从两个既有主保护回退。
        self.gold_tap_blue_context_guard = (self.gold_tap_head_guard and self.gold_tap_followers_guard
                                           and os.environ.get("MAAPJSK_GOLD_TAP_BLUE_CONTEXT_GUARD") == "1")
        if gold_tap_context_chart is not None:
            # 只对本 Anchor 的实际首手势提取一次，不能错用其他谱面的后继或在观察中读取环境。
            gold_tap_following_gestures = (gold_tap_following_context(
                gold_tap_context_chart, skip_blue_taps=self.gold_tap_blue_context_guard, expected_first=gesture))
        self.gold_tap_following_gestures = validated_gold_tap_followers(gesture, gold_tap_following_gestures)
        # 同轨后继长条保护已通过保存帧及同曲单人验收，默认开启并冻结，仅明确 0 回退。
        self.gold_tap_slide_follower_guard = (self.gold_tap_head_guard
                                             and os.environ.get("MAAPJSK_GOLD_TAP_SLIDE_FOLLOWER_GUARD") != "0")
        self.gold_tap_slide_following_gestures = validated_gold_tap_slide_follower(gesture, gold_tap_slide_following_gestures)
        self.gold_tap_slide_pair_confirmed = False
        # 本局创建时冻结保护设置；明确主开关优先，兼容旧试验开关，空值不关闭默认保护。
        guard_setting = os.environ.get("MAAPJSK_START_ANCHOR_AMBIGUITY_GUARD")
        if guard_setting not in {"0", "1"}:
            guard_setting = os.environ.get("MAAPJSK_START_ANCHOR_AMBIGUITY_TRIAL")
        self.ambiguity_guard = guard_setting != "0"
        self.ambiguity_defer: dict | None = None

    def observe(self, frame: np.ndarray, captured_at: float, *, capture_seconds: float = 0.0) -> float | None:
        if self.baseline is None:
            self.baseline = frame.copy()
            if self.trace_opening_head_guard:
                hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
                core = trace_core_mask(hsv)
                _, labels, stats, _ = cv2.connectedComponentsWithStats(core, connectivity=8)
                present, _ = trace_head_candidates(hsv, labels, stats)
                gold = cv2.inRange(hsv, (15, 55, 135), (45, 255, 255))
                _, sources = cv2.connectedComponents(gold, connectivity=8)
                # 首帧没有差分基线，但仍须同一真实金框贯穿两侧，不能把纯白装饰冒认为已有TRACE。
                actual = np.ones(frame.shape[:2], dtype=bool)
                if any(trace_source_proof(box, sources, actual, candidates=present) for box in present):
                    self.chart_bound_tracker.lost = True
                    self.chart_bound_tracker.status = 'baseline_contains_head'
                    self.failure_reason = 'trace_baseline_contains_head'
                    raise RuntimeError('首帧已包含TRACE头，无法证明尚未经过首音，拒绝中途启动')
            return None
        if self.samples and captured_at - self.samples[0][0] > 2:
            if len(self.samples) == 1:
                self.failure_reason = "candidate_motion_unconfirmed"
                raise RuntimeError("首音候选未确认运动且等待超时，拒绝从歌曲中途开始")
            self.failure_reason = "trajectory_gate_timeout"
            raise RuntimeError("首音轨迹未通过同步门槛且等待超时，拒绝从歌曲中途开始")
        try:
            slide_evidence = {}
            if self.chart_bound_tracker is not None:
                y = self.chart_bound_tracker.observe(frame, self.baseline, captured_at)
                self.observation = dict(self.chart_bound_tracker.measurement)
                self.observation['reason'] = self.chart_bound_tracker.status + ':' + self.observation.get('reason', 'unknown')
                if self.chart_bound_tracker.lost:
                    self.failure_reason = 'chart_bound_head_' + self.chart_bound_tracker.status
                    raise RuntimeError('真实首组身份已丢失，拒绝等待后继重新建立首音：' + self.chart_bound_tracker.status)
            else:
                self.observation = {'nonempty': False, 'reason': 'legacy_rejected'}
                y = first_note_y(frame, self.gesture, self.baseline, simultaneous_gestures=self.simultaneous_gestures,
                            dense_following_gestures=self.dense_following_gestures, reject_passed_slide=True,
                            gold_tap_following_gestures=self.gold_tap_following_gestures,
                            gold_tap_head_guard=self.gold_tap_head_guard, gold_slide_pair_guard=self.gold_slide_pair_guard,
                            gold_tap_followers_guard=self.gold_tap_followers_guard,
                            gold_tap_slide_following_gestures=self.gold_tap_slide_following_gestures,
                            gold_tap_slide_follower_guard=self.gold_tap_slide_follower_guard,
                            gold_tap_slide_evidence=slide_evidence, observation=self.observation)
                self.observation.update(reason='legacy_candidate' if y is not None else self.observation['reason'], measured_y=y)
        except FirstHeadOutsideWindow:
            self.failure_reason = "first_head_outside_window"
            raise
        if y is None:
            return None
        slide_context_active = (not self.chart_bound_head_guard and self.gold_tap_slide_follower_guard
                                and bool(self.gold_tap_slide_following_gestures))
        # 双芯已证明后，单芯缺失属于未知；既有轨迹不能改由剩下的后继长条继续拟合。
        if slide_context_active and self.gold_tap_slide_pair_confirmed and not slide_evidence.get("pair_confirmed"):
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
        if slide_context_active:
            self.gold_tap_slide_pair_confirmed |= bool(slide_evidence.get("pair_confirmed"))
            # 初始迟到只看到后继时不能假定它是首 TAP；完整双芯关系成立前只积累原轨迹。
            if not self.gold_tap_slide_pair_confirmed:
                return None
        recent = self.samples[-5:]
        fit_window = "recent"
        # 快截图的五帧可能仅覆盖约 125 ms，局部斜率噪声会放大成整曲相位误差。
        # 已有足够轨迹时直接利用最近 1.25 秒、最多 32 点；不为凑宽窗口等待新帧。
        # 慢链路仍沿用原五帧与低样本门禁，宽窗口不合格时也不能退回噪声短窗口。
        trace_check = None
        if len(self.samples) >= 8 and (recent[-1][0] - recent[0][0] < .250 or self.ambiguity_defer is not None):
            if self.chart_bound_tracker is not None and self.chart_bound_tracker.trace_identity_requires_constellation:
                # 已逐帧证明身份的 TRACE 只用成熟近期运动预测时刻，避免顶部非平稳段拖偏外推。
                recent = _trace_fit_window(self.samples, .500)
                trace_check = _trace_fit_window(self.samples, .250)
                if recent is None or trace_check is None:
                    self.observation['timing_windows'] = {'status': 'waiting_mature_history'}
                    return None
                fit_window = "trace_recent_trajectory"
            else:
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
            if self.gold_tap_head_guard:
                self.fit["gold_tap_head_guard"] = True
            if self.gold_slide_pair_guard:
                self.fit["gold_slide_pair_guard"] = True
            if self.gold_tap_followers_guard:
                self.fit["gold_tap_followers_guard"] = True
            self.fit["gold_tap_blue_context_guard"] = self.gold_tap_blue_context_guard
            self.fit['chart_bound_head_guard'] = self.chart_bound_head_guard
            self.fit['trace_opening_head_guard'] = self.trace_opening_head_guard
            if self.gold_tap_slide_follower_guard:
                self.fit["gold_tap_slide_follower_guard"] = True
                self.fit["gold_tap_slide_pair_confirmed"] = bool(self.gold_tap_slide_pair_confirmed)
            return first_hit - self.gesture.start
        if y < 230:
            return None
        candidates, quality_candidates = _anchor_fit_candidates(
            recent, captured_at, short_window, preserve_quality=trace_check is not None)
        check_candidates, check_quality = (_anchor_fit_candidates(trace_check, captured_at, False, preserve_quality=True)
                                           if trace_check is not None else ([], []))
        if self.ambiguity_guard and fit_window in {"wide_trajectory", "trace_recent_trajectory"}:
            # 主窗与独立核验窗都执行原模型歧义门禁，不能因窗口标签变化而绕过。
            for quality in ([quality_candidates, check_quality] if trace_check is not None else [quality_candidates]):
                if len(quality) < 2:
                    continue
                earliest = min(hit for _, _, hit in quality)
                latest = max(hit for _, _, hit in quality)
                disagreement = latest - earliest
                # 4 像素沿用严格质量门槛，80 ms 沿用启动保护量；不改变模型偏好或一般容差。
                if min(error for error, _, _ in quality) > 4 and disagreement > .080:
                    if self.ambiguity_defer is None:
                        self.ambiguity_defer = {"defer_count": 0, "first_captured_at": captured_at,
                                                "first_sample_count": len(self.samples), "max_disagreement_ms": 0.0}
                    self.ambiguity_defer.update(
                        defer_count=self.ambiguity_defer["defer_count"] + 1,
                        last_captured_at=captured_at, new_sample_count=len(self.samples) - self.ambiguity_defer["first_sample_count"],
                        disagreement_ms=disagreement * 1000,
                        max_disagreement_ms=max(self.ambiguity_defer["max_disagreement_ms"], disagreement * 1000),
                        earliest_hit=earliest, latest_hit=latest, status="waiting")
                    if earliest - captured_at <= max(0.0, capture_seconds) + .080:
                        self.ambiguity_defer["status"] = "rejected_margin"
                        self.failure_reason = "trajectory_model_ambiguity_margin"
                        raise RuntimeError("首音轨迹模型时刻仍有歧义且启动余量不足，拒绝从歌曲中途开始")
                    return None
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
        if trace_check is not None:
            if not check_candidates:
                self.observation['timing_windows'] = {'status': 'waiting_check_model'}
                return None
            check_residual, check_name, check_hit = min(check_candidates)
            check_perspective = next((candidate for candidate in check_candidates if candidate[1] == 'perspective'), None)
            if check_perspective is not None and check_perspective[0] <= check_residual + 1.0:
                check_residual, check_name, check_hit = check_perspective
            # 先按原质量及一像素偏好选择，再检查原时间域；不能删除更优晚模型去放行早模型。
            if not (captured_at + .040 <= first_hit <= captured_at + .8
                    and captured_at + .040 <= check_hit <= captured_at + .8):
                self.observation['timing_windows'] = {'status': 'waiting_selected_model_horizon',
                    'primary_first_hit': first_hit, 'check_first_hit': check_hit}
                return None
            disagreement = abs(first_hit - check_hit)
            self.observation['timing_windows'] = {
                'status': 'consistent' if disagreement <= .080 else 'waiting_disagreement',
                'primary_seconds': recent[-1][0] - recent[0][0],
                'check_seconds': trace_check[-1][0] - trace_check[0][0],
                'check_sample_count': len(trace_check), 'check_model': check_name,
                'check_residual_pixels': check_residual, 'check_first_hit': check_hit,
                'disagreement_ms': disagreement * 1000}
            if disagreement > .080:
                if min(first_hit, check_hit) - captured_at <= max(0.0, capture_seconds) + .080:
                    self.failure_reason = 'trajectory_window_disagreement_margin'
                    raise RuntimeError('首音近期窗口预测仍不一致且启动余量不足，拒绝从歌曲中途开始')
                return None
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
        if trace_check is not None:
            self.fit['timing_windows'] = dict(self.observation['timing_windows'])
        if self.ambiguity_guard:
            self.fit["ambiguity_guard"] = {"enabled": True, "deferred": self.ambiguity_defer is not None}
            if self.ambiguity_defer is not None:
                self.ambiguity_defer.update(status="resolved", new_sample_count=len(self.samples) - self.ambiguity_defer["first_sample_count"])
                self.fit["ambiguity_guard"].update(self.ambiguity_defer)
        if self.gold_tap_head_guard:
            self.fit["gold_tap_head_guard"] = True
        if self.gold_slide_pair_guard:
            self.fit["gold_slide_pair_guard"] = True
        if self.gold_tap_followers_guard:
            self.fit["gold_tap_followers_guard"] = True
        self.fit["gold_tap_blue_context_guard"] = self.gold_tap_blue_context_guard
        self.fit['chart_bound_head_guard'] = self.chart_bound_head_guard
        self.fit['trace_opening_head_guard'] = self.trace_opening_head_guard
        if self.gold_tap_slide_follower_guard:
            self.fit["gold_tap_slide_follower_guard"] = True
            self.fit["gold_tap_slide_pair_confirmed"] = bool(self.gold_tap_slide_pair_confirmed)
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
