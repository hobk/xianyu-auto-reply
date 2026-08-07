"""
真实鼠标滑块求解引擎（可选，通过系统设置选择）

为什么需要它：
- 闲鱼/阿里 baxia 风控能区分「CDP 注入的鼠标事件」与「真实硬件鼠标事件」。
  实测：Playwright(CDP) 即使回放真人轨迹也被判 code=300（拒），而用 pyautogui 驱动
  物理光标回放同一条真人轨迹则 code=0（通过）。
- 因此业务场景用 SendInput、登录场景用 pyautogui 驱动物理光标，回放预先录制的真人轨迹，
  完成 NC 滑块验证；登录场景继续使用登录专用长位移样本和原有回放逻辑。

代价与限制：
- 运行期间会**接管桌面物理光标约 2~3 秒**，期间人不能同时用鼠标；
- 仅适用于**有图形桌面的 Windows**；无头 Linux / Docker 无法驱动物理鼠标，
  故依赖以「惰性导入」方式加载，导入失败时 REAL_MOUSE_AVAILABLE=False，上层自动回退原逻辑；
- 物理光标全局唯一，故本引擎以全局锁串行执行（同一时刻只解一个滑块）。

对外入口：run_real_mouse_verification(...) -> (是否成功, x5* cookies | None)
返回契约与 run_slider_verification 一致，便于编排层无缝切换。

扩展：use_cdp=True 时通过 CDP 连接本机已登录的真实 Chrome（见 chrome_cdp.py），
仍用物理鼠标回放轨迹，兼顾「真机指纹/登录态」与「真实输入事件」。
"""
from __future__ import annotations

import atexit
import bisect
import glob
import json
import math
import os
import random
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from loguru import logger

from common.services.captcha.slider_stealth import URL_EXPIRED, CAPTCHA_NOT_REQUIRED
from common.services.captcha.weighted_scheduler import real_mouse_scheduler
from common.services.captcha import trail_stats
from common.services.captcha.real_mouse_coordinates import (
    build_geometry_mapper,
    compute_slider_distance,
)
from common.services.captcha.windows_foreground import (
    activate_page_window,
    activate_window,
    is_foreground_window,
)
from common.services.captcha.win_input import (
    display_state,
    precise_sleep,
    send_button,
    send_move_abs,
    timer_resolution,
)
from common.services.captcha.chrome_cdp import (
    chrome_proxy_server_arg,
    ensure_cdp_chrome,
    get_captcha_proxy,
    get_cdp_endpoint,
    proxy_auth_from_url,
)
from common.utils.xianyu_utils import trans_cookies

from playwright.sync_api import sync_playwright

# —— 惰性/可选依赖：仅在有桌面的 Windows 上可用，导入失败则标记为不可用 ——
try:
    import pyautogui

    pyautogui.PAUSE = 0
    pyautogui.FAILSAFE = False
    REAL_MOUSE_AVAILABLE = True
except Exception as _e:  # noqa: BLE001  （任何导入异常都视为不可用）
    pyautogui = None  # type: ignore
    REAL_MOUSE_AVAILABLE = False
    logger.warning(f"真实鼠标引擎不可用（pyautogui 导入失败，将回退原逻辑）: {_e}")

try:
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore


# 物理光标全局唯一 → 串行执行。
# 串行由 real_mouse_scheduler（加权公平单槽位调度器）保证：多来源同时排队时按权重放行，
# 只有一方排队时该方独占。替代了原先的普通 threading.Lock（无优先级、盲抢）。

# 风控未放行的 URL 关键字
_PUNISH = ("punish", "x5step=2", "action=captcha", "pureCaptcha", "/captcha")
_MAX_REPLAY_DURATION_MS = 2600.0
_BUSINESS_SEGMENT_GAP_MS = 500.0
_PREFERRED_BUSINESS_TRAIL = "human_trail_pass_1783943859.json"
# Existing business samples were collected on the standard 258px NC slider.
# New samples can override this value with a top-level slider_distance field.
_LEGACY_BUSINESS_CAPTURE_DISTANCE_PX = 258.0
_BROWSER_DEFAULT_TIMEOUT_MS = 6000
_BROWSER_NAVIGATION_TIMEOUT_MS = 12000
# 上游按「保留到底后的超出段回放」设计，故用 80px 剔除长尾样本。
# 本实现改回末点归一（见 _scale_drag_to_distance），超出段根本不会被回放，
# 这个筛选就失去意义——而且它恰好会把本机通过率最高的三条样本（尾巴 83~138px）全砍掉。
# 置为 0 表示不按超出段筛样本；样本质量仍由 passed/slide_code 把关。
_MAX_BUSINESS_CAPTURE_OVERSHOOT_PX = 0.0
_BROWSER_CRASH_LOG_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "logs", "captcha_browser_crash.jsonl")
)
_BROWSER_CRASH_LOG_LOCK = threading.Lock()
# CAPTCHA_FAST_MODE 默认关闭：过快会抬高 code=300。仅明确设 1 才加速。
def _fast_mode() -> bool:
    return (os.environ.get("CAPTCHA_FAST_MODE") or "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


# 连续 code=300 时自动进入「谨慎模式」：更慢、更随机，避免被同一指纹连打
_risk_lock = threading.Lock()
_consecutive_rejects = 0
_last_reject_at = 0.0
_last_solve_end_at = 0.0


def _note_slide_result(ok: bool, rejected_300: bool = False) -> None:
    global _consecutive_rejects, _last_reject_at, _last_solve_end_at
    with _risk_lock:
        _last_solve_end_at = time.time()
        if ok:
            _consecutive_rejects = 0
        elif rejected_300:
            _consecutive_rejects += 1
            _last_reject_at = time.time()


def _risk_level() -> int:
    """0=正常 1=偏高 2=高压（连续拒绝）。"""
    with _risk_lock:
        n = _consecutive_rejects
        age = time.time() - _last_reject_at if _last_reject_at else 9999
    if age > 600:
        return 0
    if n >= 4:
        return 2
    if n >= 2:
        return 1
    return 0


def _inter_task_gap() -> None:
    """任务间隔：正常可稍快；连续 code=300 后必须拉长，否则越打越黑。"""
    risk = _risk_level()
    if risk >= 2:
        min_gap = random.uniform(4.0, 8.0)
    elif risk == 1:
        min_gap = random.uniform(1.5, 3.0)
    else:
        min_gap = random.uniform(0.4, 1.0)
    with _risk_lock:
        last = _last_solve_end_at
    if last > 0:
        wait = min_gap - (time.time() - last)
        if wait > 0:
            logger.info(f"滑块任务间隔等待 {wait:.1f}s (risk={risk})")
            time.sleep(wait)


def _reset_risk() -> None:
    """换资料/成功后清零连续拒绝计数。"""
    global _consecutive_rejects, _last_reject_at
    with _risk_lock:
        _consecutive_rejects = 0
        _last_reject_at = 0.0


def _append_browser_crash_log(record: dict) -> None:
    """把浏览器崩溃/超时记录追加到独立 JSONL 文件。"""
    try:
        os.makedirs(os.path.dirname(_BROWSER_CRASH_LOG_PATH), exist_ok=True)
        payload = dict(record)
        payload.setdefault("ts", time.strftime("%Y-%m-%d %H:%M:%S"))
        line = json.dumps(payload, ensure_ascii=False, default=str)
        with _BROWSER_CRASH_LOG_LOCK:
            with open(_BROWSER_CRASH_LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as exc:
        logger.warning(f"记录浏览器崩溃日志失败: {exc}")


def _business_move_ms_range() -> Tuple[float, float]:
    """合成兜底轨迹的时长区间（毫秒）。

    只用于 _synthetic_business_drag——真人样本一律按录制原速回放，不再重定时。
    取值依据（来自 logs/pm2/websocket-err.log 的 2655 次回放统计，而非猜测）：
    - 录制的真人通过样本按下到松手只有 424~432ms；
    - 而旧实现把所有轨迹强行拉到 1100~2500ms，实测通过率反而随时长单调下降
      （1000~1200ms 段 10.4% → 2000~2400ms 段 5.0%）。
    """
    risk = _risk_level()
    if _fast_mode() and risk == 0:
        return 330.0, 520.0
    if risk >= 2:
        return 480.0, 950.0
    if risk == 1:
        return 420.0, 760.0
    return 380.0, 620.0


# 真人硬件鼠标的事件上报节拍：录制样本相邻 mousemove 间隔中位数 6.7~8.0ms（125~150Hz）。
# 回放时必须保持同一量级，否则页面只能收到 ~30Hz 的稀疏事件，是合成输入的显著特征。
_HW_CADENCE_MEAN_MS = 7.6
_HW_CADENCE_SIGMA_MS = 1.8
_HW_CADENCE_MIN_MS = 4.5
_HW_CADENCE_MAX_MS = 13.0
# 松手前的停顿：实测 rel=439ms 的样本 53.7% 通过、rel=249ms 40.0%、rel=0ms 仅 19.5%
_MIN_RELEASE_DELAY_MS = 220.0
_MIN_PRESS_DELAY_MS = 55.0


# 真人鼠标模式专用固定目录：本地与远程请求共用，用于复用和精确识别 Chrome 进程。
_REAL_MOUSE_BROWSER_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "browser_data", "real_mouse_shared")
)
_REAL_MOUSE_BROWSER_LOCK = os.path.join(_REAL_MOUSE_BROWSER_DIR, "browser.lock")


class _TimedDrag(list):
    """拖动点列表，并保留采集按下段的原点与松手时间。"""

    def __init__(
        self,
        points=(),
        press_delay_ms: float = 0.0,
        release_delay_ms: float = 0.0,
        origin_x: Optional[float] = None,
        origin_y: Optional[float] = None,
        pressed_at: Optional[float] = None,
        approach=(),
        approach_to_press_ms: float = 0.0,
        capture_distance_px: Optional[float] = None,
        source_file: str = "",
        synthetic: bool = False,
    ):
        super().__init__(points)
        self.press_delay_ms = max(0.0, float(press_delay_ms))
        self.release_delay_ms = max(0.0, float(release_delay_ms))
        self.origin_x = origin_x
        self.origin_y = origin_y
        self.pressed_at = pressed_at
        self.approach = list(approach)
        self.approach_to_press_ms = max(0.0, float(approach_to_press_ms))
        try:
            parsed_capture_distance = float(capture_distance_px)
        except (TypeError, ValueError):
            parsed_capture_distance = 0.0
        self.capture_distance_px = parsed_capture_distance if parsed_capture_distance > 0 else None
        # source_file 同时作为按样本统计通过率（trail_stats）的 key
        self.source_file = str(source_file or "")
        self.synthetic = bool(synthetic)

# 仅隐藏 webdriver，绝不伪造与真实 Chrome 冲突的指纹（UA/WebGL 交给真实 Chrome）
# 真机 Chrome 尽量少动指纹。webdriver 在有头真 Chrome 上通常已是 false；
# 过度 defineProperty 反而制造“被补丁过”的痕迹。仅清 Playwright 痕迹。
_STEALTH_MINIMAL = """
try { delete window.__playwright; delete window.__pw_manual; delete window.__PW_inspect; delete window.playwright; } catch (e) {}
try {
  if (navigator.webdriver === true) {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined, configurable: true });
  }
} catch (e) {}
"""

# 仅在验证页注入：捕获鼠标事件，用于「主视口坐标 -> 屏幕坐标」校准
_CAP_JS = r"""
(() => {
  if (window.__cal) return;
  window.__cal = [];
  const push = (e) => {
    try {
      window.__cal.push([e.clientX, e.clientY, e.screenX, e.screenY, e.timeStamp, e.buttons|0]);
      if (window.__cal.length > 400) window.__cal.splice(0, window.__cal.length - 300);
    } catch (err) {}
  };
  document.addEventListener('mousemove', push, true);
  document.addEventListener('pointermove', push, true);
})();
"""

_BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-popup-blocking",
    "--force-color-profile=srgb",
    "--lang=zh-CN",
    "--start-maximized",       # 窗口默认最大化（配合 no_viewport 生效）
]


def _trails_dir() -> str:
    """真人轨迹样本目录（与本文件同级 human_trails/）。"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "human_trails")


def _detect_scene(url: str) -> str:
    """按验证链接 URL 判滑块场景：登录滑块 vs 业务滑块。

    登录滑块出现在 passport 登录接口 punish（/newlogin/login.do/_____tmd_____/punish），
    其滑条更宽、需登录专用长位移轨迹并强制最大化窗口；其余（token/业务刷新）为 business。
    业务滑块 URL 不含 /newlogin/login.do，故不会误判。
    """
    return "login" if "/newlogin/login.do" in (url or "") else "business"


def _extract_business_drag(trail: list) -> list:
    """切分业务轨迹中的多次按下操作，保留完整按住时序。"""
    segments: List[_TimedDrag] = []
    current: list = []
    pressed_at: Optional[float] = None
    origin_x: Optional[float] = None
    origin_y: Optional[float] = None
    pending_approach: list = []
    active_approach: list = []

    def finish(released_at: Optional[float] = None) -> None:
        nonlocal current, pressed_at, origin_x, origin_y, active_approach
        if not current:
            return
        first_at = float(current[0][3])
        last_at = float(current[-1][3])
        press_delay_ms = first_at - pressed_at if pressed_at is not None else 0.0
        release_delay_ms = released_at - last_at if released_at is not None else 0.0
        segments.append(
            _TimedDrag(
                current,
                press_delay_ms=press_delay_ms,
                release_delay_ms=release_delay_ms,
                origin_x=origin_x,
                origin_y=origin_y,
                pressed_at=pressed_at,
                approach=active_approach,
            )
        )
        current = []
        pressed_at = None
        origin_x = None
        origin_y = None
        active_approach = []

    for event in trail:
        if not isinstance(event, list) or len(event) < 5:
            continue
        event_type = event[0]
        if event_type in ("mousedown", "pointerdown") and event[4] == 1:
            if current:
                finish()
            if pressed_at is None:
                pressed_at = float(event[3])
                origin_x = float(event[1])
                origin_y = float(event[2])
                active_approach = list(pending_approach)
            continue
        if event_type in ("mousemove", "pointermove") and event[4] == 1:
            if current and event[3] - current[-1][3] > _BUSINESS_SEGMENT_GAP_MS:
                finish()
                pressed_at = float(event[3])
                origin_x = float(event[1])
                origin_y = float(event[2])
            current.append(event)
            continue
        if event_type in ("mouseup", "pointerup"):
            finish(float(event[3]))
            pressed_at = None
            pending_approach = []
            continue
        if current and event_type in ("mousemove", "pointermove"):
            finish(float(event[3]))
        if event_type in ("mousemove", "pointermove") and event[4] == 0:
            pending_approach.append(event)
    if current:
        finish()

    forward = [segment for segment in segments if segment[-1][1] - segment[0][1] > 5]
    return max(forward, key=lambda segment: segment[-1][1] - segment[0][1], default=[])


def _load_drags(scene: str = "business") -> List[List[Tuple[float, float, float]]]:
    """加载真人通过轨迹，提取「按下拖动段」为相对位移序列 [(dx, dy, dt_ms), ...]。

    Args:
        scene: "business"（默认，业务/Token 刷新滑块，样本 human_trail_pass_*.json）
               或 "login"（登录滑块，样本 human_trail_login_*.json，长位移）
    """
    pattern = "human_trail_login_*.json" if scene == "login" else "human_trail_pass_*.json"
    files = sorted(glob.glob(os.path.join(_trails_dir(), pattern)))
    preferred: Optional[str] = None
    if scene == "business":
        preferred_path = os.path.join(_trails_dir(), _PREFERRED_BUSINESS_TRAIL)
        if os.path.isfile(preferred_path):
            preferred = preferred_path
            files = [preferred] + [f for f in files if f != preferred]
        else:
            logger.warning(
                f"业务优选真人轨迹不存在，回退全部业务样本: {_PREFERRED_BUSINESS_TRAIL}"
            )
    drags: List[List[Tuple[float, float, float]]] = []
    preferred_drag: Optional[List[Tuple[float, float, float]]] = None
    rejected_count = 0
    excessive_overshoot_count = 0
    for f in files:
        try:
            with open(f, encoding="utf-8") as trail_file:
                data = json.load(trail_file)
            if scene == "login":
                if data.get("passed") is False:
                    logger.warning(f"跳过未通过的真人轨迹样本: {f}")
                    continue
                trail = data.get("trail", [])
            else:
                if data.get("passed") is False:
                    rejected_count += 1
                    continue
                if data.get("slide_code") == 300:
                    rejected_count += 1
                    continue
                trail = data.get("trail", [])
        except Exception as e:
            logger.warning(f"加载真人轨迹失败 {f}: {e}")
            continue
        if scene == "business":
            seg = _extract_business_drag(trail)
        else:
            # 登录滑块保持原有提取方式，不改变登录专用长轨迹行为。
            moves = [e for e in trail if isinstance(e, list) and len(e) >= 5 and e[0] == "mousemove"]
            seg = [e for e in moves if len(e) >= 5 and e[4] == 1]
        if len(seg) < 5:
            continue
        if scene == "business" and getattr(seg, "pressed_at", None) is not None:
            x0 = seg.origin_x
            y0 = seg.origin_y
            prev = seg[0][3]
        else:
            x0, y0, prev = seg[0][1], seg[0][2], seg[0][3]
        raw_duration_ms = max(0.0, seg[-1][3] - seg[0][3])
        press_delay_ms = getattr(seg, "press_delay_ms", 0.0)
        release_delay_ms = getattr(seg, "release_delay_ms", 0.0)
        raw_approach = getattr(seg, "approach", []) if scene == "business" else []
        approach: List[Tuple[float, float, float]] = []
        if raw_approach:
            approach_prev = raw_approach[0][3]
            for p in raw_approach:
                approach.append(
                    (
                        p[1] - x0,
                        p[2] - y0,
                        max(0.0, p[3] - approach_prev),
                    )
                )
                approach_prev = p[3]
        approach_to_press_ms = (
            max(0.0, seg.pressed_at - raw_approach[-1][3])
            if raw_approach and getattr(seg, "pressed_at", None) is not None
            else 0.0
        )
        gesture_duration_ms = press_delay_ms + raw_duration_ms + release_delay_ms
        if scene == "business" and gesture_duration_ms > _MAX_REPLAY_DURATION_MS:
            continue
        rel: List[Tuple[float, float, float]] = []
        for p in seg:
            dt = max(0.0, p[3] - prev)
            rel.append((p[1] - x0, p[2] - y0, dt))
            prev = p[3]
        if scene == "business":
            duration_ms = sum(point[2] for point in rel)
            distance = rel[-1][0]
            if len(rel) < 20 or duration_ms < 350 or duration_ms > _MAX_REPLAY_DURATION_MS:
                continue
            if distance < 120 or distance > 1200:
                continue
            try:
                capture_distance_px = float(
                    data.get("slider_distance")
                    or _LEGACY_BUSINESS_CAPTURE_DISTANCE_PX
                )
            except (TypeError, ValueError):
                capture_distance_px = _LEGACY_BUSINESS_CAPTURE_DISTANCE_PX
            capture_overshoot_px = distance - capture_distance_px
            if (
                _MAX_BUSINESS_CAPTURE_OVERSHOOT_PX > 0
                and capture_overshoot_px > _MAX_BUSINESS_CAPTURE_OVERSHOOT_PX
            ):
                excessive_overshoot_count += 1
                continue
        else:
            capture_distance_px = None
        replay_drag = _TimedDrag(
            rel,
            press_delay_ms=press_delay_ms if scene == "business" else 0.0,
            release_delay_ms=release_delay_ms if scene == "business" else 0.0,
            origin_x=x0 if scene == "business" else None,
            origin_y=y0 if scene == "business" else None,
            pressed_at=getattr(seg, "pressed_at", None) if scene == "business" else None,
            approach=approach,
            approach_to_press_ms=approach_to_press_ms,
            capture_distance_px=capture_distance_px,
            source_file=os.path.basename(f),
        )
        drags.append(replay_drag)
        if preferred and f == preferred:
            preferred_drag = replay_drag
    if scene == "business":
        logger.debug(
            f"业务真人轨迹池: 可用={len(drags)}, "
            f"未通过或code=300={rejected_count}, "
            f"超出>{_MAX_BUSINESS_CAPTURE_OVERSHOOT_PX:.0f}px={excessive_overshoot_count}"
        )
    if scene == "business" and preferred:
        if preferred_drag is not None:
            return [preferred_drag] + [drag for drag in drags if drag is not preferred_drag]
        logger.warning(
            f"业务优选真人轨迹无效，回退其他业务样本: {_PREFERRED_BUSINESS_TRAIL}"
        )
    # 默认返回全部有效轨迹，供加权随机选用，降低固定轨迹指纹
    if scene == "business" and preferred_drag is not None and preferred_drag not in drags:
        drags.insert(0, preferred_drag)
    logger.info(f"业务滑块可用真人轨迹数: {len(drags)}")
    return drags


def _human_mouse_to(tx: int, ty: int, dur: float) -> None:
    """pyautogui 贝塞尔平滑移动物理光标到目标点（拟人接近）。"""
    x0, y0 = pyautogui.position()
    cx = x0 + (tx - x0) * random.uniform(0.2, 0.4) + random.uniform(-30, 30)
    cy = y0 + (ty - y0) * random.uniform(0.2, 0.4) + random.uniform(-30, 30)
    n = max(10, int(dur / 0.012))
    for i in range(1, n + 1):
        t = i / n
        mt = 1 - t
        x = mt * mt * x0 + 2 * mt * t * cx + t * t * tx
        y = mt * mt * y0 + 2 * mt * t * cy + t * t * ty
        pyautogui.moveTo(int(x), int(y))
        time.sleep(dur / n * random.uniform(0.6, 1.4))


def _choose_drag(drags: List[List[Tuple[float, float, float]]]) -> List[Tuple[float, float, float]]:
    """按实测通过率加权随机选择轨迹（key 为样本文件名）。

    不要改回「点数 + 时长」那类静态打分：它会把时长 <800ms 的样本压到极低权重，
    而真人录制样本恰恰就是 424~432ms。实测（2655 次回放）结果完全相反：

        真人样本  20% ~ 54% 通过
        合成兜底轨迹      7.1% 通过 (n=2538)   ← 却被选中 95.7% 的次数

    现按每条样本的历史通过率（Laplace 平滑 + 采样不足探索加成）加权，
    合成轨迹额外降权，只作真人样本全部失效时的兜底。
    """
    if not drags:
        raise ValueError("empty drags")
    weights: List[float] = []
    for drag in drags:
        key = getattr(drag, "source_file", "") or "unknown"
        w = trail_stats.weight(key)
        if getattr(drag, "synthetic", False):
            # 合成轨迹实测远差于真人录制样本，仅保留少量兜底/探索概率
            w *= 0.12
        weights.append(max(0.01, w) * random.uniform(0.85, 1.2))
    return random.choices(drags, weights=weights, k=1)[0]


def _take_drag(
    remaining_drags: List[List[Tuple[float, float, float]]],
) -> List[Tuple[float, float, float]]:
    """Choose and remove one sample so retries do not repeat it."""
    selected_drag = _choose_drag(remaining_drags)
    for index, candidate in enumerate(remaining_drags):
        if candidate is selected_drag:
            remaining_drags.pop(index)
            break
    return selected_drag


def _resample_to_hardware_cadence(
    points: List[Tuple[float, float, float]],
) -> List[Tuple[float, float, float]]:
    """把轨迹重采样到真人硬件鼠标的事件节拍（约 6~10ms/点）。

    录制样本相邻 mousemove 间隔中位数 6.7~8.0ms；若时间轴被拉长而点数不变，
    页面收到的事件密度会掉到 30Hz 量级，与真实硬件差一个数量级。
    这里按累计时间轴线性插值补点，保持原速度曲线不变、只补足事件密度。
    """
    if len(points) < 3:
        return list(points)

    times: List[float] = []
    total = 0.0
    for _, _, dt in points:
        total += max(0.0, float(dt))
        times.append(total)
    if total <= _HW_CADENCE_MAX_MS * 2:
        return list(points)

    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]

    def at(t: float) -> Tuple[float, float]:
        i = bisect.bisect_left(times, t)
        if i <= 0:
            return xs[0], ys[0]
        if i >= len(times):
            return xs[-1], ys[-1]
        t0, t1 = times[i - 1], times[i]
        ratio = 0.0 if t1 <= t0 else (t - t0) / (t1 - t0)
        return (
            xs[i - 1] + (xs[i] - xs[i - 1]) * ratio,
            ys[i - 1] + (ys[i] - ys[i - 1]) * ratio,
        )

    out: List[Tuple[float, float, float]] = [(xs[0], ys[0], times[0])]
    prev = times[0]
    cursor = times[0]
    while True:
        step = min(
            _HW_CADENCE_MAX_MS,
            max(_HW_CADENCE_MIN_MS, random.gauss(_HW_CADENCE_MEAN_MS, _HW_CADENCE_SIGMA_MS)),
        )
        cursor += step
        if cursor >= total - _HW_CADENCE_MIN_MS:
            break
        x, y = at(cursor)
        out.append((x, y, cursor - prev))
        prev = cursor
    out.append((xs[-1], ys[-1], max(3.0, total - prev)))
    return out


def _scale_drag_to_distance(
    drag: List[Tuple[float, float, float]],
    distance: float,
) -> List[Tuple[float, float, float]]:
    """把真人轨迹的末点归一到当前滑轨终点，不回放「到底后的超出段」。

    时间轴与 Y 一律保持录制原样——样本本身就是通过的那一次，原速回放最忠实。

    为什么不用上游的「按采集滑轨基准映射、保留超出段」：
    实测（本机 2026-07-26）两种映射差距悬殊——
        末点归一（本实现）  74% / 70% / 39%   （三条老样本）
        保留超出段回放      13% / 11% /  8% / 7%（四条上游样本，n=60）
    上游据此加了 `_MAX_BUSINESS_CAPTURE_OVERSHOOT_PX=80` 剔除长尾样本，但在本机环境下
    那恰好把通过率最高的三条样本全过滤掉了。故此处保留末点归一，并不再按超出段筛样本。
    """
    if not drag or distance <= 0 or drag[-1][0] <= 1:
        return drag
    capture_distance = getattr(drag, "capture_distance_px", None)
    # 末点归一：以样本自身的末点位移为基准缩放，终点精确贴合当前滑轨终点
    factor = distance / drag[-1][0]
    points = [(dx * factor, dy, dt) for dx, dy, dt in drag]

    # 按真人硬件鼠标节拍补点。录制样本相邻事件中位数 6.7~8.0ms（125~150Hz），
    # 而回放若只发原始点数，页面收到的事件密度只有 ~30Hz，与真实硬件差一个数量级。
    points = _resample_to_hardware_cadence(points)

    # 末点精确落到终点，并确保 X 单调不倒退（倒退极易 300）
    fixed = []
    last_x = -1e9
    for i, (dx, dy, dt) in enumerate(points):
        if i == len(points) - 1:
            fixed.append((distance, dy, dt))
        else:
            x = min(distance - 0.5, max(last_x + 0.05, dx))
            fixed.append((x, dy, dt))
            last_x = x
    points = fixed

    # 到位后停顿越久通过率越高（实测 rel=0ms→19.5% / 249ms→40% / 439ms→53.7%），
    # 只抬高过短的停顿，录制值本身够长时原样保留。
    press_delay = max(_MIN_PRESS_DELAY_MS, getattr(drag, "press_delay_ms", 0.0) or 0.0)
    release_delay = max(_MIN_RELEASE_DELAY_MS, getattr(drag, "release_delay_ms", 0.0) or 0.0)

    return _TimedDrag(
        points,
        press_delay_ms=press_delay,
        release_delay_ms=release_delay,
        origin_x=getattr(drag, "origin_x", None),
        origin_y=getattr(drag, "origin_y", None),
        pressed_at=getattr(drag, "pressed_at", None),
        approach=getattr(drag, "approach", []),
        approach_to_press_ms=getattr(drag, "approach_to_press_ms", 0.0),
        capture_distance_px=capture_distance,
        source_file=getattr(drag, "source_file", ""),
        synthetic=getattr(drag, "synthetic", False),
    )


def _synthetic_business_drag(distance: float) -> List[Tuple[float, float, float]]:
    """生成一条缓入缓出的业务滑块轨迹（真人样本失效时的兜底）。"""
    distance = max(120.0, float(distance))
    n = random.randint(36, 55)
    _bmin, _bmax = _business_move_ms_range()
    duration = random.uniform(_bmin, _bmax)
    points: List[Tuple[float, float, float]] = []
    prev_t = 0.0
    for i in range(n):
        t = i / (n - 1)
        # smoothstep ease-in-out
        eased = t * t * (3.0 - 2.0 * t)
        # 中段略加速再收尾减速的扰动
        eased = min(1.0, max(0.0, eased + math.sin(t * math.pi) * random.uniform(-0.02, 0.02)))
        dx = distance * eased
        dy = random.uniform(-1.2, 1.2) * math.sin(t * math.pi)
        abs_t = duration * t
        dt = abs_t - prev_t
        prev_t = abs_t
        points.append((dx, dy if i not in (0, n - 1) else 0.0, max(4.0, dt)))
    points[-1] = (distance, 0.0, points[-1][2])
    return _TimedDrag(
        _resample_to_hardware_cadence(points),
        press_delay_ms=random.uniform(60, 120),
        release_delay_ms=random.uniform(_MIN_RELEASE_DELAY_MS, 420),
        approach=[],
        approach_to_press_ms=0.0,
        source_file="synthetic",
        capture_distance_px=distance,
        synthetic=True,
    )


class _RealMouseSolver:
    """可复用真实鼠标滑块求解器（固定浏览器目录、自然指纹）。

    use_cdp=True 时改为连接本机调试 Chrome（用户已登录闲鱼的真机环境），
    不清理登录 Cookie，仅新开标签页做滑块。
    """

    def __init__(self, user_id: str, use_cdp: bool = False):
        self.user_id = str(user_id)
        self.pure_id = self.user_id.split("_")[0] if "_" in self.user_id else self.user_id
        self.use_cdp = bool(use_cdp)
        self.pw = None
        self.browser = None  # CDP 模式下的 Browser 对象
        self.context = None
        self.page = None
        self.browser_dir = _REAL_MOUSE_BROWSER_DIR
        os.makedirs(self.browser_dir, exist_ok=True)
        self._browser_lock_file = None
        self._slide_code: Optional[int] = None
        self._timed_out = False
        self._browser_broken_reason: Optional[str] = None
        self._window_handle: Optional[int] = None
        self._owned_captcha_page = False  # CDP 下仅关闭我们创建的验证页
        self._task_cookies_str: str = ""

    # ---------- 浏览器 ----------
    def update_user(self, user_id: str) -> None:
        """更新当前任务日志标识，不改变共享浏览器实例。"""
        self.user_id = str(user_id)
        self.pure_id = self.user_id.split("_")[0] if "_" in self.user_id else self.user_id

    def _attach_response_listener(self) -> None:
        """监听 slide 接口 result.code，用于判定通过/拒绝。"""
        if self.context is None:
            return

        def _on_resp(resp):
            try:
                if "/slide" in resp.url and "_____tmd_____" in resp.url:
                    v = resp.json().get("result", {}).get("code")
                    if v is not None:
                        self._slide_code = v
            except Exception:
                pass

        try:
            self.context.on("response", _on_resp)
        except Exception:
            pass

    def _mark_browser_broken(self, reason: str) -> None:
        """标记当前浏览器或页面已经失效。"""
        if self._browser_broken_reason:
            return
        self._browser_broken_reason = str(reason or "browser broken")
        page_url = ""
        try:
            if self.page is not None and not self.page.is_closed():
                page_url = self.page.url or ""
        except Exception:
            page_url = ""
        _append_browser_crash_log({
            "event": "browser_broken",
            "user_id": self.user_id,
            "pure_id": self.pure_id,
            "use_cdp": self.use_cdp,
            "reason": self._browser_broken_reason,
            "page_url": page_url,
            "browser_connected": bool(self.browser is not None and getattr(self.browser, "is_connected", lambda: False)()),
            "timed_out": self._timed_out,
        })
        logger.warning(f"【{self.pure_id}】检测到浏览器异常中断: {self._browser_broken_reason}")

    def _attach_lifecycle_listeners(self) -> None:
        """监听浏览器、上下文和页面崩溃事件。"""
        if self.browser is not None:
            try:
                self.browser.on("disconnected", lambda: self._mark_browser_broken("browser disconnected"))
            except Exception:
                pass
        if self.context is not None:
            try:
                self.context.on("close", lambda: self._mark_browser_broken("browser context closed"))
            except Exception:
                pass
        if self.page is not None:
            try:
                self.page.on("crash", lambda: self._mark_browser_broken("page crashed"))
            except Exception:
                pass

    def _apply_default_timeouts(self) -> None:
        """给页面和上下文设置统一默认超时，避免异常浏览器把单个操作卡死。"""
        for target in (self.context, self.page):
            if target is None:
                continue
            try:
                target.set_default_timeout(_BROWSER_DEFAULT_TIMEOUT_MS)
            except Exception:
                pass
            try:
                target.set_default_navigation_timeout(_BROWSER_NAVIGATION_TIMEOUT_MS)
            except Exception:
                pass

    def _browser_is_healthy(self) -> bool:
        """检查当前浏览器是否仍可继续承载本次滑块任务。"""
        if self._timed_out or self._browser_broken_reason:
            return False
        try:
            if self.browser is not None and hasattr(self.browser, "is_connected"):
                if not self.browser.is_connected():
                    self._mark_browser_broken("browser disconnected")
                    return False
        except Exception as exc:
            self._mark_browser_broken(f"browser health check failed: {exc}")
            return False
        try:
            if self.page is not None and self.page.is_closed():
                self._mark_browser_broken("page closed")
                return False
        except Exception as exc:
            self._mark_browser_broken(f"page health check failed: {exc}")
            return False
        return True

    def _init_cdp_browser(self) -> None:
        """通过 CDP 接入本机干净配置 Chrome（chrome_clean_manual）。"""
        ok, message = ensure_cdp_chrome(force_clean=True)
        if not ok:
            raise RuntimeError(message)

        endpoint = get_cdp_endpoint()
        from common.services.captcha.chrome_cdp import get_user_data_dir, is_cdp_using_expected_profile
        profile_ok, profile_detail = is_cdp_using_expected_profile()
        logger.info(
            f"【{self.pure_id}】CDP 连接干净 Chrome: {endpoint} "
            f"user_data={get_user_data_dir()} | {profile_detail}"
        )
        if not profile_ok:
            raise RuntimeError(f"CDP 未切换到干净配置: {profile_detail}")
        self.pw = sync_playwright().start()
        try:
            self.browser = self.pw.chromium.connect_over_cdp(endpoint, timeout=20000)
        except Exception as e:
            try:
                self.pw.stop()
            except Exception:
                pass
            self.pw = None
            raise RuntimeError(f"CDP 连接失败 ({endpoint}): {e}") from e

        contexts = list(self.browser.contexts or [])
        if not contexts:
            # 极少数情况下无默认 context，退回新开（依赖浏览器支持）
            raise RuntimeError("CDP 已连接但未找到 BrowserContext，请确认 Chrome 已完全启动")
        self.context = contexts[0]
        # 真机 CDP：不在整个 context 注入 init_script（污染所有标签指纹）。
        # 校准脚本仅挂到本次验证页（见 _prepare_cdp_page）。
        try:
            # 只清 playwright 痕迹，不做大范围 navigator 伪造
            self.context.add_init_script(_STEALTH_MINIMAL)
        except Exception as e:
            logger.warning(f"【{self.pure_id}】CDP context 注入最小清理失败（继续）: {e}")
        self._attach_response_listener()
        self._attach_lifecycle_listeners()
        # 验证页在 prepare_task 中复用/创建；连接后立刻清掉历史堆积标签
        self.page = None
        self._owned_captcha_page = False
        closed = self._prune_extra_pages(keep=None)
        n_pages = len(list(self.context.pages or []))
        self._browser_broken_reason = None
        logger.info(
            f"【{self.pure_id}】已连接真实 Chrome（CDP），"
            f"标签数={n_pages}（已清理堆积 {closed} 个，不做全量指纹注入）"
        )

    def init_browser(self) -> None:
        if self.use_cdp:
            self._init_cdp_browser()
            return

        self._acquire_browser_lock()
        # 当前进程没有可用上下文时，先清理固定目录对应的孤儿 Chrome。
        self._kill_browser_processes(log_result=False)
        try:
            self.pw = sync_playwright().start()
            launch_args = list(_BROWSER_ARGS)
            proxy_cfg = None
            proxy_url = get_captcha_proxy()
            proxy_server = chrome_proxy_server_arg(proxy_url)
            if proxy_server:
                # Playwright 原生代理（支持账号密码）；比 Chrome 命令行更可靠
                user, pwd = proxy_auth_from_url(proxy_url)
                proxy_cfg = {"server": proxy_server}
                if user:
                    proxy_cfg["username"] = user
                    proxy_cfg["password"] = pwd
                logger.info(f"【{self.pure_id}】真实鼠标独立 Chrome 使用代理: {proxy_server}")
            self.context = self.pw.chromium.launch_persistent_context(
                self.browser_dir,
                channel="chrome",          # 用本机真实 Chrome（自然指纹），非自带 Chromium
                headless=False,            # 真实鼠标必须有可见窗口
                args=launch_args,
                proxy=proxy_cfg,
                no_viewport=True,          # 不强制 viewport，保留真实窗口尺寸
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                ignore_https_errors=True,
                extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
                timeout=30000,
            )
        except Exception:
            self._release_browser_lock()
            raise
        self.context.add_init_script(_STEALTH_MINIMAL)
        self.context.add_init_script(_CAP_JS)
        self._attach_response_listener()
        pages = list(self.context.pages)
        self.page = pages[0] if pages else self.context.new_page()
        for extra_page in pages[1:]:
            try:
                extra_page.close()
            except Exception:
                pass
        self.page.bring_to_front()
        self._browser_broken_reason = None

    def _acquire_browser_lock(self) -> None:
        """跨进程独占固定浏览器目录，防止多个服务进程同时启动真人鼠标 Chrome。"""
        if self._browser_lock_file is not None:
            return
        if sys.platform != "win32" or msvcrt is None:
            return
        lock_file = open(_REAL_MOUSE_BROWSER_LOCK, "a+b")
        try:
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as e:
            lock_file.close()
            raise RuntimeError("真人鼠标共享浏览器已被另一个服务进程占用") from e
        self._browser_lock_file = lock_file

    def _release_browser_lock(self) -> None:
        """释放真人鼠标固定浏览器目录的跨进程锁。"""
        lock_file = self._browser_lock_file
        self._browser_lock_file = None
        if lock_file is None:
            return
        try:
            lock_file.seek(0)
            if sys.platform == "win32" and msvcrt is not None:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        try:
            lock_file.close()
        except Exception:
            pass

    def ensure_browser(self) -> None:
        """确认共享 Chrome/Context/Page 可用，失效时自动完整重启。"""
        if self.use_cdp:
            context_ok = False
            try:
                if self.context is not None and self.browser is not None:
                    _ = self.context.pages
                    context_ok = True
            except Exception:
                context_ok = False
            if context_ok:
                return
            self.close()
            self.init_browser()
            return

        context_ok = False
        try:
            if self.context is not None:
                _ = self.context.pages
                context_ok = True
        except Exception:
            context_ok = False
        if context_ok:
            try:
                if self.page is None or self.page.is_closed():
                    self.page = self.context.new_page()
                for extra_page in list(self.context.pages):
                    if extra_page is not self.page:
                        extra_page.close()
                self.page.evaluate("() => 1")
                return
            except Exception:
                pass
        self.close()
        self.init_browser()

    def prepare_task(self, user_id: str, url: str, cookies_str: str = "") -> None:
        """复用浏览器前准备任务页。

        - 独立 Chrome 模式：清理共享目录 Cookie，避免账号串号。
        - CDP 真机模式：新开验证标签；若传入账号 Cookie 则覆盖注入 goofish 域。
        """
        self.update_user(user_id)
        self._slide_code = None
        self._timed_out = False
        self._window_handle = None
        self._task_cookies_str = cookies_str or ""
        last_error: Optional[Exception] = None
        for attempt in range(2):
            try:
                self.ensure_browser()
                if self.use_cdp:
                    self._prepare_cdp_page()
                else:
                    self._prepare_clean_page(url)
                if self._task_cookies_str:
                    n = self._inject_account_cookies(self._task_cookies_str)
                    logger.info(f"【{self.pure_id}】已注入账号 Cookie {n} 个（供验证页会话对齐）")
                return
            except Exception as e:
                last_error = e
                if attempt == 0:
                    logger.warning(
                        f"【{self.pure_id}】浏览器状态准备失败，将重连后重试: {e}"
                    )
                self.close()
        mode = "CDP真机Chrome" if self.use_cdp else "共享浏览器"
        raise RuntimeError(f"{mode}重启后仍无法准备任务状态: {last_error}") from last_error

    def _inject_account_cookies(self, cookies_str: str) -> int:
        """把账号 Cookie 写入当前 BrowserContext（.goofish.com 等）。

        主浏览器资料：默认不注入、不覆盖你日常登录 Cookie（避免污染主资料）。
        可通过 CAPTCHA_INJECT_COOKIES_ON_MAIN=1 强制注入。
        """
        if not cookies_str or self.context is None:
            return 0
        if self.use_cdp and self._using_main_browser_profile():
            force = (os.environ.get("CAPTCHA_INJECT_COOKIES_ON_MAIN") or "").strip().lower()
            if force not in ("1", "true", "yes", "on"):
                logger.info(
                    f"【{self.pure_id}】主浏览器资料：跳过账号 Cookie 注入，沿用你 Edge/Chrome 里已有登录态"
                )
                return 0
        try:
            raw = trans_cookies(cookies_str) or {}
        except Exception as e:
            logger.warning(f"【{self.pure_id}】解析账号 Cookie 失败: {e}")
            return 0
        items = []
        for name, value in raw.items():
            if not name:
                continue
            val = str(value)
            for domain in (".goofish.com", ".taobao.com"):
                items.append(
                    {
                        "name": str(name),
                        "value": val,
                        "domain": domain,
                        "path": "/",
                    }
                )
        if not items:
            return 0
        try:
            # 不要为了注入 Cookie 先导航到闲鱼域名：items 里每条都带了显式 domain/path，
            # add_cookies 无需页面处于该域。之前那次 goto 在网络慢时会卡满 8s 超时，
            # 白白吃掉验证链接的有效期。
            self.context.add_cookies(items)
            return len(raw)
        except Exception as e:
            logger.warning(f"【{self.pure_id}】注入账号 Cookie 失败: {e}")
            return 0

    def _using_main_browser_profile(self) -> bool:
        """是否挂在用户日常 Edge/Chrome User Data（禁止清全站 Cookie）。

        资料池 edge_pool/pN 属于项目隔离资料，返回 False（允许清理）。
        """
        try:
            from common.services.captcha.profile_pool import pool_enabled
            if pool_enabled():
                return False
        except Exception:
            pass
        try:
            from common.services.captcha.chrome_cdp import get_user_data_dir
            ud = (get_user_data_dir() or "").replace("/", "\\").lower()
            if "\\edge_pool\\" in ud or "\\browser_data\\" in ud:
                return False
            if "\\microsoft\\edge\\user data" in ud or "\\google\\chrome\\user data" in ud:
                return True
            env_ud = (os.environ.get("CAPTCHA_CHROME_USER_DATA_DIR") or "").replace("/", "\\").lower()
            if env_ud and "\\browser_data\\" not in env_ud and "\\edge_pool\\" not in env_ud:
                return True
        except Exception:
            pass
        return False

    def _clear_site_state_for_captcha(self) -> None:
        """任务间轻量清理。

        - 主浏览器资料：只新开标签，不清 Cookie/全站存储（保留你日常登录态）。
        - 项目隔离资料：可清淘系状态，避免多账号串味。
        """
        if self.context is None or self.page is None:
            return
        if self.use_cdp and self._using_main_browser_profile():
            logger.info(
                f"【{self.pure_id}】主浏览器资料模式：保留 Cookie/登录态，仅使用新标签做验证"
            )
            return
        origins = (
            "https://h5api.m.goofish.com",
            "https://passport.goofish.com",
            "https://www.goofish.com",
            "https://m.goofish.com",
            "https://www.taobao.com",
            "https://login.taobao.com",
            "https://g.alicdn.com",
        )
        try:
            session = self.context.new_cdp_session(self.page)
            try:
                session.send("Network.clearBrowserCache")
            except Exception:
                pass
            for origin in origins:
                try:
                    session.send(
                        "Storage.clearDataForOrigin",
                        {"origin": origin, "storageTypes": "all"},
                    )
                except Exception:
                    pass
            try:
                session.detach()
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"【{self.pure_id}】清理站点存储失败（可忽略）: {e}")
        try:
            cookies = self.context.cookies()
            doomed = [
                c
                for c in cookies
                if any(
                    d in (c.get("domain") or "")
                    for d in (
                        "goofish.com",
                        "taobao.com",
                        "tmall.com",
                        "alicdn.com",
                        "alibaba.com",
                        "mmstat.com",
                    )
                )
            ]
            if doomed:
                self.context.clear_cookies()
                logger.info(
                    f"【{self.pure_id}】隔离配置下已清理 {len(doomed)} 条淘系/闲鱼 Cookie"
                )
        except Exception as e:
            logger.debug(f"【{self.pure_id}】清理 Cookie 失败: {e}")

    def _is_captcha_like_url(self, url: str) -> bool:
        u = (url or "").lower()
        if not u or u in ("about:blank", "about:newtab", "chrome://newtab/", "edge://newtab/"):
            return True
        keys = (
            "punish",
            "x5step",
            "captcha",
            "purecaptcha",
            "_____tmd_____",
            "login.taobao.com",
            "passport.goofish",
            "h5api.m.goofish",
        )
        return any(k in u for k in keys)

    def _safe_close_page(self, page) -> bool:
        try:
            if page is None:
                return False
            if page.is_closed():
                return False
            page.close()
            return True
        except Exception:
            return False

    def _prune_extra_pages(self, keep=None) -> int:
        """关掉多余标签，防止窗口/标签炸裂。

        - 资料池 / 项目隔离资料：最多保留 keep（验证页）+ 1 个非验证页
        - 主浏览器资料：只关 captcha/punish/空白 类标签，不动用户日常标签
        """
        if self.context is None:
            return 0
        try:
            pages = list(self.context.pages or [])
        except Exception:
            return 0
        if not pages:
            return 0

        main_profile = False
        try:
            main_profile = self._using_main_browser_profile()
        except Exception:
            main_profile = False

        closed = 0
        survivors = []
        for p in pages:
            try:
                if p is keep:
                    survivors.append(p)
                    continue
                if p.is_closed():
                    continue
                url = ""
                try:
                    url = p.url or ""
                except Exception:
                    url = ""
                captcha_like = self._is_captcha_like_url(url)
                if main_profile:
                    # 主资料：只清验证/空白页，保留用户打开的其它站点
                    if captcha_like and self._safe_close_page(p):
                        closed += 1
                    else:
                        survivors.append(p)
                else:
                    # 隔离池：验证页以外一律尽量关掉，最多再留 1 个
                    if captcha_like or keep is not None:
                        if self._safe_close_page(p):
                            closed += 1
                        else:
                            survivors.append(p)
                    else:
                        survivors.append(p)
            except Exception:
                continue

        if not main_profile and keep is not None:
            # 再压一轮：非 keep 全部关掉
            for p in list(survivors):
                if p is keep:
                    continue
                if self._safe_close_page(p):
                    closed += 1
            survivors = [keep] if keep is not None else survivors

        if not main_profile and keep is None and len(survivors) > 1:
            # 无 keep 时只留 1 个，其余关
            for p in survivors[1:]:
                if self._safe_close_page(p):
                    closed += 1

        if closed:
            logger.info(f"【{self.pure_id}】已清理多余浏览器标签 {closed} 个")
        return closed

    def _prepare_cdp_page(self) -> None:
        """CDP：优先复用单一验证标签；清理堆积；落 blank 后清站点状态。"""
        keep = None
        if self.page is not None and self._owned_captcha_page:
            try:
                if not self.page.is_closed():
                    keep = self.page
            except Exception:
                keep = None

        self._prune_extra_pages(keep=keep)

        if keep is None:
            # 尝试认领现有空白/验证页，避免无限 new_page
            try:
                for p in list(self.context.pages or []):
                    try:
                        if p.is_closed():
                            continue
                        if self._is_captcha_like_url(p.url or ""):
                            keep = p
                            break
                    except Exception:
                        continue
            except Exception:
                keep = None

        if keep is None:
            keep = self.context.new_page()
            logger.info(f"【{self.pure_id}】CDP 新建验证标签")
        else:
            logger.info(f"【{self.pure_id}】CDP 复用验证标签")

        self.page = keep
        self._owned_captcha_page = True
        # 校准脚本只挂在验证页
        try:
            self.page.add_init_script(_CAP_JS)
        except Exception:
            pass
        try:
            self.page.bring_to_front()
        except Exception:
            pass
        self._attach_lifecycle_listeners()
        self._apply_default_timeouts()
        try:
            self.page.goto("about:blank", wait_until="domcontentloaded", timeout=4000)
        except Exception:
            pass
        # 再 prune 一次，确保 new_page 后无残留
        self._prune_extra_pages(keep=self.page)
        self._clear_site_state_for_captcha()
        self._browser_broken_reason = None
        logger.info(
            f"【{self.pure_id}】CDP 验证标签就绪，当前标签数="
            f"{len(list(self.context.pages or []))}"
        )

    def _prepare_clean_page(self, url: str) -> None:
        """在当前共享 Context 中创建唯一干净页面，并确认无历史 Cookie。"""
        new_page = self.context.new_page()
        for old_page in list(self.context.pages):
            if old_page is not new_page:
                old_page.close()
        self.page = new_page
        self._owned_captcha_page = True
        self._attach_lifecycle_listeners()
        self._apply_default_timeouts()
        # 先关闭旧页面，避免尾部响应在首次清理后重新写入 Cookie。
        self.context.clear_cookies()
        remaining = self.context.cookies()
        if remaining:
            raise RuntimeError(f"关闭旧页面后仍残留 {len(remaining)} 个 Cookie")
        self._clear_browser_storage(url)
        # 存储清理后再次清 Cookie 并校验，任何残留都触发浏览器重启。
        self.context.clear_cookies()
        remaining = self.context.cookies()
        if remaining:
            raise RuntimeError(f"二次清理后仍残留 {len(remaining)} 个 Cookie")
        if len(self.context.pages) != 1:
            raise RuntimeError(f"共享浏览器页面数量异常: {len(self.context.pages)}")
        self.page.bring_to_front()

    def _clear_browser_storage(self, url: str) -> None:
        """清理缓存及闲鱼相关 Origin 存储，避免固定 Context 残留上一次任务状态。"""
        origins = {
            "https://h5api.m.goofish.com",
            "https://passport.goofish.com",
            "https://www.goofish.com",
            "https://m.goofish.com",
        }
        parsed = urlsplit(url or "")
        if parsed.scheme and parsed.netloc:
            origins.add(f"{parsed.scheme}://{parsed.netloc}")
        try:
            session = self.context.new_cdp_session(self.page)
            session.send("Network.clearBrowserCache")
            for origin in origins:
                session.send(
                    "Storage.clearDataForOrigin",
                    {"origin": origin, "storageTypes": "all"},
                )
        except Exception as e:
            raise RuntimeError(f"清理共享浏览器站点存储失败: {e}") from e

    def close(self) -> None:
        if self.use_cdp:
            # CDP：只关验证标签并断开 Playwright，绝不关闭用户 Chrome 进程
            try:
                if self.page is not None and self._owned_captcha_page:
                    if not self.page.is_closed():
                        self.page.close()
            except Exception:
                pass
            self.page = None
            self._owned_captcha_page = False
            self.context = None
            self.browser = None
            try:
                if self.pw is not None:
                    self.pw.stop()
            except Exception:
                pass
            self.pw = None
            return

        for fn in (
            lambda: self.page and self.page.close(),
            lambda: self.context and self.context.close(),
            lambda: self.pw and self.pw.stop(),
        ):
            try:
                fn()
            except Exception:
                pass
        self.page = None
        self.context = None
        self.browser = None
        self.pw = None
        self._owned_captcha_page = False
        self._release_browser_lock()

    def force_kill(self) -> None:
        """看门狗超时回调。

        - 独立 Chrome 模式：按固定目录精确强杀对应 Chrome（不误伤用户日常 Chrome）。
        - CDP 真机模式：仅关闭验证标签并断开连接，绝不杀用户 Chrome。
        """
        self._timed_out = True
        self._mark_browser_broken("timeout")
        _append_browser_crash_log({
            "event": "force_kill",
            "user_id": self.user_id,
            "pure_id": self.pure_id,
            "use_cdp": self.use_cdp,
            "reason": "timeout",
            "page_url": getattr(self.page, "url", "") if self.page is not None else "",
        })
        if self.use_cdp:
            try:
                if self.page is not None and self._owned_captcha_page:
                    if not self.page.is_closed():
                        self.page.close()
            except Exception:
                pass
            self.page = None
            logger.warning(f"【{self.pure_id}】CDP 真机模式超时，已关闭验证标签（未杀 Chrome）")
            return
        self._kill_browser_processes(log_result=True)

    def _kill_browser_processes(self, log_result: bool) -> None:
        """按固定目录清理真人鼠标 Chrome 主进程和子进程。"""
        if sys.platform != "win32":
            return
        try:
            browser_dir = self.browser_dir
            ps = (
                "Get-CimInstance Win32_Process | "
                f"Where-Object {{ $_.Name -eq 'chrome.exe' -and $_.CommandLine -like '*{browser_dir}*' }} | "
                "ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {} }"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, timeout=15,
            )
            if log_result:
                logger.warning(f"【{self.pure_id}】真实鼠标引擎超时，已强杀共享浏览器进程")
        except Exception as e:
            if log_result:
                logger.warning(f"【{self.pure_id}】真实鼠标引擎强杀共享浏览器失败（可忽略）: {e}")

    # ---------- 工具 ----------
    def _cookies(self) -> Dict[str, str]:
        try:
            return {c["name"]: c["value"] for c in self.context.cookies()}
        except Exception:
            return {}

    def _x5sec(self) -> str:
        return self._cookies().get("x5sec", "")

    def _in_punish(self) -> bool:
        try:
            u = self.page.url or ""
        except Exception:
            u = ""
        return any(k in u for k in _PUNISH)

    def _find_slider(self):
        for frame in self.page.frames:
            try:
                btn = frame.query_selector("#nc_1_n1z")
                if btn and btn.is_visible():
                    track = frame.query_selector("#nc_1_n1t") or frame.query_selector(".nc_scale")
                    if track:
                        return frame, btn, track
            except Exception:
                continue
        return None, None, None

    def _x5_cookies(self) -> Dict[str, str]:
        """提取 x5* 相关 cookie（成功后返回给上层）。"""
        out: Dict[str, str] = {}
        for name, value in self._cookies().items():
            low = name.lower()
            if low.startswith("x5") or "x5sec" in low:
                out[name] = value
        return out

    # ---------- 核心 ----------
    def _maximize_window(self) -> None:
        """通过 CDP 强制最大化窗口（登录滑块必须最大化才能用长位移轨迹通过）。"""
        try:
            session = self.context.new_cdp_session(self.page)
            win = session.send("Browser.getWindowForTarget")
            session.send(
                "Browser.setWindowBounds",
                {"windowId": win["windowId"], "bounds": {"windowState": "maximized"}},
            )
        except Exception as e:
            logger.warning(f"【{self.pure_id}】强制最大化窗口失败（继续）: {e}")

    def _ensure_window_foreground(self, scene: str) -> bool:
        """激活当前验证 Chrome，并校验物理输入的真实前台归属。"""
        scene_name = "登录滑块" if scene == "login" else "业务滑块"
        try:
            # 快路径：窗口已在前台就什么都不做。
            # 本函数一次任务会被调用 6~9 次，而下面的最大化 / bring_to_front /
            # activate_window 每次都会重排窗口状态，对已经在前台的窗口就是纯闪烁。
            if self._window_handle and is_foreground_window(self._window_handle):
                return True
            # CDP/独立 Chrome 都先最大化，减少窗口被挡住
            try:
                self._maximize_window()
            except Exception:
                pass
            try:
                self.page.bring_to_front()
            except Exception:
                pass
            if self._window_handle:
                success, detail = activate_window(self._window_handle)
                if success:
                    return True
            success, hwnd, detail = activate_page_window(self.page, timeout_seconds=4.0)
            if success and hwnd:
                first_detection = self._window_handle is None
                self._window_handle = hwnd
                if first_detection:
                    logger.info(
                        f"【{self.pure_id}】{scene_name}已锁定 Windows 前台窗口: {detail}"
                    )
                return True
            # CDP 真机模式：若已同会话找到窗口但 SetForeground 被系统拒绝，
            # 仍尝试滑动（用户手动点一下 Chrome 时也能提高成功率）。
            if self.use_cdp and hwnd:
                self._window_handle = hwnd
                logger.warning(
                    f"【{self.pure_id}】{scene_name}前台激活未完全成功但仍继续滑动: {detail}"
                )
                return True
            logger.error(
                f"【{self.pure_id}】{scene_name}无法激活 Windows 前台窗口，"
                f"已取消物理鼠标回放: {detail}"
            )
        except Exception as e:
            logger.error(
                f"【{self.pure_id}】{scene_name} Windows 前台校验异常，"
                f"已取消物理鼠标回放: {e}"
            )
        return False

    def solve(
        self,
        url: str,
        drags: List[List[Tuple[float, float, float]]],
        browser_timeout: int,
        url_provider: Optional[Callable[[], Optional[str]]],
        scene: str = "business",
    ) -> Tuple[bool, Optional[Dict[str, str]]]:
        start = time.time()
        self.ensure_browser()
        if not self._browser_is_healthy():
            return False, None
        # 登录场景强制最大化（业务场景保持原有窗口行为不变）
        if scene == "login":
            self._maximize_window()
            self._ensure_window_foreground(scene)

        # 导航：DOM 就绪后给 iframe 一点渲染时间，再找滑块
        target = url
        for attempt in range(2):
            try:
                self.page.goto(target, wait_until="domcontentloaded", timeout=12000)
            except Exception as e:
                logger.warning(f"【{self.pure_id}】真实鼠标引擎导航异常（继续）: {e}")
            # 验证码 iframe 常晚于 DOM；过短会导致坐标/校准飘
            time.sleep(random.uniform(0.45, 0.75))
            if scene == "login":
                self._maximize_window()
                self._ensure_window_foreground(scene)
            try:
                content = self.page.content()
            except Exception:
                content = ""
            if "抱歉，页面访问出现了问题" in content:
                if url_provider and attempt == 0:
                    try:
                        fresh = url_provider()
                    except Exception:
                        fresh = None
                    if fresh == CAPTCHA_NOT_REQUIRED:
                        logger.info(f"【{self.pure_id}】重取链接时检测到 token 已可用，无需滑块，提前结束")
                        return True, None
                    if fresh and isinstance(fresh, str):
                        target = fresh
                        logger.info(f"【{self.pure_id}】真实鼠标引擎使用刷新后的验证链接重试")
                        continue
                return False, URL_EXPIRED
            break

        if not self._browser_is_healthy():
            return False, None
        self._ensure_window_foreground(scene)

        pre_x5 = self._x5sec()
        # 3 次同页重试：配合 _take_drag 每次换一条不同样本，且 code=300 后有退避
        max_attempts = 3
        remaining_drags = list(drags) if scene == "business" else []
        for attempt in range(1, max_attempts + 1):
            if not self._browser_is_healthy():
                break
            if time.time() - start > browser_timeout:
                break

            frame = btn = track = None
            poll = 0.15
            # 首次尝试给足首屏渲染时间：goto 超时后页面仍在后台加载，
            # 只等 4.5s 就判定「未找到滑块」会白白烧掉一次重试（实测浪费 12s）。
            poll_rounds = 80 if attempt == 1 else 30
            for _ in range(poll_rounds):
                if time.time() - start > browser_timeout:
                    break
                frame, btn, track = self._find_slider()
                if btn and track:
                    break
                time.sleep(poll)
            if not btn or not track:
                if not self._in_punish() and self._x5sec() and self._x5sec() != pre_x5:
                    cookies = self._collect_success()
                    if cookies:
                        return True, cookies
                if attempt < max_attempts:
                    logger.info(f"【{self.pure_id}】未找到滑块，点重试后等待渲染…")
                    self._click_retry_human(quick=False)
                    time.sleep(random.uniform(0.6, 1.0))
                    continue
                logger.warning(f"【{self.pure_id}】真实鼠标引擎未找到滑块（第{attempt}次尝试）")
                break

            # 找到滑块后稍停，再激活窗口（避免 iframe 位置未稳定就校准）
            time.sleep(random.uniform(0.18, 0.35))
            if scene == "login":
                self._maximize_window()
            if not self._ensure_window_foreground(scene):
                return False, None

            # 计算坐标 + 物理鼠标回放真人轨迹（每次随机挑一条轨迹，降低重复模式风险）
            if scene == "business":
                if not remaining_drags:
                    remaining_drags = list(drags)
                selected_drag = _take_drag(remaining_drags)
            else:
                # Keep the existing login retry selection behavior unchanged.
                selected_drag = _choose_drag(drags)
            if scene == "login":
                logger.info(
                    f"【{self.pure_id}】登录滑块回放真人原始样本: "
                    f"点数={len(selected_drag) - 1}, "
                    f"位移={selected_drag[-1][0]:.0f}px, "
                    f"按下至末点={sum(point[2] for point in selected_drag):.0f}ms, "
                    f"首点等待={selected_drag[1][2]:.0f}ms"
                )
            else:
                move_duration_ms = sum(point[2] for point in selected_drag)
                press_delay_ms = getattr(selected_drag, "press_delay_ms", 0.0)
                release_delay_ms = getattr(selected_drag, "release_delay_ms", 0.0)
                approach = getattr(selected_drag, "approach", [])
                source_file = getattr(selected_drag, "source_file", "") or "unknown"
                logger.info(
                    f"【{self.pure_id}】业务滑块第{attempt}次选用真人原始轨迹: "
                    f"样本={source_file}, "
                    f"接近点={len(approach)}, 拖动点={len(selected_drag)}, "
                    f"位移={selected_drag[-1][0]:.0f}px, "
                    f"移动={move_duration_ms:.0f}ms, "
                    f"按下等待={press_delay_ms:.0f}ms, "
                    f"松手等待={release_delay_ms:.0f}ms, "
                    f"总按住={press_delay_ms + move_duration_ms + release_delay_ms:.0f}ms"
                )
            if not self._do_real_slide(
                frame,
                btn,
                track,
                drag=selected_drag,
                scene=scene,
            ):
                if attempt < max_attempts:
                    self._click_retry_human(quick=False)
                    time.sleep(random.uniform(0.7, 1.2))
                    continue
                break

            res = self._wait_result(pre_x5, start, browser_timeout)
            drag_source = getattr(selected_drag, "source_file", "")
            if res is True:
                cookies = self._collect_success()
                if scene == "login" and cookies:
                    logger.info(f"【{self.pure_id}】登录滑块第{attempt}次回放通过")
                if cookies:
                    _note_slide_result(True)
                    trail_stats.record(drag_source, True)
                    return True, cookies
                _note_slide_result(False, rejected_300=False)
                return False, None

            if res is False:
                _note_slide_result(False, rejected_300=True)
                trail_stats.record(drag_source, False)
                risk = _risk_level()
                logger.warning(
                    f"【{self.pure_id}】code=300 风险等级={risk} consecutive_rejects 已累加"
                )
                # 300 后立刻再滑几乎必 300：拉长间隔，最多再试 1 次
                if attempt < max_attempts and (time.time() - start) < (browser_timeout - 5):
                    pause = random.uniform(1.8, 3.2) if risk < 2 else random.uniform(2.5, 4.5)
                    logger.info(
                        f"【{self.pure_id}】第{attempt}次 code=300，等待 {pause:.1f}s 后同页重试"
                    )
                    time.sleep(pause)
                    self._click_retry_human(quick=False)
                    time.sleep(random.uniform(0.5, 0.9))
                    continue
                break

            # 不明确失败：短等后点重试
            if attempt < max_attempts and (time.time() - start) < (browser_timeout - 5):
                logger.info(
                    f"【{self.pure_id}】第{attempt}次结果不明，同页点重试"
                )
                self._click_retry_human(quick=False)
                time.sleep(random.uniform(0.5, 0.9))
                continue
            break
        return False, None

    def _do_real_slide(
        self,
        frame,
        btn,
        track,
        drag: List[Tuple[float, float, float]],
        scene: str = "business",
    ) -> bool:
        """对当前滑块做一次：坐标校准 + 物理鼠标接近/按下/回放真人轨迹/松手。返回是否完成滑动。"""
        box = btn.bounding_box()
        if not box:
            return False
        mx = box["x"] + box["width"] / 2
        my = box["y"] + box["height"] / 2
        replay_drag = drag
        if scene == "business":
            distance = compute_slider_distance(frame, btn, track)
            if distance <= 0:
                logger.error(f"【{self.pure_id}】业务滑块无法计算当前滑轨距离")
                return False
            replay_drag = _scale_drag_to_distance(drag, distance)
            overshoot = replay_drag[-1][0] - distance
            logger.info(
                f"【{self.pure_id}】业务滑块按采集滑轨基准映射真人轨迹: "
                f"采集末点={drag[-1][0]:.1f}px, 当前到底={distance:.1f}px, "
                f"末点=({replay_drag[-1][0]:.1f},{replay_drag[-1][1]:.1f})px, "
                f"到底后继续={overshoot:.1f}px, "
                f"点数={len(replay_drag)}"
            )
        else:
            track_box = track.bounding_box() if track else None
            if track_box:
                candidate_x = track_box["x"] + track_box["width"] - 1 - drag[-1][0]
                if box["x"] <= candidate_x <= box["x"] + box["width"]:
                    mx = candidate_x
        if scene == "business":
            mapper, geometry = build_geometry_mapper(self.page)
            logger.info(
                f"【{self.pure_id}】业务滑块使用被动窗口几何映射: "
                f"dpr={geometry.get('devicePixelRatio')}, "
                f"窗口=({geometry.get('screenX')},{geometry.get('screenY')}), "
                f"视口={geometry.get('innerWidth')}x{geometry.get('innerHeight')}"
            )
            to_screen = mapper.to_screen
        else:
            # 登录滑块保持原 CDP 校准逻辑，不改变 login 的滑动行为。
            dpr = self.page.evaluate("() => window.devicePixelRatio") or 1.0
            try:
                frame.evaluate("() => { window.__cal = []; }")
            except Exception:
                pass
            self.page.mouse.move(mx, my, steps=3)
            time.sleep(0.2)
            cal = []
            try:
                cal = frame.evaluate("() => window.__cal || []") or self.page.evaluate("() => window.__cal || []")
            except Exception:
                pass
            if not cal:
                logger.warning(f"【{self.pure_id}】真实鼠标引擎坐标校准失败")
                return False
            c = cal[-1]
            off_x, off_y = c[2] - mx, c[3] - my

            def to_screen(vx: float, vy: float) -> Tuple[int, int]:
                return int(round((vx + off_x) * dpr)), int(round((vy + off_y) * dpr))

        self._slide_code = None  # 每次滑动前重置，避免读到上一次的返回码

        # 坐标校准后再次校验，避免校准期间被其他程序抢走 Windows 前台窗口。
        if not self._ensure_window_foreground(scene):
            return False

        sx, sy = to_screen(mx, my)
        if scene == "login":
            logger.info(f"【{self.pure_id}】登录滑块使用业务同款 pyautogui 回放: 起点=({sx},{sy})")
        if scene == "business":
            # 接近：保留真人晃动+绕行；拖动段严格按轨迹时长（该慢就慢）
            risk = _risk_level()
            timer_resolution(True)
            try:
                self._human_idle_wander(
                    anchor_sx=sx,
                    anchor_sy=sy,
                    steps=random.randint(2, 3) if risk == 0 else random.randint(3, 5),
                )
                via_x = sx + random.randint(-70, -25)
                via_y = sy + random.randint(-25, 35)
                self._smooth_move_screen(via_x, via_y, dur=random.uniform(0.10, 0.22))
                self._smooth_move_screen(
                    sx + random.randint(-6, 6),
                    sy + random.randint(-4, 4),
                    dur=random.uniform(0.06, 0.14),
                )
                self._smooth_move_screen(sx, sy, dur=random.uniform(0.05, 0.10))
                time.sleep(random.uniform(0.05, 0.12))
                send_button(True)
                started = time.perf_counter()
                press_delay = getattr(replay_drag, "press_delay_ms", 0.0) / 1000.0
                release_delay = getattr(replay_drag, "release_delay_ms", 0.0) / 1000.0
                # 保证最短按住/移动节奏，防止过短轨迹被标 300
                press_delay = max(0.06, press_delay)
                release_delay = max(0.05, release_delay)
                precise_sleep(started + press_delay)
                move_started = started + press_delay
                elapsed = 0.0
                for dx, dy, dt in replay_drag:
                    elapsed += dt / 1000.0
                    if dt >= 3.0:
                        precise_sleep(move_started + elapsed)
                    tx, ty = to_screen(mx + dx, my + dy)
                    send_move_abs(tx, ty)
                precise_sleep(move_started + elapsed + release_delay)
            finally:
                send_button(False)
                timer_resolution(False)
        else:
            # 登录滑块保持原有 pyautogui 轨迹、坐标抖动和逐点时序，不受业务优化影响。
            ax, ay = to_screen(mx - 50, my - 40)
            _human_mouse_to(ax, ay, 0.3)
            _human_mouse_to(sx, sy, 0.2)
            time.sleep(0.15)
            pyautogui.mouseDown()
            time.sleep(0.12)
            for i, (dx, dy, dt) in enumerate(drag):
                if i == 0:
                    continue
                tx, ty = to_screen(
                    mx + dx + random.uniform(-1, 1),
                    my + dy + random.uniform(-1, 1),
                )
                pyautogui.moveTo(tx, ty)
                time.sleep(max(0.0, (dt / 1000.0) * random.uniform(0.85, 1.15)))
            time.sleep(0.08)
            pyautogui.mouseUp()
        if scene == "login":
            try:
                observed = frame.evaluate("() => window.__cal || []") or []
                pressed = [event for event in observed if len(event) >= 6 and event[5] == 1]
                pressed_duration = (
                    pressed[-1][4] - pressed[0][4] if len(pressed) >= 2 else 0
                )
                actual_start = pressed[0][2:4] if pressed else []
                actual_end = pressed[-1][2:4] if pressed else []
                actual_distance = (
                    pressed[-1][2] - pressed[0][2] if len(pressed) >= 2 else 0
                )
                logger.info(
                    f"【{self.pure_id}】登录滑块页面接收鼠标移动事件: "
                    f"总计={len(observed)}个, 按下={len(pressed)}个, "
                    f"按下时长={pressed_duration:.0f}ms, 起点={mx:.0f}, "
                    f"目标={mx + drag[-1][0]:.0f}, 实际首点={actual_start}, "
                    f"实际末点={actual_end}, 实际位移={actual_distance:.0f}px"
                )
            except Exception:
                # 成功后页面可能立即跳转并销毁 frame，无需作为异常处理。
                pass
        return True

    def _wait_result(self, pre_x5: str, start: float, browser_timeout: int) -> Optional[bool]:
        """等待并判定本次滑动结果：True=通过 / False=明确失败(code 300) / None=不明确（可重试）。"""
        if not self._browser_is_healthy():
            return None

        def _nc_ok() -> bool:
            for fr in self.page.frames:
                try:
                    if fr.evaluate("()=>!!(document.querySelector('.nc_ok_icon')||document.querySelector('.btn_ok'))"):
                        return True
                except Exception:
                    pass
            return False

        # code 一到就返回；给 set-cookie / 页面跳转留足时间
        poll = 0.12
        deadline = min(8.0, max(3.0, browser_timeout - (time.time() - start)))
        waited = 0.0
        while waited < deadline:
            if not self._browser_is_healthy():
                return None
            if self._slide_code == 300:
                logger.warning(f"【{self.pure_id}】滑块接口返回 code=300（风控拒绝）")
                return False
            if self._slide_code == 0:
                logger.info(f"【{self.pure_id}】滑块接口返回 code=0（通过）")
                return True
            if self._x5sec() and self._x5sec() != pre_x5:
                logger.info(f"【{self.pure_id}】检测到 x5sec 更新，判定通过")
                return True
            if not self._in_punish():
                logger.info(f"【{self.pure_id}】已离开 punish 页，判定通过")
                return True
            if _nc_ok():
                logger.info(f"【{self.pure_id}】页面出现成功图标，判定通过")
                return True
            time.sleep(poll)
            waited += poll
        logger.warning(
            f"【{self.pure_id}】滑块结果等待超时 slide_code={self._slide_code} "
            f"x5sec_changed={bool(self._x5sec() and self._x5sec() != pre_x5)}"
        )
        return None

    def _click_retry(self) -> None:
        """兼容旧调用：改为真人物理点击。"""
        self._click_retry_human()

    def _viewport_to_screen_rough(self, vx: float, vy: float) -> Tuple[int, int]:
        """用窗口几何粗算视口 CSS → 屏幕坐标（用于重试点/闲逛）。"""
        try:
            mapper, _ = build_geometry_mapper(self.page)
            return mapper.to_screen(vx, vy)
        except Exception:
            return int(vx), int(vy)

    def _smooth_move_screen(self, tx: int, ty: int, dur: float = 0.2) -> None:
        """物理光标贝塞尔接近目标屏幕点。"""
        try:
            x0, y0 = pyautogui.position()
        except Exception:
            send_move_abs(tx, ty)
            return
        cx = x0 + (tx - x0) * random.uniform(0.25, 0.55) + random.uniform(-40, 40)
        cy = y0 + (ty - y0) * random.uniform(0.25, 0.55) + random.uniform(-30, 30)
        n = max(8, int(dur / 0.012))
        for i in range(1, n + 1):
            t = i / n
            mt = 1 - t
            x = mt * mt * x0 + 2 * mt * t * cx + t * t * tx
            y = mt * mt * y0 + 2 * mt * t * cy + t * t * ty
            send_move_abs(int(x), int(y))
            time.sleep(dur / n * random.uniform(0.55, 1.45))

    def _human_idle_wander(
        self,
        anchor_sx: Optional[int] = None,
        anchor_sy: Optional[int] = None,
        steps: Optional[int] = None,
    ) -> None:
        """在锚点附近随机晃动鼠标，模拟真人犹豫/浏览。"""
        try:
            if anchor_sx is None or anchor_sy is None:
                # 以页面中部为锚
                geom = self.page.evaluate(
                    """() => ({
                      w: window.innerWidth, h: window.innerHeight,
                      sx: window.screenX, sy: window.screenY,
                      ow: window.outerWidth, oh: window.outerHeight
                    })"""
                )
                border = max(0, (geom["ow"] - geom["w"]) / 2)
                top = max(0, (geom["oh"] - geom["h"]) - border)
                ax = int(geom["sx"] + border + geom["w"] * random.uniform(0.35, 0.65))
                ay = int(geom["sy"] + top + geom["h"] * random.uniform(0.35, 0.7))
            else:
                ax, ay = int(anchor_sx), int(anchor_sy)
        except Exception:
            try:
                ax, ay = pyautogui.position()
            except Exception:
                return
        n = steps if steps is not None else random.randint(3, 7)
        for _ in range(n):
            nx = ax + random.randint(-110, 130)
            ny = ay + random.randint(-70, 80)
            self._smooth_move_screen(nx, ny, dur=random.uniform(0.08, 0.22))
            if random.random() < 0.35:
                time.sleep(random.uniform(0.05, 0.18))

    def _click_retry_human(self, quick: bool = False) -> None:
        """失败后用物理鼠标点击「点击框体重试」区域（同页重置，不刷新）。

        quick=True：几乎不闲逛，点完立刻返回（用于失败马上再滑）。
        """
        selectors = (
            "#nc_1_refresh1",
            ".nc_iconfont.btn_refresh",
            ".errloading",
            ".nc-lang-cnt",
            "[class*='errloading']",
            "[class*='refresh']",
            "#nc_1_n1t",
            ".nc_scale",
            ".nc-container",
            "#nc_1__scale_text",
        )
        target_box = None
        target_sel = ""
        for fr in self.page.frames:
            for sel in selectors:
                try:
                    el = fr.query_selector(sel)
                    if not el or not el.is_visible():
                        continue
                    box = el.bounding_box()
                    if not box or box["width"] < 8 or box["height"] < 8:
                        continue
                    target_box = box
                    target_sel = sel
                    break
                except Exception:
                    continue
            if target_box:
                break

        if not target_box:
            logger.warning(f"【{self.pure_id}】未定位到重试控件，尝试页面中部物理点击")
            try:
                w = self.page.evaluate("() => window.innerWidth") or 800
                h = self.page.evaluate("() => window.innerHeight") or 600
                sx, sy = self._viewport_to_screen_rough(w * 0.5, h * 0.55)
            except Exception:
                return
        else:
            vx = target_box["x"] + target_box["width"] * random.uniform(0.35, 0.65)
            vy = target_box["y"] + target_box["height"] * random.uniform(0.35, 0.65)
            sx, sy = self._viewport_to_screen_rough(vx, vy)
            logger.info(f"【{self.pure_id}】同页物理点击重试控件: {target_sel} screen=({sx},{sy})")

        self._ensure_window_foreground("business")
        if not quick:
            self._human_idle_wander(anchor_sx=sx, anchor_sy=sy, steps=1)
        self._smooth_move_screen(
            sx + random.randint(-3, 3),
            sy + random.randint(-2, 2),
            dur=random.uniform(0.04, 0.08) if quick else random.uniform(0.08, 0.14),
        )
        time.sleep(0.02 if quick else random.uniform(0.04, 0.08))
        send_button(True)
        time.sleep(0.03 if quick else random.uniform(0.03, 0.07))
        send_button(False)
        time.sleep(0.08 if quick else random.uniform(0.2, 0.4))

    def _collect_success(self) -> Optional[Dict[str, str]]:
        """成功后等待 set-cookie 落盘并返回 x5* cookies。"""
        time.sleep(0.45)
        x5 = self._x5_cookies()
        if "x5sec" not in x5:
            time.sleep(0.5)
            x5 = self._x5_cookies()
        if "x5sec" not in x5:
            logger.warning(f"【{self.pure_id}】真实鼠标引擎视觉通过但未获取到 x5sec")
            return None
        return x5


_shared_solver: Optional[_RealMouseSolver] = None
_real_mouse_executor: Optional[ThreadPoolExecutor] = None
_real_mouse_executor_lock = threading.Lock()


def _get_real_mouse_executor() -> ThreadPoolExecutor:
    """返回真人鼠标专用单线程执行器，保证 Playwright Sync 对象始终在同一线程使用。"""
    global _real_mouse_executor
    if _real_mouse_executor is None:
        with _real_mouse_executor_lock:
            if _real_mouse_executor is None:
                _real_mouse_executor = ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix="real-mouse",
                )
    return _real_mouse_executor


def _get_shared_solver(user_id: str, use_cdp: bool = False) -> _RealMouseSolver:
    """获取真人鼠标进程级共享浏览器实例（CDP / 独立目录互斥复用）。"""
    global _shared_solver
    if _shared_solver is None:
        _shared_solver = _RealMouseSolver(user_id, use_cdp=use_cdp)
    elif bool(_shared_solver.use_cdp) != bool(use_cdp):
        # 滑动方式从 real_mouse <-> chrome_cdp 切换时，必须重建实例
        try:
            _shared_solver.close()
        except Exception:
            pass
        _shared_solver = _RealMouseSolver(user_id, use_cdp=use_cdp)
    else:
        _shared_solver.update_user(user_id)
    return _shared_solver


def _close_shared_solver_in_worker() -> None:
    """在真人鼠标专用线程中关闭共享浏览器。"""
    global _shared_solver
    if _shared_solver is None:
        return
    try:
        _shared_solver.close()
    except Exception:
        pass
    _shared_solver = None


def _shutdown_real_mouse_executor() -> None:
    """服务退出时在 Playwright 所属线程关闭浏览器，再停止专用执行器。"""
    global _real_mouse_executor
    executor = _real_mouse_executor
    if executor is None:
        return
    try:
        executor.submit(_close_shared_solver_in_worker).result(timeout=15)
    except Exception:
        pass
    try:
        executor.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass
    _real_mouse_executor = None


try:
    # ThreadPoolExecutor 会在线程级退出阶段先于普通 atexit 关闭；这里后注册、先执行，
    # 确保 Playwright 仍可在所属 real-mouse 线程中正常 close，避免 Node 管道 EPIPE。
    threading._register_atexit(_shutdown_real_mouse_executor)
except AttributeError:
    atexit.register(_shutdown_real_mouse_executor)


def _execute_shared_verification(
    user_id: str,
    url: str,
    drags: List[List[Tuple[float, float, float]]],
    browser_timeout: int,
    url_provider: Optional[Callable[[], Optional[str]]],
    scene: str,
    use_cdp: bool = False,
    cookies_str: str = "",
) -> Tuple[bool, Optional[Dict[str, str]]]:
    """在真人鼠标专用线程内完成浏览器准备、滑动和结果收集。"""
    solver = _get_shared_solver(user_id, use_cdp=use_cdp)
    budget = max(browser_timeout, 40) + 20
    watchdog = threading.Timer(budget, solver.force_kill)
    watchdog.daemon = True
    watchdog.start()
    try:
        solver.prepare_task(user_id, url, cookies_str=cookies_str)
        # 保留 1 条合成轨迹作为真人样本全部被封时的兜底路径。
        # 它在 _choose_drag 里被额外降权（实测通过率 7.1% vs 真人 20~54%），
        # 只有当真人样本的实测通过率跌到比它还低时，统计权重才会让它顶上来。
        active_drags = list(drags)
        if scene == "business":
            try:
                active_drags = active_drags + [_synthetic_business_drag(260.0)]
            except Exception:
                pass
        result = solver.solve(
            url, active_drags, browser_timeout, url_provider, scene=scene
        )
        if solver._browser_broken_reason or solver._timed_out:
            logger.warning(
                f"【{user_id}】检测到浏览器崩溃/超时，重启后再试一次: "
                f"reason={solver._browser_broken_reason or 'timeout'}"
            )
            try:
                solver.close()
            except Exception:
                pass
            try:
                solver.prepare_task(user_id, url, cookies_str=cookies_str)
                retry_drags = list(drags)
                if scene == "business":
                    try:
                        retry_drags = retry_drags + [_synthetic_business_drag(260.0)]
                    except Exception:
                        pass
                result = solver.solve(
                    url, retry_drags, browser_timeout, url_provider, scene=scene
                )
            except Exception as retry_exc:
                _append_browser_crash_log({
                    "event": "retry_failed",
                    "user_id": user_id,
                    "use_cdp": use_cdp,
                    "scene": scene,
                    "reason": str(retry_exc),
                })
                logger.warning(f"【{user_id}】浏览器重启后重试失败: {retry_exc}")
        # CDP：任务结束归还 blank + 清多余标签（不关浏览器进程，保留单标签复用）
        if use_cdp:
            try:
                if solver.page is not None and solver._owned_captcha_page:
                    try:
                        if not solver.page.is_closed():
                            solver.page.goto(
                                "about:blank",
                                wait_until="domcontentloaded",
                                timeout=3000,
                            )
                    except Exception:
                        try:
                            if not solver.page.is_closed():
                                solver.page.close()
                        except Exception:
                            pass
                        solver.page = None
                        solver._owned_captcha_page = False
                solver._prune_extra_pages(
                    keep=solver.page if solver._owned_captcha_page else None
                )
            except Exception:
                pass
        return result
    finally:
        watchdog.cancel()


def _switch_browser_profile_and_reconnect(user_id: str, reason: str) -> None:
    """失败后彻底换一套资料：关 CDP 浏览器 + 销毁共享 solver + 用新资料重开。"""
    global _shared_solver
    try:
        from common.services.captcha.profile_pool import pool_enabled, rotate_profile, describe
        from common.services.captcha.chrome_cdp import kill_cdp_chrome, ensure_cdp_chrome
    except Exception as e:
        logger.warning(f"【{user_id}】资料轮换模块不可用: {e}")
        return
    if not pool_enabled():
        logger.info(f"【{user_id}】资料池未启用，跳过轮换")
        return

    idx, path = rotate_profile(reason=reason)
    logger.warning(f"【{user_id}】失败后轮换浏览器资料 -> p{idx} {path} | {describe()}")
    _reset_risk()

    # 关闭 Playwright 侧
    try:
        if _shared_solver is not None:
            try:
                _shared_solver.close()
            except Exception:
                pass
            _shared_solver = None
    except Exception:
        pass

    # 杀掉当前 CDP 浏览器进程
    try:
        kill_cdp_chrome()
    except Exception as e:
        logger.warning(f"【{user_id}】结束旧资料浏览器失败: {e}")
    time.sleep(1.0)

    ok, msg = ensure_cdp_chrome(force_clean=True)
    logger.info(f"【{user_id}】新资料浏览器启动: ok={ok} {msg}")


def run_real_mouse_verification(
    user_id: str,
    url: str,
    existing_cookies_str: str = "",
    browser_timeout: int = 60,
    url_provider: Optional[Callable[[], Optional[str]]] = None,
    weight_class: str = "local",
    use_cdp: bool = False,
) -> Tuple[bool, Optional[Dict[str, str]]]:
    """真实鼠标滑块验证入口（串行执行，物理光标唯一）。

    若启用资料池：首次失败（含 code=300）后彻底换一套浏览器资料再重试 1 次。

    Returns:
        (是否成功, x5* cookies 字典 | None)
    """
    if not REAL_MOUSE_AVAILABLE:
        return False, None

    # 前置检查：没有可用显示器就别滑。
    # 本机显示器由远程工具虚拟挂载，远程一断开显示器即被摘掉，此时 SendInput 的
    # 归一化基准、浏览器窗口几何、Chromium 合成/定时器全部失真，滑动必然 code=300。
    # 硬滑的代价不只是失败：会抬高 _risk_level、并把「环境问题」记成轨迹样本的失败，
    # 污染 trail_stats。故直接返回失败让上层稍后重试。
    screen = display_state()
    if not screen.get("usable"):
        logger.error(
            f"【{user_id}】当前无可用显示器，跳过物理鼠标滑块"
            f"（显示器数={screen.get('monitors')} 虚拟桌面="
            f"{screen.get('width')}x{screen.get('height')}）。"
            "远程桌面断开会摘掉虚拟显示器，请保持连接或配置常驻虚拟显示器。"
        )
        return False, None

    # 按 URL 自动判场景：登录滑块用登录轨迹并强制最大化；业务滑块保持原有行为
    scene = _detect_scene(url)
    drags = _load_drags(scene)
    if not drags:
        sample = "human_trail_login_*.json" if scene == "login" else "human_trail_pass_*.json"
        logger.error(f"真实鼠标引擎缺少真人轨迹样本（human_trails/{sample}，scene={scene}）")
        return False, None

    mode_label = "CDP真机+真实鼠标" if use_cdp else "真实鼠标"
    risk = _risk_level()
    try:
        from common.services.captcha.profile_pool import describe as _pool_desc, pool_enabled as _pool_on
        pool_info = _pool_desc() if _pool_on() else "pool=off"
    except Exception:
        pool_info = "pool=?"
    logger.info(
        f"【{user_id}】启动滑块引擎: {mode_label} scene={scene} "
        f"risk={risk} fast={_fast_mode()} trails={len(drags)} {pool_info} "
        f"| 显示器={screen.get('monitors')}@{screen.get('width')}x{screen.get('height')} "
        f"| {trail_stats.describe()}"
    )

    # 加权公平排队：阻塞直到轮到本来源（无限等待，与旧 with lock 语义一致）
    if not real_mouse_scheduler.acquire(weight_class):
        logger.warning(f"【{user_id}】真实鼠标引擎排队获取执行权失败")
        return False, None
    try:
        try:
            # 串行执行前先做任务间隔（在拿到执行权后，避免占坑空等）
            _inter_task_gap()
            # 高压时自动加长单次预算，给同页慢速重试留时间
            budget = browser_timeout
            if risk >= 2:
                budget = max(budget, 70)
            elif risk == 1:
                budget = max(budget, 55)

            def _once(timeout_s: int) -> Tuple[bool, Optional[Dict[str, str]]]:
                future = _get_real_mouse_executor().submit(
                    _execute_shared_verification,
                    user_id,
                    url,
                    drags,
                    timeout_s,
                    url_provider,
                    scene,
                    use_cdp,
                    existing_cookies_str or "",
                )
                return future.result()

            ok, cookies = _once(budget)
            if ok and cookies:
                return True, cookies
            if cookies == URL_EXPIRED:
                return False, URL_EXPIRED
            if cookies == CAPTCHA_NOT_REQUIRED:
                return True, None

            # 失败：换资料彻底重试一次（仅 CDP + 资料池）
            try:
                from common.services.captcha.profile_pool import pool_enabled
                can_rotate = bool(use_cdp and pool_enabled())
            except Exception:
                can_rotate = False

            if not can_rotate:
                return False, cookies

            logger.warning(
                f"【{user_id}】首次滑块失败，切换浏览器资料后重试一次…"
            )
            # 轮换与重连必须在 real-mouse 线程内执行，避免跨线程动 Playwright
            def _rotate_then_retry():
                _switch_browser_profile_and_reconnect(
                    user_id, reason=f"first_fail user={user_id}"
                )
                # 强制重建 solver（use_cdp 相同也会因浏览器进程已死需要重连）
                global _shared_solver
                try:
                    if _shared_solver is not None:
                        try:
                            _shared_solver.close()
                        except Exception:
                            pass
                finally:
                    _shared_solver = None
                return _execute_shared_verification(
                    user_id,
                    url,
                    drags,
                    max(budget, 50),
                    url_provider,
                    scene,
                    use_cdp,
                    existing_cookies_str or "",
                )

            ok2, cookies2 = _get_real_mouse_executor().submit(_rotate_then_retry).result()
            if ok2 and cookies2:
                logger.info(f"【{user_id}】换资料后重试成功")
                return True, cookies2
            logger.warning(f"【{user_id}】换资料后仍失败")
            return False, cookies2
        except Exception as e:
            logger.error(f"【{user_id}】{mode_label}引擎执行异常: {e}")
            return False, None
    finally:
        real_mouse_scheduler.release()
