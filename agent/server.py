from __future__ import annotations

import sys
from pathlib import Path

# MFA 以脚本路径启动 Agent 时，Python 默认不会搜索项目根目录。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maa.agent.agent_server import AgentServer
from maa.tasker import Tasker

import auto_live  # noqa: F401
import solo_live  # noqa: F401


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("Maa Agent socket id is required")
    Tasker.set_log_dir("./debug")
    AgentServer.start_up(sys.argv[-1])
    AgentServer.join()
    AgentServer.shut_down()


if __name__ == "__main__":
    main()
