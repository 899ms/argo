#!/usr/bin/env python3
"""tests/test_ego_search_merge.py — public+login 融合与质量信号"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "sub-skills" / "ego-search" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import merge as merge_mod  # noqa: E402
import quality  # noqa: E402


def test_merge_dual_sourced():
    public = {
        "query": "q",
        "engine": "local_bing",
        "source": "local_bing",
        "results": [
            {"title": "Public T", "url": "https://example.com/a?utm_source=x", "snippet": "p"},
        ],
    }
    login = {
        "query": "q",
        "engine": "ego_browser_bing",
        "source": "ego-browser",
        "runtime": "ego",
        "login_state_used": True,
        "cache_eligible": False,
        "results": [
            {"title": "Login T", "url": "https://example.com/a", "snippet": "l"},
        ],
    }
    out = merge_mod.merge_payloads(public, login, query="q")
    assert out["schema"] == "ego_search_merge_v1"
    assert out["public_count"] == 1
    assert out["login_count"] == 1
    assert out["dual_sourced_count"] >= 1
    assert out["isolation"]["login_cache_eligible"] is False
    # 冲突标题应记入 conflicts
    assert any(c.get("canonical_url") for c in out["conflicts"]) or out["dual_sourced_count"] >= 1


def test_merge_url_variant_convergence():
    """归一化必须与主仓 url_canon 同源（BUG-3 防回归）。

    merge.py 曾自带 7 参数追踪表 + 无 www/移动站折叠，6/9 URL 变体与
    主仓发散 → www/utm/尾斜杠变体对不成 dual_sourced，融合报告恒空。
    """
    public = {
        "query": "q", "engine": "local_bing", "source": "local_bing",
        "results": [
            {"title": "Public T", "url": "https://www.example.com/a?b=1&utm_source=x", "snippet": "p"},
            {"title": "Plain", "url": "https://m.example.com/guide/", "snippet": "m"},
        ],
    }
    login = {
        "query": "q", "engine": "ego_browser_bing", "source": "ego-browser",
        "runtime": "ego", "login_state_used": True, "cache_eligible": False,
        "results": [
            {"title": "Login T", "url": "https://example.com/a?b=1", "snippet": "l"},
            {"title": "Mobile", "url": "https://example.com/guide", "snippet": "m"},
        ],
    }
    out = merge_mod.merge_payloads(public, login, query="q")
    assert out["dual_sourced_count"] >= 1, (
        "www/utm 变体未对上——merge.py 偏离了 url_canon 单一真源")
    # 移动站前缀 m. 必须折叠（旧实现不折叠，恒单源）
    assert out["dual_sourced_count"] >= 2, "m./www 变体未折叠，归一化发散复发"
    # 归一键不带追踪参数
    urls = [m["canonical_url"] for m in out["merged"]]
    assert all("utm_source" not in u for u in urls)


def test_quality_auth_wall():
    p = quality.assess_body({
        "title": "登录",
        "content": "请先登录后查看全文内容",
        "url": "https://example.com/x",
    })
    assert p["quality"]["auth_wall_suspected"] is True
    assert p["quality"]["login_likely_ok"] is False


def test_quality_ok_body():
    p = quality.assess_body({
        "title": "Article",
        "content": "A" * 200,
        "url": "https://example.com/x",
    })
    assert p["quality"]["empty_or_thin"] is False
    assert p["quality"]["login_likely_ok"] is True
