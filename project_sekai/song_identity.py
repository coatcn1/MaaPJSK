from __future__ import annotations

from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
import unicodedata

import cv2
import numpy as np

from .chart_catalog import ChartRepository, _safe_local_path, _sha256
from .ocr import LineOcr, Reading


def read_image(path: Path) -> np.ndarray:
    image = cv2.imdecode(np.frombuffer(path.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"无法解码图片：{path.name}")
    return image


def write_image(path: Path, image: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    success, data = cv2.imencode(".png", image)
    if not success:
        raise OSError("截图编码失败")
    path.write_bytes(data.tobytes())


def normalize_title(title: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFKC", title).casefold()
                   if unicodedata.category(char)[0] in {"L", "N"})


def title_score(observed: str, expected: str) -> float:
    a, b = normalize_title(observed), normalize_title(expected)
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def thumbnail(image: np.ndarray) -> np.ndarray:
    # 平均亮度和对比度归一化，使开场封面的淡入与准备页阴影不会改变身份。
    gray = cv2.cvtColor(cv2.resize(image, (48, 48)), cv2.COLOR_BGR2GRAY).astype(np.float32)
    gray -= gray.mean()
    return (gray / max(float(np.linalg.norm(gray)), 1e-6)).ravel()


@dataclass(frozen=True)
class Identity:
    song_id: int
    title: str
    difficulty: str
    level: int
    cover_score: float
    runner_up_score: float
    observed_title: str
    title_confidence: float
    title_score: float
    phase: str

    def to_dict(self) -> dict:
        return asdict(self)


class SongMatcher:
    def __init__(self, repository: ChartRepository, ocr: LineOcr) -> None:
        self.repository = repository
        self.ocr = ocr
        self.ids, features = [], []
        for song_id, song in repository.songs.items():
            jacket = song.get("jacket")
            if not jacket:
                continue
            path = _safe_local_path(repository.root, jacket["path"])
            body = path.read_bytes()
            if _sha256(body) != jacket["sha256"]:
                continue
            image = cv2.imdecode(np.frombuffer(body, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is not None:
                self.ids.append(song_id)
                features.append(thumbnail(image))
        self.features = np.stack(features) if features else np.empty((0, 2304))

    def read_opening_text(self, frame: np.ndarray, box: tuple[int, int, int, int], *, badge=False) -> Reading:
        x1, y1, x2, y2 = box
        crop = frame[y1:y2, x1:x2]
        # 开场标题后有背景图案，难度条还会竖向移动；提取白字并完整裁剪，避免背景或字形边缘干扰。
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, np.array([0, 0, 195], np.uint8), np.array([179, 65, 255], np.uint8))
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
        foreground = np.zeros_like(mask)
        for label in range(1, count):
            x, y, width, height, area = stats[label]
            # 难度只含拉丁字母，可去除封面底边；日文标题保留浊点等小部件。
            keep = height >= 7 and width >= 2 and area >= 10 if badge else area >= 2
            if keep:
                foreground[labels == label] = 255
        points = cv2.findNonZero(foreground)
        if points is None:
            return Reading("", 0.0)
        x, y, width, height = cv2.boundingRect(points)
        text = cv2.copyMakeBorder(255 - foreground[y:y + height, x:x + width], 5, 5, 6, 6,
                                 cv2.BORDER_CONSTANT, value=255)
        text = cv2.cvtColor(text, cv2.COLOR_GRAY2BGR)
        return self.ocr.read(text, (0, 0, text.shape[1], text.shape[0]))

    def match(self, frame: np.ndarray, difficulty: str, phase: str) -> Identity:
        if phase in {"prepare", "song_select"}:
            corners = np.float32([(910, 80), (1159, 92), (1146, 341), (897, 327)])
            target = np.float32([(0, 0), (255, 0), (255, 255), (0, 255)])
            jacket = cv2.warpPerspective(frame, cv2.getPerspectiveTransform(corners, target), (256, 256))
            title_box, difficulty_box = (821, 340, 1227, 378), (965, 443, 1090, 483)
        elif phase == "final":
            jacket = frame[390:657, 95:361]
            title_box, difficulty_box = (384, 541, 1240, 581), (65, 653, 320, 692)
        else:
            raise ValueError("歌曲匹配阶段无效")
        if len(self.ids) < 2:
            raise ValueError("本地封面库不足")
        scores = self.features @ thumbnail(jacket)
        ranked = np.argsort(scores)[::-1]
        best, second = float(scores[ranked[0]]), float(scores[ranked[1]])
        if best < .85 or best - second < .06:
            raise ValueError(f"歌曲封面身份不明确：最佳={best:.3f}，次选={second:.3f}")
        title = self.read_opening_text(frame, title_box) if phase == "final" else self.ocr.read(frame, title_box)
        song_id = self.ids[int(ranked[0])]
        song = self.repository.songs[song_id]
        similarity = title_score(title.text, song["title"])
        if title.confidence < .65 or similarity < .82:
            raise ValueError(f"歌曲标题不一致：实读={title.text!r}，候选={song['title']!r}，"
                             f"相似度={similarity:.3f}，置信度={title.confidence:.3f}")
        if phase != "song_select":
            badge = (self.read_opening_text(frame, difficulty_box, badge=True) if phase == "final"
                     else self.ocr.read(frame, difficulty_box))
            if badge.confidence < .7 or normalize_title(badge.text) != difficulty:
                raise ValueError(f"实际难度不一致：请求={difficulty}，实读={badge.text!r}，置信度={badge.confidence:.3f}")
        if difficulty not in song["charts"]:
            raise ValueError(f"本地缺少歌曲 {song_id} 的 {difficulty} 谱面")
        chart = song["charts"][difficulty]
        return Identity(song_id, song["title"], difficulty, chart["play_level"], best, second,
                        title.text, title.confidence, similarity, phase)
