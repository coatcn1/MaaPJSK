import cv2


class TitleScreenDetected(RuntimeError):
    def __init__(self, frame):
        super().__init__('游戏已返回 TAP TO START 标题页')
        self.frame = frame


def _title_branding_visible(frame):
    if frame.shape[:2] != (720, 1280):
        return False

    # TAP TO START 会淡入淡出，固定的两个品牌标识提供独立证据，不受背景换图影响。
    live2d = cv2.cvtColor(frame[15:64, 1043:1090], cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(live2d, (84, 140, 155), (106, 255, 255))
    components = [cv2.boundingRect(c) for c in cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]]
    for left, right, min_height, max_height in ((4, 9, 14, 22), (10, 15, 26, 36), (24, 30, 36, 46)):
        if not any(left <= x <= right and 2 <= w <= 15 and min_height <= h <= max_height
                   for x, _, w, h in components):
            return False

    criware = cv2.cvtColor(frame[20:53, 1107:1152], cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(criware, (95, 90, 75), (123, 255, 225))
    for contour in cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]:
        x, y, w, h = cv2.boundingRect(contour)
        if 5 <= x <= 13 and 2 <= y <= 10 and 22 <= w <= 33 and 20 <= h <= 29:
            return True
    return False


def title_screen_visible(frame, navigator):
    def matches(name):
        return name in navigator.templates and navigator.match(frame, name)[0] >= navigator.threshold

    # 标题证据与主页、演奏 HUD 同时命中时拒绝接管，点击前仍复查新帧和前台。
    return ((matches('title_screen') or _title_branding_visible(frame))
            and not any(matches(name) for name in ('home', 'playing', 'life_hud')))
