#!/usr/bin/env python3
"""质量分的分项依据与语种中立性。

只给一个总分时，调用方无法分辨「分低是因为太短」还是「因为全是导航链接」，
而这两者的处置完全不同。分项落出来之后，一个一直存在却没人看见的偏差立刻
显形了：`content.split()` 按空白切词，而中文词间不写空格——同一体量的页面
英文得 1,220「词」、中文只有 85，导致权重 0.4 的 word_count 项对中文几乎是
零分（实测 0.07 vs 0.40），中文内容总分被系统性压低约 0.3。

后果不是显示难看，而是**中文信息源在筛选时被更激进地判为低质**，而这项
判断本该与语种无关。本文件把「分项可解释」与「语种不惩罚」两件事都锁住。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from fetch_quality import (  # noqa: E402
    QUALITY_FORMULA_VERSION,
    _compute_quality,
    effective_words,
    quality_breakdown,
)

ZH_ARTICLE = (
    "域名系统是互联网的一项核心服务，它作为可以将域名和 IP 地址相互映射的"
    "分布式数据库，能够使人更方便地访问互联网，而不用去记住能够被机器直接"
    "读取的 IP 数串。域名系统采用类似目录树的等级结构，服务器的身份由域名"
    "标识，而域名由若干部分构成，各部分之间用点号分隔。"
) * 6


class TestEffectiveWords(unittest.TestCase):
    def test_cjk_counted_by_char(self):
        """中文按字折算，不因没有空格而算成一个词。"""
        self.assertEqual(effective_words("这是一段中文内容"), 4)

    def test_latin_counted_by_whitespace(self):
        self.assertEqual(effective_words("one two three"), 3)

    def test_mixed_sums_both(self):
        n = effective_words("中文四个字 word word")
        self.assertEqual(n, 4, "2 个拉丁词 + 5 字中文折半")

    def test_empty(self):
        self.assertEqual(effective_words(""), 0)


class TestBreakdown(unittest.TestCase):
    def test_terms_plus_bonus_equals_total(self):
        b = quality_breakdown(ZH_ARTICLE, "<html><body><article></article></body></html>")
        self.assertAlmostEqual(b["total"], round(min(1.0, sum(b["terms"].values())
                                                    + b["bonus"]), 2), places=2)

    def test_single_source_of_formula(self):
        """公式只此一处：总分算法必须与分项一致，否则两者会各自漂移。"""
        for content in (ZH_ARTICLE, "word " * 300, "短"):
            self.assertEqual(_compute_quality(content, ""),
                             quality_breakdown(content, "")["total"])

    def test_drag_points_at_weakest_term(self):
        """拖累项要指向离自己满分最远的那个分量。"""
        short = quality_breakdown("短句。")
        self.assertIsNotNone(short["drag"])
        good = quality_breakdown(ZH_ARTICLE, "<article>" + "<p>x</p>" * 8 + "</article>")
        self.assertIsNone(good["drag"], f"高分内容不该报拖累项：{good}")

    def test_empty_content(self):
        b = quality_breakdown("")
        self.assertEqual(b["total"], 0.0)
        self.assertEqual(b["drag"], "empty")
        self.assertEqual(b["inputs"]["chars"], 0)

    def test_version_present(self):
        self.assertEqual(quality_breakdown(ZH_ARTICLE)["version"],
                         QUALITY_FORMULA_VERSION)

    def test_inputs_expose_raw_measurements(self):
        """依据要能追溯到原始测量值，而不是只给一个算完的分。"""
        b = quality_breakdown(ZH_ARTICLE)
        self.assertEqual(b["inputs"]["chars"], len(ZH_ARTICLE))
        self.assertGreater(b["inputs"]["cjk_chars"], 0)
        self.assertIn("has_structure", b["inputs"])


class TestLanguageNeutrality(unittest.TestCase):
    """语种不得成为扣分理由——同名网站、同等体量，分差应可忽略。"""

    def _score(self, text: str) -> dict:
        return quality_breakdown(text, "")

    def test_chinese_not_penalised_on_substance(self):
        zh = self._score(ZH_ARTICLE)
        self.assertGreaterEqual(
            zh["terms"]["word_count"], 0.3,
            f"中文词数项仍被压低：{zh['inputs']['word_count']} "
            f"（cjk={zh['inputs']['cjk_chars']}）")

    def test_equal_substance_scores_equally(self):
        """同等信息量下两种语言必须同分。

        不变量按**有效词数**对齐，不按字符数——等字符数不等于等信息量：
        2,000 个英文字符≈360 词，2,000 个中文字符≈1,000 有效词，两者本就
        不是一个体量。按字符数对齐会得出「中文分偏高」的伪结论。
        """
        # 两者各 300 有效词：英文 300 个词；中文 600 字按 0.5 折算
        en = "word " * 300
        zh = "中文字符内容" * 100
        en_b, zh_b = self._score(en), self._score(zh)
        self.assertAlmostEqual(en_b["terms"]["word_count"],
                               zh_b["terms"]["word_count"], delta=0.15,
                               msg="同等有效词数下 substance 项不一致")
        # 两者都未饱和时，总分应由同一套规则给出，差值只来自密度项的语言差异
        gap = abs(en_b["total"] - zh_b["total"])
        self.assertLess(gap, 0.06, f"语种间系统差仍有 {gap:.2f}")

    def test_saturated_at_comparable_substance(self):
        """各语言在各自「足够长」的体量上都应饱和——不因语种提高门槛。"""
        # 两者各 ≥500 有效词：英文 500 词；中文 1,002 字折算 501 词
        en = "word " * 500
        zh = "中文字符内容" * 167
        self.assertEqual(self._score(en)["terms"]["word_count"], 0.4)
        self.assertEqual(self._score(zh)["terms"]["word_count"], 0.4)

    def test_mixed_language_article(self):
        mixed = ("中文段落内容说明。 English paragraph follows. " * 120)
        b = self._score(mixed)
        self.assertGreater(b["terms"]["word_count"], 0.3)


class TestStaleScoreRescored(unittest.TestCase):
    """评分口径变更后，缓存里的旧分必须失效——就地从正文重算，不重新联网。"""

    def setUp(self):
        os.environ["ARGO_STATE_DIR"] = tempfile.mkdtemp(prefix="argo-qb-")

    def tearDown(self):
        os.environ.pop("ARGO_STATE_DIR", None)

    def test_old_version_rescored_and_labelled(self):
        import fetch_v3
        from cache import SearchCache
        url = "https://example.com/qb"
        cache = SearchCache()
        cache.set_fetch(url, {
            "url": url, "success": True, "content": ZH_ARTICLE,
            "length": len(ZH_ARTICLE), "title": "t",
            "quality_score": 0.31, "content_ok": True,
            "quality_breakdown": {"total": 0.31, "version": 1},
            "_max_chars": 8000,
        }, ttl=600)
        with patch("url_safety.check_url", return_value=(True, "")), \
             patch("robots_guard.robots_blocked", return_value=False):
            out = fetch_v3.fetch_v3(url, max_chars=8000, skip_cache=False)
        self.assertTrue(out.get("cached"))
        self.assertNotEqual(out.get("quality_score"), 0.31, "旧口径的分没被重算")
        self.assertEqual(out.get("quality_basis"), "rescored")
        self.assertEqual((out.get("quality_breakdown") or {}).get("version"),
                         QUALITY_FORMULA_VERSION)

    def test_current_version_not_rescored(self):
        import fetch_v3
        from cache import SearchCache
        url = "https://example.com/qb2"
        cache = SearchCache()
        cache.set_fetch(url, {
            "url": url, "success": True, "content": ZH_ARTICLE,
            "length": len(ZH_ARTICLE), "title": "t",
            "quality_score": 0.5, "content_ok": True,
            "quality_breakdown": {"total": 0.5,
                                  "version": QUALITY_FORMULA_VERSION},
            "_max_chars": 8000,
        }, ttl=600)
        with patch("url_safety.check_url", return_value=(True, "")), \
             patch("robots_guard.robots_blocked", return_value=False):
            out = fetch_v3.fetch_v3(url, max_chars=8000, skip_cache=False)
        self.assertIsNone(out.get("quality_basis"), "口径一致时不该标记重算")


if __name__ == "__main__":
    unittest.main()
