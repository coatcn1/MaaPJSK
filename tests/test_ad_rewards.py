import json
import hashlib
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from agent import ad_rewards as agent
from project_sekai.ad_rewards import AdRewardPages, AdRewards, REWARD_POINT, WATCH_POINT, loading_spinner
from project_sekai.ocr import Reading
from project_sekai.navigator import Navigator
from project_sekai.song_identity import read_image
from project_sekai import diagnostics

ROOT = Path(__file__).resolve().parents[1]


class SimulatedAdDevice:
    def __init__(self, clock, *, returns=True, popups=True, max_popup_starts=None, card_counts=(4, 3, 1),
                 world_map=False, ignored_world_requests=0, return_sequence=None):
        self.clock, self.state, self.events = clock, 'home', []
        self.returns, self.popups = returns, popups
        self.max_popup_starts = max_popup_starts
        self.card_counts, self.card_count = tuple(card_counts), card_counts[0]
        self.starts, self.returned, self.screenshots = 0, 0, 0
        self.screens_before_taps, self.fixed_click_observations = [], []
        self.last_reward_seen = None
        self.return_sequence = return_sequence
        self.preflight = Mock()
        self.shell = Mock(side_effect=self._shell)
        self.world_map, self.ignored_world_requests = world_map, ignored_world_requests
        self.world_requests = 0

    def _shell(self, command):
        self.events.append(('shell', command, self.clock.now))
        if command.startswith('monkey '):
            self.state = 'home'
        return ''

    def screenshot(self):
        self.screenshots += 1
        frame = np.full((720, 1280, 3), 255, np.uint8)
        frame[0, 0] = self.card_count
        return frame

    def observe(self, frame):
        if self.state == 'rewards':
            self.last_reward_seen = self.clock.now
        return {'state': self.state,
                **({'target': [223, 435]} if self.state == 'map' else {}),
                **({'target': [1174, 627]} if self.state == 'world_map' else {}),
                **({'target': [1152, 190]} if self.state == 'street_cm' else {})}

    def tap(self, x, y):
        self.events.append(('tap', (x, y), self.clock.now))
        self.screens_before_taps.append(((x, y), self.screenshots))
        if (x, y) == (42, 42):
            self.state = 'world_map' if self.world_map else 'map'
        elif (x, y) == (1174, 627):
            if self.state != 'world_map':
                raise AssertionError('世界地图输入前必须是新帧明确世界地图')
            self.world_requests += 1
            if self.world_requests > self.ignored_world_requests:
                self.state = 'map'
        elif (x, y) == (223, 435):
            self.state = 'street'
        elif (x, y) == (1152, 190):
            self.state = 'rewards'
        elif (x, y) == REWARD_POINT:
            self.fixed_click_observations.append((self.card_count, self.clock.now, self.last_reward_seen))
            if self.popups and (self.max_popup_starts is None or self.starts < self.max_popup_starts):
                self.state = 'watch_confirm'
        elif (x, y) == WATCH_POINT:
            if self.state != 'watch_confirm':
                raise AssertionError('未知页不得点击观看开始')
            self.starts += 1
            self.state = 'unknown'
        else:
            raise AssertionError('未经授权的广告页面点击')

    def swipe(self, *args):
        self.events.append(('swipe', args, self.clock.now))
        self.state = 'street_cm'

    def back(self):
        self.events.append(('back', (), self.clock.now))
        if self.state == 'rewards':
            raise AssertionError('已返奖励页不能再发送 BACK')
        returns = self.returns if self.return_sequence is None else self.return_sequence[self.starts - 1]
        if returns:
            self.state = 'rewards'
            self.returned += 1
            self.card_count = self.card_counts[min(self.returned, len(self.card_counts) - 1)]


class AdRewardsTests(unittest.TestCase):
    def assert_agent_completion(self, report, completed):
        context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
        with patch.object(agent, 'create_workflow') as create, \
                patch('project_sekai.diagnostics.load_policy'), patch.object(agent, 'visible_log') as visible:
            create.return_value.run.return_value = report
            self.assertIs(agent.ProjectSekaiAdRewards().run(context, SimpleNamespace()), completed)
            if completed:
                visible.assert_not_called()
            else:
                self.assertIn(report['error'], visible.call_args.args[1])

    def confirmation_pages(self, question):
        pages = AdRewardPages.__new__(AdRewardPages)
        pages.threshold = .9
        pages.navigator = None
        pages.ocr = Mock()
        pages.ocr.read.side_effect = AssertionError('观看确认不得使用 OCR')
        boxes = {'ad_watch_question': (459, 242, 821, 270),
                 'ad_watch_bonus_question': (408, 245, 872, 295),
                 'ad_watch_start': (714, 528, 808, 557),
                 'ad_rewards': (139, 312, 358, 344)}
        rng = np.random.default_rng(42)
        pages.anchors = {name: (rng.integers(0, 256, (y2 - y1, x2 - x1, 3), dtype=np.uint8), box)
                         for name, box in boxes.items() for x1, y1, x2, y2 in [box]}
        frame = np.full((720, 1280, 3), 255, np.uint8)
        for name in (question, 'ad_watch_start'):
            template, (x1, y1, x2, y2) = pages.anchors[name]
            frame[y1:y2, x1:x2] = template
        return pages, frame

    def workflow(self, root, **options):
        clock = SimpleNamespace(now=0.)
        device = SimulatedAdDevice(clock, **options)
        workflow = AdRewards(device, device, root, log_message=Mock(), clock=lambda: clock.now,
                             sleeper=lambda seconds: setattr(clock, 'now', clock.now + seconds))
        workflow.game_login = Mock()
        workflow.game_login.run.return_value = {'completed': True, 'phase': 'already_running'}
        return workflow, device, clock

    def test_reordered_four_three_one_cards_always_use_fixed_top_left_and_stop_on_no_popup(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, device, _ = self.workflow(Path(directory), max_popup_starts=3)
            report = workflow.run()
        self.assertEqual([card for card, _, _ in device.fixed_click_observations[:3]], [4, 3, 1])
        self.assertEqual(report['started_ads'], 3)
        self.assertEqual(report['normal_returns'], 3)
        self.assertIsNone(report['completed_ads'])
        self.assertIsNone(report['daily_exhausted'])
        self.assertEqual(report['finish_reason'], 'fixed_position_no_confirmation')
        self.assertIs(report['completed'], False)
        self.assertIn('观看确认', report['error'])
        self.assertTrue(all(event[1] in {REWARD_POINT, WATCH_POINT, (42, 42), (223, 435), (1152, 190)}
                            for event in device.events if event[0] == 'tap'))
        for _, clicked_at, observed_at in device.fixed_click_observations[1:3]:
            self.assertEqual(clicked_at, observed_at)

    def test_initial_reward_page_one_frame_immediately_clicks_fixed_point(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, device, _ = self.workflow(Path(directory), max_popup_starts=1)
            device.state = 'rewards'
            report = workflow.run()
        self.assertEqual(device.screens_before_taps[0], (REWARD_POINT, 1))
        self.assertEqual(report['started_ads'], 1)
        self.assertFalse(any(event[:2] == ('tap', (42, 42)) for event in device.events))

    def test_no_popup_has_three_fixed_clicks_with_short_wait_and_no_false_exhaustion(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, device, clock = self.workflow(Path(directory), popups=False)
            device.state = 'rewards'
            report = workflow.run()
        self.assertEqual(len(device.events), 3)
        for event, expected_at in zip(device.events, (0., 2., 4.)):
            self.assertEqual(event[:2], ('tap', REWARD_POINT))
            self.assertAlmostEqual(event[2], expected_at, places=5)
        self.assertLessEqual(clock.now, 6.1)
        self.assertEqual(report['started_ads'], 0)
        self.assertEqual(report['finish_reason'], 'fixed_position_no_confirmation')
        self.assertIs(report['completed'], False)
        self.assert_agent_completion(report, False)
        self.assertIsNone(report['rewards_claimed'])
        self.assertIsNone(report['daily_exhausted'])
        workflow.log.assert_called_with('固定位置未弹出观看确认，结束本次点击循环')

    def test_seven_start_request_limit_does_not_claim_seven_rewards(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, device, _ = self.workflow(Path(directory))
            report = workflow.run()
        self.assertEqual(report['started_ads'], 7)
        self.assertEqual(report['normal_returns'], 7)
        self.assertEqual(report['finish_reason'], 'start_request_limit')
        self.assertIs(report['completed'], True)
        self.assertIsNone(report['completed_ads'])
        self.assertEqual(len([e for e in device.events if e[:2] == ('tap', WATCH_POINT)]), 7)
        self.assert_agent_completion(report, True)

    def test_back_waits_ten_seconds_and_third_restart_recovers_then_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, device, _ = self.workflow(Path(directory), returns=False)
            report = workflow.run()
        starts = [e[2] for e in device.events if e[:2] == ('tap', WATCH_POINT)]
        stops = [e[2] for e in device.events if e[0] == 'shell' and e[1].startswith('am force-stop')]
        backs = [e[2] for e in device.events if e[0] == 'back']
        self.assertEqual(len(starts), 3)
        self.assertEqual(len(stops), 3)
        self.assertGreaterEqual(backs[0] - starts[0], 10.)
        for started_at, stopped_at in zip(starts, stops):
            self.assertAlmostEqual(stopped_at - started_at, 100., places=5)
            attempt_backs = [at for at in backs if started_at <= at < stopped_at]
            self.assertEqual(len(attempt_backs), 90)
            self.assertAlmostEqual(attempt_backs[0] - started_at, 10., places=5)
            self.assertTrue(all(abs(later - earlier - 1.) < 1e-6
                                for earlier, later in zip(attempt_backs, attempt_backs[1:])))
        self.assertEqual(report['restart_requests'], 3)
        self.assertEqual(report['finish_reason'], 'consecutive_restart_limit')
        self.assertIs(report['completed'], False)
        self.assertIn('连续三次', report['error'])
        self.assertEqual(device.state, 'rewards')
        self.assertIsNone(report['completed_ads'])
        self.assert_agent_completion(report, False)

    def test_return_after_old_twenty_second_limit_keeps_back_and_avoids_restart(self):
        for return_after in (21., 89., 90.):
            with self.subTest(return_after=return_after), tempfile.TemporaryDirectory() as directory:
                workflow, device, clock = self.workflow(Path(directory), returns=False, max_popup_starts=1)
                original_observe = device.observe
                watching_since = None

                def observe(frame):
                    nonlocal watching_since
                    if device.state == 'unknown' and device.starts:
                        if watching_since is None:
                            watching_since = clock.now
                        if clock.now - watching_since >= return_after - 1e-6:
                            device.state = 'rewards'
                    return original_observe(frame)

                device.observe = observe
                report = workflow.run()
                backs = [event[2] for event in device.events if event[0] == 'back']
                self.assertEqual(report['normal_returns'], 1)
                self.assertEqual(report['restart_requests'], 0)
                self.assertEqual(len(backs), int(return_after))
                self.assertLess(backs[-1], watching_since + return_after)
                device.shell.assert_not_called()

    def test_slow_observation_retains_back_hard_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, device, clock = self.workflow(Path(directory), returns=False, max_popup_starts=1)
            original_observe = device.observe

            def observe(frame):
                if device.state == 'unknown' and device.starts:
                    clock.now += 120.
                return original_observe(frame)

            device.observe = observe
            report = workflow.run()
        self.assertTrue(report['ads'][0]['hard_timeout_recovery'])
        self.assertEqual(report['ads'][0]['back_requests'], 0)
        self.assertEqual(report['restart_requests'], 1)
        started_at = next(e[2] for e in device.events if e[:2] == ('tap', WATCH_POINT))
        stopped_at = next(e[2] for e in device.events if e[0] == 'shell')
        self.assertAlmostEqual(stopped_at - started_at, 130., places=5)

    def test_normal_return_resets_consecutive_restart_counter(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, _, _ = self.workflow(Path(directory), max_popup_starts=6,
                return_sequence=[False, False, True, False, False, False])
            report = workflow.run()
        self.assertEqual(report['started_ads'], 6)
        self.assertEqual(report['normal_returns'], 1)
        self.assertEqual(report['restart_requests'], 5)
        self.assertEqual(report['consecutive_restarts'], 3)

    def test_stop_interrupts_no_popup_ten_second_wait_and_back_loop(self):
        for stop_after, popups in [(1., False), (5., True), (14., True), (99., True)]:
            with self.subTest(stop_after=stop_after), tempfile.TemporaryDirectory() as directory:
                workflow, device, clock = self.workflow(Path(directory), returns=False, popups=popups)
                device.state = 'rewards'
                workflow.stop_requested = lambda: clock.now >= stop_after
                with self.assertRaises(InterruptedError):
                    workflow.run()
                self.assertLessEqual(clock.now, stop_after + .1)
                self.assertTrue(workflow.report['cancelled'])
                self.assertFalse(any(e[2] >= stop_after for e in device.events))

    def test_manual_unknown_ad_does_not_get_fixed_click_or_force_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, device, _ = self.workflow(Path(directory))
            device.state = 'unknown'
            with self.assertRaises(TimeoutError):
                workflow.run()
        self.assertEqual(device.events, [])

    def test_first_fixed_click_changing_page_to_unknown_never_gets_followup_clicks(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, device, _ = self.workflow(Path(directory), popups=False)
            device.state = 'rewards'
            tap = device.tap
            def changed_page(x, y):
                tap(x, y)
                device.state = 'unknown'
            device.tap = changed_page
            report = workflow.run()
        self.assertEqual(device.events, [('tap', REWARD_POINT, 0.)])
        self.assertEqual(report['started_ads'], 0)
        self.assertEqual(report['finish_reason'], 'fixed_position_no_confirmation')
        device.shell.assert_not_called()

    def test_rewards_and_confirmation_do_not_use_ocr(self):
        pages = AdRewardPages.__new__(AdRewardPages)
        pages.ocr = Mock()
        pages.ocr.read.side_effect = AssertionError('奖励与确认不得读取 OCR')
        frame = np.full((720, 1280, 3), 255, np.uint8)
        pages.matches = lambda image, name: name == 'ad_rewards'
        self.assertEqual(pages.observe(frame), {'state': 'rewards'})
        pages.matches = lambda image, name: name in {'ad_watch_question', 'ad_watch_start'}
        self.assertEqual(pages.observe(frame), {'state': 'watch_confirm'})
        pages.ocr.read.assert_not_called()

    def test_old_and_bonus_questions_both_require_start_button_without_ocr(self):
        for question in ('ad_watch_question', 'ad_watch_bonus_question'):
            for has_question, has_button in ((True, True), (True, False), (False, True), (False, False)):
                with self.subTest(question=question, has_question=has_question, has_button=has_button):
                    pages, frame = self.confirmation_pages(question)
                    for name, present in ((question, has_question), ('ad_watch_start', has_button)):
                        if not present:
                            _, (x1, y1, x2, y2) = pages.anchors[name]
                            frame[y1:y2, x1:x2] = 255
                    self.assertEqual(pages.observe(frame)['state'],
                                     'watch_confirm' if has_question and has_button else 'unknown')
                    pages.ocr.read.assert_not_called()
                    self.assertEqual(pages.threshold, .9)

    def test_old_template_config_without_optional_bonus_question_still_confirms(self):
        pages, frame = self.confirmation_pages('ad_watch_question')
        del pages.anchors['ad_watch_bonus_question']
        self.assertEqual(pages.observe(frame), {'state': 'watch_confirm'})
        frame[242:270, 459:821] = 255
        self.assertEqual(pages.observe(frame), {'state': 'unknown'})

    def test_bonus_question_start_ad_clicks_once_and_rechecks_changed_page(self):
        for changed_before_click in (False, True):
            with self.subTest(changed_before_click=changed_before_click), tempfile.TemporaryDirectory() as directory:
                workflow, device, _ = self.workflow(Path(directory), max_popup_starts=1)
                device.state = 'rewards'
                pages, confirm = self.confirmation_pages('ad_watch_bonus_question')
                original_observe = device.observe
                confirmation_observations = 0

                def observe(frame):
                    nonlocal confirmation_observations
                    if device.state == 'watch_confirm':
                        confirmation_observations += 1
                        if changed_before_click and confirmation_observations == 2:
                            device.state = 'unknown'
                        else:
                            return pages.observe(confirm)
                    return original_observe(frame)

                workflow.pages = SimpleNamespace(observe=observe)
                if changed_before_click:
                    with self.assertRaisesRegex(RuntimeError, '页面已改变'):
                        workflow.run()
                    self.assertEqual(device.starts, 0)
                    self.assertFalse(any(event[:2] == ('tap', WATCH_POINT) for event in device.events))
                else:
                    report = workflow.run()
                    self.assertEqual(device.starts, 1)
                    self.assertEqual(report['started_ads'], 1)
                    self.assertEqual(len([event for event in device.events if event[:2] == ('tap', WATCH_POINT)]), 1)
                pages.ocr.read.assert_not_called()

    def test_missing_question_or_button_never_clicks_start(self):
        for missing in ('ad_watch_bonus_question', 'ad_watch_start'):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                workflow, device, _ = self.workflow(Path(directory))
                device.state = 'rewards'
                pages, frame = self.confirmation_pages('ad_watch_bonus_question')
                _, (x1, y1, x2, y2) = pages.anchors[missing]
                frame[y1:y2, x1:x2] = 255
                original_observe = device.observe
                workflow.pages = SimpleNamespace(observe=lambda image: pages.observe(frame)
                    if device.state == 'watch_confirm' else original_observe(image))
                report = workflow.run()
                self.assertEqual(report['started_ads'], 0)
                self.assertIs(report['completed'], False)
                self.assertEqual(device.events, [('tap', REWARD_POINT, 0.)])

    def test_optional_template_builder_skips_only_missing_optional_capture(self):
        module_spec = importlib.util.spec_from_file_location('ad_template_builder', ROOT / 'scripts/build-ad-rewards-templates.py')
        builder = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(builder)
        for optional, exists, valid_image in ((True, False, False), (False, False, False),
                                             (True, True, False), (True, True, True)):
            with self.subTest(optional=optional, exists=exists, valid_image=valid_image), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                spec = {'screen': [1280, 720], 'threshold': .9, 'regions': {
                    'ad_watch_bonus_question': {'capture': 'bonus.png', 'box': [408, 245, 872, 295], 'optional': optional}}}
                manifest = root / 'manifest.json'
                manifest.write_text(json.dumps(spec), encoding='utf-8')
                if exists:
                    if valid_image:
                        self.assertTrue(cv2.imwrite(str(root / 'bonus.png'), np.full((720, 1280, 3), 255, np.uint8)))
                    else:
                        (root / 'bonus.png').write_bytes(b'broken image')
                if (not exists and not optional) or (exists and not valid_image):
                    with self.assertRaises((FileNotFoundError, ValueError, RuntimeError)):
                        builder.build(root, root / 'output', manifest)
                else:
                    builder.build(root, root / 'output', manifest)
                    config = json.loads((root / 'output/config.json').read_text(encoding='utf-8'))
                    if exists:
                        anchor = config['anchors']['ad_watch_bonus_question']
                        self.assertEqual(anchor['sha256'], hashlib.sha256((root / 'output' / anchor['path']).read_bytes()).hexdigest())
                        self.assertEqual(anchor['source_sha256'], hashlib.sha256((root / 'bonus.png').read_bytes()).hexdigest())
                    else:
                        self.assertEqual(config['anchors'], {})

    def test_optional_bonus_template_corrupt_hash_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'bonus.png').write_bytes(b'corrupted')
            (root / 'config.json').write_text(json.dumps({'screen': [1280, 720], 'threshold': .9,
                'anchors': {'ad_watch_bonus_question': {'path': 'bonus.png', 'box': [408, 245, 872, 295],
                    'sha256': hashlib.sha256(b'original').hexdigest()}}}), encoding='utf-8')
            with patch('project_sekai.ad_rewards.LineOcr') as ocr:
                with self.assertRaisesRegex(ValueError, '哈希不符.*ad_watch_bonus_question'):
                    AdRewardPages(root / 'config.json', root / 'unused')
                ocr.assert_not_called()

    @unittest.skipUnless((ROOT / '.local/ad-rewards-templates/config.json').is_file()
                         and (ROOT / '.local/mfa-generic/config/maapjsk-templates.json').is_file(),
                         '本机原始奖励页回放，图片保持忽略')
    def test_real_five_and_four_card_pages_are_single_frame_rewards_without_ocr(self):
        navigator = Navigator(None, ROOT / '.local/mfa-generic/config/maapjsk-templates.json')
        pages = AdRewardPages(ROOT / '.local/ad-rewards-templates/config.json',
                              ROOT / 'resource/models/song_title_ocr', navigator=navigator)
        pages.ocr = Mock()
        pages.ocr.read.side_effect = AssertionError('奖励页点击之前不得调用次数 OCR')
        paths = [ROOT / '.local/ad-rewards-20261007/reward-list.png',
                 ROOT / '.local/mfa-generic/debug/ad-rewards-runs/20261007-233732-f12182e9/failure.png',
                 ROOT / '.local/ad-rewards-20261007/watch-confirm.png']
        for path in paths:
            with self.subTest(path=path.name):
                if not path.is_file():
                    self.skipTest(f"可选本机历史截图已清理：{path.name}")
                expected = 'watch_confirm' if path.name == 'watch-confirm.png' else 'rewards'
                self.assertEqual(pages.observe(read_image(path)), {'state': expected})
        pages.ocr.read.assert_not_called()

    def test_world_map_first_receipt_without_switch_retries_then_uses_actual_map(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, device, clock = self.workflow(Path(directory), popups=False,
                                                 world_map=True, ignored_world_requests=1)
            persist = workflow.persist
            delayed = False
            def slow_first_request_persistence():
                nonlocal delayed
                if (workflow.report['navigation'] and not delayed
                        and workflow.report['navigation'][-1].get('world_map_request') == 1):
                    delayed = True
                    clock.now += .8
                persist()
            workflow.persist = slow_first_request_persistence
            report = workflow.run()
        switches = [event for event in device.events if event[:2] == ('tap', (1174, 627))]
        self.assertEqual(len(switches), 2)
        self.assertGreaterEqual(switches[1][2] - switches[0][2], 1.)
        self.assertGreaterEqual(switches[0][2], .4)
        self.assertFalse(report['completed'])


    def test_world_map_unchanged_page_consumes_only_three_requests_then_times_out(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, device, _ = self.workflow(Path(directory), world_map=True, ignored_world_requests=99)
            with self.assertRaisesRegex(TimeoutError, '世界地图三次请求'):
                workflow.run()
        self.assertEqual(device.world_requests, 3)
        self.assertEqual(device.state, 'world_map')
        self.assertFalse(any(event[:2] == ('tap', (223, 435)) for event in device.events))


    def test_world_map_unknown_transition_does_not_receive_retry_input(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, device, _ = self.workflow(Path(directory), popups=False,
                                                 world_map=True, ignored_world_requests=1)
            observe, tap = device.observe, device.tap
            unknown_frames = 0
            def transient_observe(frame):
                nonlocal unknown_frames
                if unknown_frames > 0:
                    unknown_frames -= 1
                    return {'state': 'unknown'}
                return observe(frame)
            def request(x, y):
                nonlocal unknown_frames
                self.assertEqual(unknown_frames, 0)
                tap(x, y)
                if device.world_requests == 1 and (x, y) == (1174, 627):
                    unknown_frames = 3
            device.observe, device.tap = transient_observe, request
            report = workflow.run()
        self.assertFalse(report['completed'])
        self.assertEqual(device.world_requests, 2)


    def test_dynamic_map_and_phone_targets_reject_duplicate_candidates(self):
        pages = AdRewardPages.__new__(AdRewardPages)
        pages.threshold = .9
        template = np.random.default_rng(27).integers(0, 256, (23, 60, 3), dtype=np.uint8)
        pages.anchors = {'target': (template, (0, 0, 60, 23))}
        frame = np.zeros((720, 1280, 3), np.uint8)
        frame[180:203, 650:710] = template
        self.assertEqual(pages.unique_target(frame, 'target', (0, 100, 1280, 290))[0], [680, 191])
        frame[150:173, 100:160] = template
        self.assertIsNone(pages.unique_target(frame, 'target', (0, 100, 1280, 290)))


    def test_cm_two_scales_reject_different_phone_objects(self):
        pages = AdRewardPages.__new__(AdRewardPages)
        pages.threshold = .9
        template = np.random.default_rng(31).integers(0, 256, (31, 50, 3), dtype=np.uint8)
        pages.anchors = {'ad_cm': (template, (0, 0, 50, 31))}
        frame = np.zeros((720, 1280, 3), np.uint8)
        small = cv2.resize(template, (45, 28), interpolation=cv2.INTER_LINEAR)
        frame[185:213, 393:438] = small
        self.assertEqual(pages.cm_target(frame)[0], [415, 199])
        frame[150:181, 800:850] = template
        self.assertIsNone(pages.cm_target(frame))


    def test_two_scale_matches_of_same_phone_are_merged_without_lowering_threshold(self):
        pages = AdRewardPages.__new__(AdRewardPages)
        pages.threshold = .9
        pages.anchors = {'ad_cm': (np.zeros((31, 50, 3), np.uint8), (0, 0, 50, 31))}
        full, small = np.zeros((180, 1200), np.float32), np.zeros((180, 1200), np.float32)
        full[80, 300], small[80, 301] = .96, .95
        with patch('project_sekai.ad_rewards.cv2.matchTemplate', side_effect=[full, small]):
            self.assertEqual(pages.cm_target(np.zeros((720, 1280, 3), np.uint8))[0], [325, 195])
        self.assertEqual(pages.threshold, .9)


    def test_phone_core_with_non_cm_caption_cannot_become_clickable_cm(self):
        pages = AdRewardPages.__new__(AdRewardPages)
        pages.threshold = .9
        template = np.random.default_rng(34).integers(0, 256, (31, 50, 3), dtype=np.uint8)
        pages.anchors = {'ad_cm': (template, (0, 0, 50, 31))}
        pages.matches = lambda frame, name: name == 'ad_home'
        pages.navigator = SimpleNamespace(threshold=.83, templates={'home': None, 'life_hud': None, 'playing': None},
                                          match=lambda frame, name: (float(name == 'home'), (0, 0)))
        pages.ocr = Mock()
        pages.ocr.read.return_value = Reading('SHOP', .99)
        frame = np.zeros((720, 1280, 3), np.uint8)
        frame[150:181, 800:850] = template
        self.assertEqual(pages.observe(frame)['state'], 'home')


    def test_template_corruption_is_rejected_before_loading_ocr_or_device_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'template.png').write_bytes(b'corrupted')
            (root / 'config.json').write_text(json.dumps({'screen': [1280, 720], 'threshold': .9,
                'anchors': {'ad_home': {'path': 'template.png', 'box': [0, 0, 20, 20],
                                        'sha256': hashlib.sha256(b'original').hexdigest()}}}))
            with self.assertRaisesRegex(ValueError, '哈希不符'):
                AdRewardPages(root / 'config.json', root / 'unused')


    @unittest.skipUnless((ROOT / '.local/ad-rewards-templates/config.json').is_file()
                         and (ROOT / '.local/mfa-generic/config/maapjsk-templates.json').is_file(),
                         '仅本机保存帧回归，截图与模板保持忽略')
    def test_saved_home_map_phone_and_loading_frames_replay_actual_detection(self):
        navigator = Navigator(None, ROOT / '.local/mfa-generic/config/maapjsk-templates.json')
        pages = AdRewardPages(ROOT / '.local/ad-rewards-templates/config.json',
                              ROOT / 'resource/models/song_title_ocr', navigator=navigator)
        captures = ROOT / '.local/ad-rewards-20261007'
        for name, expected in [('home-initial', 'home'), ('restarted-home-clear', 'home'),
                               ('scramble-start', 'street'), ('restarted-home-ready', 'unknown'),
                               ('restarted-home', 'unknown'), ('first-ad-after10', 'unknown'),
                               ('restarted-map', 'world_map'), ('restarted-real-map', 'map')]:
            with self.subTest(name=name):
                self.assertEqual(pages.observe(read_image(captures / f'{name}.png'))['state'], expected)
        for name, x in [('scramble-right-1', 1152), ('restarted-street-right', 1224), ('restarted-street-right-2', 668)]:
            with self.subTest(name=name):
                state = pages.observe(read_image(captures / f'{name}.png'))
                self.assertEqual(state['state'], 'street_cm')
                self.assertAlmostEqual(state['target'][0], x, delta=3)
        self.assertTrue(loading_spinner(read_image(captures / 'first-ad-after10.png')))
        self.assertEqual(pages.observe(read_image(captures / 'reward-after-restart.png')), {'state': 'rewards'})
        failure = ROOT / '.local/mfa-generic/debug/ad-rewards-runs/20261007-213416-72bc2802/failure.png'
        if failure.is_file():
            state = pages.observe(read_image(failure))
            self.assertEqual(state['state'], 'world_map')
            self.assertAlmostEqual(state['target'][0], 1170, delta=5)
        small_phone = ROOT / '.local/mfa-generic/debug/ad-rewards-runs/20261007-215225-e51a49da/failure.png'
        if small_phone.is_file():
            state = pages.observe(read_image(small_phone))
            self.assertEqual(state['state'], 'street_cm')
            self.assertAlmostEqual(state['target'][0], 416, delta=3)
            self.assertAlmostEqual(state['target'][1], 203, delta=3)
        for name in ('first-ad-loop-final', 'first-ad-end-back'):
            self.assertEqual(pages.observe(read_image(captures / f'{name}.png'))['state'], 'unknown')


    def test_new_entry_defaults_off_and_pipeline_agent_names_resolve(self):
        interface = json.loads((ROOT / 'interface.json').read_text(encoding='utf-8'))
        task = next(task for task in interface['task'] if task['name'] == 'AdRewards')
        self.assertFalse(task['default_check'])
        self.assertEqual(task['option'], [])
        pipeline = json.loads((ROOT / 'resource/pipeline/ad_rewards.json').read_text(encoding='utf-8'))
        self.assertEqual(pipeline[task['entry']]['custom_action'], 'ProjectSekaiAdRewards')
        context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
        with patch.object(agent, 'create_workflow') as create:
            create.return_value.run.return_value = {'completed': True}
            self.assertTrue(agent.ProjectSekaiAdRewards().run(context, SimpleNamespace()))
            create.return_value.run.assert_called_once()
        context.tasker.stopping = True
        with patch.object(agent, 'create_workflow') as create:
            self.assertFalse(agent.ProjectSekaiAdRewards().run(context, SimpleNamespace()))
            create.assert_not_called()

    def test_agent_rejects_missing_false_or_non_boolean_completion_with_visible_error(self):
        context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
        for report in (None, [], {}, {'completed': False, 'error': '未弹出观看确认'},
                       {'completed': 'true'}, {'completed': 1}, {'completed': None}):
            with self.subTest(report=report), patch.object(agent, 'create_workflow') as create, \
                    patch('project_sekai.diagnostics.load_policy'), patch.object(agent, 'visible_log') as visible:
                create.return_value.run.return_value = report
                self.assertFalse(agent.ProjectSekaiAdRewards().run(context, SimpleNamespace()))
                self.assertIn('广告奖励中止', visible.call_args.args[1])
                if isinstance(report, dict) and report.get('error'):
                    self.assertIn(report['error'], visible.call_args.args[1])

    def test_agent_failed_no_confirmation_with_logging_disabled_creates_no_files(self):
        context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
        was_disabled = not diagnostics.enabled()
        try:
            diagnostics.configure(True, framework=False)
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workflow, device, _ = self.workflow(root / 'reports', popups=False)
                device.state = 'rewards'
                with patch.object(agent, 'create_workflow', return_value=workflow), \
                        patch('project_sekai.diagnostics.load_policy'), patch.object(agent, 'visible_log') as visible:
                    self.assertFalse(agent.ProjectSekaiAdRewards().run(context, SimpleNamespace()))
                self.assertEqual(workflow.report['started_ads'], 0)
                self.assertIs(workflow.report['completed'], False)
                self.assertIn('观看确认', visible.call_args.args[1])
                self.assertEqual(list(root.iterdir()), [])
        finally:
            diagnostics.configure(was_disabled, framework=False)


    def test_agent_propagates_failure_and_cancel_without_claiming_success(self):
        context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
        for failure in (TimeoutError('广告超时'), InterruptedError('停止')):
            with self.subTest(failure=failure), patch.object(agent, 'create_workflow') as create, \
                    patch.object(agent, 'visible_log'):
                create.return_value.run.side_effect = failure
                self.assertFalse(agent.ProjectSekaiAdRewards().run(context, SimpleNamespace()))



if __name__ == "__main__":
    unittest.main()
