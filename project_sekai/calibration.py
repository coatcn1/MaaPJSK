from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
from uuid import uuid4

from .chart_catalog import _write_json
from . import native_engine
from .chart_player import START_ANCHOR_VERSION, TOUCH_PLAN_VERSION


def environment_signature(device) -> dict:
    device.preflight()
    info = device.controller.info
    if not isinstance(info, dict) or not info.get("adb_serial"):
        raise RuntimeError("无法确认当前 MFA 设备，拒绝使用校准配置")
    identity = hashlib.sha256(str(info["adb_serial"]).encode()).hexdigest()
    capture = hashlib.sha256(json.dumps({key: info.get(key) for key in ("screencap_methods", "input_methods", "config")},
                                      sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    package = device.shell("dumpsys package com.sega.pjsekai")
    version = next((line.strip().split("versionName=", 1)[1] for line in package.splitlines() if "versionName=" in line), "")
    if not version:
        raise RuntimeError("无法读取当前日服版本，拒绝使用校准配置")
    return {"device_hash": identity, "controller_hash": capture, "game_version": version,
            "width": 1280, "height": 720, "dpi": 240, "render_mode": "light",
            "engine": "native", "engine_version": native_engine.module().version(),
            "touch_plan_version": TOUCH_PLAN_VERSION, "start_anchor_version": START_ANCHOR_VERSION}


def profile_key(environment: dict, difficulty: str) -> str:
    digest = hashlib.sha256(json.dumps(environment, sort_keys=True).encode()).hexdigest()[:20]
    return f"{difficulty}-{digest}"


class CalibrationProfiles:
    def __init__(self, root: Path):
        self.root = root

    def load(self, environment: dict, difficulty: str):
        path = self.root / f"{profile_key(environment, difficulty)}.json"
        if not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if (value.get("schema_version") != 1 or value.get("accepted") is not True
                or value.get("environment") != environment or value.get("difficulty") != difficulty
                or type(value.get("offset_ms")) is not int or not -600 <= value["offset_ms"] <= 600
                or not validation_passed(value.get("validation_report", {}))):
            raise ValueError("当前 Native 校准配置无效，请重新运行校准任务")
        return value

    def save(self, profile: dict):
        if profile.get("accepted") is not True or not validation_passed(profile.get("validation_report", {})):
            raise ValueError("未通过正式验证的校准结果不能替换已启用配置")
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{profile_key(profile['environment'], profile['difficulty'])}.json"
        _write_json(path, profile)
        return path


def timing_feedback(result: dict) -> dict:
    fast, late = result.get("fast"), result.get("late")
    if type(fast) is not int or type(late) is not int or min(fast, late) < 0:
        raise ValueError("排练结算 FAST/LATE 未完整读取，不能推算偏移")
    feedback, error = fast + late, late - fast
    tolerance = max(2, round(feedback * .10))
    return {"fast": fast, "late": late, "count": feedback, "error": error,
            "tolerance": tolerance, "balanced": abs(error) <= tolerance}


def adjusted_offset(current: int, result: dict) -> int:
    values = timing_feedback(result)
    feedback, error = values["count"], values["error"]
    if values["balanced"]:
        return current
    ratio = feedback / max(1, result["total"])
    step = 48 if ratio >= .6 else 24 if ratio >= .35 else 12 if ratio >= .02 else 3
    delta = round(step * error / feedback) or (1 if error > 0 else -1)
    # PJSK 的正值表示延后，与参考项目的提前约定相反；LATE 多时减小偏移。
    return max(-250, min(250, current - delta))


def valid_calibration_evidence(report: dict) -> bool:
    result = report.get("judgements", {})
    playback = report.get("playback", {})
    return bool(report.get("completed") is True and report.get("live_status") == "cleared"
                and result.get("total_matches_chart") is True
                and playback.get("release_confirmed") is True
                and playback.get("planned_actions", 0) > 0
                and playback.get("planned_actions") == playback.get("sent_actions") == playback.get("executed_actions"))


def validation_passed(report: dict) -> bool:
    try:
        return valid_calibration_evidence(report) and timing_feedback(report.get("judgements", {}))["balanced"]
    except ValueError:
        return False


def verify_profile_anchor(profile: dict, report: dict):
    reference = profile.get("anchor_rate")
    current = report.get("start_anchor", {}).get("trajectory_rate")
    if (not isinstance(reference, (int, float)) or not isinstance(current, (int, float))
            or not math.isfinite(reference) or not math.isfinite(current) or reference <= 0
            or abs(current / reference - 1) > .15):
        raise RuntimeError("首音下落速度与校准配置不同，请确认游戏流速并重新校准")


class CalibrationRunner:
    def __init__(self, workflow, profiles: CalibrationProfiles, environment: dict, settings, session_root: Path, *,
                 test_bonus_consumption=0, initial_offset_ms=None, test_recovery_mode="off", test_recovery_count=1):
        self.workflow, self.profiles, self.environment, self.settings = workflow, profiles, environment, settings
        self.session_root = session_root
        if test_bonus_consumption != "current" and (type(test_bonus_consumption) is not int or not 0 <= test_bonus_consumption <= 10):
            raise ValueError("校准验收体力配置无效")
        self.test_bonus_consumption = test_bonus_consumption
        if initial_offset_ms is not None and (type(initial_offset_ms) is not int or not -250 <= initial_offset_ms <= 250):
            raise ValueError("校准验收起始偏移无效")
        self.initial_offset_ms = initial_offset_ms
        if (test_recovery_mode not in {"off", "small", "large"} or type(test_recovery_count) is not int
                or not 1 <= test_recovery_count <= 99):
            raise ValueError("校准验收用药配置无效")
        self.test_recovery_mode, self.test_recovery_count = test_recovery_mode, test_recovery_count

    def warm_candidate(self, difficulty: str):
        # 未通过平衡门槛的候选只能用于下一次排练，不能作为正常演奏配置启用。
        best = None
        allowed_root = (self.session_root.parent / "solo-chart-runs").resolve()
        for path in sorted(self.session_root.glob("*/session.json"), reverse=True)[:12]:
            try:
                session = json.loads(path.read_text(encoding="utf-8-sig"))
                if session.get("environment") != self.environment or session.get("difficulty") != difficulty:
                    continue
                seed = session.get("warm_seed_offset_ms")
                if "warm_seed_offset_ms" not in session and session.get("initial_offset_source") in {"manual", "explicit"}:
                    seed = session.get("initial_offset_ms")
                if type(seed) is not int:
                    seed = None
                for item in reversed(session.get("rounds", [])):
                    try:
                        report_path = Path(item["report"]).resolve()
                        if not report_path.is_relative_to(allowed_root):
                            continue
                        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
                        playback = report.get("playback", {})
                        values = report.get("judgements", {})
                        if not valid_calibration_evidence(report):
                            continue
                        feedback = timing_feedback(values)
                        # 失败候选仅延续同一明确起点，不能用未知沿袭覆盖用户当前手动值。
                        if not feedback["balanced"] and seed != self.settings.touch_offset_ms:
                            continue
                        # 先选已平衡候选，再比较偏斜比例和绝对差；相同证据保留较新会话。
                        rank = (not feedback["balanced"], abs(feedback["error"]) / max(1, feedback["count"]),
                                abs(feedback["error"]))
                        offset = report.get("timing_offset_ms")
                        if type(offset) is int and -250 <= offset <= 250 and (best is None or rank < best["rank"]):
                            best = {"rank": rank, "feedback": feedback, "offset": offset, "latency": playback.get("latency_offsets", {}),
                                    "session": path.parent.name, "seed_offset_ms": seed, "report": str(report_path)}
                    except (OSError, ValueError, KeyError, TypeError):
                        # 单轮缺失或损坏不应掩盖同一会话内其他完整证据。
                        continue
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return best

    def run(self, difficulty: str, song_mode: str):
        directory = self.session_root / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:8])
        directory.mkdir(parents=True)
        session = {"schema_version": 1, "environment": self.environment, "difficulty": difficulty,
                   "started_at": datetime.now().astimezone().isoformat(), "status": "running", "rounds": [],
                   "settings_snapshot": asdict(self.settings), "bonus_consumption": self.test_bonus_consumption}
        session["recovery_settings"] = {"mode": self.test_recovery_mode, "count": self.test_recovery_count}
        previous = self.profiles.load(self.environment, difficulty)
        # 配置保存的是相对手动值的残差；新环境也必须沿用用户的实际手动起点。
        offset = self.settings.touch_offset_ms + (previous["offset_ms"] if previous else 0)
        source = "profile" if previous else "manual"
        seed = None if previous else self.settings.touch_offset_ms
        latency = previous.get("latency_offsets", {}) if previous else {}
        if previous is None:
            warm = self.warm_candidate(difficulty)
            if warm is not None:
                offset, latency = warm["offset"], warm["latency"]
                source = "warm"
                seed = warm["seed_offset_ms"]
                session["warm_start"] = warm["session"]
                session["warm_start_report"] = warm["report"]
        if self.initial_offset_ms is not None:
            offset = self.initial_offset_ms
            session["test_initial_offset_ms"] = offset
            source = "explicit"
            seed = offset
        session["initial_offset_ms"] = offset
        session["initial_offset_source"] = source
        session["warm_seed_offset_ms"] = seed
        pending_profile = None
        bonus_snapshot = {}
        try:
            _write_json(directory / "session.json", session)
            for index, stage in enumerate(("rehearsal", "formal-validation"), 1):
                bonus_label = "沿用游戏体力设置" if self.test_bonus_consumption == "current" else f"{self.test_bonus_consumption} 体力"
                self.workflow.log(f"Native 校准：{stage}，第 {index} / 2 局，{bonus_label}，触控偏移 {offset} ms")
                reports = self.workflow.run(1, difficulty, song_mode if index == 1 else "current", offset,
                                            bonus_consumption=self.test_bonus_consumption, recovery_mode=self.test_recovery_mode,
                                            recovery_count=self.test_recovery_count,
                                            engine="native", latency_offsets=latency, _bonus_snapshot=bonus_snapshot)
                report = reports[-1]
                session.setdefault("original_bonus_consumption", report.get("bonus", {}).get("original_consumption"))
                if index == 2 and report["preparation_identity"]["song_id"] != session["rounds"][0]["song_id"]:
                    raise RuntimeError("排练与正式验证歌曲不同，不能接受校准配置")
                latency = report["playback"].get("latency_offsets", {})
                session["rounds"].append({"stage": stage, "offset_ms": offset, "report": report["report_path"],
                                          "song_id": report["preparation_identity"]["song_id"], "judgements": report["judgements"]})
                self.workflow.log(f"Native 校准：已完成 {index} / 总数 2；{stage}")
                if index == 1:
                    offset = adjusted_offset(offset, report["judgements"])
                    session["suggested_offset_ms"] = offset
                    session["rehearsal_feedback"] = timing_feedback(report["judgements"])
                    _write_json(directory / "session.json", session)
                else:
                    if not validation_passed(report):
                        session["status"] = "rejected"
                        raise RuntimeError("Native 正式验证未通过：要求 LIVE CLEAR、音数匹配、完整执行和释放及完整 FAST/LATE 平衡；本次候选未启用")
                    profile = {"schema_version": 1, "accepted": True, "environment": self.environment,
                               "difficulty": difficulty, "created_at": datetime.now().astimezone().isoformat(),
                               "offset_ms": offset - self.settings.touch_offset_ms, "latency_offsets": latency,
                               "anchor_rate": report["start_anchor"]["trajectory_rate"],
                               "validation": report["judgements"], "validation_report": report,
                               "validation_feedback": timing_feedback(report["judgements"]),
                               "calibration_session": directory.name}
                    pending_profile = profile
                    session["status"] = "validated"
            return session
        except Exception as error:
            session["error"] = f"{type(error).__name__}: {error}"
            if session["status"] == "running":
                session["status"] = "interrupted" if self.workflow.stop_requested() else "failed"
            raise
        finally:
            # 校准保留用户指定的消耗；沿用模式保持开场设置，不改写 MFA 保存的选项。
            original = session.get("original_bonus_consumption")
            if original is None:
                original = getattr(self.workflow, "last_report", {}).get("bonus", {}).get("original_consumption")
            if type(self.test_bonus_consumption) is int:
                original = self.test_bonus_consumption
            restoration_error = None
            if type(original) is int and hasattr(self.workflow, "restore_calibration_bonus"):
                try:
                    # 后一局导航失败可能还未创建消耗证据；已保存的任务快照仍证明设置保留。
                    latest = bonus_snapshot or getattr(self.workflow, "last_report", {}).get("bonus", {})
                    if latest.get("confirmed") is True and latest.get("consumption") == original:
                        # 已确认并且未改动的用户数量不重复打开菜单，也不掩盖准备页的原始错误。
                        session["bonus_restoration"] = {"confirmed": True, "consumption": original, "retained": True}
                    else:
                        session["bonus_restoration"] = self.workflow.restore_calibration_bonus(original, directory)
                except Exception as error:
                    restoration_error = error
                    session["bonus_restoration_error"] = f"{type(error).__name__}: {error}"
                    self.workflow.log(f"校准已停止，但原体力消耗恢复未确认：{error}")
            if pending_profile is not None and session["status"] == "validated":
                if restoration_error is not None or self.workflow.stop_requested():
                    session["status"] = "interrupted" if self.workflow.stop_requested() else "failed"
                else:
                    session["profile"] = self.profiles.save(pending_profile).name
                    session["status"] = "accepted"
                    feedback = pending_profile["validation_feedback"]
                    self.workflow.log(f"Native 校准已通过并保存：实际偏移 {offset} ms；FAST {feedback['fast']} / LATE {feedback['late']}，允许差值 {feedback['tolerance']}")
            session["finished_at"] = datetime.now().astimezone().isoformat()
            _write_json(directory / "session.json", session)
            if restoration_error is not None and not self.workflow.stop_requested():
                raise RuntimeError("校准后的原体力消耗恢复未确认；本次候选未启用") from restoration_error
