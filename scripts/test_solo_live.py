from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "agent")]


def controller_options(instance: dict, runtime: Path) -> dict:
    from maa.define import MaaAdbInputMethodEnum, MaaAdbScreencapMethodEnum
    config = instance.get("Config") or {}
    if isinstance(config, str):
        config = json.loads(config)
    if not isinstance(config, dict):
        raise ValueError("MFA 保存的控制器配置不是对象")
    # MuMu 专用截图依赖 extras 配置；只传端点会退回慢截图，不能代表 MFA 的实际链路。
    result = {"screencap_methods": int(instance.get("ScreencapMethods", MaaAdbScreencapMethodEnum.Default)),
              "input_methods": int(instance.get("InputMethods", MaaAdbInputMethodEnum.Default)),
              "config": config}
    # MFA 2.12 实际固定使用 libs/MaaAgentBinary，设备信息里的旧 AgentPath 不参与其连接。
    agent_path = runtime / "libs/MaaAgentBinary"
    if not agent_path.is_dir():
        raise ValueError("MFA 输入运行文件不存在，请重新部署后再验收")
    result["agent_path"] = str(agent_path.resolve())
    return result


def main():
    parser = argparse.ArgumentParser(description="通过 MFA 保存的设备和 MaaFramework 执行单人谱面验收")
    parser.add_argument("--mfa-root", type=Path, default=ROOT / ".local/mfa-generic")
    parser.add_argument("--difficulty", choices=["easy", "normal", "hard", "expert", "master", "append"], default="easy")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--offset-ms", type=int, help="手动实际偏移；校准时作为本次排练的起始候选")
    parser.add_argument("--engine", choices=["legacy", "native"], default="legacy")
    parser.add_argument("--calibrate", action="store_true")
    parser.add_argument("--use-calibration-profile", action="store_true", help="验收正常演出加载当前设备和难度已验证配置的路径")
    parser.add_argument("--allow-stamina-spend", action="store_true", help="仅在已获用户授权时显式允许验收消耗指定体力")
    parser.add_argument("--allow-item-use", action="store_true", help="仅在已获用户授权时显式允许验收使用指定饮料")
    parser.add_argument("--bonus-consumption", choices=["current", *map(str, range(11))], default="0",
                        help="每局体力：current 沿用游戏设置，或指定 0 到 10；验收入口默认 0")
    parser.add_argument("--recovery-mode", choices=["off", "small", "large"], default="off")
    parser.add_argument("--recovery-count", type=int, default=1)
    parser.add_argument("--inspect-only", action="store_true")
    arguments = parser.parse_args()
    if (not 1 <= arguments.count <= 999 or (arguments.offset_ms is not None and not -250 <= arguments.offset_ms <= 250)
            or not 1 <= arguments.recovery_count <= 99):
        parser.error("次数、偏移量或每次回复瓶数越界")
    if not arguments.inspect_only and ((arguments.recovery_mode != "off" and not arguments.allow_item_use) or arguments.bonus_consumption == "current"
            or (arguments.bonus_consumption != "0" and not arguments.allow_stamina_spend)):
        parser.error("独立验收默认 0 体力且关闭用药；消耗体力需 --allow-stamina-spend，用药另需 --allow-item-use")
    runtime = arguments.mfa_root.resolve()
    sys.path[:0] = [str(runtime), str(runtime / "agent")]
    from maa.controller import AdbController
    from maa.resource import Resource
    from maa.tasker import Tasker
    from maa.toolkit import Toolkit
    from maa.agent_client import AgentClient
    from project_sekai.maa_device import MaaDevice
    from project_sekai.song_identity import write_image

    if sys.platform == "win32":
        import subprocess
        # 与 MFA 实例互斥，避免两个控制器同时向同一设备发送输入。
        command = "$p=Get-CimInstance Win32_Process -Filter \"Name='MFAAvalonia.exe' OR Name='MaaBanGDream.exe'\"; if($p){exit 2}"
        result = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True)
        if result.returncode:
            raise RuntimeError("独立验收前请关闭 MFA，避免同时控制设备")
    instance = json.loads((runtime / "config/instances/default.json").read_text(encoding="utf-8-sig"))["AdbDevice"]
    debug = ROOT / ".local/solo-acceptance"
    debug.mkdir(parents=True, exist_ok=True)
    Toolkit.init_option(str(debug))
    controller = AdbController(instance["AdbPath"], instance["AdbSerial"], **controller_options(instance, runtime))
    if not controller.post_connection().wait().succeeded:
        raise RuntimeError("MaaFramework 无法连接 MFA 保存的设备")
    device = MaaDevice(controller)
    device.preflight()
    if arguments.inspect_only:
        write_image(debug / "current.png", device.screenshot())
        print("设备自检和截图完成，未发送游戏输入")
        return
    resource = Resource()
    if not resource.post_bundle(runtime / "resource").wait().succeeded:
        raise RuntimeError("部署的 MFA 资源加载失败")
    tasker = Tasker()
    if not tasker.bind(resource, controller):
        raise RuntimeError("MaaFramework 控制器绑定失败")
    os.environ["MAAPJSK_TEMPLATE_CONFIG"] = str(runtime / "config/maapjsk-templates.json")
    import subprocess
    client = AgentClient()
    client.set_timeout(1800000 if arguments.calibrate else 900000)
    if not client.bind(resource):
        raise RuntimeError("Agent 资源绑定失败")
    from dataclasses import asdict
    from project_sekai.performance_settings import PerformanceSettings
    performance = PerformanceSettings(engine=arguments.engine, touch_offset_ms=arguments.offset_ms or 0,
                                      bonus_consumption=int(arguments.bonus_consumption),
                                      use_calibration_profile=arguments.use_calibration_profile)
    if arguments.calibrate:
        saved = PerformanceSettings.load(runtime / "config/performance-settings.json")
        performance = PerformanceSettings(engine="native", touch_offset_ms=saved.touch_offset_ms,
                                          bonus_consumption=int(arguments.bonus_consumption), use_calibration_profile=False)
    environment = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1",
                       MAAPJSK_TEST_PERFORMANCE_JSON=json.dumps(asdict(performance)))
    if arguments.allow_stamina_spend:
        environment["MAAPJSK_TEST_ALLOW_STAMINA"] = "1"
    if arguments.allow_item_use:
        environment["MAAPJSK_TEST_ALLOW_ITEMS"] = "1"
        environment["MAAPJSK_TEST_RECOVERY_JSON"] = json.dumps({"mode": arguments.recovery_mode, "count": arguments.recovery_count})
    if arguments.calibrate and arguments.offset_ms is not None:
        environment["MAAPJSK_TEST_INITIAL_OFFSET_MS"] = str(arguments.offset_ms)
    agent = subprocess.Popen([sys.executable, str(runtime / "agent/server.py"), client.identifier],
                             cwd=runtime, env=environment,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    overrides = {
        "SoloChartLiveDifficultyConfig": {"custom_action_param": {"difficulty": arguments.difficulty}},
        "SoloChartLiveCountConfig": {"custom_action_param": {"count": arguments.count}},
        "SoloChartLiveRecoveryModeConfig": {"custom_action_param": {"recovery_mode": arguments.recovery_mode}},
        "SoloChartLiveRecoveryCountConfig": {"custom_action_param": {"recovery_count": arguments.recovery_count}},
    }
    try:
        if not client.connect():
            raise RuntimeError("部署版 Agent 连接失败")
        if arguments.calibrate:
            overrides = {"SoloChartCalibrationDifficultyConfig": {"custom_action_param": {"difficulty": arguments.difficulty}}}
        result = tasker.post_task("SoloChartCalibration" if arguments.calibrate else "SoloChartLive", overrides).wait()
        if not result.succeeded:
            raise RuntimeError("单人谱面验收任务未完成，查看 debug/solo-chart-runs 本局报告")
    except KeyboardInterrupt:
        tasker.post_stop().wait()
        raise
    finally:
        client.disconnect()
        try:
            agent.wait(timeout=8)
        except subprocess.TimeoutExpired:
            agent.terminate()
            agent.wait(timeout=5)
    print("单人谱面验收完成")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    main()
