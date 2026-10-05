from __future__ import annotations

import json
import os
from pathlib import Path
import traceback

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction

from project_sekai.cooperative_live import CooperativeLive
from project_sekai.maa_device import MaaDevice
from project_sekai.song_identity import COOPERATIVE_DIFFICULTIES
from solo_live import load_performance, re_integer


_SETTINGS: dict[int, dict] = {}


def task_id(argv):
    value = getattr(argv.task_detail, "task_id", None)
    if type(value) is not int or value <= 0:
        raise ValueError("协力任务缺少当前任务 ID")
    return value


def validate_option(key, value):
    choices = {"room": {"free", "veteran"}, "song_mode": {"current", "random"},
               "difficulty": set(COOPERATIVE_DIFFICULTIES), "recovery_mode": {"off", "small", "large"}}
    if key in choices and isinstance(value, str) and value in choices[key]:
        return value
    if key in {"count", "recovery_count"} and not isinstance(value, bool) and re_integer(value):
        number = int(value)
        if 1 <= number <= (999 if key == "count" else 99):
            return number
    raise ValueError(f"协力选项无效：{key}")


def visible_log(context, content):
    print(content, flush=True)
    if context.tasker.stopping:
        return
    detail = context.run_task("CooperativeChartLiveLog", {"CooperativeChartLiveLog": {"focus": {
        "Node.Action.Succeeded": {"content": content, "display": ["log"]}}}})
    if not detail or not detail.status.succeeded:
        raise RuntimeError("协力任务日志节点执行失败")


def create_workflow(context, root):
    config = Path(os.environ.get("MAAPJSK_TEMPLATE_CONFIG", root / "config/maapjsk-templates.json"))
    cooperative = Path(os.environ.get("MAAPJSK_COOPERATIVE_TEMPLATE_CONFIG", root / "config/cooperative-templates.json"))
    catalog = json.loads((root / "config/chart-sync.json").read_text(encoding="utf-8-sig"))
    return CooperativeLive(MaaDevice(context.tasker.controller), config, cooperative, Path(catalog["output_root"]),
                           root / "resource/models/song_title_ocr", root / "debug/cooperative-chart-runs",
                           stop_requested=lambda: bool(context.tasker.stopping),
                           log_message=lambda content: visible_log(context, content))


@AgentServer.custom_action("ProjectSekaiCooperativeLiveConfig")
class ProjectSekaiCooperativeLiveConfig(CustomAction):
    def run(self, context, argv):
        identifier = None
        try:
            identifier = task_id(argv)
            if context.tasker.stopping:
                _SETTINGS.pop(identifier, None)
                return False
            options = json.loads(argv.custom_action_param or "{}")
            if not isinstance(options, dict) or len(options) != 1:
                raise ValueError("每个协力配置节点只能提供一项选项")
            key, value = next(iter(options.items()))
            validated = validate_option(key, value)
            if key == "room":
                # 公房节点是入口；同任务重新开始时清空旧配置，各任务 ID 之间相互隔离。
                _SETTINGS[identifier] = {"room": validated}
            else:
                _SETTINGS[identifier][key] = validated
            return True
        except Exception as error:
            if identifier is not None:
                _SETTINGS.pop(identifier, None)
            print(f"协力配置失败：{error}", flush=True)
            return False


@AgentServer.custom_action("ProjectSekaiCooperativeLive")
class ProjectSekaiCooperativeLive(CustomAction):
    def run(self, context, argv):
        workflow, settings = None, None
        try:
            settings = _SETTINGS.pop(task_id(argv), None)
            if context.tasker.stopping:
                return False
            if settings is None or set(settings) != {"room", "song_mode", "difficulty", "count", "recovery_mode", "recovery_count"}:
                raise ValueError("协力配置不完整，请从任务入口重新运行")
            root = Path(__file__).resolve().parents[1]
            performance = load_performance(root)
            if type(performance.cooperative_game_timing_feedback) is not bool:
                raise ValueError("协力 FAST / LATE 微调开关必须为布尔值")
            # 单人 Profile 尚未验证协力链路；显式失败，不能静默复用或忽略用户的校准开关。
            if performance.engine == "native" and performance.use_calibration_profile:
                raise RuntimeError("协力尚无独立验收的校准配置，请在演奏设置中关闭自动使用校准配置后采用手动偏移")
            workflow = create_workflow(context, root)
            feedback_status = "开" if performance.cooperative_game_timing_feedback else "关"
            try:
                visible_log(context, f"协力演奏设置：{performance.engine}；手动触控偏移 {performance.touch_offset_ms} ms；"
                            f"FAST / LATE 微调（试验）：{feedback_status}")
            except Exception:
                # 初始日志 IPC 不属于配置或游戏证据，失效不能取消已经合法的演出任务。
                pass
            workflow.run(settings["count"], settings["difficulty"], settings["song_mode"], performance.touch_offset_ms,
                         room=settings["room"], bonus_consumption=performance.bonus_consumption,
                         recovery_mode=settings["recovery_mode"], recovery_count=settings["recovery_count"],
                         engine=performance.engine, game_timing_feedback=performance.cooperative_game_timing_feedback)
            return True
        except InterruptedError:
            return False
        except Exception as error:
            if not context.tasker.stopping:
                completed = workflow.completed_rounds if workflow else 0
                count = settings.get("count", 0) if settings else 0
                try:
                    visible_log(context, f"协力谱面演出中止：已完成 {completed} / 总数 {count}；{error}")
                except Exception:
                    traceback.print_exc()
            traceback.print_exc()
            return False
