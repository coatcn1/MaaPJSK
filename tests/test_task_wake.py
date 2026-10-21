import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import test_solo_live as cli
from scripts.task_wake import ES_CONTINUOUS, ES_DISPLAY_REQUIRED, ES_SYSTEM_REQUIRED, TaskWakeScope


REQUIRED = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED


class TaskWakeTests(unittest.TestCase):
    def test_acquire_and_restore_exact_previous_flags_once(self):
        previous = ES_CONTINUOUS | ES_DISPLAY_REQUIRED
        api = Mock(side_effect=[previous, REQUIRED])
        scope = TaskWakeScope(set_execution_state=api, platform='win32')
        with scope:
            self.assertTrue(scope.active)
            self.assertEqual(scope.previous_flags, previous)
        scope.close()
        self.assertFalse(scope.active)
        self.assertEqual([call.args[0] for call in api.call_args_list], [REQUIRED, previous])

    def test_acquisition_failure_never_enters_task_or_claims_active(self):
        api = Mock(return_value=0)
        scope = TaskWakeScope(set_execution_state=api, platform='win32')
        with self.assertRaisesRegex(OSError, '申请.*失败'):
            with scope:
                self.fail('失败申请不能进入任务')
        self.assertFalse(scope.active)
        api.assert_called_once_with(REQUIRED)

    def test_exception_keyboard_interrupt_and_stop_restore_before_propagating(self):
        for error in (RuntimeError('任务失败'), KeyboardInterrupt(), InterruptedError('STOP')):
            with self.subTest(error=type(error).__name__):
                api = Mock(side_effect=[ES_CONTINUOUS, REQUIRED])
                with self.assertRaises(type(error)) as raised:
                    with TaskWakeScope(set_execution_state=api, platform='win32'):
                        raise error
                self.assertIs(raised.exception, error)
                self.assertEqual(api.call_args.args, (ES_CONTINUOUS,))

    def test_restore_failure_is_visible_without_hiding_original_error(self):
        original = InterruptedError('原停止')
        scope = TaskWakeScope(set_execution_state=Mock(side_effect=[ES_CONTINUOUS, 0]), platform='win32')
        with self.assertRaises(InterruptedError) as raised:
            with scope:
                raise original
        self.assertIs(raised.exception, original)
        self.assertIsNotNone(scope.last_release_error)
        self.assertTrue(any('恢复' in note for note in original.__notes__))
        with self.assertRaisesRegex(OSError, '恢复.*失败'):
            with TaskWakeScope(set_execution_state=Mock(side_effect=[ES_CONTINUOUS, 0]), platform='win32'):
                pass

    def test_release_rejects_another_thread_without_changing_native_state(self):
        api, errors = Mock(side_effect=[ES_CONTINUOUS, REQUIRED]), []
        scope = TaskWakeScope(set_execution_state=api, platform='win32')
        with scope:
            def wrong_thread():
                try:
                    scope.close()
                except RuntimeError as error:
                    errors.append(error)
            worker = threading.Thread(target=wrong_thread)
            worker.start()
            worker.join()
            self.assertEqual(len(errors), 1)
            self.assertEqual(api.call_count, 1)
        self.assertEqual(api.call_count, 2)

    def test_non_windows_does_not_claim_active_or_call_windows_api(self):
        api = Mock()
        with TaskWakeScope(set_execution_state=api, platform='linux') as scope:
            self.assertFalse(scope.active)
        api.assert_not_called()

    def test_cli_holds_wake_before_connection_through_complete_cleanup(self):
        for error in (None, RuntimeError('演出错误'), KeyboardInterrupt(), InterruptedError('STOP')):
            with self.subTest(error=type(error).__name__ if error else None):
                calls = []
                def api(flags):
                    calls.append(('power', flags))
                    return ES_CONTINUOUS if len(calls) == 1 else REQUIRED
                scope = TaskWakeScope(set_execution_state=api, platform='win32')
                def execution(arguments):
                    self.assertTrue(scope.active)
                    calls.append(('connection', None))
                    try:
                        if error:
                            raise error
                    finally:
                        for phase in ('post_stop', 'release', 'agent_exit'):
                            self.assertTrue(scope.active)
                            calls.append((phase, None))
                with patch.object(sys, 'argv', ['test_solo_live.py', '--inspect-only']), \
                        patch('scripts.task_wake.TaskWakeScope', return_value=scope), \
                        patch.object(cli, '_run', side_effect=execution), contextlib.redirect_stdout(io.StringIO()):
                    if error:
                        with self.assertRaises(type(error)):
                            cli.main()
                    else:
                        cli.main()
                self.assertEqual(calls[0], ('power', REQUIRED))
                self.assertEqual(calls[-2:], [('agent_exit', None), ('power', ES_CONTINUOUS)])

    def test_cli_wake_failure_prevents_connection(self):
        scope = TaskWakeScope(set_execution_state=Mock(return_value=0), platform='win32')
        with patch.object(sys, 'argv', ['test_solo_live.py', '--inspect-only']), \
                patch('scripts.task_wake.TaskWakeScope', return_value=scope), patch.object(cli, '_run') as execution:
            with self.assertRaises(OSError):
                cli.main()
        execution.assert_not_called()

    def test_real_cli_cleanup_waits_for_agent_even_when_disconnect_fails(self):
        scope = TaskWakeScope(set_execution_state=Mock(side_effect=[ES_CONTINUOUS, REQUIRED]), platform='win32')
        disconnected = RuntimeError('IPC断开失败')
        order = []
        def while_awake(label, error=None):
            self.assertTrue(scope.active)
            order.append(label)
            if error:
                raise error
        controller, resource, tasker, client, agent = [Mock() for _ in range(5)]
        controller.post_connection.return_value.wait.return_value.succeeded = True
        resource.post_bundle.return_value.wait.return_value.succeeded = True
        tasker.bind.return_value = True
        tasker.post_task.return_value.wait.return_value.succeeded = True
        client.connect.return_value = client.bind.return_value = client.set_timeout.return_value = True
        client.identifier = 'offline-client'
        client.disconnect.side_effect = lambda: while_awake('disconnect', disconnected)
        agent.wait.side_effect = lambda **kwargs: while_awake('agent_exit')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'config/instances').mkdir(parents=True)
            (root / 'config/instances/default.json').write_text(json.dumps({'AdbDevice': {
                'AdbPath': 'offline-adb', 'AdbSerial': 'offline-not-a-device'}}), encoding='utf-8')
            with patch.object(sys, 'argv', ['test_solo_live.py', '--mfa-root', str(root)]), \
                    patch.object(sys, 'path', list(sys.path)), patch.dict(os.environ, {}), \
                    patch('scripts.task_wake.TaskWakeScope', return_value=scope), \
                    patch.object(cli, 'controller_options', return_value={}), \
                    patch('maa.controller.AdbController', return_value=controller), \
                    patch('maa.resource.Resource', return_value=resource), patch('maa.tasker.Tasker', return_value=tasker), \
                    patch('maa.agent_client.AgentClient', return_value=client), \
                    patch('project_sekai.maa_device.MaaDevice'), \
                    patch('project_sekai.diagnostics.load_policy', return_value=SimpleNamespace(disable_logging=True)), \
                    patch('project_sekai.diagnostics.make_directory'), \
                    patch('subprocess.run', return_value=SimpleNamespace(returncode=0)), \
                    patch('subprocess.Popen', return_value=agent), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(RuntimeError) as raised:
                    cli.main()
        self.assertIs(raised.exception, disconnected)
        self.assertEqual(order, ['disconnect', 'agent_exit'])
        self.assertFalse(scope.active)

    def test_agent_cleanup_errors_keep_original_stop_and_still_wait(self):
        original = InterruptedError('原STOP')
        client, agent = Mock(), Mock()
        client.disconnect.side_effect = RuntimeError('断开失败')
        with TaskWakeScope(set_execution_state=Mock(side_effect=[ES_CONTINUOUS, REQUIRED]), platform='win32') as scope:
            agent.wait.side_effect = lambda **kwargs: self.assertTrue(scope.active)
            cli._finish_acceptance_agent(client, agent, primary_error=original)
        agent.wait.assert_called_once_with(timeout=8)
        self.assertTrue(any('断开失败' in note for note in original.__notes__))


if __name__ == '__main__':
    unittest.main()
