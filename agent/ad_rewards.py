import os
from pathlib import Path
import traceback

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction

from project_sekai.ad_rewards import AdRewardPages, AdRewards
from project_sekai.maa_device import MaaDevice
from project_sekai.game_login import GameLogin
from project_sekai.navigator import Navigator


def visible_log(context, content):
    print(content, flush=True)
    if context.tasker.stopping:
        return
    detail = context.run_task("AdRewardsLog", {"AdRewardsLog": {"focus": {
        "Node.Action.Succeeded": {"content": content, "display": ["log"]}}}})
    if not detail or not detail.status.succeeded:
        raise RuntimeError("广告任务日志节点执行失败")


def create_workflow(context, root):
    config = Path(os.environ.get("MAAPJSK_AD_REWARDS_CONFIG", root / "config/ad-rewards-templates.json"))
    if not config.is_file():
        raise RuntimeError("缺少本机广告模板，请先按广告任务说明采样并生成模板")
    device = MaaDevice(context.tasker.controller)
    standard = Path(os.environ.get("MAAPJSK_TEMPLATE_CONFIG", root / "config/maapjsk-templates.json"))
    navigator = Navigator(device, standard, stop_requested=lambda: bool(context.tasker.stopping))
    pages = AdRewardPages(config, root / "resource/models/song_title_ocr", navigator=navigator)
    login = GameLogin(device, navigator, root / "debug/game-login-runs",
                      stop_requested=lambda: bool(context.tasker.stopping), ocr_root=root / "resource/models/song_title_ocr")
    return AdRewards(device, pages, root / "debug/ad-rewards-runs", game_login=login,
                     stop_requested=lambda: bool(context.tasker.stopping),
                     log_message=lambda content: visible_log(context, content))


@AgentServer.custom_action("ProjectSekaiAdRewards")
class ProjectSekaiAdRewards(CustomAction):
    def run(self, context, argv):
        try:
            if context.tasker.stopping:
                return False
            create_workflow(context, Path(__file__).resolve().parents[1]).run()
            return True
        except InterruptedError:
            return False
        except Exception as error:
            if not context.tasker.stopping:
                try:
                    visible_log(context, f"广告奖励中止：{error}")
                except Exception:
                    traceback.print_exc()
            traceback.print_exc()
            return False
