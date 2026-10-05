from __future__ import annotations

import json
import os
from pathlib import Path
import traceback

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction

from project_sekai.maa_device import MaaDevice
from project_sekai.solo_live import SoloLive
from project_sekai.performance_settings import PerformanceSettings
from project_sekai.calibration import CalibrationProfiles, CalibrationRunner, environment_signature


_SETTINGS: dict[int, dict] = {}


def load_performance(root: Path):
    settings = PerformanceSettings.load(root / "config/performance-settings.json")
    override = os.environ.get("MAAPJSK_TEST_PERFORMANCE_JSON")
    if override:
        # 独立验收入口以环境传递一次性快照，实际保存的用户设置不被改写。
        temporary = json.loads(override)
        if temporary.get("bonus_consumption") != 0 and os.environ.get("MAAPJSK_TEST_ALLOW_STAMINA") != "1":
            raise ValueError("独立验收只允许 0 体力")
        settings = PerformanceSettings(**temporary)
        if settings.engine not in {"legacy", "native"} or not -300 <= settings.touch_offset_ms <= 300:
            raise ValueError("独立验收演奏设置无效")
    return settings


def create_workflow(context, root: Path):
    config = Path(os.environ.get("MAAPJSK_TEMPLATE_CONFIG", root / "config/maapjsk-templates.json"))
    chart_config = json.loads((root / "config/chart-sync.json").read_text(encoding="utf-8-sig"))
    return SoloLive(MaaDevice(context.tasker.controller), config, Path(chart_config["output_root"]),
                    root / "resource/models/song_title_ocr", root / "debug/solo-chart-runs",
                    stop_requested=lambda: bool(context.tasker.stopping),
                    log_message=lambda message: visible_log(context, message))


def task_id(argv) -> int:
    value = getattr(argv.task_detail, "task_id", None)
    if not isinstance(value, int) or value <= 0:
        raise ValueError("单人谱面演出缺少当前任务 ID")
    return value


def validate_option(key: str, value):
    if key == "difficulty" and value in {"easy", "normal", "hard", "expert", "master", "append"}:
        return value
    if key == "song_mode" and value in {"current", "random"}:
        return value
    if key == "bonus_consumption" and value == "current":
        return value
    if key == "recovery_mode" and value in {"off", "small", "large"}:
        return value
    if key in {"count", "offset_ms", "bonus_consumption", "recovery_count"} and not isinstance(value, bool) and re_integer(value):
        number = int(value)
        if ((key == "count" and 1 <= number <= 999) or (key == "offset_ms" and -300 <= number <= 300)
                or (key == "bonus_consumption" and 0 <= number <= 10)
                or (key == "recovery_count" and 1 <= number <= 99)):
            return number
    raise ValueError(f"单人谱面选项无效：{key}")


def re_integer(value) -> bool:
    import re
    return bool(re.fullmatch(r"-?\d+", str(value)))


def visible_log(context, content: str):
    print(content, flush=True)
    if context.tasker.stopping:
        return
    detail = context.run_task("SoloChartLiveLog", {"SoloChartLiveLog": {"focus": {
        "Node.Action.Succeeded": {"content": content, "display": ["log"]}}}})
    if not detail or not detail.status.succeeded:
        raise RuntimeError("单人谱面任务日志节点执行失败")


@AgentServer.custom_action("ProjectSekaiSoloLiveConfig")
class ProjectSekaiSoloLiveConfig(CustomAction):
    def run(self, context, argv) -> bool:
        identifier = None
        try:
            if context.tasker.stopping:
                return False
            identifier = task_id(argv)
            options = json.loads(argv.custom_action_param or "{}")
            if len(options) != 1:
                raise ValueError("每个配置节点必须只提供一项选项")
            key, value = next(iter(options.items()))
            validated = validate_option(key, value)
            if key == "difficulty":
                # 入口重置本任务数据；不复用上一局或其他 MFA 任务的难度配置。
                _SETTINGS[identifier] = {"difficulty": validated}
            else:
                _SETTINGS[identifier][key] = validated
            return True
        except Exception as error:
            if identifier is not None:
                _SETTINGS.pop(identifier, None)
            print(f"单人谱面设置失败：{error}", flush=True)
            return False


@AgentServer.custom_action("ProjectSekaiSoloLive")
class ProjectSekaiSoloLive(CustomAction):
    def run(self, context, argv) -> bool:
        workflow = None
        settings = None
        try:
            if context.tasker.stopping:
                return False
            settings = _SETTINGS.pop(task_id(argv))
            if set(settings) != {"difficulty", "song_mode", "count", "recovery_mode", "recovery_count"}:
                raise ValueError("单人谱面任务配置不完整")
            root = Path(__file__).resolve().parents[1]
            performance = load_performance(root)
            workflow = create_workflow(context, root)
            profile = None
            if performance.engine == "native" and performance.use_calibration_profile:
                environment = environment_signature(workflow.device)
                profile = CalibrationProfiles(root / "config/calibration-profiles").load(environment, settings["difficulty"])
                if profile is None:
                    raise RuntimeError(f"当前设备的 {settings['difficulty'].upper()} 尚无已验证 Native 校准配置，请先运行 Native 谱面校准任务")
            offset = performance.touch_offset_ms + (profile["offset_ms"] if profile else 0)
            visible_log(context, f"演奏设置：{performance.engine}；实际触控偏移 {offset} ms；{'已加载校准配置' if profile else '使用手动偏移'}")
            workflow.run(settings["count"], settings["difficulty"], settings["song_mode"], offset,
                         bonus_consumption=performance.bonus_consumption, recovery_mode=settings["recovery_mode"],
                         recovery_count=settings["recovery_count"], engine=performance.engine,
                         latency_offsets=profile.get("latency_offsets", {}) if profile else None,
                         calibration_profile=profile)
            return True
        except InterruptedError:
            return False
        except Exception as error:
            if not context.tasker.stopping:
                completed = workflow.completed_rounds if workflow else 0
                count = settings.get("count", 0) if settings else 0
                try:
                    visible_log(context, f"单人谱面演出中止：已完成 {completed} / 总数 {count}；{error}")
                except Exception:
                    traceback.print_exc()
            traceback.print_exc()
            return False


@AgentServer.custom_action("ProjectSekaiNativeCalibration")
class ProjectSekaiNativeCalibration(CustomAction):
    def run(self, context, argv) -> bool:
        try:
            if context.tasker.stopping:
                return False
            options = _SETTINGS.pop(task_id(argv))
            if set(options) != {"difficulty", "song_mode"}:
                raise ValueError("Native 校准任务配置不完整")
            root = Path(__file__).resolve().parents[1]
            performance = load_performance(root)
            workflow = create_workflow(context, root)
            recovery = json.loads(os.environ.get("MAAPJSK_TEST_RECOVERY_JSON", '{"mode":"off","count":1}'))
            if recovery.get("mode", "off") != "off" and os.environ.get("MAAPJSK_TEST_ALLOW_ITEMS") != "1":
                raise ValueError("校准验收用药未获授权")
            runner = CalibrationRunner(workflow, CalibrationProfiles(root / "config/calibration-profiles"),
                                       environment_signature(workflow.device), performance, root / "debug/calibration-runs",
                                       test_bonus_consumption=performance.bonus_consumption,
                                       initial_offset_ms=int(os.environ["MAAPJSK_TEST_INITIAL_OFFSET_MS"])
                                       if os.environ.get("MAAPJSK_TEST_PERFORMANCE_JSON") and os.environ.get("MAAPJSK_TEST_INITIAL_OFFSET_MS") else None,
                                       test_recovery_mode=recovery.get("mode", "off"), test_recovery_count=recovery.get("count", 1))
            runner.run(options["difficulty"], options["song_mode"])
            return True
        except InterruptedError:
            return False
        except Exception as error:
            if not context.tasker.stopping:
                try:
                    visible_log(context, f"Native 校准中止：{error}")
                except Exception:
                    traceback.print_exc()
            traceback.print_exc()
            return False
