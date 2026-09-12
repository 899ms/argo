#!/usr/bin/env python3
"""test_timeout_declaration_policy.py — 引擎 timeout 声明策略 回归测试。

## 背景

`search._engine_retries()` 的约定：**声明了 `spec.timeout >= 8s` 的引擎
不再叠加引擎级重试**（设计意图是「慢源超时即弃，避免重试线性放大」）。

这条约定产生了一个**必须区分的二分**（2026-09-10 核查 6 个未声明引擎时发现）：

| 引擎内部情况 | 该不该声明 spec.timeout | 理由 |
|---|---|---|
| **有广谱重试**（429 + URLError，自带退避） | **应该声明** | 否则内外两层重试叠乘（anysearch 曾 31s） |
| **无内部重试** | **不该声明** | 声明会关掉外层重试 = 一次失败即放弃，降低成功率 |

一刀切两种都错：
  · 全声明 → 无内部重试的引擎失去外层重试（成功率下降）
  · 全不声明 → 有内部重试的引擎叠乘（延迟爆炸）

## 实测依据

各 social 引擎共用的 `_http_get_with_retry` 是**广谱重试**：
    except HTTPError as e:  if e.code == 429: continue      # 限流重试
    except (URLError, OSError):  if attempt < max_retries:  # 网络错误重试
        time.sleep(2 ** attempt + 0.5)
即 weibo/reddit/bilibili/twitter 内部最多 3 次尝试带指数退避 —— 与 anysearch
同构，故都应声明 timeout。

而 local_search / xiaohongshu 内部**无重试**，不应声明。

## 本文件锁定

  1. 有内部重试的引擎必须声明 timeout（防叠乘）
  2. 无内部重试的引擎**不得**被误声明（防成功率下降）—— 以断言文档化意图
  3. `execution.per_engine_budget_s` 可配置、非法值安全回落
  4. 预算常量与 default_timeout 的合理关系
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(ROOT / "scripts"))

from config import load_config  # noqa: E402
import search  # noqa: E402

# ── 分组：按「内部是否有广谱重试」划分（实测确认）──────────────────────
HAS_INTERNAL_RETRY = ("weibo", "reddit", "bilibili", "twitter", "anysearch")
NO_INTERNAL_RETRY = ("local_search", "xiaohongshu")

# 内部重试的特征：共用 _http_get_with_retry（429 + URLError 双分支）
_RETRY_SIGNS = (
    re.compile(r"def _http_get_with_retry"),
    re.compile(r"except\s+urllib\.error\.HTTPError"),
    re.compile(r"except\s+\(urllib\.error\.URLError,\s*OSError\)"),
    re.compile(r"max_retries"),
)


def _engine_source(engine_id: str) -> str:
    """读引擎实现源码。

    解析顺序：social_engines/<id>_engine.py → scripts/<id>_engine.py →
    **从 config 的 cmd 字段取路径**（local_search 这类引擎的实现在
    sub-skills/ 下，不在 scripts/，硬编码候选路径会解析不到 →
    测试空转、给虚假安全感。实测教训：首版漏了第三条路径，
    local_search 的源码长度为 0、该断言被静默跳过）。
    """
    candidates = [
        ROOT / "scripts" / "social_engines" / f"{engine_id}_engine.py",
        ROOT / "scripts" / f"{engine_id}_engine.py",
    ]
    for p in candidates:
        if p.is_file():
            return p.read_text(encoding="utf-8")
    # 回落到 config 声明的 cmd 路径
    try:
        spec = (load_config().get("engines") or {}).get(engine_id) or {}
        for part in (spec.get("cmd") or []):
            if isinstance(part, str) and part.endswith(".py"):
                p = ROOT / part
                if p.is_file():
                    return p.read_text(encoding="utf-8")
    except Exception:
        pass
    return ""


def _has_internal_retry(src: str) -> bool:
    return sum(1 for rx in _RETRY_SIGNS if rx.search(src)) >= 2


class TestRetryPolicyConsistency:
    """分组必须与实现一致——防止分类过时。"""

    def test_has_retry_group_really_has_retry(self):
        for eng in HAS_INTERNAL_RETRY:
            if eng == "anysearch":
                continue  # HTTP builder，非 social 脚本，另测
            src = _engine_source(eng)
            assert src, f"{eng} 源码未找到（分组过时？）"
            assert _has_internal_retry(src), (
                f"{eng} 被列为「有内部重试」但源码未见重试特征；"
                f"若实现已变，需重新评估是否该声明 timeout")

    def test_no_retry_group_really_has_no_retry(self):
        for eng in NO_INTERNAL_RETRY:
            src = _engine_source(eng)
            # 不再「找不到就跳过」——那会让断言空转、给出虚假安全感。
            # 找不到源码说明分组或路径解析过时，必须显式失败。
            assert src, (
                f"{eng} 源码未解析到（分组过时或路径解析失效）——"
                f"本断言不可静默跳过")
            assert not _has_internal_retry(src), (
                f"{eng} 被列为「无内部重试」但源码出现重试特征；"
                f"若已加重试，应声明 timeout 消除叠乘")

    def test_anysearch_builder_has_no_http_retry(self):
        """anysearch 的 HTTP 级重试已移除（max_retries=0）。"""
        src = (ROOT / "scripts" / "engines_builders_tech.py").read_text(encoding="utf-8")
        assert "max_retries=0, jitter=False" in src, (
            "anysearch builder 又出现了 HTTP 级重试 → 会与编排层叠乘")


class TestTimeoutDeclaration:
    """声明策略：有内部重试的必须声明；无内部重试的不应声明。"""

    def test_retry_engines_declare_timeout(self):
        cfg = load_config(force=True)
        engines = cfg.get("engines") or {}
        missing = []
        for eng in HAS_INTERNAL_RETRY:
            spec = engines.get(eng) or {}
            to = spec.get("timeout")
            if not (isinstance(to, (int, float)) and to >= 8):
                missing.append(f"{eng}(timeout={to})")
        assert not missing, (
            "以下引擎内部有广谱重试但未声明 timeout>=8 → 会与引擎级重试叠乘：\n  "
            + "\n  ".join(missing))

    def test_no_retry_engines_not_declared(self):
        """无内部重试的引擎不得声明 ≥8s timeout（否则失去外层重试）。

        这是**意图文档化**断言：若将来给它们加了内部重试，应同时移入
        HAS_INTERNAL_RETRY 并声明 timeout。
        """
        cfg = load_config(force=True)
        engines = cfg.get("engines") or {}
        wrongly = []
        for eng in NO_INTERNAL_RETRY:
            spec = engines.get(eng) or {}
            to = spec.get("timeout")
            if isinstance(to, (int, float)) and to >= 8:
                wrongly.append(f"{eng}(timeout={to})")
        assert not wrongly, (
            "以下引擎内部无重试，声明 timeout>=8 会关掉外层重试、降低成功率：\n  "
            + "\n  ".join(wrongly)
            + "\n（若确实要关重试，请说明理由并更新本测试的分组）")


class TestBudgetConfig:
    """预算可配置 + 非法值安全回落。"""

    def test_config_has_budget_key(self):
        ex = load_config(force=True).get("execution") or {}
        assert "per_engine_budget_s" in ex, (
            "execution.per_engine_budget_s 缺失 → 用户无法调整预算")
        assert float(ex["per_engine_budget_s"]) > 0

    def test_budget_relation_to_default_timeout(self):
        """预算必须 ≥ default_timeout，否则单次正常尝试会被截断。"""
        ex = load_config(force=True).get("execution") or {}
        budget = float(ex.get("per_engine_budget_s", 10))
        default_to = float(ex.get("default_timeout", 8))
        assert budget >= default_to, (
            f"预算 {budget}s < default_timeout {default_to}s → 会截断正常请求")

    def test_constant_fallback_is_sane(self):
        assert 8.0 <= search._PER_ENGINE_BUDGET_S <= 15.0


class TestXhsInnerTimeout:
    """小红书内部子进程超时应可配且短于预算（避免内层白跑）。"""

    def test_inner_timeout_default_below_budget(self):
        src = (ROOT / "scripts" / "social_engines" / "xiaohongshu_engine.py"
               ).read_text(encoding="utf-8")
        m = re.search(r"_DEFAULT_TIMEOUT\s*=\s*(\d+)", src)
        assert m, "未找到 _DEFAULT_TIMEOUT"
        v = int(m.group(1))
        assert v <= search._PER_ENGINE_BUDGET_S, (
            f"内部超时 {v}s 长于预算 {search._PER_ENGINE_BUDGET_S}s → 内层会白跑")

    def test_inner_timeout_env_overridable(self, monkeypatch):
        sys.path.insert(0, str(ROOT / "scripts" / "social_engines"))
        import importlib
        import xiaohongshu_engine as xe
        importlib.reload(xe)
        monkeypatch.setenv("ARGO_XHS_TIMEOUT", "3")
        assert xe._subprocess_timeout() == 3
        monkeypatch.setenv("ARGO_XHS_TIMEOUT", "abc")
        assert xe._subprocess_timeout() == xe._DEFAULT_TIMEOUT
