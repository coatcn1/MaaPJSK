from dataclasses import replace
import math
import os
import unittest
from unittest.mock import patch

from project_sekai import chart_player as cp
from project_sekai.sus_chart import Chart, Gesture, Point
import test_chart_bound_head_tracker as tracker_tests


class GoldTapPairAnchorTests(unittest.TestCase):
    def setUp(self):
        self.delay = 1 / 3
        self.heads = tuple(Gesture((Point(0., lane, 4, 2),), "tap", critical=True) for lane in (3, 9))
        followers = tuple(replace(head, points=(replace(head.points[0], time=self.delay),)) for head in self.heads)
        blue_tap = Gesture((Point(.5, 6, 4),), "tap")
        self.chart = Chart((*self.heads, *followers, blue_tap), 480, ((0., 120.),))
        self.frame = tracker_tests.ChartBoundHeadTrackerTests().frame

    def anchor(self, chart=None, *, enabled=True, **env):
        chart = chart or self.chart
        flags = {"MAAPJSK_CHART_BOUND_HEAD_GUARD": "0", "MAAPJSK_GOLD_TAP_HEAD_GUARD": "1",
                 "MAAPJSK_GOLD_TAP_BLUE_CONTEXT_GUARD": "0",
                 "MAAPJSK_GOLD_TAP_FOLLOWERS_GUARD": "1"}
        flags.update(env)
        with patch.dict(os.environ, flags), patch.object(cp, "GOLD_TAP_PAIR_GUARD", enabled):
            return cp.StartAnchor(chart.first, minimum_samples=4, gold_tap_context_chart=chart)

    def replay(self, anchor, *, rear_delay=None):
        anchor.observe(self.frame(), 9.)
        for step in range(24):
            elapsed = step * .050
            front = round(90 * math.exp(1.5 * elapsed) - 36)
            heads = [(head, front) for head in self.heads]
            if rear_delay is not None and elapsed >= rear_delay:
                rear = round(90 * math.exp(1.5 * (elapsed - rear_delay)) - 36)
                heads.extend((head, rear) for head in self.heads)
            epoch = anchor.observe(self.frame(*heads), 10. + elapsed)
            if epoch is not None:
                return epoch
        return None

    def test_repeated_separated_first_pair_keeps_measuring_after_the_legacy_path_stalls(self):
        self.assertIsNone(self.replay(self.anchor(enabled=False), rear_delay=self.delay))
        anchor = self.anchor()
        epoch = self.replay(anchor, rear_delay=self.delay)
        self.assertAlmostEqual(epoch, 10. + math.log(600 / 90) / 1.5, delta=.025)
        self.assertTrue(anchor.fit["gold_tap_pair_guard"])
        self.assertTrue(anchor.fit["legacy_first_head_seed"])

    def test_first_pair_alone_or_with_the_wrong_repetition_cannot_publish_an_epoch(self):
        for rear_delay in (None, .52):
            with self.subTest(rear_delay=rear_delay):
                anchor = self.anchor()
                self.assertIsNone(self.replay(anchor, rear_delay=rear_delay))
                self.assertTrue(anchor.gold_tap_pair_guard)

    def test_later_matching_pair_interval_cannot_prove_first_head_identity(self):
        repeated_again = tuple(replace(head, points=(replace(head.points[0], time=2 * self.delay),))
                               for head in self.heads)
        chart = replace(self.chart, gestures=(*self.chart.gestures, *repeated_again))
        anchor = self.anchor(chart)
        self.assertTrue(anchor.gold_tap_pair_guard)
        self.assertIsNone(self.replay(anchor, rear_delay=self.delay))

    def test_requires_real_separated_pair_metadata_and_respects_main_guard(self):
        missing = replace(self.chart, gestures=(*self.chart.gestures[:3], self.chart.gestures[-1]))
        self.assertFalse(self.anchor(missing).gold_tap_pair_guard)
        self.assertFalse(self.anchor(enabled=False).gold_tap_pair_guard)
        self.assertFalse(self.anchor(**{"MAAPJSK_GOLD_TAP_HEAD_GUARD": "0"}).gold_tap_pair_guard)
        self.assertTrue(self.anchor().gold_tap_pair_guard)

    def test_passed_first_pair_rejects_reseeding_even_if_rear_pair_is_complete(self):
        anchor = self.anchor()
        anchor.observe(self.frame(), 9.)
        with self.assertRaises(cp.FirstHeadOutsideWindow):
            anchor.observe(self.frame(*((head, y) for head in self.heads for y in (532, 82))), 10.)
        with self.assertRaisesRegex(RuntimeError, "禁止由后继重新播种"):
            anchor.observe(self.frame(*((head, 91) for head in self.heads)), 10.05)
        self.assertEqual(anchor.samples, [])

    def test_separated_pair_with_single_gold_follower_and_green_slides(self):
        first, left = self.heads[::-1]
        rear = replace(first, points=(replace(first.points[0], time=self.delay),))

        def slide(head):
            point = head.points[0]
            return Gesture((replace(point, time=head.start + .001, kind=1),
                            replace(point, time=head.start + .2)), "slide")

        chart = replace(self.chart, gestures=(first, left, slide(left), rear, slide(rear)))
        later_opening = tuple(replace(head, points=(replace(head.points[0], time=head.start + 4),))
                              for head in (first, left, rear))
        ambiguous = replace(chart, gestures=(*chart.gestures, *later_opening))

        with patch.object(cp, "GOLD_TAP_SINGLE_FOLLOWER_GUARD", True):
            for score, rear_head, accepted in ((chart, rear, True), (chart, left, False),
                                               (chart, None, False), (ambiguous, rear, False)):
                with self.subTest(rear_head=rear_head, accepted=accepted):
                    anchor = self.anchor(score)
                    anchor.observe(self.frame(), 9.)
                    for step in range(24):
                        elapsed = step * .050
                        front = round(90 * math.exp(1.5 * elapsed) - 36)
                        heads = [(first, front), (left, front)]
                        if rear_head is not None and elapsed >= self.delay:
                            heads.append((rear_head, round(90 * math.exp(1.5 * (elapsed - self.delay)) - 36)))
                        epoch = anchor.observe(self.frame(*heads), 10. + elapsed)
                        if epoch is not None:
                            break
                    self.assertEqual(epoch is not None, accepted)
                    if accepted:
                        self.assertAlmostEqual(epoch, 10. + math.log(600 / 90) / 1.5, delta=.025)
                        self.assertTrue(anchor.fit["gold_tap_single_follower_guard"])
            invalid = replace(chart, gestures=(first, left, slide(left), rear, replace(slide(rear), critical=True)))
            self.assertFalse(self.anchor(invalid).gold_tap_single_follower_guard)
