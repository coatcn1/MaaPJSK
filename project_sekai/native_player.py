from __future__ import annotations

from collections import deque
from dataclasses import asdict
import math
from pathlib import Path
import time

import numpy as np

from . import native_engine
from .chart_player import validate_touches
from .native_minitouch import NativeMinitouchDevice

OFFSET_FIELDS = ("down_ms", "up_ms", "move_ms", "wait_ms", "interval_ms")


def offsets_from_dict(values: dict):
    offsets = native_engine.module().TouchLatencyOffsets()
    for field in OFFSET_FIELDS:
        value = float(values.get(field, 0))
        if not math.isfinite(value) or abs(value) > 100:
            raise ValueError("Native 命令耗时配置无效")
        setattr(offsets, field, value)
    return offsets


def offsets_to_dict(offsets):
    return {field: float(getattr(offsets, field)) for field in OFFSET_FIELDS}


class NativePlayer:
    """在开演前准备设备，身份与首音锁定后发布 C++ 生成的滚动脚本。"""

    def __init__(self, controller, events, directory: Path, stop_requested, *, latency_offsets=None,
                 idle_observer=None, device=None, clock=time.perf_counter, sleeper=time.sleep):
        validate_touches(events)
        self.events = events
        self.stop_requested, self.clock, self.sleeper = stop_requested, clock, sleeper
        self.idle_observer, self.last_observation = idle_observer, -math.inf
        module = native_engine.module()
        self.timeline = module.Timeline([asdict(event) for event in events])
        self.compiler = module.ScriptCompiler()
        self.calibrator = module.LatencyCalibrator()
        self.offsets = offsets_from_dict(latency_offsets or {})
        self.compiler.set_offsets(self.offsets)
        if device is None:
            info = controller.info
            if not isinstance(info, dict) or not info.get("adb_path") or not info.get("adb_serial"):
                raise RuntimeError("Native 需要当前 MFA 控制器的 ADB 连接信息，请重新连接模拟器")
            device = NativeMinitouchDevice(info["adb_path"], info["adb_serial"], jlog_path=directory / "native-jlog.jsonl")
        self.device = device
        self.cursor = 0
        self.expected = deque()
        self.pending_receipts = []
        self.active = set()
        self.drift = []
        self.wait_costs = deque(maxlen=64)
        self.clock_offset = None
        self.prepared = False
        self.report = {"engine": "native", "planned_actions": len(events), "sent_actions": 0,
                       "executed_actions": 0, "release_confirmed": False, "chunks": 0,
                       "calibration_chunks": 0, "clock_basis": "probe-midpoint", "native_version": module.version()}

    def check_stop(self):
        if self.stop_requested():
            raise InterruptedError("用户已停止 Native 谱面演出")

    def prepare(self):
        self.check_stop()
        self.device.start()
        self.check_stop()
        if self.device.max_contacts < 10:
            raise RuntimeError("Native 设备不足十个触点，拒绝开演")
        self.rotation = self.device.surface_rotation
        if self.rotation == 0 and self.device.max_y > self.device.max_x:
            self.rotation = 1
        self.cursor, _ = self.device.log_records_since(0)
        expected = deque(("c", "w 0", "c"))
        sent_at = self.clock()
        # 无触点探测只建立设备时钟关联；正式演奏的回执与耗时窗口从 start 重新计数。
        self.device.publish("c\nw 0\nc\n")
        deadline = self.clock() + 2
        while expected and self.clock() < deadline:
            self.check_stop()
            self.cursor, records = self.device.log_records_since(self.cursor)
            for line, received in records:
                event = native_engine.parse_minitouch_log(line)
                if event is None:
                    continue
                command = event["command"].strip()
                if not expected or command != expected.popleft():
                    raise RuntimeError("Native 无触点探测回执顺序不匹配")
                if command == "w 0":
                    self.clock_offset = sent_at + (received - sent_at) / 2 - event["start_ms"] / 1000
                    self.report["probe_round_trip_ms"] = (received - sent_at) * 1000
            self.sleeper(.002)
        if expected or self.clock_offset is None:
            raise RuntimeError("Native 无触点探测未取得设备执行回执")
        self.prepared = True
        self.report["touch_surface"] = {"max_x": self.device.max_x, "max_y": self.device.max_y,
                                        "rotation": self.rotation}

    def _observe(self):
        self.cursor, records = self.device.log_records_since(self.cursor)
        for line, _ in records:
            event = native_engine.parse_minitouch_log(line)
            if event is None:
                continue
            if not self.expected:
                raise RuntimeError("Native 收到本轮脚本以外的设备回执")
            expected = self.expected.popleft()
            command = event["command"].strip()
            if command != expected["command"]:
                raise RuntimeError(f"Native 设备回执顺序不匹配：预期 {expected['command']!r}，实际 {command!r}")
            self.calibrator.observe(event)
            if command.startswith("w "):
                self.wait_costs.append(event["cost_ms"] - int(command.split()[1]))
            if expected["receipt"] is not None:
                self.pending_receipts.append(expected["receipt"])
                contact = int(command.split()[1])
                if command.startswith("d "):
                    self.active.add(contact)
                if command.startswith("u "):
                    self.active.discard(contact)
            if command == "c":
                actual = event["end_ms"] / 1000 + self.clock_offset
                self.report["executed_actions"] += len(self.pending_receipts)
                self.drift.extend((actual - receipt["time"]) * 1000 for receipt in self.pending_receipts)
                self.pending_receipts.clear()
            if expected["last"]:
                used = expected["used"]
                measured = self.calibrator.offsets
                counts = self.calibrator.sample_counts
                for field in OFFSET_FIELDS:
                    if counts[field.removesuffix("_ms")] <= 0:
                        setattr(measured, field, getattr(self.offsets, field))
                if counts["wait"] > 0 and len(self.wait_costs) >= 8:
                    values = np.asarray(self.wait_costs)
                    middle = float(np.median(values))
                    limit = max(.20, 4 * float(np.median(np.abs(values - middle))))
                    measured.wait_ms = float(np.clip(values, middle - limit, middle + limit).mean())
                # 只修改未来未编译的窗口；回执延迟不是游戏 FAST/LATE，不能改本局触控偏移。
                self.compiler.add_residual_ms(self.calibrator.correction_ms(used))
                self.offsets = measured
                self.compiler.set_offsets(measured)
                self.calibrator.reset()
                self.report["calibration_chunks"] += 1
                self.report["latency_offsets"] = offsets_to_dict(measured)

    def play(self, epoch: float, offset_ms: int):
        if not self.prepared:
            raise RuntimeError("Native 尚未在准备页完成连接")
        self.timeline.start(epoch, self.clock(), offset_ms)
        final_sent = False
        queued_until = self.clock()
        deadline = epoch + self.events[-1].time + offset_ms / 1000 + 5
        while not final_sent or self.expected:
            self.check_stop()
            self._observe()
            if self.clock() > deadline:
                raise RuntimeError("Native 最后一块未取得完整执行回执")
            if not final_sent:
                chunk = self.timeline.next(self.clock())
                if chunk is not None:
                    script = self.compiler.compile(chunk, self.device.max_x, self.device.max_y, self.rotation)
                    lines = script["lines"]
                    receipts = {value["line"]: value for value in script["receipts"]}
                    used = offsets_from_dict(offsets_to_dict(self.offsets))
                    self.expected.extend({"command": line, "receipt": receipts.get(index),
                                          "last": index == len(lines) - 1, "used": used}
                                         for index, line in enumerate(lines))
                    self.device.publish("\n".join(lines) + "\n")
                    final_sent = chunk["final"]
                    queued_until = chunk["end"]
                    self.report["chunks"] += 1
                    self.report["sent_actions"] = self.timeline.sent
            sent = self.timeline.sent
            # Native 由设备执行已排队的触控；补足窗口后串行截图，不依赖密集谱面的空档。
            if (self.idle_observer is not None and sent < len(self.events)
                    and queued_until - self.clock() >= .42
                    and self.clock() - self.last_observation > .75):
                self.idle_observer()
                self.last_observation = self.clock()
            self.sleeper(.002)
        if self.report["executed_actions"] != len(self.events) or self.active:
            raise RuntimeError("Native 执行动作或触点收尾不完整")
        self.record_execution_metrics()
        return self.report

    def record_execution_metrics(self):
        self.report["queue_underflows"] = self.timeline.underflows
        if self.drift:
            self.report.update({"execution_drift_ms_p50": float(np.percentile(self.drift, 50)),
                                "execution_drift_ms_p95": float(np.percentile(self.drift, 95)),
                                "execution_drift_ms_max_abs": max(map(abs, self.drift))})

    def close(self):
        # 生命保护和取消同样需要保留停止前的调度证据，不能只在完整结算时记录。
        self.record_execution_metrics()
        released = self.device.stop()
        self.report["release_confirmed"] = released
        self.report["release"] = self.device.release_diagnostics
        if not released:
            raise RuntimeError(f"Native 触点释放未确认：{self.device.last_release_error}")
