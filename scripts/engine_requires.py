#!/usr/bin/env python3
"""engine_requires.py — 引擎后端依赖声明的解析、检查与修复指引。

背景（v2.4.2）：argo 的 CLI 引擎此前只校验 `cmd[-1]` 指向的脚本文件是否存在
（见 config._validate_engine_paths）。这对「壳脚本」型引擎完全无效：例如
xiaohongshu 引擎的 cmd 是 `python3 scripts/social_engines/xiaohongshu_engine.py`，
脚本文件当然存在，于是状态显示 ready；但该脚本内部真正调用的 `xhs` 二进制
依赖外部 CLI（xhs-cli），用户没装时它只是静默返回空列表——`--list-engines
--detail` 仍报 ready，调用后白等到超时、拿到 0 条，且无从知道「缺什么、怎么装」。

本模块引入声明式依赖：

  engines:
    xiaohongshu:
      requires:
        - bin: xhs                      # 必需的 PATH 可执行文件
          fix: npm i -g xiaohongshu-cli  # 缺失时的修复指引
          optional: false                # true 时缺失只降级不报 missing_dep
        - bin: yt-dlp
          min_version: "2024.01.01"      # 可选：最低版本（尽力检查）

设计原则（与既有 required_env / engine_env.py 同构，属扩展而非外来物）：
  - 只声明「后端工具」，不声明「密钥」（密钥仍归 required_env）
  - 检查是只读的：`shutil.which` + 一次 `--version` 调用，绝不自动安装
  - 缺失必须给出可执行 fix 指令，否则声明即无意义
  - 检查结果可缓存（同进程内），避免 list-engines 对 168 引擎反复起子进程
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from typing import Any

# 进程内缓存：{(bin, min_version): (ok, actual_version, reason)}
_cache: dict[tuple[str, str | None], tuple[bool, str | None, str | None]] = {}
_cache_lock = threading.Lock()

# version 探测的候选参数（不同工具约定不同）
_VERSION_ARGS = ("--version", "-version", "-V", "version")


def _probe_version(bin_path: str, timeout: float = 3.0) -> str | None:
    """尽力探测可执行文件的版本串；失败返回 None（不作为失败依据）。"""
    for arg in _VERSION_ARGS:
        try:
            r = subprocess.run(
                [bin_path, arg],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",  # Windows GBK 下防乱码/崩溃
                timeout=timeout,
            )
            out = (r.stdout or "") + (r.stderr or "")
            out = out.strip()
            if out:
                return out.splitlines()[0][:120]
        except Exception:
            continue
    return None


def _parse_version_numbers(text: str | None) -> tuple[int, ...]:
    """从版本串里抽出数字段，用于比较。抽不到返回空元组。"""
    if not text:
        return ()
    nums: list[int] = []
    cur = ""
    for ch in text:
        if ch.isdigit():
            cur += ch
        else:
            if cur:
                nums.append(int(cur))
                cur = ""
    if cur:
        nums.append(int(cur))
    return tuple(nums[:4])


def _version_ok(actual: str | None, minimum: str | None) -> bool:
    """actual >= minimum？任一解析不出数字则视为通过（不误报缺失）。"""
    if not minimum:
        return True
    a, b = _parse_version_numbers(actual), _parse_version_numbers(minimum)
    if not a or not b:
        return True
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return a >= b


def check_requirement(req: dict[str, Any]) -> dict[str, Any]:
    """检查单条依赖声明，返回结构化结果。

    返回 {bin, ok, actual_version, required_version, reason, fix, optional}
    """
    bin_name = str(req.get("bin") or "").strip()
    minimum = req.get("min_version")
    optional = bool(req.get("optional", False))
    fix = str(req.get("fix") or "").strip()
    out = {
        "bin": bin_name,
        "ok": False,
        "actual_version": None,
        "required_version": minimum,
        "reason": None,
        "fix": fix,
        "optional": optional,
    }
    if not bin_name:
        out["reason"] = "invalid-requirement"
        return out

    key = (bin_name, minimum)
    with _cache_lock:
        cached = _cache.get(key)
    if cached is not None:
        ok, actual, reason = cached
        out.update({"ok": ok, "actual_version": actual, "reason": reason})
        return out

    path = shutil.which(bin_name)
    if not path:
        ok, actual, reason = False, None, "bin-not-found"
    else:
        actual = _probe_version(path)
        if _version_ok(actual, minimum):
            ok, reason = True, None
        else:
            ok, reason = False, "version-too-old"
    with _cache_lock:
        _cache[key] = (ok, actual, reason)
    out.update({"ok": ok, "actual_version": actual, "reason": reason})
    return out


def requirements_of(spec: dict[str, Any] | None) -> list[dict[str, Any]]:
    """从 engine spec 取出 requires 声明（规范化成 list[dict]）。

    容错：允许简写 `requires: [{bin: xhs}]`，也允许字符串简写
    `requires: ["xhs"]`（等价 {bin: xhs}），便于手写 config。
    """
    spec = spec or {}
    raw = spec.get("requires")
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append({"bin": item.strip()})
        elif isinstance(item, dict) and item.get("bin"):
            out.append(dict(item))
    return out


def requires_status(spec: dict[str, Any] | None) -> dict[str, Any]:
    """引擎依赖总览：缺失的必需项 + 全部检查明细。

    missing_deps 只含必需项（optional 缺失不算），保证状态机
    `missing_dep` 只反映「真的跑不了」，不把可选增强算成故障。
    """
    reqs = requirements_of(spec)
    if not reqs:
        return {"requires": [], "missing_deps": [], "dep_ready": True,
                "dep_fixes": []}
    checked = [check_requirement(r) for r in reqs]
    missing = [c for c in checked if not c["ok"] and not c["optional"]]
    return {
        "requires": checked,
        "missing_deps": [{"bin": c["bin"], "reason": c["reason"],
                          "fix": c["fix"]} for c in missing],
        "dep_ready": not missing,
        # 修复指引：缺失项自带 fix；未声明 fix 的给通用兜底提示
        "dep_fixes": [
            c["fix"] or f"安装 {c['bin']} 并确保其在 PATH 上"
            for c in missing
        ],
    }


def clear_cache() -> None:
    """清空探测缓存（测试与配置热更新用）。"""
    with _cache_lock:
        _cache.clear()
