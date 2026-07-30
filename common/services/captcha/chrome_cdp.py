"""
连接本机真实浏览器（Edge/Chrome CDP）辅助模块。

设计目的：
- 闲鱼风控对「自动化浏览器 + 注入鼠标事件」识别较强；
- 对本机日常浏览器（尤其用户手测能过的 Edge）+ 物理鼠标识别较弱；
- 通过 CDP 接入浏览器，打开验证页后由真实鼠标引擎完成滑动。

环境变量：
- CAPTCHA_BROWSER               edge（默认）| chrome
- CAPTCHA_CHROME_CDP_URL        默认 http://127.0.0.1:9222
- CAPTCHA_CHROME_AUTO_LAUNCH    默认 true
- CAPTCHA_CHROME_PATH           msedge.exe / chrome.exe 路径（可选）
- CAPTCHA_CHROME_USER_DATA_DIR  用户数据目录
- CAPTCHA_CHROME_PROFILE        配置目录名，默认 Default
- CAPTCHA_CHROME_DEBUG_PORT     调试端口，默认 9222
- CAPTCHA_CHROME_PROXY          代理 URL；空=本机直连
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from typing import List, Optional, Tuple
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, build_opener

from loguru import logger

# DevTools 端点固定在本机回环地址，绝不能走系统/环境变量里的代理。
#
# 踩过的坑：服务由 pm2 拉起时会继承 http_proxy=http://127.0.0.1:10808 却没有 no_proxy。
# CPython 的 urllib 一旦在环境变量里发现代理，proxy_bypass() 就只查 no_proxy，
# 不再读注册表的 ProxyOverride（那里面本来有 127.* ），于是 urlopen 会把
# http://127.0.0.1:9222/json/version 也发给代理 → CDP 明明已就绪却永远探测不到 →
# 上层判定「端口未就绪」并不停 kill + 重启 Edge，表现为浏览器窗口一闪即逝。
_NO_PROXY_OPENER = build_opener(ProxyHandler({}))


def get_browser_kind() -> str:
    """edge | chrome，默认 edge（本机手测能过）。"""
    raw = (
        os.environ.get("CAPTCHA_BROWSER")
        or os.environ.get("CAPTCHA_CDP_BROWSER")
        or "edge"
    ).strip().lower()
    if raw in ("chrome", "chromium", "google-chrome"):
        return "chrome"
    return "edge"


def get_cdp_endpoint() -> str:
    """返回 CDP HTTP 端点（无尾部斜杠）。"""
    raw = (
        os.environ.get("CAPTCHA_CHROME_CDP_URL")
        or os.environ.get("CHROME_CDP_URL")
        or "http://127.0.0.1:9222"
    ).strip()
    return raw.rstrip("/")


def get_debug_port() -> int:
    """从端点或环境变量解析调试端口。"""
    env_port = (os.environ.get("CAPTCHA_CHROME_DEBUG_PORT") or "").strip()
    if env_port.isdigit():
        return int(env_port)
    endpoint = get_cdp_endpoint()
    try:
        # http://127.0.0.1:9222
        host_port = endpoint.split("://", 1)[-1]
        port_part = host_port.rsplit(":", 1)[-1]
        if port_part.isdigit():
            return int(port_part)
    except Exception:
        pass
    return 9222


def find_chrome_executable() -> Optional[str]:
    """定位本机浏览器：默认 Edge，可切 Chrome。"""
    env_path = (os.environ.get("CAPTCHA_CHROME_PATH") or "").strip().strip('"')
    if env_path and os.path.isfile(env_path):
        return env_path

    kind = get_browser_kind()
    candidates: List[str] = []

    def _add_edge(out: List[str]) -> None:
        for key in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            base = os.environ.get(key) or ""
            if base:
                out.append(
                    os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")
                )
        out.append(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
        out.append(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")

    def _add_chrome(out: List[str]) -> None:
        for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(key) or ""
            if base:
                out.append(
                    os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")
                )
        out.append(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
        out.append(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe")

    if kind == "chrome":
        _add_chrome(candidates)
        _add_edge(candidates)
    else:
        _add_edge(candidates)
        _add_chrome(candidates)

    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def get_user_data_dir() -> str:
    """浏览器用户数据目录（含 Default/Profile 的上一级）。

    优先顺序：
    1. 资料池启用时：browser_data/edge_pool/p{N}（失败可轮换）
    2. CAPTCHA_CHROME_USER_DATA_DIR（显式固定资料）
    3. Edge/Chrome 本机或项目回退路径
    """
    # 资料池：多套独立指纹，失败后 rotate
    try:
        from common.services.captcha.profile_pool import pool_enabled, get_active_profile_dir, describe
        if pool_enabled():
            path = get_active_profile_dir()
            logger.debug(f"使用资料池: {describe()}")
            return path
    except Exception as e:
        logger.warning(f"资料池读取失败，回退固定目录: {e}")

    env_dir = (os.environ.get("CAPTCHA_CHROME_USER_DATA_DIR") or "").strip().strip('"')
    if env_dir and os.path.isdir(env_dir):
        return env_dir

    project_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..")
    )
    kind = get_browser_kind()
    local = (os.environ.get("LOCALAPPDATA") or "").strip()
    userprofile = (os.environ.get("USERPROFILE") or "").strip()

    candidates: List[str] = []
    if kind == "edge":
        if local and "systemprofile" not in local.lower():
            candidates.append(os.path.join(local, "Microsoft", "Edge", "User Data"))
        if userprofile and "systemprofile" not in userprofile.lower():
            candidates.append(
                os.path.join(
                    userprofile,
                    "AppData",
                    "Local",
                    "Microsoft",
                    "Edge",
                    "User Data",
                )
            )
        candidates.append(os.path.join(project_root, "browser_data", "edge_manual"))
        candidates.append(r"C:\Users\Admin\AppData\Local\Microsoft\Edge\User Data")
    else:
        for name in ("chrome_env_v3", "chrome_clean_manual"):
            candidates.append(os.path.join(project_root, "browser_data", name))
        if local and "systemprofile" not in local.lower():
            candidates.append(os.path.join(local, "Google", "Chrome", "User Data"))

    for path in candidates:
        if path and os.path.isdir(path):
            return path

    if kind == "edge" and local:
        return os.path.join(local, "Microsoft", "Edge", "User Data")
    return os.path.join(project_root, "browser_data", "edge_manual")


def get_profile_directory() -> str:
    """Chrome 配置目录名（Default / Profile 1 等）。"""
    return (os.environ.get("CAPTCHA_CHROME_PROFILE") or "Default").strip() or "Default"


def get_captcha_proxy() -> str:
    """返回过滑块 Chrome 使用的代理 URL；空字符串表示强制直连。

    支持：
    - http://host:port
    - http://user:pass@host:port
    - socks5://host:port
    - socks5://user:pass@host:port
    - direct / none / off → 直连
    """
    raw = (
        os.environ.get("CAPTCHA_CHROME_PROXY")
        or os.environ.get("CAPTCHA_PROXY")
        or ""
    ).strip()
    if not raw or raw.lower() in ("direct", "none", "off", "false", "0"):
        return ""
    if raw.lower() == "auto":
        # 沿用系统常见本地代理（若你确认出口 IP 与直连不同再用）
        return (os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or "").strip()
    return raw


def chrome_proxy_server_arg(proxy_url: str) -> Optional[str]:
    """把代理 URL 转成 Chrome --proxy-server= 参数值。"""
    if not proxy_url:
        return None
    p = urlparse(proxy_url)
    scheme = (p.scheme or "http").lower()
    if scheme in ("socks5", "socks5h"):
        host = p.hostname or ""
        port = p.port or 1080
        return f"socks5://{host}:{port}"
    if scheme in ("http", "https"):
        host = p.hostname or ""
        port = p.port or 8080
        return f"http://{host}:{port}"
    # 已是 host:port 形式
    if "://" not in proxy_url:
        return proxy_url
    return proxy_url


def proxy_auth_from_url(proxy_url: str) -> Tuple[str, str]:
    """从代理 URL 解析用户名密码（Chrome 命令行无法直接带 auth，需扩展或手动）。"""
    if not proxy_url:
        return "", ""
    p = urlparse(proxy_url)
    return (p.username or "", p.password or "")


def is_cdp_ready(timeout: float = 1.5) -> bool:
    """探测 CDP 调试端口是否可访问（强制直连，不走任何代理）。"""
    endpoint = get_cdp_endpoint()
    try:
        with _NO_PROXY_OPENER.open(f"{endpoint}/json/version", timeout=timeout) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except (URLError, TimeoutError, OSError, ValueError):
        return False


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _chrome_process_running() -> bool:
    """粗略判断是否已有 Edge/Chrome 在运行。"""
    if sys.platform != "win32":
        return False
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "(Get-Process msedge,chrome -ErrorAction SilentlyContinue | Measure-Object).Count",
            ],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
        )
        count = int((result.stdout or "0").strip() or "0")
        return count > 0
    except Exception:
        return False


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(path or "")).rstrip("\\/")


def get_cdp_chrome_command_lines() -> List[str]:
    """返回带 remote-debugging-port 的 Edge/Chrome 主进程命令行。"""
    if sys.platform != "win32":
        return []
    port = get_debug_port()
    ps = (
        f"Get-CimInstance Win32_Process | "
        f"Where-Object {{ $_.Name -match '^(msedge|chrome)\\.exe$' -and $_.CommandLine -and "
        f"$_.CommandLine -match 'remote-debugging-port={port}' -and "
        f"$_.CommandLine -notmatch '--type=' }} | "
        f"ForEach-Object {{ $_.CommandLine }}"
    )
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                ps,
            ],
            capture_output=True,
            text=True,
            # PowerShell 在中文 Windows 上按 GBK 输出，而服务进程带 PYTHONUTF8=1，
            # 不加 errors 会抛 UnicodeDecodeError 并让 stdout 变空 —— 那会被误判成
            # 「没有带调试端口的浏览器」，进而触发无谓的强制重启。
            errors="replace",
            timeout=15,
        )
        lines = [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
        return lines
    except Exception:
        return []


def is_cdp_using_expected_profile() -> Tuple[bool, str]:
    """检查当前 CDP Chrome 是否使用配置的干净 user-data-dir。"""
    expected = _norm_path(get_user_data_dir())
    cmds = get_cdp_chrome_command_lines()
    if not cmds:
        if is_cdp_ready():
            return False, "CDP 端口可达但未找到带调试端口的 chrome 主进程命令行"
        return False, "CDP Chrome 未运行"
    for cmd in cmds:
        # --user-data-dir=... 或 --user-data-dir="..."
        marker = "--user-data-dir="
        idx = cmd.lower().find(marker)
        if idx < 0:
            continue
        rest = cmd[idx + len(marker) :]
        if rest.startswith('"'):
            end = rest.find('"', 1)
            raw = rest[1:end] if end > 0 else rest[1:]
        else:
            raw = rest.split(" ", 1)[0]
        actual = _norm_path(raw)
        if actual == expected or expected in actual or actual in expected:
            return True, f"CDP 使用干净配置: {actual}"
        return False, f"CDP 配置目录不匹配: 实际={actual} 期望={expected}"
    return False, f"CDP Chrome 命令行无 user-data-dir，期望={expected}"


def _cdp_process_filter() -> str:
    """PowerShell Where-Object 过滤式：只匹配本项目的 CDP 浏览器进程。

    匹配调试端口、当前资料目录、以及项目专用的 edge_pool/chrome_* 目录；
    绝不匹配用户日常的 Edge/Chrome。
    """
    port = get_debug_port()
    user_data = get_user_data_dir().replace("'", "''")
    return (
        f"$_.Name -match '^(msedge|chrome)\\.exe$' -and $_.CommandLine -and ("
        f"$_.CommandLine -match 'remote-debugging-port={port}' -or "
        f"$_.CommandLine -like '*{user_data}*' -or "
        f"$_.CommandLine -match 'edge_pool|chrome_clean_manual|chrome_env_v3|edge_manual|real_mouse_shared'"
        f")"
    )


def count_cdp_browser_processes() -> int:
    """统计仍存活的本项目 CDP 浏览器进程数（含渲染等子进程）。"""
    if sys.platform != "win32":
        return 0
    ps = (
        f"(Get-CimInstance Win32_Process | "
        f"Where-Object {{ {_cdp_process_filter()} }} | Measure-Object).Count"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=15,
        )
        return int((result.stdout or "0").strip() or "0")
    except Exception:
        return 0


def kill_cdp_chrome() -> None:
    """结束占用调试端口 / 目标配置目录的 Edge/Chrome 进程树，并等进程真正消失。

    只杀我们的 CDP 实例，绝不 Stop-Process 整机 Edge/Chrome（否则日常浏览器被杀后
    自动恢复会叠出一堆窗口）。

    为什么必须等进程退出：taskkill 返回不代表进程已消失（实测返回时仍有 13 个
    msedge 存活），此时 user-data-dir 的 lockfile 还被将死的实例占着。若立刻拉起
    新 Edge，新实例会认为已有实例接管、把命令行转交后自己退出（表现为「浏览器窗口
    一闪就没了」），--remote-debugging-port 于是永远不会被绑定，调用方只能空等超时。
    """
    if sys.platform != "win32":
        return
    ps = (
        f"$targets = Get-CimInstance Win32_Process | "
        f"Where-Object {{ {_cdp_process_filter()} }}; "
        f"foreach ($t in $targets) {{ taskkill /PID $t.ProcessId /T /F 2>$null }}"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
        )
    except Exception as e:
        logger.warning(f"结束 CDP 浏览器失败: {e}")

    # 等端口释放
    for _ in range(20):
        if not is_cdp_ready(timeout=0.3):
            break
        time.sleep(0.15)

    # 再等进程真正退出，确保 user-data-dir 的 lockfile 已释放。
    # 上限 6s：实测 taskkill 后 1~2s 内就能退干净，等满 15s 只是在烧验证链接的有效期；
    # 真退不掉时下游 launch 还有一轮「清理后重试」兜底。
    deadline = time.time() + 6.0
    remaining = count_cdp_browser_processes()
    while remaining > 0 and time.time() < deadline:
        time.sleep(0.25)
        remaining = count_cdp_browser_processes()
    if remaining > 0:
        logger.warning(
            f"CDP 浏览器进程在 6s 内未完全退出（仍剩 {remaining} 个），"
            "新实例可能因资料目录被占用而启动失败"
        )
    else:
        # 给文件系统一点时间落盘释放 lockfile
        time.sleep(0.3)


def launch_chrome_with_cdp(force: bool = False) -> Tuple[bool, str]:
    """启动带远程调试端口的本机 Chrome（干净配置目录）。

    force=True 时：若当前 CDP 不是期望配置，先杀掉再拉起干净环境。

    Returns:
        (是否成功, 说明信息)
    """
    user_data = get_user_data_dir()
    profile = get_profile_directory()

    if is_cdp_ready() and not force:
        ok, detail = is_cdp_using_expected_profile()
        if ok:
            return True, f"CDP 已就绪(干净配置): {get_cdp_endpoint()} | {detail}"
        logger.warning(f"CDP 已开但非干净配置，将强制切换: {detail}")
        force = True

    if force and (is_cdp_ready() or _chrome_process_running()):
        logger.info("强制重启 CDP Chrome 以切换到干净配置…")
        kill_cdp_chrome()
        time.sleep(0.5)

    if is_cdp_ready() and not force:
        return True, f"CDP 已就绪: {get_cdp_endpoint()}"

    chrome = find_chrome_executable()
    if not chrome:
        return False, "未找到 msedge.exe/chrome.exe，请安装 Edge 或设置 CAPTCHA_CHROME_PATH"

    port = get_debug_port()
    kind = "Edge" if chrome.lower().endswith("msedge.exe") else "Chrome"

    if not os.path.isdir(user_data):
        try:
            os.makedirs(os.path.join(user_data, profile), exist_ok=True)
        except Exception:
            return False, f"浏览器用户数据目录不存在且无法创建: {user_data}"

    if _chrome_process_running() and not is_cdp_ready() and not force:
        return (
            False,
            f"检测到 {kind} 已在运行但未开启远程调试。请先完全退出浏览器，"
            "再运行 scripts\\start-chrome-cdp.ps1，"
            f"以便使用真实环境。目标端口: {port}",
        )

    # 尽量少的启动参数：接近日常桌面浏览器
    args = [
        chrome,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data}",
        f"--profile-directory={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--lang=zh-CN",
        "--start-maximized",
    ]
    proxy_url = get_captcha_proxy()
    proxy_server = chrome_proxy_server_arg(proxy_url)
    if proxy_server:
        args.append(f"--proxy-server={proxy_server}")
        args.append("--proxy-bypass-list=<-loopback>;localhost;127.0.0.1")
        user, pwd = proxy_auth_from_url(proxy_url)
        if user:
            logger.warning(
                "CAPTCHA_CHROME_PROXY 含账号密码：命令行无法自动填充代理认证"
            )
        logger.info(f"过滑块 {kind} 将使用代理: {proxy_server}")
    else:
        logger.info(f"过滑块 {kind} 使用本机直连（未配置 CAPTCHA_CHROME_PROXY）")

    logger.info(
        f"启动 CDP {kind}: data={user_data} profile={profile} port={port}"
    )

    # 最多试 2 轮：Edge 若因资料目录被占用而「转交命令行后自退」，调试端口永远不会
    # 绑定，空等到超时毫无意义；此时清干净残留进程再拉一次才有意义。
    for attempt in (1, 2):
        try:
            # Windows: CREATE_NO_WINDOW 避免再弹黑框控制台（浏览器 GUI 仍正常）
            creationflags = 0
            if sys.platform == "win32":
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                close_fds=True,
                creationflags=creationflags,
            )
        except Exception as e:
            return False, f"启动 {kind} 失败: {e}"

        # 等待 CDP 就绪（正常 1~3s 即可绑定，给到 15s 足够）
        deadline = time.time() + 15.0
        while time.time() < deadline:
            time.sleep(0.25)
            if is_cdp_ready(timeout=0.8):
                ok, detail = is_cdp_using_expected_profile()
                logger.info(
                    f"已启动调试 {kind}，CDP={get_cdp_endpoint()} profile={profile} "
                    f"proxy={proxy_server or 'direct'} | {detail}"
                )
                return True, f"已启动 {kind} 并开启 CDP: {get_cdp_endpoint()} | {detail}"

        if attempt == 1:
            logger.warning(
                f"{kind} 启动后 15s 内未绑定调试端口 {port}"
                "（多半是资料目录被残留实例占用导致新窗口自退），清理残留后重试一次"
            )
            kill_cdp_chrome()

    return False, f"{kind} 已拉起但 CDP 端口 {port} 未在超时内就绪"


def ensure_cdp_chrome(force_clean: Optional[bool] = None) -> Tuple[bool, str]:
    """确保 CDP 可用且使用干净配置。

    force_clean:
      - None: 读环境变量 CAPTCHA_CHROME_FORCE_CLEAN（默认 true）
      - True/False: 显式覆盖
    """
    if force_clean is None:
        raw = (os.environ.get("CAPTCHA_CHROME_FORCE_CLEAN") or "true").strip().lower()
        force_clean = raw not in ("0", "false", "no", "off")

    if is_cdp_ready():
        ok, detail = is_cdp_using_expected_profile()
        if ok:
            return True, f"CDP 已连接干净配置: {get_cdp_endpoint()} | {detail}"
        if force_clean:
            logger.warning(f"CDP 非干净配置，强制切换: {detail}")
            return launch_chrome_with_cdp(force=True)
        logger.warning(f"CDP 非干净配置但未强制切换: {detail}")
        return True, f"CDP 已连接(未校验配置): {get_cdp_endpoint()} | {detail}"

    auto = (os.environ.get("CAPTCHA_CHROME_AUTO_LAUNCH") or "true").strip().lower()
    if auto in ("0", "false", "no", "off"):
        return (
            False,
            f"CDP 未就绪（{get_cdp_endpoint()}），且 CAPTCHA_CHROME_AUTO_LAUNCH 已关闭。"
            "请先运行 scripts\\start-chrome-cdp.ps1",
        )
    return launch_chrome_with_cdp(force=bool(force_clean))
