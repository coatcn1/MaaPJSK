from __future__ import annotations

import json
import math
from pathlib import Path
import zlib

import cv2
import numpy as np

from .song_identity import read_image


GAME_TIMING_FEEDBACK_VERSION = 1
TIMING_AREA = (555, 383, 727, 424)
JUDGEMENT_AREA = (535, 421, 750, 469)
COMBO_AREA = (980, 270, 1200, 367)


def glyph_mask(image, ink):
    blue, green, red = cv2.split(image[:, :, :3])
    if ink == "white":
        mask = (blue >= 232) & (green >= 232) & (red >= 232)
    elif ink == "pink":
        mask = (blue >= 180) & (red >= 180) & (green.astype(np.float32) < np.minimum(blue, red) * .83)
    elif ink == "pastel":
        low = np.minimum(np.minimum(blue, green), red).astype(np.int16)
        high = np.maximum(np.maximum(blue, green), red).astype(np.int16)
        mask = (low >= 145) & (high >= 220) & (high - low >= 15)
    else:
        raise ValueError("游戏判定模板颜色无效")
    return mask.astype(np.uint8) * 255


class GameTimingTemplates:
    """只匹配固定 HUD 的字形；方向文字与 GREAT 必须在同帧同时成立。"""

    def __init__(self, templates, threshold=.90, direction_margin=.08):
        if set(templates) != {"fast", "late", "great", "perfect"}:
            raise ValueError("游戏判定模板必须包含 FAST、LATE、GREAT 与 PERFECT")
        self.templates = templates
        self.threshold = float(threshold)
        self.direction_margin = float(direction_margin)
        if not .90 <= self.threshold <= 1 or not .08 <= self.direction_margin <= 1:
            raise ValueError("游戏判定模板门槛无效")
        for name, template in templates.items():
            area = JUDGEMENT_AREA if name in {"great", "perfect"} else TIMING_AREA
            if (template.ndim != 2 or template.dtype != np.uint8 or not 3 <= template.shape[0] <= area[3] - area[1]
                    or not 3 <= template.shape[1] <= area[2] - area[0]
                    or np.count_nonzero(template) < 20 or np.count_nonzero(template) == template.size
                    or not np.isin(template, (0, 255)).all()):
                raise ValueError(f"游戏判定字形模板无效：{name}")

    @classmethod
    def load(cls, path: Path):
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if value.get("schema_version") != 1:
            raise ValueError("游戏判定模板版本无效")
        templates = {}
        for name, relative in value["templates"].items():
            image = read_image(path.parent / relative)
            if not np.array_equal(image[:, :, 0], image[:, :, 1]) or not np.array_equal(image[:, :, 0], image[:, :, 2]):
                raise ValueError("游戏判定模板必须是二值灰度图")
            templates[name] = image[:, :, 0]
        return cls(templates, value.get("threshold", .90), value.get("direction_margin", .08))

    @staticmethod
    def _score(mask, template):
        result = cv2.matchTemplate(mask, template, cv2.TM_CCOEFF_NORMED)
        score = float(result.max())
        return score if math.isfinite(score) else 0.

    def observe(self, frame):
        if frame.shape[:2] != (720, 1280):
            return {"direction": None, "judgement": None, "reason": "unsupported_frame_size"}
        x1, y1, x2, y2 = TIMING_AREA
        timing = glyph_mask(frame[y1:y2, x1:x2], "white")
        x1, y1, x2, y2 = JUDGEMENT_AREA
        judgement = glyph_mask(frame[y1:y2, x1:x2], "pink")
        perfect = glyph_mask(frame[y1:y2, x1:x2], "pastel")
        scores = {name: self._score(judgement if name == "great" else perfect if name == "perfect" else timing, template)
                  for name, template in self.templates.items()}
        direction = "fast" if scores["fast"] >= scores["late"] else "late"
        other = "late" if direction == "fast" else "fast"
        reason = "confirmed"
        if scores["great"] >= self.threshold and scores["perfect"] >= self.threshold:
            reason = "judgement_conflict"
        elif scores["perfect"] >= self.threshold:
            reason = "perfect_confirmed"
        elif scores["great"] < self.threshold:
            reason = "great_unconfirmed"
        elif scores["fast"] >= self.threshold and scores["late"] >= self.threshold:
            reason = "direction_conflict"
        elif scores[direction] < self.threshold or scores[direction] - scores[other] < self.direction_margin:
            reason = "direction_unconfirmed"
        x1, y1, x2, y2 = COMBO_AREA
        combo = glyph_mask(frame[y1:y2, x1:x2], "white")
        signature = f"{zlib.crc32(combo.tobytes()):08x}" if np.count_nonzero(combo) >= 150 else None
        if reason == "confirmed" and signature is None:
            reason = "combo_unconfirmed"
        return {"direction": direction if reason == "confirmed" else None,
                "judgement": None if reason == "judgement_conflict" else "perfect" if reason == "perfect_confirmed" else "great" if scores["great"] >= self.threshold else None,
                "scores": scores, "combo_signature": signature, "reason": reason}


class GameTimingGuard:
    def __init__(self):
        self.correction_ms = 0.
        self.last_elapsed = None
        self.last_signature = None
        self.direction = None
        self.streak = 0
        self.evidence = []
        self.last_adjustment_elapsed = None
        self.report = {"version": GAME_TIMING_FEEDBACK_VERSION, "enabled": True, "effective": True,
                       "status": "observing", "source": "game_fast_late_and_great_templates",
                       "sample_interval_s": 2., "required_streak": 3, "step_ms": 5., "limit_ms": 60.,
                       "evidence_window_s": 12., "adjustment_interval_s": 6.,
                       "correction_ms": 0., "observations": [], "adjustments": [], "dropped_observations": 0}

    def observe(self, evidence, elapsed):
        elapsed = float(elapsed)
        if not math.isfinite(elapsed) or (self.last_elapsed is not None and elapsed - self.last_elapsed < 2. - 1e-6):
            return None
        if self.last_elapsed is not None and elapsed - self.last_elapsed > 3.5:
            self.direction, self.streak, self.evidence = None, 0, []
        self.last_elapsed = elapsed
        self.evidence = [row for row in self.evidence if elapsed - row["elapsed_s"] <= 12. + 1e-6]
        if not self.evidence:
            self.direction = None
        row = {"elapsed_s": elapsed, **evidence}
        direction = evidence.get("direction")
        signature = evidence.get("combo_signature")
        if signature and signature == self.last_signature:
            self.direction, self.evidence = None, []
            row["reason"] = "unchanged_combo"
        elif (direction in {"fast", "late"} and evidence.get("judgement") == "great" and signature):
            if direction != self.direction:
                self.evidence = []
            self.direction = direction
            self.evidence.append({"elapsed_s": elapsed, "direction": direction, "combo_signature": signature,
                                  "scores": dict(evidence.get("scores", {}))})
            self.evidence = self.evidence[-3:]
        elif evidence.get("reason") not in {"great_unconfirmed", "direction_unconfirmed", "combo_unconfirmed"}:
            # 文字短暂消失可等待下一张有效判定；明确 PERFECT、矛盾或 HUD 未知会清空旧票。
            self.direction, self.evidence = None, []
        else:
            row["reason"] = "unknown_no_vote"
        self.streak = len(self.evidence)
        if signature:
            self.last_signature = signature
        adjustment = None
        if self.streak >= 3 and (self.last_adjustment_elapsed is None or elapsed - self.last_adjustment_elapsed >= 6. - 1e-6):
            # EARLY 只延后尚未发布的输入；不改首音 epoch、手动偏移或已排入设备的队列。
            delta = 5. if self.direction == "fast" else -5.
            updated = float(np.clip(self.correction_ms + delta, -60., 60.))
            if updated != self.correction_ms:
                adjustment = {"elapsed_s": elapsed, "direction": self.direction, "evidence_count": self.streak,
                              "old_ms": self.correction_ms, "new_ms": updated,
                              "evidence": list(self.evidence),
                              "reason": "three_consecutive_game_great_samples"}
                self.correction_ms = updated
                self.report["adjustments"].append(adjustment)
                self.last_adjustment_elapsed = elapsed
                row["reason"] = "adjusted"
            else:
                row["reason"] = "correction_limit"
            self.direction, self.streak, self.evidence = None, 0, []
        row.update(streak=self.streak, correction_ms=self.correction_ms)
        self.report["correction_ms"] = self.correction_ms
        self.report["latest_observation"] = row
        if len(self.report["observations"]) < 4096:
            self.report["observations"].append(row)
        else:
            self.report["dropped_observations"] += 1
        return adjustment
