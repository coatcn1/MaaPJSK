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


COOPERATIVE_DIFFICULTIES = {
    "easy": ((872, 486), (65, 90)), "normal": ((952, 492), (90, 115)),
    "hard": ((1031, 496), (18, 38)), "expert": ((1109, 503), (155, 179)),
    "master": ((1191, 510), (128, 153)),
}


def cooperative_difficulty_selected(frame: np.ndarray, difficulty: str) -> bool:
    if difficulty not in COOPERATIVE_DIFFICULTIES:
        return False
    # 协力准备页没有独立难度条；选中圆钮的实心颜色是本机难度的证据，其他玩家卡片不参与确认。
    (x, y), (low, high) = COOPERATIVE_DIFFICULTIES[difficulty]
    crop = cv2.cvtColor(frame[y - 23:y + 24, x - 23:x + 24], cv2.COLOR_BGR2HSV)
    yy, xx = np.ogrid[-23:24, -23:24]
    disk = xx * xx + yy * yy <= 23 * 23
    colored = ((crop[:, :, 0] >= low) & (crop[:, :, 0] <= high)
               & (crop[:, :, 1] >= 110) & (crop[:, :, 2] >= 150))
    return float(np.mean(colored[disk])) >= .55


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
        self._cover_features = {}

    def read_preparation_title(self, frame: np.ndarray, box: tuple[int, int, int, int]) -> Reading:
        x1, y1, x2, y2 = box
        crop = frame[y1:y2, x1:x2]
        mask = cv2.inRange(cv2.cvtColor(crop, cv2.COLOR_BGR2HSV),
                           np.array([0, 0, 195], np.uint8), np.array([179, 65, 255], np.uint8))
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
        # 封面底边会进入标题区域；先用标题下半部的字形定位横向范围，再保留范围内的浊点。
        anchors = [(x, width) for x, y, width, height, area in stats[1:]
                   if y > 0 and height >= 7 and area >= 10 and y + height >= mask.shape[0] * .55]
        if not anchors:
            return Reading("", 0.0)
        left = min(x for x, _ in anchors)
        right = max(x + width for x, width in anchors)
        foreground = np.zeros_like(mask)
        for label in range(1, count):
            x, y, width, _, area = stats[label]
            if y > 0 and area >= 2 and left <= x and x + width <= right:
                foreground[labels == label] = 255
        points = cv2.findNonZero(foreground)
        if points is None:
            return Reading("", 0.0)
        x, y, width, height = cv2.boundingRect(points)
        text = cv2.copyMakeBorder(255 - foreground[y:y + height, x:x + width], 5, 5, 6, 6,
                                 cv2.BORDER_CONSTANT, value=255)
        text = cv2.cvtColor(text, cv2.COLOR_GRAY2BGR)
        return self.ocr.read(text, (0, 0, text.shape[1], text.shape[0]))

    def registered_cover(self, song_id: int, observed_features) -> dict:
        result = {"song_id": song_id, "confirmed": False}
        cache = getattr(self, "_cover_features", None)
        if cache is None:
            cache = self._cover_features = {}
        if song_id not in cache:
            entry = self.repository.songs[song_id].get("jacket")
            if not entry:
                return result
            body = _safe_local_path(self.repository.root, entry["path"]).read_bytes()
            if _sha256(body) != entry["sha256"]:
                return result
            image = cv2.imdecode(np.frombuffer(body, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
            if image is None:
                return result
            points, descriptors = cv2.AKAZE_create().detectAndCompute(cv2.resize(image, (256, 256)), None)
            cache[song_id] = (points, descriptors)
        points, descriptors = cache[song_id]
        observed_points, observed_descriptors = observed_features
        if descriptors is None or observed_descriptors is None or len(observed_descriptors) < 2:
            return result
        pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(descriptors, observed_descriptors, k=2)
        matches = [first for pair in pairs if len(pair) == 2 for first, second in [pair]
                   if first.distance < .7 * second.distance]
        if len(matches) < 16:
            return result
        source = np.float32([points[match.queryIdx].pt for match in matches])
        target = np.float32([observed_points[match.trainIdx].pt for match in matches])
        transform, mask = cv2.findHomography(source, target, cv2.RANSAC, 3)
        if transform is None or mask is None:
            return result
        inliers = mask.ravel().astype(bool)
        count, ratio = int(np.count_nonzero(inliers)), float(np.mean(inliers))
        if count < 16 or ratio < .6:
            return result
        corners = cv2.perspectiveTransform(np.float32([[[0, 0], [255, 0], [255, 255], [0, 255]]]), transform)[0]
        coverage = float(cv2.contourArea(cv2.convexHull(target[inliers])) / (256 * 256))
        area = float(cv2.contourArea(corners) / (256 * 256))
        # 评级标记遮挡、轻微缩放与透视由几何配准处理；局部相似图案不能冒充整张封面。
        confirmed = (np.all(np.isfinite(corners)) and cv2.isContourConvex(corners)
                     and np.all(corners >= -32) and np.all(corners <= 287)
                     and .65 <= area <= 1.45 and coverage >= .2)
        return dict(result, confirmed=bool(confirmed), inliers=count, inlier_ratio=ratio, coverage=coverage)

    def identify(self, jacket: np.ndarray, title: Reading, phase: str) -> tuple[int, float, float, float]:
        cover = {"status": "unavailable", "song_id": None}
        scores = self.features @ thumbnail(jacket) if len(self.ids) else np.empty(0)
        ranked = np.argsort(scores)[::-1]
        if len(ranked):
            best = float(scores[ranked[0]])
            second = float(scores[ranked[1]]) if len(ranked) > 1 else 0.0
            cover.update(status="ambiguous", best_song_id=self.ids[int(ranked[0])], score=best, runner_up_score=second)
            if best >= .85 and best - second >= .06:
                cover.update(status="matched", song_id=self.ids[int(ranked[0])], method="thumbnail")
            elif best >= .35:
                observed = cv2.AKAZE_create().detectAndCompute(cv2.cvtColor(jacket, cv2.COLOR_BGR2GRAY), None)
                checks = [self.registered_cover(self.ids[int(index)], observed) for index in ranked[:3]]
                verified = [check["song_id"] for check in checks if check["confirmed"]]
                cover["feature_checks"] = checks
                if len(verified) == 1:
                    cover.update(status="matched", song_id=verified[0], method="features")
        titles = sorted(((title_score(title.text, song["title"]), song_id)
                         for song_id, song in self.repository.songs.items()), reverse=True)
        score, title_id = titles[0] if titles else (0.0, None)
        margin = score - (titles[1][0] if len(titles) > 1 else 0.0)
        title_matched = title.confidence >= .85 and score >= .9 and margin >= .08
        title_evidence = {"status": "matched" if title_matched else "unconfirmed", "text": title.text,
                          "confidence": title.confidence, "best_song_id": title_id, "score": score, "margin": margin,
                          "song_id": title_id if title_matched else None}
        self.last_evidence = {"phase": phase, "cover": cover, "title": title_evidence}
        cover_id = cover["song_id"]
        if cover_id is not None and title_matched and cover_id != title_id:
            raise ValueError(f"封面与标题身份冲突：封面={cover_id}，标题={title_id}")
        song_id = cover_id if cover_id is not None else title_id if title_matched else None
        if song_id is None:
            raise ValueError(f"歌曲封面与标题均未确认：封面={cover.get('score', 0):.3f}，"
                             f"标题={title.text!r}，相似度={score:.3f}，置信度={title.confidence:.3f}")
        self.last_evidence["resolved_song_id"] = song_id
        self.last_evidence["identified_by"] = ("cover_and_title" if cover_id is not None and title_matched
                                               else "cover" if cover_id is not None else "title")
        chosen = self.ids.index(song_id) if song_id in self.ids else None
        best = float(scores[chosen]) if chosen is not None else 0.0
        other_scores = np.delete(scores, chosen) if chosen is not None else scores
        second = float(np.max(other_scores)) if len(other_scores) else 0.0
        return song_id, best, second, title_score(title.text, self.repository.songs[song_id]["title"])

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

    def read_result_title(self, frame: np.ndarray, box: tuple[int, int, int, int]) -> Reading:
        x1, y1, x2, y2 = box
        crop = frame[y1:y2, x1:x2]
        # 个人页标题使用灰底深字；去掉短标题后的留白，避免横向压缩漏掉长音和浊点。
        mask = cv2.inRange(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), 0, 120)
        points = cv2.findNonZero(mask)
        if points is None:
            return Reading("", 0.0)
        x, y, width, height = cv2.boundingRect(points)
        text = cv2.cvtColor(255 - mask[y:y + height, x:x + width], cv2.COLOR_GRAY2BGR)
        text = cv2.copyMakeBorder(text, 5, 5, 6, 6, cv2.BORDER_CONSTANT, value=(255, 255, 255))
        return self.ocr.read(text, (0, 0, text.shape[1], text.shape[0]))

    def match(self, frame: np.ndarray, difficulty: str, phase: str) -> Identity:
        if phase in {"prepare", "song_select"}:
            corners = np.float32([(910, 80), (1159, 92), (1146, 341), (897, 327)])
            target = np.float32([(0, 0), (255, 0), (255, 255), (0, 255)])
            jacket = cv2.warpPerspective(frame, cv2.getPerspectiveTransform(corners, target), (256, 256))
            title_box, difficulty_box = (821, 340, 1227, 378), (965, 443, 1090, 483)
        elif phase in {"final", "cooperative_final", "one_shot_final"}:
            jacket = frame[390:657, 95:361]
            title_box, difficulty_box = (384, 541, 1240, 581), (65, 653, 320, 692)
        elif phase in {"cooperative_select", "cooperative_prepare"}:
            corners = ([(886, 79), (1146, 92), (1131, 355), (872, 341)] if phase == "cooperative_select"
                       else [(922, 79), (1182, 92), (1171, 355), (904, 341)])
            target = np.float32([(0, 0), (255, 0), (255, 255), (0, 255)])
            jacket = cv2.warpPerspective(frame, cv2.getPerspectiveTransform(np.float32(corners), target), (256, 256))
            title_box = (808, 340, 1170, 379) if phase == "cooperative_select" else (839, 340, 1238, 382)
        elif phase == "cooperative_result":
            jacket = frame[19:98, 152:231]
            title_box, difficulty_box = (248, 17, 627, 59), (268, 64, 374, 103)
        else:
            raise ValueError("歌曲匹配阶段无效")
        # 两个通道都先完成判断，封面分数不足不能阻止标题提供独立身份。
        title = (self.read_opening_text(frame, title_box) if phase in {"final", "cooperative_final", "one_shot_final"} else
                 self.read_result_title(frame, title_box) if phase == "cooperative_result" else
                 self.read_preparation_title(frame, title_box))
        song_id, best, second, similarity = self.identify(jacket, title, phase)
        song = self.repository.songs[song_id]
        chart = song["charts"].get(difficulty, {})
        self.last_evidence["chart_available"] = bool(chart)
        if phase == "one_shot_final":
            # 一键任务仅确认最终封面与标题；用户负责使任务难度和已手动选择的游戏难度一致。
            self.last_evidence["difficulty"] = {"requested": difficulty, "source": "task_setting", "confirmed": None}
        elif phase not in {"song_select", "cooperative_select", "cooperative_prepare"}:
            badge = (self.read_opening_text(frame, difficulty_box, badge=True) if phase in {"final", "cooperative_final"}
                     else self.ocr.read(frame, difficulty_box))
            confirmed = badge.confidence >= .7 and normalize_title(badge.text) == difficulty
            self.last_evidence["difficulty"] = {"requested": difficulty, "text": badge.text,
                                                "confidence": badge.confidence, "confirmed": confirmed}
            if not confirmed:
                raise ValueError(f"实际难度不一致：请求={difficulty}，实读={badge.text!r}，置信度={badge.confidence:.3f}")
        if difficulty not in song["charts"] and phase not in {"cooperative_prepare", "cooperative_final", "one_shot_final"}:
            raise ValueError(f"本地缺少歌曲 {song_id} 的 {difficulty} 谱面")
        if phase == "cooperative_prepare":
            confirmed = cooperative_difficulty_selected(frame, difficulty)
            self.last_evidence["difficulty"] = {"requested": difficulty, "confirmed": confirmed}
            if not confirmed:
                raise ValueError(f"协力实际难度未选中：{difficulty}")
        return Identity(song_id, song["title"], difficulty, chart.get("play_level", 0), best, second,
                        title.text, title.confidence, similarity, phase)
