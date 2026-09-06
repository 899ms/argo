#!/usr/bin/env python3
"""test_race_fast_budget_0906 — Wave-1 race 与 fast 总预算 单测。

2026-09-06 时延改造的回归门：
  1. race：parallel+fast 下 primary 与次引擎并行起跑，先完成且合格者赢，
     输家慢线程不被等待（shutdown(wait=False)）——primary 慢不再拖整体；
  2. fast 总预算：deadline 之后串行路径不再起新引擎（拖尾止损）；
  3. 对照：auto 模式无预算截断，行为不变。
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import search  # noqa: E402
from search import execute_search  # noqa: E402
from cache import SearchCache  # noqa: E402

QUERY = "race budget probe query"


class _AllowAllBreaker:
    """测试用：关闭熔断/负缓存干扰（与 test_route_golden_0902 同款）。"""

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
         "snippet": f"relevant snippet about {QUERY} variant {i}",
         "url": f"https://example.com/{tag}/{i}"}
        for i in range(3)
    ]


# 与查询零词面交集 → 覆盖守卫拒绝早停（逼出串行救援路径）
_GARBAGE = [
    {"title": f"zz{i}", "snippet": "unrelated words entirely", "url": f"https://x.com/{i}"}
    for i in range(4)
]


class TestWave1Race(unittest.TestCase):
    def _execute(self, decision: dict, fake, mode: str = "fast"):
        calls: list[str] = []

        def _spy(query_: str, eng: str, **kwargs):
            calls.append(eng)
            return fake(query_, eng)

        cache = SearchCache(db_path=":memory:")
        t0 = time.perf_counter()
        with (
            patch("search.engine_search", side_effect=_spy),
            patch("circuit_breaker.get_breaker", return_value=_AllowAllBreaker()),
            patch("quota.get_quota_manager", return_value=MagicMock()),
        ):
            out = execute_search(
                QUERY, decision, max_results=5, timeout=10, depth="fast",
                cache=cache, skip_cache=True, mode=mode)
        wall = time.perf_counter() - t0
        return out, calls, wall

    def _decision(self, combo: list[str], parallel: bool = True) -> dict:
        return {"engines_combo": combo, "engines": combo, "parallel": parallel,
                "domain": "general_search", "engine": combo[0]}

    def test_fast_secondary_wins_race_over_slow_primary(self):
        """primary 慢(0.45s)而次引擎快(0.02s)且合格 → 次引擎赢，墙钟不被 primary 拖住。"""
        def fake(_q, eng):
            if eng == "slow_primary":
                time.sleep(0.45)
                return _good("slow")
            if eng == "fast_second":
                time.sleep(0.02)
                return _good("fast")
            return []

        out, calls, wall = self._execute(
            self._decision(["slow_primary", "fast_second"]), fake)
        self.assertIn("fast_second", calls)
        self.assertLess(wall, 0.40,
                        f"race 未生效：墙钟 {wall:.2f}s 仍被慢 primary 拖住")
        titles = " ".join(r.get("title", "") for r in out.get("results", []))
        self.assertIn("fast", titles, "融合结果应包含赢家次引擎的结果")

    def test_fast_good_primary_early_stops_without_waiting(self):
        """控制组：primary 快且合格 → 立即早停，不等慢次引擎（成本不回退）。"""
        def fake(_q, eng):
            if eng == "quick_primary":
                time.sleep(0.02)
                return _good("quick")
            if eng == "slow_second":
                time.sleep(0.45)
                return _good("slow2")
            return []

        out, calls, wall = self._execute(
            self._decision(["quick_primary", "slow_second"]), fake)
        self.assertIn("quick_primary", calls)
        self.assertLess(wall, 0.40,
                        f"早停未跳过慢次引擎等待：墙钟 {wall:.2f}s")
        titles = " ".join(r.get("title", "") for r in out.get("results", []))
        self.assertIn("quick", titles)

    def test_race_both_bad_falls_to_wave2_rest(self):
        """race 双成员都不合格（垃圾被守卫拒）→ 落 wave-2 补跑剩余引擎。"""
        def fake(_q, eng):
            if eng in ("bad_a", "bad_b"):
                time.sleep(0.02)
                return _GARBAGE
            if eng == "rescuer":
                time.sleep(0.02)
                return _good("rescue")
            return []

        out, calls, wall = self._execute(
            self._decision(["bad_a", "bad_b", "rescuer"]), fake)
        self.assertIn("rescuer", calls, "race 全灭应触发 wave-2 补跑")
        titles = " ".join(r.get("title", "") for r in out.get("results", []))
        self.assertIn("rescue", titles)


class TestFastTotalBudget(unittest.TestCase):
    def _execute(self, decision, fake, mode, patch_budget=None):
        calls: list[str] = []

        def _spy(query_, eng, **kwargs):
            calls.append(eng)
            return fake(query_, eng)

        cache = SearchCache(db_path=":memory:")
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in (
                patch("search.engine_search", side_effect=_spy),
                patch("circuit_breaker.get_breaker", return_value=_AllowAllBreaker()),
                patch("quota.get_quota_manager", return_value=MagicMock()),
                *( [patch.object(search, "_FAST_TOTAL_BUDGET_S", patch_budget)]
                   if patch_budget is not None else [] ),
            ):
                stack.enter_context(p)
            out = execute_search(
                QUERY, decision, max_results=5, timeout=10, depth="fast",
                cache=cache, skip_cache=True, mode=mode)
        return out, calls

    def test_budget_stops_serial_rescue_after_deadline(self):
        """fast + 预算 0.25s：首引擎 0.3s 垃圾（守卫拒）→ deadline 已过，
        串行救援不再起第二引擎（原行为会跑满）。"""
        def fake(_q, eng):
            time.sleep(0.3)
            return _GARBAGE

        _out, calls = self._execute(
            {"engines_combo": ["g1", "g2", "g3"], "engines": ["g1", "g2", "g3"],
             "parallel": False, "domain": "general_search", "engine": "g1"},
            fake, mode="fast", patch_budget=0.25)
        self.assertEqual(len(calls), 1,
                         f"预算耗尽后不应再起新引擎，实际调用 {calls}")

    def test_auto_mode_has_no_budget_cutoff(self):
        """对照：auto 无预算截断，串行救援路径完整（行为不回退）。"""
        def fake(_q, eng):
            time.sleep(0.05)
            return _GARBAGE

        _out, calls = self._execute(
            {"engines_combo": ["g1", "g2", "g3"], "engines": ["g1", "g2", "g3"],
             "parallel": False, "domain": "general_search", "engine": "g1"},
            fake, mode="auto")
        self.assertGreaterEqual(len(calls), 2,
                                "auto 模式不应受 fast 预算影响")


if __name__ == "__main__":
    unittest.main()
