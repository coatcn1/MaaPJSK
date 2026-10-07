import json
import csv
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock
import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agent'))
import solo_live as agent
from project_sekai.ocr import LineOcr, Reading
from project_sekai.life_monitor import LifeDepleted, LifeGuard, ZeroLifeTemplate
from project_sekai.solo_live import LiveResult, SoloLive, numeric, is_light_mode, append_result_index
from project_sekai.song_identity import SongMatcher, read_image, title_score
from project_sekai.sus_chart import Chart, Gesture, Point
from scripts.test_solo_live import controller_options


class SoloTests(unittest.TestCase):
    def test_date_update_notice_requires_modal_two_exact_lines_and_no_hud(self):
        workflow = SoloLive.__new__(SoloLive)
        workflow.navigator = SimpleNamespace(templates={'life_hud': None}, threshold=.83,
                                            match=Mock(return_value=(0., (0, 0))))
        workflow.ocr = Mock()
        workflow.ocr.read.side_effect = [Reading('日付が更新されました。', .99), Reading('タイトルに戻ります。', .99)]
        frame = np.zeros((720, 1280, 3), np.uint8)
        cv2.rectangle(frame, (251, 248), (1028, 471), (235, 235, 240), -1)
        self.assertIsNotNone(workflow.date_update_notice(frame))
        workflow.ocr.read.reset_mock()
        self.assertIsNone(workflow.date_update_notice(np.zeros_like(frame)))
        workflow.ocr.read.assert_not_called()
        workflow.ocr.read.side_effect = [Reading('プロジェクトセカイを終了しますか？', .99), Reading('タイトルに戻ります。', .99)]
        self.assertIsNone(workflow.date_update_notice(frame))
        workflow.ocr.read.reset_mock()
        workflow.navigator.match.return_value = (1., (0, 0))
        self.assertIsNone(workflow.date_update_notice(frame))
        workflow.ocr.read.assert_not_called()

    def install_search_ocr(self, workflow, readings):
        values = iter(readings)
        last_query = [Reading('', 0.)]
        def read(frame, box):
            if box in ((151, 20, 580, 65), (151, 27, 580, 57), (12, 642, 460, 696)):
                return last_query[0]
            value = next(values)
            if box == (151, 24, 580, 62):
                last_query[0] = value
            return value
        workflow.ocr.read.side_effect = read

    def test_specified_song_name_requires_unique_complete_local_title(self):
        workflow = SoloLive.__new__(SoloLive)
        workflow.repository = SimpleNamespace(songs={54: {'title': 'Ready Steady', 'charts': {'expert': {}}},
                                                    55: {'title': 'Ready', 'charts': {'expert': {}}}}, load_chart=Mock())
        self.assertEqual(workflow.resolve_specified_song('Ready Steady', 'expert'), 54)
        self.assertEqual(workflow.resolve_specified_song(' ready steady ', 'expert'), 54)
        for name in ('', 'Ready Ste', 'unknown'):
            with self.assertRaises(ValueError):
                workflow.resolve_specified_song(name, 'expert')
        workflow.repository.songs[56] = {'title': 'ReadySteady', 'charts': {'expert': {}}}
        with self.assertRaises(ValueError):
            workflow.resolve_specified_song('Ready Steady', 'expert')

    def test_specified_invalid_local_chart_prevents_all_navigation(self):
        workflow = SoloLive.__new__(SoloLive)
        workflow.device, workflow.navigator = Mock(), Mock()
        workflow.repository = SimpleNamespace(songs={54: {'title': 'Ready Steady', 'charts': {'expert': {}}}},
                                              load_chart=Mock(side_effect=ValueError('本地谱面 SHA256 校验失败')))
        with self.assertRaisesRegex(ValueError, 'SHA256'):
            workflow.select('expert', 'specified', song_name='Ready Steady')
        workflow.device.assert_not_called()
        workflow.navigator.return_to_home.assert_not_called()
        workflow.navigator.tap.assert_not_called()

    def test_specified_configuration_adds_name_node_only_to_new_mode(self):
        root = Path(__file__).resolve().parents[1]
        interface = json.loads((root / 'interface.json').read_text(encoding='utf8'))
        pipeline = json.loads((root / 'resource/pipeline/solo_chart_live.json').read_text(encoding='utf8'))
        cases = {case['name']: case for case in interface['option']['SoloChartLiveSongMode']['cases']}
        self.assertEqual(pipeline['SoloChartLiveSongConfig']['next'], ['SoloChartLiveCountConfig'])
        for mode in ('Current', 'Random'):
            self.assertNotIn('next', cases[mode]['pipeline_override']['SoloChartLiveSongConfig'])
        self.assertEqual(cases['Specified']['pipeline_override']['SoloChartLiveSongConfig']['next'], ['SoloChartLiveSongNameConfig'])
        self.assertEqual(agent.validate_option('song_mode', 'specified'), 'specified')
        self.assertEqual(agent.validate_option('song_name', ' Ready Steady '), 'Ready Steady')
        for name in ('', '  ', None, 54):
            with self.assertRaises(ValueError):
                agent.validate_option('song_name', name)

    def test_specified_search_uses_native_confirm_without_clearing_submitted_query(self):
        workflow = SoloLive.__new__(SoloLive)
        frame = np.full((720, 1280, 3), 100, np.uint8)
        workflow.device, workflow.navigator, workflow.ocr = Mock(), Mock(), Mock()
        workflow.search_clear_visible = Mock(return_value=False)
        workflow.checked_search_page = Mock(return_value=frame)
        workflow.screenshot = Mock(return_value=frame)
        workflow.native_search_text_present = Mock(side_effect=[False, True, True])
        workflow.last_report = {'requested_song_name': 'Ready Steady', 'expected_song_id': 54}
        workflow.pause = lambda _: None
        self.install_search_ocr(workflow, [Reading('确定', .99),
                                       Reading('Ready Steady', .99), Reading('确定', .99),
                                       Reading('Ready Steady', .99), Reading('确定', .99)])
        workflow.device.controller.post_input_text.return_value.wait.return_value.succeeded = True
        workflow.submit_song_search('Ready Steady')
        workflow.device.controller.post_input_text.assert_called_once_with('ReadySteady')
        self.assertEqual(workflow.last_report['search_query'], 'ReadySteady')
        self.assertEqual(workflow.last_report['requested_song_name'], 'Ready Steady')
        self.assertEqual([(call.args[0], call.args[1]) for call in workflow.navigator.tap.call_args_list],
                         [(350, 42), (1215, 667)])

    def test_specified_search_stop_or_empty_text_cannot_submit(self):
        for stop in (True, False):
            workflow = SoloLive.__new__(SoloLive)
            workflow.device, workflow.navigator, workflow.ocr = Mock(), Mock(), Mock()
            workflow.checked_search_page = Mock(return_value=np.full((720, 1280, 3), 100, np.uint8))
            workflow.screenshot = Mock(return_value=np.full((720, 1280, 3), 100, np.uint8))
            clock = SimpleNamespace(now=0.)
            workflow.native_search_text_present = Mock(return_value=False)
            workflow.search_clear_visible = Mock(return_value=False)
            workflow.pause = lambda seconds: setattr(clock, 'now', clock.now + seconds)
            query_reads = []
            def read(frame, box):
                if box[0] == 151:
                    query_reads.append(None)
                    return Reading('曲名・クリエイター名から探す' if len(query_reads) == 1 else 'wrong', .99)
                return Reading('确定', .99)
            workflow.ocr.read.side_effect = read
            if stop:
                workflow.navigator._check_stop.side_effect = InterruptedError('stop')
            with patch('project_sekai.solo_live.time.monotonic', side_effect=lambda: clock.now), self.assertRaises(InterruptedError if stop else RuntimeError):
                workflow.submit_song_search('Ready Steady')
            self.assertNotIn((1215, 667), [(call.args[0], call.args[1]) for call in workflow.navigator.tap.call_args_list])

    def test_specified_all_category_scroll_is_bounded_and_requires_text(self):
        workflow = SoloLive.__new__(SoloLive)
        workflow.device, workflow.navigator, workflow.ocr = Mock(), Mock(), Mock()
        workflow.checked_search_page = Mock(return_value=np.full((720, 1280, 3), 100, np.uint8))
        workflow.pause = lambda _: None
        workflow.ocr.read.return_value = Reading('その他', .99)
        with self.assertRaises(RuntimeError):
            workflow.select_all_search_category()
        self.assertEqual(workflow.device.swipe.call_count, 3)
        workflow.navigator.tap.assert_not_called()
        workflow.device.swipe.assert_called_with(70, 180, 70, 550)

    def test_specified_search_old_query_clears_only_before_new_input(self):
        workflow = SoloLive.__new__(SoloLive)
        frame = np.full((720, 1280, 3), 100, np.uint8)
        workflow.device, workflow.navigator, workflow.ocr = Mock(), Mock(), Mock()
        workflow.checked_search_page = Mock(return_value=frame)
        workflow.screenshot = Mock(return_value=frame)
        workflow.search_clear_visible = Mock(side_effect=[True, False])
        workflow.native_search_text_present = Mock(side_effect=[False, True, True])
        workflow.pause = lambda _: None
        self.install_search_ocr(workflow, [Reading('确定', .99), Reading('Ready Steady', .99), Reading('确定', .99),
                                       Reading('Ready Steady', .99), Reading('确定', .99)])
        workflow.submit_song_search('Ready Steady')
        self.assertEqual([(call.args[0], call.args[1]) for call in workflow.navigator.tap.call_args_list],
                         [(610, 42), (350, 42), (1215, 667)])

    def test_specified_search_result_must_really_select_expected_id(self):
        workflow = SoloLive.__new__(SoloLive)
        workflow.repository = SimpleNamespace(songs={548: {'title': 'ぼくのかみさま'}})
        workflow.select_all_search_category, workflow.submit_song_search = Mock(), Mock()
        workflow.checked_search_page = Mock(return_value=np.full((720, 1280, 3), 100, np.uint8))
        workflow.ocr, workflow.matcher = Mock(), Mock()
        workflow.ocr.read.return_value = Reading('ぼくのかみさま', .99)
        workflow.matcher.match.return_value = SimpleNamespace(song_id=54)
        workflow.search_clear_visible = Mock(return_value=True)
        workflow.pause = lambda _: None
        with self.assertRaisesRegex(RuntimeError, '未选中指定歌曲'):
            workflow.search_specified_song(548, 'expert')
        workflow.matcher.match.return_value = SimpleNamespace(song_id=548)
        self.assertEqual(workflow.search_specified_song(548, 'expert').song_id, 548)

    def test_specified_search_clear_x_does_not_confuse_magnifier(self):
        frame = np.full((720, 1280, 3), 255, np.uint8)
        cv2.line(frame, (600, 32), (620, 52), (60, 60, 60), 4)
        cv2.line(frame, (600, 52), (620, 32), (60, 60, 60), 4)
        self.assertTrue(SoloLive.search_clear_visible(frame))
        frame[:] = 255
        cv2.circle(frame, (607, 39), 8, (60, 60, 60), 2)
        cv2.line(frame, (613, 45), (620, 52), (60, 60, 60), 3)
        self.assertFalse(SoloLive.search_clear_visible(frame))

    def test_specified_nonempty_native_input_rejects_repeated_paste(self):
        workflow = SoloLive.__new__(SoloLive)
        workflow.device, workflow.navigator, workflow.ocr = Mock(), Mock(), Mock()
        workflow.checked_search_page = Mock(return_value=np.full((720, 1280, 3), 100, np.uint8))
        workflow.search_clear_visible = Mock(return_value=False)
        workflow.native_search_text_present = Mock(return_value=True)
        workflow.pause = lambda _: None
        workflow.ocr.read.return_value = Reading('确定', .99)
        with self.assertRaisesRegex(RuntimeError, '拒绝重复输入'):
            workflow.submit_song_search('Ready Steady')
        workflow.device.controller.post_input_text.assert_not_called()
        self.assertNotIn((1215, 667), [call.args[:2] for call in workflow.navigator.tap.call_args_list])

    def test_native_editor_shape_distinguishes_blank_cursor_from_text(self):
        frame = np.full((720, 1280, 3), 255, np.uint8)
        cv2.line(frame, (16, 650), (16, 685), (0, 0, 0), 1)
        self.assertFalse(SoloLive.native_search_text_present(frame))
        cv2.putText(frame, 'QUERY', (16, 681), cv2.FONT_HERSHEY_SIMPLEX, .8, (0, 0, 0), 2)
        self.assertTrue(SoloLive.native_search_text_present(frame))
        frame[:] = 255
        cv2.rectangle(frame, (450, 645), (480, 690), (0, 0, 0), -1)
        self.assertFalse(SoloLive.native_search_text_present(frame))

    def test_search_submission_waits_only_for_fresh_confirmation(self):
        workflow = SoloLive.__new__(SoloLive)
        workflow.search_clear_visible = Mock(return_value=False)
        workflow.checked_search_page = Mock(return_value=np.full((720, 1280, 3), 100, np.uint8))
        workflow.device, workflow.ocr = Mock(), Mock()
        workflow.last_report = {}
        clock = SimpleNamespace(now=0.)
        workflow.native_search_text_present = Mock(side_effect=[False, True, True])
        workflow.pause = lambda seconds: setattr(clock, 'now', clock.now + seconds)
        self.install_search_ocr(workflow, [Reading('old', .99), Reading('确定', .99),
                                       Reading('デーモンロード', .88), Reading('确定', .99),
                                       Reading('デーモンロード', .88), Reading('确定', .99)])
        with patch('project_sekai.solo_live.time.monotonic', side_effect=lambda: clock.now):
            workflow.wait_search_submission('デーモンロード')
        self.assertEqual(workflow.checked_search_page.call_count, 3)
        self.assertLessEqual(clock.now, 2.)
        self.assertEqual(workflow.last_report['search_submission_readings']['typed']['text'], 'デーモンロード')
        workflow.device.controller.post_input_text.assert_not_called()

    def test_search_submission_timeout_records_reads_and_stop_interrupts_wait(self):
        for stop in (False, True):
            workflow = SoloLive.__new__(SoloLive)
            workflow.checked_search_page = Mock(return_value=np.full((720, 1280, 3), 100, np.uint8))
            workflow.ocr, workflow.device = Mock(), Mock()
            workflow.last_report = {}
            clock = SimpleNamespace(now=0.)
            def pause(seconds):
                if stop:
                    raise InterruptedError('stop')
                clock.now += seconds
            workflow.native_search_text_present = Mock(return_value=False)
            workflow.search_clear_visible = Mock(return_value=False)
            workflow.pause = pause
            workflow.ocr.read.side_effect = lambda frame, box: Reading('wrong' if box[0] == 151 else '确定', .99)
            with patch('project_sekai.solo_live.time.monotonic', side_effect=lambda: clock.now), self.assertRaises(InterruptedError if stop else RuntimeError):
                workflow.wait_search_submission('デーモンロード')
            self.assertLessEqual(clock.now, 2.)
            self.assertEqual(workflow.last_report['search_submission_readings']['typed']['text'], 'wrong')
            workflow.device.controller.post_input_text.assert_not_called()

    def test_search_native_exact_channel_does_not_correct_wrong_diacritic(self):
        workflow = SoloLive.__new__(SoloLive)
        workflow.search_clear_visible = Mock(return_value=False)
        workflow.checked_search_page = Mock(return_value=np.full((720, 1280, 3), 100, np.uint8))
        workflow.ocr, workflow.device = Mock(), Mock()
        workflow.last_report = {}
        clock = SimpleNamespace(now=0.)
        workflow.native_search_text_present = Mock(side_effect=[True, True])
        workflow.pause = lambda seconds: setattr(clock, 'now', clock.now + seconds)
        def read(frame, box):
            if box == (1180, 644, 1254, 693):
                return Reading('确定', .99)
            return Reading('デーモンロード' if box == (12, 642, 460, 696) else 'テーモンロード', .99)
        workflow.ocr.read.side_effect = read
        with patch('project_sekai.solo_live.time.monotonic', side_effect=lambda: clock.now):
            workflow.wait_search_submission('デーモンロード')
        evidence = workflow.last_report['search_submission_readings']
        self.assertEqual(evidence['typed']['text'], 'テーモンロード')
        self.assertEqual(len(evidence['query_readings']), 4)
        self.assertEqual(evidence['query_readings'][0]['text'], 'テーモンロード')
        workflow.device.controller.post_input_text.assert_not_called()

    def test_search_committed_state_still_requires_current_proof_and_real_target(self):
        workflow = SoloLive.__new__(SoloLive)
        title = 'デーモンロード'
        workflow.repository = SimpleNamespace(songs={767: {'title': title}})
        workflow.select_all_search_category, workflow.submit_song_search = Mock(), Mock()
        workflow.checked_search_page = Mock(return_value=np.full((720, 1280, 3), 100, np.uint8))
        workflow.ocr, workflow.matcher = Mock(), Mock()
        workflow.ocr.read.side_effect = lambda frame, box: Reading('' if box[0] == 1180 else 'テーモンロード', .99)
        workflow.search_clear_visible = Mock(return_value=True)
        workflow._verified_search_title = title
        workflow.matcher.match.return_value = SimpleNamespace(song_id=767)
        workflow.pause = lambda _: None
        self.assertEqual(workflow.search_specified_song(767, 'expert').song_id, 767)
        self.assertEqual(workflow.matcher.match.call_count, 2)
        workflow.search_clear_visible.return_value = False
        with self.assertRaisesRegex(RuntimeError, '提交状态未确认'):
            workflow.search_specified_song(767, 'expert')
        workflow.search_clear_visible.return_value = True
        workflow.matcher.match.return_value = SimpleNamespace(song_id=54)
        with self.assertRaisesRegex(RuntimeError, '未选中指定歌曲'):
            workflow.search_specified_song(767, 'expert')

    def test_specified_locked_state_requires_both_lock_and_disabled_decide(self):
        workflow = SoloLive.__new__(SoloLive)
        workflow.ocr = Mock()
        workflow.ocr.read.return_value = Reading('决定', .9)
        workflow.selected_card_padlock = Mock(return_value=True)
        frame = np.full((720, 1280, 3), 84, np.uint8)
        self.assertEqual(workflow.specified_decide_state(frame), 'locked')
        workflow.selected_card_padlock.return_value = False
        self.assertEqual(workflow.specified_decide_state(frame), 'unknown')
        frame[:] = 134
        self.assertEqual(workflow.specified_decide_state(frame), 'ready')
        workflow.selected_card_padlock.return_value = True
        self.assertEqual(workflow.specified_decide_state(frame), 'unknown')
        workflow.ocr.read.return_value = Reading('unknown', .99)
        frame[:] = 84
        self.assertEqual(workflow.specified_decide_state(frame), 'unknown')

    def test_selected_card_lock_needs_border_shackle_and_keyhole(self):
        frame = np.full((720, 1280, 3), 60, np.uint8)
        cv2.rectangle(frame, (178, 187), (352, 361), (255, 255, 255), 4)
        cv2.ellipse(frame, (265, 266), (13, 12), 0, 0, 360, (255, 255, 255), 5)
        cv2.rectangle(frame, (247, 269), (283, 294), (255, 255, 255), -1)
        cv2.circle(frame, (265, 279), 4, (60, 60, 60), -1)
        cv2.rectangle(frame, (263, 279), (267, 286), (60, 60, 60), -1)
        self.assertTrue(SoloLive.selected_card_padlock(frame))
        cv2.rectangle(frame, (260, 274), (271, 289), (255, 255, 255), -1)
        self.assertFalse(SoloLive.selected_card_padlock(frame))

    def test_specified_locked_target_never_clicks_disabled_decide(self):
        workflow = SoloLive.__new__(SoloLive)
        workflow.resolve_specified_song = Mock(return_value=767)
        workflow.navigator, workflow.matcher, workflow.ocr = Mock(), Mock(), Mock()
        frame = np.full((720, 1280, 3), 100, np.uint8)
        identity = SimpleNamespace(song_id=767)
        workflow.navigator.wait.return_value = frame
        workflow.ocr.read.return_value = Reading('HARD', .99)
        workflow.search_specified_song = Mock(return_value=identity)
        workflow.checked_search_page = Mock(return_value=frame)
        workflow.matcher.match.return_value = identity
        workflow.specified_decide_state = Mock(return_value='locked')
        workflow.pause = lambda _: None
        with self.assertRaisesRegex(RuntimeError, '指定歌曲尚未解锁'):
            workflow.select('expert', 'specified', song_name='デーモンロード')
        self.assertNotIn((1007, 590), [call.args[:2] for call in workflow.navigator.tap.call_args_list])
        workflow.navigator.tap.reset_mock()
        workflow.specified_decide_state.side_effect = AssertionError('old mode must not inspect locks')
        workflow.select('expert', 'current')
        self.assertIn((1007, 590), [call.args[:2] for call in workflow.navigator.tap.call_args_list])

    def test_acceptance_controller_reuses_mfa_screenshot_extras_and_input_configuration(self):
        config = {'extras':{'type':'MuMu','index':0}}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'libs/MaaAgentBinary').mkdir(parents=True)
            result = controller_options({'ScreencapMethods':64,'InputMethods':16,'Config':json.dumps(config),
                                         'AgentPath':'saved-agent'},root)
            self.assertEqual(result,{'screencap_methods':64,'input_methods':16,'config':config,
                                     'agent_path':str((root/'libs/MaaAgentBinary').resolve())})
            self.assertEqual(controller_options({'Config':config},root)['config'],config)

    def test_acceptance_controller_missing_input_agent_fails_before_connecting(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError,'重新部署'):
                controller_options({'AgentPath':'missing'},Path(directory))

    def test_acceptance_controller_rejects_unknown_config_instead_of_using_slow_defaults(self):
        with self.assertRaisesRegex(ValueError,'不是对象'):
            controller_options({'Config':'[]'},Path('.'))

    def test_ocr_yaml_quotes_are_not_part_of_digit_characters(self):
        fake = SimpleNamespace(SessionOptions=lambda: SimpleNamespace(), InferenceSession=lambda *args,**kwargs: None)
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {'onnxruntime':fake}):
            root = Path(directory)
            (root/'inference.yml').write_text("PostProcess:\n  character_dict:\n  - '0'\n  - '1'\n  - 初\n",encoding='utf-8')
            ocr = LineOcr(root)
            self.assertEqual(ocr.characters,['','0','1','初',' '])
    def test_light_mode_accepts_font_variant_but_rejects_mv_or_uncertain_text(self):
        self.assertTrue(is_light_mode(Reading('輕量',.85)))
        self.assertFalse(is_light_mode(Reading('3DMV',.99)))
        self.assertFalse(is_light_mode(Reading('軽量',.3)))
    def test_interface_exposes_all_difficulties_without_changing_auto_task(self):
        interface = json.loads((Path(__file__).resolve().parents[1] / 'interface.json').read_text(encoding='utf-8'))
        task = next(task for task in interface['task'] if task['name'] == 'SoloChartLive')
        self.assertFalse(task['default_check'])
        cases = interface['option']['SoloChartLiveDifficulty']['cases']
        self.assertEqual([case['name'] for case in cases], ['EASY', 'NORMAL', 'HARD', 'EXPERT', 'MASTER', 'APPEND'])
        self.assertEqual(interface['task'][0]['name'], 'AutoLive')

    def test_option_nodes_store_one_task_and_do_not_overwrite_other_options(self):
        context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
        settings = {'difficulty':'hard', 'song_mode':'random', 'count':2, 'offset_ms':-10,
                    'bonus_consumption':5, 'recovery_mode':'large', 'recovery_count':2}
        for key, value in settings.items():
            argv = SimpleNamespace(task_detail=SimpleNamespace(task_id=11), custom_action_param=json.dumps({key:value}))
            self.assertTrue(agent.ProjectSekaiSoloLiveConfig().run(context, argv))
        self.assertEqual(agent._SETTINGS.pop(11), settings)

    def test_invalid_config_clears_task_state(self):
        context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
        agent._SETTINGS[12] = {'difficulty':'easy'}
        argv = SimpleNamespace(task_detail=SimpleNamespace(task_id=12), custom_action_param='{"count":0}')
        self.assertFalse(agent.ProjectSekaiSoloLiveConfig().run(context, argv))
        self.assertNotIn(12, agent._SETTINGS)

    def test_accuracy_metrics_have_explicit_meaning(self):
        result = LiveResult(90,5,2,1,2).to_dict()
        self.assertEqual(result['total'], 100)
        self.assertEqual(result['perfect_rate'], .9)
        self.assertEqual(result['hit_rate'], .98)

    def test_unreadable_numbers_are_not_recorded_as_zero(self):
        for reading in [Reading('', 1), Reading('0 4', .9), Reading('4', .3)]:
            with self.assertRaises(ValueError): numeric(reading)
        self.assertEqual(numeric(Reading("'0'",.99)),0)

    def test_short_song_titles_cannot_be_confirmed_by_substring(self):
        self.assertEqual(title_score('R','R'),1)
        self.assertLess(title_score('RANDOM','R'),.82)

    def test_opening_white_text_keeps_small_japanese_voicing_marks(self):
        matcher = SongMatcher.__new__(SongMatcher)
        matcher.ocr = Mock()
        frame = np.full((40,160,3),(115,90,80),np.uint8)
        frame[15:33,10:20] = 255
        frame[8:11,22:25] = 255
        matcher.read_opening_text(frame,(0,0,160,40))
        processed = matcher.ocr.read.call_args.args[0]
        self.assertEqual(np.count_nonzero(processed[:,:,0] == 0),189)
        self.assertEqual(processed.shape[0],35)

    def test_opening_difficulty_does_not_crop_moving_badge_or_read_jacket_edge(self):
        matcher = SongMatcher.__new__(SongMatcher)
        matcher.ocr = Mock()
        frame = np.full((720,1280,3),(200,180,20),np.uint8)
        frame[653:656,95:320] = 255
        for y in [657,664]:
            frame[656:692,65:320] = (200,180,20)
            frame[y:y+18,70:160] = 255
            matcher.read_opening_text(frame,(65,653,320,692),badge=True)
            processed = matcher.ocr.read.call_args.args[0]
            self.assertEqual(processed.shape,(28,102,3))
            self.assertEqual(np.count_nonzero(processed[:,:,0] == 0),1620)

    def test_loading_frame_without_white_text_cannot_be_sent_to_ocr(self):
        matcher = SongMatcher.__new__(SongMatcher)
        matcher.ocr = Mock()
        frame = np.zeros((720,1280,3),np.uint8)
        self.assertEqual(matcher.read_opening_text(frame,(384,541,1240,581)),Reading('',0))
        matcher.ocr.read.assert_not_called()

    def test_result_index_preserves_japanese_title_and_missing_optional_number(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = {'started_at':'2026-10-03','requested_difficulty':'easy','completed':True,
                      'preparation_identity':{'song_id':730,'title':'エメラルド'},
                      'judgements':LiveResult(90,5,2,1,2).to_dict()}
            for run in ['first','second']:
                append_result_index(root,root/run,report)
            with (root/'results.csv').open(encoding='utf-8-sig',newline='') as stream:
                rows=list(csv.DictReader(stream))
            self.assertEqual(len(rows),2)
            self.assertEqual(rows[0]['title'],'エメラルド')
            self.assertEqual(rows[0]['late'],'')
            self.assertEqual(rows[0]['perfect_rate'],'0.9')

    def _collection(self, sequence, clear, failed, judge):
        workflow = SoloLive.__new__(SoloLive)
        workflow.device = Mock()
        workflow.navigator = SimpleNamespace(threshold=.83,templates={'live_failed':None})
        workflow.navigator.match=lambda frame,name,*args: (1.0 if (name=='home' and frame is sequence[-1]) or
            (name=='live_clear' and frame is clear) or (name=='live_failed' and frame is failed) else 0.0,(0,0))
        workflow.navigator._check_stop=lambda:None
        workflow.screenshot=Mock(side_effect=sequence)
        workflow.pause=lambda _:None
        workflow.read_result=lambda frame: LiveResult(250,5,2,1,1) if frame is judge else None
        return workflow

    def test_date_update_collect_stops_before_inputs_and_preserves_results(self):
        frames = [np.full((720, 1280, 3), value, np.uint8) for value in (140, 150)]
        workflow = self._collection(frames, None, None, None)
        workflow.date_update_notice = Mock(return_value={'updated': {'text': '日付が更新されました。'},
                                                        'return_to_title': {'text': 'タイトルに戻ります。'}})
        judgements = {'perfect': 1147, 'great': 1, 'total': 1148, 'total_matches_chart': True}
        report = {'engine': 'native', 'completed': False, 'live_status': 'cleared', 'judgements': dict(judgements),
                  'playback': {'release_confirmed': True, 'sent_actions': 5889,
                               'release': {'reset_executed': True, 'release_proof': 'current-reset-jlog-and-cleanup'}}}
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(RuntimeError, '需要重新登录'):
            workflow.collect(report, Path(directory))
        workflow.device.tap.assert_not_called()
        workflow.device.back.assert_not_called()
        self.assertTrue(report['requires_relogin'])
        self.assertEqual(report['date_update_notice']['confirmation_frames'], 2)
        self.assertEqual(report['judgements'], judgements)
        self.assertEqual(report['live_status'], 'cleared')
        self.assertFalse(report['completed'])
        self.assertFalse(report.get('returned_home', False))
        self.assertFalse(report.get('performance_completed', False))

    def test_date_update_collect_without_current_release_cannot_claim_relogin(self):
        frame = np.full((720, 1280, 3), 140, np.uint8)
        workflow = self._collection([frame, InterruptedError('stop')], None, None, None)
        workflow.date_update_notice = Mock(return_value={'date': True})
        report = {'engine': 'native', 'playback': {'release_confirmed': True, 'sent_actions': 1, 'release': {}}}
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(InterruptedError):
            workflow.collect(report, Path(directory))
        workflow.date_update_notice.assert_not_called()
        workflow.device.tap.assert_not_called()
        workflow.device.back.assert_not_called()
        self.assertFalse(report.get('requires_relogin', False))

    def test_date_update_on_post_animation_confirmation_never_sends_back(self):
        clear, first, second = [np.full((720, 1280, 3), value, np.uint8) for value in (130, 140, 150)]
        workflow = self._collection([clear, first, second], clear, None, None)
        workflow.date_update_notice = Mock(side_effect=[None, {'date': True}, {'date': True}])
        report = {'playback': {'release_confirmed': True}}
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(RuntimeError, '需要重新登录'):
            workflow.collect(report, Path(directory))
        workflow.device.tap.assert_called_once_with(1279, 719)
        workflow.device.back.assert_not_called()
        self.assertTrue(report['requires_relogin'])

    def test_collect_clear_or_home_never_bypasses_missing_current_release(self):
        for destination in ('clear', 'home'):
            for release in ({}, {'reset_executed': False, 'release_proof': 'current-reset-jlog-and-cleanup'},
                            {'reset_executed': True, 'release_proof': 'reset-jlog-without-cleanup'}):
                clear, home = [np.full((720, 1280, 3), value, np.uint8) for value in (140, 160)]
                workflow = self._collection([clear, home] if destination == 'clear' else [home], clear, None, None)
                report = {'engine': 'native', 'completed': False, 'playback_error': 'RuntimeError: original ACK failure',
                          'playback': {'release_confirmed': bool(release), 'sent_actions': 1, 'release': release}}
                with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(RuntimeError, '严格触点释放'):
                    workflow.collect(report, Path(directory))
                workflow.device.tap.assert_not_called()
                workflow.device.back.assert_not_called()
                self.assertFalse(report['completed'])
                self.assertFalse(report.get('returned_home', False))
                self.assertEqual(report['playback_error'], 'RuntimeError: original ACK failure')

    def test_collect_rechecks_release_before_back_after_safe_animation_tap(self):
        clear, other, home = [np.full((720, 1280, 3), value, np.uint8) for value in (140, 150, 160)]
        workflow = self._collection([clear, other, home], clear, None, None)
        report = {'engine': 'native', 'playback': {'release_confirmed': True, 'sent_actions': 1,
                  'release': {'reset_executed': True, 'release_proof': 'current-reset-jlog-and-cleanup'}}}
        frames = iter((clear, other))
        def screenshot():
            frame = next(frames)
            if frame is other:
                report['playback']['release']['reset_executed'] = False
            return frame
        workflow.screenshot.side_effect = screenshot
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(RuntimeError, '严格触点释放'):
            workflow.collect(report, Path(directory))
        workflow.device.tap.assert_called_once_with(1279, 719)
        workflow.device.back.assert_not_called()

    def test_failed_banner_keeps_round_failed_after_reading_judgements(self):
        failed,judge,other,home=[np.full((720,1280,3),value,np.uint8) for value in [180,190,200,210]]
        workflow=self._collection([failed,other,judge,judge,other,home],None,failed,judge)
        report={'engine':'legacy','playback':{'release_confirmed':True},'chart':{'total_note_count':259}}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError,'演出失败'):
                workflow.collect(report,Path(directory))
        self.assertEqual(report['judgements']['perfect'],250)
        self.assertEqual(report['live_status'],'failed')
        self.assertFalse(report.get('completed',False))

    def test_black_transition_and_missing_clear_use_life_hud_end_confirmation(self):
        black = np.zeros((720, 1280, 3), np.uint8)
        other, judge, home = [np.full((720, 1280, 3), value, np.uint8) for value in (80, 100, 120)]
        workflow = self._collection([black, other, judge, judge, home, home], None, None, judge)
        workflow.life_guard = SimpleNamespace(hud_seen=True)
        workflow.navigator.templates['life_hud'] = None
        report = {'engine': 'legacy', 'chart': {'total_note_count': 259}, 'playback': {
            'planned_actions': 4, 'sent_actions': 4, 'release_confirmed': True}}
        with tempfile.TemporaryDirectory() as directory:
            workflow.collect(report, Path(directory))
        self.assertTrue(report['completed'])
        self.assertEqual(report['live_status'], 'ended')
        self.assertEqual(report['end_detection']['method'], 'life_hud_disappeared')

    def test_unreadable_result_at_home_is_recorded_as_warning(self):
        clear, other, home = [np.full((720, 1280, 3), value, np.uint8) for value in (80, 100, 120)]
        workflow = self._collection([clear, other, home, home], clear, None, None)
        report = {'engine':'legacy','playback':{'release_confirmed':True},'chart': {'total_note_count': 259}}
        with tempfile.TemporaryDirectory() as directory:
            workflow.collect(report, Path(directory))
        self.assertFalse(report['completed'])
        self.assertTrue(report['returned_home'])
        self.assertEqual(report['result_status'], 'unreadable')
        self.assertIn('继续下一曲', report['settlement_warning'])

    def test_result_note_mismatch_preserves_numbers_without_stopping(self):
        clear, judge, home = [np.full((720, 1280, 3), value, np.uint8) for value in (80, 100, 120)]
        workflow = self._collection([clear, judge, judge, home, home], clear, None, judge)
        report = {'engine':'legacy','playback':{'release_confirmed':True},'chart': {'total_note_count': 260}}
        with tempfile.TemporaryDirectory() as directory:
            workflow.collect(report, Path(directory))
        self.assertFalse(report['completed'])
        self.assertEqual(report['judgements']['total'], 259)
        self.assertEqual(report['result_status'], 'note_count_mismatch')
        self.assertTrue(report['returned_home'])

    def test_home_after_animation_tap_prevents_another_back(self):
        clear,judge,other,home=[np.full((720,1280,3),value,np.uint8) for value in [180,190,200,210]]
        workflow=self._collection([clear,other,judge,judge,home,home],clear,None,judge)
        report={'engine':'legacy','playback':{'release_confirmed':True},'chart':{'total_note_count':259}}
        with tempfile.TemporaryDirectory() as directory:
            workflow.collect(report,Path(directory))
        self.assertTrue(report['completed'])
        self.assertEqual(workflow.device.back.call_count,1)
        self.assertEqual(workflow.device.tap.call_args.args,(1279,719))

    def test_judgement_page_arriving_in_confirmation_frame_is_read_before_back(self):
        clear,judge,home=[np.full((720,1280,3),value,np.uint8) for value in [180,190,210]]
        workflow=self._collection([clear,judge,judge,home,home],clear,None,judge)
        report={'engine':'legacy','playback':{'release_confirmed':True},'chart':{'total_note_count':259}}
        with tempfile.TemporaryDirectory() as directory:
            workflow.collect(report,Path(directory))
        self.assertTrue(report['completed'])
        self.assertEqual(report['judgements']['total'],259)
        self.assertEqual(workflow.device.back.call_count,0)

    def test_live_clear_banner_in_confirmation_frame_counts_only_after_result_and_home(self):
        other,clear,judge,home=[np.full((720,1280,3),value,np.uint8) for value in [170,180,190,210]]
        workflow=self._collection([other,clear,judge,judge,home,home],clear,None,judge)
        report={'engine':'legacy','playback':{'release_confirmed':True},'chart':{'total_note_count':259}}
        with tempfile.TemporaryDirectory() as directory:
            workflow.collect(report,Path(directory))
        self.assertTrue(report['completed'])
        self.assertEqual(report['live_status'],'cleared')

    def test_normal_selection_returns_from_append_group_before_clicking_difficulty(self):
        workflow=SoloLive.__new__(SoloLive)
        workflow.navigator=Mock()
        frame=np.zeros((720,1280,3),np.uint8)
        workflow.navigator.wait.return_value=frame
        workflow.ocr=Mock()
        workflow.ocr.read.side_effect=[Reading('APPEND',.99),Reading('HARD',.99)]
        identity=SimpleNamespace(song_id=730)
        workflow.matcher=Mock()
        workflow.matcher.match.return_value=identity
        workflow.pause=lambda _:None
        self.assertIs(workflow.select('normal','current')[0],identity)
        points=[call.args[:2] for call in workflow.navigator.tap.call_args_list]
        self.assertLess(points.index((803,485)),points.index((934,492)))

    def test_append_selection_uses_the_single_central_difficulty_button(self):
        workflow=SoloLive.__new__(SoloLive)
        workflow.navigator=Mock()
        workflow.navigator.wait.return_value=np.zeros((720,1280,3),np.uint8)
        workflow.ocr=Mock()
        workflow.ocr.read.return_value=Reading('APPEND',.99)
        workflow.matcher=Mock()
        workflow.matcher.match.return_value=SimpleNamespace(song_id=730)
        workflow.pause=lambda _:None
        workflow.select('append','current')
        points=[call.args[:2] for call in workflow.navigator.tap.call_args_list]
        self.assertIn((1015,500),points)
        self.assertNotIn((1175,504),points)

    def test_bonus_adjustment_rechecks_each_step_after_ignored_first_plus(self):
        workflow=SoloLive.__new__(SoloLive)
        workflow.navigator=Mock()
        workflow.screenshot=Mock()
        workflow.pause=lambda _:None
        workflow.read_bonus=Mock(side_effect=[(value,Reading(str(value),.99)) for value in [0,0,1,2,3,4,5]])
        self.assertEqual(workflow.adjust_bonus_count(5,np.zeros((1,1,3),np.uint8)),5)
        self.assertEqual(workflow.navigator.tap.call_count,6)

    def test_two_digit_consumption_preserves_the_complete_zero_glyph(self):
        workflow = SoloLive.__new__(SoloLive)
        frame = np.zeros((720,1280,3),np.uint8)
        cv2.putText(frame,'10',(651,279),cv2.FONT_HERSHEY_SIMPLEX,1,(180,50,240),2)
        def read(image,box):
            mask = (image.min(axis=2)<127).astype(np.uint8)
            _,hierarchy = cv2.findContours(mask,cv2.RETR_CCOMP,cv2.CHAIN_APPROX_SIMPLE)
            closed_zero = hierarchy is not None and np.any(hierarchy[0,:,3]>=0)
            return Reading('10' if closed_zero else '1C',.99)
        workflow.ocr = SimpleNamespace(read=read)
        self.assertEqual(workflow.read_bonus(frame)[0],10)

    def test_consumption_with_a_clipped_letter_is_not_coerced_into_a_number(self):
        workflow = SoloLive.__new__(SoloLive)
        workflow.ocr = Mock()
        workflow.ocr.read.return_value = Reading('1C',.99)
        with self.assertRaisesRegex(ValueError,'数字不完整'):
            workflow.read_bonus(np.zeros((720,1280,3),np.uint8))

    def _bonus_workflow(self, current=5):
        workflow = SoloLive.__new__(SoloLive)
        workflow.navigator = Mock(threshold=.83)
        workflow.device = Mock()
        workflow.screenshot = Mock(return_value=np.zeros((720,1280,3),np.uint8))
        workflow.pause = lambda _:None
        workflow.ocr = Mock()
        workflow.ocr.read.side_effect = [Reading('ライブボーナス消費',.99),Reading('消費量を設定します',.99),
                                       Reading('軽量',.99)]
        workflow.read_bonus = Mock(return_value=(current,Reading(str(current),.99)))
        workflow.adjust_bonus_count = Mock()
        workflow.log = Mock()
        return workflow

    def test_current_consumption_is_preserved_without_adjusting_slider(self):
        workflow = self._bonus_workflow(5)
        report = {}
        with tempfile.TemporaryDirectory() as directory:
            workflow.prepare_bonus('current',report,Path(directory))
        workflow.adjust_bonus_count.assert_not_called()
        workflow.device.swipe.assert_not_called()
        self.assertEqual(report['bonus']['consumption'],5)
        self.assertEqual(report['bonus']['requested_consumption'],'current')

    def test_recovery_tab_is_never_accepted_as_consumption_settings(self):
        workflow = self._bonus_workflow(5)
        workflow.ocr.read.side_effect = [Reading('ライブボーナス消費',.99),Reading('アイテム',.99)]*6
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(RuntimeError,'未到达体力消耗页'):
            workflow.prepare_bonus(0,{},Path(directory))
        workflow.read_bonus.assert_not_called()
        workflow.adjust_bonus_count.assert_not_called()
        self.assertIn((470,42),[call.args[:2] for call in workflow.navigator.tap.call_args_list])

    def test_consumption_page_waits_for_text_after_tab_transition(self):
        workflow = self._bonus_workflow(5)
        workflow.ocr.read.side_effect = [Reading('ライブボーナス消貨',.99),Reading('消員量',.8),
                                       Reading('ライブボーナス消費',.99),Reading('消費量を設定します',.99),
                                       Reading('軽量',.99)]
        with tempfile.TemporaryDirectory() as directory:
            workflow.prepare_bonus('current',{},Path(directory))
        self.assertEqual([call.args[:2] for call in workflow.navigator.tap.call_args_list].count((470,42)),1)

    def test_every_user_selected_consumption_is_confirmed_exactly(self):
        for target in range(11):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory:
                workflow = self._bonus_workflow()
                workflow.read_bonus.side_effect = [(5,Reading('5',.99)), *[(target,Reading(str(target),.99))]*2]
                report = {}
                workflow.prepare_bonus(target,report,Path(directory))
                self.assertEqual(report['bonus']['consumption'],target)
                if target != 5:
                    workflow.adjust_bonus_count.assert_called_once()
                    self.assertEqual(workflow.adjust_bonus_count.call_args.args[0],target)
                else:
                    workflow.adjust_bonus_count.assert_not_called()

    def test_unconfirmed_consumption_is_not_saved_or_used_to_start(self):
        workflow = self._bonus_workflow(5)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError,'未确认用户指定'):
                workflow.prepare_bonus(3,{},Path(directory))
        self.assertNotIn((762,655),[call.args[:2] for call in workflow.navigator.tap.call_args_list])

    def test_task_snapshot_is_not_cached_after_setting_save_failure(self):
        for failure in ('save_request', 'return_page'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                workflow = self._bonus_workflow(5)
                def tap(x, y, reason):
                    if failure == 'save_request' and (x, y) == (762, 655):
                        raise RuntimeError('保存请求失败')
                workflow.navigator.tap.side_effect = tap
                if failure == 'return_page':
                    workflow.navigator.wait.side_effect = RuntimeError('保存后未回到准备页')
                snapshot = {}
                report = {'report_path': str(Path(directory) / 'report.json')}
                with self.assertRaisesRegex(RuntimeError, '保存请求失败|未回到准备页'):
                    workflow.prepare_task_bonus('current', report, Path(directory), snapshot)
                self.assertEqual(snapshot, {})
                self.assertFalse(report['bonus']['confirmed'])

    def test_calibration_failed_setting_save_is_not_reported_as_retained(self):
        from project_sekai.calibration import CalibrationProfiles, CalibrationRunner
        from project_sekai.performance_settings import PerformanceSettings
        for failure in ('save_request', 'return_page'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workflow = self._bonus_workflow(5)
                workflow.stop_requested = lambda: False
                workflow.restore_calibration_bonus = Mock(return_value={'confirmed': True, 'consumption': 5})
                def tap(x, y, reason):
                    if failure == 'save_request' and (x, y) == (762, 655):
                        raise RuntimeError('保存请求失败')
                workflow.navigator.tap.side_effect = tap
                if failure == 'return_page':
                    workflow.navigator.wait.side_effect = RuntimeError('保存后未回到准备页')
                def run(*args, **kwargs):
                    workflow.last_report = {'report_path': str(root / 'report.json')}
                    workflow.prepare_task_bonus(kwargs['bonus_consumption'], workflow.last_report,
                                                root, kwargs['_bonus_snapshot'])
                    self.fail('保存失败不得进入校准演奏')
                workflow.run = run
                runner = CalibrationRunner(workflow, CalibrationProfiles(root / 'profiles'), {},
                                           PerformanceSettings(), root / 'sessions', test_bonus_consumption=5)
                with self.assertRaisesRegex(RuntimeError, '保存请求失败|未回到准备页'):
                    runner.run('easy', 'current')
                session = json.loads(next((root / 'sessions').glob('*/session.json')).read_text(encoding='utf-8'))
                self.assertEqual(session['status'], 'failed')
                self.assertFalse(workflow.last_report['bonus']['confirmed'])
                self.assertNotIn('retained', session['bonus_restoration'])
                workflow.restore_calibration_bonus.assert_called_once()

    def test_task_snapshot_inherits_current_setting_without_fresh_ocr(self):
        workflow = self._bonus_workflow(7)
        snapshot = {}
        with tempfile.TemporaryDirectory() as directory:
            first = {'report_path': str(Path(directory) / 'first.json')}
            workflow.prepare_task_bonus('current', first, Path(directory), snapshot)
            workflow.read_bonus.reset_mock()
            workflow.navigator.tap.reset_mock()
            second = {'report_path': str(Path(directory) / 'second.json')}
            workflow.prepare_task_bonus('current', second, Path(directory), snapshot)
        self.assertEqual(second['bonus']['consumption'], 7)
        self.assertEqual(second['bonus']['confirmation_source'], 'task_snapshot')
        self.assertEqual(second['bonus']['source_report'], first['report_path'])
        self.assertNotIn('readings', second['bonus'])
        workflow.read_bonus.assert_not_called()
        workflow.navigator.tap.assert_not_called()

    def test_bonus_settings_validate_limits_before_any_device_input(self):
        workflow = SoloLive.__new__(SoloLive)
        workflow.device = Mock()
        for value in [True,-1,11,'5',None]:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError,'每局体力消耗'):
                workflow.run(1,'easy','current',bonus_consumption=value)
        workflow.device.preflight.assert_not_called()
        for key, values in {'bonus_consumption':[True,-1,11], 'recovery_count':[True,0,100],
                            'recovery_mode':['crystal','ad']}.items():
            for value in values:
                with self.subTest(key=key,value=value), self.assertRaises(ValueError):
                    agent.validate_option(key,value)

    def _availability_workflow(self, available, consumption=5):
        workflow = SoloLive.__new__(SoloLive)
        workflow.navigator = Mock()
        workflow.log = Mock()
        workflow.adjust_bonus_count = Mock()
        frame = np.zeros((720,1280,3),np.uint8)
        workflow.wait_available_bonus = Mock(side_effect=[(frame,value,[{'text':str(value)}]) for value in available])
        report = {'bonus':{'consumption':consumption,'confirmed':True}}
        return workflow,report

    def test_zero_consumption_never_reads_available_bonus_or_uses_drinks(self):
        workflow,report = self._availability_workflow([],0)
        workflow.ensure_bonus_available('large',99,report,Path('unused'))
        workflow.wait_available_bonus.assert_not_called()
        workflow.navigator._recover_and_verify.assert_not_called()
        self.assertEqual(report['bonus']['availability'],'not_required')

    def test_sufficient_bonus_does_not_use_drinks(self):
        workflow,report = self._availability_workflow([5])
        workflow.ensure_bonus_available('small',3,report,Path('unused'))
        workflow.navigator.tap.assert_not_called()
        workflow.navigator._recover_and_verify.assert_not_called()
        self.assertEqual(report['bonus']['availability'],'sufficient')

    def test_insufficient_bonus_with_recovery_disabled_does_not_lower_consumption(self):
        workflow,report = self._availability_workflow([4])
        with self.assertRaisesRegex(RuntimeError,'自动用药已关闭'):
            workflow.ensure_bonus_available('off',1,report,Path('unused'))
        workflow.navigator.tap.assert_not_called()
        workflow.adjust_bonus_count.assert_not_called()
        self.assertEqual(report['bonus']['consumption'],5)

    def test_recovery_uses_exact_user_type_and_bottles_and_keeps_auto_off(self):
        for mode, bottles, after in [('small',3,6),('large',2,23)]:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                workflow,report = self._availability_workflow([3,after])
                workflow.ensure_bonus_available(mode,bottles,report,Path(directory))
                self.assertEqual(workflow.navigator._recover_and_verify.call_args.args[1:],(mode,bottles))
                workflow.navigator.ensure_auto.assert_called_once_with(False)
                self.assertEqual(report['recovery']['completed_bottles'],bottles)
                self.assertEqual(report['bonus']['consumption'],5)
                self.assertEqual(report['bonus']['availability'],'recovered')
                workflow.adjust_bonus_count.assert_not_called()

    def test_recovery_can_run_again_in_later_round_without_a_cumulative_bottle_cap(self):
        workflow,report = self._availability_workflow([0,10,3,13])
        with tempfile.TemporaryDirectory() as directory:
            for _ in range(2):
                workflow.ensure_bonus_available('large',1,report,Path(directory))
        self.assertEqual(workflow.navigator._recover_and_verify.call_count,2)

    def test_confirmed_batch_is_persisted_before_notice_dismissal_fails(self):
        workflow, report = self._availability_workflow([2])
        frame = np.zeros((720,1280,3),np.uint8)
        workflow.read_available_bonus = Mock(return_value=(7,Reading('7/50',.99)))
        def recovered(before, mode, count, *, on_recovered):
            on_recovered(count,frame)
            raise RuntimeError('完成提示关闭失败')
        workflow.navigator._recover_and_verify.side_effect = recovered
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError,'完成提示'):
                workflow.ensure_bonus_available('small',5,report,Path(directory))
            saved = json.loads((Path(directory)/'report.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['recovery']['completed_bottles'],5)
        self.assertEqual(saved['recovery']['available_after'],7)
        self.assertEqual(saved['recovery']['requested_bottles'],5)
        self.assertEqual(saved['recovery']['status'],'credited')
        workflow.navigator._recover_and_verify.assert_called_once()
        workflow.navigator.ensure_auto.assert_not_called()

    def test_failed_batch_keeps_actual_readings_without_claiming_all_bottles(self):
        workflow, report = self._availability_workflow([2])
        workflow.navigator.recovery_evidence = {'available_before':2, 'available_after':3,
                                                'expected_increase':5, 'ok_requested':True}
        workflow.navigator._recover_and_verify.side_effect = RuntimeError('未足额到账')
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError,'未足额'):
                workflow.ensure_bonus_available('small',5,report,Path(directory))
            saved = json.loads((Path(directory)/'report.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['recovery']['completed_bottles'],0)
        self.assertEqual(saved['recovery']['available_after'],3)
        self.assertEqual(saved['recovery']['status'],'started')
        workflow.navigator._recover_and_verify.assert_called_once()

    def test_new_report_inherits_unconfirmed_batch_and_never_reselects(self):
        workflow,report = self._availability_workflow([4])
        workflow.navigator.recovery_evidence = {'status':'started','ok_requested':True,
            'requested_bottles':5,'completed_bottles':0,'available_before':2,'available_after':5}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError,'先前已请求'):
                workflow.ensure_bonus_available('small',5,report,Path(directory))
            saved = json.loads((Path(directory)/'report.json').read_text(encoding='utf-8'))
        self.assertTrue(saved['recovery']['inherited_pending_batch'])
        self.assertEqual(saved['recovery']['current_available'],4)
        self.assertEqual(saved['recovery']['available_before'],2)
        self.assertEqual(saved['recovery']['available_after'],5)
        workflow.navigator.tap.assert_not_called()
        workflow.navigator._recover_and_verify.assert_not_called()

    def test_sufficient_bonus_still_preserves_pending_partial_batch(self):
        workflow, report = self._availability_workflow([5])
        workflow.navigator.recovery_evidence = {'status': 'started', 'ok_requested': True,
            'requested_bottles': 5, 'completed_bottles': 0, 'expected_increase': 5,
            'available_before': 2, 'available_after': 5}
        with tempfile.TemporaryDirectory() as directory:
            workflow.ensure_bonus_available('small', 5, report, Path(directory))
            saved = json.loads((Path(directory) / 'report.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['bonus']['availability'], 'sufficient')
        self.assertEqual(saved['recovery']['status'], 'started')
        self.assertEqual(saved['recovery']['completed_bottles'], 0)
        self.assertEqual(saved['recovery']['current_available'], 5)
        self.assertTrue(saved['recovery']['inherited_pending_batch'])
        workflow.navigator.tap.assert_not_called()
        workflow.navigator._recover_and_verify.assert_not_called()

    def test_recovery_still_insufficient_stops_without_extra_drinks_or_lowering_consumption(self):
        workflow,report = self._availability_workflow([0,1])
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError,'仍不足 5'):
                workflow.ensure_bonus_available('small',1,report,Path(directory))
        workflow.navigator._recover_and_verify.assert_called_once()
        workflow.navigator.ensure_auto.assert_not_called()
        workflow.adjust_bonus_count.assert_not_called()
        self.assertEqual(report['bonus']['consumption'],5)

    def test_uncertain_available_bonus_cannot_trigger_drinks(self):
        workflow,report = self._availability_workflow([])
        workflow.wait_available_bonus.side_effect = RuntimeError('当前体力未稳定确认')
        with self.assertRaisesRegex(RuntimeError,'当前体力未稳定确认'):
            workflow.ensure_bonus_available('large',1,report,Path('unused'))
        workflow.navigator.tap.assert_not_called()

    def test_available_bonus_rejects_conflicting_or_incomplete_numbers(self):
        workflow = SoloLive.__new__(SoloLive)
        workflow.ocr = Mock()
        frame = np.zeros((720,1280,3),np.uint8)
        for first,second in [(Reading('0/50',.99),Reading('10/50',.99)),
                             (Reading('1/50',.2),Reading('1',.99)),
                             (Reading('5/0',.99),Reading('5/0',.99))]:
            with self.subTest(first=first):
                workflow.ocr.read.side_effect = [first,second]
                with self.assertRaises(ValueError):
                    workflow.read_available_bonus(frame)
        workflow.ocr.read.side_effect = [Reading('1 0 / 5 0',.99),Reading('10/50',.99)]
        self.assertEqual(workflow.read_available_bonus(frame)[0],10)

    def test_available_bonus_requires_two_consecutive_preparation_readings(self):
        workflow = SoloLive.__new__(SoloLive)
        frame = np.zeros((720,1280,3),np.uint8)
        workflow.navigator = Mock(threshold=.83)
        workflow.navigator.match.return_value = (1.0,(0,0))
        workflow.screenshot = Mock(return_value=frame)
        workflow.pause = lambda _:None
        workflow.read_available_bonus = Mock(side_effect=[(value,Reading(f'{value}/50',.99)) for value in [0,1,1]])
        _,value,readings = workflow.wait_available_bonus()
        self.assertEqual(value,1)
        self.assertEqual(len(readings),2)
        self.assertEqual(workflow.screenshot.call_count,3)

    def test_pipeline_defaults_and_selected_options_pass_complete_agent_configuration(self):
        root = Path(__file__).resolve().parents[1]
        interface = json.loads((root/'interface.json').read_text(encoding='utf-8'))
        pipeline = json.loads((root/'resource/pipeline/solo_chart_live.json').read_text(encoding='utf-8'))
        self.assertNotIn('SoloChartLiveBonusConsumption', interface['option'])
        self.assertNotIn('SoloChartLiveOffset', interface['option'])
        context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
        node = 'SoloChartLiveDifficultyConfig'
        while node != 'SoloChartLiveRun':
            parameters = pipeline[node]['custom_action_param']
            argv = SimpleNamespace(task_detail=SimpleNamespace(task_id=31),custom_action_param=json.dumps(parameters))
            self.assertTrue(agent.ProjectSekaiSoloLiveConfig().run(context,argv))
            node = pipeline[node]['next'][0]
        self.assertEqual(agent._SETTINGS.pop(31),{'difficulty':'easy','song_mode':'current','count':1,
                                               'recovery_mode':'off','recovery_count':1})

    def test_successful_batch_retains_user_consumption_without_restoring_old_value(self):
        workflow = SoloLive.__new__(SoloLive)
        workflow.device = Mock()
        workflow.log = Mock()
        workflow.stop_requested = lambda:False
        workflow.completed_rounds = 0
        workflow.repository = Mock()
        workflow.repository.songs = {730:{'charts':{'easy':{'music_id':730,'difficulty':'easy','sha256':'hash',
                                                          'path':'chart.sus','total_note_count':1}}}}
        identity = SimpleNamespace(song_id=730,to_dict=lambda:{'song_id':730})
        workflow.select = Mock(return_value=(identity,np.zeros((1,1,3),np.uint8)))
        workflow.prepare_bonus = Mock(side_effect=lambda requested, report, *args, **kwargs: report.update(bonus={
            'requested_consumption': requested, 'consumption': 5, 'confirmed': True, 'original_consumption': 5,
            'readings': [{'text': '5'}, {'text': '5'}]}))
        workflow.prepare_playback = Mock()
        workflow.ensure_bonus_available = Mock()
        workflow.start = Mock(return_value=0)
        workflow.play = Mock()
        workflow.collect = lambda report,directory:report.update(completed=True)
        workflow.restore_bonus = Mock()
        with (tempfile.TemporaryDirectory() as directory, patch('project_sekai.solo_live.parse_sus') as parse,
              patch('project_sekai.solo_live.compile_touches',return_value=[])):
            workflow.report_root = Path(directory)
            parse.return_value.duration = 1.0
            reports = workflow.run(2,'easy','current',bonus_consumption=5,recovery_mode='small',recovery_count=3)
        self.assertEqual(workflow.completed_rounds,2)
        self.assertEqual(workflow.prepare_bonus.call_count,1)
        self.assertEqual(workflow.prepare_playback.call_count,2)
        self.assertEqual(reports[1]['bonus']['confirmation_source'], 'task_snapshot')
        self.assertEqual(reports[1]['bonus']['source_report'], reports[0]['report_path'])
        self.assertNotIn('readings', reports[1]['bonus'])
        self.assertTrue(all(call.args[0]==5 for call in workflow.prepare_bonus.call_args_list))
        self.assertTrue(all(call.args[:2]==('small',3) for call in workflow.ensure_bonus_available.call_args_list))
        workflow.restore_bonus.assert_not_called()

        with (tempfile.TemporaryDirectory() as directory, patch('project_sekai.solo_live.parse_sus') as parse,
              patch('project_sekai.solo_live.compile_touches', return_value=[])):
            workflow.report_root = Path(directory)
            parse.return_value.duration = 1.0
            fresh = workflow.run(1, 'easy', 'current', bonus_consumption=5)
        self.assertEqual(workflow.prepare_bonus.call_count, 2)
        self.assertEqual(fresh[0]['bonus']['confirmation_source'], 'observed')

    def test_partial_solo_result_continues_and_persists_before_progress(self):
        workflow = SoloLive.__new__(SoloLive)
        workflow.device = Mock()
        workflow.stop_requested = lambda: False
        workflow.completed_rounds = 0
        workflow.repository = Mock()
        workflow.repository.songs = {730: {'charts': {'easy': {
            'music_id': 730, 'difficulty': 'easy', 'sha256': 'hash', 'path': 'chart.sus', 'total_note_count': 259}}}}
        selected = SimpleNamespace(song_id=730, to_dict=lambda: {'song_id': 730})
        workflow.select = Mock(return_value=(selected, np.zeros((1, 1, 3), np.uint8)))
        workflow.prepare_task_bonus = Mock()
        workflow.prepare_playback = Mock()
        workflow.ensure_bonus_available = Mock()
        workflow.start = Mock(return_value=0)
        workflow.play = Mock()

        def collect(report, _):
            report.update(returned_home=True, live_status='cleared', completed=report['round'] != 1)
            if report['round'] == 1:
                report.update(result_status='unreadable', settlement_warning='数字未完整读取，继续下一曲')
            else:
                report.update(result_status='recorded', judgements=LiveResult(250, 5, 2, 1, 1).to_dict())

        workflow.collect = Mock(side_effect=collect)
        with tempfile.TemporaryDirectory() as directory, \
                patch('project_sekai.solo_live.parse_sus', return_value=SimpleNamespace(duration=1)), \
                patch('project_sekai.solo_live.compile_touches', return_value=[]):
            workflow.report_root = Path(directory)

            def log(message):
                if '已完成 1' in message:
                    self.assertTrue(Path(workflow.last_report['report_path']).is_file())
                    self.assertTrue((workflow.report_root / 'results.csv').is_file())

            workflow.log = Mock(side_effect=log)
            reports = workflow.run(2, 'easy', 'current')
        self.assertEqual(workflow.play.call_count, 2)
        self.assertEqual(workflow.completed_rounds, 1)
        self.assertFalse(reports[0]['completed'])
        self.assertTrue(reports[1]['completed'])

    def test_start_detects_two_preplay_zero_frames_without_extra_capture_or_ocr(self):
        frame = np.full((720, 1280, 3), 50, np.uint8)
        zero = np.zeros((29, 97, 3), np.uint8)
        zero[8:20, 43:49] = 255
        frame[8:37, 1090:1187] = zero
        workflow = SoloLive.__new__(SoloLive)
        workflow.device = Mock()
        workflow.navigator = Mock(threshold=.83, templates={'life_hud': frame})
        workflow.navigator.zero_life_template = ZeroLifeTemplate(zero)
        workflow.navigator.match.side_effect = lambda image, name, *args: (float(name == 'life_hud'), (0, 0))
        workflow.life_guard = LifeGuard()
        workflow.matcher = Mock()
        workflow.matcher.match.side_effect = ValueError('尚未确认开场身份')
        workflow.log = Mock()
        clock = SimpleNamespace(now=0.)
        def capture():
            clock.now += 1.
            return frame
        workflow.screenshot = Mock(side_effect=capture)
        workflow.pause = lambda _: None
        workflow.read_optional_number = Mock(side_effect=AssertionError('禁止追加OCR'))
        report = {"requested_difficulty": "easy"}
        with tempfile.TemporaryDirectory() as directory, patch('project_sekai.solo_live.time.monotonic', side_effect=lambda: clock.now), patch('project_sekai.solo_live.time.perf_counter', side_effect=lambda: clock.now), self.assertRaises(LifeDepleted):
            workflow.start(None, None, report, Path(directory), ready_action=lambda: None, opening_timeout=10.)
        self.assertEqual(workflow.screenshot.call_count, 3)
        workflow.device.screenshot.assert_not_called()
        workflow.read_optional_number.assert_not_called()
        self.assertTrue(report['preplay_death_confirmed'])

    def test_preplay_life_unknown_healthy_and_missing_template_never_mean_death(self):
        zero = np.zeros((29, 97, 3), np.uint8)
        zero[8:20, 43:49] = 255
        for mode in ('healthy_bar', 'number_1000', 'missing_life', 'missing_template', 'blank'):
            with self.subTest(mode=mode):
                workflow = SoloLive.__new__(SoloLive)
                frame = np.full((720, 1280, 3), 50, np.uint8)
                frame[8:37, 1090:1187] = zero
                if mode == 'healthy_bar': frame[40:48, 1008:1177] = (0, 255, 0)
                if mode == 'number_1000': frame[8:25, 1091:1101] = 255
                if mode == 'blank': frame[:] = 0
                workflow.life_guard = LifeGuard()
                workflow.navigator = Mock(threshold=.83, templates={'life_hud': frame})
                workflow.navigator.zero_life_template = None if mode == 'missing_template' else ZeroLifeTemplate(zero)
                workflow.navigator.match.return_value = (0. if mode == 'missing_life' else 1., (0, 0))
                workflow.read_optional_number = Mock(side_effect=AssertionError('禁止开场OCR'))
                report = {}
                for when in (10., 10.1, 12.):
                    workflow.observe_preplay_life(frame, when, report)
                self.assertFalse(workflow.life_guard.zero_confirmed)
                self.assertEqual(len(report['preplay_life_monitor']['samples']), 2)
                workflow.read_optional_number.assert_not_called()

    def test_preplay_followup_requires_current_release_and_healthy_recheck_does_not_exit(self):
        workflow = SoloLive.__new__(SoloLive)
        frame = np.full((720, 1280, 3), 50, np.uint8)
        zero = np.zeros((29, 97, 3), np.uint8)
        zero[8:20, 43:49] = 255
        frame[8:37, 1090:1187] = zero
        workflow.navigator = Mock(threshold=.83, templates={'life_hud': frame})
        workflow.navigator.zero_life_template = ZeroLifeTemplate(zero)
        workflow.navigator.match.return_value = (1., (0, 0))
        workflow.life_guard = LifeGuard()
        clock = SimpleNamespace(now=0.)
        workflow.pause = lambda duration: setattr(clock, 'now', clock.now + duration)
        workflow.screenshot = Mock(return_value=frame)
        report = {'engine': 'native', 'playback': {'engine': 'native', 'sent_actions': 0, 'release_confirmed': True}}
        workflow.observe_preplay_life(frame, 0., report)
        self.assertFalse(workflow.recheck_failed_preplay_life(report))
        workflow.screenshot.assert_not_called()
        report['playback']['release'] = {'release_proof': 'no-touch-possible-and-cleanup'}
        frame[40:48, 1008:1177] = (0, 255, 0)
        with patch('project_sekai.solo_live.time.perf_counter', side_effect=lambda: clock.now):
            self.assertFalse(workflow.recheck_failed_preplay_life(report))
        self.assertFalse(workflow.life_guard.zero_confirmed)
        self.assertEqual(workflow.life_guard.zero_streak, 0)
        workflow.screenshot.assert_called_once()

    def test_anchor_failure_with_one_strict_zero_rechecks_once_after_native_cleanup(self):
        workflow = SoloLive.__new__(SoloLive)
        workflow.device, workflow.log = Mock(), Mock()
        workflow.stop_requested = lambda: False
        workflow.repository = Mock()
        workflow.repository.songs = {730: {'charts': {'easy': {'music_id': 730, 'difficulty': 'easy',
            'sha256': 'hash', 'path': 'chart.sus', 'total_note_count': 1}}}}
        identity = SimpleNamespace(song_id=730, to_dict=lambda: {'song_id': 730})
        workflow.select = Mock(return_value=(identity, np.zeros((1, 1, 3), np.uint8)))
        workflow.prepare_task_bonus, workflow.ensure_bonus_available, workflow.collect = Mock(), Mock(), Mock()
        workflow.prepare_playback = Mock()
        frame = np.full((720, 1280, 3), 50, np.uint8)
        zero = np.zeros((29, 97, 3), np.uint8)
        zero[8:20, 43:49] = 255
        frame[8:37, 1090:1187] = zero
        workflow.navigator = Mock(threshold=.83, templates={'life_hud': frame})
        workflow.navigator.zero_life_template = ZeroLifeTemplate(zero)
        workflow.navigator.match.return_value = (1., (0, 0))
        clock = SimpleNamespace(now=0.)
        workflow.pause = lambda duration: setattr(clock, 'now', clock.now + duration)
        workflow.screenshot = Mock(return_value=frame)
        order = []
        player = Mock()
        player.report = {'engine': 'native', 'planned_actions': 1, 'sent_actions': 0, 'executed_actions': 0, 'release_confirmed': False}
        def failed(chart, identity, report, directory):
            workflow.observe_preplay_life(frame, clock.now, report)
            self.assertEqual(workflow.life_guard.zero_streak, 1)
            raise RuntimeError('原首音两秒门槛拒绝')
        def close():
            order.append('release')
            player.report.update(release_confirmed=True, release={'release_proof': 'no-touch-possible-and-cleanup'})
        player.close.side_effect = close
        workflow.start = Mock(side_effect=failed)
        workflow.exit_depleted_live = Mock(side_effect=lambda *args: order.append('exit'))
        with tempfile.TemporaryDirectory() as directory, patch('project_sekai.solo_live.time.perf_counter', side_effect=lambda: clock.now), patch('project_sekai.solo_live.parse_sus') as parse, patch('project_sekai.solo_live.compile_touches', return_value=[]), patch('project_sekai.native_player.NativePlayer', return_value=player), self.assertRaises(LifeDepleted):
            workflow.report_root = Path(directory)
            parse.return_value.duration = 1.
            workflow.run(1, 'easy', 'current', engine='native')
        self.assertEqual(order, ['release', 'exit'])
        self.assertGreaterEqual(clock.now, 2.)
        workflow.screenshot.assert_called_once()
        self.assertIn('原首音', workflow.last_report['error'])
        self.assertTrue(workflow.last_report['preplay_death_confirmed'])
        player.play.assert_not_called()
        workflow.collect.assert_not_called()

    def test_native_preplay_death_closes_original_player_before_solo_exit(self):
        workflow = SoloLive.__new__(SoloLive)
        workflow.device, workflow.log = Mock(), Mock()
        workflow.stop_requested = lambda: False
        workflow.repository = Mock()
        workflow.repository.songs = {730: {'charts': {'easy': {'music_id': 730, 'difficulty': 'easy',
            'sha256': 'hash', 'path': 'chart.sus', 'total_note_count': 1}}}}
        identity = SimpleNamespace(song_id=730, to_dict=lambda: {'song_id': 730})
        workflow.select = Mock(return_value=(identity, np.zeros((1, 1, 3), np.uint8)))
        workflow.prepare_task_bonus, workflow.ensure_bonus_available = Mock(), Mock()
        workflow.prepare_playback = Mock()
        workflow.collect = Mock()
        order = []
        player = Mock()
        player.report = {'engine': 'native', 'planned_actions': 1, 'sent_actions': 0, 'executed_actions': 0, 'release_confirmed': False}
        def dying(chart, identity, report, directory):
            report['preplay_death_confirmed'] = True
            report['death_confirmed'] = True
            workflow.life_guard.zero_confirmed = True
            raise LifeDepleted('开场两帧真实生命零')
        def close():
            order.append('release')
            player.report.update(release_confirmed=True, release={'release_proof': 'no-touch-possible-and-cleanup'})
        player.close.side_effect = close
        workflow.start = Mock(side_effect=dying)
        workflow.exit_depleted_live = Mock(side_effect=lambda *args: order.append('exit'))
        with tempfile.TemporaryDirectory() as directory, patch('project_sekai.solo_live.parse_sus') as parse, patch('project_sekai.solo_live.compile_touches', return_value=[]), patch('project_sekai.native_player.NativePlayer', return_value=player), self.assertRaises(LifeDepleted):
            workflow.report_root = Path(directory)
            parse.return_value.duration = 1.
            workflow.run(1, 'easy', 'current', engine='native')
        self.assertEqual(order, ['release', 'exit'])
        player.play.assert_not_called()
        workflow.collect.assert_not_called()
        self.assertEqual(workflow.completed_rounds, 0)

    def _opening_workflow(self, sequence, identities):
        workflow = SoloLive.__new__(SoloLive)
        workflow.device = Mock()
        workflow.navigator = Mock(threshold=.83)
        workflow.navigator.match.side_effect = lambda frame,name,*args:(1.0 if name=='playing' and frame is sequence[-1] else 0.0,(0,0))
        workflow.screenshot = Mock(side_effect=sequence)
        workflow.matcher = Mock()
        workflow.matcher.match.side_effect = identities
        workflow.log = Mock()
        workflow.pause = lambda _:None
        return workflow

    def test_preparation_and_one_clear_opening_frame_are_two_distinct_identity_checks(self):
        final,playing = [np.full((720,1280,3),value,np.uint8) for value in [140,160]]
        identity = SimpleNamespace(song_id=730,difficulty='normal',title='エメラルド',to_dict=lambda:{'song_id':730})
        workflow = self._opening_workflow([final,playing],[identity,ValueError('封面已消失')])
        chart = SimpleNamespace(first=SimpleNamespace(start=1.0),gestures=[])
        report = {'preparation_identity':identity.to_dict()}
        with tempfile.TemporaryDirectory() as directory, patch('project_sekai.solo_live.StartAnchor') as anchor:
            anchor.return_value.observe.return_value = 123.0
            anchor.return_value.samples = []
            anchor.return_value.fit = {}
            self.assertEqual(workflow.start(chart,identity,report,Path(directory)),123.0)
        self.assertEqual(report['final_identity']['song_id'],730)
        workflow.matcher.match.assert_called_once()
        workflow.log.assert_not_called()

    def test_dense_context_comes_from_actual_chart_before_and_after_final_identity(self):
        first = Gesture((Point(1., 2, 12, 2),), 'tap', critical=True)
        followers = tuple(Gesture((Point(1. + index * .016, 3, 10, 6),), 'trace', critical=True)
                          for index in range(1, 5))
        chart = Chart((first, *followers), 480, ((0., 120.),))
        for deferred in (False, True):
            with self.subTest(deferred=deferred):
                final, playing = [np.full((720, 1280, 3), value, np.uint8) for value in (140, 160)]
                identity = SimpleNamespace(song_id=814, difficulty='expert', title='song', to_dict=lambda: {'song_id': 814})
                workflow = self._opening_workflow([final, playing], [identity])
                loaded = Mock(return_value=chart)
                with tempfile.TemporaryDirectory() as directory, patch('project_sekai.solo_live.StartAnchor') as factory:
                    factory.return_value.observe.return_value = 123.
                    factory.return_value.samples = []
                    factory.return_value.fit = {}
                    self.assertEqual(workflow.start(None if deferred else chart, identity, {}, Path(directory),
                                                    on_final_identity=loaded if deferred else None), 123.)
                    self.assertEqual(factory.call_args.kwargs['dense_following_gestures'], followers)
                    self.assertEqual(factory.call_args.kwargs['simultaneous_gestures'], ())
                    factory.assert_called_once()
                if deferred:
                    loaded.assert_called_once_with(identity)

    def test_loading_frames_are_retried_until_opening_cover_and_title_match(self):
        loading,final,playing = [np.full((720,1280,3),value,np.uint8) for value in [255,140,160]]
        identity = SimpleNamespace(song_id=730,difficulty='normal',title='エメラルド',to_dict=lambda:{'song_id':730})
        workflow = self._opening_workflow([loading]*7+[final,playing],[ValueError('最佳=0.3')]*7+[identity])
        chart = SimpleNamespace(first=SimpleNamespace(start=1.0),gestures=[])
        report = {}
        with tempfile.TemporaryDirectory() as directory, patch('project_sekai.solo_live.StartAnchor') as anchor:
            anchor.return_value.observe.return_value = 123.0
            anchor.return_value.samples = []
            anchor.return_value.fit = {}
            self.assertEqual(workflow.start(chart,identity,report,Path(directory)),123.0)
        self.assertEqual(workflow.matcher.match.call_count,8)
        self.assertEqual(len(report['final_identity_attempts']),7)
        self.assertIsNotNone(workflow.loading_frame)

    def test_long_intro_failure_keeps_baseline_and_recent_anchor_evidence(self):
        final = np.full((80,160,3),10,np.uint8)
        frames = [np.full((80,160,3),value,np.uint8) for value in range(20,60)]
        identity = SimpleNamespace(song_id=730,difficulty='normal',title='エメラルド',to_dict=lambda:{'song_id':730})
        workflow = self._opening_workflow([final]+frames,[identity])
        workflow.navigator.match.side_effect = lambda frame,name,*args:(float(name=='playing' and frame is not final),(0,0))
        chart = SimpleNamespace(first=SimpleNamespace(start=4.0),gestures=[])
        report = {}
        with tempfile.TemporaryDirectory() as directory, patch('project_sekai.solo_live.StartAnchor') as anchor:
            anchor.return_value.observe.side_effect = [None]*39+[RuntimeError('首音失败')]
            anchor.return_value.samples = []
            with self.assertRaisesRegex(RuntimeError,'首音失败'):
                workflow.start(chart,identity,report,Path(directory))
            saved = report['anchor_frames']
            self.assertEqual(len(saved),32)
            self.assertTrue(np.array_equal(read_image(Path(directory)/saved[0]['path']),frames[0]))
            self.assertTrue(np.array_equal(read_image(Path(directory)/saved[-1]['path']),frames[-1]))

    def test_replaced_single_candidate_report_and_pinned_frame_follow_actual_state(self):
        final=np.full((80,160,3),10,np.uint8)
        frames=[np.full((80,160,3),value,np.uint8) for value in range(20,60)]
        identity=SimpleNamespace(song_id=730,difficulty='normal',title='song',to_dict=lambda:{'song_id':730})
        workflow=self._opening_workflow([final]+frames,[identity])
        workflow.navigator.match.side_effect=lambda frame,name,*args:(float(name=='playing' and frame is not final),(0,0))
        chart=SimpleNamespace(first=SimpleNamespace(start=4.),gestures=[]);report={}
        with tempfile.TemporaryDirectory() as directory,patch('project_sekai.solo_live.StartAnchor') as factory:
            anchor=factory.return_value;anchor.samples=[];anchor.failure_reason=None
            def observe(frame,when,**kwargs):
                if frame is frames[1]:anchor.samples=[(when,220.)]
                if frame is frames[3]:anchor.samples=[(when,221.)]
                if frame is frames[-1]:raise RuntimeError('候选未确认运动')
                return None
            anchor.observe.side_effect=observe
            with self.assertRaisesRegex(RuntimeError,'候选未确认运动'):
                workflow.start(chart,identity,report,Path(directory))
            self.assertEqual(report['anchor_attempt']['samples'],anchor.samples)
            self.assertEqual(report['anchor_attempt']['sample_count'],1)
            self.assertEqual(report['anchor_attempt']['sample_state'],'candidate_unconfirmed')
            saved=report['anchor_frames'];self.assertEqual(len(saved),33)
            pixels=[read_image(Path(directory)/item['path']) for item in saved]
            self.assertTrue(any(np.array_equal(image,frames[3]) for image in pixels))
            self.assertFalse(any(np.array_equal(image,frames[1]) for image in pixels))
            self.assertEqual([item['captured_at'] for item in saved],sorted(item['captured_at'] for item in saved))

    def test_anchor_evidence_keeps_all_early_accepted_samples_with_capacity(self):
        final = np.full((80, 160, 3), 10, np.uint8)
        frames = [np.full((80, 160, 3), value, np.uint8) for value in range(20, 60)]
        identity = SimpleNamespace(song_id=730, difficulty='normal', title='song', to_dict=lambda: {'song_id': 730})
        workflow = self._opening_workflow([final] + frames, [identity])
        workflow.navigator.match.side_effect = lambda frame, name, *args: (float(name == 'playing' and frame is not final), (0, 0))
        chart = SimpleNamespace(first=SimpleNamespace(start=4.), gestures=[])
        report = {}
        with tempfile.TemporaryDirectory() as directory, patch('project_sekai.solo_live.StartAnchor') as factory:
            anchor = factory.return_value
            anchor.samples = []
            anchor.failure_reason = None
            def observe(frame, when, **kwargs):
                if frame is frames[1] or frame is frames[2]:
                    anchor.samples.append((when, 213. if frame is frames[1] else 217.))
                if frame is frames[-1]:
                    raise RuntimeError('轨迹失败')
                return None
            anchor.observe.side_effect = observe
            with self.assertRaisesRegex(RuntimeError, '轨迹失败'):
                workflow.start(chart, identity, report, Path(directory))
            retained = [read_image(Path(directory) / item['path']) for item in report['anchor_frames']]
            for frame in frames[1:3]:
                self.assertTrue(any(np.array_equal(frame, image) for image in retained))
            self.assertLessEqual(len(retained), 65)
            self.assertEqual(report['anchor_attempt']['sample_frames_retained'], 2)
            self.assertFalse(report['anchor_attempt']['sample_frames_truncated'])

    def test_optional_room_deadline_provider_can_extend_only_pre_final_wait(self):
        first = np.full((80, 160, 3), 10, np.uint8)
        playing = np.full((80, 160, 3), 20, np.uint8)
        identity = SimpleNamespace(song_id=730, difficulty='normal', title='song', to_dict=lambda: {'song_id': 730})
        workflow = self._opening_workflow([first, playing], [ValueError('身份尚未明确'), identity])
        clock = SimpleNamespace(now=0.)
        workflow.pause = lambda _: setattr(clock, 'now', 2.)
        provider = Mock(side_effect=[1., 1000.])
        chart = SimpleNamespace(first=SimpleNamespace(start=4.), gestures=[])
        with tempfile.TemporaryDirectory() as directory, patch('project_sekai.solo_live.time.monotonic', side_effect=lambda: clock.now), patch('project_sekai.solo_live.StartAnchor') as factory:
            factory.return_value.samples = []
            factory.return_value.fit = {}
            factory.return_value.observe.return_value = 123.
            self.assertEqual(workflow.start(chart, identity, {}, Path(directory), ready_action=lambda: None,
                                           opening_timeout=1., opening_deadline_provider=provider), 123.)
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(workflow.matcher.match.call_count, 2)

    def test_room_deadline_provider_stops_after_final_identity_and_keeps_fixed_120_seconds(self):
        final = np.full((80, 160, 3), 10, np.uint8)
        playing = np.full((80, 160, 3), 20, np.uint8)
        identity = SimpleNamespace(song_id=730, difficulty='normal', title='song', to_dict=lambda: {'song_id': 730})
        workflow = self._opening_workflow([final, playing], [identity])
        clock = SimpleNamespace(now=0.)
        workflow.pause = lambda _: setattr(clock, 'now', 131.)
        provider = Mock(return_value=1000.)
        chart = SimpleNamespace(first=SimpleNamespace(start=4.), gestures=[])
        with tempfile.TemporaryDirectory() as directory, patch('project_sekai.solo_live.time.monotonic', side_effect=lambda: clock.now), patch('project_sekai.solo_live.StartAnchor') as factory:
            factory.return_value.samples = []
            factory.return_value.observe.return_value = 123.
            with self.assertRaisesRegex(RuntimeError, '锚点未确认'):
                workflow.start(chart, identity, {}, Path(directory), ready_action=lambda: None,
                               opening_timeout=180, opening_deadline_provider=provider)
            factory.return_value.observe.assert_not_called()
        self.assertEqual(provider.call_count, 1)
        workflow.matcher.match.assert_called_once()

    def test_success_defers_encoding_and_retains_early_candidate_frame(self):
        final=np.full((80,160,3),10,np.uint8)
        frames=[np.full((80,160,3),value,np.uint8) for value in range(20,60)]
        identity=SimpleNamespace(song_id=730,difficulty='normal',title='song',to_dict=lambda:{'song_id':730})
        workflow=self._opening_workflow([final]+frames,[identity])
        workflow.navigator.match.side_effect=lambda frame,name,*args:(float(name=='playing' and frame is not final),(0,0))
        chart=SimpleNamespace(first=SimpleNamespace(start=4.),gestures=[]);report={}
        with tempfile.TemporaryDirectory() as directory,patch('project_sekai.solo_live.StartAnchor') as factory,patch('project_sekai.solo_live.write_image') as write:
            anchor=factory.return_value;anchor.samples=[];anchor.fit={}
            def observe(frame,when,**kwargs):
                if frame is frames[1]:anchor.samples=[(when,220.)]
                if frame is frames[-1]:
                    anchor.samples.append((when,300.))
                    return 123.
                return None
            anchor.observe.side_effect=observe
            self.assertEqual(workflow.start(chart,identity,report,Path(directory)),123.)
            write.assert_not_called()
            self.assertEqual(report['anchor_attempt']['sample_state'],'accepted')
            self.assertEqual(len(workflow.anchor_frames),33)
            self.assertTrue(any(np.array_equal(image,frames[1]) for _,image in workflow.anchor_frames))
