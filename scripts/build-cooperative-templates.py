from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from project_sekai.chart_catalog import _write_json
from project_sekai.song_identity import read_image, write_image


def build(captures: Path, output: Path, manifest: Path):
    specifications = json.loads(manifest.read_text(encoding="utf-8-sig"))
    paths = {}
    for name, region in specifications["regions"].items():
        source = captures / region["capture"]
        # 只有显式可选且缺失的截图可跳过；已有坏图或非法裁剪仍须报错。
        if region.get("optional") is True and not source.exists():
            continue
        frame = read_image(source)
        x1, y1, x2, y2 = region["box"]
        if frame.shape[:2] != (720, 1280) or not (0 <= x1 < x2 <= 1280 and 0 <= y1 < y2 <= 720):
            raise ValueError(f"协力截图尺寸或裁剪区域无效：{name}")
        relative = f"templates/{name}.png"
        write_image(output / relative, frame[y1:y2, x1:x2])
        paths[name] = relative
    _write_json(output / "config.json", {"threshold": specifications["threshold"], "templates": paths})
    print(f"已生成 {len(paths)} 个本机协力模板：{output.resolve()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="按照编号截图裁剪日服公房协力模板；图片仅写入本机忽略目录")
    parser.add_argument("--captures", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / ".local/cooperative-templates")
    parser.add_argument("--manifest", type=Path, default=ROOT / "examples/cooperative-template-manifest.json")
    args = parser.parse_args()
    build(args.captures, args.output, args.manifest)
