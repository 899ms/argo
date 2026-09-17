#!/usr/bin/env python3
"""代码审查补丁的回归门（2026-09-17）。

## 这一批锁的是什么

一轮代码审查在 argo 主路径上找到六处「看起来对、量起来错」的缺陷。它们的
共同形态不是崩溃，而是**自洽性被破坏**：报出来的数字之间互相矛盾，于是读它
的人（包括下一轮的优化决策）会指向错误的对象。这类缺陷没有测试就一定会复发，
所以每一条都在这里留一道门。

| 缺陷 | 症状 | 本文件的门 |
|------|------|-----------|
| route 为读空 dict 付整笔 `engines` 导入 | 每次进程启动白付约 15 ms | `TestSpecsSnapshotDoesNotImportEngines` |
| 超时路径漏记 `engine_latency` | 全引擎超时时报「跑了 0 个引擎」 | `TestTimeoutPathKeepsLatency` |
| `partial` 被当失败 + 写负缓存 | 交付了结果的引擎被自己毒掉 30s | `TestPartialOutcomeIsNotAFailure` |
| `cache.stats` 把 L2 命中重复计进分母 | L2 越有效命中率报得越低 | `TestCacheStatsSelfConsistency` |
| deep 并行分支不认总预算 | `used_ms > total_ms` | `TestDeepParallelHonorsBudget` |

## 写门的纪律

每条门都要能在**改造前**的代码上失败。`_specs_snapshot` 与 deep 预算两条用的是
「两个世界可区分」的断言（不是「没崩就行」），`partial` 那条断言的是「哪个
回调被调到、哪个没被调到」，都是变异即红。
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import search  # noqa: E402
from cache import SearchCache  # noqa: E402
from search import StageTiming, execute_search  # noqa: E402
from engine_dispatch import classify_engine_outcome  # noqa: E402

QUERY = "中国的通胀率是多少"


def _good(tag: str, n: int = 3) -> list:
    return [
        {"title": f"{QUERY} 数据 {tag} {i}",
         "snippet": f"关于{QUERY}的说明 {i}",
         "url": f"https://example.com/{tag}/{i}"}
        for i in range(n)
    ]


class _RecordingBreaker:
    """把熔断侧的每一次调用记下来——门下的是「调到哪个、没调到哪个」。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def _log(self, name, *a):
        self.calls.append((name, a[0] if a else ""))

    def allow(self, *a):
        self._log("allow", *a)
        return True, ""

    def get_negative(self, *a):
        return None

    def status(self, *a):
        return {"state": "closed"}

    def record_success(self, *a):
        self._log("record_success", *a)

    def record_failure(self, *a, **_k):
        self._log("record_failure", *a)

    def record_note(self, *a, **_k):
        self._log("record_note", *a)

    def set_negative(self, *a, **_k):
        self._log("set_negative", *a)

    def clear_negative(self, *a):
        self._log("clear_negative", *a)

    def called(self, name: str) -> list[str]:
        return [eng for fn, eng in self.calls if fn == name]


class _AllowAllBreaker(_RecordingBreaker):
    pass


class _DispatchBase(unittest.TestCase):
    def _run(self, fake, engines, *, depth="fast", mode="fast", parallel=False,
             timeout=10, breaker=None, auto_budget=None):
        calls: list[str] = []

        def _spy(q, eng, **kw):
            calls.append(eng)
            return fake(q, eng)

        cache = SearchCache(db_path=":memory:")
        decision = {"engines_combo": list(engines), "engines": list(engines),
                    "parallel": parallel, "domain": "macro_data",
                    "engine": engines[0], "early_stop_min_results": None,
                    "no_early_stop": False}
        patches = [
            patch("search.engine_search", side_effect=_spy),
            patch("circuit_breaker.get_breaker",
                  return_value=breaker or _AllowAllBreaker()),
            patch("quota.get_quota_manager", return_value=MagicMock()),
        ]
        if auto_budget is not None:
            patches.append(patch.object(search, "_AUTO_TOTAL_BUDGET_S", auto_budget))
        t0 = time.perf_counter()
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            out = execute_search(
                QUERY, decision, max_results=5, timeout=timeout, depth=depth,
                cache=cache, skip_cache=True, mode=mode, timing=StageTiming())
        return out, calls, time.perf_counter() - t0


class TestSpecsSnapshotDoesNotImportEngines(unittest.TestCase):
    """route 不为一个空 dict 付 `engines`（连带 urllib/http.client）的导入钱。

    `engines._engine_specs` 只在 registry 被 `_load_registry()` 填充，而 route
    从不加载 registry——旧写法 `from engines import _engine_specs` 付了整笔
    导入、拿回空表。门断言的是「route 走完，engines 没进过 sys.modules」，
    以及「engines 真被导入过时取到的是同一个对象」（语义不变的那一半）。
    """

    def test_route_never_imports_engines(self):
        code = textwrap.dedent("""
            import sys
            sys.path.insert(0, %r)
            import route
            snap = route._specs_snapshot()
            assert snap == {}, snap
            assert "engines" not in sys.modules, "route 为了空 dict 导入了 engines"
            route.route_query("python 怎么读 csv", engine_override="auto")
            assert "engines" not in sys.modules, "route_query 导入了 engines"
            print("OK")
        """) % str(SCRIPTS)
        p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, timeout=120)
        self.assertIn("OK", p.stdout, f"stdout={p.stdout!r} stderr={p.stderr[-800:]!r}")

    def test_reads_the_live_specs_once_engines_is_loaded(self):
        """已加载时取到的是同一个 dict——「不导入」不是「永远拿空的」。"""
        import engines
        import route
        engines.get_registry()
        snap = route._specs_snapshot()
        self.assertIs(snap, engines._engine_specs)
        self.assertTrue(snap, "registry 已加载，specs 不该还是空的")


class TestTimeoutPathKeepsLatency(_DispatchBase):
    """超时收尾必须和正常收尾记同一份账。

    改造前 `_settle_pending` 的 is_alive 分支手写两行，漏掉 engine_latency；
    而 `timing.dispatch.engines_run` / `parallel_sufficiency` 全从它推——
    一轮里引擎全部超时时会报出 engines_run=0，恰是最该被看见的那一轮。
    """

    def test_timed_out_engines_are_counted_as_run(self):
        def fake(_q, eng):
            time.sleep(5.0)          # 远超下面的等待窗，必然被标 timeout
            return []

        # 走 deep 全量并行分支（≥2 个引擎才会 parallel=True），由**总预算**
        # 逼出超时收尾——per-engine 预算由 config 的 per_engine_budget_s 管，
        # 测试不碰它。
        out, calls, _wall = self._run(
            fake, ["hang_a", "hang_b"], depth="deep", mode="auto",
            parallel=True, timeout=1, auto_budget=0.3)
        d = out["timing"]["dispatch"]
        self.assertEqual(d["engines_run"], len(calls),
                         f"超时引擎没有计入 engines_run：{d} calls={calls}")
        self.assertGreater(d["engines_run"], 0,
                           f"全部超时时报出「跑了 0 个引擎」：{d}")
        self.assertGreater(d["engine_sum_ms"], 0, f"engine_sum_ms 丢账：{d}")


class TestPartialOutcomeIsNotAFailure(_DispatchBase):
    """`partial`（有结果 + 有错误条目）是贡献者，不是失败。

    改造前它落到熔断的 else 分支：记 kind=error 的失败（累计 opens，可能把
    正常交付的引擎推向 auto-disable），并写进负缓存 30s——同一个查询再搜时
    直接跳过该引擎，把它自己刚交出来的结果丢掉。
    """

    def test_partial_is_recorded_as_success_and_not_negative_cached(self):
        def fake(_q, eng):
            return _good("p") + [{"error": "上游 502", "source": eng}]

        self.assertEqual(
            classify_engine_outcome("mixed", fake("q", "mixed"), 10)["status"],
            "partial", "前置条件：混合结果应当归类为 partial")

        breaker = _RecordingBreaker()
        out, calls, _wall = self._run(fake, ["mixed"], breaker=breaker)
        self.assertIn("mixed", calls)
        self.assertIn("mixed", breaker.called("record_success"),
                      f"partial 没被记成成功：{breaker.calls}")
        self.assertNotIn("mixed", breaker.called("record_failure"),
                         f"partial 被记成了失败：{breaker.calls}")
        self.assertNotIn("mixed", breaker.called("set_negative"),
                         f"partial 被写进了负缓存：{breaker.calls}")
        self.assertTrue(out["results"], "partial 的结果没有交付")


class TestCacheStatsSelfConsistency(unittest.TestCase):
    """命中率的分母不能把 L2 命中重复计一次。

    `hits + misses` 必须恒等于总查找次数（L1 命中 + L1 未命中）；而一次
    「L1 未命中、L2 命中」只算一次查找、一次命中。改造前 hits 含 L2 命中、
    分母却用 l1.misses，同一笔查找在分子分母里各出现一次，L2 越有效报得越低。
    """

    def test_hits_plus_misses_equals_lookups(self):
        c = SearchCache(db_path=":memory:")
        c.set("q1", "e1", 5, {"v": 1}, domain="d", mode="m", depth="f")

        def read():
            return c.get("q1", "e1", 5, domain="d", mode="m", depth="f")

        read()                      # L1 命中
        c._l1._store.clear()        # 只清 L1 条目，不动统计计数
        read()                      # L1 未命中 → L2 命中
        c.get("nope", "e1", 5, domain="d", mode="m", depth="f")   # 真 miss

        s = c.stats
        lookups = s["l1"]["hits"] + s["l1"]["misses"]
        self.assertEqual(s["hits"] + s["misses"], lookups,
                         f"命中率分子分母不对齐：{s}")
        self.assertEqual(s["misses"], s["l2"]["misses"],
                         f"真 miss 应当就是 L2 的 miss：{s}")

    def test_l2_hit_is_counted_once(self):
        """单次「L1 空 + L2 命中」→ 命中率必须是 1.0，不是 0.5。"""
        c = SearchCache(db_path=":memory:")
        c.set("q1", "e1", 5, {"v": 1}, domain="d", mode="m", depth="f")
        c._l1._store.clear()
        c.get("q1", "e1", 5, domain="d", mode="m", depth="f")
        s = c.stats
        self.assertEqual(s["hits"], 1, f"L2 命中没被算作命中：{s}")
        self.assertEqual(s["misses"], 0, f"L2 命中被当成了 miss：{s}")
        self.assertEqual(s["hit_rate"], 1.0, f"命中率算错：{s}")


class TestDeepParallelHonorsBudget(_DispatchBase):
    """depth=deep 的全量并行分支也要认总墙钟预算。

    改造前这一支直接等 `net_timeout + 2`，而 budget_s 是按 mode 定的
    （--depth deep 时 mode 仍是 auto，10s）——于是同一轮里
    used_ms 可以大于 total_ms，读起来自相矛盾。
    """

    def test_deep_parallel_stops_at_the_budget(self):
        def fake(_q, eng):
            time.sleep(5.0)
            return []

        budget = 0.3
        _out, calls, wall = self._run(
            fake, ["hang_a", "hang_b"], depth="deep", mode="auto", parallel=True,
            timeout=10, auto_budget=budget)
        self.assertEqual(len(calls), 2, "前置条件：deep 并行分支应当同时起跑两个引擎")
        # 不认预算时墙钟会走到 net_timeout+2（≥12s）；认预算时应贴着 0.3s。
        self.assertLess(wall, 2.0,
                        f"deep 并行分支没有在总预算处收手：墙钟 {wall:.2f}s "
                        f"（预算 {budget}s）")


class TestLauncherSyncsEnvFileBeforePickingPython(unittest.TestCase):
    """`bin/argo` 必须**先**同步 env 文件、**再**选解释器。

    `_pick_python` 认的权威覆盖就是 ARGO_PYTHON，而它读的是 `os.environ`。
    同步若排在后面，用户照文档把 `ARGO_PYTHON=/opt/python312/bin/python3` 写进
    `~/.config/argo/env` 就等于没写——而 `mcp_launch.sh` 是「先 source 再探测」
    （line 32 早于 line 67），两个启动器对同一个文件给出不同答案。这个变量只在
    「当前解释器不够用」时才被读到，而那恰恰是用户会去写它的情形。

    为什么用源码顺序断言而不是行为断言：要构造「当前解释器不够用」得先有一个
    缺依赖的解释器，在 CI 上不可移植。这条不变量本身就是**顺序**，直接锁顺序。
    """

    def test_env_sync_precedes_interpreter_selection(self):
        src = (ROOT / "bin" / "argo").read_text(encoding="utf-8")
        sync_at = src.find("sync_envfile_to_environ()")
        pick_at = src.find("else _pick_python()")
        self.assertNotEqual(sync_at, -1, "找不到 sync_envfile_to_environ() 调用点")
        self.assertNotEqual(pick_at, -1, "找不到 _pick_python() 调用点")
        self.assertLess(
            sync_at, pick_at,
            "env 文件同步排在了选解释器之后——env 文件里的 ARGO_PYTHON 会被忽略")


if __name__ == "__main__":
    unittest.main()
