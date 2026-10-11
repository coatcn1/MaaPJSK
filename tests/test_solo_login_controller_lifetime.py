import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent import solo_live
from project_sekai import game_login


class ReplacingRemoteTasker:
    stopping = False

    def __init__(self):
        self.generation = 0

    @property
    def controller(self):
        # MaaFramework 5.10.2 每次获取控制器都会替换唯一远程代理，使上一句柄失效。
        self.generation += 1
        return SimpleNamespace(tasker=self, generation=self.generation)


class BorrowedDevice:
    def __init__(self, controller):
        self.controller = controller

    def screenshot(self):
        if self.controller.generation != self.controller.tasker.generation:
            raise RuntimeError('远程控制器句柄已被再次访问替换')
        return 'frame'


class SoloLoginControllerLifetimeTests(unittest.TestCase):
    def test_login_and_live_share_one_borrowed_controller(self):
        tasker = ReplacingRemoteTasker()
        context = SimpleNamespace(tasker=tasker)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config').mkdir()
            (root / 'config/chart-sync.json').write_text(json.dumps({'output_root': 'charts'}), encoding='utf-8')
            with patch.object(solo_live, 'MaaDevice', BorrowedDevice), \
                    patch.object(game_login, 'MaaDevice', BorrowedDevice), \
                    patch.object(solo_live, 'SoloLive', side_effect=lambda device, *args, **kwargs: SimpleNamespace(device=device)), \
                    patch.object(game_login, 'Navigator'), \
                    patch.object(game_login, 'GameLogin') as login:
                workflow = solo_live.create_workflow(context, root)
                self.assertEqual(workflow.device.screenshot(), 'frame')
                self.assertEqual(tasker.generation, 1)
                self.assertIs(login.call_args.args[0], workflow.device)
                login.return_value.run.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
