from __future__ import annotations

import json
import os
from pathlib import Path
import traceback

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from project_sekai.maa_device import MaaDevice
from project_sekai.navigator import Navigator

_SETTINGS: dict[int, dict[str, object]] = {}


def _task_id(argv: CustomAction.RunArg) -> int:
    value = getattr(argv.task_detail, "task_id", None)
    if not isinstance(value, int) or value <= 0:
        raise ValueError("缺少 MFA 当前任务 ID")
    return value


def _config_path() -> Path:
    configured = os.environ.get("MAAPJSK_TEMPLATE_CONFIG")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent.parent / "config" / "maapjsk-templates.json"


def _round_count(raw: object) -> int:
    if isinstance(raw, bool) or not str(raw).isdigit():
        raise ValueError("演出次数必须为 1 到 999 的整数")
    count = int(raw)
    if not 1 <= count <= 999:
        raise ValueError("演出次数必须为 1 到 999 的整数")
    return count


def _visible_log(context: Context, content: str) -> None:
    print(content, flush=True)
    if context.tasker.stopping:
        return
    detail = context.run_task("AutoLiveLog", {
        "AutoLiveLog": {"focus": {"Node.Action.Succeeded": {
            "content": content, "display": ["log"],
        }}}
    })
    if not detail or not detail.status.succeeded:
        raise RuntimeError("MFA 任务日志节点执行失败")


@AgentServer.custom_action("ProjectSekaiRecoveryModeConfig")
class ProjectSekaiRecoveryModeConfig(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            mode = json.loads(argv.custom_action_param or "{}").get("mode", "off")
            if mode not in {"off", "small", "large"}:
                return False
            _SETTINGS[_task_id(argv)] = {"mode": mode}
            return True
        except Exception:
            return False


@AgentServer.custom_action("ProjectSekaiRecoveryLimitConfig")
class ProjectSekaiRecoveryLimitConfig(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            raw = json.loads(argv.custom_action_param or "{}").get("limit", 1)
            if isinstance(raw, bool) or not str(raw).isdigit() or not 1 <= int(raw) <= 99:
                return False
            _SETTINGS[_task_id(argv)]["limit"] = int(raw)
            return True
        except Exception:
            return False


@AgentServer.custom_action("ProjectSekaiSongModeConfig")
class ProjectSekaiSongModeConfig(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            mode = json.loads(argv.custom_action_param or "{}").get("mode", "current")
            if mode not in {"current", "random"}:
                return False
            _SETTINGS.setdefault(_task_id(argv), {})["song_mode"] = mode
            return True
        except Exception:
            return False


@AgentServer.custom_action("ProjectSekaiAutoLive")
class ProjectSekaiAutoLive(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        navigator = None
        count = 0
        try:
            parameters = json.loads(argv.custom_action_param or "{}")
            count = _round_count(parameters.get("count", 1))
            settings = _SETTINGS.pop(_task_id(argv), {"mode": "off", "limit": 1})
            mode = str(settings["mode"])
            song_mode = str(settings.get("song_mode", "current"))
            limit = int(settings.get("limit", 1)) if mode != "off" else 0
            device = MaaDevice(context.tasker.controller)
            navigator = Navigator(
                device,
                _config_path(),
                stop_requested=lambda: bool(context.tasker.stopping),
                log_message=lambda content: _visible_log(context, content),
            )
            navigator.auto_live_loop(count, song_mode=song_mode, recovery_mode=mode, recovery_limit=limit)
            return True
        except InterruptedError:
            return False
        except Exception as error:
            if not context.tasker.stopping:
                completed = navigator.completed_rounds if navigator is not None else 0
                try:
                    _visible_log(context, f"自动演出中止：已完成 {completed} / 总数 {count}；{error}")
                except Exception:
                    traceback.print_exc()
            print(f"ProjectSekaiAutoLive failed: {type(error).__name__}: {error}", flush=True)
            traceback.print_exc()
            return False


@AgentServer.custom_action("ProjectSekaiConnectionCheck")
class ProjectSekaiConnectionCheck(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            MaaDevice(context.tasker.controller).preflight()
            _visible_log(context, "MFA 设备自检通过：1280×720、240 DPI、日服 Project SEKAI 前台")
            return True
        except Exception as error:
            print(f"MFA 设备自检失败：{type(error).__name__}: {error}", flush=True)
            return False
