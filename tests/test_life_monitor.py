from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from project_sekai.life_monitor import LifeDepleted, LifeGuard, ZeroLifeTemplate
from project_sekai.solo_live import SoloLive


class LifeTests(unittest.TestCase):
    def frame(self, hue=None, width=169):
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        if hue is not None:
            color = cv2.cvtColor(np.array([[[hue, 200, 240]]], dtype=np.uint8), cv2.COLOR_HSV2BGR)[0, 0]
            frame[40:48, 1008:1008 + width] = color
        return frame

    def test_green_yellow_and_critical_red_are_alive(self):
        for hue, width in ((60,169),(25,40),(0,3),(175,3)):
            guard = LifeGuard()
            def unexpected(_):
                self.fail('有填充时不能把条带误读为零')
            self.assertFalse(guard.observe(self.frame(hue,width), unexpected))

    def test_zero_requires_two_readable_frames_and_unknown_resets_streak(self):
        guard = LifeGuard(); frame = self.frame()
        self.assertFalse(guard.observe(frame, lambda _: True))
        self.assertFalse(guard.observe(frame, lambda _: False))
        self.assertFalse(guard.observe(frame, lambda _: True))
        self.assertTrue(guard.observe(frame, lambda _: True))

    def test_cooperative_bar_fill_uses_the_lower_strip_and_avoids_numeric_ocr(self):
        frame = np.zeros((720,1280,3),np.uint8)
        frame[48:56,1008:1177] = (125,255,120)
        guard = LifeGuard(bar_area=(1008,48,1177,56))
        reader = Mock(return_value=True)
        for _ in range(2):
            self.assertFalse(guard.observe(frame,reader))
        reader.assert_not_called()

    def test_zero_template_rejects_nonzero_values_with_a_trailing_zero(self):
        def value_image(text):
            image = np.zeros((29,97,3),np.uint8)
            (width,_),_ = cv2.getTextSize(text,cv2.FONT_HERSHEY_SIMPLEX,.7,2)
            cv2.putText(image,text,(95-width,23),cv2.FONT_HERSHEY_SIMPLEX,.7,(255,255,255),2)
            return image
        template = ZeroLifeTemplate(value_image('0'))
        for text in ('10','100','1000',''):
            self.assertLess(template.score(value_image(text),(0,0,97,29)),.90,text)
        self.assertGreater(template.score(value_image('0'),(0,0,97,29)),.99)

    def test_pause_quit_confirmation_and_home_are_ordered(self):
        frames = [np.full((720,1280,3), value, dtype=np.uint8) for value in range(4)]
        workflow = object.__new__(SoloLive); calls=[]
        workflow.screenshot=lambda:frames.pop(0)
        workflow.pause=lambda _:None;workflow.log=lambda _:None
        workflow.navigator=SimpleNamespace(threshold=.83,_check_stop=lambda:None,
            tap=lambda x,y,reason:calls.append((x,y)),
            match=lambda frame,name,*area:(1 if (int(frame[0,0,0])==0 and name=='playing')
                                            or (int(frame[0,0,0])==3 and name=='home') else 0,(0,0)))
        workflow.dialog_action=lambda frame,labels:{1:(450,450),2:(760,450)}.get(int(frame[0,0,0]))
        report={'completed':True}
        with tempfile.TemporaryDirectory() as directory:
            workflow.exit_depleted_live(report,Path(directory))
        self.assertEqual(calls,[(1223,44),(450,450),(760,450)])
        self.assertFalse(report['completed'])
        self.assertTrue(report['life_exit']['confirmed'])

    def test_depleted_round_is_released_then_exited_without_collecting_or_starting_next(self):
        workflow=object.__new__(SoloLive)
        workflow.device=Mock();workflow.log=Mock();workflow.stop_requested=lambda:False
        workflow.repository=Mock()
        workflow.repository.songs={730:{'charts':{'master':{'music_id':730,'difficulty':'master','sha256':'hash',
                                                         'path':'master.sus','total_note_count':1}}}}
        identity=SimpleNamespace(song_id=730,to_dict=lambda:{'song_id':730})
        workflow.select=Mock(return_value=(identity,np.zeros((1,1,3),np.uint8)))
        workflow.prepare_bonus=Mock();workflow.ensure_bonus_available=Mock();workflow.start=Mock(return_value=0)
        workflow.collect=Mock();workflow.exit_depleted_live=Mock()
        def die(events,epoch,offset,report,directory):
            report['playback']={'release_confirmed':True}
            workflow.life_guard.zero_confirmed=True
            raise LifeDepleted('生命归零')
        workflow.play=die
        with (tempfile.TemporaryDirectory() as directory, patch('project_sekai.solo_live.parse_sus') as parse,
              patch('project_sekai.solo_live.compile_touches',return_value=[])):
            workflow.report_root=Path(directory);parse.return_value.duration=1
            with self.assertRaises(LifeDepleted):workflow.run(2,'master','current',bonus_consumption=5)
        workflow.exit_depleted_live.assert_called_once()
        workflow.collect.assert_not_called()
        self.assertEqual(workflow.completed_rounds,0)
        self.assertEqual(workflow.select.call_count,1)


if __name__ == '__main__':
    unittest.main()
