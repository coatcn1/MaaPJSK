from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

import numpy as np

from project_sekai.life_monitor import LifeGuard, LifeDepleted
from project_sekai.native_observation import NativeObservation
from project_sekai.solo_live import SoloLive
from project_sekai.cooperative_live import CooperativeLive
from project_sekai.one_shot_live import OneShotLive
from project_sekai.game_timing import GameTimingGuard
from tests.test_native_playback import Clock


class CaptureJob:
    def __init__(self, clock, delay=.05, job_id=1, frame=None):
        self.clock, self.at, self.delay, self.job_id = clock, clock(), delay, job_id
        self.frame = np.zeros((720, 1280, 3), dtype=np.uint8) if frame is None else frame
        self.succeeded = True
        self.get_calls = 0

    @property
    def done(self):
        return self.clock() - self.at >= self.delay

    def get(self):
        self.get_calls += 1
        return self.frame

    def wait(self):
        raise AssertionError("Native 输入 owner 不得等待截图")


class ObservationTests(unittest.TestCase):
    def make_observer(self, clock, *, delay=.05, interval=.75, observe=None, invalidate=None):
        jobs = []
        def request():
            job = CaptureJob(clock, delay, len(jobs) + 1)
            jobs.append(job)
            return job
        observer = NativeObservation(request, observe or Mock(), interval=interval,
                                     invalidate=invalidate, clock=clock)
        return observer, jobs

    def test_one_pending_request_and_late_frames_never_accumulate_zero(self):
        clock = Clock(); guard = LifeGuard(); invalidations = []
        def invalidate(reason, requested_at):
            invalidations.append((reason, requested_at)); guard.zero_streak = 0
        observer, jobs = self.make_observer(clock, delay=1.917,
            observe=lambda frame, requested_at: guard.observe(frame, lambda _: True), invalidate=invalidate)
        for _ in range(4000):
            observer.step(); clock.sleep(.002)
        self.assertFalse(guard.zero_confirmed)
        self.assertEqual(guard.samples, 0)
        self.assertEqual(len(jobs), 5)
        self.assertTrue(all(job.get_calls == 0 for job in jobs))
        self.assertEqual(observer.report["skipped_stale"], 4)
        self.assertEqual(len(invalidations), 4)
        self.assertGreater(observer.report["skipped_pending_periods"], 0)
        observer.close()

    def test_two_fresh_frames_use_original_life_gate_and_request_timestamps(self):
        clock = Clock(); guard = LifeGuard(); observed = []
        def observe(frame, requested_at):
            observed.append(requested_at)
            if guard.observe(frame, lambda _: True):
                raise LifeDepleted("生命零")
        observer, jobs = self.make_observer(clock, interval=2., observe=observe)
        with self.assertRaises(LifeDepleted):
            for _ in range(1300):
                observer.step(); clock.sleep(.002)
        self.assertEqual(len(observed), 2)
        self.assertGreaterEqual(observed[1] - observed[0], 2.)
        self.assertEqual(observed, [job.at for job in jobs])
        self.assertTrue(guard.zero_confirmed)
        self.assertEqual(observer.report["samples"], 2)

    def test_duplicate_job_id_breaks_evidence(self):
        clock = Clock(); observe = Mock(); invalidate = Mock()
        observer = NativeObservation(lambda: CaptureJob(clock, 0, job_id=7), observe,
                                     interval=.75, invalidate=invalidate, clock=clock)
        observer.step(); observer.step()
        clock.sleep(.75); observer.step(); observer.step()
        self.assertEqual(observe.call_count, 1)
        self.assertEqual(observer.report["skipped_duplicate"], 1)
        invalidate.assert_called_once_with("duplicate", 1000.75)

    def test_stale_gap_resets_zero_abnormal_and_feedback_without_changing_phase(self):
        workflow = object.__new__(CooperativeLive)
        workflow.life_guard = LifeGuard()
        workflow.life_guard.zero_streak = 1
        workflow.life_guard.abnormal_page, workflow.life_guard.abnormal_frames = "home", 1
        workflow.game_timing_guard = GameTimingGuard()
        feedback = workflow.game_timing_guard
        feedback.correction_ms = 5
        feedback.direction, feedback.streak, feedback.evidence = "fast", 2, [{}, {}]
        workflow.invalidate_play_observation("stale", 1.)
        self.assertEqual(workflow.life_guard.zero_streak, 0)
        self.assertEqual(workflow.life_guard.abnormal_frames, 0)
        self.assertIsNone(workflow.life_guard.abnormal_page)
        self.assertEqual(feedback.evidence, [])
        self.assertEqual(feedback.streak, 0)
        self.assertEqual(feedback.correction_ms, 5)

    def test_permanent_pending_is_bounded_and_close_never_waits_or_requests(self):
        clock = Clock(); observer, jobs = self.make_observer(clock, delay=100)
        observer.step(); clock.sleep(10.)
        with self.assertRaisesRegex(RuntimeError, "十秒"):
            observer.step()
        self.assertEqual(len(jobs), 1)
        observer.close(); observer.step()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(observer.report["discarded_pending"], 1)
        self.assertFalse(observer.report["pending"])
        self.assertEqual(jobs[0].get_calls, 0)

    def test_prepare_wait_does_not_consume_playback_observation_deadline(self):
        clock = Clock(); observer, _ = self.make_observer(clock)
        clock.sleep(300.)
        observer.start(); observer.step()
        self.assertTrue(observer.report["pending"])

    def test_failed_job_and_invalid_frame_are_real_errors(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                clock = Clock()
                job = CaptureJob(clock, delay=0, frame=np.zeros((10,10,3)))
                job.succeeded = not failed
                observer = NativeObservation(lambda: job, Mock(), interval=.75, clock=clock)
                observer.step()
                with self.assertRaisesRegex(RuntimeError, "作业失败" if failed else "1280"):
                    observer.step()

    def test_default_off_options_preserve_old_call_and_all_native_tasks_can_enable(self):
        for cls in (SoloLive, CooperativeLive, OneShotLive):
            workflow = object.__new__(cls)
            workflow.device = SimpleNamespace(controller=SimpleNamespace(post_screencap=Mock()))
            workflow.observe_play_state = Mock()
            with patch.dict("os.environ", {}, clear=True):
                self.assertEqual(workflow.native_observation_options(Path('.')), {})
            with patch.dict("os.environ", {"MAAPJSK_NATIVE_ASYNC_OBSERVATION": "1"}):
                options = workflow.native_observation_options(Path('.'))
            options["captured_frame_observer"]("frame", 2.)
            workflow.observe_play_state.assert_called_once_with(Path('.'), frame="frame", requested_at=2.)
            self.assertTrue(options["async_observation"])

    def test_stop_during_callback_does_not_request_next_capture(self):
        clock = Clock(); stopping = [False]; jobs = []
        def request():
            job = CaptureJob(clock, delay=0, job_id=len(jobs) + 1); jobs.append(job); return job
        def observe(*_):
            clock.sleep(1.)
            stopping[0] = True
        observer = NativeObservation(request, observe, interval=.75, clock=clock,
                                     stop_requested=lambda: stopping[0])
        observer.step()
        with self.assertRaises(InterruptedError): observer.step()
        self.assertEqual(len(jobs), 1)
        observer.close()

    def test_death_evidence_waits_for_release_and_disk_error_cannot_replace_death(self):
        workflow = object.__new__(SoloLive)
        workflow.navigator = SimpleNamespace(_check_stop=Mock())
        workflow.check_observation_frame = lambda frame: frame
        workflow.observe_playfield = lambda *_: True
        workflow.life_epoch = 0.
        workflow.life_frames = []
        workflow.life_guard = LifeGuard()
        workflow.read_optional_number = lambda *_: 0
        workflow.record_performance_trace = Mock()
        frame = np.zeros((720,1280,3), dtype=np.uint8)
        release = threading.Event()
        order = []
        def write(*_):
            self.assertTrue(release.is_set())
            order.append("png")
            raise OSError("disk blocked/failed")
        with patch("project_sekai.solo_live.write_image", side_effect=write):
            workflow.observe_play_state(Path('.'), frame=frame, requested_at=1.)
            with self.assertRaises(LifeDepleted):
                workflow.observe_play_state(Path('.'), frame=frame, requested_at=2.)
            report = {"error": "LifeDepleted: life zero", "playback": {"release_confirmed": False}}
            workflow.save_playback_evidence(report, Path('.'))
            self.assertEqual(order, [])
            release.set(); order.append("reset")
            report["playback"]["release_confirmed"] = True
            workflow.save_playback_evidence(report, Path('.'))
        self.assertEqual(order, ["reset", "png"])
        self.assertEqual(report["error"], "LifeDepleted: life zero")
        self.assertIn("disk blocked", report["playback_evidence_errors"][0]["error"])
