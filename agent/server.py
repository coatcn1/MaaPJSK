from __future__ import annotations

import sys
from pathlib import Path

# MFA 以脚本路径启动 Agent 时，Python 默认不会搜索项目根目录。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maa.agent.agent_server import AgentServer
from maa.tasker import Tasker
from project_sekai.diagnostics import load_policy

# 自定义动作注册也会调用 Agent 原生库；注册前应用策略，避免启动时先生成文件日志。
load_policy(Path(__file__).resolve().parents[1])

import auto_live  # noqa: F401
import solo_live  # noqa: F401
import cooperative_live  # noqa: F401
import one_shot_live  # noqa: F401
import ad_rewards  # noqa: F401


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("Maa Agent socket id is required")
    load_policy(Path(__file__).resolve().parents[1])
    AgentServer.start_up(sys.argv[-1])
    AgentServer.join()
    AgentServer.shut_down()


if __name__ == "__main__":
    main()
