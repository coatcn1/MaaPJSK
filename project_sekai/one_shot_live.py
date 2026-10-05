from datetime import datetime
import csv
from pathlib import Path
import time
from uuid import uuid4

from .chart_catalog import _write_json
from .chart_player import compile_touches
from .life_monitor import LifeDepleted, LifeGuard
from .live_end import LiveEndGuard, blank_transition
from .live_exit import background_game
from .solo_live import DIFFICULTY_POINTS, SoloLive
from .song_identity import write_image
from .sus_chart import parse_sus


OPENING_WAIT_SECONDS = 300


class MissingOneShotChart(ValueError):
    """歌曲身份已确认，但任务难度的本地谱面不可用。"""


def append_one_shot_result_index(root, directory, report):
    identity = report.get("final_identity", {})
    playback = report.get("playback", {})
    row = {"run": directory.name, "started_at": report["started_at"], "song_id": identity.get("song_id"),
           "title": identity.get("title"), "difficulty": report["requested_difficulty"],
           "completed": report["completed"], "live_status": report.get("live_status", "unknown"),
           "result_status": report["result_status"], "error": report.get("error"),
           **{key: playback.get(key) for key in ("planned_actions", "sent_actions", "executed_actions", "release_confirmed")}}
    path = root / "results.csv"
    existing = path.is_file() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8" if existing else "utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(row))
        if not existing:
            writer.writeheader()
        writer.writerow(row)


class OneShotLive(SoloLive):
    def screenshot(self):
        # 等待入口不导航，也不因其他游戏页面而提前结束；只检查取消与截图连接。
        self.navigator._check_stop()
        return self.device.screenshot()

    @staticmethod
    def verify_playback(report):
        playback = report.get("playback", {})
        planned = playback.get("planned_actions", 0)
        if planned <= 0 or planned != playback.get("sent_actions") or not playback.get("release_confirmed"):
            raise RuntimeError("一键演出的输入或触点释放不完整，未确认完成")
        if report["engine"] == "native":
            release = playback.get("release", {})
            if (planned != playback.get("executed_actions") or playback.get("queue_underflows") != 0
                    or release.get("reset_executed") is not True
                    or release.get("release_proof") != "current-reset-jlog-and-cleanup"):
                raise RuntimeError("一键演出未取得完整 Native 执行、本轮 reset 与清理证据")
        report["input_completed"] = True

    def wait_for_finish(self, report, directory):
        deadline = time.monotonic() + 120
        end_guard = LiveEndGuard(self.navigator, report, life_seen=getattr(getattr(self, "life_guard", None), "hud_seen", False))

        def read_zero(frame):
            try:
                return self.read_optional_number(frame, (1090, 8, 1187, 37)) == 0
            except ValueError:
                return False

        while time.monotonic() < deadline:
            frame = self.screenshot()
            if blank_transition(frame):
                end_guard.observe(frame)
                self.pause(.2)
                continue
            clear = self.navigator.match(frame, "live_clear")[0]
            failed = self.navigator.match(frame, "live_failed")[0]
            if failed >= self.navigator.threshold and failed > clear + .04:
                report["live_status"] = "failed"
                write_image(directory / "live-failed.png", frame)
                raise LifeDepleted("游戏已确认 LIVE FAILED，一键演出结束")
            if clear >= self.navigator.threshold and clear > failed + .04:
                report["live_status"] = "cleared"
                write_image(directory / "live-clear.png", frame)
                return
            if end_guard.observe(frame):
                write_image(directory / "life-hud-disappeared.png", frame)
                return
            if (self.navigator.match(frame, "playing", (1190, 10, 1256, 80))[0] >= self.navigator.threshold
                    and self.life_guard.observe(frame, read_zero)):
                report["live_status"] = "life_depleted"
                write_image(directory / "life-zero.png", frame)
                raise LifeDepleted("连续两帧确认生命零，一键演出结束")
            self.pause(.1)
        write_image(directory / "finish-unconfirmed.png", frame)
        raise TimeoutError("一键演出输入已结束，但生命栏消失或演出结束仍未确认")

    def save_evidence(self, report, directory):
        self.finish_performance_trace(report, directory)
        for attribute, name in (("loading_frame", "loading.png"), ("final_frame", "final-cover.png"),
                                ("identity_problem_frame", "identity-unconfirmed.png")):
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

    def run(self, difficulty, offset_ms=0, *, engine="legacy"):
        if (difficulty not in {*DIFFICULTY_POINTS, "append"} or engine not in {"legacy", "native"}
                or type(offset_ms) is not int or not -300 <= offset_ms <= 300):
            raise ValueError("一键演出选项无效")
        directory = self.report_root / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:8])
        directory.mkdir(parents=True)
        report = {"schema_version": 1, "mode": "one_shot", "started_at": datetime.now().astimezone().isoformat(),
                  "requested_difficulty": difficulty, "difficulty_source": "task_setting", "engine": engine,
                  "timing_offset_ms": offset_ms, "opening_timeout_seconds": OPENING_WAIT_SECONDS,
                  "completed": False, "phase": "waiting_opening", "result_status": "not_collected",
                  "report_path": str(directory / "report.json")}
        self.last_report = report
        self.begin_performance_trace()
        self.life_guard, self.life_frames, self.anchor_frames = LifeGuard(), [], []
        events, native_player = None, None

        def prepare_chart(identity):
            nonlocal events, native_player
            try:
                entry = self.repository.songs[identity.song_id]["charts"][difficulty]
                chart = parse_sus(self.repository.load_chart(identity.song_id, difficulty))
                events = compile_touches(chart)
                self.set_performance_trace_plan(events)
            except (KeyError, FileNotFoundError, ValueError) as error:
                raise MissingOneShotChart(f"本地歌曲 {identity.song_id} 的 {difficulty.upper()} 谱面缺失或校验失败：{error}") from error
            report["chart"] = {key: entry[key] for key in ("music_id", "difficulty", "sha256", "path", "total_note_count")}
            report["chart"].update(duration=chart.duration, planned_actions=len(events))
            report["phase"] = "anchor"
            if engine == "native":
                from .native_player import NativePlayer
                native_player = NativePlayer(self.device.controller, events, directory, self.stop_requested,
                                             idle_observer=lambda: self.observe_play_state(directory))
                report["playback"] = native_player.report
                native_player.prepare()
            return chart

        def close_player():
            nonlocal native_player
            if native_player is not None:
                player, native_player = native_player, None
                try:
                    player.close()
                except Exception as error:
                    report["release_error"] = f"{type(error).__name__}: {error}"
                    raise

        _write_json(directory / "report.json", report)
        try:
            try:
                self.log(f"一键演出：最长等待 5 分钟最终封面；{difficulty.upper()}，{engine}，偏移 {offset_ms} ms")
                epoch = self.start(None, None, report, directory, ready_action=lambda: None,
                                   identity_phase="one_shot_final", on_final_identity=prepare_chart,
                                   opening_timeout=OPENING_WAIT_SECONDS, wait_for_opening=True)
                self.life_epoch = epoch
                report["phase"] = "playing"
                _write_json(directory / "report.json", report)
                if native_player is not None:
                    native_player.play(epoch, offset_ms)
                else:
                    self.play(events, epoch, offset_ms, report, directory)
                close_player()
                self.verify_playback(report)
                report["phase"] = "finishing"
                self.wait_for_finish(report, directory)
                # 一键任务以实际演奏结束为终点；不推进结算或虚构尚未读取的判定成绩。
                report["completed"] = True
                report["phase"] = "finished"
            finally:
                close_player()
        except Exception as error:
            report["completed"] = False
            report["error"] = f"{type(error).__name__}: {error}"
            report["cancelled"] = self.stop_requested() or isinstance(error, InterruptedError)
            if not report["cancelled"]:
                try:
                    write_image(directory / "failure.png", self.device.screenshot())
                except Exception:
                    pass
                if report.get("final_identity"):
                    try:
                        reason = ("life_zero" if isinstance(error, LifeDepleted) else "missing_chart"
                                  if isinstance(error, MissingOneShotChart) else "performance_failed")
                        background_game(self.device, report, directory, reason, self.log, task_label="一键演出",
                                        stop_requested=self.stop_requested)
                    except InterruptedError as cancellation:
                        # 故障退出期间的新停止优先；原失败仍保留供诊断，MFA 应显示取消。
                        report["cancelled"] = True
                        report["background_error"] = f"{type(cancellation).__name__}: {cancellation}"
                        raise
                    except Exception as background_error:
                        report["background_error"] = f"{type(background_error).__name__}: {background_error}"
            raise
        finally:
            report["life_monitor"] = {"samples": self.life_guard.samples, "zero_confirmed": self.life_guard.zero_confirmed}
            report["finished_at"] = datetime.now().astimezone().isoformat()
            try:
                self.save_evidence(report, directory)
                _write_json(directory / "report.json", report)
                append_one_shot_result_index(self.report_root, directory, report)
            except Exception as error:
                report["completed"] = False
                report["persistence_error"] = f"{type(error).__name__}: {error}"
                _write_json(directory / "report.json", report)
                raise
        return report
