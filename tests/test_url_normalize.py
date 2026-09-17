#!/usr/bin/env python3
"""URL 语法规范化回归门（RFC 3986 §6.2.2 / §6.2.3）。

只做**等价改写**：同一个资源的不同写法归一。它不改善抓取本身，但影响
缓存命中与去重——大小写、默认端口、点段、百分号编码任一不同，同一页就会
被判成两条，缓存与去重双双失效，而外部看不出来。

不变量：规范化必须幂等（再规范化一次不变），且不得改动路径大小写、
不得解码保留字符（那会改变语义）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from fetch_v3 import _optimize_url, normalize_url  # noqa: E402


class TestNormalizeUrl(unittest.TestCase):
    def test_scheme_and_host_lowercased(self):
        self.assertEqual(normalize_url("HTTP://Example.COM/a"),
                         "http://example.com/a")

    def test_path_case_preserved(self):
        """路径大小写敏感，规范化不得动它。"""
        self.assertEqual(normalize_url("https://x.com/A/B"), "https://x.com/A/B")

    def test_default_port_removed(self):
        self.assertEqual(normalize_url("http://x.com:80/a"), "http://x.com/a")
        self.assertEqual(normalize_url("https://x.com:443/a"), "https://x.com/a")

    def test_non_default_port_kept(self):
        self.assertEqual(normalize_url("https://x.com:8443/a"),
                         "https://x.com:8443/a")

    def test_dot_segments_removed(self):
        self.assertEqual(normalize_url("https://x.com/a/./b/../c"),
                         "https://x.com/a/c")
        self.assertEqual(normalize_url("https://x.com/../../a"), "https://x.com/a")

    def test_unreserved_percent_decoded(self):
        self.assertEqual(normalize_url("https://x.com/%7Euser"), "https://x.com/~user")
        self.assertEqual(normalize_url("https://x.com/%41bc"), "https://x.com/Abc")

    def test_reserved_percent_kept(self):
        """保留字符不得解码——%2F 解成 / 会改变语义。"""
        self.assertEqual(normalize_url("https://x.com/a%2Fb"), "https://x.com/a%2Fb")

    def test_non_ascii_stays_encoded(self):
        self.assertEqual(normalize_url("https://x.com/p?q=%E4%B8%AD"),
                         "https://x.com/p?q=%E4%B8%AD")

    def test_empty_path_becomes_root(self):
        self.assertEqual(normalize_url("https://x.com"), "https://x.com/")

    def test_query_preserved(self):
        self.assertEqual(normalize_url("https://x.com/p?a=1&b=2"),
                         "https://x.com/p?a=1&b=2")

    def test_idempotent(self):
        for u in ("HTTP://Example.COM:80/a/./b?x=1", "https://x.com/%7Ea",
                  "https://x.com", "https://x.com/a%2Fb"):
            once = normalize_url(u)
            self.assertEqual(normalize_url(once), once, f"不幂等：{u}")

    def test_non_url_returned_as_is(self):
        for u in ("not a url", "mailto:a@b.com", ""):
            self.assertEqual(normalize_url(u), u)


class TestOptimizeUrlStillWorks(unittest.TestCase):
    """规范化接进 _optimize_url 之后，原有职责不得丢。"""

    def test_normalization_is_wired_in(self):
        """规范化必须真的接在 _optimize_url 上——只写函数不接线等于没有。"""
        self.assertEqual(_optimize_url("HTTP://X.com:80/a/./b"),
                         "http://x.com/a/b")
        self.assertEqual(_optimize_url("https://x.com"), "https://x.com/")

    def test_tracking_params_still_cleaned(self):
        self.assertEqual(_optimize_url("https://x.com/a?utm_source=z&k=1"),
                         "https://x.com/a?k=1")

    def test_reddit_still_rewritten(self):
        self.assertEqual(_optimize_url("https://www.reddit.com/r/x"),
                         "https://old.reddit.com/r/x")

    def test_both_applied_together(self):
        self.assertEqual(
            _optimize_url("HTTP://WWW.Reddit.COM:80/r/x/?utm_source=z"),
            "http://old.reddit.com/r/x/")


class TestNormalizationHelpsDedup(unittest.TestCase):
    """同一页的不同写法必须落到同一个键上——这是规范化的实际收益。"""

    VARIANTS = [
        "https://Example.com/a/b",
        "https://example.com/a/b",
        "https://example.com:443/a/b",
        "https://example.com/a/./b",
        "https://example.com/a/c/../b",
    ]

    def test_variants_collapse(self):
        keys = {normalize_url(u) for u in self.VARIANTS}
        self.assertEqual(len(keys), 1, f"未收敛到同一键：{keys}")


if __name__ == "__main__":
    unittest.main()
