#!/usr/bin/env python3
"""test_fetch_cache_max_chars.py — fetch URL 缓存不重放 max_chars 的回归。

背景（2026-09-16 实测缺陷）：
  缓存写入的是「按本次 max_chars 裁剪后」的正文，但缓存条目没有记录这个
  上限。于是同一 URL 先用默认 8000 抓一次、再带 --max-chars 60000 重抓时，
  命中缓存的正文只有 8000 字且 8000 <= 60000 不触发截断分支，调用方静默
  拿到被截短的正文（实测该次正文比首次更短），看起来像「max-chars 不生效」。

修复契约（与 search 侧 _max_results 柔性命中同构）：
  - 写缓存时记录 _max_chars
  - 读缓存时若本次 max_chars 更大 → 判 miss 回源重抓（不是截断后返回）
  - 本次更小或相等 → 正常命中并按需截断
  - 老条目（无 _max_chars）长度不可信，退回按 content 实际长度判断
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
os.environ.setdefault("ARGO_STATE_DIR", tempfile.mkdtemp(prefix="argo_test_mc_"))

import fetch_v3  # noqa: E402
from cache import SearchCache  # noqa: E402


def _body(n: int) -> str:
    unit = "The quick brown fox jumps over the lazy dog. "
    return (unit * (n // len(unit) + 1))[:n]


@pytest.fixture
def clean_env(monkeypatch):
    """隔离：robots 放行 + 关闭全部可选真实网络级 + 关闭 deadline。"""
    import robots_guard
    monkeypatch.setattr(robots_guard, "robots_blocked", lambda u, timeout=5.0: False)
    for var in ("ARGO_FETCH_MD_VARIANT", "ARGO_FETCH_JINA", "ARGO_FETCH_PARALLEL",
                "ARGO_FETCH_IMPERSONATE", "ARGO_FETCH_MOBILE", "ARGO_FETCH_TINYFISH"):
        monkeypatch.setenv(var, "0")
    monkeypatch.setenv("ARGO_FETCH_DEADLINE_S", "0")
    monkeypatch.delenv("TINYFISH_API_KEY", raising=False)


def _install_http(monkeypatch, calls):
    """HTTP 级成功，且像真实实现那样按 max_chars 裁剪正文。"""
    def fn(url, max_chars=8000, timeout=8.0):
        calls.append(max_chars)
        content = _body(max_chars)
        return {"url": url, "content": content, "html": "", "title": "t",
                "length": len(content), "success": True, "error": None,
                "fetch_method": "http"}
    monkeypatch.setattr(fetch_v3, "_http_fetch", fn)
    monkeypatch.setattr(fetch_v3, "_wayback_fetch",
                        lambda url, max_chars=8000, timeout=8.0:
                        {"url": url, "content": "", "html": "", "title": "",
                         "length": 0, "success": False, "error": "wayback",
                         "fetch_method": "wayback"})
    monkeypatch.setattr(fetch_v3, "_browser_fetch",
                        lambda url, max_chars=8000, timeout=15.0, actions=None:
                        {"url": url, "content": "", "html": "", "title": "",
                         "length": 0, "success": False, "error": "browser",
                         "fetch_method": "browser"})


def _fetch(url, max_chars):
    return fetch_v3.fetch_v3(url, max_chars=max_chars,
                             use_browser_fallback=False, timeout=5.0)


def test_larger_request_misses_cache_and_refetches(clean_env, monkeypatch):
    """核心回归：请求更大 max_chars 时必须回源，不得返回被裁剪的短正文。"""
    calls = []
    _install_http(monkeypatch, calls)
    url = "https://example.com/regress-larger"

    first = _fetch(url, 1000)
    assert first["cached"] is False
    assert len(first["content"]) == 1000
    assert SearchCache().get_fetch(url).get("_max_chars") == 1000

    second = _fetch(url, 6000)
    assert second.get("cached") is not True, "更大 max_chars 不得直接吃被裁剪的缓存"
    assert len(second["content"]) > len(first["content"])
    assert calls == [1000, 6000], "应真的回源重抓一次"
    assert SearchCache().get_fetch(url).get("_max_chars") == 6000

    third = _fetch(url, 2000)
    assert third.get("cached") is True, "请求变小时应重新命中缓存"
    assert len(third["content"]) == 2000


def test_same_request_hits_cache(clean_env, monkeypatch):
    """同等 max_chars 重复请求：正常命中，不产生多余抓取。"""
    calls = []
    _install_http(monkeypatch, calls)
    url = "https://example.com/regress-same"

    _fetch(url, 4000)
    again = _fetch(url, 4000)
    assert again["cached"] is True
    assert calls == [4000], "同等请求不应回源"


def test_legacy_cache_without_max_chars_reraises_when_short(clean_env, monkeypatch):
    """修复前写入的老条目（无 _max_chars）：请求更大时按内容长度判 miss。"""
    calls = []
    _install_http(monkeypatch, calls)
    url = "https://example.com/regress-legacy-short"
    SearchCache().set_fetch(url, {
        "url": url, "title": "legacy", "content": _body(500), "length": 500,
        "success": True, "error": None, "fetch_method": "http",
    }, ttl=600)

    out = _fetch(url, 5000)
    assert out.get("cached") is not True
    assert len(out["content"]) > 500
    assert calls == [5000]


def test_legacy_cache_without_max_chars_hits_when_content_long_enough(clean_env, monkeypatch):
    """老条目但正文本身就够长：命中原样返回，且内容不被无谓改写。"""
    calls = []
    _install_http(monkeypatch, calls)
    url = "https://example.com/regress-legacy-long"
    SearchCache().set_fetch(url, {
        "url": url, "title": "legacy", "content": _body(9000), "length": 9000,
        "success": True, "error": None, "fetch_method": "http",
    }, ttl=600)

    out = _fetch(url, 3000)
    assert out["cached"] is True
    assert out.get("cache_level") in ("L1", "L2")
    assert calls == [], "内容够用时不应回源"


def test_internal_max_chars_not_leaked(clean_env, monkeypatch):
    """_max_chars 是缓存内部字段，不得泄漏进给调用方的结果。"""
    calls = []
    _install_http(monkeypatch, calls)
    url = "https://example.com/regress-no-leak"

    out = _fetch(url, 2000)
    assert "_max_chars" not in out
    assert not any(str(k).startswith("_") for k in out if k != "cache_level")
