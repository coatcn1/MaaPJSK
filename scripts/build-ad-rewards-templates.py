import argparse
import json
import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from project_sekai.chart_catalog import _write_json
from project_sekai.song_identity import read_image, write_image


def build(captures, output, manifest):
    spec = json.loads(Path(manifest).read_text(encoding="utf-8-sig"))
    anchors = {}
    for name, region in spec["regions"].items():
        source = Path(captures) / region["capture"]
        frame = read_image(source)
        x1, y1, x2, y2 = region["box"]
        if frame.shape[:2] != (720, 1280) or not (0 <= x1 < x2 <= 1280 and 0 <= y1 < y2 <= 720):
            raise ValueError(f"广告截图尺寸或裁剪区域无效：{name}")
        relative = f"templates/{name}.png"
        write_image(Path(output) / relative, frame[y1:y2, x1:x2])
        anchors[name] = {"path": relative, "box": region["box"],
                         "sha256": hashlib.sha256((Path(output) / relative).read_bytes()).hexdigest(),
                         "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    _write_json(Path(output) / "config.json", {"screen": spec["screen"], "threshold": spec["threshold"], "anchors": anchors})
    print(f"已生成 {len(anchors)} 个本机广告模板：{Path(output).resolve()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="由本机保存帧生成广告页面模板；图片保持忽略")
    parser.add_argument("--captures", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / ".local/ad-rewards-templates")
    parser.add_argument("--manifest", type=Path, default=ROOT / "examples/ad-rewards-template-manifest.json")
    args = parser.parse_args()
    build(args.captures, args.output, args.manifest)
