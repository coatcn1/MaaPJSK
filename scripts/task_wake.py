from __future__ import annotations

import ctypes
import sys
import threading


ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
TASK_EXECUTION_STATE = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED


def windows_execution_state_api():
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    function = kernel32.SetThreadExecutionState
    function.argtypes = [ctypes.c_uint32]
    function.restype = ctypes.c_uint32
    return function


class TaskWakeScope:
    """仅在当前任务生命周期内保持Windows系统与显示器唤醒。"""

    def __init__(self, *, set_execution_state=None, platform=None):
        self._api = set_execution_state
        self._platform = sys.platform if platform is None else platform
        self.active = False
        self.previous_flags = None
        self.owner_thread_id = None
        self.last_release_error = None

    def __enter__(self):
        if self.active:
            raise RuntimeError('任务防息屏作用域不能重复进入')
        if self._platform != 'win32':
            return self
        if self._api is None:
            self._api = windows_execution_state_api()
        previous = int(self._api(TASK_EXECUTION_STATE))
        if previous == 0:
            raise OSError('申请Windows任务防息屏失败：SetThreadExecutionState返回0；尚未连接设备')
        self.previous_flags = previous
        self.owner_thread_id = threading.get_ident()
        self.active = True
        return self

    def close(self):
        if not self.active:
            return
        # Windows执行状态属于调用线程；恢复原值而非无条件清空，保留外层已有请求。
        if threading.get_ident() != self.owner_thread_id:
            raise RuntimeError('任务防息屏必须由申请线程恢复')
        if int(self._api(self.previous_flags)) == 0:
            raise OSError('恢复Windows任务防息屏原状态失败：SetThreadExecutionState返回0')
        self.active = False

    def __exit__(self, error_type, error, traceback):
        try:
            self.close()
        except BaseException as release_error:
            self.last_release_error = release_error
            if error is None:
                raise
            # 恢复失败保留为原异常的附注，不能掩盖先发生的演出错误、用户停止或KeyboardInterrupt。
            error.add_note(f'任务防息屏恢复异常：{type(release_error).__name__}: {release_error}')
        return False
