from __future__ import annotations


def validate_retry_count(value):
    if type(value) is not int or not 0 <= value <= 99:
        raise ValueError('演奏失败重试次数必须为 0 到 99 的整数')
    return value


class FailureRetryBudget:
    def __init__(self, limit, *, on_change=None):
        self.limit = validate_retry_count(limit)
        self.failure_count = 0
        self.retries_used = 0
        self._failures = set()
        self._reserved = set()
        self._on_change = on_change
        self._dirty = False

    def snapshot(self):
        return {'retry_limit': self.limit, 'failure_count': self.failure_count,
                'retries_used': self.retries_used}

    def _publish(self):
        if not self._dirty:
            return
        if self._on_change is not None:
            self._on_change(self.snapshot())
        self._dirty = False

    def publish(self):
        self._publish()

    def record_failure(self, event_id, *, publish=True):
        # 身份来自本轮内存事件，禁日志或报告换目录都不会重复计入同一次真实死亡。
        if event_id not in self._failures:
            self._failures.add(event_id)
            self.failure_count += 1
            self._dirty = True
        if publish:
            self._publish()

    def reserve_retry(self, event_id):
        if event_id not in self._failures:
            raise ValueError('未确认真实死亡，不能预约重试')
        if event_id in self._reserved or self.retries_used >= self.limit:
            return False
        self._reserved.add(event_id)
        self.retries_used += 1
        self._dirty = True
        # 状态保存失败时抛错，调用者不能在缺少可审计计数的情况下开始下一局。
        self._publish()
        return True
