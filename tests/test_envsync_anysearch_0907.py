#!/usr/bin/env python3
"""test_envsync_anysearch_0907 — env 同步 + anysearch 升权/多语言注入 + 配额口径 回归门。

2026-09-07 数据源权重盘点轮的三组修复：
  1. env 文件 → os.environ 同步（只填缺失、不覆盖已有、幂等）——兼容口径：
     读取方保持标准 os.environ 直读不动，入口同步一份过去；
  2. ja/ko 查询 anysearch 前二注入（策略/预算截断之后，防 must_keep 换位
     挤出）；english_tech/chinese_general 升权；
  3. quota_profiles 对齐服务商真实口径（zhihu 5000/anysearch 2000/
     zhihu_global 5000）+ null 引擎计数周期归零。
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import engine_env  # noqa: E402
from route import route_query  # noqa: E402
from quota import QuotaManager  # noqa: E402


class TestEnvFileSync(unittest.TestCase):
    def _sync_with_envfile(self, content: str):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        tmp.write(content)
        tmp.close()
        old_sig = engine_env._envfile_sig
        engine_env._envfile_sig = ()  # 强制重读
        try:
            with patch.object(engine_env, "_envfile_path",
                              return_value=Path(tmp.name)):
                injected = engine_env.sync_envfile_to_environ()
        finally:
            engine_env._envfile_sig = old_sig
            Path(tmp.name).unlink(missing_ok=True)
        return injected

    def test_fill_missing(self):
        injected = self._sync_with_envfile("export TEST_SYNC_A=tok_a\n")
        try:
            self.assertIn("TEST_SYNC_A", injected)
            self.assertEqual(engine_env.os.environ.get("TEST_SYNC_A"), "tok_a")
        finally:
            engine_env.os.environ.pop("TEST_SYNC_A", None)

    def test_no_overwrite_existing(self):
        engine_env.os.environ["TEST_SYNC_B"] = "keep_me"
        try:
            injected = self._sync_with_envfile("TEST_SYNC_B=file_val\n")
            self.assertNotIn("TEST_SYNC_B", injected)
            self.assertEqual(engine_env.os.environ["TEST_SYNC_B"], "keep_me")
        finally:
            engine_env.os.environ.pop("TEST_SYNC_B", None)

    def test_idempotent(self):
        injected1 = self._sync_with_envfile("TEST_SYNC_C=v1\n")
        injected2 = self._sync_with_envfile("TEST_SYNC_C=v1\n")
        try:
            self.assertIn("TEST_SYNC_C", injected1)
            self.assertEqual(injected2, [])
        finally:
            engine_env.os.environ.pop("TEST_SYNC_C", None)


class TestMultilingualAnysearchInjection(unittest.TestCase):
    """ja/ko 查询 anysearch 前二（策略截断后注入，防 must_keep 换位挤出）。"""

    def test_ja_geo_anysearch_front2(self):
        d = _isolated_route("東京 おすすめ ラーメン 屋 はどこ")
        combo = d.get("engines_combo") or []
        self.assertIn("anysearch", combo[:2],
                      f"ja 查询 anysearch 应在前二: {combo}")

    def test_ko_anysearch_front2(self):
        d = _isolated_route("서울 최고의 카페 추천 위치")
        combo = d.get("engines_combo") or []
        self.assertIn("anysearch", combo[:2],
                      f"ko 查询 anysearch 应在前二: {combo}")

    def test_ja_catchall_anysearch_front2(self):
        d = _isolated_route("東京タワー の高さ は いくつ")
        combo = d.get("engines_combo") or []
        self.assertIn("anysearch", combo[:2],
                      f"ja catch-all 也应注入 anysearch: {combo}")

    def test_zh_vertical_not_injected(self):
        d = _isolated_route("贵州茅台 股价")
        combo = d.get("engines_combo") or []
        self.assertEqual(combo[0], "sina_quote",
                         f"zh 点查域 primary 不得被注入顶掉: {combo}")

    def test_domain_primary_stays_first(self):
        d = _isolated_route("東京 おすすめ ラーメン 屋 はどこ")
        self.assertEqual(d.get("engines_combo", [None])[0],
                         "local_openstreetmap",
                         f"注入不得顶掉域主源: {d.get('reason')}")


class TestQuotaProfilesAligned(unittest.TestCase):
    """对齐服务商面板真实口径（2026-09-06）：知乎搜索 5000/天、AnySearch
    2000/天、知乎全网搜 5000/天。修复前 zhihu=1000 会在本地提前封禁
    （浪费 80% 额度）、anysearch=null 裸奔无保护。"""

    def setUp(self):
        self.qm = QuotaManager()

    def test_limits(self):
        cases = {"zhihu": 5000, "anysearch": 2000, "zhihu_global": 5000}
        for eng, limit in cases.items():
            p = self.qm._profiles.get(eng, {})
            self.assertEqual(p.get("limit"), limit, eng)
            self.assertEqual(p.get("period"), "day", eng)

    def test_null_limit_engine_counter_resets_periodically(self):
        """null 引擎 get_remaining_ratio 也要按周期归零（原实现提前 return
        跳过重置，遥测永久累计）。"""
        qm = self.qm
        qm._state["_test_null_eng"] = {
            "used": 123, "errors": 0, "calls": [],
            "last_reset": time.time() - 2 * 86400,
        }
        with patch.object(qm, "_save_state", lambda: None):
            ratio = qm.get_remaining_ratio("_test_null_eng")
        try:
            self.assertEqual(ratio, 1.0)
            self.assertEqual(qm._state["_test_null_eng"]["used"], 0,
                             "null 引擎计数未按周期归零")
        finally:
            qm._state.pop("_test_null_eng", None)


class _AllowAllBreaker:
    """隔离共享熔断/配额状态：combo 断言只验路由语义，不验运行时健康。"""

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


def _isolated_route(query: str, **kwargs):
    """route_query 但打桩熔断器与配额（并发 live 评测会写共享状态文件，
    直连真实单例会让 combo 断言偶发抖动）。"""
    from unittest.mock import MagicMock
    with patch("circuit_breaker.get_breaker",
               return_value=_AllowAllBreaker()), \
         patch("quota.get_quota_manager", return_value=MagicMock()):
        return route_query(query, **kwargs)


class TestZhihuGlobalUtilization(unittest.TestCase):
    """zhihu_global（全网搜 SearchDB=all，5000/天）防饿死回归门。

    死因链：learner 同族按分重排把它挪到 anysearch 之后 + auto 预算=2 截断
    → 自家主域永远轮不上（37 天仅 53 次）。修复：zh 查询下 zhihu_content
    固定 [zhihu, zhihu_global] 成对、learner 过滤豁免；news_realtime 接入 #2。
    """

    def test_zh_opinion_pair(self):
        d = _isolated_route("怎么看待 AI 编程工具取代程序员")
        combo = d.get("engines_combo") or []
        self.assertEqual(combo[:2], ["zhihu", "zhihu_global"],
                         f"观点查询应为站内+全网搜成对: {combo}")

    def test_news_intent_pair(self):
        d = _isolated_route("新能源车 销量 最新新闻")
        self.assertEqual(d.get("domain"), "news_realtime")
        combo = d.get("engines_combo") or []
        self.assertIn("zhihu_global", combo[:2],
                      f"新闻意图 zhihu_global 应在前二: {combo}")

    def test_error_item_not_silent_empty(self):
        """HTTP 失败必须返回 error item（原静默 []，把鉴权失败伪装成没结果）。"""
        import engines
        import urllib.error
        env = patch.dict("os.environ", {"ZHIHU_ACCESS_SECRET": "test_secret"})
        with env, patch("urllib.request.urlopen",
                        side_effect=urllib.error.HTTPError(
                            "u", 401, "Unauthorized", {}, None)):
            res = engines.search("测试查询", "zhihu_global", n=5, timeout=5)
        self.assertTrue(res and isinstance(res[0], dict) and "error" in res[0],
                        f"HTTP 401 应产生 error item: {res}")


class TestZhihuUserEngine(unittest.TestCase):
    """zhihu_user（个人数据：本人创作/收藏/关注）接入回归门。

    查本人数据用 Access Secret 直调无需 OAuth；路由上须压过 social 泛域
    （收藏/关注是社交平台通用功能词）与 zhihu_content（知乎词面）。
    """

    def test_registered(self):
        from config import load_config, get_engines
        spec = get_engines(load_config()).get("zhihu_user")
        self.assertIsNotNone(spec, "zhihu_user 未注册")
        self.assertEqual(spec.get("type"), "zhihu_user")
        self.assertEqual(spec.get("family"), "personal_data")

    def test_routing_trio_distinct(self):
        """站内/站外/个人数据三源语义各自路由（知乎源分工的根）。"""
        d1 = route_query("怎么看待 AI 编程工具取代程序员")
        self.assertEqual(d1.get("domain"), "zhihu_content", "观点→站内")
        d2 = route_query("我的收藏")
        self.assertEqual(d2.get("domain"), "zhihu_user_data", "个人数据意图→个人域")
        d3 = route_query("我的知乎")
        self.assertEqual(d3.get("domain"), "zhihu_user_data")

    def test_sub_intent_parsing(self):
        from engines_builders_cn import _parse_zhihu_user_intent
        cases = [
            ("我的回答 点赞最多", ("contents", {"ContentType": "answer"})),
            ("我的文章", ("contents", {"ContentType": "article"})),
            ("我的收藏", ("collections", {})),
            ("我的收藏夹", ("favlists", {})),
            ("我关注的人", ("followees", {})),
            ("我的知乎", ("contents", {"ContentType": "all"})),
        ]
        for q, (endpoint, extra) in cases:
            got_endpoint, got_extra, _ = _parse_zhihu_user_intent(q)
            self.assertEqual((got_endpoint, got_extra), (endpoint, extra), q)

    def test_error_item_not_silent_empty(self):
        import engines
        import urllib.error
        env = patch.dict("os.environ", {"ZHIHU_ACCESS_SECRET": "test_secret"})
        with env, patch("urllib.request.urlopen",
                        side_effect=urllib.error.HTTPError(
                            "u", 401, "Unauthorized", {}, None)):
            res = engines.search("我的回答", "zhihu_user", n=5, timeout=5)
        self.assertTrue(res and isinstance(res[0], dict) and "error" in res[0],
                        f"HTTP 401 应产生 error item: {res}")


if __name__ == "__main__":
    unittest.main()
