import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from project_sekai.failure_retry import FailureRetryBudget
from project_sekai.failure_summary import FailureSummary, attach_failure_summary, finish_failure_summary


class FailureSummaryTests(unittest.TestCase):
    def seed(self, root, summary_id='b' * 32, execution_id='a' * 32):
        parameters = {'execution_id': execution_id, 'summary_id': summary_id}
        path = root / 'config/play-failure-summaries' / (summary_id + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'schema_version': 1, **parameters, 'mode': 'solo',
            'agent_attached': False, 'finalized': False, 'failure_count': 0, 'retries_used': 0}), encoding='utf-8')
        return parameters, path

    def test_disabled_diagnostics_still_persist_failure_and_stop_finalization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parameters, path = self.seed(root)
            with patch('project_sekai.diagnostics._disabled', True):
                summary = FailureSummary(root, parameters, 'solo', 1)
                budget = FailureRetryBudget(1, on_change=summary.update)
                budget.record_failure('attempt-a')
                budget.reserve_retry('attempt-a')
                budget.record_failure('attempt-b')
                summary.finish('stopped', counters=budget.snapshot())
            value = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual((value['failure_count'], value['retries_used'], value['status']), (2, 1, 'stopped'))
            self.assertTrue(value['finalized'])

    def test_missing_or_old_execution_never_attaches_to_the_current_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parameters, path = self.seed(root)
            wrong = {**parameters, 'execution_id': 'c' * 32}
            with self.assertRaises(ValueError):
                FailureSummary(root, wrong, 'solo', 1)
            with self.assertRaises(ValueError):
                FailureSummary(root, parameters, 'cooperative', 1)
            FailureSummary(root, parameters, 'solo', 1)
            with self.assertRaises(ValueError):
                FailureSummary(root, parameters, 'solo', 1)
            path.unlink()
            with self.assertRaises(FileNotFoundError):
                FailureSummary(root, parameters, 'solo', 1)
            with self.assertRaises(ValueError):
                attach_failure_summary(root, '{}', 'solo', 1)

    def test_reserved_identity_keys_allow_preserved_custom_action_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parameters, path = self.seed(root)
            summary = attach_failure_summary(root, json.dumps({**parameters, 'preserved': 7}), 'solo', 1)
            self.assertEqual(summary.value['summary_id'], parameters['summary_id'])

    def test_initial_and_final_io_failures_are_explicit_and_never_claim_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parameters, path = self.seed(root)
            with patch('project_sekai.failure_summary._write_json', side_effect=OSError('read only')):
                with self.assertRaises(OSError):
                    FailureSummary(root, parameters, 'solo', 1)
            summary = FailureSummary(root, parameters, 'solo', 1)
            with patch('project_sekai.failure_summary._write_json', side_effect=OSError('read only')):
                self.assertFalse(finish_failure_summary(summary, None, 'stopped'))
            self.assertFalse(json.loads(path.read_text(encoding='utf-8'))['finalized'])


if __name__ == '__main__':
    unittest.main()
