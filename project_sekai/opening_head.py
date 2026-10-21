"""开场专用的实际金色 TRACE 标记测量；不执行截图、输入或磁盘操作。"""
from __future__ import annotations

import math
import os
from bisect import bisect_right

import cv2
import numpy as np


def trace_opening_head_guard_requested():
    return (os.environ.get('MAAPJSK_TRACE_OPENING_HEAD_GUARD', '1') == '1'
            and os.environ.get('MAAPJSK_GOLD_TAP_HEAD_GUARD') != '0')


def trace_head_geometry(x, y, width, height, lane, span):
    center = y + height / 2
    scale = trace_projection_scale(center)
    expected_width = span * (1000 / 12) * scale
    expected_center = 640 + (140 + (lane - 2 + span / 2) * (1000 / 12) - 640) * scale
    # TRACE 三角有独立中心，使用其实际横框的窄投影容差，不能借旧横芯的25像素容差替换邻轨。
    return (40 < y and width > 14 and 2 <= height <= max(36, center * .16)
            and width > height * 2 and expected_width * .5 < width < expected_width * 1.5 + 12
            and abs(x + width / 2 - expected_center) <= max(3, expected_width * .15))


def trace_projection_scale(center):
    # TRACE窄位置门槛与既有透视拟合使用同一消失线；不得靠扩大中心容差补偿模型不一致。
    return (center + 30) / 600


def trace_core_mask(hsv):
    # 中性白Hue可能为0，白芯只以原饱和度/亮度测量，金色归属另由实际外框和两侧来源证明。
    return cv2.inRange(hsv, (0, 0, 235), (179, 110, 255))


def trace_head_candidates(hsv, core_labels, core_stats):
    """从白亮三角和真实连续横条提取框；横条端点来自实际像素，不裁谱面预期宽度。"""
    # 白三角仍须V235；金条沿既有V205浅亮条件，避免将抗锯齿完整横条截成单行或右半条。
    bright = (cv2.inRange(hsv, (15, 55, 205), (45, 255, 255)) != 0) | (core_labels != 0)
    rows = {}
    candidates = []
    markers = set()

    def runs(row):
        if row not in rows:
            edges = np.flatnonzero(np.diff(np.r_[False, bright[row], False].astype(np.int8)))
            rows[row] = tuple(zip(edges[::2], edges[1::2]))
        return rows[row]

    for label, (x, y, width, height, area) in enumerate(core_stats[1:], 1):
        if not (40 < y < 540 and 3 <= width <= 100 and 2 <= height <= 40
                and .6 <= width / height <= 3.5 and width * height * .2 <= area <= width * height * .8):
            continue
        local = core_labels[y:y + height, x:x + width] == label
        row_widths = np.count_nonzero(local, axis=1)
        half = max(1, height // 2)
        if np.max(row_widths[half:]) < np.max(row_widths[:half]) * 1.25:
            continue
        cx = x + width / 2
        spans = []
        for row in range(y + height // 2, min(540, y + height + 6)):
            for left, right in runs(row):
                actual_width = right - left
                if actual_width >= max(16, width * 1.5) and left < cx < right:
                    spans.append((row, int(left), int(right)))
        blocks = []
        for row, left, right in spans:
            if (not blocks or row != blocks[-1][-1][0] + 1
                    or abs(left - blocks[-1][-1][1]) > 2 or abs(right - blocks[-1][-1][2]) > 2):
                blocks.append([])
            blocks[-1].append((row, left, right))
        blocks = [block for block in blocks if len(block) >= 2]
        if not blocks:
            continue
        block = max(blocks, key=lambda values: (min(right - left for _, left, right in values), len(values)))
        top, bottom = block[0][0], block[-1][0] + 1
        left = min(value[1] for value in block)
        right = max(value[2] for value in block)
        if not left < cx < right or not y < top <= y + height + 2:
            continue
        bar_pixels = bright[top:bottom, left:right]
        if np.count_nonzero(bar_pixels) < bar_pixels.size * .9:
            continue
        markers.add(label)
        # 同步位置取真实完整标记（三角顶至横条底）的中心，不拿最下方亮行的中心制造时相偏移。
        candidates.append((left, int(y), right - left, bottom - int(y), bright[y:bottom, left:right], (float(cx),)))
    merged = []
    for candidate in candidates:
        left, top, width, height, _, centers = candidate
        match = next((index for index, old in enumerate(merged) if old[0] == left and old[2] == width
                      and abs(old[1] - top) <= 2 and old[1] + old[3] == top + height), None)
        if match is None:
            merged.append(candidate)
        else:
            old = merged[match]
            new_top = min(top, old[1])
            bottom = top + height
            merged[match] = (left, new_top, width, bottom - new_top,
                             bright[new_top:bottom, left:left + width], old[5] + centers)
    merged = [value for value in merged if abs(float(np.mean(value[5])) - (value[0] + value[2] / 2))
              <= max(3, value[2] * .12)]
    return merged, markers


def trace_source_proof(box, sources, changed, *, candidates=()):
    if len(candidates) > 8:
        return False
    x, y, width, height, pixels, _ = box
    if y <= 40 or y + height >= 540 or not np.any(pixels):
        return False
    if np.count_nonzero(changed[y:y + height, x:x + width][pixels]) < np.count_nonzero(pixels) * .75:
        return False
    padding = max(2, math.ceil(height / 2))
    strip = sources[max(0, y - padding):y + height + padding,
                    max(0, x - padding):x + width + padding]
    labels = np.unique(strip[strip != 0])
    left = sources[y:y + height, max(0, x - padding):x + 2]
    right = sources[y:y + height, x + width - 2:x + width + padding]
    owners = [label for label in labels if np.count_nonzero(left == label) >= height
              and np.count_nonzero(right == label) >= height]
    if len(owners) != 1:
        return False
    for label in labels:
        if label == owners[0]:
            continue
        # 条带外的额外来源须独立完整且实际横框不相交；只检本帧归属，不递归借用相互证明。
        explained = False
        for other in candidates:
            ox, oy, ow, oh, opixels, _ = other
            if (not (oy + oh <= y or y + height <= oy or ox + ow <= x or x + width <= ox)
                    or np.count_nonzero(sources[oy:oy + oh, ox:ox + ow] == label) < ow
                    or not np.any(opixels)
                    or np.count_nonzero(changed[oy:oy + oh, ox:ox + ow][opixels]) < np.count_nonzero(opixels) * .75):
                continue
            opad = max(2, math.ceil(oh / 2))
            if (np.count_nonzero(sources[oy:oy + oh, max(0, ox - opad):ox + 2] == label) >= oh
                    and np.count_nonzero(sources[oy:oy + oh, ox + ow - 2:ox + ow + opad] == label) >= oh):
                explained = True
                break
        if not explained:
            return False
    return True


class TracePhaseContext:
    """初始化冻结全谱竞争起点，只用同帧正向标记证明唯一首前缀，不以缺失头排除后继。"""
    max_heads = 10000
    max_window_entries = 200000
    max_intervals = 64
    max_observed = 8
    max_comparisons = 20000

    def __init__(self, geometry, trace_events=()):
        self.patterns = []
        self.first_following = tuple((time, lane, span, count) for time, lane, span, count in trace_events if 1e-9 < time <= 1.)
        if geometry is None or len(geometry) > self.max_heads:
            return
        ordered = sorted(geometry)
        times = [value[0] for value in ordered]
        count = 0
        for index, origin in enumerate(ordered):
            end = bisect_right(times, origin[0] + 1.)
            start = bisect_right(times, origin[0] + 1e-9)
            following = tuple((time - origin[0], lane, span, 0) for time, lane, span in ordered[start:end])
            count += len(following)
            if count > self.max_window_entries:
                self.patterns = []
                return
            self.patterns.append((origin, following))

    def unique(self, head, observed):
        if not self.patterns:
            return False, 'phase_context_unavailable'
        if len(observed) > self.max_observed:
            return False, 'constellation_observation_limit'
        x, y, width, height = head[:4]
        front = y + height / 2
        rear = sorted((box for box in observed if box[1] + box[3] / 2 < front - 3),
                      key=lambda box: -(box[1] + box[3] / 2))
        depth_groups = []
        for box in rear:
            if not depth_groups or depth_groups[-1][0][1] + depth_groups[-1][0][3] / 2 - (box[1] + box[3] / 2) > 3:
                depth_groups.append([])
            depth_groups[-1].append(box)
        if len(depth_groups) < 2:
            return False, 'constellation_incomplete'

        comparisons = 0

        def explains(following, *, positive=False):
            nonlocal comparisons
            intervals = [(1., 10., 0.)]
            for group in depth_groups:
                states = [(lo, hi, previous, ()) for lo, hi, previous in intervals]
                for index, box in enumerate(group):
                    bx, by, bw, bh = box[:4]
                    center = by + bh / 2
                    possible = []
                    for delta, lane, span, count in following:
                        comparisons += 1
                        if comparisons > self.max_comparisons:
                            raise OverflowError
                        if positive and count != box[4] or not trace_head_geometry(bx, by, bw, bh, lane, span):
                            continue
                        lower = math.log((front + 30 - 3) / (center + 30 + 3)) / delta
                        upper = math.log((front + 30 + 3) / (center + 30 - 3)) / delta
                        for lo, hi, previous_delta, used in states:
                            comparisons += 1
                            if comparisons > self.max_comparisons:
                                raise OverflowError
                            time_matches = delta > previous_delta + 1e-9 if index == 0 else abs(delta - previous_delta) <= 1e-9
                            # 同深度可匹配同刻多头，但原始成员区间不能重复占用，合并跨度也不能再借给另一芯。
                            if (time_matches and not any(lane < right and lane + span > left for left, right in used)
                                    and max(lo, lower) <= min(hi, upper)):
                                possible.append((max(lo, lower), min(hi, upper), delta, used + ((lane, lane + span),)))
                    if not possible:
                        return False
                    if len(possible) > self.max_intervals:
                        raise OverflowError
                    states = possible
                possible = [(lo, hi, delta) for lo, hi, delta, _ in states]
                possible.sort(key=lambda value: (value[2], value[0]))
                merged = []
                for lo, hi, delta in possible:
                    if merged and delta == merged[-1][2] and lo <= merged[-1][1]:
                        merged[-1] = (merged[-1][0], max(hi, merged[-1][1]), delta)
                    else:
                        merged.append((lo, hi, delta))
                if len(merged) > self.max_intervals:
                    raise OverflowError
                intervals = merged
            return True

        first_explains = False
        try:
            for (time, lane, span), following in self.patterns:
                if not trace_head_geometry(x, y, width, height, lane, span):
                    continue
                if time <= 1e-9:
                    first_explains |= explains(self.first_following, positive=True)
                elif explains(following):
                    return False, 'later_constellation_compatible'
        except OverflowError:
            return False, 'constellation_comparison_limit'
        return (True, 'unique_first_constellation') if first_explains else (False, 'first_constellation_inconsistent')


class OpeningEvidence:
    """有界早期证据：非空拒绝按原因和时间分布留样，空前奏不占槽。"""
    capacity = 12

    def __init__(self, enabled):
        self.enabled = enabled
        self.frames = []
        self.reasons = []
        self.count = 0
        self.first_at = None

    def observe(self, when, frame, detail):
        if not self.enabled or not detail or not detail.get('nonempty'):
            return
        self.count += 1
        if self.first_at is None:
            self.first_at = when
        reason = str(detail.get('reason', 'unknown'))
        # 首八个几何非空帧连续保留，再留四个新原因槽，不能用半秒抽样挖掉身份建立所需的短前缀。
        if self.count <= 8:
            key = ('initial', self.count - 1)
        elif reason not in self.reasons:
            key = ('reason', reason)
        else:
            return
        if len(self.frames) >= self.capacity:
            return
        if reason not in self.reasons:
            self.reasons.append(reason)
        self.frames.append((key, when, frame, dict(detail)))

    def summary(self):
        return {'capacity': self.capacity, 'retained': len(self.frames), 'observed_nonempty': self.count,
                'frames': [{'captured_at': when, **detail} for _, when, _, detail in self.frames]}
