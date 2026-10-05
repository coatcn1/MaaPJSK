from __future__ import annotations

from collections import deque
from dataclasses import asdict
import json
import math
from pathlib import Path
import time
from typing import Callable

import numpy as np

from . import native_engine
from .chart_player import validate_touches
from .native_minitouch import NativeMinitouchDevice

OFFSET_FIELDS = ("down_ms", "up_ms", "move_ms", "wait_ms", "interval_ms")
GAME_PHASE_FEEDBACK_VERSION = 1


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
                 idle_observer=None, device=None, clock=time.perf_counter, sleeper=time.sleep,
                 observation_interval=.75, observation_budget=None,
                 game_timing_correction: Callable[[], float] | None = None):
        validate_touches(events)
        if (not math.isfinite(observation_interval) or observation_interval <= 0
                or observation_budget is not None and (not math.isfinite(observation_budget) or observation_budget < 0)):
            raise ValueError("演奏保护采样间隔或耗时预算无效")
        self.events = events
        self.directory = Path(directory)
        self.stop_requested, self.clock, self.sleeper = stop_requested, clock, sleeper
        self.idle_observer, self.last_observation = idle_observer, -math.inf
        self.observation_interval = observation_interval
        self.observation_budget = observation_budget
        self.game_timing_correction = game_timing_correction
        self.game_phase_correction_ms = 0.0
        self.last_game_phase_request = None
        self.published_game_phase_ms = 0.0
        self.pending_game_phase_application = None
        self.playback_epoch = None
        self.action_receipts = []
        self.action_receipt_capacity = min(max(16384, len(events)), 262144)
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
        self.planned_drift = []
        self.wait_costs = deque(maxlen=64)
        self.clock_offset = None
        self.prepared = False
        self.report = {"engine": "native", "planned_actions": len(events), "sent_actions": 0,
                       "executed_actions": 0, "release_confirmed": False, "chunks": 0,
                       "calibration_chunks": 0, "clock_basis": "probe-midpoint", "native_version": module.version(),
                       "game_phase_feedback_version": GAME_PHASE_FEEDBACK_VERSION,
                       "game_phase_feedback_enabled": game_timing_correction is not None,
                       "game_phase_correction_ms": 0.0, "game_phase_applications": [],
                       "game_phase_rejections": []}
        if game_timing_correction is not None and not hasattr(self.timeline, "set_future_phase_correction"):
            raise RuntimeError("游戏判定反馈需要重新构建 Native 1.2.0")
        if game_timing_correction is not None:
            self.report["native_touch_receipts"] = {"schema_version": 1, "status": "pending-release",
                "path": str((self.directory / "native-touch-receipts.jsonl").resolve()),
                "planned_actions": len(events), "captured_actions": 0,
                "capacity": self.action_receipt_capacity, "truncated": False, "dropped_actions": 0}
        if idle_observer is not None:
            self.report["observation"] = {"interval_s": observation_interval, "samples": 0, "max_cost_ms": 0,
                                          "minimum_headroom_ms": max(.42, (observation_budget or 0) + .15) * 1000,
                                          "deferred_windows": 0}

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
        self.report["device_clock_offset_s"] = self.clock_offset
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
                if self.game_timing_correction is not None:
                    self.planned_drift.extend((actual - receipt.get("planned_time", receipt["time"])) * 1000
                                              for receipt in self.pending_receipts)
                    metadata = self.report["native_touch_receipts"]
                    for receipt in self.pending_receipts:
                        if len(self.action_receipts) < self.action_receipt_capacity:
                            self.action_receipts.append({"index": receipt["index"],
                                "planned_target_relative_s": receipt.get("planned_time", receipt["time"]) - self.playback_epoch,
                                "effective_target_relative_s": receipt["time"] - self.playback_epoch,
                                "game_phase_correction_ms": receipt.get("game_phase_correction_ms", 0.),
                                "actual_barrier_relative_s": actual - self.playback_epoch,
                                "execution_drift_ms": (actual - receipt["time"]) * 1000})
                        else:
                            metadata["truncated"] = True
                            metadata["dropped_actions"] += 1
                    metadata["captured_actions"] = len(self.action_receipts)
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

    def _update_game_phase(self):
        if self.game_timing_correction is None:
            return
        try:
            raw = self.game_timing_correction()
            request_key = repr(raw)
            if request_key == self.last_game_phase_request:
                return
            self.last_game_phase_request = request_key
            if isinstance(raw, bool):
                raise ValueError("游戏相位反馈不能为布尔值")
            requested = float(raw)
            if not math.isfinite(requested) or abs(requested) > 60 or abs(requested - self.game_phase_correction_ms) > 5 + 1e-9:
                raise ValueError("游戏相位反馈无效或超过单步限幅")
            self.timeline.set_future_phase_correction(requested)
        except (TypeError, ValueError, OverflowError) as error:
            # 非法可选反馈不改变设备脚本；只保留诊断，不能把它伪装成有效游戏证据。
            rejections = self.report["game_phase_rejections"]
            if len(rejections) < 64:
                rejections.append({"requested": self.last_game_phase_request,
                                   "at_s": self.clock(), "reason": str(error)})
            else:
                self.report["game_phase_rejections_dropped"] = self.report.get("game_phase_rejections_dropped", 0) + 1
            return
        self.game_phase_correction_ms = requested
        self.report["game_phase_correction_ms"] = requested

    def _record_game_phase_application(self, chunk):
        phase = float(chunk.get("game_phase_correction_ms", 0))
        if phase != self.published_game_phase_ms:
            if self.pending_game_phase_application is not None:
                self.pending_game_phase_application["status"] = "superseded-before-action"
            application = {"old_ms": self.published_game_phase_ms, "new_ms": phase,
                           "window_sequence": chunk["sequence"], "effective_after_s": chunk["start"],
                           "published_at_s": self.clock(), "first_action_index": None,
                           "first_action_time_s": None, "first_action_planned_time_s": None,
                           "first_action_effective_correction_ms": None, "status": "waiting-for-action",
                           "reason": "game-feedback-provider"}
            self.report["game_phase_applications"].append(application)
            self.pending_game_phase_application = application
            self.published_game_phase_ms = phase
        if self.pending_game_phase_application is not None and chunk["events"]:
            first = chunk["events"][0]
            self.pending_game_phase_application.update(first_action_index=first["index"],
                first_action_time_s=first["time"], first_action_planned_time_s=first["planned_time"],
                first_action_effective_correction_ms=first["game_phase_correction_ms"], status="applied")
            self.pending_game_phase_application = None

    def play(self, epoch: float, offset_ms: int):
        if not self.prepared:
            raise RuntimeError("Native 尚未在准备页完成连接")
        self.timeline.start(epoch, self.clock(), offset_ms)
        self.playback_epoch = epoch
        self.report.update(playback_epoch_s=epoch, frozen_timing_offset_ms=offset_ms)
        final_sent = False
        queued_until = self.clock()
        deadline = epoch + self.events[-1].time + offset_ms / 1000 + 5
        while not final_sent or self.expected:
            self.check_stop()
            self._observe()
            if self.clock() > deadline:
                raise RuntimeError("Native 最后一块未取得完整执行回执")
            chunk = None
            if not final_sent:
                self._update_game_phase()
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
                    self._record_game_phase_application(chunk)
                    final_sent = chunk["final"]
                    queued_until = chunk["end"]
                    deadline = max(deadline, queued_until + 5)
                    self.report["chunks"] += 1
                    self.report["sent_actions"] = self.timeline.sent
            sent = self.timeline.sent
            # 补足队列后才采样；保留至少 150 ms 供回执处理和下一窗口发布，耗时变大时推迟采样。
            headroom = max(.42, (self.observation_budget or 0) + .15)
            if (self.idle_observer is not None and sent < len(self.events)
                    and self.clock() - self.last_observation >= self.observation_interval):
                observation = self.report["observation"]
                if queued_until - self.clock() >= headroom:
                    observed_at = self.clock()
                    try:
                        self.idle_observer()
                    finally:
                        self.last_observation = self.clock()
                        cost = self.last_observation - observed_at
                        observation["samples"] += 1
                        observation["max_cost_ms"] = max(observation["max_cost_ms"], cost * 1000)
                        if self.observation_budget is not None:
                            self.observation_budget = max(self.observation_budget, cost)
                            observation["minimum_headroom_ms"] = max(.42, self.observation_budget + .15) * 1000
                elif chunk is not None:
                    observation["deferred_windows"] += 1
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
        if self.planned_drift:
            self.report.update({"planned_execution_drift_ms_p50": float(np.percentile(self.planned_drift, 50)),
                                "planned_execution_drift_ms_p95": float(np.percentile(self.planned_drift, 95)),
                                "planned_execution_drift_ms_max_abs": max(map(abs, self.planned_drift))})

    def close(self):
        # 生命保护和取消同样需要保留停止前的调度证据，不能只在完整结算时记录。
        self.record_execution_metrics()
        released = self.device.stop()
        self.report["release_confirmed"] = released
        self.report["release"] = self.device.release_diagnostics
        if not released:
            if self.game_timing_correction is not None:
                self.report["native_touch_receipts"]["status"] = "release-unconfirmed"
            raise RuntimeError(f"Native 触点释放未确认：{self.device.last_release_error}")
        if self.game_timing_correction is not None:
            metadata = self.report["native_touch_receipts"]
            try:
                # 逐动作目标和真实回执只在本轮释放确认后落盘，不占用演奏队列的发布余量。
                self.directory.mkdir(parents=True, exist_ok=True)
                with Path(metadata["path"]).open("w", encoding="utf-8", newline="\n") as stream:
                    for receipt in self.action_receipts:
                        stream.write(json.dumps(receipt, ensure_ascii=False, allow_nan=False) + "\n")
                metadata["status"] = "written"
            except (OSError, TypeError, ValueError) as error:
                metadata.update(status="failed", error=f"{type(error).__name__}: {error}")
