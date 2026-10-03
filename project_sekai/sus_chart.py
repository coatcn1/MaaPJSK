from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from fractions import Fraction
import math
import re


@dataclass(frozen=True)
class Point:
    time: float
    lane: int
    width: int
    kind: int = 1
    direction: int = 0

    @property
    def x(self) -> float:
        return 140 + (self.lane - 2 + self.width / 2) * (1000 / 12)


@dataclass(frozen=True)
class Gesture:
    points: tuple[Point, ...]
    kind: str
    flick: int = 0
    critical: bool = False

    @property
    def start(self) -> float:
        return self.points[0].time

    @property
    def end(self) -> float:
        return self.points[-1].time


@dataclass(frozen=True)
class Chart:
    gestures: tuple[Gesture, ...]
    ticks_per_beat: int
    bpm_changes: tuple[tuple[float, float], ...]

    @property
    def first(self) -> Gesture:
        return min(self.gestures, key=lambda gesture: gesture.start)

    @property
    def duration(self) -> float:
        return max(gesture.end for gesture in self.gestures)


def _positive(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("SUS 节拍或 BPM 必须为有限正数")
    return number


def parse_sus(text: str) -> Chart:
    """将 SUS 节拍转换为时间；装饰轨道不参与触控。"""
    rows: list[tuple[int, str, str]] = []
    lengths: dict[int, Fraction] = {0: Fraction(4)}
    bpms: dict[str, float] = {}
    ticks = 480
    base = 0
    wave_offset = 0.0
    for raw in text.splitlines():
        line = raw.strip()
        if found := re.match(r'^#REQUEST\s+"ticks_per_beat\s+(\d+)"', line, re.I):
            ticks = int(found[1])
        elif found := re.match(r"^#MEASUREBS\s+(\d+)", line, re.I):
            base = int(found[1])
        elif found := re.match(r"^#WAVEOFFSET\s+([-+\d.eE]+)", line, re.I):
            wave_offset = float(found[1])
        elif found := re.match(r"^#BPM([0-9a-z]{2}):\s*(\S+)", line, re.I):
            bpms[found[1].upper()] = _positive(found[2])
        elif found := re.match(r"^#(\d{3})([0-9a-z]{2,3}):\s*(\S*)", line, re.I):
            measure, channel, body = int(found[1]) + base, found[2].lower(), found[3]
            if channel == "02":
                _positive(body)
                lengths[measure] = Fraction(body)
            elif body:
                if len(body) % 2 or not re.fullmatch(r"[0-9a-z]+", body, re.I):
                    raise ValueError("SUS 数据行不是合法的双字符序列")
                rows.append((measure, channel, body))
    if ticks <= 0 or not math.isfinite(wave_offset):
        raise ValueError("SUS 时间属性无效")
    starts: dict[int, Fraction] = {}
    beat = Fraction(0)
    length = Fraction(4)
    for measure in range(max((row[0] for row in rows), default=0) + 1):
        length = lengths.get(measure, length)
        starts[measure] = beat
        beat += length
    length_measures = sorted(lengths)

    def positions(measure: int, body: str):
        length = lengths[length_measures[bisect_right(length_measures, measure) - 1]]
        for index in range(len(body) // 2):
            token = body[index * 2:index * 2 + 2]
            if token != "00":
                yield starts[measure] + length * Fraction(index, len(body) // 2), token

    tempos: dict[Fraction, float] = {}
    for measure, channel, body in rows:
        if channel == "08":
            for position, token in positions(measure, body):
                if token.upper() not in bpms:
                    raise ValueError("SUS 引用了未定义的 BPM")
                tempos[position] = bpms[token.upper()]
    if Fraction(0) not in tempos:
        raise ValueError("SUS 缺少起始 BPM")
    tempo_beats = sorted(tempos)
    tempo_seconds = [0.0]
    for previous, current in zip(tempo_beats, tempo_beats[1:]):
        tempo_seconds.append(tempo_seconds[-1] + float(current - previous) * 60 / tempos[previous])

    def seconds(position: Fraction) -> float:
        index = bisect_right(tempo_beats, position) - 1
        return tempo_seconds[index] + float(position - tempo_beats[index]) * 60 / tempos[tempo_beats[index]] - wave_offset

    shorts: dict[tuple[Fraction, int], tuple[int, int]] = {}
    directions: dict[tuple[Fraction, int], int] = {}
    slide_rows: dict[str, list[tuple[Fraction, int, int, int]]] = {}
    for measure, channel, body in rows:
        if channel == "08" or channel[0] == "9":
            continue
        if int(channel[1], 36) not in range(2, 14):
            continue
        if channel[0] not in {"1", "3", "5"}:
            raise ValueError(f"尚未支持的 SUS 轨道：{channel}")
        if len(channel) != (3 if channel[0] == "3" else 2):
            raise ValueError("SUS 轨道长度无效")
        lane = int(channel[1], 36)
        for position, token in positions(measure, body):
            kind, width = int(token[0], 36), int(token[1], 36)
            # 0、1、f 是技能或 fever 等系统通道；不会向演奏区域派发它们。
            if not 2 <= lane <= 13:
                continue
            if width < 1 or lane + width > 14:
                raise ValueError("SUS 音符超出十二条演奏轨道")
            if channel[0] == "1":
                if kind not in range(1, 9):
                    raise ValueError(f"未知短音符类型：{kind}")
                shorts[position, lane] = (kind, width)
            elif channel[0] == "5":
                if kind not in range(1, 7):
                    raise ValueError("未知 flick 或缓动方向")
                directions[position, lane] = kind
            else:
                if kind not in {1, 2, 3, 4, 5}:
                    raise ValueError("未知滑条节点类型")
                slide_rows.setdefault(channel[2], []).append((position, lane, width, kind))
    gestures: list[Gesture] = []
    consumed: set[tuple[Fraction, int]] = set()
    for nodes in slide_rows.values():
        active: list[Point] = []
        active_critical = False
        for position, lane, width, kind in sorted(nodes, key=lambda item: (item[0], 0 if item[3] == 2 else 1)):
            point = Point(seconds(position), lane, width, kind, directions.get((position, lane), 0))
            consumed.add((position, lane))
            if kind == 1:
                if active:
                    raise ValueError("同一滑条通道存在重叠开头")
                active = [point]
                active_critical = shorts.get((position, lane), (1, width))[0] in {2, 6}
            elif not active:
                raise ValueError("滑条节点没有对应的开头")
            else:
                active.append(point)
                if kind == 2:
                    if point.time <= active[0].time:
                        raise ValueError("滑条持续时间必须大于零")
                    gestures.append(Gesture(tuple(active), "slide", point.direction, active_critical))
                    active = []
        if active:
            raise ValueError("滑条没有结束节点")
    for (position, lane), (kind, width) in shorts.items():
        if (position, lane) in consumed or kind in {4, 7, 8}:
            continue
        direction = directions.get((position, lane), 0)
        if kind == 3 and not direction:
            direction = 1
        gestures.append(Gesture((Point(seconds(position), lane, width, kind),), "trace" if kind in {5, 6} else "tap", direction, kind in {2, 6}))
    if not gestures or any(not math.isfinite(gesture.start) for gesture in gestures):
        raise ValueError("SUS 没有可演奏音符或开始时间无效")
    return Chart(tuple(sorted(gestures, key=lambda gesture: gesture.start)), ticks,
                 tuple((float(position), tempos[position]) for position in tempo_beats))


def slide_x(points: tuple[Point, ...], when: float) -> float:
    """使用节点的时间坐标反解 Bézier 参数，避免把控制点当作直线节点。"""
    if when <= points[0].time:
        return points[0].x
    if when >= points[-1].time:
        return points[-1].x
    anchor = 0
    for index in range(1, len(points)):
        if points[index].kind == 4:
            continue
        if when > points[index].time:
            anchor = index
            continue
        segment = points[anchor:index + 1]

        def curve(progress: float) -> tuple[float, float]:
            values = [(point.time, point.x) for point in segment]
            while len(values) > 1:
                values = [((1 - progress) * a[0] + progress * b[0],
                           (1 - progress) * a[1] + progress * b[1]) for a, b in zip(values, values[1:])]
            return values[0]

        lower, upper = 0.0, 1.0
        for _ in range(20):
            middle = (lower + upper) / 2
            if curve(middle)[0] < when:
                lower = middle
            else:
                upper = middle
        progress = (lower + upper) / 2
        if len(segment) == 2:
            if segment[0].direction == 2:
                progress *= progress
            elif segment[0].direction in {5, 6}:
                progress = 1 - (1 - progress) ** 2
        return curve(progress)[1]
    raise ValueError("无法找到滑条时间段")
