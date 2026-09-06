#!/usr/bin/env python3
"""test_race_fast_budget_0906 — Wave-1 hedged（首发+分岔）与 fast 总预算 单测。

2026-09-07 hedged 改造后回归门：
  1. hedged cost-win：primary 在 grace 窗内完成且合格 → 只付 1 次调用（原 race
     固定 2 次），次引擎不被启动（成本回退消除）；
  2. hedged latency-win：primary 未在 grace 窗内完成 → 补发次引擎并行 race，
     先合格者赢，慢 primary 不拖整体（延迟收益保留）；
  3. 弃置线程 daemon 化：赢家早停后弃置的慢线程不再阻塞进程退出（修掉原
     shutdown(wait=False) 的「函数内快、进程级假快」）；
  4. fast 总预算：deadline 之后串行路径不再起新引擎（拖尾止损）；
  5. 对照：auto 模式无预算截断，行为不变。
"""
from __future__ import annotations

import subprocess
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

QUERY = "race budget probe query"
SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")


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


class TestWave1Hedged(unittest.TestCase):
    def _execute(self, decision: dict, fake, mode: str = "fast",
                 patch_grace: float | None = None,
                 patch_budget: float | None = None):
        calls: list[str] = []

        def _spy(query_: str, eng: str, **kwargs):
            calls.append(eng)
            return fake(query_, eng)

        cache = SearchCache(db_path=":memory:")
        t0 = time.perf_counter()
        with ExitStack() as stack:
            for p in (
                patch("search.engine_search", side_effect=_spy),
                patch("circuit_breaker.get_breaker", return_value=_AllowAllBreaker()),
                patch("quota.get_quota_manager", return_value=MagicMock()),
                *([patch.object(search, "_PRIMARY_GRACE_S", patch_grace)]
                  if patch_grace is not None else []),
                *([patch.object(search, "_FAST_TOTAL_BUDGET_S", patch_budget)]
                  if patch_budget is not None else []),
            ):
                stack.enter_context(p)
            out = execute_search(
                QUERY, decision, max_results=5, timeout=10, depth="fast",
                cache=cache, skip_cache=True, mode=mode)
        wall = time.perf_counter() - t0
        return out, calls, wall

    def _decision(self, combo: list[str], parallel: bool = True) -> dict:
        return {"engines_combo": combo, "engines": combo, "parallel": parallel,
                "domain": "general_search", "engine": combo[0]}

    def test_primary_within_grace_no_backup_cost_win(self):
        """hedged 成本赢：primary 在 grace 窗内完成且合格 → 只付 1 次调用，
        次引擎不被启动（原 race 固定 2 次，这里证明成本回退已消除）。"""
        def fake(_q, eng):
            if eng == "ok_primary":
                time.sleep(0.05)
                return _good("okp")
            if eng == "fast_second":
                time.sleep(0.02)
                return _good("fs")
            return []

        out, calls, wall = self._execute(
            self._decision(["ok_primary", "fast_second"]), fake)
        self.assertEqual(calls, ["ok_primary"], f"grace 内合格 primary 不应启用 backup：{calls}")
        self.assertLess(wall, 0.6, f"hedge 成本赢被破坏：墙钟 {wall:.2f}s")
        self.assertIn("okp", " ".join(r.get("title", "") for r in out["results"]))

    def test_slow_primary_hedges_backup_and_races(self):
        """hedged 延迟赢：primary 未在 grace 窗内完成 → 补发次引擎并行 race，
        先合格者赢，墙钟由 backup 决定（约 grace+backup，而非 primary 全程）。"""
        def fake(_q, eng):
            if eng == "slow_primary":
                time.sleep(3.5)
                return _good("slow")  # 慢（>grace 0.3s），结果合格但来不及
            if eng == "fast_backup":
                time.sleep(0.02)
                return _good("backup")
            return []

        out, calls, wall = self._execute(
            self._decision(["slow_primary", "fast_backup"]), fake, patch_grace=0.3)
        self.assertIn("fast_backup", calls, "慢 primary 应触发 hedging 补发 backup")
        self.assertIn("slow_primary", calls, "primary 已启动")
        self.assertLess(wall, 1.0, f"wall 应≈grace+backup，实际 {wall:.2f}s")
        titles = " ".join(r.get("title", "") for r in out.get("results", []))
        self.assertIn("backup", titles, "融合结果应包含赢家 backup 的结果")

    def test_slow_primary_wins_over_hedge_backup(self):
        """hedged 无早停（no_early）时：两成员都收，primary 虽慢仍被采集。"""
        def fake(_q, eng):
            if eng == "slow_primary":
                time.sleep(0.5)
                return _good("slow")
            if eng == "fast_backup":
                time.sleep(0.02)
                return _good("backup")
            return []

        decision = self._decision(["slow_primary", "fast_backup"])
        decision["no_early_stop"] = True
        out, calls, wall = self._execute(decision, fake, patch_grace=0.2)
        self.assertEqual(set(calls), {"slow_primary", "fast_backup"})
        titles = " ".join(r.get("title", "") for r in out.get("results", []))
        self.assertIn("slow", titles, "no_early 下慢 primary 结果也应被采集")

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
        self.assertLess(wall, 0.4, f"早停未跳过慢次引擎等待：墙钟 {wall:.2f}s")
        self.assertIn("quick", " ".join(r.get("title", "") for r in out["results"]))

    def test_hedge_both_bad_falls_to_wave2(self):
        """hedge 双成员都不合格（垃圾被守卫拒）→ 落 wave-2 补跑剩余引擎。"""
        def fake(_q, eng):
            if eng in ("bad_a", "bad_b"):
                time.sleep(0.02)
                return _GARBAGE
            if eng == "rescuer":
                time.sleep(0.02)
                return _good("rescue")
            return []

        out, calls, wall = self._execute(
            self._decision(["bad_a", "bad_b", "rescuer"]), fake, patch_grace=0.2)
        self.assertIn("rescuer", calls, "hedge 全灭应触发 wave-2 补跑")
        self.assertIn("rescue", " ".join(r.get("title", "") for r in out["results"]))

    def test_hedge_race_timeout_marks_and_closes_budget(self):
        """hedge 双成员都超 race 窗（fast 预算 0.5s 处截停）→ 双双标 timeout；
        wave-2 不得在 deadline 后再起新引擎（2026-09-07 预算收口回归门，
        与串行路径 `time.time() >= _deadline` 止损同语义）。"""
        def fake(_q, eng):
            if eng in ("slow_bad_a", "slow_bad_b"):
                # 0.6s > patched budget 0.5s：race 窗在预算处截停时仍未完成
                time.sleep(0.6)
                return _GARBAGE
            if eng == "rescuer":
                time.sleep(0.02)
                return _good("rescue")
            return []

        out, calls, wall = self._execute(
            self._decision(["slow_bad_a", "slow_bad_b", "rescuer"]), fake,
            patch_grace=0.3, patch_budget=0.5)
        self.assertNotIn("rescuer", calls,
                         "fast 预算耗尽后 wave-2 不得再起新引擎")
        timeout_engines = {
            o.get("engine") for o in out.get("engine_outcomes", [])
            if o.get("status") == "timeout"}
        self.assertTrue({"slow_bad_a", "slow_bad_b"} <= timeout_engines,
                        f"超窗成员应标 timeout: {out.get('engine_outcomes')}")
        self.assertLess(wall, 2.0, f"预算收口失效（拖尾）：{wall:.2f}s")

    def test_auto_mode_wave2_runs_without_budget_cutoff(self):
        """对照：auto 无预算 → hedge 双成员超窗完成后均不合格，wave-2 正常
        补跑 rescuer（证明 fast 收口只影响 fast 语义）。"""
        def fake(_q, eng):
            if eng in ("slow_bad_a", "slow_bad_b"):
                time.sleep(0.6)
                return _GARBAGE
            if eng == "rescuer":
                time.sleep(0.02)
                return _good("rescue")
            return []

        out, calls, _ = self._execute(
            self._decision(["slow_bad_a", "slow_bad_b", "rescuer"]), fake,
            mode="auto", patch_grace=0.3)
        self.assertIn("rescuer", calls, "auto 模式无预算截断，wave-2 应补跑")
        self.assertIn("rescue", " ".join(
            r.get("title", "") for r in out["results"]))

    def test_abandoned_slow_primary_does_not_block_process_exit(self):
        """E2E：赢家早停后弃置的慢 primary 线程必须不阻塞进程退出。

        子进程内跑 execute_search（fast+parallel，primary 睡 8s、backup 0.02s），
        backup 赢、primary 弃置。若弃置线程非 daemon（如原 shutdown(wait=False)
        的 ThreadPoolExecutor），进程退出会被 atexit join 拖到 8s；daemon 化后
        进程在 ~grace+backup 内退出。断言子进程在 5s 内返回，即证明「假快」已修。
        """
        script = f"""
import sys, time
sys.path.insert(0, {SCRIPTS!r})
from unittest.mock import patch, MagicMock
import search
from cache import SearchCache

QUERY = "e2e abort probe"
class _Allow:
    def allow(self, eng): return True, "closed"
    def get_negative(self, *a, **k): return None
    def status(self, eng): return {{"state": "closed"}}
    def record_success(self, *a, **k): pass
    def record_failure(self, *a, **k): pass
    def set_negative(self, *a, **k): pass
    def clear_negative(self, *a, **k): pass

def good(tag):
    return [{{"title": f"{QUERY} {{tag}} {{i}}", "snippet": "relevant " + QUERY + " " + str(i), "url": f"https://e.com/{{tag}}/{{i}}"}} for i in range(3)]

def fake(_q, eng, **kwargs):
    if eng == "slow":
        time.sleep(8.0)   # 远超 grace；赢家早停后此线程被弃置
    if eng == "fast":
        time.sleep(0.02)
    return good(eng)

decision = {{"engines_combo": ["slow", "fast"], "engines": ["slow", "fast"],
             "parallel": True, "domain": "general_search", "engine": "slow"}}
cache = SearchCache(db_path=":memory:")
with patch("search.engine_search", side_effect=fake), \\
     patch("circuit_breaker.get_breaker", return_value=_Allow()), \\
     patch("quota.get_quota_manager", return_value=MagicMock()), \\
     patch.object(search, "_PRIMARY_GRACE_S", 0.3):
    out = search.execute_search(QUERY, decision, max_results=5, timeout=10,
                                depth="fast", cache=cache, skip_cache=True, mode="fast")
titles = " ".join(r.get("title", "") for r in out.get("results", []))
print("E2E_OK" if "fast" in titles else "E2E_BAD")
"""
        t0 = time.perf_counter()
        try:
            r = subprocess.run([sys.executable, "-c", script], capture_output=True,
                               text=True, timeout=5)
        except subprocess.TimeoutExpired:
            self.fail("弃置线程阻塞了进程退出：daemon 化未生效（慢线程被 join）")
        wall = time.perf_counter() - t0
        self.assertIn("E2E_OK", r.stdout,
                      f"子进程应返回赢家结果，stdout={r.stdout!r} stderr={r.stderr!r}")
        self.assertLess(wall, 5.0, f"子进程应快速退出，实际 {wall:.2f}s")


class TestFastTotalBudget(unittest.TestCase):
    def _execute(self, decision, fake, mode, patch_budget=None):
        calls: list[str] = []

        def _spy(query_, eng, **kwargs):
            calls.append(eng)
            return fake(query_, eng)

        cache = SearchCache(db_path=":memory:")
        with ExitStack() as stack:
            for p in (
                patch("search.engine_search", side_effect=_spy),
                patch("circuit_breaker.get_breaker", return_value=_AllowAllBreaker()),
                patch("quota.get_quota_manager", return_value=MagicMock()),
                *([patch.object(search, "_FAST_TOTAL_BUDGET_S", patch_budget)]
                   if patch_budget is not None else []),
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
        self.assertEqual(len(calls), 1, f"预算耗尽后不应再起新引擎，实际调用 {calls}")

    def test_auto_mode_has_no_budget_cutoff(self):
        """对照：auto 无预算截断，串行救援路径完整（行为不回退）。"""
        def fake(_q, eng):
            time.sleep(0.05)
            return _GARBAGE

        _out, calls = self._execute(
            {"engines_combo": ["g1", "g2", "g3"], "engines": ["g1", "g2", "g3"],
             "parallel": False, "domain": "general_search", "engine": "g1"},
            fake, mode="auto")
        self.assertGreaterEqual(len(calls), 2, "auto 模式不应受 fast 预算影响")


if __name__ == "__main__":
    unittest.main()
