#!/usr/bin/env python3
"""test_v2ex_nodes.py — V2EX 节点路由（三层递进）回归测试。

背景：第一批的 V2EX 引擎只用 hot+latest 候选池（20 条全站热帖），
长尾查询必然落空。本模块引入节点路由（官方 API 的
`show.json?node_name=X`），把「全站热帖过滤」升级为「相关节点内检索」。

三层递进：exact（倒排）→ synonym（同义/中英）→ cn_title（中文标题反查）
→ header（语义保底）。全空则 layer=none，调用方回落 hot/latest。

本文件锁定四条契约：
  1. 各层按预期触发（exact 优先于 synonym 优先于 cn_title 优先于 header）
  2. 无命中时诚实返回 none，不硬凑结果
  3. 单字节点名被排除（结构性噪声）
  4. 刻意不做热度/名实筛选——节点好坏是「节点 × 查询」联合属性
"""

import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import v2ex_nodes as vn  # noqa: E402


def _node(name, title="", header="", topics=1000):
    return {"name": name, "title": title, "header": header, "topics": topics}


@pytest.fixture()
def nodes():
    return [
        _node("python", "Python", "各种 Python 编程话题"),
        _node("docker", "Docker", "容器与编排"),
        _node("paper", "Paper", "绘画软件讨论"),
        _node("k8s", "Kubernetes", "容器编排集群"),
        _node("jobs", "酷工作", "招聘与求职"),
        _node("shanghai", "上海", "上海生活"),
        _node("c", "C", "C 语言"),          # 单字：应被排除
        _node("u", "大学", "校园生活"),       # 单字：应被排除
        _node("pro", "PRO", "很冷门的节点", topics=17),
    ]


class TestLayerExact:
    """L1 精确倒排：查询词元 ∩ 节点名。"""

    def test_direct_node_name_hit(self, nodes):
        r = vn.pick_nodes("python asyncio 怎么用", nodes=nodes)
        assert r["layer"] == "exact" and r["confidence"] == 1.0
        assert r["nodes"] == ["python"]

    def test_multiple_node_hits(self, nodes):
        r = vn.pick_nodes("docker 和 python 的区别", nodes=nodes)
        assert r["layer"] == "exact"
        assert set(r["nodes"]) == {"docker", "python"}

    def test_top_k_limits(self, nodes):
        r = vn.pick_nodes("docker python paper", top_k=2, nodes=nodes)
        assert len(r["nodes"]) == 2


class TestLayerSynonym:
    """L2 同义/中英扩展后倒排。"""

    def test_synonym_expansion_hits_node(self, nodes, monkeypatch):
        # 「论文」经 argo 同义表扩展出 paper
        monkeypatch.setattr(vn, "_expand_synonyms", lambda q: {"paper"})
        r = vn.pick_nodes("论文 下载", nodes=nodes)
        assert r["layer"] == "synonym" and r["confidence"] == 0.8
        assert r["nodes"] == ["paper"]

    def test_synonym_not_used_when_exact_hits(self, nodes, monkeypatch):
        """exact 优先：同义扩展不应覆盖更强的精确命中。"""
        monkeypatch.setattr(vn, "_expand_synonyms", lambda q: {"paper"})
        r = vn.pick_nodes("python 论文", nodes=nodes)
        assert r["layer"] == "exact"
        assert "python" in r["nodes"]


class TestLayerCnTitle:
    """L2b 中文标题反查：节点的 title/header 是中文。"""

    def test_cn_title_reverse_lookup(self, nodes):
        r = vn.pick_nodes("工作 机会", nodes=nodes)
        assert r["layer"] == "cn_title" and r["confidence"] == 0.7
        assert "jobs" in r["nodes"]

    def test_cn_single_char_not_used(self, nodes):
        """单字 CJK 不参与反查（避免「上」「大」噪声）。"""
        r = vn.pick_nodes("上", nodes=nodes)
        assert r["layer"] == "none"

    def test_cn_term_two_chars_required(self, nodes):
        r = vn.pick_nodes("上海 生活", nodes=nodes)
        assert r["layer"] == "cn_title" and "shanghai" in r["nodes"]


class TestLayerHeader:
    """L3 header 语义保底（低置信）。"""

    def test_header_match(self, nodes):
        # kubernetes 不在任何节点名里，但 k8s 节点的 header/title 含 Kubernetes
        r = vn.pick_nodes("kubernetes networking", nodes=nodes)
        assert r["layer"] == "header" and r["confidence"] == 0.5
        assert "k8s" in r["nodes"]

    def test_short_probe_words_not_used(self, nodes):
        """L3 只认 ≥4 字符词，避免通用短词误命中。"""
        r = vn.pick_nodes("abc xyz", nodes=nodes)
        assert r["layer"] == "none"


class TestHonestNone:
    """无命中时诚实返回 none，不硬凑。"""

    def test_unrelated_query_returns_none(self, nodes):
        r = vn.pick_nodes("完全无关的查询xyzabc", nodes=nodes)
        assert r["nodes"] == [] and r["layer"] == "none" and r["confidence"] == 0.0

    def test_empty_query(self, nodes):
        assert vn.pick_nodes("", nodes=nodes)["layer"] == "none"

    def test_no_nodes_available(self):
        assert vn.pick_nodes("python", nodes=[])["layer"] == "none"


class TestStructuralNoiseExcluded:
    """结构性噪声排除：仅按名字长度，不按热度/名实。"""

    def test_single_char_nodes_excluded(self, nodes):
        """查询含 "c"/"u" 时不得命中单字节点。"""
        r = vn.pick_nodes("c 语言怎么学", nodes=nodes)
        assert "c" not in r["nodes"]

    def test_low_topic_node_still_eligible(self, nodes):
        """刻意不按热度筛选：pro(17 主题) 仍可被路由。

        这是设计决定而非疏漏——节点好坏是「节点 × 查询」联合属性，
        路由层无法预判；判据后移到既有相关性过滤。
        """
        r = vn.pick_nodes("pro 是什么", nodes=nodes)
        assert r["layer"] == "exact" and "pro" in r["nodes"]


class TestCacheBehaviour:
    """节点表缓存：匹配阶段零 API 调用 + 不完整数据不固化。"""

    def test_fetch_reads_from_fetcher(self):
        vn.clear_cache()
        calls = []

        def fake_fetcher(url):
            calls.append(url)
            import json
            return json.dumps([_node("python", "Python")])

        n1 = vn.fetch_all_nodes(fetcher=fake_fetcher)
        assert calls and n1 and n1[0]["name"] == "python"

    def test_fetch_handles_bad_response(self):
        vn.clear_cache()
        assert vn.fetch_all_nodes(fetcher=lambda u: None) == []
        assert vn.fetch_all_nodes(fetcher=lambda u: "not-json") == []

    def test_fetch_filters_empty_nodes(self):
        vn.clear_cache()
        import json
        raw = json.dumps([
            _node("python", "Python", topics=100),
            {"name": "empty", "title": "空节点", "topics": 0},
            {"no_name": True},
        ])
        got = vn.fetch_all_nodes(fetcher=lambda u: raw)
        assert [n["name"] for n in got] == ["python"]

    def test_small_table_not_persisted(self, tmp_path, monkeypatch):
        """完整性防御：节点数低于下限时不写缓存。

        实测教训：测试 fixture（1 个假节点）曾被写进生产缓存路径，
        导致后续所有真实查询只看得见那一个节点、路由全失败。
        """
        vn.clear_cache()
        import json
        cache = tmp_path / "nodes.json"
        monkeypatch.setattr(vn, "_cache_path", lambda: cache)
        vn.fetch_all_nodes(fetcher=lambda u: json.dumps([_node("python", "Python")]))
        assert not cache.exists(), "不完整节点表不得写入缓存"

    def test_full_table_persisted(self, tmp_path, monkeypatch):
        vn.clear_cache()
        import json
        cache = tmp_path / "nodes.json"
        monkeypatch.setattr(vn, "_cache_path", lambda: cache)
        big = [_node(f"node{i}", f"Node{i}") for i in range(vn.MIN_NODE_TABLE_SIZE + 5)]
        vn.fetch_all_nodes(fetcher=lambda u: json.dumps(big))
        assert cache.exists(), "完整节点表应写入缓存"
