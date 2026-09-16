#!/usr/bin/env python3
"""url_canon 唯一来源契约测试。

锁定三件事：
  1. 折叠规则本身（追踪参数/www/移动站/尾斜杠/参数序/转义）
  2. 不误合并（路径大小写、有语义查询参数、不同 host）
  3. 四个历史调用点（search / plan / candidate_envelope / research_dossier）
     折叠结果一致——这是本模块存在的理由，必须回归锁死。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from url_canon import TRACKING_PARAMS, canonical_url, is_tracking_param  # noqa: E402


class TestCanonicalFolding(unittest.TestCase):
    def test_strips_utm_family(self):
        u = canonical_url("https://ex.com/p?utm_source=x&id=1")
        self.assertIn("id=1", u)
        self.assertNotIn("utm_source", u)

    def test_strips_utm_prefix_family_unknown_keys(self):
        # utm_ 任意子键都要删（族规则，不是枚举）
        u = canonical_url("https://ex.com/p?utm_campaign_new=1&utm_foo=2&keep=3")
        self.assertNotIn("utm_campaign_new", u)
        self.assertNotIn("utm_foo", u)
        self.assertIn("keep=3", u)

    def test_strips_platform_share_params(self):
        for p in ("spm=a1z.2", "share_token=abc", "share_source=copy",
                  "fbclid=IwAR123", "gclid=Cjw", "igshid=xyz"):
            u = canonical_url(f"https://ex.com/p?{p}&keep=1")
            key = p.split("=")[0]
            self.assertNotIn(key, u, f"{key} 应被删除")

    def test_www_and_mobile_folded(self):
        base = "https://ex.com/p"
        for variant in ("https://www.ex.com/p", "https://m.ex.com/p",
                        "https://mobile.ex.com/p", "https://amp.ex.com/p"):
            self.assertEqual(canonical_url(variant), canonical_url(base), variant)

    def test_scheme_and_default_port_folded(self):
        for variant in ("http://ex.com/p", "https://ex.com:443/p",
                        "http://ex.com:80/p"):
            self.assertEqual(canonical_url(variant), "https://ex.com/p", variant)

    def test_fragment_dropped(self):
        self.assertEqual(canonical_url("https://ex.com/p#sec2"),
                         canonical_url("https://ex.com/p"))

    def test_trailing_slash_and_double_slash(self):
        for variant in ("https://ex.com/p/", "https://ex.com//p",
                        "https://ex.com///p"):
            self.assertEqual(canonical_url(variant), "https://ex.com/p", variant)

    def test_root_path_kept(self):
        self.assertEqual(canonical_url("https://ex.com/"), "https://ex.com/")

    def test_query_order_normalized(self):
        self.assertEqual(canonical_url("https://ex.com/p?b=2&a=1"),
                         canonical_url("https://ex.com/p?a=1&b=2"))

    def test_pct_escape_normalized(self):
        # %7E 与 ~ 等价；%3a 与 %3A 大小写等价
        self.assertEqual(canonical_url("https://ex.com/%7Euser"),
                         canonical_url("https://ex.com/~user"))
        self.assertEqual(canonical_url("https://ex.com/p?q=%3a"),
                         canonical_url("https://ex.com/p?q=%3A"))

    def test_userinfo_stripped(self):
        u = canonical_url("https://user:pass@ex.com/p")
        self.assertNotIn("user", u)
        self.assertNotIn("pass", u)
        self.assertIn("ex.com", u)

    def test_idempotent(self):
        raw = "http://www.ex.com/a/?utm_source=q&b=2&a=1#f"
        once = canonical_url(raw)
        self.assertEqual(canonical_url(once), once)


class TestNoOverMerge(unittest.TestCase):
    """防误合并：这些变体必须保持不同键。"""

    def test_path_case_preserved(self):
        self.assertNotEqual(canonical_url("https://ex.com/Wiki/Foo"),
                            canonical_url("https://ex.com/wiki/foo"))

    def test_host_differs(self):
        self.assertNotEqual(canonical_url("https://a.com/p"),
                            canonical_url("https://b.com/p"))

    def test_path_differs(self):
        self.assertNotEqual(canonical_url("https://ex.com/a"),
                            canonical_url("https://ex.com/b"))

    def test_meaningful_query_kept(self):
        self.assertNotEqual(canonical_url("https://ex.com/p?id=1"),
                            canonical_url("https://ex.com/p?id=2"))
        # 有语义参数不能被当追踪删掉
        u = canonical_url("https://ex.com/search?q=hello")
        self.assertIn("q=hello", u)

    def test_short_domain_not_mangled(self):
        # m.co 不应被削成 co
        self.assertEqual(canonical_url("https://m.co/p"), "https://m.co/p")

    def test_empty_and_non_http(self):
        self.assertEqual(canonical_url(""), "")
        self.assertEqual(canonical_url("file:///tmp/a.txt"), "file:///tmp/a.txt")
        self.assertEqual(canonical_url("not a url"), "not a url")

    def test_failless_on_garbage(self):
        # 任何输入都不抛异常
        for bad in ("http://", "://x", "https://ex.com/%zz", "%%"):
            canonical_url(bad)


class TestTrackingParamPredicate(unittest.TestCase):
    def test_known(self):
        self.assertTrue(is_tracking_param("utm_source"))
        self.assertTrue(is_tracking_param("UTM_Medium"))
        self.assertTrue(is_tracking_param("fbclid"))

    def test_not_tracking(self):
        for k in ("q", "id", "page", "lang", "v"):
            self.assertFalse(is_tracking_param(k), k)

    def test_table_sane(self):
        self.assertGreater(len(TRACKING_PARAMS), 40)
        self.assertTrue(all(p == p.lower() for p in TRACKING_PARAMS))


class TestCallSitesAgree(unittest.TestCase):
    """四个历史调用点必须与单来源一致（这是本模块的存在理由）。"""

    CASES = [
        "http://www.Ex.com/p/?utm_source=x&b=2&a=1#frag",
        "https://m.ex.com/p?spm=a1z&keep=1",
        "https://ex.com/p/",
        "https://ex.com/p?gclid=1&q=hi",
    ]

    def test_search_site(self):
        import search
        for c in self.CASES:
            self.assertEqual(search._canonical_url(c), canonical_url(c), c)

    def test_plan_site(self):
        import plan
        for c in self.CASES:
            self.assertEqual(plan.canonicalize_url(c), canonical_url(c), c)

    def test_candidate_envelope_site(self):
        import candidate_envelope
        for c in self.CASES:
            self.assertEqual(candidate_envelope.canonicalize_url(c), canonical_url(c), c)

    def test_research_dossier_site(self):
        import research_dossier
        for c in self.CASES:
            self.assertEqual(research_dossier.canonical_url(c), canonical_url(c), c)


if __name__ == "__main__":
    unittest.main()
