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

    def test_visible_slide_checkpoint_uses_path_instead_of_placeholder_lane(self):
        points = (Point(0, 2, 3), Point(.5, 2, 3, 3, path_node=False), Point(1, 11, 3, 2))
        self.assertAlmostEqual(slide_x(points, .5), 640, places=3)
        self.assertAlmostEqual(slide_x(points, .25), 452.5, places=3)

    def test_visible_slide_checkpoint_does_not_split_hidden_node_easing(self):
        points = (Point(0, 5, 3, 1, 5), Point(.336, 5, 3, 3, path_node=False),
                  Point(.420, 2, 3, 5, 2), Point(.504, 5, 3, 3, path_node=False), Point(1.345, 5, 3, 2))
        self.assertAlmostEqual(slide_x(points, .336), 275, places=3)
        self.assertAlmostEqual(slide_x(points, .504), 267.0616, places=3)

    def test_sus_visible_relay_requires_attachment_to_be_judgement_only(self):
        rows = '#000320:13003300\n#0013b0:23'
        path_relay = parse_sus(HEADER + rows).first
        self.assertTrue(path_relay.points[1].path_node)
        attached = parse_sus(HEADER + rows + '\n#00012:00001300').first
        self.assertFalse(attached.points[1].path_node)
        self.assertAlmostEqual(slide_x(attached.points, 1), 640, places=3)

    def test_sus_directed_relay_remains_path_node_even_with_short_note(self):
        rows = '#000320:13003300\n#0013b0:23\n#00012:00001300\n#00052:00005300'
        directed = parse_sus(HEADER + rows).first
        self.assertTrue(directed.points[1].path_node)
        self.assertAlmostEqual(slide_x(directed.points, 1), directed.points[1].x, places=3)

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

    def test_slide_node_candidate_preserves_baseline_and_samples_fast_changes(self):
        def chain(high,low):
            return Gesture(tuple(Point(t,l,3,k,d) for t,l,k,d in [
                (101.2,high,1,0),(101.4,high,3,0),(101.4+1/1200,low,5,0),
                (101.6,low,3,0),(101.6+1/1200,high,5,0),(101.8,high,3,0),
                (101.8+1/1200,low,5,0),(102.,low,2,4)]),'slide',4)
        chart=Chart((chain(11,8),chain(5,2)),480,((0.,150.),))
        baseline=compile_touches(chart)
        self.assertEqual(baseline,compile_touches(chart,sample_slide_nodes=False))
        candidate=compile_touches(chart,sample_slide_nodes=True)
        extra=[event for event in candidate if event not in baseline]
        self.assertEqual(len(extra),6)
        self.assertTrue(all(event in candidate for event in baseline))
        self.assertEqual([event for event in baseline if event.kind!='move' or event.y!=570],
                         [event for event in candidate if event.kind!='move' or event.y!=570])
        validate_touches(candidate)
        self.assertEqual(len({event.contact for event in candidate if event.kind=='down'}),2)
        for gesture in chart.gestures:
            contact=next(event.contact for event in candidate if event.kind=='down' and event.x==round(gesture.points[0].x))
            for point in gesture.points[1:-1]:
                self.assertTrue(any(event.contact==contact and abs(event.time-point.time)<=1e-9
                    and event.x==round(slide_x(gesture.points,point.time)) for event in candidate))

    def test_slide_node_candidate_excludes_control_and_placeholder_and_deduplicates_grid(self):
        fixtures=[
            (Point(0,2,2),Point(.203,12,4,4),Point(1,2,3,2)),
            (Point(0,5,3,1,5),Point(.336,5,3,3,path_node=False),Point(.420,2,2,5,2),
             Point(.504,5,3,3,path_node=False),Point(1.345,5,3,2)),
            (Point(0,2,3),Point(.4+5e-10,5,3,5),Point(1,8,3,2))]
        for points in fixtures:
            with self.subTest(points=points):
                chart=Chart((Gesture(points,'slide'),),480,((0.,150.),))
                baseline=compile_touches(chart);candidate=compile_touches(chart,sample_slide_nodes=True)
                self.assertTrue(all(event in candidate for event in baseline))
                extra=[event for event in candidate if event not in baseline]
                self.assertFalse(any(event.time==point.time for event in extra for point in points
                    if not point.path_node or point.kind==4))
                self.assertTrue(all(event.x==round(slide_x(points,event.time)) for event in candidate if event.kind=='move'))
                if points is fixtures[-1]:self.assertEqual(extra,[])
                validate_touches(candidate)


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

    def test_first_trace_arrow_keeps_taller_outline_at_low_scroll_speed(self):
        gesture = Gesture((Point(0,7,1,5),),'trace')
        baseline = np.zeros((720,1280,3),np.uint8)
        frame = baseline.copy()
        center = round(640+(gesture.points[0].x-640)*196/570)
        # 低流速下先出现较小的完整箭头；外框包含三角顶，不能套用普通横条的长宽比。
        points = np.array([(center-14,196),(center-14,204),(center+14,204),(center+14,196),
                           (center+5,196),(center,186),(center-5,196)],np.int32)
        cv2.fillPoly(frame,[points],(0,255,0))
        next_center = round(640+(gesture.points[0].x-640)*110/570)
        cv2.rectangle(frame,(next_center-8,107),(next_center+8,113),(0,255,0),-1)
        self.assertGreater(first_note_y(frame,gesture,baseline),190)

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

    def test_native_anchor_keeps_perspective_when_early_models_differ_below_one_pixel(self):
        anchor = StartAnchor(Gesture((Point(0,10,4),),'tap'), minimum_samples=4)
        frame = np.zeros((720,1280,3),np.uint8)
        # 实际失败局在顶端五帧中两模型只差 0.29 像素，指数外推却早了约 47 毫秒。
        times = [10,10.0951079,10.1868247,10.2983601,10.3898068]
        with patch('project_sekai.chart_player.first_note_y',side_effect=[87.5,114,140.5,185,233.5]):
            anchor.observe(frame,9)
            for when in times[:-1]:
                self.assertIsNone(anchor.observe(frame,when,capture_seconds=.085))
            epoch = anchor.observe(frame,times[-1],capture_seconds=.085)
        self.assertIsNotNone(epoch)
        self.assertEqual(anchor.fit['model'],'perspective')
        self.assertLess(anchor.fit['residual_pixels'],4)
        self.assertAlmostEqual(epoch,10.795068,places=5)
        self.assertEqual(anchor.fit['selection'],'perspective_pixel_tie')

    def test_anchor_does_not_prefer_perspective_when_its_fit_is_distinctly_worse(self):
        anchor = StartAnchor(Gesture((Point(0,10,4),),'tap'), minimum_samples=4)
        frame = np.zeros((720,1280,3),np.uint8)
        with patch('project_sekai.chart_player.first_note_y',side_effect=[80,120,180,270]):
            anchor.observe(frame,9)
            for when in [10,10.2,10.4]:
                self.assertIsNone(anchor.observe(frame,when,capture_seconds=.05))
            self.assertIsNotNone(anchor.observe(frame,10.6,capture_seconds=.05))
        self.assertEqual(anchor.fit['model'],'exponential')
        self.assertEqual(anchor.fit['selection'],'minimum_residual')

    def test_large_first_note_near_judgement_is_not_replaced_by_its_successor(self):
        gesture = Gesture((Point(0,8,3),),'tap')
        baseline = np.zeros((720,1280,3),np.uint8)
        frame = baseline.copy()
        cv2.rectangle(frame,(641,438),(840,485),(255,130,65),-1)
        cv2.rectangle(frame,(641,253),(754,272),(255,130,65),-1)
        self.assertAlmostEqual(first_note_y(frame,gesture,baseline),462,delta=1)

    def test_critical_first_note_keeps_its_taller_gold_rim_before_the_successor(self):
        gesture = Gesture((Point(1,11,3,2),),'tap',critical=True)
        baseline = np.zeros((720,1280,3),np.uint8)
        frame = baseline.copy()
        cv2.rectangle(frame,(776,281),(928,320),(30,210,255),-1)
        cv2.rectangle(frame,(692,83),(745,98),(30,210,255),-1)
        self.assertAlmostEqual(first_note_y(frame,gesture,baseline),301,delta=1)

    def test_slide_ending_in_flick_still_anchors_on_its_green_head(self):
        gesture = Gesture((Point(2.5,2,6,1), Point(3.5,2,3,2)), 'slide', flick=1)
        baseline = np.zeros((720,1280,3),np.uint8)
        frame = baseline.copy()
        cv2.rectangle(frame,(476,192),(613,211),(80,255,30),-1)
        cv2.rectangle(frame,(579,70),(637,81),(130,45,255),-1)
        self.assertAlmostEqual(first_note_y(frame,gesture,baseline),202,delta=1)

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
