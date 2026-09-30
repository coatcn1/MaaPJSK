from __future__ import annotations

import argparse
import sys

from project_sekai.device import AdbDevice
from project_sekai.navigator import Navigator


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Project SEKAI 日服 1280×720 自动演奏原型")
    parser.add_argument("--adb", default="adb", help="ADB 可执行文件路径")
    parser.add_argument("--serial", help="ADB 设备序列号；有多个设备时必须指定")
    parser.add_argument("--config", default=".local/config.json", help="本机页面模板配置")
    commands = parser.add_subparsers(dest="command", required=True)
    auto = commands.add_parser("auto-live", help="从主页运行游戏内 AUTO LIVE 并结算")
    auto.add_argument("--rounds", type=int, default=1)
    auto.add_argument("--dry-run", action="store_true", help="只检查主页并打印第一步，不发送触控")
    commands.add_parser("inspect", help="打印当前页面的模板匹配分数")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.serial:
        raise ValueError("设备控制需要显式指定 --serial，防止误触其他模拟器")
    device = AdbDevice(args.adb, args.serial)
    navigator = Navigator(device, args.config, dry_run=getattr(args, "dry_run", False))
    if args.command == "inspect":
        device.preflight()
        frame = device.screenshot()
        for name, score in sorted(((name, navigator.match(frame, name)[0]) for name in navigator.templates), key=lambda item: -item[1]):
            print(f"{name:<20} {score:.3f}")
        return 0
    if args.command == "auto-live":
        if not 1 <= args.rounds <= 99:
            raise ValueError("rounds 必须在 1..99 之间")
        navigator.auto_live_loop(args.rounds)
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, RuntimeError, TimeoutError, KeyboardInterrupt) as error:
        print(f"已停止：{error}", file=sys.stderr)
        sys.exit(1)
