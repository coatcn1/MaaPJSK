from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

import cv2
import numpy as np
import yaml


@dataclass(frozen=True)
class Reading:
    text: str
    confidence: float


class LineOcr:
    """固定区域的离线文字识别；模型只在任务开头加载一次。"""

    def __init__(self, root: Path) -> None:
        import onnxruntime as ort
        configuration = yaml.safe_load((root / "inference.yml").read_text(encoding="utf-8"))
        dictionary = configuration["PostProcess"]["character_dict"]
        # 数字在 YAML 中带引号；必须按 YAML 解码，不能将引号并入字形表。
        if not dictionary or not all(isinstance(character, str) for character in dictionary):
            raise ValueError("OCR 字典为空")
        self.characters = ["", *dictionary, " "]
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.log_severity_level = 3
        self.session = ort.InferenceSession(str(root / "inference.onnx"), options, providers=["CPUExecutionProvider"])

    def read(self, image: np.ndarray, box: tuple[int, int, int, int]) -> Reading:
        x1, y1, x2, y2 = box
        crop = image[y1:y2, x1:x2]
        if not crop.size or min(crop.shape[:2]) < 2:
            raise ValueError("OCR 区域无效")
        width = min(1600, max(32, round(48 * crop.shape[1] / crop.shape[0])))
        resized = cv2.resize(crop, (width, 48)).astype(np.float32)
        tensor = ((resized / 127.5) - 1).transpose(2, 0, 1)[None]
        values = self.session.run(None, {self.session.get_inputs()[0].name: tensor})[0][0]
        indexes = values.argmax(axis=1)
        scores = values.max(axis=1)
        text, confidences = [], []
        previous = -1
        for index, score in zip(indexes, scores):
            if index and index != previous:
                text.append(self.characters[index])
                confidences.append(float(score))
            previous = index
        return Reading(re.sub(r"\s+", " ", "".join(text)).strip(),
                       float(np.mean(confidences)) if confidences else 0.0)
