import json
import os
from pathlib import Path
import traceback

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction

from project_sekai.maa_device import MaaDevice
from project_sekai.one_shot_live import OneShotLive
from solo_live import load_performance


_SETTINGS: dict[int, dict] = {}
DIFFICULTIES = {"easy", "normal", "hard", "expert", "master", "append"}


def task_id(argv):
    value = getattr(argv.task_detail, "task_id", None)
    if type(value) is not int or value <= 0:
        raise ValueError("一键演出缺少当前任务 ID")
    return value


def visible_log(context, content):
    print(content, flush=True)
    if context.tasker.stopping:
        return
    detail = context.run_task("OneShotChartLiveLog", {"OneShotChartLiveLog": {"focus": {
        "Node.Action.Succeeded": {"content": content, "display": ["log"]}}}})
    if not detail or not detail.status.succeeded:
        raise RuntimeError("一键演出日志节点执行失败")


def create_workflow(context, root):
    config = Path(os.environ.get("MAAPJSK_TEMPLATE_CONFIG", root / "config/maapjsk-templates.json"))
    catalog = json.loads((root / "config/chart-sync.json").read_text(encoding="utf-8-sig"))
    return OneShotLive(MaaDevice(context.tasker.controller), config, Path(catalog["output_root"]),
                       root / "resource/models/song_title_ocr", root / "debug/one-shot-chart-runs",
                       stop_requested=lambda: bool(context.tasker.stopping),
                       log_message=lambda content: visible_log(context, content))


@AgentServer.custom_action("ProjectSekaiOneShotLiveConfig")
class ProjectSekaiOneShotLiveConfig(CustomAction):
    def run(self, context, argv):
        identifier = None
        try:
            identifier = task_id(argv)
            _SETTINGS.pop(identifier, None)
            if context.tasker.stopping:
                return False
            settings = json.loads(argv.custom_action_param or "{}")
            if (not isinstance(settings, dict) or set(settings) != {"difficulty"}
                    or not isinstance(settings["difficulty"], str) or settings["difficulty"] not in DIFFICULTIES):
                raise ValueError("一键演出只接受有效的谱面难度选项")
            # 入口始终重建当前任务配置，不能复用上一轮或其他演出任务的难度。
            _SETTINGS[identifier] = settings
            return True
        except Exception as error:
            print(f"一键演出设置失败：{error}", flush=True)
            return False


@AgentServer.custom_action("ProjectSekaiOneShotLive")
class ProjectSekaiOneShotLive(CustomAction):
    def run(self, context, argv):
        try:
            settings = _SETTINGS.pop(task_id(argv), None)
            if context.tasker.stopping:
                return False
            if settings is None or set(settings) != {"difficulty"}:
                raise ValueError("一键演出配置不完整，请从任务入口重新运行")
            root = Path(__file__).resolve().parents[1]
            performance = load_performance(root)
            # 入口不判断单人或协力，不能把单人 Profile 迁移到未经验证的手动开场链路。
            if performance.engine == "native" and performance.use_calibration_profile:
                raise RuntimeError("一键演出请在演奏设置中关闭自动使用校准配置，采用手动偏移")
            workflow = create_workflow(context, root)
            workflow.run(settings["difficulty"], performance.touch_offset_ms, engine=performance.engine)
            visible_log(context, "一键演出已完成本曲，任务结束")
            return True
        except InterruptedError:
            return False
        except Exception as error:
            if not context.tasker.stopping:
                try:
                    visible_log(context, f"一键演出中止：{error}")
                except Exception:
                    traceback.print_exc()
            traceback.print_exc()
            return False
