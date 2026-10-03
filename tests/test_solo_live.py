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
from project_sekai.solo_live import LiveResult, SoloLive, numeric, is_light_mode, append_result_index
from project_sekai.song_identity import SongMatcher, title_score
from scripts.test_solo_live import controller_options


class SoloTests(unittest.TestCase):
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
        workflow.navigator.match=lambda frame,name: (1.0 if (name=='home' and frame is sequence[-1]) or
            (name=='live_clear' and frame is clear) or (name=='live_failed' and frame is failed) else 0.0,(0,0))
        workflow.navigator._check_stop=lambda:None
        workflow.screenshot=Mock(side_effect=sequence)
        workflow.pause=lambda _:None
        workflow.read_result=lambda frame: LiveResult(250,5,2,1,1) if frame is judge else None
        return workflow

    def test_failed_banner_keeps_round_failed_after_reading_judgements(self):
        failed,judge,other,home=[np.full((720,1280,3),value,np.uint8) for value in [180,190,200,210]]
        workflow=self._collection([failed,other,judge,judge,other,home],None,failed,judge)
        report={'chart':{'total_note_count':259}}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError,'演出失败'):
                workflow.collect(report,Path(directory))
        self.assertEqual(report['judgements']['perfect'],250)
        self.assertEqual(report['live_status'],'failed')
        self.assertFalse(report.get('completed',False))

    def test_home_after_animation_tap_prevents_another_back(self):
        clear,judge,other,home=[np.full((720,1280,3),value,np.uint8) for value in [180,190,200,210]]
        workflow=self._collection([clear,other,judge,judge,home,home],clear,None,judge)
        report={'chart':{'total_note_count':259}}
        with tempfile.TemporaryDirectory() as directory:
            workflow.collect(report,Path(directory))
        self.assertTrue(report['completed'])
        self.assertEqual(workflow.device.back.call_count,1)
        self.assertEqual(workflow.device.tap.call_args.args,(1279,719))

    def test_judgement_page_arriving_in_confirmation_frame_is_read_before_back(self):
        clear,judge,home=[np.full((720,1280,3),value,np.uint8) for value in [180,190,210]]
        workflow=self._collection([clear,judge,judge,home,home],clear,None,judge)
        report={'chart':{'total_note_count':259}}
        with tempfile.TemporaryDirectory() as directory:
            workflow.collect(report,Path(directory))
        self.assertTrue(report['completed'])
        self.assertEqual(report['judgements']['total'],259)
        self.assertEqual(workflow.device.back.call_count,0)

    def test_live_clear_banner_in_confirmation_frame_counts_only_after_result_and_home(self):
        other,clear,judge,home=[np.full((720,1280,3),value,np.uint8) for value in [170,180,190,210]]
        workflow=self._collection([other,clear,judge,judge,home,home],clear,None,judge)
        report={'chart':{'total_note_count':259}}
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
        workflow.prepare_bonus = Mock()
        workflow.ensure_bonus_available = Mock()
        workflow.start = Mock(return_value=0)
        workflow.play = Mock()
        workflow.collect = lambda report,directory:report.update(completed=True)
        workflow.restore_bonus = Mock()
        with (tempfile.TemporaryDirectory() as directory, patch('project_sekai.solo_live.parse_sus') as parse,
              patch('project_sekai.solo_live.compile_touches',return_value=[])):
            workflow.report_root = Path(directory)
            parse.return_value.duration = 1.0
            workflow.run(2,'easy','current',bonus_consumption=5,recovery_mode='small',recovery_count=3)
        self.assertEqual(workflow.completed_rounds,2)
        self.assertEqual(workflow.prepare_bonus.call_count,2)
        self.assertTrue(all(call.args[0]==5 for call in workflow.prepare_bonus.call_args_list))
        self.assertTrue(all(call.args[:2]==('small',3) for call in workflow.ensure_bonus_available.call_args_list))
        workflow.restore_bonus.assert_not_called()

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
