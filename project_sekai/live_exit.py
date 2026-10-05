from pathlib import Path
import re
import time

from .chart_catalog import _write_json
from .live_end import release_finished
from .song_identity import write_image


def background_game(device, report, directory: Path, reason, log=print, *, task_label="协力", stop_requested=lambda: False):
    playback = report.get("playback")
    if playback is not None and not release_finished(report):
        raise RuntimeError("本轮触点释放未确认，禁止发送 HOME")
    report.setdefault("background_exit", {"reason": reason, "method": "android_home", "confirmed": False,
                                          "touch_cleanup": "confirmed" if playback else "no_gameplay_started"})
    _write_json(directory / "report.json", report)
    launcher = None
    try:
        resolved = device.shell("cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.HOME")
        component = re.search(r"(?m)^([A-Za-z0-9_.]+)/[^\s]+\s*$", resolved)
        launcher = component[1] if component else None
    except Exception as error:
        report["background_exit"]["launcher_resolution_error"] = str(error)
    if stop_requested():
        raise InterruptedError("用户已停止任务")
    if not report["background_exit"].get("home_request_succeeded"):
        device.home()
        report["background_exit"]["home_request_succeeded"] = True
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if stop_requested():
            raise InterruptedError("用户已停止任务")
        focus = device.shell("dumpsys window")
        current = re.search(r"mCurrentFocus=[^\n]*?\s([A-Za-z0-9_.]+)/[^\s}]+", focus)
        if launcher and current and current[1] == launcher and launcher != "com.sega.pjsekai":
            report["background_exit"].update(confirmed=True, foreground_package=launcher)
            try:
                write_image(directory / "emulator-home.png", device.screenshot())
            except Exception as error:
                report["background_exit"]["screenshot_error"] = str(error)
            try:
                log(f"{task_label}已返回模拟器主页，游戏留在后台，结束任务")
            except Exception:
                pass
            return
        # HOME 只发送一次；连接恢复后可继续确认焦点，用户停止仍优先。
        time.sleep(.15)
    raise RuntimeError("HOME 请求已发送，但未确认模拟器主页焦点，保留失败证据")
