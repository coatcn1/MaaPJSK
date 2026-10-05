import copy
from types import SimpleNamespace
import unittest

import cv2
import numpy as np

from project_sekai.live_end import LiveEndGuard
from project_sekai.navigator import Navigator


class LiveEndTests(unittest.TestCase):
    def guard(self, *, seen=True, engine="legacy"):
        self.life = np.full((720, 1280, 3), 40, np.uint8)
        self.absent = np.full((720, 1280, 3), 60, np.uint8)
        self.black = np.zeros((720, 1280, 3), np.uint8)
        navigator = SimpleNamespace(threshold=.83, templates={"life_hud": None})
        navigator.match = lambda frame, page, *args: (
            1.0 if frame is self.life and page == "life_hud" else 0.0, (0, 0))
        report = {"engine": engine, "playback": {
            "planned_actions": 4, "sent_actions": 4, "executed_actions": 4, "release_confirmed": True,
            "release": {"reset_executed": True, "release_proof": "current-reset-jlog-and-cleanup"}}}
        return LiveEndGuard(navigator, report, life_seen=seen)

    def test_requires_life_hud_to_have_been_seen_during_this_round(self):
        guard = self.guard(seen=False)
        for _ in range(3):
            self.assertFalse(guard.observe(self.absent))
        self.assertFalse(guard.ended)
        self.assertNotIn("end_detection", guard.report)

    def test_two_missing_frames_confirm_end_only_after_input_and_release(self):
        guard = self.guard()
        self.assertFalse(guard.observe(self.absent))
        self.assertTrue(guard.observe(self.absent))
        self.assertEqual(guard.report["live_status"], "ended")
        self.assertNotEqual(guard.report["live_status"], "cleared")

    def test_life_reappearing_resets_a_single_missing_frame(self):
        guard = self.guard()
        self.assertFalse(guard.observe(self.absent))
        self.assertFalse(guard.observe(self.life))
        self.assertFalse(guard.observe(self.absent))
        self.assertTrue(guard.observe(self.absent))

    def test_black_and_white_transitions_reset_absence_confirmation(self):
        guard = self.guard()
        white = np.full((720, 1280, 3), 255, np.uint8)
        for blank in (self.black, white):
            self.assertFalse(guard.observe(self.absent))
            self.assertFalse(guard.observe(blank))
            self.assertEqual(guard.absent_frames, 0)
        self.assertFalse(guard.observe(self.absent))
        self.assertTrue(guard.observe(self.absent))

    def test_zero_life_still_has_hud_and_is_not_an_end(self):
        guard = self.guard()
        for _ in range(3):
            self.assertFalse(guard.observe(self.life))
        self.assertFalse(guard.ended)

    def test_life_returning_after_confirmation_blocks_further_end_actions(self):
        guard = self.guard()
        self.assertFalse(guard.observe(self.absent))
        self.assertTrue(guard.observe(self.absent))
        self.assertFalse(guard.observe(self.life))
        self.assertFalse(guard.ended)

    def test_incomplete_input_release_and_native_receipts_block_end(self):
        baseline = self.guard(engine="native").report
        for fault in ("sent_actions", "executed_actions", "release_confirmed", "reset_executed", "release_proof", "playback_error"):
            with self.subTest(fault=fault):
                guard = self.guard(engine="native")
                guard.report = copy.deepcopy(baseline)
                playback = guard.report["playback"]
                if fault == "playback_error":
                    guard.report[fault] = "输入失败"
                elif fault in {"reset_executed", "release_proof"}:
                    playback["release"][fault] = False
                else:
                    playback[fault] = 0
                for _ in range(3):
                    self.assertFalse(guard.observe(self.absent))
                self.assertFalse(guard.ended)

    def test_older_template_pack_can_use_pause_button_for_hud_presence(self):
        guard = self.guard(seen=False)
        guard.navigator.templates = {}
        guard.navigator.match = lambda frame, page, *args: (1.0 if frame is self.life and page == "playing" else 0.0, (0, 0))
        self.assertFalse(guard.observe(self.life))
        self.assertFalse(guard.observe(self.absent))
        self.assertTrue(guard.observe(self.absent))
        self.assertEqual(guard.report["end_detection"]["hud_detector"], "pause_button")

    def test_visible_pause_button_blocks_end_when_life_label_match_is_lost(self):
        guard = self.guard()
        guard.navigator.match = lambda frame, page, *args: (1.0 if page == "playing" else 0.0, (0, 0))
        for _ in range(3):
            self.assertFalse(guard.observe(self.absent))
        self.assertFalse(guard.ended)

    def test_cooperative_end_requires_stable_positive_settlement_evidence_after_all_receipts(self):
        guard = self.guard(engine='native')
        guard = LiveEndGuard(guard.navigator,guard.report,life_seen=True,require_settlement=True)
        for _ in range(4):
            self.assertFalse(guard.observe(self.absent))
        self.assertFalse(guard.observe(self.absent,settlement_page='team_result'))
        self.assertTrue(guard.observe(self.absent,settlement_page='team_result'))
        self.assertEqual(guard.report['end_detection']['method'],'cooperative_settlement_confirmed')
        self.assertTrue(guard.observe(self.absent))
        self.assertFalse(guard.observe(self.life))

    def test_cooperative_end_cannot_confirm_different_pages_or_incomplete_native_input(self):
        guard = self.guard(engine='native')
        guard = LiveEndGuard(guard.navigator,guard.report,life_seen=True,require_settlement=True)
        self.assertFalse(guard.observe(self.absent,settlement_page='team_result'))
        self.assertFalse(guard.observe(self.absent,settlement_page='personal_result'))
        guard.report['playback']['executed_actions'] = 3
        self.assertFalse(guard.observe(self.absent,settlement_page='personal_result'))
        self.assertNotIn('end_detection',guard.report)

    def test_real_template_match_covers_shifted_and_dimmed_life_labels(self):
        template = np.full((21, 53, 3), 100, np.uint8)
        cv2.putText(template, "LIFE", (2, 16), cv2.FONT_HERSHEY_SIMPLEX, .55, (250, 250, 250), 1)
        navigator = Navigator.__new__(Navigator)
        navigator.templates = {"life_hud": template}
        navigator.threshold = .83
        guard = LiveEndGuard(navigator, {}, life_seen=False)
        for top in (13, 21, 29):
            for scale in (1.0, .4):
                with self.subTest(top=top, scale=scale):
                    frame = np.full((720, 1280, 3), 40, np.uint8)
                    frame[top:top + 21, 1008:1061] = (template * scale).astype(np.uint8)
                    self.assertTrue(guard.visible(frame))
        self.assertFalse(guard.visible(np.full((720, 1280, 3), 40, np.uint8)))
