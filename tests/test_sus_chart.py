from pathlib import Path
import unittest
from unittest.mock import patch
import cv2
import numpy as np

from project_sekai.chart_player import ChartPlayer, StartAnchor, compile_touches, first_note_y, validate_touches
from project_sekai.sus_chart import Chart, Gesture, Point, parse_sus, slide_x


HEADER = '#REQUEST "ticks_per_beat 480"\n#00002:4\n#BPM01:120\n#00008:01\n'


class SusTests(unittest.TestCase):
    def test_bpm_and_meter_changes_use_beats_not_measure_fraction(self):
        chart = parse_sus(HEADER + '#BPM02:240\n#00008:0002\n#00102:3\n#00112:14\n#00212:14')
        self.assertAlmostEqual(chart.gestures[0].start, 1.5)
        self.assertAlmostEqual(chart.gestures[1].start, 2.25)

    def test_slide_owns_overlapping_short_notes_and_end_flick(self):
        chart = parse_sus(HEADER + '#000320:14000000\n#000360:00340000\n#0003a0:00002400\n#00012:14000000\n#0001a:00002400\n#0005a:00003400')
        self.assertEqual(len(chart.gestures), 1)
        self.assertEqual(chart.first.kind, 'slide')
        self.assertEqual(chart.first.flick, 3)
        self.assertEqual(len(chart.first.points), 3)

    def test_trace_and_decorative_guides(self):
        chart = parse_sus(HEADER + '#00012:5400\n#000920:14002400\n#0001f:1100\n#00010:4100')
        self.assertEqual(len(chart.gestures), 1)
        self.assertEqual(chart.first.kind, 'trace')

    def test_critical_slide_retains_short_note_color(self):
        chart = parse_sus(HEADER + '#00012:24\n#000320:14\n#001320:24')
        self.assertTrue(chart.first.critical)

    def test_measure_base_and_wave_offset(self):
        chart = parse_sus(HEADER + '#WAVEOFFSET 1\n#MEASUREBS 2\n#00012:14')
        self.assertEqual(chart.first.start, 3)

    def test_invalid_sus_fails_before_any_touch(self):
        for suffix in ['#00012:1', '#00012:zz', '#000320:24', '#000320:14', '#00012:1d', '#00008:zz\n#00012:14']:
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                parse_sus(HEADER + suffix)

    def test_bezier_time_is_inverted_and_endpoints_preserved(self):
        points = (Point(0, 2, 2), Point(.2, 12, 2, 4), Point(1, 2, 2, 2))
        self.assertEqual(slide_x(points, 0), points[0].x)
        self.assertEqual(slide_x(points, 1), points[-1].x)
        self.assertGreater(slide_x(points, .4), points[0].x + 200)

    def test_twelve_lane_geometry_and_note_width(self):
        self.assertAlmostEqual(Point(0, 2, 1).x, 181.6666667)
        self.assertAlmostEqual(Point(0, 13, 1).x, 1098.3333333)
        self.assertEqual(Point(0, 2, 12).x, 640)

    def test_overlapping_slides_and_flicks_have_separate_contacts(self):
        chart = Chart((Gesture((Point(0, 2, 2), Point(1, 8, 2, 2)), 'slide'),
                       Gesture((Point(.5, 2, 2),), 'tap', 4)), 480, ((0, 120),))
        events = compile_touches(chart)
        validate_touches(events)
        downs = [event for event in events if event.kind == 'down']
        self.assertEqual(len({event.contact for event in downs}), 2)
        self.assertTrue(any(event.kind == 'move' and event.y < 570 for event in events))

    def test_more_than_ten_contacts_is_rejected_offline(self):
        chart = Chart(tuple(Gesture((Point(0, 2, 1),), 'tap') for _ in range(11)), 480, ((0, 120),))
        with self.assertRaises(ValueError):
            compile_touches(chart)


class Clock:
    def __init__(self): self.now = 0
    def __call__(self): return self.now
    def sleep(self, duration): self.now += duration


class Controller:
    def __init__(self): self.events = []; self.fail_move = False; self.stopped = False
    def post_touch_down(self, x, y, contact, pressure):
        self.events.append(('down', contact)); return self
    def post_touch_move(self, x, y, contact, pressure):
        self.events.append(('move', contact)); return self
    def post_touch_up(self, contact): self.events.append(('up', contact)); return self
    def wait(self): return self
    @property
    def succeeded(self): return not (self.fail_move and self.events[-1][0] == 'move')


class PlayerTests(unittest.TestCase):
    def test_start_anchor_ignores_static_cover_then_tracks_first_note(self):
        gesture = Gesture((Point(1, 8, 4, 5),), 'trace')
        baseline = np.zeros((720,1280,3),np.uint8)
        cv2.rectangle(baseline,(640,180),(760,188),(0,200,0),-1)
        anchor = StartAnchor(gesture)
        self.assertIsNone(anchor.observe(baseline,10))
        self.assertIsNone(anchor.observe(baseline,10.05))
        self.assertEqual(anchor.samples,[])
        epoch = None
        for index,y in enumerate([240,300,360]):
            frame = baseline.copy()
            center = 640 + (gesture.points[0].x-640)*y/570
            width = gesture.points[0].width*(1000/12)*y/570
            cv2.rectangle(frame,(round(center-width/2),y-4),(round(center+width/2),y+4),(0,255,0),-1)
            epoch=anchor.observe(frame,10.1+index*.1)
        self.assertIsNotNone(epoch)
        self.assertAlmostEqual(epoch,9.65,places=2)

    def test_first_trace_does_not_use_purple_judgement_line(self):
        gesture = Gesture((Point(0, 9, 4, 5),), 'trace')
        anchor = StartAnchor(gesture)
        baseline = np.zeros((720,1280,3),np.uint8)
        anchor.observe(baseline,10)
        frame = baseline.copy()
        cv2.rectangle(frame,(715,547),(1040,553),(220,65,155),-1)
        for when in [10.1,10.5,11,12.5]:
            self.assertIsNone(anchor.observe(frame,when))
        self.assertEqual(anchor.samples,[])

    def test_start_anchor_uses_perspective_motion_before_judgement(self):
        gesture = Gesture((Point(0,9,4,5),),'trace')
        anchor = StartAnchor(gesture)
        frame = np.zeros((720,1280,3),np.uint8)
        with patch('project_sekai.chart_player.first_note_y',side_effect=[51.5,125.5,264.0]):
            anchor.observe(frame,9)
            self.assertIsNone(anchor.observe(frame,10))
            self.assertIsNone(anchor.observe(frame,10.316))
            epoch=anchor.observe(frame,10.632)
        self.assertIsNotNone(epoch)
        self.assertEqual(anchor.fit['model'],'perspective')
        self.assertGreater(epoch,10.95)
        self.assertLess(epoch,11.01)

    def test_calibrated_perspective_can_sync_before_slow_third_screenshot(self):
        anchor = StartAnchor(Gesture((Point(0,10,4),),'tap'))
        frame = np.zeros((720,1280,3),np.uint8)
        with patch('project_sekai.chart_player.first_note_y',side_effect=[72.0,165.0]):
            anchor.observe(frame,9)
            self.assertIsNone(anchor.observe(frame,10))
            epoch=anchor.observe(frame,10.311)
        self.assertIsNotNone(epoch)
        self.assertEqual(anchor.fit['model'],'calibrated_perspective')
        self.assertGreater(epoch,10.83)
        self.assertLess(epoch,10.87)

    def test_native_anchor_waits_for_four_motion_samples_before_freezing_epoch(self):
        anchor = StartAnchor(Gesture((Point(0,10,4),),'tap'), minimum_samples=4)
        frame = np.zeros((720,1280,3),np.uint8)
        with patch('project_sekai.chart_player.first_note_y',side_effect=[97.0,146.5,210.0,299.0]):
            anchor.observe(frame,9)
            self.assertIsNone(anchor.observe(frame,10))
            self.assertIsNone(anchor.observe(frame,10.156))
            self.assertIsNone(anchor.observe(frame,10.304))
            epoch = anchor.observe(frame,10.460)
        self.assertIsNotNone(epoch)
        self.assertEqual(len(anchor.samples),4)
        self.assertNotEqual(anchor.fit['model'],'calibrated_perspective')
        self.assertLess(anchor.fit['residual_pixels'],4)

    def test_large_first_note_near_judgement_is_not_replaced_by_its_successor(self):
        gesture = Gesture((Point(0,8,3),),'tap')
        baseline = np.zeros((720,1280,3),np.uint8)
        frame = baseline.copy()
        cv2.rectangle(frame,(641,438),(840,485),(255,130,65),-1)
        cv2.rectangle(frame,(641,253),(754,272),(255,130,65),-1)
        self.assertAlmostEqual(first_note_y(frame,gesture,baseline),462,delta=1)

    def test_native_short_window_accepts_three_consistent_samples_before_first_note(self):
        anchor = StartAnchor(Gesture((Point(0,8,3),),'tap'),minimum_samples=4)
        frame = np.zeros((720,1280,3),np.uint8)
        with patch('project_sekai.chart_player.first_note_y',side_effect=[56.5,124.5,243.5]):
            anchor.observe(frame,9)
            self.assertIsNone(anchor.observe(frame,10,capture_seconds=.24))
            self.assertIsNone(anchor.observe(frame,10.282,capture_seconds=.24))
            epoch = anchor.observe(frame,10.562,capture_seconds=.24)
        self.assertIsNotNone(epoch)
        self.assertEqual(len(anchor.samples),3)
        self.assertTrue(anchor.fit['short_window'])
        self.assertLess(anchor.fit['residual_pixels'],4)
        self.assertGreater(epoch,10.562+.24+.08)

    def test_native_short_window_rejects_noisy_three_point_fit(self):
        anchor = StartAnchor(Gesture((Point(0,8,3),),'tap'),minimum_samples=4)
        frame = np.zeros((720,1280,3),np.uint8)
        with patch('project_sekai.chart_player.first_note_y',side_effect=[56.5,230.0,243.5]):
            anchor.observe(frame,9)
            anchor.observe(frame,10,capture_seconds=.24)
            anchor.observe(frame,10.282,capture_seconds=.24)
            self.assertIsNone(anchor.observe(frame,10.562,capture_seconds=.24))

    def test_unconfirmed_background_candidate_reseeds_when_first_note_enters_from_top(self):
        anchor = StartAnchor(Gesture((Point(0,9,4,5),),'trace'))
        frame = np.zeros((720,1280,3),np.uint8)
        with patch('project_sekai.chart_player.first_note_y',side_effect=[177.0,None,63.5,208.5]):
            anchor.observe(frame,9)
            self.assertIsNone(anchor.observe(frame,10))
            self.assertIsNone(anchor.observe(frame,10.4))
            self.assertIsNone(anchor.observe(frame,10.8))
            epoch=anchor.observe(frame,11.206)
        self.assertIsNotNone(epoch)
        self.assertEqual(anchor.samples,[(10.8,63.5),(11.206,208.5)])
        self.assertEqual(anchor.fit['model'],'calibrated_perspective')

    def test_first_note_reseed_cannot_replace_an_established_trajectory(self):
        anchor = StartAnchor(Gesture((Point(0,9,4,5),),'trace'))
        frame = np.zeros((720,1280,3),np.uint8)
        with patch('project_sekai.chart_player.first_note_y',side_effect=[60.0,100.0,40.0]):
            anchor.observe(frame,9)
            anchor.observe(frame,10)
            anchor.observe(frame,10.1)
            self.assertIsNone(anchor.observe(frame,10.2))
        self.assertEqual(anchor.samples,[(10,60.0),(10.1,100.0)])

    def test_scheduled_multitouch_and_offset(self):
        clock, controller = Clock(), Controller()
        chart = parse_sus(HEADER + '#00112:14\n#00118:14')
        events = compile_touches(chart)
        player = ChartPlayer(controller, lambda: False, clock=clock, sleeper=clock.sleep)
        stats = player.play(events, 1, 100)
        self.assertEqual(stats['sent_actions'], 4)
        self.assertTrue(stats['release_confirmed'])
        self.assertGreater(clock.now, 3.1)

    def test_screenshots_only_use_safe_idle_gaps_without_making_touches_late(self):
        clock, controller = Clock(), Controller()
        checks = []
        def observe():
            checks.append(clock.now)
            clock.sleep(.3)
        events = compile_touches(parse_sus(HEADER + '#00112:14\n#00218:14'))
        player = ChartPlayer(controller,lambda:False,clock=clock,sleeper=clock.sleep,idle_observer=observe)
        stats = player.play(events,0)
        self.assertTrue(checks)
        self.assertEqual(stats['sent_actions'],4)
        self.assertLess(stats['lateness_ms_max'],1)

    def test_continuous_hold_does_not_compete_with_screenshots(self):
        clock, controller = Clock(), Controller()
        checks = []
        events = compile_touches(parse_sus(HEADER + '#000320:14\n#002320:24'))
        player = ChartPlayer(controller,lambda:False,clock=clock,sleeper=clock.sleep,idle_observer=lambda:checks.append(clock.now))
        player.play(events,0)
        self.assertEqual(checks,[])

    def test_failure_releases_owned_contacts(self):
        clock, controller = Clock(), Controller()
        controller.fail_move = True
        chart = parse_sus(HEADER + '#00112:34')
        player = ChartPlayer(controller, lambda: False, clock=clock, sleeper=clock.sleep)
        with self.assertRaises(RuntimeError):
            player.play(compile_touches(chart), 1)
        self.assertEqual(controller.events[-1][0], 'up')
        self.assertFalse(player.active)

    def test_stop_during_wait_releases_without_more_downs(self):
        clock, controller = Clock(), Controller()
        chart = parse_sus(HEADER + '#000320:14\n#001320:24')
        player = ChartPlayer(controller, lambda: clock.now > .1, clock=clock, sleeper=clock.sleep)
        with self.assertRaises(InterruptedError):
            player.play(compile_touches(chart), 0)
        self.assertEqual(controller.events[-1][0], 'up')
        self.assertEqual(sum(event[0] == 'down' for event in controller.events), 1)

    def test_late_start_does_not_fire_backlog(self):
        controller, clock = Controller(), Clock()
        clock.now = 5
        player = ChartPlayer(controller, lambda: False, clock=clock, sleeper=clock.sleep)
        with self.assertRaises(RuntimeError):
            player.play(compile_touches(parse_sus(HEADER + '#00112:14')), 0)
        self.assertEqual(controller.events, [])
