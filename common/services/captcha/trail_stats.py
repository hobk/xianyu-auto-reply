"""
真人轨迹样本的在线成功率统计。

为什么需要它：
- 业务滑块可用的真人轨迹样本只有几条，不同样本被风控接受的概率差异极大
  （实测同一批任务里，某条真人样本 53%，另一条 19%，合成兜底轨迹仅 7%）；
- 原先 _choose_drag 用「点数/时长」等静态启发式打分，恰好把通过率最高的短时长
  真人样本压到最低权重，越失败越只用最差的轨迹，形成死循环。

做法：
- 以样本来源（文件名+位移）为 key，持久化 (总次数, 成功次数)；
- 选轨迹时按 Laplace 平滑后的成功率加权，并给采样不足的样本探索加成，
  让引擎自己收敛到当前风控环境下最有效的那条轨迹。

统计文件：<project>/run/captcha_trail_stats.json
"""
from __future__ import annotations

import json
import os
import threading
from typing import Dict, Tuple

from loguru import logger

_lock = threading.RLock()
_cache: Dict[str, Dict[str, int]] = {}
_loaded = False

# 采样不足时的探索加成阈值：每条轨迹至少先试这么多次再让成功率主导权重
_MIN_SAMPLES = 8
# Laplace 平滑先验（相当于「先验成功率 1/3」），避免一次失败就把样本判死
_PRIOR_SUCCESS = 1.0
_PRIOR_TOTAL = 3.0


def _project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def _stats_file() -> str:
    run_dir = os.path.join(_project_root(), "run")
    os.makedirs(run_dir, exist_ok=True)
    return os.path.join(run_dir, "captcha_trail_stats.json")


def _load() -> None:
    global _loaded
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        try:
            path = _stats_file()
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as fh:
                    raw = json.load(fh)
                if isinstance(raw, dict):
                    for key, value in raw.items():
                        if isinstance(value, dict):
                            _cache[str(key)] = {
                                "total": int(value.get("total") or 0),
                                "success": int(value.get("success") or 0),
                            }
        except Exception as exc:
            logger.warning(f"加载真人轨迹统计失败（按空统计继续）: {exc}")
        _loaded = True


def _save() -> None:
    """原子落盘；并发写入偶发丢失一次记录不影响整体收敛。"""
    try:
        path = _stats_file()
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_cache, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as exc:
        logger.debug(f"保存真人轨迹统计失败（可忽略）: {exc}")


def record(key: str, success: bool) -> None:
    """记录一次轨迹回放结果。"""
    if not key:
        return
    _load()
    with _lock:
        entry = _cache.setdefault(str(key), {"total": 0, "success": 0})
        entry["total"] += 1
        if success:
            entry["success"] += 1
        _save()


def get(key: str) -> Tuple[int, int]:
    """返回 (总次数, 成功次数)。"""
    _load()
    with _lock:
        entry = _cache.get(str(key)) or {}
        return int(entry.get("total") or 0), int(entry.get("success") or 0)


def weight(key: str) -> float:
    """返回该轨迹的选用权重（Laplace 平滑成功率 + 探索加成）。"""
    total, success = get(key)
    rate = (success + _PRIOR_SUCCESS) / (total + _PRIOR_TOTAL)
    if total < _MIN_SAMPLES:
        # 采样不足：抬到不低于先验，保证每条样本都有机会被试出来
        rate = max(rate, _PRIOR_SUCCESS / _PRIOR_TOTAL)
    return max(0.02, float(rate))


def describe() -> str:
    """给日志用的一行摘要，按成功率倒序。"""
    _load()
    with _lock:
        items = list(_cache.items())
    if not items:
        return "trail_stats=empty"
    parts = []
    for key, value in sorted(
        items,
        key=lambda kv: -(kv[1].get("success", 0) / max(1, kv[1].get("total", 1))),
    ):
        total = int(value.get("total") or 0)
        success = int(value.get("success") or 0)
        pct = (100.0 * success / total) if total else 0.0
        parts.append(f"{key}={success}/{total}({pct:.0f}%)")
    return "trail_stats " + " ".join(parts)
