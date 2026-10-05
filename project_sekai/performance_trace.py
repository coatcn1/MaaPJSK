from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path

from .chart_player import START_ANCHOR_VERSION, TOUCH_PLAN_VERSION


class PerformanceTrace:
    """复用保护采样记录数值，输入结束后的证据阶段才序列化与写盘。"""

    def __init__(self, max_samples=16384):
        self.samples = []
        self.events = ()
        self.max_samples = max_samples
        self.dropped_samples = 0
        self.legacy_lateness = None

    def set_plan(self, events):
        # Touch 是不可变对象；只引用已经编译的计划，不在派发窗口复制整张谱面。
        self.events = events

    def set_legacy_feedback(self, lateness):
        # Legacy 已逐动作计算延迟，保留列表引用即可，不能再给每个输入增加记录回调。
        self.legacy_lateness = lateness

    def observe(self, elapsed, report, guard, *, life_sampled, zero_template_score=None, playback=None):
        if len(self.samples) >= self.max_samples:
            self.dropped_samples += 1
            return
        playback = report.get("playback", {}) if playback is None else playback
        row = {"kind": "observation", "phase": "playing", "elapsed_s": elapsed,
               "life": {"sampled": life_sampled, "hud_seen": guard.hud_seen,
                        "bar_fill_pixels": getattr(guard, "bar_fill_pixels", None) if life_sampled else None,
                        "bar_total_pixels": getattr(guard, "bar_total_pixels", None) if life_sampled else None,
                        "value": 0 if life_sampled and guard.zero_streak else None,
                        "zero_streak": guard.zero_streak, "zero_confirmed": guard.zero_confirmed,
                        "zero_template_score": zero_template_score},
               "playfield": dict(report.get("playfield_monitor", {})),
               "playback": {key: playback.get(key) for key in (
                   "planned_actions", "sent_actions", "executed_actions", "release_confirmed",
                   "queue_underflows", "chunks", "calibration_chunks", "active_contacts")},
               "latency_offsets": dict(playback.get("latency_offsets", {}))}
        if "active_contacts" in playback:
            row["playback"]["active_contacts"] = list(playback["active_contacts"])
        feedback = report.get("game_timing_feedback")
        if feedback is not None:
            row["game_timing"] = {"status": feedback.get("status"), "effective": feedback.get("effective"),
                                  "requested_correction_ms": feedback.get("correction_ms"),
                                  "published_correction_ms": playback.get("game_phase_correction_ms", 0.),
                                  "observation": dict(feedback.get("latest_observation", {}))}
        self.samples.append(row)

    @staticmethod
    def _write_lines(path, rows):
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                for row in rows:
                    stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def save(self, directory: Path, report):
        if not self.samples and not self.events:
            return {"status": "not_started", "samples": 0}
        header = {"kind": "session", "schema_version": 1, "recording_mode": "trace-only",
                  "mode": report.get("live_mode", report.get("mode", "solo")), "engine": report.get("engine"),
                  "chart": report.get("chart"), "identity": report.get("final_identity"),
                  "start_anchor": report.get("start_anchor"), "timing_offset_ms": report.get("timing_offset_ms"),
                  "start_anchor_version": START_ANCHOR_VERSION, "touch_plan_version": TOUCH_PLAN_VERSION,
                  "sample_source": "existing_playback_protection", "dropped_samples": self.dropped_samples,
                  "life_measurement": "bar_color_pixels_and_confirmed_zero; unread_values_are_null",
                  "touch_plan": "touch-plan.jsonl",
                  "native_receipts": "native-jlog.jsonl" if (directory / "native-jlog.jsonl").is_file() else None,
                  "native_touch_receipts": "native-touch-receipts.jsonl" if (directory / "native-touch-receipts.jsonl").is_file() else None,
                  "game_timing_feedback": {key: report.get("game_timing_feedback", {}).get(key)
                                           for key in ("version", "enabled", "effective", "status", "step_ms", "limit_ms")},
                  "device_clock_offset_s": report.get("playback", {}).get("device_clock_offset_s"),
                  "playback_epoch_s": report.get("playback", {}).get("playback_epoch_s"),
                  "touch_surface": report.get("playback", {}).get("touch_surface")}
        outcome = {"kind": "outcome", "completed": report.get("completed", False),
                   "cancelled": report.get("cancelled", False), "error": report.get("error"),
                   "playback_error": report.get("playback_error"), "life_zero_elapsed_s": report.get("life_zero_elapsed_s"),
                   "playback": report.get("playback", {}), "live_status": report.get("live_status"),
                   "judgements": report.get("judgements"), "end_detection": report.get("end_detection"),
                   "game_timing_feedback": report.get("game_timing_feedback"),
                   "background_exit": report.get("background_exit")}
        self._write_lines(directory / "touch-plan.jsonl", self._plan_rows(report))
        self._write_lines(directory / "trace.jsonl", self._rows(header, outcome))
        root = Path(report["report_path"]).parent if report.get("report_path") else directory
        try:
            prefix = directory.relative_to(root)
        except ValueError:
            prefix = Path(".")
        return {"status": "truncated" if self.dropped_samples else "saved", "mode": "trace-only",
                "path": (prefix / "trace.jsonl").as_posix(),
                "touch_plan_path": (prefix / "touch-plan.jsonl").as_posix(),
                "samples": len(self.samples), "dropped_samples": self.dropped_samples,
                "first_elapsed_s": self.samples[0]["elapsed_s"] if self.samples else None,
                "last_elapsed_s": self.samples[-1]["elapsed_s"] if self.samples else None}

    def _rows(self, header, outcome):
        yield header
        yield from self.samples
        yield outcome

    def _plan_rows(self, report):
        sent = report.get("playback", {}).get("sent_actions", 0)
        for index, event in enumerate(self.events):
            row = {"index": index, **asdict(event)}
            if self.legacy_lateness is not None and index < len(self.legacy_lateness):
                row["legacy_dispatch_lateness_ms"] = self.legacy_lateness[index] * 1000
                row["legacy_input_succeeded"] = index < sent
            yield row
