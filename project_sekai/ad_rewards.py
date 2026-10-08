from __future__ import annotations

from datetime import datetime
import json
import hashlib
from pathlib import Path
import re
import time
from uuid import uuid4

import cv2
import numpy as np

from .chart_catalog import _write_json
from .ocr import LineOcr
from .song_identity import read_image, write_image


REWARD_POINT = (249, 328)
WATCH_POINT = (760, 542)
MAX_AD_STARTS = 7
BACK_RECOVERY_SECONDS = 20.


def loading_spinner(frame):
    crop = cv2.cvtColor(frame[618:710, 1185:1269], cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(crop, (0, 0, 235), (179, 50, 255))
    circles = 0
    for contour in cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]:
        x, y, width, height = cv2.boundingRect(contour)
        radius = np.hypot(x + width / 2 - 39, y + height / 2 - 44)
        if 3 <= width <= 12 and 3 <= height <= 12 and 16 <= radius <= 36:
            circles += 1
    return circles >= 4


class AdRewardPages:
    def __init__(self, config_path: Path, ocr_root: Path, *, navigator=None):
        self.config_path = Path(config_path).resolve()
        config = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
        if config.get("screen") != [1280, 720] or not .8 <= config.get("threshold", 0) < 1:
            raise ValueError("广告模板环境或门槛无效，请重新采样本机页面")
        self.threshold = config["threshold"]
        self.anchors = {}
        for name, spec in config["anchors"].items():
            path = self.config_path.parent / spec["path"]
            if hashlib.sha256(path.read_bytes()).hexdigest() != spec.get('sha256'):
                raise ValueError(f"广告模板哈希不符，请重新生成：{name}")
            self.anchors[name] = (read_image(path), tuple(spec["box"]))
        required = {"ad_home", "ad_map", "ad_street", "ad_cm", "ad_rewards", "ad_watch_question", "ad_watch_start"}
        required |= {"ad_map_return", "ad_world_map"}
        if not required <= self.anchors.keys():
            raise ValueError("广告模板不完整，请先生成本机广告任务模板")
        self.ocr = LineOcr(ocr_root)
        self.navigator = navigator

    def matches(self, frame, name):
        template, (x1, y1, x2, y2) = self.anchors[name]
        crop = frame[y1:y2, x1:x2]
        if crop.shape != template.shape:
            return False
        score = cv2.matchTemplate(crop, template, cv2.TM_CCOEFF_NORMED)[0, 0]
        return bool(np.isfinite(score) and score >= self.threshold)

    def text_is(self, frame, box, expected):
        reading = self.ocr.read(frame, box)
        return reading.confidence >= .8 and re.sub(r"\s+", "", reading.text) == expected

    def unique_target(self, frame, name, viewport):
        template, _ = self.anchors[name]
        x1, y1, x2, y2 = viewport
        result = cv2.matchTemplate(frame[y1:y2, x1:x2], template, cv2.TM_CCOEFF_NORMED)
        _, score, _, (x, y) = cv2.minMaxLoc(result)
        if score < self.threshold:
            return None
        height, width = template.shape[:2]
        # 地图与街道视口都会移动；仅接受有限区域内唯一完整标记，不沿用截图坐标。
        result[max(0, y - height):y + height, max(0, x - width):x + width] = -1
        if cv2.minMaxLoc(result)[1] >= self.threshold:
            return None
        box = (x1 + x - 2, y1 + y - 3, x1 + x + width + 2, y1 + y + height + 3)
        return [x1 + x + width // 2, y1 + y + height // 2], box

    def cm_target(self, frame):
        template, _ = self.anchors['ad_cm']
        matches = []
        height, width = template.shape[:2]
        for scale in (1., .9):
            # 只接受已验证的两个手机核心尺寸，其余页面、唯一性和 0.9 门槛均不改变。
            sized = template if scale == 1. else cv2.resize(
                template, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_LINEAR)
            sh, sw = sized.shape[:2]
            result = cv2.matchTemplate(frame[100:290], sized, cv2.TM_CCOEFF_NORMED)
            while True:
                _, score, _, (x, y) = cv2.minMaxLoc(result)
                if not np.isfinite(score) or score < self.threshold:
                    break
                matches.append((float(score), [x + sw // 2, 100 + y + sh // 2],
                                (x - 2, 100 + y - 3, x + sw + 2, 100 + y + sh + 3)))
                result[max(0, y - sh):y + sh, max(0, x - sw):x + sw] = -1
        if not matches:
            return None
        matches.sort(reverse=True, key=lambda item: item[0])
        best = matches[0]
        # 同一手机可在两个尺度命中；只合并近邻同位置，其他明确对象仍拒绝。
        if any(max(abs(center[index] - best[1][index]) for index in range(2)) > 8
               for _, center, _ in matches[1:]):
            return None
        return best[1], best[2]

    def observe(self, frame):
        if frame.shape != (720, 1280, 3):
            raise RuntimeError("广告任务截图尺寸不符合本机基准")
        if loading_spinner(frame):
            return {'state': 'unknown', 'reason': '加载转圈尚未结束'}
        if self.matches(frame, "ad_watch_question") and self.matches(frame, "ad_watch_start"):
            return {"state": "watch_confirm"}
        # 遮罩与加载帧仍保留背景文字；绝对亮度门槛阻止归一化模板把它们误判为返回。
        if self.matches(frame, "ad_rewards") and np.median(frame[145:157, 141:170]) >= 235:
            return {"state": "rewards"}
        navigator = getattr(self, 'navigator', None)
        hud_absent = (navigator is not None and not any(
            name in navigator.templates and navigator.match(frame, name)[0] >= navigator.threshold
            for name in ('life_hud', 'playing')))
        if hud_absent and self.matches(frame, "ad_map_return"):
            world = self.unique_target(frame, 'ad_world_map', (1050, 535, 1280, 720))
            if world is not None and self.text_is(frame, world[1], "現実世界へ"):
                return {"state": "world_map", "target": [world[0][0], world[0][1] - 35]}
            target = self.unique_target(frame, "ad_map", (50, 90, 1200, 575))
            if target is not None and self.text_is(frame, target[1], "スクランブル交差点"):
                return {"state": "map", "target": target[0]}
        home_ready = (navigator is not None and navigator.match(frame, 'home')[0] >= navigator.threshold
                      and not any(name in navigator.templates and navigator.match(frame, name)[0] >= navigator.threshold
                                  for name in ('life_hud', 'playing')))
        if home_ready and self.matches(frame, "ad_home"):
            target = self.cm_target(frame)
            if target is not None:
                center = target[0]
                if self.text_is(frame, (center[0] - 27, center[1] + 17, center[0] + 30, center[1] + 42), "CM"):
                    return {"state": "street_cm", "target": center}
            if self.matches(frame, "ad_street"):
                return {"state": "street"}
            return {"state": "home"}
        return {"state": "unknown"}


class AdRewards:
    def __init__(self, device, pages, report_root, *, stop_requested=None, log_message=None,
                 clock=None, sleeper=None, game_login=None):
        self.device, self.pages = device, pages
        self.report_root = Path(report_root)
        self.stop_requested = stop_requested or (lambda: False)
        self.log = log_message or (lambda message: print(message, flush=True))
        self.clock, self.sleeper = clock or time.monotonic, sleeper or time.sleep
        self.game_login = game_login

    def check_stop(self):
        if self.stop_requested():
            raise InterruptedError("用户已停止广告奖励任务")

    def pause(self, seconds):
        deadline = self.clock() + seconds
        while self.clock() < deadline:
            self.check_stop()
            self.sleeper(min(.1, deadline - self.clock()))
        self.check_stop()

    def persist(self):
        _write_json(self.directory / "report.json", self.report)

    def observe(self):
        self.check_stop()
        frame = self.device.screenshot()
        self.check_stop()
        state = self.pages.observe(frame)
        self.last_frame = frame
        return state, frame

    def wait_state(self, expected, timeout=20):
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            state, frame = self.observe()
            if state["state"] in expected:
                return state, frame
            self.pause(.25)
        raise TimeoutError("广告导航未确认目标页面：" + "、".join(sorted(expected)))

    def tap_state(self, expected, point, reason):
        state, _ = self.observe()
        if state["state"] not in expected:
            raise RuntimeError(f"{reason}前页面已改变，拒绝使用旧帧输入")
        self.check_stop()
        self.report["navigation"].append({"request": reason, "point": list(point)})
        self.persist()
        self.device.tap(*point)

    def switch_world_map(self):
        deadline = self.clock() + 20.
        requests, last_request = 0, float('-inf')
        stable_since, stable_target = None, None
        while self.clock() < deadline:
            state, _ = self.observe()
            if state['state'] == 'map':
                return state
            if state['state'] != 'world_map' or not state.get('target'):
                # 转场和未知页不追加点击；重新明确世界地图后再等待控件稳定。
                stable_since, stable_target = None, None
                self.pause(.25)
                continue
            if stable_target != state['target']:
                stable_since, stable_target = self.clock(), state['target']
            if (requests < 3 and self.clock() - stable_since >= .4
                    and self.clock() - last_request >= 1.):
                fresh, _ = self.observe()
                if fresh['state'] != 'world_map' or fresh.get('target') != stable_target:
                    stable_since, stable_target = None, None
                    continue
                if self.clock() >= deadline:
                    break
                self.check_stop()
                requests += 1
                last_request = self.clock()
                request = {'request': '从世界地图返回现实世界', 'point': fresh['target'],
                           'world_map_request': requests, 'requested_at': last_request}
                self.report['navigation'].append(request)
                self.persist()
                try:
                    self.device.tap(*fresh['target'])
                except InterruptedError:
                    raise
                except Exception as error:
                    self.check_stop()
                    # 回执失败也占用本次入口的请求预算；是否切换仍由下一张实际地图证明。
                    request['ack_error'] = f'{type(error).__name__}: {error}'
                    self.persist()
                last_request = self.clock()
            self.pause(.25)
        raise TimeoutError("世界地图三次请求或 20 秒期限内未确认实际城市地图")

    def navigate(self):
        state, frame = self.wait_state({"home", "street", "street_cm", "rewards"})
        if state['state'] == 'rewards':
            return state, frame
        self.tap_state({"home", "street", "street_cm"}, (42, 42), "打开主页地图")
        state, _ = self.wait_state({"map", "world_map"})
        if state["state"] == "world_map":
            self.switch_world_map()
        current, _ = self.observe()
        if current["state"] != "map" or not current.get("target"):
            raise RuntimeError("点击前地图标签位置未确认，拒绝沿用旧位置")
        self.report["navigation"].append({"request": "前往スクランブル交差点", "point": current['target']})
        self.persist()
        self.check_stop()
        self.device.tap(*current['target'])
        self.wait_state({"home", "street", "street_cm"})
        self.pause(1.)
        for _ in range(3):
            state, _ = self.observe()
            if state["state"] == "street_cm":
                break
            if state["state"] not in {"home", "street"}:
                state, _ = self.wait_state({"home", "street", "street_cm"})
                if state["state"] == "street_cm":
                    break
            self.check_stop()
            self.report["navigation"].append({"request": "向左滑到街道最右", "swipe": [1050, 520, 250, 520]})
            self.persist()
            self.device.swipe(1050, 520, 250, 520, 450)
            self.pause(1.)
        self.wait_state({"street_cm"})
        current, _ = self.observe()
        if current["state"] != "street_cm" or not current.get("target"):
            raise RuntimeError("点击前手机 CM 位置未确认，拒绝沿用旧位置")
        self.report['navigation'].append({'request': '打开 CM 奖励页', 'point': current['target']})
        self.persist()
        self.check_stop()
        self.device.tap(*current['target'])
        self.pause(.3)
        deadline = self.clock() + 2.
        while self.clock() < deadline:
            state, frame = self.observe()
            if not loading_spinner(frame):
                # 最近一次 CM 请求已成功；不再等待奖励布局或次数识别，以实际观看弹窗决定是否可点。
                return {'state': 'reward_entry'}, frame
            self.pause(.1)
        raise TimeoutError('CM 入口加载转圈未结束，停止固定位置输入')

    def restart_game(self, attempt):
        if not attempt.get("start_requested"):
            raise RuntimeError("尚未确认本任务广告启动，禁止关闭游戏")
        self.check_stop()
        restart = {"force_stop_requested": True, "status": "restarting"}
        attempt["restart"] = restart
        self.report['restart_requests'] += 1
        self.persist()
        self.log("广告 BACK 20 秒未返回：关闭并重启日服游戏，再回到奖励页")
        # 只关闭本任务已请求广告的日服进程；不会触碰其他应用或调用演出控制器。
        self.device.shell("am force-stop com.sega.pjsekai")
        self.check_stop()
        self.device.shell("monkey -p com.sega.pjsekai -c android.intent.category.LAUNCHER 1")
        if self.game_login is None:
            raise RuntimeError("广告重启缺少共用登录服务，停止追加请求")
        restart["login"] = self.game_login.run(wait_for_home=True)
        restart["status"] = "login_returned"
        self.persist()
        self.device.preflight()
        return self.navigate()[0]

    def record_return(self, attempt, state, *, restarted=False):
        attempt.update(status='restarted' if restarted else 'returned', returned_at=self.clock(),
                       returned_after_restart=restarted)
        if restarted:
            self.report['consecutive_restarts'] += 1
        else:
            self.report['normal_returns'] += 1
            self.report['consecutive_restarts'] = 0
        write_image(self.directory / f"ad-{attempt['index']:02d}-returned.png", self.last_frame)
        self.persist()
        return state

    def start_ad(self, state):
        attempt = {'index': len(self.report['ads']) + 1, 'status': 'opening_confirmation',
                   'fixed_click_requests': 0, 'start_requested': False, 'back_requests': 0}
        self.report["ads"].append(attempt)
        for _ in range(3):
            if state['state'] not in {'rewards', 'reward_entry'}:
                state, _ = self.wait_state({'rewards', 'watch_confirm'}, timeout=2.)
            if state['state'] != 'watch_confirm':
                self.check_stop()
                attempt['fixed_click_requests'] += 1
                self.persist()
                self.device.tap(*REWARD_POINT)
            deadline = self.clock() + 2.
            while self.clock() < deadline:
                state, _ = self.observe()
                if state['state'] == 'watch_confirm':
                    self.tap_state({'watch_confirm'}, WATCH_POINT, '视聴開始')
                    attempt.update(start_requested=True, status='waiting_ad')
                    self.report['started_ads'] += 1
                    self.persist()
                    return attempt
                self.pause(.1)
            if state['state'] != 'rewards':
                # 首次 CM 上下文只允许首次固定点击；无弹窗且页面未知时结束，不追加未知页输入。
                break
        attempt['status'] = 'no_confirmation'
        self.persist()
        return None

    def watch(self, attempt):
        self.log(f"广告奖励：第 {attempt['index']} 次，先等待 10 秒，再按 BACK 返回")
        self.pause(10.)
        deadline = self.clock() + 120.
        recovery_at = self.clock() + BACK_RECOVERY_SECONDS
        while self.clock() < deadline:
            state, _ = self.observe()
            if self.clock() >= deadline:
                break
            if state["state"] == "rewards":
                return self.record_return(attempt, state)
            if self.clock() >= recovery_at:
                if self.report['consecutive_restarts'] >= 2:
                    attempt['final_recovery_only'] = True
                restored = self.restart_game(attempt)
                return self.record_return(attempt, restored, restarted=True)
            if state["state"] in {"home", "map", "street", "street_cm", "watch_confirm", "title"}:
                raise RuntimeError("广告等待已离开播放器或返回确认弹窗，停止 BACK 并保留未完成记录")
            self.check_stop()
            attempt["back_requests"] += 1
            self.persist()
            # 广告页只使用用户指定的 BACK，不触碰外链、购买、下载或安装按钮。
            self.device.back()
            self.pause(1.)
        self.check_stop()
        attempt['hard_timeout_recovery'] = True
        restored = self.restart_game(attempt)
        return self.record_return(attempt, restored, restarted=True)

    def run(self):
        self.directory = self.report_root / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:8])
        self.directory.mkdir(parents=True)
        self.report = {"schema_version": 2, "task": "ad_rewards", "completed": False,
                       "started_at": datetime.now().astimezone().isoformat(), "navigation": [],
                       "ads": [], "started_ads": 0, "normal_returns": 0, "restart_requests": 0,
                       "consecutive_restarts": 0, "completed_ads": None, "rewards_claimed": None,
                       "daily_exhausted": None}
        self.last_frame = None
        try:
            self.persist()
            self.check_stop()
            if self.game_login is not None:
                self.report['startup_login'] = self.game_login.run()
            self.device.preflight()
            state, _ = self.navigate()
            while self.report['started_ads'] < MAX_AD_STARTS:
                attempt = self.start_ad(state)
                if attempt is None:
                    self.report['finish_reason'] = 'fixed_position_no_confirmation'
                    self.log('固定位置未弹出观看确认，结束本次点击循环')
                    break
                state = self.watch(attempt)
                if self.report['consecutive_restarts'] >= 3:
                    self.report['finish_reason'] = 'consecutive_restart_limit'
                    self.log('连续三次 BACK 无效，已恢复游戏页面，结束本次点击循环')
                    break
            else:
                self.report['finish_reason'] = 'start_request_limit'
                self.log('本次已请求开始 7 次，结束本次点击循环；领取数量未确认')
            self.report['completed'] = True
            return self.report
        except Exception as error:
            self.report.update(error=f"{type(error).__name__}: {error}", cancelled=self.stop_requested())
            if self.last_frame is not None and not self.stop_requested():
                write_image(self.directory / "failure.png", self.last_frame)
            raise
        finally:
            self.report["finished_at"] = datetime.now().astimezone().isoformat()
            self.persist()
