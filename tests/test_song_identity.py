from pathlib import Path
from types import SimpleNamespace
import hashlib
import tempfile
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from project_sekai.ocr import Reading
from project_sekai.song_identity import SongMatcher, write_image


class SongIdentityTests(unittest.TestCase):
    def matcher(self, scores):
        matcher = SongMatcher.__new__(SongMatcher)
        matcher.ids = [520, 90]
        matcher.features = np.array([[scores[0], 0], [scores[1], 0]])
        matcher.repository = SimpleNamespace(songs={
            520: {"title": "透明エレジー", "charts": {"easy": {"play_level": 6}}},
            90: {"title": "限りなく灰色へ", "charts": {"easy": {"play_level": 5}}}})
        matcher.ocr = Mock()
        return matcher

    def identify(self, matcher, title):
        with patch("project_sekai.song_identity.thumbnail", return_value=np.array([1, 0])):
            return matcher.identify(np.zeros((256, 256, 3), np.uint8), title, "prepare")

    def test_solo_title_can_confirm_a_song_when_the_cover_is_obscured(self):
        matcher = self.matcher((.514, .325))
        result = self.identify(matcher, Reading("透明エレジー", .99))
        self.assertEqual(result[0], 520)
        self.assertEqual(matcher.last_evidence["identified_by"], "title")
        self.assertEqual(matcher.last_evidence["cover"]["status"], "ambiguous")

    def test_cover_can_confirm_when_title_ocr_is_incomplete(self):
        matcher = self.matcher((.95, .45))
        self.assertEqual(self.identify(matcher, Reading("透明", .45))[0], 520)
        self.assertEqual(matcher.last_evidence["identified_by"], "cover")

    def test_conflicting_strong_cover_and_title_never_choose_either_song(self):
        matcher = self.matcher((.95, .45))
        with self.assertRaisesRegex(ValueError, "冲突"):
            self.identify(matcher, Reading("限りなく灰色へ", .99))
        self.assertNotIn("resolved_song_id", matcher.last_evidence)

    def test_duplicate_normalized_titles_cannot_identify_without_a_cover(self):
        matcher = self.matcher((.5, .49))
        matcher.repository.songs[90]["title"] = "透明 エレジー"
        with self.assertRaisesRegex(ValueError, "均未确认"):
            self.identify(matcher, Reading("透明エレジー", .99))

    def test_preparation_title_removes_cover_edge_but_keeps_voicing_marks(self):
        matcher = self.matcher((.5, .49))
        frame = np.full((42, 120, 3), (115, 90, 80), np.uint8)
        frame[:4, 15:105] = 255
        frame[16:36, 10:25] = 255
        frame[8:11, 19:22] = 255
        matcher.read_preparation_title(frame, (0, 0, 120, 42))
        processed = matcher.ocr.read.call_args.args[0]
        self.assertEqual(np.count_nonzero(processed[:, :, 0] == 0), 309)

    def test_cooperative_opening_records_identity_before_the_missing_chart_decision(self):
        matcher = self.matcher((.95, .45))
        matcher.repository.songs[520]["charts"] = {}
        matcher.read_opening_text = Mock(side_effect=[Reading("透明エレジー", .99), Reading("EASY", .99)])
        with patch("project_sekai.song_identity.thumbnail", return_value=np.array([1, 0])):
            actual = matcher.match(np.zeros((720, 1280, 3), np.uint8), "easy", "cooperative_final")
        self.assertEqual(actual.song_id, 520)
        self.assertFalse(matcher.last_evidence["chart_available"])
        self.assertEqual(actual.level, 0)

    def test_one_shot_opening_uses_task_difficulty_without_reading_the_badge(self):
        matcher = self.matcher((.95, .45))
        matcher.repository.songs[520]["charts"]["master"] = {"play_level": 31}
        matcher.read_opening_text = Mock(return_value=Reading("透明エレジー", .99))
        with patch("project_sekai.song_identity.thumbnail", return_value=np.array([1, 0])):
            actual = matcher.match(np.zeros((720, 1280, 3), np.uint8), "master", "one_shot_final")
        self.assertEqual(actual.difficulty, "master")
        self.assertEqual(actual.level, 31)
        matcher.read_opening_text.assert_called_once()
        self.assertEqual(matcher.last_evidence["difficulty"]["source"], "task_setting")

    def test_one_shot_identity_is_returned_even_when_selected_chart_is_missing(self):
        matcher = self.matcher((.95, .45))
        matcher.read_opening_text = Mock(return_value=Reading("透明エレジー", .99))
        with patch("project_sekai.song_identity.thumbnail", return_value=np.array([1, 0])):
            actual = matcher.match(np.zeros((720, 1280, 3), np.uint8), "master", "one_shot_final")
        self.assertEqual(actual.song_id, 520)
        self.assertFalse(matcher.last_evidence["chart_available"])

    def test_geometric_cover_matching_survives_the_rating_strip_and_rejects_a_fragment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rng = np.random.default_rng(24)
            reference = np.full((256, 256, 3), 175, np.uint8)
            for _ in range(90):
                point = tuple(int(value) for value in rng.integers(12, 244, 2))
                color = tuple(int(value) for value in rng.integers(0, 256, 3))
                cv2.circle(reference, point, int(rng.integers(3, 12)), color, -1)
            path = root / "cover.png"
            write_image(path, reference)
            repository = SimpleNamespace(root=root, songs={520: {
                "title": "透明エレジー", "charts": {}, "jacket": {
                    "path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}}})
            matcher = SongMatcher(repository, Mock())
            covered = reference.copy()
            covered[180:] = (175, 175, 175)
            actual = matcher.identify(covered, Reading("", 0), "cooperative_prepare")
            self.assertEqual(actual[0], 520)
            self.assertEqual(matcher.last_evidence["cover"]["method"], "features")
            fragment = np.zeros_like(reference)
            fragment[80:150, 80:150] = reference[80:150, 80:150]
            observed = cv2.AKAZE_create().detectAndCompute(cv2.cvtColor(fragment, cv2.COLOR_BGR2GRAY), None)
            self.assertFalse(matcher.registered_cover(520, observed)["confirmed"])


if __name__ == "__main__":
    unittest.main()
