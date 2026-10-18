from __future__ import annotations

import json
from pathlib import Path
import re

from .chart_catalog import _write_json
from .failure_retry import validate_retry_count


class FailureSummary:
    def __init__(self, root, parameters, mode, retry_limit):
        validate_retry_count(retry_limit)
        if not isinstance(parameters, dict) or not {'execution_id', 'summary_id'} <= set(parameters):
            raise ValueError('失败汇总缺少本次 MFA 执行身份')
        parameters = {key: parameters[key] for key in ('execution_id', 'summary_id')}
        if mode not in {'solo', 'cooperative'} or any(
                not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{32}', value) is None
                for value in parameters.values()):
            raise ValueError('失败汇总执行身份无效')
        self.path = Path(root) / 'config/play-failure-summaries' / (parameters['summary_id'] + '.json')
        value = json.loads(self.path.read_text(encoding='utf-8-sig'))
        if (not isinstance(value, dict) or value.get('schema_version') != 1
                or any(value.get(key) != expected for key, expected in parameters.items())
                or value.get('mode') != mode or value.get('agent_attached') is not False
                or value.get('finalized') is not False):
            raise ValueError('失败汇总与本次 MFA 任务不一致或已被使用')
        self.value = {**value, 'agent_attached': True, 'retry_limit': retry_limit,
                      'failure_count': 0, 'retries_used': 0, 'status': 'running'}
        # 这是任务状态而非诊断日志；连接前验证实际可写，禁日志不能关闭它。
        self._save()

    def _save(self):
        _write_json(self.path, self.value)

    def update(self, counters):
        self.value.update(counters)
        self._save()

    def finish(self, status, *, counters=None):
        if status not in {'succeeded', 'failed', 'stopped'}:
            raise ValueError('失败汇总终态无效')
        if counters is not None:
            self.value.update(counters)
        self.value.update(status=status, finalized=True)
        self._save()


def attach_failure_summary(root, raw_parameters, mode, retry_limit):
    parameters = json.loads(raw_parameters or '{}')
    if parameters == {} and retry_limit == 0:
        # 兼容未接入 MFA 终态桥的旧调用与零重试独立验收；启用重试时必须有真实执行身份。
        return None
    return FailureSummary(root, parameters, mode, retry_limit)


def finish_failure_summary(summary, workflow, status):
    if summary is None:
        return True
    budget = getattr(workflow, 'failure_retry_budget', None)
    try:
        counters = budget.snapshot() if budget is not None else {}
        for key in ('performed_rounds', 'completed_rounds'):
            value = getattr(workflow, key, 0)
            counters[key] = value if type(value) is int and value >= 0 else 0
        summary.finish(status, counters=counters)
        return True
    except Exception as error:
        # 终态桥失败必须让任务失败；MFA 将显示最后确认计数及统计未完成警告。
        print(f'任务失败汇总保存失败：{type(error).__name__}: {error}', flush=True)
        return False
