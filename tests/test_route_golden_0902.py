#!/usr/bin/env python3
"""P1 金标回归门（2026-09-02）：fast 早停冗余守卫 + 路由结构金标钉。

背景（重定义证据链）：
  上午实测 fast 模式英文技术查询只执行 anysearch 单引擎且结果全不相关；
  逐层复现后定位真问题——串行路径「任一引擎有结果即 break」，首引擎上游
  波动返回高计数垃圾时早停吞掉次引擎，无第二意见。auto 因跑满 combo 有
  RRF 融合而幸存。

修复：
  search.py 新增 _query_coverage_ok（查询-结果词面覆盖守卫）接入
  _sufficient_internal（wave-1/wave-2）与串行 break；只影响「是否早停」，
  不丢弃结果，最坏代价多跑一个引擎。

本文件三层断言（全部离线，mock 引擎）：
  1. 守卫单元：当日事故样本（mdn 垃圾）必须被判「不充分」；好结果不误伤；
     CJK fail-open；min_results 答案型语义不动。
  2. 执行集成：真实 route_query 决策 + mock 引擎，垃圾首引擎 → 次引擎
     被补跑且融合结果包含救援来源；好首引擎 → 早停保持（零成本不回退）。
  3. 路由金标钉：无域 fast 兜底 combo 必含 ≥2 免费通用源等结构契约。

运行：
  python3 -m pytest tests/test_route_golden_0902.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from search import (  # noqa: E402
    _query_coverage_ok,
    _results_sufficient,
    execute_search,
)
from cache import SearchCache  # noqa: E402
from route import route_query  # noqa: E402


class _AllowAllBreaker:
    """测试用：关闭熔断/负缓存干扰（与 test_new_engines 同款）。"""

    def allow(self, eng: str) -> tuple[bool, str]:
        return True, "closed"

    def get_negative(self, *args: Any, **kwargs: Any) -> None:
        return None

    def record_success(self, *args: Any, **kwargs: Any) -> None:
        return None

    def record_failure(self, *args: Any, **kwargs: Any) -> None:
        return None

    def set_negative(self, *args: Any, **kwargs: Any) -> None:
        return None

    def clear_negative(self, *args: Any, **kwargs: Any) -> None:
        return None


# ── 事故样本（2026-09-02 上午真实输出节选，mdn 噪声）─────────────────────────

QUERY = "Crawl4AI pruning content filter extraction"

MORNING_GARBAGE = [
    {"title": "The structured clone algorithm",
     "url": "https://developer.mozilla.org/en-US/docs/Web/API/Structured_clone_algorithm",
     "snippet": "The structured clone algorithm copies complex JavaScript objects.",
     "source": "anysearch"},
    {"title": "Link macros",
     "url": "https://developer.mozilla.org/en-US/docs/MDN/Writing_guidelines/Page_structures/Links",
     "snippet": "MDN provides numerous macros to create always up-to-date links.",
     "source": "anysearch"},
    {"title": "<link> HTML external resource link element",
     "url": "https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/link",
     "snippet": "The <link> HTML element specifies relationships.",
     "source": "anysearch"},
    {"title": "GitHub Discussions",
     "url": "https://developer.mozilla.org/en-US/docs/MDN/Community/Discussions",
     "snippet": "GitHub Discussions is a collaborative communication forum.",
     "source": "anysearch"},
    {"title": "Using filter effects",
     "url": "https://developer.mozilla.org/en-US/docs/Web/CSS/filter-effects",
     "snippet": "You have just hovered over a black-and-white or sepia image.",
     "source": "anysearch"},
]

GOOD_RESULTS = [
    {"title": "Fit Markdown with Pruning & BM25 - Crawl4AI documentation",
     "url": "https://docs.crawl4ai.com/core/fit-markdown/",
     "snippet": "PruningContentFilter removes noise based on link density and text density.",
     "source": "anysearch"},
    {"title": "crawl4ai/docs/md_v2/blog/releases/0.4.0.md at main - GitHub",
     "url": "https://github.com/unclecode/crawl4ai/blob/main/docs/md_v2/blog/releases/0.4.0.md",
     "snippet": "Introducing PruningContentFilter and BM25ContentFilter for extraction.",
     "source": "anysearch"},
]


# ── 1. 守卫单元 ───────────────────────────────────────────────────────────────

class TestQueryCoverageGuard(unittest.TestCase):
    def test_morning_garbage_not_sufficient_fast(self):
        """事故样本：5 条高计数 mdn 垃圾必须被判「不充分」（修复前 True）。"""
        self.assertFalse(_results_sufficient(
            MORNING_GARBAGE, mode="fast", query=QUERY))

    def test_morning_garbage_not_sufficient_auto(self):
        self.assertFalse(_results_sufficient(
            MORNING_GARBAGE, mode="auto", query=QUERY))

    def test_good_results_still_sufficient_fast(self):
        """好结果不误伤：早停成本语义保持。"""
        self.assertTrue(_results_sufficient(
            GOOD_RESULTS, mode="fast", query=QUERY))

    def test_cjk_char_level_guard(self):
        """CJK 字符级覆盖：零字符交集的垃圾同样拒早停；正常中文结果放行。"""
        garbage = [{"title": "完全无关的页面", "snippet": "随便什么内容",
                    "source": "anysearch"},
                   {"title": "另一条", "snippet": "内容", "source": "anysearch"}]
        self.assertFalse(_results_sufficient(
            garbage, mode="fast", query="苹果 手机 推荐"))
        good = [{"title": "苹果手机推荐榜单", "snippet": "2026 年最值得买的机型",
                 "source": "anysearch"},
                {"title": "苹果手机选购攻略", "snippet": "各型号对比与推荐",
                 "source": "anysearch"}]
        self.assertTrue(_results_sufficient(
            good, mode="fast", query="苹果 手机 推荐"))

    def test_empty_query_legacy_behavior(self):
        """无查询词（理论边界）→ 守卫放行，保持历史行为。"""
        self.assertTrue(_results_sufficient(
            GOOD_RESULTS, mode="fast", query=""))

    def test_min_results_answer_type_untouched(self):
        """答案型域 min_results 语义不动：1 条快照即够用（计数判定）。"""
        self.assertTrue(_results_sufficient(
            [{"title": "irrelevant title", "snippet": "body text",
              "source": "worldbank"}],
            mode="auto", min_results=1, query="china gdp 2024"))


# ── 2. 执行集成（真实路由决策 + mock 引擎）────────────────────────────────────

class TestEarlyStopRescueIntegration(unittest.TestCase):
    def _execute(self, query: str, decision: dict[str, Any], fake: Any) -> tuple[dict, list[str]]:
        calls: list[str] = []
        cache = SearchCache(db_path=":memory:")

        def _spy(query_: str, eng: str, **kwargs: Any) -> list:
            calls.append(eng)
            return fake(query_, eng, **kwargs)

        with (
            patch("search.engine_search", side_effect=_spy),
            patch("circuit_breaker.get_breaker", return_value=_AllowAllBreaker()),
            patch("quota.get_quota_manager", return_value=MagicMock()),
        ):
            out = execute_search(
                query, decision, max_results=5, timeout=5, depth="fast",
                cache=cache, skip_cache=True)
        return out, calls

    def test_garbage_primary_rescued_by_second_engine(self):
        """事故场景回放：anysearch 垃圾 → 守卫拒绝早停 → duckduckgo 补跑。"""
        decision = route_query(QUERY, mode="fast", depth="fast", context="search")
        self.assertEqual(decision["engines_combo"], ["anysearch", "local_bing"])

        def fake(_q: str, eng: str, **_k: Any) -> list:
            if eng == "anysearch":
                return MORNING_GARBAGE
            if eng == "local_bing":
                return GOOD_RESULTS
            return []

        out, calls = self._execute(QUERY, decision, fake)
        self.assertIn("local_bing", calls, "守卫应触发次引擎补跑")
        merged_titles = " ".join(r.get("title", "") for r in out.get("results", []))
        self.assertIn("Crawl4AI", merged_titles, "融合结果应包含救援来源")
        self.assertIn("local_bing", out.get("engines_used", []))

    def test_good_primary_keeps_early_stop(self):
        """控制组：首引擎好结果 → 早停保持，次引擎零调用（成本不回退）。"""
        decision = route_query(QUERY, mode="fast", depth="fast", context="search")

        def fake(_q: str, eng: str, **_k: Any) -> list:
            return GOOD_RESULTS if eng == "anysearch" else []

        _out, calls = self._execute(QUERY, decision, fake)
        self.assertNotIn("local_bing", calls, "好结果不应触发补跑")
        self.assertEqual(calls[0], "anysearch")

    def test_wave_path_parallel_rescue(self):
        """并行 wave 路径同语义：垃圾主引擎 → wave-2 补跑次引擎。"""
        decision = route_query(QUERY, mode="fast", depth="fast", context="search")
        decision = {**decision, "parallel": True}

        def fake(_q: str, eng: str, **_k: Any) -> list:
            if eng == "anysearch":
                return MORNING_GARBAGE
            if eng == "local_bing":
                return GOOD_RESULTS
            return []

        out, calls = self._execute(QUERY, decision, fake)
        self.assertIn("local_bing", calls)
        self.assertIn("local_bing", out.get("engines_used", []))


# ── 3. 路由金标钉（结构契约，与 matrix_search_eval ROUTE_MATRIX 增补同源）──────

class TestRouteGoldenPins(unittest.TestCase):
    def test_nodomain_fast_fallback_has_two_free_engines(self):
        """无域查询 fast 兜底 combo 必含 ≥2 免费通用源（单引擎无冗余即事故温床）。

        route_query 把无模式命中归一化为 catch-all 域 general_search。
        """
        d = route_query(QUERY, mode="fast", depth="fast", context="search")
        self.assertEqual(d.get("domain"), "general_search")
        combo = d.get("engines_combo") or []
        free_general = {"anysearch", "local_bing", "uapi"}
        self.assertGreaterEqual(
            len(free_general & set(combo)), 2,
            f"fast 兜底 combo 应含 ≥2 免费通用源，实际 {combo}")

    def test_english_tech_domain_hit(self):
        d = route_query("python asyncio tutorial", mode="fast", depth="fast", context="search")
        self.assertEqual(d.get("domain"), "english_tech")
        self.assertIn(d.get("engine"), ("octen", "anysearch", "duckduckgo"))

    def test_zh_food_domain_hit(self):
        d = route_query("附近好吃的本帮菜馆", mode="fast", depth="fast", context="search")
        self.assertEqual(d.get("domain"), "chinese_general")
        self.assertEqual((d.get("features") or {}).get("primary_lang"), "zh")


if __name__ == "__main__":
    unittest.main()
