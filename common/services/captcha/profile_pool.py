"""
浏览器资料池：多套独立 user-data-dir，失败后轮换再试。

默认目录：
  <project>/browser_data/edge_pool/p0 .. p{N-1}

状态文件：
  <project>/run/captcha_profile_index.txt   当前资料序号
  <project>/run/captcha_profile_path.txt    当前资料完整路径

环境变量：
  CAPTCHA_PROFILE_POOL_SIZE   默认 4
  CAPTCHA_PROFILE_POOL_DIR    默认 browser_data/edge_pool
  CAPTCHA_PROFILE_POOL        1=启用（默认启用）
"""
from __future__ import annotations

import os
import threading
from typing import List, Tuple

from loguru import logger

_lock = threading.RLock()


def _project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def pool_enabled() -> bool:
    raw = (os.environ.get("CAPTCHA_PROFILE_POOL") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def pool_size() -> int:
    try:
        n = int(os.environ.get("CAPTCHA_PROFILE_POOL_SIZE") or "4")
    except Exception:
        n = 4
    return max(2, min(n, 12))


def pool_root() -> str:
    env = (os.environ.get("CAPTCHA_PROFILE_POOL_DIR") or "").strip().strip('"')
    if env:
        return os.path.abspath(env)
    return os.path.join(_project_root(), "browser_data", "edge_pool")


def _run_dir() -> str:
    d = os.path.join(_project_root(), "run")
    os.makedirs(d, exist_ok=True)
    return d


def _index_file() -> str:
    return os.path.join(_run_dir(), "captcha_profile_index.txt")


def _path_file() -> str:
    return os.path.join(_run_dir(), "captcha_profile_path.txt")


def list_profiles() -> List[str]:
    """返回资料目录列表（按序号），不存在则创建。"""
    root = pool_root()
    paths: List[str] = []
    for i in range(pool_size()):
        p = os.path.join(root, f"p{i}")
        os.makedirs(os.path.join(p, "Default"), exist_ok=True)
        paths.append(p)
    return paths


def get_index() -> int:
    with _lock:
        f = _index_file()
        if not os.path.isfile(f):
            return 0
        try:
            raw = open(f, encoding="utf-8").read().strip()
            idx = int(raw)
            return max(0, idx) % pool_size()
        except Exception:
            return 0


def _write_state(idx: int, path: str) -> None:
    try:
        with open(_index_file(), "w", encoding="utf-8") as fh:
            fh.write(str(idx))
        with open(_path_file(), "w", encoding="utf-8") as fh:
            fh.write(path)
    except Exception as e:
        logger.warning(f"写入资料池状态失败: {e}")


def get_active_profile_dir() -> str:
    """当前应使用的 user-data-dir。"""
    profiles = list_profiles()
    idx = get_index()
    path = profiles[idx]
    _write_state(idx, path)
    return path


def rotate_profile(reason: str = "") -> Tuple[int, str]:
    """轮换到下一套资料，返回 (新序号, 路径)。"""
    with _lock:
        profiles = list_profiles()
        old = get_index()
        new = (old + 1) % len(profiles)
        path = profiles[new]
        _write_state(new, path)
        logger.warning(
            f"浏览器资料池轮换: p{old} -> p{new} path={path} reason={reason or 'n/a'}"
        )
        return new, path


def describe() -> str:
    idx = get_index()
    path = get_active_profile_dir()
    return f"pool={pool_enabled()} size={pool_size()} active=p{idx} path={path}"
