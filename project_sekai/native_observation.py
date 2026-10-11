"""Native 输入 owner 仅轮询截图作业；不等待设备截图完成。"""

from __future__ import annotations

import math
import time


class NativeObservation:
    def __init__(self, request, observe, *, interval, invalidate=None, clock=time.perf_counter,
                 max_age=None, unavailable_timeout=10., stop_requested=lambda: False):
        self.request, self.observe, self.invalidate = request, observe, invalidate
        self.clock, self.interval = clock, interval
        self.stop_requested = stop_requested
        self.max_age = min(1., interval) if max_age is None else max_age
        self.unavailable_timeout = unavailable_timeout
        if (not math.isfinite(interval) or interval <= 0 or not math.isfinite(self.max_age) or self.max_age <= 0
                or not math.isfinite(unavailable_timeout) or unavailable_timeout <= self.max_age):
            raise ValueError("Native 异步保护截图期限无效")
        self.pending = None
        self.pending_completed_at = None
        self.pending_timing = None
        self.requested_at = None
        self.last_request = -math.inf
        self.last_valid = clock()
        self.last_delivered = -math.inf
        self.last_job_id = None
        self.closed = False
        self.report = {"async_enabled": True, "capture_clock": "request_start", "interval_s": interval,
                       "max_frame_age_ms": self.max_age * 1000, "unavailable_timeout_s": unavailable_timeout,
                       "requests": 0, "samples": 0, "skipped_stale": 0, "skipped_duplicate": 0,
                       "skipped_pending_periods": 0, "discarded_pending": 0, "pending": False,
                       "max_capture_cost_ms": 0., "max_request_cost_ms": 0., "max_callback_cost_ms": 0.,
                       "max_cost_ms": 0., "max_frame_age_observed_ms": 0., "slow_callbacks": 0}
        self.report.update(capture_cost_basis="done_poll_minus_request", max_get_cost_ms=0.,
                           timing_samples=[], timing_samples_dropped=0)
        self.last_pending_period = 0

    def start(self):
        # 准备与首音同步可等待数分钟，保护超时必须从正式输入开始计算。
        self.last_valid = self.clock()

    def _skip(self, reason, requested_at):
        self.report["skipped_" + reason] += 1
        if self.invalidate is not None:
            self.invalidate(reason, requested_at)

    def step(self, *, allow_request=True, allow_callback=True):
        if self.closed:
            return
        self.check_stop()
        now = self.clock()
        if now - self.last_valid >= self.unavailable_timeout:
            self.report["error"] = "fresh-frame-timeout"
            raise RuntimeError("Native 演奏保护截图连续十秒未取得新鲜画面，停止本轮输入")
        if self.pending is not None:
            age = now - self.requested_at
            self.report["pending_age_ms"] = age * 1000
            period = int(age / self.interval)
            if period > self.last_pending_period:
                self.report["skipped_pending_periods"] += period - self.last_pending_period
                self.last_pending_period = period
            if self.pending.done:
                if self.pending_completed_at is None:
                    self.pending_completed_at = now
                    capture_cost = (now - self.requested_at) * 1000
                    self.report["max_capture_cost_ms"] = max(self.report["max_capture_cost_ms"], capture_cost)
                    if self.pending_timing is not None:
                        self.pending_timing["completion_observed_at_s"] = now
                job, requested_at = self.pending, self.requested_at
                if age <= self.max_age and not allow_callback:
                    return
                self.pending = None
                self.report["pending"] = False
                self.report["max_frame_age_observed_ms"] = max(self.report["max_frame_age_observed_ms"], age * 1000)
                if not job.succeeded:
                    self.report["error"] = "capture-job-failed"
                    raise RuntimeError("MFA 异步保护截图作业失败")
                if age > self.max_age:
                    if self.pending_timing is not None:
                        self.pending_timing.update(status="stale", discarded_at_s=now, frame_age_ms=age * 1000)
                    self._skip("stale", requested_at)
                elif requested_at <= self.last_delivered:
                    self._skip("duplicate", requested_at)
                else:
                    # Job.get 的默认 wait=False；只在 done 后读当前作业结果。
                    get_started = self.clock()
                    frame = job.get()
                    self.report["max_get_cost_ms"] = max(self.report["max_get_cost_ms"], (self.clock() - get_started) * 1000)
                    if frame is None or frame.shape[:2] != (720, 1280):
                        self.report["error"] = "invalid-frame"
                        raise RuntimeError(f"MFA 截图不是 1280×720：{getattr(frame, 'shape', None)}")
                    began = self.clock()
                    self.check_stop()
                    if began - requested_at > self.max_age:
                        if self.pending_timing is not None:
                            self.pending_timing.update(status="stale-after-get", discarded_at_s=began,
                                                       frame_age_ms=(began - requested_at) * 1000)
                        self._skip("stale", requested_at)
                        return
                    if self.pending_timing is not None:
                        self.pending_timing.update(status="consuming", consumer_started_at_s=began,
                                                   frame_age_ms=(began - requested_at) * 1000)
                    self.last_delivered = requested_at
                    self.last_valid = now
                    try:
                        self.observe(frame, requested_at)
                    except Exception as error:
                        if self.pending_timing is not None:
                            self.pending_timing["callback_error"] = f"{type(error).__name__}: {error}"
                        raise
                    finally:
                        cost = (self.clock() - began) * 1000
                        if self.pending_timing is not None:
                            self.pending_timing.update(status="consumed", consumer_finished_at_s=self.clock(),
                                                       callback_cost_ms=cost)
                        self.report["samples"] += 1
                        self.report["max_callback_cost_ms"] = max(self.report["max_callback_cost_ms"], cost)
                        self.report["max_cost_ms"] = max(self.report["max_cost_ms"], cost)
                        self.report["slow_callbacks"] += int(cost > 180)
        now = self.clock()
        if self.pending is None and allow_request and now - self.last_request >= self.interval:
            self.check_stop()
            self.last_request = self.requested_at = now
            self.report["requests"] += 1
            timing = {"request": self.report["requests"], "requested_at_s": now, "status": "pending"}
            if len(self.report["timing_samples"]) < 4096:
                self.report["timing_samples"].append(timing)
            else:
                self.report["timing_samples_dropped"] += 1
            job = self.request()
            timing.update(request_returned_at_s=self.clock(), job_id=getattr(job, "job_id", None))
            request_cost = (self.clock() - now) * 1000
            self.report["max_request_cost_ms"] = max(self.report["max_request_cost_ms"], request_cost)
            job_id = getattr(job, "job_id", None)
            if job_id is not None and job_id == self.last_job_id:
                timing["status"] = "duplicate"
                self._skip("duplicate", now)
                return
            self.last_job_id = job_id
            self.pending = job
            self.pending_timing = timing
            self.pending_completed_at = None
            self.last_pending_period = 0
            self.report["pending"] = True

    def check_stop(self):
        if self.stop_requested():
            raise InterruptedError("用户已停止 Native 谱面演出")

    def close(self):
        # Maa 作业无法撤回；放弃结果且不等它完成，触点释放不能等待截图。
        self.closed = True
        if self.pending is not None:
            self.report["discarded_pending"] += 1
            if self.pending_timing is not None:
                self.pending_timing.update(status="discarded-on-close", discarded_at_s=self.clock())
        self.pending = None
        self.report["pending"] = False
