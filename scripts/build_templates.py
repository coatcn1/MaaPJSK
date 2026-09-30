from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2


def build(manifest_path: Path, captures: Path, output: Path) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    output.mkdir(parents=True, exist_ok=True)
    template_dir = output / "templates"
    template_dir.mkdir(exist_ok=True)
    paths: dict[str, str] = {}
    for name, specification in manifest["regions"].items():
        source = captures / specification["capture"]
        frame = cv2.imread(str(source), cv2.IMREAD_COLOR)
        if frame is None or frame.shape[:2] != (720, 1280):
            raise ValueError(f"截图不存在或尺寸不是 1280×720：{source}")
        x1, y1, x2, y2 = specification["box"]
        if not (0 <= x1 < x2 <= 1280 and 0 <= y1 < y2 <= 720):
            raise ValueError(f"模板 {name} 的裁剪范围无效")
        destination = template_dir / f"{name}.png"
        if not cv2.imwrite(str(destination), frame[y1:y2, x1:x2]):
            raise RuntimeError(f"无法写入模板：{destination}")
        paths[name] = f"templates/{name}.png"
    config = {"threshold": manifest["threshold"], "templates": paths}
    (output / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="从本机页面截图重建页面识别模板")
    parser.add_argument("--manifest", type=Path, default=Path("examples/template-manifest.json"))
    parser.add_argument("--captures", type=Path, default=Path(".local/captures"))
    parser.add_argument("--output", type=Path, default=Path(".local"))
    args = parser.parse_args()
    build(args.manifest, args.captures, args.output)
