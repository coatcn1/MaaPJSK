from __future__ import annotations

import re
import subprocess

import cv2
import numpy as np


class AdbDevice:
    def __init__(self, adb: str, serial: str) -> None:
        self.adb = adb
        self.serial = serial

    def run(self, *args: str, timeout: float = 15) -> bytes:
        command = [self.adb, "-s", self.serial, *args]
        result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
        if result.returncode:
            raise RuntimeError(f"ADB 命令失败：{' '.join(args)}：{result.stderr.decode(errors='replace').strip()}")
        return result.stdout

    def screenshot(self) -> np.ndarray:
        data = self.run("exec-out", "screencap", "-p", timeout=20)
        frame = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise RuntimeError("ADB 截图解码失败")
        return frame

    def tap(self, x: int, y: int) -> None:
        self.run("shell", "input", "tap", str(x), str(y))

    def back(self) -> None:
        self.run("shell", "input", "keyevent", "4")

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 450) -> None:
        self.run("shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms))

    def preflight(self) -> None:
        state = self.run("get-state").decode(errors="replace").strip()
        if state != "device":
            raise RuntimeError(f"设备状态为 {state!r}")
        density = self.run("shell", "wm", "density").decode(errors="replace")
        if not re.search(r"(?:Physical|Override) density: 240\b", density):
            raise RuntimeError(f"当前 DPI 不是 240：{density.strip()}")
        frame = self.screenshot()
        if frame.shape[:2] != (720, 1280):
            raise RuntimeError(f"截图分辨率不是 1280×720：{frame.shape[1]}×{frame.shape[0]}")
        focus = self.run("shell", "dumpsys", "window").decode(errors="replace")
        if "mCurrentFocus" not in focus or "com.sega.pjsekai" not in focus.split("mCurrentFocus=", 1)[-1].splitlines()[0]:
            raise RuntimeError("当前前台不是日服 Project SEKAI")
