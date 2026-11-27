from __future__ import annotations

from pathlib import Path
import os

_disabled = False
_framework_configured = False
_framework_logging_enabled = False
FRAMEWORK_LOGGING_ENV = "MAAPJSK_MAAFW_LOGGING"


def enabled():
    return not _disabled


def framework_logging_enabled():
    return _framework_logging_enabled


def _warn(message):
    try:
        print(f"警告：{message}", flush=True)
    except Exception:
        # 附加诊断通道故障不能阻断演出清理。
        pass


def configure(disabled: bool, *, framework=True, framework_logging=False, inherit_framework_snapshot=True):
    global _disabled, _framework_configured, _framework_logging_enabled
    if type(disabled) is not bool or type(framework_logging) is not bool:
        raise ValueError("日志开关必须为布尔值")
    if framework and not _framework_configured:
        raw = os.environ.get(FRAMEWORK_LOGGING_ENV) if inherit_framework_snapshot else None
        selected = (not disabled and framework_logging) if raw is None else raw == "1"
        if raw not in (None, "0", "1"):
            _warn("MaaFramework 启动日志快照无效，按关闭处理")
        # 5.10.2 已打开的文件流无法可靠热关闭；GUI 和 Agent 均只在首次初始化应用。
        _framework_configured = True
        _framework_logging_enabled = selected
        try:
            from maa.tasker import Tasker
            from maa.library import Library
            accepted = [Tasker.set_log_dir("./debug" if selected else "")]
            # AgentServer 代理只接受目录；调试图由 GUI 或独立验收主 Tasker 控制。
            if not Library.is_agent_server():
                accepted.extend([Tasker.set_save_draw(False), Tasker.set_save_on_error(selected)])
            if not all(accepted):
                _warn("MaaFramework 未接受文件日志设置，继续启动")
        except Exception as error:
            _warn(f"MaaFramework 文件日志初始化失败：{type(error).__name__}，继续启动")
    _disabled = disabled


def load_policy(root: Path, *, inherit_framework_snapshot=True):
    from .performance_settings import PerformanceSettings
    settings = PerformanceSettings.load(root / "config/performance-settings.json")
    configure(settings.disable_logging, framework_logging=settings.framework_logging,
              inherit_framework_snapshot=inherit_framework_snapshot)
    return settings


def make_directory(path: Path, **kwargs):
    if enabled():
        path.mkdir(**kwargs)


def write_json(path, value):
    # 仅诊断调用点使用此入口，配置、谱面索引与业务状态继续严格持久化。
    if enabled():
        from .chart_catalog import _write_json
        _write_json(path, value)
