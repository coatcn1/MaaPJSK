from __future__ import annotations

from collections import deque
from dataclasses import asdict
from datetime import datetime
import csv
import json
from pathlib import Path
import re
import time
from uuid import uuid4

import numpy as np

from .chart_catalog import _write_json
from .chart_player import compile_touches
from .game_timing import GameTimingGuard, GameTimingTemplates
from .life_monitor import LifeDepleted, LifeGuard, ZeroLifeTemplate
from .live_end import LIFE_HUD_AREA, LiveEndGuard, blank_transition, input_finished, release_finished
from .live_exit import background_game as exit_to_emulator_home
from .navigator import Navigator
from .solo_live import LiveResult, SoloLive, is_light_mode
from .song_identity import COOPERATIVE_DIFFICULTIES, cooperative_difficulty_selected, read_image, write_image
from .sus_chart import parse_sus


JUDGEMENTS = ("perfect", "great", "good", "bad", "miss")
PAGE_AREAS = {
    "home": (1127, 620, 1272, 694),
    "title_screen": (528, 590, 752, 646),
    "cooperative_room": (730, 178, 1040, 275),
    "cooperative_matching": (60, 10, 342, 62),
    "cooperative_member_waiting": (40, 340, 1240, 385),
    "cooperative_member_decided": (1030, 638, 1220, 695),
    "cooperative_shuffle": (860, 575, 1145, 665),
    "cooperative_select": (982, 504, 1168, 585),
    "cooperative_ready": (933, 555, 1118, 633),
    "cooperative_cancel": (933, 555, 1118, 633),
    "cooperative_disbanded": (460, 335, 830, 388),
    "cooperative_disbanded_dialog": (482, 310, 797, 363),
    "cooperative_disbanded_dialog_ok": (509, 380, 773, 469),
    "cooperative_personal_result": (133, 388, 282, 607),
    "cooperative_result_banner": (118, 202, 992, 339),
    "cooperative_total_score": (733, 467, 874, 515),
    "playing": (1190, 10, 1256, 80),
}
ENTRY_WAIT_SECONDS = 60
ROOM_WAIT_SECONDS = 180
PLAY_OBSERVATION_INTERVAL = 2.0
COOPERATIVE_LIFE_BAR = (1008, 48, 1177, 56)


class CooperativePlaybackInterrupted(RuntimeError):
    """未完成谱面输入时，连续确认实际已转入同一非演奏页面。"""


class RoomIdleTimeout(RuntimeError):
    """房内连续无正向进展超过期限；清理后仅在明确安全房内页允许 ESC 恢复。"""


class RoomDisbanded(RuntimeError):
    """仅在匹配、选曲、准备及加载阶段允许返回房间选择后重匹配。"""


class RoomMatchingTimeout(RoomDisbanded):
    """成员房间等待超时，确认安全退出后复用有限重匹配。"""


class MissingCooperativeChart(ValueError):
    """实际抽选歌曲在本地缺谱或资源校验失败，不派发谱面输入。"""


def append_cooperative_result_index(root, directory, report):
    identity = report.get("resolved_identity") or report.get("preparation_identity", {})
    values = report.get("judgements", report.get("partial_judgements", {}))
    playback = (report.get("playback") or {})
    row = {"run": directory.name, "started_at": report["started_at"], "room": report["room"],
           "song_id": identity.get("song_id"), "title": identity.get("title"),
           "difficulty": report["requested_difficulty"], "completed": report.get("completed", False),
           "live_status": report.get("live_status", "unknown"), "result_status": report.get("result_status", "not_available"),
           "error": report.get("error"),
           **{key: values.get(key) for key in (*JUDGEMENTS, "total", "perfect_rate", "hit_rate",
                                             "combo", "score", "late", "fast", "flick", "total_matches_chart")},
           **{key: playback.get(key) for key in ("planned_actions", "sent_actions", "executed_actions", "release_confirmed")}}
    path = root / "results.csv"
    rows = []
    if path.is_file() and path.stat().st_size > 0:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        if any(item.get("run") == directory.name for item in rows):
            return
    temporary = path.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(row))
        writer.writeheader()
        writer.writerows(rows)
        writer.writerow(row)
    temporary.replace(path)


class CooperativeNavigator(Navigator):
    def __init__(self, device, base_config: Path, cooperative_config: Path, **kwargs):
        super().__init__(device, base_config, **kwargs)
        config = json.loads(cooperative_config.read_text(encoding="utf-8-sig"))
        for name, relative in config["templates"].items():
            if not name.startswith("cooperative_"):
                raise ValueError("协力模板不得覆盖单人模板")
            self.templates[name] = read_image(cooperative_config.parent / relative)
        missing = (set(PAGE_AREAS) - {"playing", "cooperative_member_decided", "cooperative_shuffle"} | {"life_hud"}) - self.templates.keys()
        if missing:
            raise ValueError(f"缺少协力模板：{', '.join(sorted(missing))}；请先生成本机协力模板")
        self.zero_life_template = (ZeroLifeTemplate(self.templates["life_zero_value"])
                                   if "life_zero_value" in self.templates else None)

    def match(self, frame, name, area=None):
        # 共用回复流程在公房匹配前执行；其返回页名称仍为 prepare，实际证据来自房间选择页。
        if name == "prepare":
            name = "cooperative_room"
        return super().match(frame, name, area or PAGE_AREAS.get(name))

    def ensure_auto(self, enabled):
        self._check_stop()
        if enabled:
            raise ValueError("协力没有游戏内 AUTO 入口，只能按本地谱面演奏")


class CooperativeLive(SoloLive):
    def __init__(self, device, template_config: Path, cooperative_config: Path, chart_root: Path,
                 model_root: Path, report_root: Path, *, stop_requested=lambda: False, log_message=print):
        super().__init__(device, template_config, chart_root, model_root, report_root,
                         stop_requested=stop_requested, log_message=log_message)
        self.navigator = CooperativeNavigator(device, template_config, cooperative_config,
                                             stop_requested=stop_requested, log_message=log_message,
                                             bonus_reader=self.read_available_bonus)
        self.current_report = None
        self.matched = False
        self.game_timing_config = template_config.parent / "game-timing-templates.json"
        self.game_timing_guard = None
        self.game_timing_templates = None

    def prepare_game_timing_feedback(self, report, enabled, engine):
        self.game_timing_guard = None
        self.game_timing_detector_failed = False
        status = "disabled" if not enabled else "unsupported_engine"
        if enabled and engine == "native":
            try:
                if self.game_timing_templates is None:
                    self.game_timing_templates = GameTimingTemplates.load(self.game_timing_config)
                self.game_timing_guard = GameTimingGuard()
                report["game_timing_feedback"] = self.game_timing_guard.report
                return
            except (OSError, KeyError, TypeError, ValueError) as error:
                # 可选判定模板不可用时维持原输入相位，不能让一局演出因此中止。
                status = "templates_unavailable"
                report["game_timing_feedback_error"] = f"{type(error).__name__}: {error}"
        report["game_timing_feedback"] = {"enabled": enabled, "effective": False,
                                          "status": status, "correction_ms": 0.}

    def observe_game_timing_feedback(self, frame, elapsed, sample):
        guard = getattr(self, "game_timing_guard", None)
        if guard is None or getattr(self, "game_timing_detector_failed", False):
            return
        began = time.perf_counter()
        try:
            if sample.get("playfield_visible") and not self.life_guard.zero_streak:
                evidence = self.game_timing_templates.observe(frame)
            else:
                evidence = {"direction": None, "judgement": None, "reason": "life_hud_unconfirmed_or_zero"}
            guard.observe(evidence, elapsed)
            sample["game_timing"] = dict(guard.report.get("latest_observation", {}))
        except Exception as error:
            # 判定反馈失效时冻结已生效的修正；触控与生命保护继续按原生命周期执行。
            self.game_timing_detector_failed = True
            guard.report.update(status="recognition_failed", error=f"{type(error).__name__}: {error}")
        finally:
            cost_ms = (time.perf_counter() - began) * 1000
            guard.report["max_detection_cost_ms"] = max(cost_ms, guard.report.get("max_detection_cost_ms", 0.))

    def score(self, frame, page):
        return self.navigator.match(frame, page, PAGE_AREAS.get(page))[0]

    def guard_room(self, frame):
        if (self.score(frame, "cooperative_disbanded") >= self.navigator.threshold
                or self.score(frame, "cooperative_disbanded_dialog") >= self.navigator.threshold
                or (self.matched and self.score(frame, "cooperative_room") >= self.navigator.threshold)):
            raise RoomDisbanded("房间已解散，等待返回房间选择页")

    def return_after_disbanded(self, report, directory):
        deadline = time.monotonic() + 20
        recovery = report.setdefault("room_recovery", {"dialog_detected": False, "ok_clicked": False,
                                                       "returned_to_room": False})
        last_notice_click = None

        def observe_notice_closed(frame):
            if blank_transition(frame):
                return
            if self.score(frame, "cooperative_disbanded") >= self.navigator.threshold:
                recovery["notice_dismissed"] = False
            elif recovery.get("notice_click_requested", False) and room_return_confirmed(frame):
                # 转场或一次负匹配不是关闭证明；确认无横幅的实际房间选择页才记成功。
                recovery["notice_dismissed"] = True

        def room_return_confirmed(frame):
            # 卡片可在提示后方先出现，阻挡输入的横幅或弹窗消失后才算返回成功。
            return (not blank_transition(frame)
                    and self.score(frame, "cooperative_room") >= self.navigator.threshold
                    and self.score(frame, "cooperative_disbanded") < self.navigator.threshold
                    and self.score(frame, "cooperative_disbanded_dialog") < self.navigator.threshold)

        while time.monotonic() < deadline:
            self.navigator._check_stop()
            frame = self.screenshot()
            observe_notice_closed(frame)
            if blank_transition(frame):
                self.pause(.15)
                continue
            if room_return_confirmed(frame):
                recovery["returned_to_room"] = True
                return
            if self.score(frame, "playing") >= self.navigator.threshold:
                raise RuntimeError("房间恢复时已进入演奏，拒绝点击确认或重匹配")
            if self.score(frame, "cooperative_disbanded") >= self.navigator.threshold:
                recovery["dialog_detected"] = True
                recovery.setdefault("notice_dismissed", False)
                recovery.setdefault("notice_click_count", 0)
                recovery.setdefault("notice_back_count", 0)
                count = recovery.setdefault("notice_dismiss_request_count", 0)
                if (not recovery["notice_dismissed"] and count < 3
                        and (last_notice_click is None or time.monotonic() - last_notice_click >= 1)):
                    self.safe_log("成员退出：关闭 (105) 横幅后等待房间选择页")
                    # 用户确认横幅点击和 BACK 都可关闭：首请求点击，持续横幅才有限使用 BACK。
                    confirmation = self.screenshot()
                    observe_notice_closed(confirmation)
                    if self.score(confirmation, "playing") >= self.navigator.threshold:
                        raise RuntimeError("房间恢复时已进入演奏，拒绝点击确认或重匹配")
                    if room_return_confirmed(confirmation):
                        recovery["returned_to_room"] = True
                        return
                    if self.score(confirmation, "cooperative_disbanded") >= self.navigator.threshold:
                        write_image(directory / "room-disbanded-notice.png", confirmation)
                        # 日志和编码可能跨过转场，必须在它们完成后再用新帧确认输入对象。
                        confirmation = self.screenshot()
                        observe_notice_closed(confirmation)
                        self.navigator._check_stop()
                        if time.monotonic() >= deadline:
                            break
                        if self.score(confirmation, "playing") >= self.navigator.threshold:
                            raise RuntimeError("房间恢复时已进入演奏，拒绝点击确认或重匹配")
                        if room_return_confirmed(confirmation):
                            recovery["returned_to_room"] = True
                            return
                        if (blank_transition(confirmation)
                                or self.score(confirmation, "cooperative_disbanded") < self.navigator.threshold):
                            self.pause(.15)
                            continue
                        last_notice_click = time.monotonic()
                        recovery["notice_click_requested"] = True
                        recovery["notice_dismiss_request_count"] = count + 1
                        method = "tap" if count == 0 else "back"
                        recovery.setdefault("notice_dismiss_requests", []).append({"at": last_notice_click, "method": method})
                        if method == "tap":
                            recovery["notice_click_count"] += 1
                            self.device.tap(640, 360)
                        else:
                            recovery["notice_back_count"] += 1
                            self.device.back()
            if self.score(frame, "cooperative_disbanded_dialog") >= self.navigator.threshold:
                recovery["dialog_detected"] = True
                if (not recovery["ok_clicked"]
                        and self.score(frame, "cooperative_disbanded_dialog_ok") >= self.navigator.threshold):
                    self.safe_log("成员房间已解散：确认 OK 后等待房间选择页")
                    # 日志可能阻塞；确认文字和按钮必须在实际点击前的同一张新帧上都成立。
                    confirmation = self.screenshot()
                    if (self.score(confirmation, "cooperative_disbanded_dialog") >= self.navigator.threshold
                            and self.score(confirmation, "cooperative_disbanded_dialog_ok") >= self.navigator.threshold):
                        write_image(directory / "room-disbanded-dialog.png", confirmation)
                        confirmation = self.screenshot()
                        self.navigator._check_stop()
                        if time.monotonic() >= deadline:
                            break
                        if self.score(confirmation, "playing") >= self.navigator.threshold:
                            raise RuntimeError("房间恢复时已进入演奏，拒绝点击确认或重匹配")
                        if room_return_confirmed(confirmation):
                            recovery["returned_to_room"] = True
                            return
                        if (blank_transition(confirmation)
                                or self.score(confirmation, "cooperative_disbanded_dialog") < self.navigator.threshold
                                or self.score(confirmation, "cooperative_disbanded_dialog_ok") < self.navigator.threshold):
                            self.pause(.15)
                            continue
                        self.device.tap(640, 425)
                        recovery["ok_clicked"] = True
            # 横幅只有限重试，带 OK 的确认弹窗仍只点一次；实际房间选择页才算恢复。
            self.pause(.15)
        write_image(directory / "room-recovery-timeout.png", frame)
        raise TimeoutError("房间解散后未返回房间选择页，停止重匹配")

    def return_after_matching_timeout(self, report, directory):
        playback = (self.current_report or {}).get("playback")
        if playback is not None and (playback.get("sent_actions", 0) or not release_finished(self.current_report)):
            raise RuntimeError("已有谱面输入或触点释放未确认，禁止退出成员房间重匹配")
        recovery = report.setdefault("room_recovery", {"dialog_detected": False, "ok_clicked": False,
                                                       "returned_to_room": False})
        recovery.update(reason="member_matching_timeout", exit_method="member_back_button", exit_limit=3)
        recovery.setdefault("exit_clicks", 0)
        recovery.setdefault("selection_arrived", False)
        recovery.setdefault("exit_unavailable_observed", False)
        deadline = recovery.setdefault("exit_deadline", time.monotonic() + 20)
        captured = False
        if time.monotonic() >= deadline:
            raise TimeoutError("成员房间退出的 20 秒恢复期限已耗尽，保留任务等待用户处理")

        def destination(frame):
            if blank_transition(frame):
                return None
            if not self.recovery_hud_absent(frame):
                raise RuntimeError("成员房间恢复时出现演奏 HUD，拒绝退出房间或重匹配")
            if self.score(frame, "cooperative_room") >= self.navigator.threshold:
                recovery["returned_to_room"] = True
                return "room"
            if self.score(frame, "playing") >= self.navigator.threshold:
                raise RuntimeError("成员房间恢复时已进入演奏，拒绝退出房间或重匹配")
            if self.score(frame, "cooperative_select") >= self.navigator.threshold:
                # 退出请求可能与满员转场竞争；尚未确认离房时继续原房间，不能结束正在推进的演出。
                recovery["selection_arrived"] = True
                return "selection"
            if (self.score(frame, "cooperative_disbanded") >= self.navigator.threshold
                    or self.score(frame, "cooperative_disbanded_dialog") >= self.navigator.threshold):
                raise RoomDisbanded("成员匹配恢复期间房间已解散，改用解散提示恢复")
            return None

        while time.monotonic() < deadline:
            self.check_runtime_stop()
            frame = self.screenshot()
            arrived = destination(frame)
            if arrived == "room":
                write_image(directory / "matching-room-return.png", frame)
                return None
            if arrived == "selection":
                return frame
            if recovery["exit_clicks"] < recovery["exit_limit"] and self.member_room_can_leave(frame):
                if not captured:
                    write_image(directory / "matching-timeout-before-exit.png", frame)
                    captured = True
                self.safe_log(f"成员匹配超时：点击返回箭头 {recovery['exit_clicks'] + 1}/{recovery['exit_limit']}")
                # 满员后 MATCHING 标题仍在；输入前的新帧还须确认空位及返回按钮可用。
                frame = self.screenshot()
                arrived = destination(frame)
                if arrived == "room":
                    write_image(directory / "matching-room-return.png", frame)
                    return None
                if arrived == "selection":
                    return frame
                if self.member_room_can_leave(frame):
                    self.check_runtime_stop()
                    if time.monotonic() >= deadline:
                        break
                    recovery["exit_clicks"] += 1
                    self.device.tap(43, 43)
                    self.pause(1)
                    continue
            if (self.score(frame, "cooperative_matching") >= self.navigator.threshold
                    and not self.member_room_can_leave(frame)):
                recovery["exit_unavailable_observed"] = True
            # 满员或返回不可用时等待原房间选歌；退出成功必须以实际房间选择页证明。
            self.pause(.15)
        write_image(directory / "matching-room-recovery-timeout.png", frame)
        raise TimeoutError("成员匹配超时后未返回房间选择页，停止重匹配")

    def member_room_can_leave(self, frame):
        if blank_transition(frame) or not self.recovery_hud_absent(frame):
            return False
        if self.score(frame, "cooperative_matching") < self.navigator.threshold:
            return False
        if any(self.score(frame, page) >= self.navigator.threshold for page in
               ("cooperative_select", "cooperative_ready", "cooperative_cancel", "cooperative_disbanded_dialog")):
            return False
        waiting = self.score(frame, "cooperative_member_waiting") >= self.navigator.threshold
        decided = ("cooperative_member_decided" in self.navigator.templates
                   and self.score(frame, "cooperative_member_decided") >= max(.90, self.navigator.threshold))
        if not (waiting or decided):
            return False
        # 归一化模板分数不能区分白色可用与灰色禁用按钮；另核对返回圆底的亮度覆盖。
        button = frame[17:68, 17:69]
        return np.mean(np.all(button > 225, axis=2)) >= .4

    def wait_page(self, page, timeout=30, *, guard=True):
        deadline = time.monotonic() + timeout
        frame = None
        while time.monotonic() < deadline:
            frame = self.screenshot()
            if guard:
                self.guard_room(frame)
            if self.score(frame, page) >= self.navigator.threshold:
                return frame
            if self.score(frame, "cooperative_matching") >= self.navigator.threshold:
                self.matched = True
            if self.score(frame, "playing") >= self.navigator.threshold:
                raise RuntimeError("准备流程已进入演奏，未确认歌曲或错过首音，拒绝从中途派发输入")
            self.pause(.15)
        if frame is not None:
            if page == "cooperative_select" and guard:
                # 截止时再取帧，避免用最后一张等待画面退出刚进入选曲的正常房间。
                frame = self.screenshot()
                self.guard_room(frame)
                if self.score(frame, page) >= self.navigator.threshold:
                    return frame
                if self.score(frame, "playing") >= self.navigator.threshold:
                    raise RuntimeError("选曲等待结束时已进入演奏，拒绝退出房间或从中途派发输入")
            write_image(self.current_directory / f"timeout-{page}.png", frame)
            if (page == "cooperative_select" and guard
                    and self.score(frame, "cooperative_matching") >= self.navigator.threshold):
                self.matched = True
                raise RoomMatchingTimeout(f"成员房间等待选曲超过 {timeout} 秒，返回房间选择页重匹配")
        raise TimeoutError(f"协力等待 {page} 超时，停止本轮")

    def tap_page(self, page, x, y, reason):
        frame = self.screenshot()
        self.guard_room(frame)
        if self.score(frame, page) < self.navigator.threshold:
            raise RuntimeError(f"{reason}前页面已改变，拒绝使用过期页面点击")
        if page in {"home", "live_menu"} and not self.recovery_hud_absent(frame):
            raise RuntimeError("主页入口出现演奏 HUD，拒绝点击")
        self.navigator.tap(x, y, reason)
        return frame

    def open_rooms(self):
        if (getattr(self, "resume_selection", False)
                or getattr(self, "resume_stage", None) in {"select", "shuffle", "ready", "cancel"}):
            return
        self.matched = False
        if getattr(self, "entry_deadline", None) is None:
            self.entry_deadline = time.monotonic() + ENTRY_WAIT_SECONDS
        last_request = None
        while True:
            self.check_runtime_stop()
            frame = self.screenshot()
            if not blank_transition(frame) and self.recovery_hud_absent(frame):
                if self.room_destination(frame):
                    # 实际到达房间选择页才结束这次入口计时；同目标的按钮请求预算继续保留。
                    self.entry_deadline = None
                    return frame
                if time.monotonic() >= self.entry_deadline:
                    raise TimeoutError("主页到多人房间选择超过 60 秒，保留本局等待安全恢复")
                if last_request is None or time.monotonic() - last_request >= 1:
                    if self.score(frame, "home") >= self.navigator.threshold and getattr(self, "entry_request_count", 0) < 3:
                        self.entry_request_count = getattr(self, "entry_request_count", 0) + 1
                        last_request = time.monotonic()
                        self.tap_page("home", 1194, 649, "主页 Live")
                    elif self.score(frame, "live_menu") >= self.navigator.threshold and getattr(self, "menu_request_count", 0) < 3:
                        self.menu_request_count = getattr(self, "menu_request_count", 0) + 1
                        last_request = time.monotonic()
                        self.tap_page("live_menu", 907, 235, "多人 Live")
            if time.monotonic() >= self.entry_deadline:
                raise TimeoutError("协力入口未在 60 秒内确认，保留本局等待安全恢复")
            self.pause(.25)

    def safe_log(self, message):
        # 日志 IPC 不是游戏状态证据，失效时仍保留可取消的恢复流程。
        try:
            self.log(message)
        except Exception:
            pass

    def check_runtime_stop(self):
        if self.stop_requested():
            raise InterruptedError("用户已停止协力")
        self.navigator._check_stop()

    def recovery_state(self, report, directory, state, error=None):
        previous = report.setdefault("runtime_recovery", {})
        detail = str(error) if error is not None else None
        if previous.get("state") == state and previous.get("detail") == detail:
            return
        previous.update(state=state, detail=detail)
        report["task_state"] = state
        if error is not None:
            previous["error"] = f"{type(error).__name__}: {error}"
        # 相同等待状态不刷日志和磁盘；恢复、释放及持久化证据分别记录。
        labels = {"waiting_release": "等待触点释放确认", "waiting_connection": "等待连接恢复",
                  "waiting_page": "等待安全页面", "waiting_room": "等待房间安全恢复",
                  "waiting_persistence": "等待结果文件可写", "waiting_resource": "等待用户补充体力或处理回复",
                  "ready": "已恢复准备"}
        try:
            self.safe_log(f"协力恢复：{labels.get(state, '等待恢复')}；{detail or '保留当前状态'}；本局未增加完成次数")
        except Exception:
            pass
        try:
            _write_json(directory / "report.json", report)
        except OSError as persistence_error:
            report["persistence_error"] = str(persistence_error)

    def close_pending_player(self, player, report, directory, *, cancelled=False):
        self.pending_native_player = player
        while True:
            try:
                player.close()
            except Exception as error:
                report["release_error"] = f"{type(error).__name__}: {error}"
            report["playback"] = player.report
            if release_finished(report):
                self.pending_native_player = None
                return
            if cancelled:
                return
            self.recovery_state(report, directory, "waiting_release")
            self.check_runtime_stop()
            self.pause(1.)

    def recover_runtime_failure(self, report, directory, error):
        self.current_report = self.last_report = report
        try:
            while True:
                try:
                    if report.get("death_confirmed"):
                        # 死亡已经确证，二次清理或落盘异常只能等待终止，不能恢复新局。
                        self.finish_death(report, directory)
                        self.persist_runtime_report(report, directory)
                        raise LifeDepleted("恢复等待中确认演出死亡，触点释放后结束协力整批")
                    return self.wait_runtime_destination(report, directory, error)
                except (InterruptedError, LifeDepleted):
                    raise
                except Exception as recovery_error:
                    state = "waiting_connection" if isinstance(recovery_error, OSError) else "waiting_page"
                    self.recovery_state(report, directory, state, recovery_error)
                    self.check_runtime_stop()
                    self.pause(2.)
        except InterruptedError:
            # 截图、恢复或退避中的新停止都经过同一审计出口；保留原失败原因。
            report["cancelled"] = True
            report["stopped_at"] = datetime.now().astimezone().isoformat()
            try:
                _write_json(directory / "report.json", report)
            except (OSError, ValueError):
                pass
            raise

    def wait_runtime_destination(self, report, directory, error):
        safe_backs = report.get("runtime_recovery", {}).get("safe_backs", 0)
        last_death_sample = float("-inf")
        while True:
            self.check_runtime_stop()
            if report.get("playback") is not None and not release_finished(report):
                # 连接恢复、主页截图或新的 Native 进程都不能替代上一轮 reset 的释放证明。
                self.recovery_state(report, directory, "waiting_release", error)
                self.pause(1.)
                continue
            captured_at = time.monotonic()
            try:
                frame = self.device.screenshot()
            except Exception as connection_error:
                self.recovery_state(report, directory, "waiting_connection", connection_error)
                self.pause(1.)
                continue
            if (report.get("skipped_after_input") or report.get("joined_room")
                    or report.get("ready_confirmed")) and time.monotonic() - last_death_sample >= PLAY_OBSERVATION_INTERVAL:
                last_death_sample = time.monotonic()
                if self.observe_recovery_death(frame, directory, captured_at=captured_at):
                    report["death_confirmed"] = True
                    report["death_confirmed_at"] = datetime.now().astimezone().isoformat()
                    self.finish_death(report, directory)
                    self.persist_runtime_report(report, directory)
                    raise LifeDepleted("恢复等待中确认演出死亡，触点释放后结束协力整批")
            if report.get("rematch_limit_reached"):
                self.recovery_state(report, directory, "waiting_room", error)
                self.pause(2.)
                continue
            if not blank_transition(frame):
                hud_absent = self.recovery_hud_absent(frame)
                attempt = (report["attempts"][-1] if report.get("attempts") else report.setdefault("runtime_attempt", {}))
                matching_recovery = attempt.get("room_recovery", {})
                no_input = (report.get("playback") or {}).get("sent_actions", 0) == 0
                if (not getattr(self, "room_progress", None) and attempt.get("member_wait_deadline") is not None
                        and self.room_stage(frame) == "matching"):
                    self.room_progress = {"stage": "matching", "deadline": attempt["member_wait_deadline"]}
                stage = self.observe_room_progress(frame, enforce=False) if hud_absent and no_input else None
                progress = getattr(self, "room_progress", {})
                if stage is not None:
                    # ACK 失败也可能已实际进房；唯一房内新帧才补记入房证明并沿袭预算。
                    self.matched = True
                    report["joined_room"] = True
                    attempt.setdefault("join_confirmed_at", captured_at)
                    attempt.setdefault("join_confirmed_page", "cooperative_" + stage)
                    self.join_deadline = None
                    if stage == "matching":
                        attempt.setdefault("member_wait_deadline", progress["deadline"])
                    if time.monotonic() >= progress["deadline"]:
                        if (matching_recovery.get("exit_requests", matching_recovery.get("exit_clicks", 0)) >= 3
                                or time.monotonic() >= matching_recovery.get("exit_deadline", float("inf"))):
                            self.recovery_state(report, directory, "waiting_room", "房内 ESC 请求或 20 秒期限已耗尽，等待安全返回")
                            self.pause(2.)
                            continue
                        destination = self.return_after_room_timeout(attempt, directory)
                        if destination is not None:
                            frame, stage = destination
                            self.resume_stage = stage
                            self.resume_selection = stage == "select"
                            self.entry_deadline = None
                            self.device.preflight()
                            self.recovery_state(report, directory, "ready")
                            return
                        frame = self.device.screenshot()
                        hud_absent = self.recovery_hud_absent(frame) and not blank_transition(frame)
                    elif stage != "matching":
                        self.resume_stage = stage
                        self.resume_selection = stage == "select"
                        self.entry_deadline = None
                        self.device.preflight()
                        self.recovery_state(report, directory, "ready")
                        return
                    else:
                        self.recovery_state(report, directory, "waiting_room", "仍在原房间 180 秒无进展等待期内，不提前退房")
                        self.pause(2.)
                        continue
                if hud_absent and self.clear_navigation_overlay(frame) and (self.room_destination(frame)
                        or any(self.score(frame, page) >= self.navigator.threshold for page in ("home", "live_menu"))):
                    if self.room_destination(frame):
                        self.entry_deadline = None
                        if (getattr(self, "join_deadline", None) is not None
                                and time.monotonic() >= self.join_deadline):
                            self.recovery_state(report, directory, "waiting_room", "入房确认 60 秒已耗尽，未见实际房内页，不重复加入")
                            self.pause(2.)
                            continue
                    if ((getattr(self, "entry_deadline", None) is not None
                         and time.monotonic() >= self.entry_deadline
                         and self.score(frame, "cooperative_room") < self.navigator.threshold)
                            or (getattr(self, "entry_request_count", 0) >= 3 and self.score(frame, "home") >= self.navigator.threshold)
                            or (getattr(self, "menu_request_count", 0) >= 3 and self.score(frame, "live_menu") >= self.navigator.threshold)):
                        self.recovery_state(report, directory, "waiting_page", error)
                        self.pause(2.)
                        continue
                    bonus = report.get("bonus", {})
                    recovery = report.get("recovery", {})
                    if bonus.get("availability") == "insufficient" or recovery.get("status") == "started":
                        try:
                            available, _ = self.read_available_bonus(frame)
                            consumption = bonus.get("consumption")
                            if type(consumption) is not int or available < consumption:
                                raise ValueError("等待用户补充体力或完成回复处理，不追加另一批饮料")
                        except ValueError as resource_error:
                            self.recovery_state(report, directory, "waiting_resource", resource_error)
                            self.pause(1.)
                            continue
                    # 只在实际安全目的页准备新入房时消费重匹配预算，失败本身不预扣。
                    if report.get("joined_room") and not report.get("rematch_budget_consumed"):
                        if getattr(self, "round_rematches", 0) >= getattr(self, "max_rematches", 3):
                            report["rematch_limit_reached"] = True
                            self.recovery_state(report, directory, "waiting_room", "当前演出的三次重匹配已耗尽")
                            self.pause(2.)
                            continue
                    # 环境仍不合格时留在同一恢复等待，避免不断创建失败报告。
                    self.device.preflight()
                    if report.get("joined_room") and not report.get("rematch_budget_consumed"):
                        self.round_rematches = getattr(self, "round_rematches", 0) + 1
                        report["rematch_budget_consumed"] = True
                    if getattr(self, "recovery_sample_frames", None):
                        self.persist_runtime_report(report, directory)
                    self.recovery_state(report, directory, "ready")
                    return
                # 结算必须有明确正向标签；标题、开场、加载、未知页和演奏页不能盲按 BACK。
                settlement = self.settlement_page(frame)
                if safe_backs < 3 and hud_absent and settlement in {"team_result", "personal_result"}:
                    self.pause(PLAY_OBSERVATION_INTERVAL)
                    confirmation = self.device.screenshot()
                    if (not blank_transition(confirmation) and self.recovery_hud_absent(confirmation)
                            and self.settlement_page(confirmation) == settlement):
                        self.check_runtime_stop()
                        safe_backs += 1
                        report.setdefault("runtime_recovery", {})["safe_backs"] = safe_backs
                        self.device.back()
                        self.pause(1.)
                        continue
            self.recovery_state(report, directory, "waiting_page", error)
            self.pause(2.)

    def clear_navigation_overlay(self, frame):
        return all(self.score(frame, page) < self.navigator.threshold
                   for page in ("cooperative_disbanded", "cooperative_disbanded_dialog"))

    def room_destination(self, frame):
        return (not blank_transition(frame) and self.recovery_hud_absent(frame)
                and self.score(frame, "cooperative_room") >= self.navigator.threshold
                and self.clear_navigation_overlay(frame))

    def recovery_hud_absent(self, frame):
        return (self.score(frame, "playing") < self.navigator.threshold
                and self.navigator.match(frame, "life_hud", LIFE_HUD_AREA)[0] < self.navigator.threshold)

    def make_report_directory(self, directory):
        while True:
            self.check_runtime_stop()
            try:
                directory.mkdir(parents=True, exist_ok=True)
                return
            except OSError:
                # 日志目录不可写时保留同一目录重试，不开始任何设备或页面操作。
                self.pause(1.)

    def observe_recovery_death(self, frame, directory, *, captured_at=None):
        captured_at = time.monotonic() if captured_at is None else captured_at
        if captured_at - self.life_guard.last_sample_at < PLAY_OBSERVATION_INTERVAL:
            return False
        self.life_guard.last_sample_at = captured_at
        if not hasattr(self, "recovery_sample_frames"):
            self.recovery_sample_frames = deque(maxlen=8)
        sample = {"captured_at": captured_at, "playfield_visible": False, "bar_observed": False, "zero_method": "not_read", "zero_template_score": None,
                  "zero_value": None, "death_class": None}
        depleted = False
        life_observed = False
        try:
            try:
                visible = self.observe_cooperative_playfield(frame, directory)
                sample["playfield_visible"] = visible
            except CooperativePlaybackInterrupted:
                monitor = self.last_report.get("playfield_monitor", {})
                depleted = monitor.get("abnormal_page") == "live_failed" and monitor.get("confirmation_frames", 0) >= 2
                sample["death_class"] = "live_failed" if depleted else None
                return depleted
            if not visible:
                return False
            def read_zero(image):
                template = getattr(self.navigator, "zero_life_template", None)
                if isinstance(template, ZeroLifeTemplate):
                    sample["zero_method"] = "template"
                    sample["zero_template_score"] = template.score(image, (1090, 16, 1187, 45))
                    return sample["zero_template_score"] >= .90
                sample["zero_method"] = "ocr"
                try:
                    sample["zero_value"] = self.read_optional_number(image, (1090, 16, 1187, 45))
                    return sample["zero_value"] == 0
                except ValueError:
                    return False
            life_observed = True
            sample["bar_observed"] = True
            depleted = self.life_guard.observe(frame, read_zero)
            sample["death_class"] = "life_zero" if depleted else None
            return depleted
        finally:
            monitor = self.last_report.get("playfield_monitor", {})
            sample.update(pause_score=monitor.get("pause_score"), life_hud_score=monitor.get("life_hud_score"),
                          filled_pixels=self.life_guard.bar_fill_pixels if life_observed else None,
                          total_pixels=self.life_guard.bar_total_pixels if life_observed else None,
                          zero_streak=self.life_guard.zero_streak, death_confirmed=depleted,
                          abnormal_page=monitor.get("abnormal_page"), confirmation_frames=monitor.get("confirmation_frames"))
            # 复用原两秒观察帧并固定内存容量；只在已经清理后保存，不追加设备采样。
            sample["blank_transition"] = bool(blank_transition(frame))
            if sample["blank_transition"]:
                # 转场没有可用页面分数；不能把上一帧 FAILED 或 HUD 证据贴到本帧。
                sample.update(pause_score=None, life_hud_score=None, abnormal_page=None, confirmation_frames=0)
            self.recovery_observation_count = getattr(self, "recovery_observation_count", 0) + 1
            self.recovery_sample_frames.append((sample, frame))
            self.last_report["recovery_observation"] = {"capacity": 8, "capture_clock": "request_start_monotonic",
                                                       "sample_interval_s": PLAY_OBSERVATION_INTERVAL,
                                                       "total_observations": self.recovery_observation_count,
                                                       "truncated": self.recovery_observation_count > 8,
                                                       "samples": [item[0] for item in self.recovery_sample_frames]}
            self.last_report["life_monitor"] = {"samples": self.life_guard.samples,
                                                "zero_confirmed": self.life_guard.zero_confirmed,
                                                "zero_streak": self.life_guard.zero_streak}

    def wait_death_release(self, report, directory):
        while report.get("playback") is not None and not release_finished(report):
            self.recovery_state(report, directory, "waiting_release")
            self.check_runtime_stop()
            self.pause(1.)

    def finish_death(self, report, directory):
        self.wait_death_release(report, directory)
        while True:
            self.check_runtime_stop()
            try:
                self.background_game(report, directory, "life_zero")
                return
            except InterruptedError:
                raise
            except Exception as error:
                self.recovery_state(report, directory, "waiting_connection", error)
                self.pause(2.)

    def save_recovery_evidence(self, report, directory):
        if report.get("playback") is not None and not release_finished(report):
            return
        frames = getattr(self, "recovery_sample_frames", ())
        for index, (sample, frame) in enumerate(frames):
            name = f"recovery-sample-{index:02d}.png"
            write_image(directory / name, frame)
            sample["path"] = name
        if frames:
            report["recovery_observation"]["samples"] = [sample for sample, _ in frames]

    def persist_runtime_report(self, report, directory):
        evidence_saved = False
        completion_candidate = report.get("completed", False)
        while True:
            try:
                if not evidence_saved:
                    self.save_evidence(report, self.current_directory)
                    evidence_saved = True
                report["completed"] = completion_candidate
                _write_json(directory / "report.json", report)
                append_cooperative_result_index(self.report_root, directory, report)
                report["persistence_confirmed"] = True
                report["task_state"] = "recorded"
                if report.get("runtime_recovery", {}).get("state") == "waiting_persistence":
                    report["runtime_recovery"]["state"] = "persisted"
                _write_json(directory / "report.json", report)
                return
            except (OSError, UnicodeError, csv.Error, ValueError) as error:
                report["completed"] = False
                report["persistence_error"] = f"{type(error).__name__}: {error}"
                if report.get("cancelled"):
                    return
                self.recovery_state(report, directory, "waiting_persistence", error)
                self.check_runtime_stop()
                self.pause(1.)

    def room_stage(self, frame):
        if blank_transition(frame) or not self.recovery_hud_absent(frame):
            return None
        if any(self.score(frame, page) >= self.navigator.threshold for page in
               ("cooperative_disbanded", "cooperative_disbanded_dialog")):
            return None
        pages = {"matching": "cooperative_matching", "select": "cooperative_select",
                 "shuffle": "cooperative_shuffle", "ready": "cooperative_ready", "cancel": "cooperative_cancel"}
        observed = [stage for stage, page in pages.items() if page in self.navigator.templates
                    and self.score(frame, page) >= (max(.90, self.navigator.threshold) if stage == "shuffle" else self.navigator.threshold)]
        return observed[0] if len(observed) == 1 else None

    def observe_room_progress(self, frame, *, enforce=True):
        stage = self.room_stage(frame)
        progress = getattr(self, "room_progress", None)
        if progress is None:
            progress = self.room_progress = {}
        ranks = {"matching": 0, "select": 1, "shuffle": 2, "ready": 3, "cancel": 4}
        if stage is not None and (not progress or ranks[stage] > ranks.get(progress.get("stage"), -1)):
            progress.update(stage=stage, observed_at=time.monotonic(), deadline=time.monotonic() + ROOM_WAIT_SECONDS)
        report = getattr(self, "current_report", None)
        if isinstance(report, dict):
            report["room_progress"] = progress
        if enforce and progress and time.monotonic() >= progress["deadline"]:
            raise RoomIdleTimeout(f"房内 {progress['stage']} 连续 180 秒无后续，清理后尝试 ESC 返回")
        return stage

    def wait_room_stage(self, target_stages, *, first_frame=None):
        frame = first_frame
        while True:
            self.check_runtime_stop()
            if frame is None:
                frame = self.screenshot()
            self.guard_room(frame)
            stage = self.observe_room_progress(frame)
            if stage in target_stages:
                return frame, stage
            if not self.recovery_hud_absent(frame):
                raise RuntimeError("房内等待已出现演奏 HUD，拒绝从歌曲中途开始")
            self.pause(.15)
            frame = None

    def return_after_room_timeout(self, attempt, directory):
        current = self.current_report or {}
        playback = current.get("playback") or {}
        if playback.get("sent_actions", 0) or (playback and not release_finished(current)):
            raise RuntimeError("已有谱面输入或当前触点释放未确认，禁止 ESC 退房")
        recovery = attempt.setdefault("room_recovery", {})
        recovery.setdefault("exit_requests", recovery.get("exit_clicks", 0))
        recovery.setdefault("exit_deadline", time.monotonic() + 20)
        recovery.update(reason="room_idle_timeout", exit_method="android_back", exit_limit=3)
        rank = {"matching": 0, "select": 1, "shuffle": 2, "ready": 3, "cancel": 4}
        original = getattr(self, "room_progress", {}).get("stage")
        while time.monotonic() < recovery["exit_deadline"]:
            self.check_runtime_stop()
            frame = self.screenshot()
            if self.room_destination(frame):
                recovery["returned_to_room"] = True
                return None
            stage = self.observe_room_progress(frame, enforce=False)
            if stage is not None and original is not None and rank[stage] > rank[original]:
                recovery["progress_arrived"] = stage
                return frame, stage
            if stage is not None and recovery["exit_requests"] < 3:
                write_image(directory / "room-idle-before-exit.png", frame)
                self.safe_log(f"房内等待超时：ESC 请求 {recovery['exit_requests'] + 1}/3")
                frame = self.screenshot()
                self.check_runtime_stop()
                if time.monotonic() >= recovery["exit_deadline"]:
                    break
                if self.room_destination(frame):
                    recovery["returned_to_room"] = True
                    return None
                stage = self.observe_room_progress(frame, enforce=False)
                if stage is not None and original is not None and rank[stage] > rank[original]:
                    recovery["progress_arrived"] = stage
                    return frame, stage
                if stage is not None:
                    recovery["exit_requests"] += 1
                    self.device.back()
                    self.pause(1.)
                    continue
            self.pause(.15)
        raise TimeoutError("房内 ESC 恢复的 20 秒／三次请求已耗尽，等待安全页面或用户处理")

    def confirm_room_join(self, attempt):
        if getattr(self, "join_deadline", None) is None:
            self.join_deadline = time.monotonic() + ENTRY_WAIT_SECONDS
        attempt.setdefault("join_deadline", self.join_deadline)
        while time.monotonic() < self.join_deadline:
            self.check_runtime_stop()
            frame = self.screenshot()
            self.guard_room(frame)
            stage = self.room_stage(frame)
            if stage is not None:
                page = "cooperative_" + stage
                attempt["join_confirmed_at"] = time.monotonic()
                attempt["join_confirmed_page"] = page
                self.join_deadline = None
                self.matched = True
                self.observe_room_progress(frame)
                if stage == "matching":
                    attempt.setdefault("member_wait_deadline", self.room_progress["deadline"])
                return frame, page
            self.pause(.15)
        raise TimeoutError("点击房间后 60 秒未确认实际入房，保留本局等待安全恢复")

    def choose(self, room, difficulty, song_mode, report, directory):
        attempt = report["attempts"][-1] if report.get("attempts") else report.setdefault("runtime_attempt", {})
        if getattr(self, "resume_selection", False) or getattr(self, "resume_stage", None):
            frame = self.screenshot()
            stage = self.observe_room_progress(frame)
            if stage is None:
                raise RuntimeError("原房间恢复页面未确认，保留沿袭，不重新入房")
            if getattr(self, "resume_stage", None) == "cancel" and stage != "cancel":
                raise RuntimeError("已准备房间的当前状态未确认，禁止重复投票或准备")
        else:
            self.matched = False
            self.wait_page("cooperative_room", ENTRY_WAIT_SECONDS, guard=False)
            if getattr(self, "join_deadline", None) is not None and time.monotonic() >= self.join_deadline:
                raise TimeoutError("同次入房确认 60 秒已耗尽，不重复加入")
            if getattr(self, "join_request_count", 0) >= 1 + getattr(self, "max_rematches", 3):
                raise TimeoutError("当前目标的入房请求预算已耗尽，等待用户处理")
            if getattr(self, "join_deadline", None) is None:
                self.join_deadline = time.monotonic() + ENTRY_WAIT_SECONDS
            attempt.setdefault("join_deadline", self.join_deadline)
            self.join_request_count = getattr(self, "join_request_count", 0) + 1
            self.room_progress = {}
            self.tap_page("cooperative_room", 920, 245 if room == "free" else 431,
                          "匹配自由房间" if room == "free" else "匹配资深房间")
            frame, _ = self.confirm_room_join(attempt)
            stage = self.room_stage(frame)
        self.matched = True
        if stage == "matching":
            frame, stage = self.wait_room_stage({"select", "shuffle", "ready", "cancel"}, first_frame=frame)
        if stage == "select":
            write_image(directory / "song-vote.png", frame)
            report["song_vote"] = {"mode": song_mode, "scope": "nomination_only"}
            if song_mode == "random":
                self.tap_page("cooperative_select", 908, 536, "提交随机选曲（おまかせ）")
            else:
                self.tap_page("cooperative_select", 1070, 543, "提交当前歌曲")
            self.resume_selection = False
            self.resume_stage = None
            frame, stage = self.wait_room_stage({"ready", "cancel"})
        elif stage == "shuffle":
            frame, stage = self.wait_room_stage({"ready", "cancel"}, first_frame=frame)
        if stage == "cancel":
            report["ready_confirmed"] = True
            self.resume_stage = "cancel"
            return None, frame
        self.resume_stage = None
        for _ in range(3):
            mode = self.ocr.read(frame, (125, 646, 239, 687))
            if is_light_mode(mode):
                report["background"] = asdict(mode)
                break
            self.tap_page("cooperative_ready", 182, 667, "切换到轻量演出")
            self.pause(.2)
            frame = self.wait_page("cooperative_ready", 8)
        else:
            raise RuntimeError("协力轻量背景未确认，拒绝使用未知背景开演")
        point = COOPERATIVE_DIFFICULTIES[difficulty][0]
        self.tap_page("cooperative_ready", *point, f"选择 {difficulty.upper()} 难度")
        last_error = ""
        previous = None
        for _ in range(2):
            frame = self.wait_page("cooperative_ready", 8)
            try:
                identity = self.matcher.match(frame, difficulty, "cooperative_prepare")
                self.record_identity_check(report, "preparation")
            except ValueError as error:
                last_error = str(error)
                self.record_identity_check(report, "preparation", error=last_error)
                previous = None
            else:
                if previous is not None and previous.song_id == identity.song_id:
                    return identity, frame
                previous = identity
            self.pause(.08)
        report["preparation_identity_error"] = last_error or "准备页歌曲未取得两帧一致确认"
        self.safe_log("准备页身份未确认，保留开场封面和标题的第二阶段判断机会")
        return None, frame

    def submit_ready(self, identity, report):
        frame = self.screenshot()
        self.guard_room(frame)
        self.observe_room_progress(frame)
        if self.score(frame, "cooperative_ready") < self.navigator.threshold:
            raise RuntimeError("准备按钮已消失，拒绝在未知页面重复提交准备")
        difficulty = identity.difficulty if identity is not None else report["requested_difficulty"]
        try:
            actual = self.matcher.match(frame, difficulty, "cooperative_prepare")
            self.record_identity_check(report, "preparation")
        except ValueError as error:
            self.record_identity_check(report, "preparation", error=str(error))
            report["ready_identity_error"] = str(error)
            if not cooperative_difficulty_selected(frame, difficulty):
                raise RuntimeError("提交准备前实际难度未确认，拒绝开演") from error
            actual = None
        if identity is not None and actual is not None and actual.song_id != identity.song_id:
            raise RuntimeError("提交准备前歌曲已改变，未派发谱面触控")
        if actual is not None:
            report["ready_identity"] = actual.to_dict()
        self.navigator.tap(1027, 591, "协力准备完成")
        return actual

    def start(self, chart, identity, report, directory, *, on_final_identity=None):
        def ready_action():
            if getattr(self, "resume_stage", None) == "cancel":
                frame = self.screenshot()
                if self.room_stage(frame) != "cancel":
                    raise RuntimeError("恢复已准备房间时状态改变，禁止重复准备或投票")
                self.observe_room_progress(frame)
                report["ready_confirmed"] = True
                self.resume_stage = None
                self.resume_selection = False
                return identity
            return self.submit_ready(identity, report)

        def loading_guard(frame):
            self.guard_room(frame)
            if not report.get("final_identity"):
                self.observe_room_progress(frame)
            if self.score(frame, "cooperative_cancel") >= self.navigator.threshold:
                report["ready_confirmed"] = True
            if self.score(frame, "cooperative_ready") >= self.navigator.threshold:
                report["ready_wait_frames"] = report.get("ready_wait_frames", 0) + 1

        def opening_deadline():
            progress = getattr(self, "room_progress", None)
            if not progress:
                self.room_progress = {"stage": "ready", "deadline": time.monotonic() + ROOM_WAIT_SECONDS}
            if time.monotonic() >= self.room_progress["deadline"]:
                raise RoomIdleTimeout("已准备／开场等待连续 180 秒无后续，清理后尝试 ESC 恢复")
            return self.room_progress["deadline"]

        return super().start(chart, identity, report, directory,
                             ready_action=ready_action, frame_guard=loading_guard,
                             identity_phase="cooperative_final", on_final_identity=on_final_identity,
                             opening_timeout=ROOM_WAIT_SECONDS, opening_deadline_provider=opening_deadline)

    def observe_play_state(self, directory):
        requested_at = time.perf_counter()
        guard = self.life_guard
        if requested_at - guard.last_sample_at < PLAY_OBSERVATION_INTERVAL:
            return
        guard.last_sample_at = requested_at
        frame = self.screenshot()
        elapsed = requested_at - self.life_epoch
        if elapsed < 8:
            self.life_frames.append((elapsed, frame))
        if not hasattr(self, "life_recent_frames"):
            self.life_recent_frames, self.life_sample_images = deque(maxlen=8), deque(maxlen=240)
        # 沿用两秒采样，在内存中保留最近画面及整曲小区域；输入清理后才压缩写盘。
        self.life_recent_frames.append((elapsed, frame))
        sample = {"elapsed_s": elapsed}
        self.life_sample_images.append((sample, frame[16:60, 970:1188].copy(), frame[380:470, 470:815].copy()))
        previous_samples = guard.samples
        try:
            sample["playfield_visible"] = self.observe_cooperative_playfield(frame, directory)
            if not sample["playfield_visible"]:
                sample["zero_streak"] = guard.zero_streak
                return

            def read_zero(image):
                template = getattr(self.navigator, "zero_life_template", None)
                if isinstance(template, ZeroLifeTemplate):
                    score = template.score(image, (1090, 16, 1187, 45))
                    self.last_report.setdefault("life_monitor", {})["zero_template_score"] = score
                    sample["zero_template_score"] = score
                    return score >= .90
                try:
                    return self.read_optional_number(image, (1090, 16, 1187, 45)) == 0
                except ValueError:
                    return False

            depleted = self.life_guard.observe(frame, read_zero)
            sample["zero_streak"] = guard.zero_streak
            if depleted:
                # 先抛出保护异常让播放器释放；PNG 压缩和文件写入留到清理后执行。
                self.life_zero_frame = frame
                self.last_report["life_zero_elapsed_s"] = elapsed
                raise LifeDepleted("协力连续两帧生命零，释放触点后返回模拟器主页；本局不计数")
        finally:
            self.observe_game_timing_feedback(frame, elapsed, sample)
            self.record_performance_trace(elapsed, previous_samples, zero_template_score=sample.get("zero_template_score"))

    def settlement_page(self, frame):
        threshold = max(.90, self.navigator.threshold)
        if ("cooperative_result_banner" in self.navigator.templates
                and "cooperative_total_score" in self.navigator.templates
                and self.score(frame, "cooperative_result_banner") >= threshold
                and self.score(frame, "cooperative_total_score") >= threshold):
            return "team_result"
        if self.score(frame, "cooperative_personal_result") >= threshold:
            return "personal_result"
        if self.score(frame, "home") >= threshold:
            return "home"
        return None

    def observe_cooperative_playfield(self, frame, directory):
        guard = self.life_guard
        if blank_transition(frame):
            guard.playfield_missing_frames = guard.zero_streak = 0
            guard.abnormal_page, guard.abnormal_frames = None, 0
            return False
        pause_score = self.score(frame, "playing")
        life_score = self.navigator.match(frame, "life_hud", LIFE_HUD_AREA)[0]
        life_visible = life_score >= self.navigator.threshold
        visible = pause_score >= self.navigator.threshold or life_visible
        page, candidates = None, []
        if visible:
            guard.playfield_missing_frames = 0
            guard.abnormal_page, guard.abnormal_frames = None, 0
            guard.hud_seen |= life_visible
            if pause_score < self.navigator.threshold:
                guard.pause_fallback_frames += 1
        else:
            guard.playfield_missing_frames += 1
            # 控件缺失只能说明本帧未知；必须另有明确页面证据，不能把特效当作结束。
            threshold = max(.90, self.navigator.threshold)
            for name in ("home", "title_screen", "cooperative_room", "cooperative_matching",
                         "cooperative_select", "cooperative_disbanded"):
                if name in self.navigator.templates and self.score(frame, name) >= threshold:
                    candidates.append(name)
            if ("cooperative_disbanded_dialog" in self.navigator.templates
                    and self.score(frame, "cooperative_disbanded_dialog") >= threshold
                    and self.score(frame, "cooperative_disbanded_dialog_ok") >= threshold):
                candidates.append("cooperative_disbanded_dialog")
            settlement = self.settlement_page(frame)
            if settlement and settlement != "home":
                candidates.append(settlement)
            if "live_failed" in self.navigator.templates:
                failed_score = self.score(frame, "live_failed")
                clear_score = self.score(frame, "live_clear")
                if failed_score >= threshold and failed_score > clear_score + .04:
                    candidates.append("live_failed")
            # 互相矛盾的页面匹配不累计；下一次仍按正常采样间隔获取新画面。
            page = candidates[0] if len(candidates) == 1 else None
            if page is None:
                guard.abnormal_page, guard.abnormal_frames = None, 0
            elif getattr(guard, "abnormal_page", None) == page:
                guard.abnormal_frames += 1
            else:
                guard.abnormal_page, guard.abnormal_frames = page, 1
                self.playback_candidate_frame = frame
        if not life_visible:
            guard.zero_streak = 0
        report = getattr(self, "last_report", None)
        if isinstance(report, dict):
            report["playfield_monitor"] = {
                "pause_score": pause_score, "life_hud_score": life_score,
                "consecutive_missing": guard.playfield_missing_frames,
                "pause_fallback_frames": guard.pause_fallback_frames,
                "abnormal_page": page, "confirmation_frames": getattr(guard, "abnormal_frames", 0),
                "candidate_pages": candidates, "sample_interval_s": PLAY_OBSERVATION_INTERVAL,
                "unknown_frames_keep_playing": True,
            }
        if getattr(guard, "abnormal_frames", 0) >= 2:
            self.playback_interrupted_frame = frame
            raise CooperativePlaybackInterrupted(f"协力演奏期间连续确认 {page} 页面；释放后进入恢复等待")
        # 暂停按钮可保护连续输入，但不能替代 LIFE 标签来验证生命零。
        return life_visible

    def background_game(self, report, directory, reason):
        return exit_to_emulator_home(self.device, report, directory, reason, self.log, stop_requested=self.stop_requested)

    def read_result(self, frame):
        if self.score(frame, "cooperative_personal_result") < self.navigator.threshold:
            return None
        report = self.current_report
        if report is None:
            raise RuntimeError("协力结算缺少本局身份，拒绝读取旧成绩")
        try:
            identity = self.matcher.match(frame, report["requested_difficulty"], "cooperative_result")
            expected = report.get("resolved_identity") or report["preparation_identity"]
            if identity.song_id != expected["song_id"]:
                raise ValueError("个人成绩页歌曲与本局歌曲不一致")
        except ValueError as error:
            report["result_identity_error"] = str(error)
            return None
        report["result_identity"] = identity.to_dict()
        boxes = {"perfect": (302, 391, 384, 434), "great": (302, 435, 384, 475),
                 "good": (302, 479, 384, 519), "bad": (302, 522, 384, 562), "miss": (302, 565, 384, 605)}
        values, errors = {}, {}
        for key, box in boxes.items():
            try:
                values[key] = self.read_judgement_number(frame, box)
            except ValueError as error:
                values[key] = None
                errors[key] = str(error)
        report["partial_judgements"] = {**values, "errors": errors, "complete": not errors}
        if errors:
            return None
        for key, box in {"late": (448, 514, 554, 542), "fast": (556, 514, 638, 542),
                         "flick": (609, 557, 643, 590), "combo": (536, 390, 668, 435),
                         "score": (282, 205, 670, 274)}.items():
            try:
                values[key] = self.read_optional_number(frame, box)
            except ValueError:
                values[key] = None
        return LiveResult(**values)

    def collect(self, report, directory):
        deadline = time.monotonic() + 180
        cleared, failed, previous, result = False, False, None, None
        result_wait_started = None
        result_skipped = False
        end_guard = LiveEndGuard(self.navigator, report,
                                 life_seen=getattr(getattr(self, "life_guard", None), "hud_seen", False),
                                 require_settlement=True)
        index = 0
        self.current_report = report

        def inspect(frame):
            nonlocal cleared, failed, previous, result, result_wait_started
            clear_score = self.navigator.match(frame, "live_clear")[0]
            failed_score = self.navigator.match(frame, "live_failed")[0] if "live_failed" in self.navigator.templates else 0
            personal = self.score(frame, "cooperative_personal_result") >= self.navigator.threshold
            page = self.settlement_page(frame)
            if page is None and clear_score >= self.navigator.threshold and clear_score > failed_score + .04:
                page = "live_clear"
            if page is None and failed_score >= self.navigator.threshold and failed_score > clear_score + .04:
                page = "live_failed"
            end_guard.observe(frame, settlement_page=page)
            if end_guard.ended and end_guard.confirmed_page == "live_clear":
                cleared = True
                report["live_status"] = "cleared"
                write_image(directory / "live-clear.png", frame)
            if end_guard.ended and end_guard.confirmed_page == "live_failed":
                failed = True
                report["live_status"] = "failed"
                raise LifeDepleted("连续两帧确认游戏 LIVE FAILED，释放后切出游戏")
            if personal:
                result_wait_started = result_wait_started or time.monotonic()
                candidate = self.read_result(frame)
                write_image(directory / "personal-result-last.png", frame)
                if candidate is not None:
                    values = candidate.to_dict()
                    report.setdefault("result_readings", [])
                    if len(report["result_readings"]) < 12:
                        report["result_readings"].append(values)
                    if previous is not None and all(getattr(previous, key) == getattr(candidate, key) for key in JUDGEMENTS):
                        # 可选数字只在两帧一致时保存；动画数字或缺失数据不会被补成零。
                        for key in ("late", "fast", "flick", "combo", "score"):
                            if getattr(previous, key) != getattr(candidate, key):
                                values[key] = None
                        expected = report["chart"]["total_note_count"]
                        values["expected_total"] = expected
                        values["total_matches_chart"] = type(expected) is int and expected > 0 and values["total"] == expected
                        result = report["judgements"] = values
                        report["result_status"] = "recorded" if values["total_matches_chart"] else "note_count_mismatch"
                        write_image(directory / "result.png", frame)
                        _write_json(directory / "report.json", report)
                    previous = candidate
                else:
                    previous = None
                    # 已取得本局两帧一致成绩后，后续动画中的漏读不能抹掉已保存的成绩。
            else:
                previous = None
            return personal

        while time.monotonic() < deadline:
            frame = self.screenshot()
            if blank_transition(frame):
                end_guard.observe(frame)
                self.pause(.3)
                continue
            personal = inspect(frame)
            if self.score(frame, "home") >= self.navigator.threshold:
                if failed:
                    raise LifeDepleted("游戏判定协力演出失败，释放后切出游戏")
                if not end_guard.ended:
                    self.pause(.2)
                    continue
                report["returned_home"] = True
                report["performance_completed"] = True
                report["completed"] = result is not None and result.get("total_matches_chart") is True
                if result is None:
                    report["result_status"] = "incomplete"
                    report["settlement_warning"] = "个人判定未完整读取，保存部分数据并继续下一曲"
                elif not result["total_matches_chart"]:
                    report["settlement_warning"] = "个人音数未匹配本地谱面，保留实际数字并继续下一曲"
                return
            if (self.score(frame, "playing") >= self.navigator.threshold
                    and self.navigator.match(frame, "live_clear")[0] < self.navigator.threshold
                    and ("live_failed" not in self.navigator.templates
                         or self.navigator.match(frame, "live_failed")[0] < self.navigator.threshold)):
                # 全部输入结束后仍等实际页面切换，演奏画面内不发送 BACK。
                self.pause(.3)
                continue
            if not end_guard.ended:
                self.pause(.2)
                continue
            if personal and result is None and not result_skipped:
                _write_json(directory / "report.json", report)
                if time.monotonic() - result_wait_started < 8:
                    self.pause(.2)
                    continue
                report["result_status"] = "incomplete"
                result_skipped = True
                self.safe_log("协力个人判定暂未完整读取：保留证据并继续结算")
            if index < 40:
                write_image(directory / f"settlement-{index:02d}.png", frame)
                index += 1
            # 与单人结算保持一致：加速后重新取帧，成绩先持久化，再用 BACK 推进到实际主页。
            self.device.tap(1279, 719)
            self.pause(1.0)
            confirmation = self.screenshot()
            if blank_transition(confirmation):
                end_guard.observe(confirmation)
                self.pause(.3)
                continue
            personal = inspect(confirmation)
            if self.score(confirmation, "home") >= self.navigator.threshold:
                continue
            if personal and result is None and not result_skipped:
                self.pause(.2)
                continue
            if self.score(confirmation, "playing") >= self.navigator.threshold:
                continue
            if np.mean(confirmation.max(axis=2) < 15) > .95:
                self.pause(.4)
                continue
            self.navigator._check_stop()
            self.device.back()
            self.pause(.8)
        raise TimeoutError("协力结算或返回主页超时")

    def save_evidence(self, report, directory):
        self.save_preplay_life_evidence(report, directory)
        self.save_recovery_evidence(report, directory)
        self.finish_performance_trace(report, directory)
        for attribute, name in (("loading_frame", "loading.png"), ("final_frame", "final-cover.png"),
                                ("identity_problem_frame", "identity-unconfirmed.png"),
                                ("playback_candidate_frame", "playfield-candidate.png"),
                                ("playback_interrupted_frame", "playback-interrupted.png"),
                                ("life_zero_frame", "life-zero.png")):
            frame = getattr(self, attribute, None)
            if frame is not None:
                write_image(directory / name, frame)
                delattr(self, attribute)
        for index, (when, frame) in enumerate(getattr(self, "anchor_frames", [])):
            name = f"anchor-frame-{index:02d}.png"
            write_image(directory / name, frame)
            report.setdefault("anchor_frames", []).append({"captured_at": when, "path": name})
        self.anchor_frames = []
        for index, (when, frame) in enumerate(self.life_frames):
            name = f"life-frame-{index:02d}.png"
            write_image(directory / name, frame)
            report.setdefault("life_frames", []).append({"elapsed_s": when, "path": name})
        self.life_frames = []
        for index, (when, frame) in enumerate(getattr(self, "life_recent_frames", [])):
            name = f"life-recent-{index:02d}.png"
            write_image(directory / name, frame)
            report.setdefault("life_recent_frames", []).append({"elapsed_s": when, "path": name})
        for index, (sample, hud, judgement) in enumerate(getattr(self, "life_sample_images", [])):
            names = {"hud_path": f"life-sample-{index:03d}-hud.png",
                     "judgement_path": f"life-sample-{index:03d}-judgement.png"}
            write_image(directory / names["hud_path"], hud)
            write_image(directory / names["judgement_path"], judgement)
            report.setdefault("life_samples", []).append({**sample, **names})
        self.life_recent_frames, self.life_sample_images = deque(maxlen=8), deque(maxlen=240)

    @staticmethod
    def verify_playback(report):
        if not input_finished(report):
            raise RuntimeError("协力输入派发、设备执行或本轮释放不完整，不能计入完成")

    def run(self, count, difficulty, song_mode, offset_ms=0, *, room="free", bonus_consumption="current",
            recovery_mode="off", recovery_count=1, engine="legacy", max_rematches=3, game_timing_feedback=False):
        if (type(count) is not int or not 1 <= count <= 999 or difficulty not in COOPERATIVE_DIFFICULTIES
                or song_mode not in {"current", "random"} or room not in {"free", "veteran"}
                or type(offset_ms) is not int or not -300 <= offset_ms <= 300
                or engine not in {"legacy", "native"} or type(max_rematches) is not int or not 0 <= max_rematches <= 3
                or type(game_timing_feedback) is not bool):
            raise ValueError("协力任务选项无效")
        if bonus_consumption != "current" and (type(bonus_consumption) is not int or not 0 <= bonus_consumption <= 10):
            raise ValueError("体力消耗只能沿用游戏设置或指定 0 到 10")
        if recovery_mode not in {"off", "small", "large"} or type(recovery_count) is not int or not 1 <= recovery_count <= 99:
            raise ValueError("协力回复选项无效")
        self.completed_rounds = 0
        self.performed_rounds = 0
        self.round_rematches = 0
        self.entry_request_count = self.menu_request_count = 0
        self.entry_deadline = None
        self.join_deadline = None
        self.resume_selection = False
        self.resume_stage = None
        self.room_progress = {}
        self.join_request_count = 0
        self.max_rematches = max_rematches
        bonus_snapshot = {}
        reports = []
        self.safe_log(f"协力谱面演出：已完成 0 / 总数 {count}；{'自由' if room == 'free' else '资深'}公房；{difficulty.upper()}")
        while self.performed_rounds < count:
            self.check_runtime_stop()
            round_index = self.performed_rounds + 1
            directory = self.report_root / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:8])
            self.make_report_directory(directory)
            self.current_directory = directory
            report = {"schema_version": 1, "live_mode": "cooperative", "started_at": datetime.now().astimezone().isoformat(),
                      "round": round_index, "total_rounds": count, "requested_difficulty": difficulty,
                      "song_mode": song_mode, "room": room, "engine": engine, "timing_offset_ms": offset_ms,
                      "requested_bonus_consumption": bonus_consumption, "completed": False,
                      "recovery_settings": {"mode": recovery_mode, "count": recovery_count}, "attempts": [],
                      "life_zero_policy": "release_then_android_home_and_stop"}
            if bonus_snapshot:
                # 新报告可能先在连接或入口失败，仍须说明本任务已确认的消耗来源。
                report["bonus"] = dict(bonus_snapshot, confirmation_source="task_snapshot")
            self.current_report = self.last_report = report
            self.recovery_sample_frames = deque(maxlen=8)
            self.recovery_observation_count = 0
            self.life_guard, self.life_frames, self.anchor_frames = LifeGuard(bar_area=COOPERATIVE_LIFE_BAR), [], []
            self.life_recent_frames, self.life_sample_images = deque(maxlen=8), deque(maxlen=240)
            report["report_path"] = str(directory / "report.json")
            native_player = None
            runtime_error = None
            try:
                self.device.preflight()
                self.open_rooms()
                if bonus_snapshot or not (self.resume_selection or self.resume_stage):
                    self.prepare_task_bonus(bonus_consumption, report, directory, bonus_snapshot,
                                            return_page="cooperative_room")
                for attempt in range(max_rematches - self.round_rematches + 1):
                    native_player = None
                    self.life_guard, self.life_frames = LifeGuard(bar_area=COOPERATIVE_LIFE_BAR), []
                    self.life_recent_frames, self.life_sample_images = deque(maxlen=8), deque(maxlen=240)
                    self.anchor_frames = []
                    attempt_dir = directory / f"attempt-{attempt + 1}"
                    attempt_dir.mkdir()
                    self.current_directory = attempt_dir
                    self.begin_performance_trace()
                    self.prepare_game_timing_feedback(report, game_timing_feedback, engine)
                    attempt_report = {"attempt": attempt + 1, "status": "preparing"}
                    attempt_report["game_timing_feedback"] = report["game_timing_feedback"]
                    report["attempts"].append(attempt_report)
                    try:
                        if not (self.resume_selection or self.resume_stage):
                            self.ensure_bonus_available(recovery_mode, recovery_count, report, directory)
                        identity, frame = self.choose(room, difficulty, song_mode, report, attempt_dir)
                        report["joined_room"] = bool(getattr(self, "matched", False))
                        if identity is not None:
                            report["preparation_identity"] = identity.to_dict()
                        write_image(attempt_dir / "prepare.png", frame)
                        chart, events = None, None

                        def prepare_chart(actual):
                            nonlocal chart, events, native_player
                            if chart is not None:
                                return chart
                            try:
                                entry = self.repository.songs[actual.song_id]["charts"][difficulty]
                                report["chart"] = {key: entry[key] for key in ("music_id", "difficulty", "sha256", "path", "total_note_count")}
                                chart = parse_sus(self.repository.load_chart(actual.song_id, difficulty))
                                events = compile_touches(chart)
                                self.set_performance_trace_plan(events)
                            except (KeyError, FileNotFoundError, ValueError) as error:
                                report["chart_error"] = f"{type(error).__name__}: {error}"
                                raise MissingCooperativeChart(f"实际抽选歌曲的本地谱面缺失或校验失败：{error}") from error
                            report["chart"].update(duration=chart.duration, planned_actions=len(events))
                            if engine == "native":
                                from .native_player import NativePlayer
                                feedback_options = ({"game_timing_correction": lambda guard=self.game_timing_guard: guard.correction_ms}
                                                    if self.game_timing_guard is not None else {})
                                native_player = NativePlayer(self.device.controller, events, attempt_dir, self.stop_requested,
                                                             idle_observer=lambda: self.observe_play_state(attempt_dir),
                                                             observation_interval=PLAY_OBSERVATION_INTERVAL,
                                                             observation_budget=.30,
                                                             **feedback_options)
                                report["playback"] = native_player.report
                                native_player.prepare()
                            return chart

                        def prepare_final(actual):
                            report["resolved_identity"] = actual.to_dict()
                            return prepare_chart(actual)

                        # 准备页已确认时预热 Native；否则只提交准备，等开场确认身份后才加载本地谱面。
                        if identity is not None:
                            prepare_chart(identity)
                        _write_json(directory / "report.json", report)
                        if identity is not None:
                            self.safe_log(f"抽选结果：{identity.song_id} {identity.title} {difficulty.upper()}；准备后等待实际开场")
                        epoch = self.start(chart, identity, report, attempt_dir, on_final_identity=prepare_final)
                        report["ready_confirmed"] = True
                        self.life_epoch = epoch
                        report["phase"] = "playing"
                        playback_error = None
                        try:
                            if native_player is not None:
                                native_player.play(epoch, offset_ms)
                            else:
                                self.play(events, epoch, offset_ms, report, attempt_dir)
                        except Exception as error:
                            playback_error = error
                            report["playback_error"] = f"{type(error).__name__}: {error}"
                            monitor = report.get("playfield_monitor", {})
                            if (isinstance(error, LifeDepleted) or self.life_guard.zero_confirmed
                                    or (monitor.get("abnormal_page") == "live_failed" and monitor.get("confirmation_frames", 0) >= 2)):
                                report["death_confirmed"] = True
                        finally:
                            if native_player is not None:
                                self.close_pending_player(native_player, report, directory,
                                                          cancelled=isinstance(playback_error, InterruptedError) or self.stop_requested())
                                native_player = None
                        report.setdefault("life_monitor", {}).update(
                            samples=self.life_guard.samples, zero_confirmed=self.life_guard.zero_confirmed,
                            zero_detector="full_value_template" if isinstance(
                                getattr(self.navigator,"zero_life_template",None),ZeroLifeTemplate) else "numeric_ocr")
                        if self.stop_requested():
                            raise InterruptedError("用户已停止协力，触点已释放")
                        if not release_finished(report):
                            raise RuntimeError("协力触点释放未确认，禁止继续操作页面")
                        if playback_error is not None:
                            raise playback_error
                        # 输入异常不能伪装成结算成功；保留原失败并清理，外层等待安全页面或真实死亡。
                        self.verify_playback(report)
                        report["phase"] = "settlement"
                        self.collect(report, directory)
                        report["performance_completed"] = report.get("returned_home") is True and input_finished(report)
                        attempt_report["status"] = report.get("live_status", "ended")
                        break
                    except RoomDisbanded as error:
                        matching_timeout = isinstance(error, RoomMatchingTimeout)
                        attempt_report.update(status="matching_timeout" if matching_timeout else "disbanded", error=str(error))
                        if native_player is not None:
                            closing_player = native_player
                            self.close_pending_player(closing_player, report, directory)
                            native_player = None
                            attempt_report["playback"] = dict(closing_player.report)
                        if (report.get("playback") or {}).get("sent_actions", 0):
                            raise RuntimeError("已有谱面输入后房间异常，禁止重匹配并重放") from error
                        self.save_evidence(report, attempt_dir)
                        _write_json(directory / "report.json", report)
                        self.return_after_disbanded(attempt_report, attempt_dir)
                        self.matched = False
                        if self.round_rematches >= max_rematches:
                            report["rematch_limit_reached"] = True
                            raise RuntimeError("房间恢复重匹配次数已达上限") from error
                        self.round_rematches += 1
                        self.safe_log(f"{'成员匹配超时' if matching_timeout else '房间解散'}：已返回房间选择页，重匹配 {attempt + 1}/{max_rematches}")
                        for key in ("preparation_identity", "preparation_identity_error", "ready_identity", "ready_identity_error",
                                    "ready_confirmed", "final_identity", "resolved_identity", "identity_checks", "chart", "playback"):
                            if key in report:
                                attempt_report[key] = report.pop(key)
                    except Exception as error:
                        if isinstance(error, RoomIdleTimeout):
                            attempt_report["status"] = "room_timeout"
                        report.setdefault("failure_reason", f"{type(error).__name__}: {error}")
                        if isinstance(error, LifeDepleted):
                            report["death_confirmed"] = True
                        raise
                    finally:
                        if native_player is not None:
                            self.close_pending_player(native_player, report, directory,
                                                      cancelled=self.stop_requested() or report.get("failure_reason", "").startswith("InterruptedError:"))
                            native_player = None
                report["completed"] = bool(report.get("returned_home")
                                           and report.get("live_status") in {"cleared", "ended"}
                                           and report.get("judgements", {}).get("total_matches_chart") is True)
            except Exception as error:
                report["completed"] = False
                report["error"] = f"{type(error).__name__}: {error}"
                report["cancelled"] = self.stop_requested() or isinstance(error, InterruptedError)
                report["death_confirmed"] = (report.get("death_confirmed", False) or isinstance(error, LifeDepleted)
                                             or self.life_guard.zero_confirmed)
                if not report["cancelled"]:
                    try:
                        # 先保存实际失败页；HOME 后另存桌面，避免把退出后的截图当作识别失败证据。
                        write_image(directory / "failure.png", self.device.screenshot())
                    except Exception:
                        pass
                report["joined_room"] = report.get("joined_room", False) or bool(getattr(self, "matched", False))
                if report["death_confirmed"] and not report["cancelled"]:
                    self.finish_death(report, directory)
                    runtime_error = LifeDepleted(report.get("failure_reason", str(error)))
                elif report["cancelled"]:
                    runtime_error = error
                else:
                    runtime_error = error
                    report["skipped_after_input"] = (report.get("playback") or {}).get("sent_actions", 0) > 0
            finally:
                report["finished_at"] = datetime.now().astimezone().isoformat()
                self.persist_runtime_report(report, directory)
            # JSON、CSV 与释放证据全部落盘后才更新进度；写入失败不能伪装成完成。
            if report["completed"]:
                self.completed_rounds += 1
            reports.append(report)
            if report.get("cancelled") or report.get("death_confirmed"):
                raise runtime_error or InterruptedError("用户已停止协力")
            if runtime_error is not None:
                self.recover_runtime_failure(report, directory, runtime_error)
                continue
            if report.get("performance_completed"):
                self.performed_rounds += 1
                self.round_rematches = 0
                self.entry_request_count = self.menu_request_count = 0
                self.entry_deadline = None
                self.join_deadline = None
                self.join_request_count = 0
                self.room_progress = {}
            self.safe_log(f"协力谱面演出：已完成 {self.completed_rounds} / 总数 {count}；报告 {directory.name}")
            if report.get("settlement_warning"):
                self.safe_log(f"{report['settlement_warning']}；已演出 {round_index}/{count}，完整记录 {self.completed_rounds}")
            if report.get("judgements"):
                values = report["judgements"]
                self.safe_log(f"PERFECT {values['perfect']}，GREAT {values['great']}，GOOD {values['good']}，BAD {values['bad']}，MISS {values['miss']}；PERFECT 占比 {values['perfect_rate']:.2%}")
        return reports
