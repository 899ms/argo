#!/usr/bin/env python3
"""test_per_engine_budget.py — 单引擎墙钟预算与备选源切换 回归测试。

## 背景：实测发现的 31 秒问题

anysearch 曾同时具备两层重试：

    引擎级 retry_count=1        → 2 次尝试（因 spec 未声明 timeout）
    HTTP 级 max_retries=1       → 2 次尝试
    8s 超时
    最坏 2 × 2 × 8 = 32s（实测 31.3s）

用户侧表现是「一个查询卡半分钟」，且这期间**既没切备选源、也没有任何
信号说明在等什么**。根因是重试**叠乘**且缺总预算。

## 修法（三层）

  1. `_PER_ENGINE_BUDGET_S = 10.0`：单引擎总墙钟上界，约束**每一次**尝试
     （首试也受约束，否则 fast 的 6s 总预算会被 8s 首试突破）
  2. anysearch 声明 `timeout: 8` → `_engine_retries` 返回 0（设计好的逃生门）
  3. anysearch builder `max_retries=0` → 去掉 HTTP 级重试

失败切换的职责归**调度层**（熔断 + hedged 补发 + 本预算），
引擎内重复尝试只会放大延迟。

本文件锁定：
  · 单引擎总耗时不被重试叠乘突破（含 auto/fast/deep 三模式）
  · 预算确实压缩了后续尝试的超时（而非只是跳过）
  · 慢主引擎 + 快备选：备选结果确实被采用（用户要求的「切备选源」）
"""

from __future__ import annotations

import sys
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import search  # noqa: E402
from search import execute_search  # noqa: E402
from cache import SearchCache  # noqa: E402

QUERY = "budget probe query"


class _AllowAllBreaker:
    def allow(self, eng):
        return True, "closed"

    def get_negative(self, *a, **k):
        return None

    def status(self, eng):
        return {"state": "closed"}

    def record_success(self, *a, **k):
        pass

    def record_failure(self, *a, **k):
        pass

    def set_negative(self, *a, **k):
        pass

    def clear_negative(self, *a, **k):
        pass


def _good(tag: str) -> list:
    return [
        {"title": f"{QUERY} result {tag} {i}",
         "snippet": f"relevant snippet about {QUERY} {i}",
         "url": f"https://example.com/{tag}/{i}"}
        for i in range(3)
    ]


class TestPerEngineBudget(unittest.TestCase):
    """单引擎总墙钟上界：重试不得叠乘。"""

    def _exec(self, fake, mode: str, parallel: bool = False,
              budget: float | None = 2.0, combo: list[str] | None = None,
              timeout: int = 8):
        combo = combo or ["slow_engine"]
        calls: list[float] = []

        def _spy(query_, eng, **kw):
            calls.append(round(kw.get("timeout") or 0, 2))
            # 忠实模拟：真实引擎会把 timeout 传给 HttpClient/urlopen，
            # 即「睡满超时就放弃」。若 fixture 硬编码 sleep 而不理会 timeout，
            # 测的就不是产品行为（首版即因此误报 16s）。
            return fake(query_, eng, kw.get("timeout"))

        dec = {"engines_combo": combo, "engines": combo, "parallel": parallel,
               "domain": "general_search", "engine": combo[0],
               "features": {"primary_lang": "zh"}}
        patches = [
            patch("search.engine_search", side_effect=_spy),
            patch("circuit_breaker.get_breaker", return_value=_AllowAllBreaker()),
            patch("quota.get_quota_manager", return_value=MagicMock()),
        ]
        if budget is not None:
            # 预算现在从 config 读（execution.per_engine_budget_s），
            # 故打桩 get_execution_config 而非模块常量 —— 后者已不生效。
            patches.append(patch("search.get_execution_config",
                                 return_value={"retry_count": 1,
                                               "per_engine_budget_s": budget}))
        t0 = time.perf_counter()
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            execute_search(QUERY, dec, 5, timeout, "fast", SearchCache(":memory:"),
                           True, mode=mode)
        return time.perf_counter() - t0, calls

    def test_hanging_engine_bounded_auto(self):
        """挂起引擎 + auto：总耗时不越预算（原 31.3s）。"""
        def hang(_q, _e, to):
            time.sleep(min(to or 8, 8))   # 睡满超时 = 挂起源
            return []
        wall, calls = self._exec(hang, "auto", budget=2.0)
        self.assertLessEqual(wall, 3.0, f"auto 总耗时 {wall:.1f}s 越界（预算 2s）")
        self.assertGreaterEqual(len(calls), 1, "应至少尝试一次")

    def test_hanging_engine_bounded_deep(self):
        def hang(_q, _e, to):
            time.sleep(to or 8)
            return []
        wall, _ = self._exec(hang, "deep", budget=2.0)
        self.assertLessEqual(wall, 3.0, f"deep 总耗时 {wall:.1f}s 越界")

    def test_fast_mode_respects_tighter_budget(self):
        """fast 的 6s 总预算必须能约束**首次**尝试（否则 8s 首试会突破）。"""
        def hang(_q, _e, to):
            time.sleep(to or 8)
            return []
        wall, calls = self._exec(hang, "fast", budget=2.0)
        self.assertLessEqual(wall, 3.0, f"fast 总耗时 {wall:.1f}s 未受预算约束")
        # 首试超时也应被压缩到预算内，而非原样传 8s
        if calls:
            self.assertLessEqual(calls[0], 2.01,
                                 f"首试超时 {calls[0]}s 未被预算收紧")

    def test_budget_compresses_later_attempts(self):
        """预算确实**压缩**后续尝试的超时（而非只是跳过尝试）。"""
        def hang(_q, _e, to):
            time.sleep(to or 8)
            return []
        wall, calls = self._exec(hang, "auto", budget=2.0)
        if len(calls) >= 2:
            # 第二次可用超时 = 剩余预算 < 首试
            self.assertLess(calls[1], calls[0],
                            f"后续尝试未收缩：{calls}")

    def test_normal_engine_unaffected(self):
        """快引擎不受预算影响（防误杀）。"""
        def fast(_q, eng, to):
            time.sleep(0.05)
            return _good("fast")
        wall, calls = self._exec(fast, "auto")
        self.assertLess(wall, 1.0, f"快引擎被拖慢：{wall:.2f}s")
        self.assertEqual(len(calls), 1, "合格首试不应重试")


class TestBudgetConstant(unittest.TestCase):
    """守卫真实默认值：防止有人把预算调得过大（失去保护）或过小（误杀）。"""

    def test_default_budget_is_sane(self):
        b = search._PER_ENGINE_BUDGET_S
        self.assertGreaterEqual(b, 8.0,
                                f"预算 {b}s 小于 default_timeout(8s)，会误杀正常源")
        self.assertLessEqual(b, 15.0,
                             f"预算 {b}s 过大，用户体感仍会「卡半分钟」")


class TestBackupFailover(unittest.TestCase):
    """慢主引擎 + 快备选：备选结果应被采用（用户要求的「切备选源」）。"""

    def _exec(self, fake, parallel: bool = True):
        calls: list[str] = []

        def _spy(query_, eng, **kw):
            calls.append(eng)
            # 与 TestPerEngineBudget 同一计算方式：把 timeout 传给 fake，
            # 否则 fake 签名带 to 会 TypeError（被引擎层吞掉→静默空结果）。
            return fake(query_, eng, kw.get("timeout"))

        combo = ["slow_primary", "fast_backup"]
        dec = {"engines_combo": combo, "engines": combo, "parallel": parallel,
               "domain": "general_search", "engine": combo[0],
               "features": {"primary_lang": "zh"}}
        with ExitStack() as stack:
            for p in (
                patch("search.engine_search", side_effect=_spy),
                patch("circuit_breaker.get_breaker", return_value=_AllowAllBreaker()),
                patch("quota.get_quota_manager", return_value=MagicMock()),
            ):
                stack.enter_context(p)
            t0 = time.perf_counter()
            out = execute_search(QUERY, dec, 5, 8, "fast", SearchCache(":memory:"),
                                 True, mode="auto")
            wall = time.perf_counter() - t0
        return out, calls, wall

    def test_backup_raced_when_primary_slow(self):
        """主引擎超过 grace 未完成 → 补发备选并采用其结果。"""
        def fake(_q, eng, to):
            if eng == "slow_primary":
                time.sleep(min(3, to or 3))   # 远超 grace(2.0)，且尊重超时
                return _good("slow")
            if eng == "fast_backup":
                time.sleep(0.05)
                return _good("backup")
            return []

        out, calls, wall = self._exec(fake)
        self.assertIn("fast_backup", calls, "慢主引擎应触发补发备选")
        titles = " ".join(r.get("title", "") for r in out.get("results", []))
        self.assertIn("backup", titles, "应采用备选源结果")
        # 总耗时由备选决定，而非等慢主引擎跑完（6s）
        self.assertLess(wall, 2.8,
                        f"未切备选（等了主引擎）：{wall:.2f}s")


if __name__ == "__main__":
    unittest.main()
