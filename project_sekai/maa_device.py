from __future__ import annotations

import re
import ctypes
from typing import Any

import numpy as np
from maa.define import MaaBool, MaaControllerHandle, MaaCtrlId, MaaStringBufferHandle
from maa.library import Library


def _bind_shell_api() -> None:
    # MaaFramework Python Binding 5.10.2 未声明这两个函数的参数类型，64 位句柄会被截断。
    framework = Library.framework()
    framework.MaaControllerPostShell.restype = MaaCtrlId
    framework.MaaControllerPostShell.argtypes = [MaaControllerHandle, ctypes.c_char_p, ctypes.c_int32]
    framework.MaaControllerGetShellOutput.restype = MaaBool
    framework.MaaControllerGetShellOutput.argtypes = [MaaControllerHandle, MaaStringBufferHandle]


class MaaDevice:
    """通过 MFA 已连接的 MaaFramework Controller 控制当前设备。"""

    def __init__(self, controller: Any) -> None:
        self.controller = controller

    def screenshot(self) -> np.ndarray:
        frame = self.controller.post_screencap().wait().get()
        if frame is None or frame.shape[:2] != (720, 1280):
            shape = getattr(frame, "shape", None)
            raise RuntimeError(f"MFA 截图不是 1280×720：{shape}")
        return frame

    def tap(self, x: int, y: int) -> None:
        if not self.controller.post_click(x, y).wait().succeeded:
            raise RuntimeError(f"MFA 控制器点击失败：({x}, {y})")

    def back(self) -> None:
        self.controller.post_click_key(4).wait()

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 450) -> None:
        self.controller.post_swipe(x1, y1, x2, y2, duration_ms).wait()

    def shell(self, command: str) -> str:
        _bind_shell_api()
        return str(self.controller.post_shell(command, 5000).wait().get())

    def preflight(self) -> None:
        self.screenshot()
        density = self.shell("wm density")
        if not re.search(r"(?:Physical|Override) density: 240\b", density):
            raise RuntimeError(f"当前 DPI 不是 240：{density.strip()}")
        focus = self.shell("dumpsys window")
        marker = "mCurrentFocus="
        if marker not in focus or "com.sega.pjsekai" not in focus.split(marker, 1)[1].splitlines()[0]:
            raise RuntimeError("MFA 所连设备当前前台不是日服 Project SEKAI")
