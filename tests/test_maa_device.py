import os
import unittest
from unittest.mock import Mock, patch

import numpy as np

from project_sekai.maa_device import MaaDevice


class MaaDeviceScreenshotTests(unittest.TestCase):
    def device(self, value, *, succeeded=True, frame=None):
        job = Mock(succeeded=succeeded)
        job.wait.return_value = job
        job.get.return_value = np.full((720, 1280, 3), 50, np.uint8) if frame is None else frame
        controller = Mock()
        controller.post_screencap.return_value = job
        with patch.dict(os.environ, {}, clear=True):
            if value is not None:
                os.environ['MAAPJSK_SCREENSHOT_SUCCESS_GUARD'] = value
            device = MaaDevice(controller)
        return device, job

    def test_failed_job_never_reads_cached_frame_when_candidate_enabled(self):
        device, job = self.device('1', succeeded=False)
        with self.assertRaisesRegex(RuntimeError, '截图作业失败'):
            device.screenshot()
        job.get.assert_not_called()

    def test_guard_defaults_on_and_only_explicit_zero_disables(self):
        for value in (None, '', '0', '1', 'true', 'other'):
            with self.subTest(value=value):
                device, job = self.device(value, succeeded=False)
                if value != '0':
                    with self.assertRaises(RuntimeError):
                        device.screenshot()
                    job.get.assert_not_called()
                else:
                    self.assertIs(device.screenshot(), job.get.return_value)

    def test_guard_freezes_when_device_is_created(self):
        for initial, changed in ((None, '0'), ('', '0'), ('1', '0'), ('true', '0'), ('other', '0'), ('0', '1')):
            with self.subTest(initial=initial):
                device, job = self.device(initial, succeeded=False)
                with patch.dict(os.environ, {'MAAPJSK_SCREENSHOT_SUCCESS_GUARD': changed}):
                    if initial != '0':
                        with self.assertRaises(RuntimeError):
                            device.screenshot()
                        job.get.assert_not_called()
                    else:
                        self.assertIs(device.screenshot(), job.get.return_value)

    def test_success_keeps_original_shape_gate(self):
        for value in ('0', '1'):
            with self.subTest(value=value):
                device, job = self.device(value)
                self.assertIs(device.screenshot(), job.get.return_value)
                job.get.return_value = np.zeros((360, 640, 3), np.uint8)
                with self.assertRaisesRegex(RuntimeError, '1280×720'):
                    device.screenshot()


if __name__ == '__main__':
    unittest.main()
