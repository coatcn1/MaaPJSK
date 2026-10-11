from __future__ import annotations

from pathlib import Path

_disabled = False


def enabled():
    return not _disabled


def configure(disabled: bool, *, framework=True):
    global _disabled
    if type(disabled) is not bool:
        raise ValueError("禁用日志开关必须为布尔值")
    if framework:
        from maa.tasker import Tasker
        from maa.library import Library
        accepted = [Tasker.set_log_dir("" if disabled else "./debug")]
        # AgentServer 代理只接受日志目录；识别截图由 GUI 或独立验收的主 Tasker 全局选项控制。
        if not Library.is_agent_server():
            accepted.extend([Tasker.set_save_draw(False), Tasker.set_save_on_error(not disabled)])
        if not all(accepted):
            raise RuntimeError("MaaFramework 拒绝文件日志设置，任务未开始")
    _disabled = disabled


def load_policy(root: Path):
    from .performance_settings import PerformanceSettings
    settings = PerformanceSettings.load(root / "config/performance-settings.json")
    configure(settings.disable_logging)
    return settings


def make_directory(path: Path, **kwargs):
    if enabled():
        path.mkdir(**kwargs)


def write_json(path, value):
    # 仅诊断调用点使用此入口，配置、谱面索引与业务状态继续严格持久化。
    if enabled():
        from .chart_catalog import _write_json
        _write_json(path, value)
