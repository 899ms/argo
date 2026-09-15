#!/usr/bin/env python3
"""test_hotpath_memoization.py — 热路径重复劳动的消除门禁（2026-09-15）。

## 守的是什么

一次真实搜索的 cProfile 显示两处**同一份计算被反复做**，都是纯函数、
都只差一层记忆化：

1. `cache._signature`：`query_similarity(q1, q2)` 要和缓存里多条候选比对，
   旧写法每次都把两边的 n-gram 集合全量重哈希——一次搜索实测 28,552 次
   `_hash_token` 调用，绝大多数是同一批 token 的重复置换。签名化 + 记忆化
   后同一查询全进程只算一次。**等价性必须逐位保持**（这是缓存命中判据）。
2. `config.config_stamp()`：取值要 stat 全部 63 个外置声明文件，而
   `engines.get_registry()` 每访问一次注册表就调它一次——一次搜索触发 28 次
   = 1764 次 stat。按 TTL 折叠（默认 1 s，可用 `ARGO_CONFIG_STAMP_TTL_S`
   关闭/调节），热加载语义不要求亚秒级感知。

本文件锁：等价性、记忆化生效、TTL 可关闭、非法配置容错。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import cache  # noqa: E402
import config  # noqa: E402


def _reference_similarity(q1: str, q2: str) -> float:
    """改动前的实现，作为等价性基准（原样复刻，勿按新实现改写）。"""
    a = cache._ngrams(q1)
    b = cache._ngrams(q2)
    if not a or not b:
        return 0.0
    hits = 0
    for seed in range(cache._MINHASH_PERM):
        if (min(cache._hash_token(t, seed) for t in a)
                == min(cache._hash_token(t, seed) for t in b)):
            hits += 1
    return hits / cache._MINHASH_PERM


_QUERIES = [
    "苹果 2025 营收", "苹果 2025 年营收", "苹果2025营收",
    "2026 中国新能源汽车出口数据", "2026 中国新能源汽车 出口 数据",
    "特斯拉 财报", "", " ", "a", "中", "苹果 2025 营收 ",
    "Bernanke 2005 savings glut", "savings glut Bernanke 2005",
]


class TestQuerySimilarityEquivalence:
    """签名化不得改变任何一对输入的相似度值——它是缓存软命中的判据。"""

    @pytest.mark.parametrize("q1", _QUERIES)
    def test_matches_reference_implementation(self, q1):
        for q2 in _QUERIES:
            new = cache.query_similarity(q1, q2)
            old = _reference_similarity(q1, q2)
            assert abs(new - old) < 1e-12, (
                f"签名化改变了相似度：{q1!r} vs {q2!r} → {new} ≠ {old}")

    def test_near_duplicate_still_high(self):
        """近重复查询仍要判为高相似（语义缓存软命中的业务前提）。"""
        assert cache.query_similarity("苹果 2025 营收", "苹果 2025 年营收") > 0.7

    def test_empty_side_is_zero(self):
        assert cache.query_similarity("", "苹果 2025 营收") == 0.0
        assert cache.query_similarity("苹果", "") == 0.0


class TestSignatureIsMemoized:
    def test_repeated_comparison_hits_cache(self):
        cache._signature.cache_clear()
        for _ in range(50):
            cache.query_similarity("2026 中国新能源汽车出口数据",
                                   "2026 中国新能源汽车 出口 数据")
        info = cache._signature.cache_info()
        # 两条查询各算一次，其余全命中——这正是旧实现被重复消耗的部分
        assert info.misses == 2, f"签名重算了（misses={info.misses}）"
        assert info.hits >= 98, f"命中数不足（hits={info.hits}）"

    def test_signature_is_pure(self):
        cache._signature.cache_clear()
        assert cache._signature("苹果 2025 营收") == cache._signature("苹果 2025 营收")

    def test_signature_length_is_perm_count(self):
        assert len(cache._signature("苹果 2025 营收")) == cache._MINHASH_PERM


class TestConfigStampTtl:
    """config_stamp 按 TTL 折叠重复扫盘；语义与可关闭性都要站得住。"""

    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        monkeypatch.setattr(config, "_stamp_cache", None)
        monkeypatch.setenv("ARGO_CONFIG_STAMP_TTL_S", "1.0")
        yield
        config._stamp_cache = None

    def _count_scans(self, monkeypatch):
        n = [0]
        real = config._external_engines_mtime

        def counting():
            n[0] += 1
            return real()

        monkeypatch.setattr(config, "_external_engines_mtime", counting)
        return n

    def test_repeated_calls_scan_once(self, monkeypatch):
        n = self._count_scans(monkeypatch)
        vals = [config.config_stamp() for _ in range(28)]
        assert n[0] == 1, f"TTL 内扫盘 {n[0]} 次（应 1 次）——get_registry 每次访问都会调它"
        assert len(set(vals)) == 1, "TTL 内返回值不一致"

    def test_ttl_zero_disables_memoization(self, monkeypatch):
        monkeypatch.setenv("ARGO_CONFIG_STAMP_TTL_S", "0")
        n = self._count_scans(monkeypatch)
        for _ in range(5):
            config.config_stamp()
        assert n[0] == 5, "TTL=0 应关闭记忆化（热加载即时感知的逃生门）"

    def test_expiry_rescans(self, monkeypatch):
        monkeypatch.setenv("ARGO_CONFIG_STAMP_TTL_S", "0.05")
        n = self._count_scans(monkeypatch)
        config.config_stamp()
        config.config_stamp()
        assert n[0] == 1, "TTL 内应复用"
        time.sleep(0.08)
        config.config_stamp()
        assert n[0] == 2, "过期后应重新取值"

    @pytest.mark.parametrize("bad", ["abc", "", "None"])
    def test_invalid_ttl_falls_back(self, monkeypatch, bad):
        monkeypatch.setenv("ARGO_CONFIG_STAMP_TTL_S", bad)
        assert config._stamp_ttl() == 1.0

    def test_stamp_value_is_still_mtime_max(self, monkeypatch):
        """折叠的是调用次数，不是取值——TTL 后取到的必须还是真实 mtime 最大值。"""
        config._stamp_cache = None
        got = config.config_stamp()
        expected = 0.0
        try:
            expected = config.CONFIG_PATH.stat().st_mtime
        except OSError:
            pass
        assert got >= expected


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
