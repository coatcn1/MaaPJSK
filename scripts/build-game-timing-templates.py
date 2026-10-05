from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import cv2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from project_sekai.chart_catalog import _write_json
from project_sekai.game_timing import GameTimingTemplates, glyph_mask
from project_sekai.song_identity import read_image, write_image


def build(captures: Path, output: Path, manifest: Path):
    specifications = json.loads(manifest.read_text(encoding="utf-8-sig"))
    paths = {}
    for name, region in specifications["regions"].items():
        frame = read_image(captures / region["capture"])
        x1, y1, x2, y2 = region["box"]
        if frame.shape[:2] != (720, 1280) or not (0 <= x1 < x2 <= 1280 and 0 <= y1 < y2 <= 720):
            raise ValueError(f"游戏判定参考图尺寸或裁剪区域无效：{name}")
        mask = glyph_mask(frame[y1:y2, x1:x2], region["ink"])
        nonzero = cv2.findNonZero(mask)
        if nonzero is None:
            raise ValueError(f"游戏判定参考图中没有字形：{name}")
        x, y, width, height = cv2.boundingRect(nonzero)
        # 模板只保留文字二值字形，去掉歌曲封面与玩家信息；图片仍仅写入本机忽略目录。
        template = cv2.copyMakeBorder(mask[y:y + height, x:x + width], 2, 2, 2, 2, cv2.BORDER_CONSTANT)
        relative = f"templates/{name}.png"
        write_image(output / relative, template)
        paths[name] = relative
    path = output / "config.json"
    _write_json(path, {"schema_version": 1, "threshold": .90, "direction_margin": .08, "templates": paths})
    GameTimingTemplates.load(path)
    print(f"已生成 {len(paths)} 个本机游戏判定字形模板：{output.resolve()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="从确认过的游戏 HUD 参考图生成 FAST、LATE、GREAT、PERFECT 字形；不接管设备")
    parser.add_argument("--captures", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / ".local/game-timing-templates")
    parser.add_argument("--manifest", type=Path, default=ROOT / "examples/game-timing-template-manifest.json")
    arguments = parser.parse_args()
    build(arguments.captures, arguments.output, arguments.manifest)
