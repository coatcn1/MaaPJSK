from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
import urllib.request


FILES = {
    "inference.onnx": "da72dc72ca4dc220df0dfde68c1dedc31c58d3e76a25871122e5056227d50092",
    "inference.yml": "5dfeb2777f6d0db8177d8128a8acfcf6e6276dc4ac73ea3bf0dc06d6a5e85d8e",
}
SOURCE = "https://huggingface.co/PaddlePaddle/PP-OCRv5_mobile_rec_onnx/resolve/main/"


def setup(root: Path, source_dir: Path | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, expected in FILES.items():
        target = root / name
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == expected:
            print(f"OCR {name}: verified")
            continue
        temporary = target.with_suffix(target.suffix + ".part")
        try:
            if source_dir is not None:
                shutil.copyfile(source_dir / name, temporary)
            else:
                with urllib.request.urlopen(SOURCE + name, timeout=90) as response:
                    temporary.write_bytes(response.read())
            if hashlib.sha256(temporary.read_bytes()).hexdigest() != expected:
                raise ValueError(f"OCR {name} SHA256 不匹配")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        print(f"OCR {name}: ready")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="安装离线 PP-OCRv5 文字识别模型，不下载谱面")
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "resource/models/song_title_ocr")
    arguments = parser.parse_args()
    setup(arguments.output, arguments.source_dir)
