#!/usr/bin/env python3
"""test_cache_engine_isolation.py — 软命中（find_similar）的引擎隔离回归测试。

背景（v2.4.2 修复）：SearchCache.get 的语义软命中分支调用
`self._l2.find_similar(query, engine, domain)` 时传了 engine，但
find_similar 的 SQL WHERE 子句只过滤 domain、从未使用 engine 形参。
后果是软命中跨引擎串味，实测两类：

  1. `argo search --engine v2ex` 可命中 bilibili 的缓存载荷
     （结果 source 全变成 bilibili）；
  2. fetch 与 evidence 同 domain 下 URL 词面高度相似
     （`.../x` 与 `.../x.md` 相似度 0.875；两个不同站点 URL 相似度 0.75）
     会互相串正文——抓 A 站可能拿到 B 站内容。

另修一处标记丢失：软命中回填 L1 时存的是他人原始 s_hit，缺少
_semantic_hit / _semantic_similarity 等标记，导致 L1 二次命中
伪装成硬命中，调用方无法区分「精确命中」与「相似查询命中」。

本文件锁定三条契约：engine 精确隔离、auto 显式通配、标记不丢失。
"""

import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import cache  # noqa: E402


@pytest.fixture()
def sc(tmp_path):
    """独立 SQLite 缓存（不碰真实 ~/.cache）。"""
    return cache.SearchCache(db_path=str(tmp_path / "cache.db"))


def _payload(title, url):
    return {"results": [{"title": title, "url": url}]}


class TestEngineIsolation:
    """engine 维度必须参与软命中过滤。"""

    def test_similar_does_not_cross_engines(self, sc):
        """同 domain、不同 engine、查询词面相似 → 各自只看自己的缓存。"""
        sc.set("苹果 2025 营收", "eastmoney", 10,
               _payload("东财", "https://e/1"), domain="financial")
        sc.set("苹果 2025 年营收", "xueqiu", 10,
               _payload("雪球", "https://x/1"), domain="financial")

        q = "苹果 2025 年营收预测"  # 与两条都相似，但与两者都不完全相同
        got_em = {c["query"] for c in sc._l2.find_similar(q, "eastmoney", "financial", limit=50)}
        got_xq = {c["query"] for c in sc._l2.find_similar(q, "xueqiu", "financial", limit=50)}

        assert got_em == {"苹果 2025 营收"}, f"eastmoney 只应看见自己的缓存，实际 {got_em}"
        assert got_xq == {"苹果 2025 年营收"}, f"xueqiu 只应看见自己的缓存，实际 {got_xq}"

    def test_auto_is_explicit_wildcard(self, sc):
        """engine='auto' 是显式通配：调用方声明不关心来源时可见全部。"""
        sc.set("苹果 2025 营收", "eastmoney", 10,
               _payload("东财", "https://e/1"), domain="financial")
        sc.set("苹果 2025 年营收", "xueqiu", 10,
               _payload("雪球", "https://x/1"), domain="financial")

        got = {c["query"] for c in sc._l2.find_similar(
            "苹果 2025 年营收预测", "auto", "financial", limit=50)}
        assert got == {"苹果 2025 营收", "苹果 2025 年营收"}

    def test_unknown_engine_sees_nothing(self, sc):
        """不存在的引擎名 → 空结果（修复前会返回任意引擎的缓存）。"""
        sc.set("小红书 AI 绘画 评价", "bilibili", 10,
               _payload("B站", "https://b/1"), domain="social")
        got = sc._l2.find_similar("小红书 AI 绘画 评价精选", "不存在的引擎", "social", limit=50)
        assert got == [], "未知引擎不应命中他人缓存"

    def test_fetch_and_evidence_are_isolated(self, sc):
        """fetch 与 evidence 作为不同 engine 值必须隔离（URL 词面近似场景）。"""
        url = "https://developers.openai.com/api/docs/guides/image-prompting"
        sc.set(url, "fetch", 10, _payload("正文", url), domain="general")
        # evidence 缓存的是「另一个 URL」，但词面与上面高度相似
        sc.set(url + ".md", "evidence", 10, _payload("证据分", url + ".md"), domain="general")

        got_fetch = {c["query"] for c in sc._l2.find_similar(url, "fetch", "general", limit=50)}
        # 同 engine 下 url 与 url.md 相似度 0.875，属合法的近重复软命中
        got_ev = {c["query"] for c in sc._l2.find_similar(url, "evidence", "general", limit=50)}

        assert got_ev == {url + ".md"}, f"evidence 不应串到 fetch 载荷，实际 {got_ev}"
        assert url not in got_fetch  # 精确同名被 cached_q == nq 跳过

    def test_combo_keys_excluded(self, sc):
        """组合键（多引擎拼接 `a+b`）不参与软命中：组合结果集不可与单引擎互换。"""
        sc.set("苹果 2025 营收", "eastmoney+xueqiu", 10,
               _payload("组合", "https://c/1"), domain="financial")
        got = sc._l2.find_similar("苹果 2025 年营收", "auto", "financial", limit=50)
        assert got == [], "组合键不应作为单引擎查询的软命中来源"


class TestSemanticMarkerPreserved:
    """软命中标记必须在 L1 往返后保留。"""

    def test_marker_survives_l1_second_hit(self, sc):
        """第 1 次软命中(L2) → L1 回填 → 第 2 次命中(L1) 仍须带软命中标记。"""
        sc.set("苹果 2025 营收", "eastmoney", 5,
               _payload("东财", "https://e/1"), domain="financial")

        o1 = sc.get("苹果 2025 年营收", "eastmoney", 5,
                    domain="financial", mode="auto", depth="fast")
        assert o1 is not None and o1.get("_semantic_hit") is True

        o2 = sc.get("苹果 2025 年营收", "eastmoney", 5,
                    domain="financial", mode="auto", depth="fast")
        assert o2 is not None
        assert o2.get("_semantic_hit") is True, (
            "L1 二次命中丢失软命中标记 → 伪装成硬命中（修复前行为）"
        )
        assert o2.get("_semantic_query") == "苹果 2025 营收"
        assert o2.get("_semantic_similarity") is not None

    def test_l1_payload_carries_ttl(self, sc):
        """L1 回填必须带 _ttl/_ts，否则 L1 分支的过期判断失效。"""
        sc.set("苹果 2025 营收", "eastmoney", 5,
               _payload("东财", "https://e/1"), domain="financial")
        sc.get("苹果 2025 年营收", "eastmoney", 5,
               domain="financial", mode="auto", depth="fast")

        key = sc._key("苹果 2025 年营收", "eastmoney", 5,
                      "financial", "auto", "fast", kind="combo")
        l1v = sc._l1.get(key) or {}
        assert l1v.get("_ttl"), "L1 载荷缺 _ttl → 过期判断失效"
        assert l1v.get("_ts"), "L1 载荷缺 _ts → 过期判断失效"
