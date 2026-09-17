#!/usr/bin/env python3
"""focus_extract 选块质量回归门：目录排除、样板排除、额度与选块的先后关系。

背景（2026-09 实测，RFC 3986 全文）：对「6.2.2 Syntax-Based Normalization」
执行聚焦，返回的是题头与目录，目标小节正文一个字没有。拆开看是三个独立缺陷，
每一个都足以让功能失效，故各自成门：

  1. 目录块把所有章节标题聚在一起，词密度碾压正文段落
  2. 运行页眉/页脚等重复短块因 BM25 长度归一化而集体胜出
  3. max_chars 在聚焦**之前**生效，聚焦只能在文档头部选段；
     即便选对了，结果又按文档顺序输出并被截断，靠后的高相关块照样被砍掉

三者都修好，聚焦才谈得上「选得中」。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from focus_extract import (  # noqa: E402
    _tokens,
    _boilerplate_blocks,
    _fit_to_budget,
    _is_toc_block,
    _split_blocks,
    apply_focus,
    focus_extract,
    focus_fetch_chars,
)


# 注意：focus_extract 对短于 2000 字符的文本直接原样返回（不值得裁剪），
# 因此所有夹具都必须长过这个阈值，否则测的就不是选块逻辑。
_MIN_FOCUS_LEN = 2000


def _pad(n: int = 120) -> str:
    return "补充正文内容。" * (n // 7 + 1)


def _toc_text() -> str:
    """带引导点目录的长文本（RFC/论文形态），长度超过聚焦阈值。"""
    toc = "\n".join(
        f"   {i}.  Section Title Number {i} . . . . . . . . . . . . . . . {10+i}"
        for i in range(1, 12)
    )
    def _section(i: int) -> str:
        # 第 3、7 节带一个只属于它们的标记词，用来验证「选得中正文」
        mark = "以及 zzzalpha 这一独有主题的处理方式。" if i in (3, 7) else ""
        return (f"## {i}. Section Title Number {i}\n\n这是第 {i} 节的正文段落，讲述 "
                f"Section Title Number 相关主题的具体内容与实现细节，长度足够构成"
                f"一个独立段落。{mark}{_pad(160)}")

    body = "\n\n".join(_section(i) for i in range(1, 12))
    text = f"Document Title\n\nTable of Contents\n\n{toc}\n\n{body}\n"
    assert len(text) > _MIN_FOCUS_LEN, f"夹具过短（{len(text)}），测不到选块逻辑"
    return text


class TestTocDetection(unittest.TestCase):
    def test_leader_dot_toc_detected(self):
        toc = "\n".join(f"6.2.2.  Syntax-Based Normalization . . . . . . 40"
                        for _ in range(6))
        self.assertTrue(_is_toc_block(toc))

    def test_markdown_toc_detected(self):
        toc = "\n".join(f"- [Getting Started](#getting-started-{i})"
                        for i in range(6))
        self.assertTrue(_is_toc_block(toc))

    def test_normal_paragraph_not_toc(self):
        self.assertFalse(_is_toc_block("这是一段普通正文，没有任何索引特征。"))
        self.assertFalse(_is_toc_block(""))

    def test_two_line_list_not_toc(self):
        """不足 4 行不成目录，避免误伤普通列表。"""
        self.assertFalse(_is_toc_block("- [a](#a)\n- [b](#b)"))

    def test_toc_block_not_selected(self):
        """目录块不得进入聚焦结果。

        夹具必须让目录**本来会赢**，否则测不到这条过滤：目录把全部章节标题
        聚成一块，标题词在该块的密度天然高于任何单个正文块，于是它在词面
        打分上碾压正文（实测去掉过滤后，输出是 8 行目录）。
        注意断言只认目录行本身——目录里的章节标题与正文标题同形，
        拿标题词当断言会被目录自己的文字骗过。
        """
        text = _toc_text()
        out = focus_extract(text, "Section Title Number", top_k=5)
        leaders = [ln for ln in out.splitlines() if ". . ." in ln]
        self.assertEqual(leaders, [], f"目录行出现在聚焦结果里: {leaders[:3]}")
        self.assertGreater(len(out.strip()), 0, "输出不能为空")


class TestBoilerplateDetection(unittest.TestCase):
    def test_repeated_header_detected(self):
        text = "\n\n".join(["RFC 3986   URI Generic Syntax   January 2005"] * 5
                           + ["真正的正文段落内容，需要足够长以构成一个块。"])
        blocks = _split_blocks(text)
        drop = _boilerplate_blocks(blocks)
        self.assertEqual(len(drop), 5)

    def test_long_repeated_paragraph_kept(self):
        """长段落即便重复也不判样板（可能是合法的重复引用/模板正文）。"""
        para = "长" * 400
        blocks = _split_blocks("\n\n".join([para] * 5))
        self.assertEqual(_boilerplate_blocks(blocks), set())

    def test_repeated_block_not_selected(self):
        """运行页眉含查询词时不得抢占正文。

        页眉短、每页重复，BM25 的长度归一化偏爱短块，于是十几份副本会集体
        压过正文（实测去掉过滤后输出被 10 份页眉灌满、正文比例被挤到零）。
        断言用页眉全文计数——它够长够独特，不会被正文偶然命中。
        """
        header = "alpha beta gamma handbook edition 2026"
        body = ("## 4. 核心章节\n\n这一节说明 alpha beta gamma 在工程上的取舍"
                "与边界条件，包含若干实现细节。")
        filler = "\n\n".join(f"无关段落 {i}\n\n" + "普通填充内容。" * 30
                             for i in range(10))
        text = "\n\n".join([header] * 10) + "\n\n" + body + "\n\n" + filler
        assert len(text) > _MIN_FOCUS_LEN
        out = focus_extract(text, "alpha beta gamma 工程取舍", top_k=5)
        self.assertEqual(out.count(header), 0, "重复页眉出现在聚焦结果里")
        self.assertIn("工程上的取舍", out)


class TestTokenization(unittest.TestCase):
    """分词：版本号整体成词、CJK 单字保留（聚焦选段的前提）。"""

    def test_version_number_is_one_token(self):
        """`6.2.2` 必须整体成词。

        原式会把它切成 6/2/2，再被长度下限逐个滤掉——查询里的章节号等于
        没写，对 RFC 这类以编号定位的文档是致命的。
        """
        toks = _tokens("6.2.2 Syntax-Based Normalization")
        self.assertIn("6.2.2", toks)
        self.assertIn("syntax", toks)

    def test_section_number_in_query_matches_document(self):
        q = set(_tokens("6.2.3 Scheme-Based Normalization"))
        doc = set(_tokens("see 6.2.3 Scheme-Based Normalization for details"))
        self.assertTrue(q & doc, "查询与文档的章节号无法对上")

    def test_single_cjk_char_kept(self):
        """单个汉字本身就是一个词，不该被长度下限滤掉。"""
        self.assertIn("茶", _tokens("茶 文化"))

    def test_single_latin_letter_dropped(self):
        self.assertNotIn("a", _tokens("a b cd"))

    def test_alnum_word_not_split(self):
        self.assertIn("ipv6", _tokens("IPv6 transition"))


class TestFetchBudget(unittest.TestCase):
    def test_budget_enlarged_only_with_query(self):
        self.assertEqual(focus_fetch_chars(8000, ""), 8000)
        self.assertEqual(focus_fetch_chars(8000, None), 8000)
        self.assertGreater(focus_fetch_chars(8000, "关键词"), 8000)

    def test_budget_has_absolute_floor(self):
        self.assertGreaterEqual(focus_fetch_chars(500, "关键词"), 50000)

    def test_selection_respects_budget(self):
        """选块阶段就按额度择优，输出不得超出 max_chars。"""
        text = "\n\n".join(f"段落 {i} " + "内容填充。" * 40 for i in range(60))
        r = {"content": text, "length": len(text), "success": True}
        out = apply_focus(r, "段落 7", top_k=5, max_chars=2000)
        self.assertLessEqual(len(out["content"]), 2000)
        self.assertEqual(out["length"], len(out["content"]))

    def test_fit_to_budget_never_empties_a_nonempty_set(self):
        """额度装不下任何完整块时，不得返回空集。

        返回空集的后果不是「少给一点」，而是输出只剩头部提示行、调用方却
        以为聚焦成功。实测场景：单个小节 8,452 字符 + 默认 max_chars=8000，
        正文会一个字都不剩。宁可交出被截断的正文。
        """
        blocks = ["x" * 5000, "y" * 20]
        scores = [9.0, 1.0]
        # budget=10：连次小的块（22 字符）都装不下，装填结果必为空
        out = _fit_to_budget([0, 1], blocks, scores, budget=10)
        self.assertTrue(out, "装不下就返回空集了")
        self.assertEqual(out, [0], "应保留最高分块")

    def test_fit_to_budget_drops_lowest_scores_first(self):
        """额度不足时按分数从低到高丢，保住高相关块。"""
        blocks = ["a" * 100, "b" * 100, "c" * 100]
        scores = [1.0, 9.0, 2.0]
        out = _fit_to_budget([0, 1, 2], blocks, scores, budget=210)
        self.assertIn(1, out, "最高分块必须留下")
        self.assertNotIn(0, out, "最低分块应先被丢掉")

    def test_fit_to_budget_passthrough_when_it_fits(self):
        blocks = ["a" * 10, "b" * 10]
        self.assertEqual(_fit_to_budget([0, 1], blocks, [1.0, 2.0], budget=10_000),
                         [0, 1])
        self.assertEqual(_fit_to_budget([0, 1], blocks, [1.0, 2.0], budget=0),
                         [0, 1], "budget<=0 表示不限，原样返回")

    def test_bodyless_focus_is_not_reported_as_applied(self):
        """聚焦后正文为空时必须退回全文并标未生效。

        交出「只剩一行提示」比不聚焦更糟：调用方会以为省到了 token，
        实际拿到了空正文。这里把选块函数替换成「只吐头部提示行」，
        精确打靶这条守卫——否则该分支在正常路径上不可达，测不到。
        """
        original = "正文原文，长度足够。" * 300
        r = {"content": original, "length": len(original), "success": True}

        import focus_extract as fx
        saved = fx.focus_extract
        fx.focus_extract = lambda content, query, **kw: "[Focus: x; showing 0 of 0 blocks.]\n\n"
        try:
            out = apply_focus(r, "任意查询", top_k=5, max_chars=8000)
        finally:
            fx.focus_extract = saved

        self.assertFalse(out["focus_applied"], "只剩提示行却标了已生效")
        self.assertEqual(out["content"], original, "应退回全文")

    def test_no_budget_keeps_legacy_behavior(self):
        """不传 max_chars 时行为与从前一致（只按阈值选，不裁剪）。"""
        text = "\n\n".join(f"段落 {i} " + "内容。" * 50 for i in range(30))
        out = focus_extract(text, "段落 3", top_k=5)
        self.assertIn("段落 3", out)


if __name__ == "__main__":
    unittest.main()
