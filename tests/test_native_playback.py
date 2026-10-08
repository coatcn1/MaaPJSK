from dataclasses import asdict
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from project_sekai import native_engine
from project_sekai.chart_player import Touch, compile_touches
from project_sekai.sus_chart import Chart, Gesture, Point
from project_sekai.native_player import NativePlayer
from project_sekai.calibration import CalibrationProfiles, CalibrationRunner, adjusted_offset, validation_passed, verify_profile_anchor
from project_sekai.performance_settings import PerformanceSettings, migrate_settings
from project_sekai.life_monitor import LifeDepleted


class Clock:
    def __init__(self):
        self.value = 1000.

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class ScriptDevice:
    max_contacts, max_x, max_y, surface_rotation = 10, 1280, 720, 0

    def __init__(self, clock):
        self.clock = clock
        self.tail = clock()
        self.pending = []
        self.records = []
        self.commands = []
        self.connected = True
        self.active = set()
        self.release_diagnostics = {"release_proof": "current-reset-jlog-and-cleanup"}
        self.last_release_error = None
        self.release_ok = True
        self.corrupt = False

    def start(self):
        pass

    def publish(self, text):
        for line in text.splitlines():
            self.commands.append(line)
            start = max(self.tail, self.clock())
            cost = (int(line.split()[1]) / 1000 if line.startswith('w ') else 0) + .00004
            end = start + cost
            event = {'st':(start-900)*1000,'et':(end-900)*1000,'c':cost*1000,'cmd':line}
            if self.corrupt and line.startswith('d '):
                event['cmd'] = 'd 0 1 1 50'
            self.pending.append((end, 'jlog '+json.dumps(event), line))
            self.tail = end

    def log_records_since(self, cursor):
        while self.pending and self.pending[0][0] <= self.clock():
            end, line, command = self.pending.pop(0)
            self.records.append((line,end))
            if command.startswith('d '): self.active.add(int(command.split()[1]))
            if command.startswith('u '): self.active.discard(int(command.split()[1]))
        return len(self.records), self.records[cursor:]

    def stop(self):
        self.active.clear()
        self.pending.clear()
        self.connected = False
        return self.release_ok


@unittest.skipUnless(native_engine.available(), '先运行 scripts/build-native.ps1 才能验证 Native')
class NativeTests(unittest.TestCase):
    def test_whole_song_receipts_cover_chords_slides_flick_coordinates_and_release(self):
        events = (Touch(.6,2,0,'down',190),Touch(.6,2,1,'down',1000),
                  Touch(.624,0,1,'up'),Touch(.8,1,0,'move',640),Touch(.9,1,0,'move',700,486),Touch(1,0,0,'up'))
        clock = Clock()
        device = ScriptDevice(clock)
        player = NativePlayer(None,events,Path('.'),lambda:False,device=device,clock=clock,sleeper=clock.sleep)
        player.prepare()
        report = player.play(1000,20)
        player.close()
        self.assertEqual((report['sent_actions'],report['executed_actions']), (6,6))
        self.assertTrue(report['release_confirmed'])
        self.assertIn('m 0 700 486 50',device.commands)
        self.assertFalse(device.active)
        self.assertGreater(report['calibration_chunks'],0)
        self.assertLess(report['execution_drift_ms_max_abs'],10)

    def test_mismatched_current_run_receipt_fails_and_can_be_released(self):
        clock = Clock(); device = ScriptDevice(clock)
        player = NativePlayer(None,(Touch(.3,2,1,'down',500),Touch(.4,0,1,'up')),Path('.'),lambda:False,
                              device=device,clock=clock,sleeper=clock.sleep)
        player.prepare(); device.corrupt=True
        with self.assertRaisesRegex(RuntimeError,'顺序不匹配'): player.play(1000,0)
        player.close()
        self.assertTrue(player.report['release_confirmed'])
        self.assertLess(player.report['executed_actions'],2)

    def test_stop_does_not_publish_next_window_and_release_failure_is_visible(self):
        clock=Clock(); device=ScriptDevice(clock)
        events=(Touch(.1,2,0,'down',500),Touch(3,0,0,'up'))
        player=NativePlayer(None,events,Path('.'),lambda:clock()>1000.35,device=device,clock=clock,sleeper=clock.sleep)
        player.prepare()
        with self.assertRaises(InterruptedError): player.play(1000,0)
        count=len(device.commands); device.release_ok=False
        with self.assertRaisesRegex(RuntimeError,'释放未确认'): player.close()
        self.assertEqual(len(device.commands),count)
        self.assertFalse(player.report['release_confirmed'])

    def test_life_sampling_is_available_during_continuous_hold_and_stops_input(self):
        clock=Clock();device=ScriptDevice(clock);samples=[]
        events=(Touch(.1,2,0,'down',500),Touch(4,0,0,'up'))
        def sample():
            samples.append(clock());clock.sleep(.15)
            if len(samples)==2:raise LifeDepleted('生命归零')
        player=NativePlayer(None,events,Path('.'),lambda:False,device=device,clock=clock,sleeper=clock.sleep,
                            idle_observer=sample)
        player.prepare()
        with self.assertRaises(LifeDepleted):player.play(1000,0)
        self.assertLess(player.report['sent_actions'],2)
        player.close()
        self.assertTrue(player.report['release_confirmed'])
        self.assertFalse(device.active)
        self.assertIn('execution_drift_ms_p95',player.report)
        self.assertIn('queue_underflows',player.report)

    def test_portrait_touch_surface_rotates_both_flick_coordinates(self):
        timeline=native_engine.module().Timeline([asdict(Touch(.1,2,0,'down',640,570)),asdict(Touch(.12,1,0,'move',670,514)),asdict(Touch(.2,0,0,'up'))])
        timeline.start(10,10,0)
        script=native_engine.module().ScriptCompiler().compile(timeline.next(10),720,1280,1)
        self.assertIn('d 0 149 640 50',script['lines'])
        self.assertIn('m 0 205 670 50',script['lines'])

    def test_cooperative_observer_rate_and_queue_margin_cover_a_continuous_hold(self):
        clock=Clock();device=ScriptDevice(clock);samples=[]
        events=(Touch(.1,2,0,'down',500),Touch(8,0,0,'up'))
        def sample():
            samples.append(clock())
            clock.sleep(.20)
        player=NativePlayer(None,events,Path('.'),lambda:False,device=device,clock=clock,sleeper=clock.sleep,
                            idle_observer=sample,observation_interval=2.0,observation_budget=.30)
        player.prepare()
        player.play(1000,0)
        player.close()
        self.assertGreaterEqual(len(samples),3)
        self.assertTrue(all(second-first>=2 for first,second in zip(samples,samples[1:])))
        self.assertEqual(player.report['queue_underflows'],0)
        self.assertEqual(player.report['executed_actions'],2)
        self.assertEqual(player.report['observation']['samples'],len(samples))

    def test_observed_cost_increases_the_required_queue_margin_without_burst_sampling(self):
        clock=Clock();device=ScriptDevice(clock);samples=[]
        events=(Touch(.1,2,0,'down',500),Touch(10,0,0,'up'))
        def sample():
            samples.append(clock())
            clock.sleep(.45)
        player=NativePlayer(None,events,Path('.'),lambda:False,device=device,clock=clock,sleeper=clock.sleep,
                            idle_observer=sample,observation_interval=2.0,observation_budget=.30)
        player.prepare()
        player.play(1000,0)
        player.close()
        self.assertGreaterEqual(len(samples),3)
        self.assertEqual(player.report['queue_underflows'],0)
        self.assertAlmostEqual(player.report['observation']['max_cost_ms'],450)
        self.assertGreaterEqual(player.report['observation']['minimum_headroom_ms'],600)

    def test_late_start_and_invalid_touch_plan_are_rejected_before_publication(self):
        module=native_engine.module()
        with self.assertRaises(ValueError): module.Timeline([asdict(Touch(.1,1,0,'move',600))])
        timeline=module.Timeline([asdict(Touch(.1,2,0,'down',600)),asdict(Touch(.2,0,0,'up'))])
        with self.assertRaisesRegex(RuntimeError,'第一音'): timeline.start(10,11,0)


def good_report(perfect=100, fast=0, late=0):
    return {'completed':True,'live_status':'cleared','report_path':'round/report.json',
            'preparation_identity':{'song_id':730},'start_anchor':{'trajectory_rate':3.},
            'judgements':{'perfect':perfect,'great':100-perfect,'good':0,'bad':0,'miss':0,'total':100,
                          'perfect_rate':perfect/100,'total_matches_chart':True,'fast':fast,'late':late},
            'playback':{'engine':'native','planned_actions':200,'sent_actions':200,'executed_actions':200,
                        'release_confirmed':True,'latency_offsets':{}}}


class SettingsCalibrationTests(unittest.TestCase):
    def write_warm_fixture(self, root, name, offset, *, fast=0, late=17, **metadata):
        runs=root/'solo-chart-runs';runs.mkdir(exist_ok=True)
        report=good_report(fast=fast,late=late);report['timing_offset_ms']=offset
        report_path=runs/f'{name}.json';report_path.write_text(json.dumps(report))
        directory=root/'calibration-runs'/name;directory.mkdir(parents=True)
        (directory/'session.json').write_text(json.dumps({'environment':{},'difficulty':'normal',
            'rounds':[{'stage':'formal-validation','report':str(report_path)}],**metadata}))
        return report_path

    def test_unknown_failed_warm_does_not_override_manual_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            self.write_warm_fixture(root,'old',-24)
            self.write_warm_fixture(root,'new',-27,late=4,initial_offset_source='warm',initial_offset_ms=-24)
            calls=[]
            workflow=SimpleNamespace(log=lambda _:None,stop_requested=lambda:False,
                run=lambda *args,**kwargs:calls.append(args[3]) or [good_report()])
            session=CalibrationRunner(workflow,CalibrationProfiles(root/'profiles'),{},
                PerformanceSettings(touch_offset_ms=-51),root/'calibration-runs').run('normal','current')
            self.assertEqual(calls,[-51,-51])
            self.assertEqual(session['initial_offset_source'],'manual')
            self.assertEqual(session['warm_seed_offset_ms'],-51)

    def test_same_manual_seed_continues_unbalanced_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            report_path=self.write_warm_fixture(root,'old',7,warm_seed_offset_ms=10)
            calls=[]
            workflow=SimpleNamespace(log=lambda _:None,stop_requested=lambda:False,
                run=lambda *args,**kwargs:calls.append(args[3]) or [good_report()])
            session=CalibrationRunner(workflow,CalibrationProfiles(root/'profiles'),{},
                PerformanceSettings(touch_offset_ms=10),root/'calibration-runs').run('normal','current')
            self.assertEqual(calls,[7,7])
            self.assertEqual(session['warm_seed_offset_ms'],10)
            self.assertEqual(session['warm_start_report'],str(report_path.resolve()))

    def test_failed_warm_seed_requires_exact_manual_and_known_integer_lineage(self):
        for metadata,expected in [
                ({'warm_seed_offset_ms':20},False),({'warm_seed_offset_ms':True},False),
                ({'warm_seed_offset_ms':None,'initial_offset_source':'manual','initial_offset_ms':10},False),
                ({'initial_offset_source':'manual','initial_offset_ms':10},True),
                ({'initial_offset_source':'explicit','initial_offset_ms':10},True),
                ({'initial_offset_source':'explicit','initial_offset_ms':-23},False),
                ({'initial_offset_source':'manual','initial_offset_ms':True},False),
                ({'initial_offset_source':'warm','initial_offset_ms':10},False)]:
            with self.subTest(metadata=metadata),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);self.write_warm_fixture(root,'old',7,**metadata)
                runner=CalibrationRunner(SimpleNamespace(),CalibrationProfiles(root/'profiles'),{},
                    PerformanceSettings(touch_offset_ms=10),root/'calibration-runs')
                self.assertEqual(runner.warm_candidate('normal') is not None,expected)

    def test_unknown_better_rank_does_not_hide_eligible_failed_warm(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            self.write_warm_fixture(root,'new',-27,fast=4,late=7)
            self.write_warm_fixture(root,'old',7,warm_seed_offset_ms=10)
            runner=CalibrationRunner(SimpleNamespace(),CalibrationProfiles(root/'profiles'),{},
                PerformanceSettings(touch_offset_ms=10),root/'calibration-runs')
            self.assertEqual(runner.warm_candidate('normal')['offset'],7)

    def test_balanced_legacy_warm_keeps_unknown_seed_and_explicit_resets_it(self):
        for explicit,seed,source in [(None,None,'warm'),(-23,-23,'explicit')]:
            with self.subTest(explicit=explicit),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);self.write_warm_fixture(root,'old',7,late=1)
                workflow=SimpleNamespace(log=lambda _:None,stop_requested=lambda:False,
                    run=lambda *args,**kwargs:[good_report()])
                session=CalibrationRunner(workflow,CalibrationProfiles(root/'profiles'),{},
                    PerformanceSettings(touch_offset_ms=10),root/'calibration-runs',initial_offset_ms=explicit).run('normal','current')
                self.assertEqual(session['warm_seed_offset_ms'],seed)
                self.assertEqual(session['initial_offset_source'],source)

    def test_invalid_latest_round_does_not_hide_valid_older_round_in_same_session(self):
        for invalid in ['feedback','missing','json']:
            with self.subTest(invalid=invalid),tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                old=self.write_warm_fixture(root,'old',-30,fast=10,late=10,warm_seed_offset_ms=0)
                newer=old.parent/'newer.json'
                if invalid=='feedback':
                    report=good_report(fast=None,late=None);report['timing_offset_ms']=-24
                    newer.write_text(json.dumps(report))
                elif invalid=='json':newer.write_text('{invalid')
                session_path=root/'calibration-runs'/'old'/'session.json'
                session=json.loads(session_path.read_text())
                session['rounds'].append({'report':str(newer)})
                session_path.write_text(json.dumps(session))
                runner=CalibrationRunner(SimpleNamespace(),CalibrationProfiles(root/'profiles'),{},
                    PerformanceSettings(),root/'calibration-runs')
                candidate=runner.warm_candidate('normal')
                self.assertIsNotNone(candidate)
                self.assertEqual(candidate['offset'],-30)

    def test_trace_before_same_lane_tap_reuses_held_contact_instead_of_early_down(self):
        chart=Chart((Gesture((Point(1,4,3,5),),'trace'),Gesture((Point(1.1,6,3,5),),'trace'),
                     Gesture((Point(1.2,6,3,1),),'tap')),480,((0,120),))
        events=compile_touches(chart)
        target=round(chart.gestures[1].points[0].x)
        self.assertFalse(any(event.kind=='down' and event.x==target and event.time<1.2 for event in events))
        self.assertTrue(any(event.kind=='move' and event.x==target and abs(event.time-1.08)<1e-6 for event in events))
        self.assertEqual(sum(event.kind=='down' for event in events),2)

    def test_migration_preserves_active_instance_offset_and_stamina_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/'config/instances').mkdir(parents=True)
            (root/'appsettings.json').write_text(json.dumps({'Instances.LastActive':'mine'}))
            instance={'TaskItems':[{'name':'SoloChartLive','option':[{'name':'SoloChartLiveOffset','data':{'Value':'80'}},
                        {'name':'SoloChartLiveBonusConsumption','index':6}]}]}
            (root/'config/instances/mine.json').write_text(json.dumps(instance))
            migrate_settings(root)
            settings=PerformanceSettings.load(root/'config/performance-settings.json')
            self.assertEqual((settings.touch_offset_ms,settings.bonus_consumption),(80,5))
            original=(root/'config/performance-settings.json').read_bytes()
            instance['TaskItems'][0]['option'][0]['data']['Value']='20'
            (root/'config/instances/mine.json').write_text(json.dumps(instance))
            migrate_settings(root)
            self.assertEqual((root/'config/performance-settings.json').read_bytes(),original)

    def test_performance_settings_reject_booleans_and_invalid_quantity(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json'
            for key,value in [('touch_offset_ms',True),('bonus_consumption',True),('bonus_consumption',11),('engine','unknown')]:
                payload=asdict(PerformanceSettings());payload[key]=value;path.write_text(json.dumps(payload))
                with self.assertRaises(ValueError): PerformanceSettings.load(path)

    def test_pjsk_late_feedback_moves_timing_earlier_and_missing_feedback_fails(self):
        self.assertEqual(adjusted_offset(0,good_report(30,late=70)['judgements']),-48)
        self.assertEqual(adjusted_offset(0,good_report(30,fast=70)['judgements']),48)
        self.assertEqual(adjusted_offset(5,good_report()['judgements']),5)
        with self.assertRaises(ValueError): adjusted_offset(0,{'fast':None,'late':0,'total':100})
        bad=good_report(99,fast=1)['judgements'];bad['bad']=1
        self.assertEqual(adjusted_offset(-24,bad),-24)

    def test_authorized_five_stamina_calibration_keeps_requested_quantity(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);report=good_report();report['bonus']={'original_consumption':0}
            calls=[];restored=[]
            def run(*args,**kwargs):
                calls.append((args,kwargs));return [report]
            workflow=SimpleNamespace(log=lambda _:None,run=run,stop_requested=lambda:False,
                restore_calibration_bonus=lambda amount,path:restored.append(amount) or {'confirmed':True})
            runner=CalibrationRunner(workflow,CalibrationProfiles(root/'profiles'),{},PerformanceSettings(),root/'sessions',
                                      test_bonus_consumption=5,initial_offset_ms=-23)
            session=runner.run('master','current')
            self.assertEqual(session['status'],'accepted')
            self.assertEqual(restored,[5])
            self.assertTrue(all(kwargs['bonus_consumption']==5 and kwargs['recovery_mode']=='off' for _,kwargs in calls))
            self.assertEqual(calls[0][0][3],-23)
            self.assertEqual(session['initial_offset_source'],'explicit')
            self.assertEqual(session['initial_offset_ms'],-23)

    def test_calibration_two_rounds_share_one_task_snapshot_and_next_run_refreshes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshots = []
            def run(*args, **kwargs):
                snapshot = kwargs['_bonus_snapshot']
                snapshots.append(snapshot)
                snapshot.setdefault('consumption', 5)
                return [good_report()]
            workflow = SimpleNamespace(log=lambda _: None, run=run, stop_requested=lambda: False)
            runner = CalibrationRunner(workflow, CalibrationProfiles(root / 'profiles'), {},
                                       PerformanceSettings(), root / 'sessions')
            runner.run('easy', 'current')
            runner.run('easy', 'current')
            self.assertIs(snapshots[0], snapshots[1])
            self.assertIs(snapshots[2], snapshots[3])
            self.assertIsNot(snapshots[0], snapshots[2])

    def test_calibration_validation_navigation_failure_retains_confirmed_task_consumption(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = []
            workflow = SimpleNamespace(log=lambda _: None, stop_requested=lambda: False,
                                       restore_calibration_bonus=Mock())
            def run(*args, **kwargs):
                if reports:
                    workflow.last_report = {'error': '正式验证选曲失败'}
                    raise RuntimeError('正式验证选曲失败')
                kwargs['_bonus_snapshot'].update(consumption=5, confirmed=True, original_consumption=5)
                report = good_report()
                report['bonus'] = {'consumption': 5, 'confirmed': True, 'original_consumption': 5}
                reports.append(report)
                workflow.last_report = report
                return [report]
            workflow.run = run
            runner = CalibrationRunner(workflow, CalibrationProfiles(root / 'profiles'), {},
                                       PerformanceSettings(), root / 'sessions', test_bonus_consumption='current')
            with self.assertRaisesRegex(RuntimeError, '正式验证选曲失败'):
                runner.run('easy', 'current')
            workflow.restore_calibration_bonus.assert_not_called()

    def test_formal_validation_requires_real_full_execution_and_correct_chart_total(self):
        self.assertTrue(validation_passed(good_report(95)))
        for key,value in [('total_matches_chart',False)]:
            report=good_report();report['judgements'][key]=value
            self.assertFalse(validation_passed(report))
        for key,value in [('executed_actions',199),('release_confirmed',False)]:
            report=good_report();report['playback'][key]=value
            self.assertFalse(validation_passed(report))

    def test_balanced_feedback_accepts_complex_chart_without_precision_gate(self):
        report=good_report();report['requested_difficulty']='master'
        report['judgements'].update(perfect=20,great=20,good=10,bad=10,miss=40,perfect_rate=.2,fast=20,late=21)
        self.assertTrue(validation_passed(report))

    def test_unbalanced_or_incomplete_feedback_rejects_even_perfect_chart(self):
        for fast,late in [(0,17),(None,0),(-1,0),(True,0),(0,False),(0,None)]:
            with self.subTest(fast=fast,late=late):
                report=good_report(fast=fast,late=late)
                self.assertFalse(validation_passed(report))
        self.assertTrue(validation_passed(good_report(fast=0,late=0)))
        report=good_report();report['judgements'].pop('fast')
        self.assertFalse(validation_passed(report))
        for key,value in [('completed',False),('live_status','failed')]:
            report=good_report();report[key]=value
            self.assertFalse(validation_passed(report))

    def test_balanced_low_perfect_profile_save_and_load_use_same_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            report=good_report(20,fast=10,late=11)
            report['judgements'].update(great=20,good=10,bad=10,miss=40)
            profile={'schema_version':1,'accepted':True,'environment':{},'difficulty':'master',
                     'offset_ms':0,'validation_report':report,'latency_offsets':{}}
            profiles=CalibrationProfiles(Path(directory))
            profiles.save(profile)
            self.assertEqual(profiles.load({},'master'),profile)
            report['judgements'].update(fast=0,late=17)
            with self.assertRaises(ValueError):profiles.save(profile)

    def test_warm_candidate_ranks_feedback_balance_before_perfect_rate(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);runs=root/'solo-chart-runs';runs.mkdir()
            for name,report,offset in [('20261005-new',good_report(100,late=17),-24),
                                       ('20261005-old',good_report(20,fast=10,late=10),-51)]:
                report['timing_offset_ms']=offset;report_path=runs/f'{name}.json'
                report_path.write_text(json.dumps(report))
                session=root/'calibration-runs'/name;session.mkdir(parents=True)
                (session/'session.json').write_text(json.dumps({'environment':{},'difficulty':'normal',
                    'rounds':[{'report':str(report_path)}]}))
            runner=CalibrationRunner(SimpleNamespace(),CalibrationProfiles(root/'profiles'),{},
                PerformanceSettings(),root/'calibration-runs')
            self.assertEqual(runner.warm_candidate('normal')['offset'],-51)

    def test_calibration_is_two_zero_stamina_rounds_then_activates_only_after_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); calls=[]
            def run(*args,**kwargs):
                calls.append((args,kwargs))
                return [good_report(90,late=10) if len(calls)==1 else good_report()]
            workflow=SimpleNamespace(log=lambda _:None,run=run,stop_requested=lambda:False)
            profiles=CalibrationProfiles(root/'profiles')
            runner=CalibrationRunner(workflow,profiles,{'engine':'native'},PerformanceSettings(touch_offset_ms=80),root/'sessions')
            session=runner.run('normal','random')
            self.assertEqual(session['status'],'accepted')
            self.assertEqual([args[2] for args,_ in calls],['random','current'])
            self.assertTrue(all(kwargs['bonus_consumption']==0 and kwargs['recovery_mode']=='off' for _,kwargs in calls))
            self.assertEqual(calls[0][0][3],80)
            self.assertEqual(calls[1][0][3],68)
            self.assertEqual(profiles.load({'engine':'native'},'normal')['offset_ms'],-12)
            self.assertIsNone(profiles.load({'engine':'another-device'},'normal'))

    def test_new_environment_calibration_keeps_negative_manual_offset(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); calls=[]
            settings=PerformanceSettings(touch_offset_ms=-51,use_calibration_profile=False)
            snapshot=asdict(settings)
            workflow=SimpleNamespace(log=lambda _:None,stop_requested=lambda:False,
                run=lambda *args,**kwargs:calls.append(args[3]) or [good_report()])
            profiles=CalibrationProfiles(root/'profiles')
            session=CalibrationRunner(workflow,profiles,{'version':'new'},settings,root/'sessions').run('normal','current')
            self.assertEqual(calls,[-51,-51])
            self.assertEqual(session['initial_offset_ms'],-51)
            self.assertEqual(session['initial_offset_source'],'manual')
            self.assertEqual(profiles.load({'version':'new'},'normal')['offset_ms'],0)
            self.assertEqual(asdict(settings),snapshot)

    def test_failed_validation_preserves_existing_accepted_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); profiles=CalibrationProfiles(root/'profiles')
            initial={'schema_version':1,'accepted':True,'environment':{},'difficulty':'easy','offset_ms':15,
                     'validation_report':good_report(),'latency_offsets':{}}
            path=profiles.save(initial); before=path.read_bytes()
            calls=[];settings=PerformanceSettings(touch_offset_ms=-51)
            snapshot=asdict(settings)
            workflow=SimpleNamespace(log=lambda _:None,run=lambda *args,**kwargs:calls.append(args[3]) or [good_report(80,late=1) if len(calls)==1 else good_report(100,late=17)],stop_requested=lambda:False)
            runner=CalibrationRunner(workflow,profiles,{},settings,root/'sessions')
            with self.assertRaisesRegex(RuntimeError,'未通过'):runner.run('easy','current')
            self.assertEqual(path.read_bytes(),before)
            self.assertEqual(calls,[-36,-36])
            self.assertEqual(asdict(settings),snapshot)
            session=json.loads(next((root/'sessions').glob('*/session.json')).read_text())
            self.assertEqual(session['initial_offset_source'],'profile')
            self.assertEqual(session['initial_offset_ms'],-36)

    def test_calibration_offset_priority_and_old_environment_isolation(self):
        for environment, explicit, expected, source in [
                ({'version':'old'},None,-12,'warm'),
                ({'version':'new'},None,-51,'manual'),
                ({'version':'old'},-23,-23,'explicit')]:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                root=Path(directory);session_dir=root/'calibration-runs'/'old';session_dir.mkdir(parents=True)
                runs=root/'solo-chart-runs';runs.mkdir()
                report=good_report();report['timing_offset_ms']=-12
                report_path=runs/'report.json';report_path.write_text(json.dumps(report))
                (session_dir/'session.json').write_text(json.dumps({'environment':{'version':'old'},
                    'difficulty':'normal','rounds':[{'report':str(report_path)}]}))
                calls=[];settings=PerformanceSettings(touch_offset_ms=-51)
                workflow=SimpleNamespace(log=lambda _:None,stop_requested=lambda:False,
                    run=lambda *args,**kwargs:calls.append(args[3]) or [good_report()])
                session=CalibrationRunner(workflow,CalibrationProfiles(root/'profiles'),environment,settings,
                    root/'calibration-runs',initial_offset_ms=explicit).run('normal','current')
                self.assertEqual(calls,[expected,expected])
                self.assertEqual(session['initial_offset_source'],source)
                self.assertEqual(session['initial_offset_ms'],expected)

    def test_explicit_candidate_overrides_profile_without_changing_manual_setting(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);profiles=CalibrationProfiles(root/'profiles')
            profiles.save({'schema_version':1,'accepted':True,'environment':{},'difficulty':'normal',
                'offset_ms':15,'validation_report':good_report(),'latency_offsets':{}})
            calls=[];settings=PerformanceSettings(touch_offset_ms=-51)
            workflow=SimpleNamespace(log=lambda _:None,stop_requested=lambda:False,
                run=lambda *args,**kwargs:calls.append(args[3]) or [good_report()])
            session=CalibrationRunner(workflow,profiles,{},settings,root/'sessions',initial_offset_ms=-23).run('normal','current')
            self.assertEqual(calls,[-23,-23])
            self.assertEqual(session['initial_offset_source'],'explicit')
            self.assertEqual(profiles.load({},'normal')['offset_ms'],28)
            self.assertEqual(settings.touch_offset_ms,-51)

    def test_speed_mismatch_prevents_using_profile_before_native_start(self):
        verify_profile_anchor({'anchor_rate':3},good_report())
        report=good_report();report['start_anchor']['trajectory_rate']=4
        with self.assertRaisesRegex(RuntimeError,'流速'):verify_profile_anchor({'anchor_rate':3},report)

    def test_calibration_restores_original_game_quantity_before_enabling_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); report=good_report();report['bonus']={'original_consumption':5}
            restored=[]
            workflow=SimpleNamespace(log=lambda _:None,run=lambda *args,**kwargs:[report],stop_requested=lambda:False,
                                     restore_calibration_bonus=lambda amount,path:restored.append(amount) or {'confirmed':True})
            session=CalibrationRunner(workflow,CalibrationProfiles(root/'profiles'),{},PerformanceSettings(),root/'sessions').run('easy','current')
            self.assertEqual(session['status'],'accepted')
            self.assertEqual(restored,[0])
            def fail(*args):raise RuntimeError('保存消耗未确认')
            workflow.restore_calibration_bonus=fail
            profiles=CalibrationProfiles(root/'failure-profiles')
            with self.assertRaisesRegex(RuntimeError,'候选未启用'):
                CalibrationRunner(workflow,profiles,{},PerformanceSettings(),root/'failed-sessions').run('easy','current')
            self.assertIsNone(profiles.load({},'easy'))

    def test_rejected_candidate_warms_rehearsal_only_for_same_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);session=root/'calibration-runs'/'old';session.mkdir(parents=True)
            runs=root/'solo-chart-runs';runs.mkdir(); report=good_report(85);report['timing_offset_ms']=-12
            report_path=runs/'report.json';report_path.write_text(json.dumps(report))
            (session/'session.json').write_text(json.dumps({'environment':{'device':'one'},'difficulty':'normal',
                'status':'rejected','rounds':[{'report':str(report_path)}]}))
            workflow=SimpleNamespace()
            profiles=CalibrationProfiles(root/'profiles')
            runner=CalibrationRunner(workflow,profiles,{'device':'one'},PerformanceSettings(),root/'calibration-runs')
            self.assertEqual(runner.warm_candidate('normal')['offset'],-12)
            self.assertIsNone(runner.warm_candidate('master'))
            runner.environment={'device':'two'}
            self.assertIsNone(runner.warm_candidate('normal'))
            self.assertIsNone(profiles.load({'device':'one'},'normal'))


if __name__=='__main__':
    unittest.main()
