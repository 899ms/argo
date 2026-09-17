#!/usr/bin/env python3
"""readability_extract.py 的单元测试：正文提取、噪音排除、顺序保持。

P0 验收：
  1. 链接密集的导航/侧栏/页脚不进入正文
  2. 正文段落保持文档顺序（旧实现 sort-by-density 会把页脚插进正文）
  3. 标题提取正确
  4. 无正文页面返回空
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from readability_extract import (  # noqa: E402
    ReadabilityExtractor,
    extract_readability,
    score_blocks,
)


def _article_html() -> str:
    """标准文章页：导航 + 侧栏 + 正文 + 页脚。"""
    nav_links = "".join(
        f'<a href="/{i}">导航项目{i} 查看更多内容点击这里</a>' for i in range(8)
    )
    side_links = "".join(
        f'<a href="/tag/{i}">标签{i} 相关推荐更多内容</a>' for i in range(6)
    )
    return f"""<html><head><title>测试文章标题</title></head><body>
<nav>{nav_links}</nav>
<div class="sidebar">{side_links}</div>
<article>
  <h1>第一段正文标题内容</h1>
  <p>这是正文的第一段。讲述了人工智能与机器学习在现代社会中的应用场景，
  以及它们如何改变我们的日常工作和生活方式。内容充实且连贯，适合作为测试用例。</p>
  <p>这是正文的第二段。继续阐述深度学习模型在自然语言处理领域取得的重要进展，
  包括注意力机制与大规模预训练模型的技术细节与工程实践。</p>
  <p>这是正文的第三段。讨论模型部署与推理优化的挑战，包括量化、剪枝与蒸馏等
  技术路径，以及它们在真实业务系统中的落地效果。</p>
</article>
<div class="related">相关阅读 <a href="/r1">推荐文章一</a> <a href="/r2">推荐文章二</a></div>
<footer>© 2026 测试站 | <a href="/about">关于我们</a> <a href="/privacy">隐私政策</a></footer>
</body></html>"""


class TestReadabilityExtract(unittest.TestCase):
    def test_extracts_title(self):
        content, title = extract_readability(_article_html())
        self.assertEqual(title, "测试文章标题")

    def test_keeps_document_order(self):
        """正文三段的顺序必须保持（不按密度重排）。"""
        content, _ = extract_readability(_article_html())
        self.assertIn("第一段正文标题内容", content)
        self.assertIn("这是正文的第一段", content)
        self.assertIn("这是正文的第二段", content)
        self.assertIn("这是正文的第三段", content)
        first = content.index("这是正文的第一段")
        second = content.index("这是正文的第二段")
        third = content.index("这是正文的第三段")
        self.assertLess(first, second)
        self.assertLess(second, third)

    def test_excludes_nav_and_footer(self):
        """导航/侧栏/页脚不进入正文。"""
        content, _ = extract_readability(_article_html())
        self.assertNotIn("导航项目", content)
        self.assertNotIn("标签0", content)
        self.assertNotIn("关于我们", content)
        self.assertNotIn("隐私政策", content)
        self.assertNotIn("© 2026", content)

    def test_body_dominates(self):
        """正文应占输出绝大部分。"""
        content, _ = extract_readability(_article_html(), max_chars=8000)
        # 导航 8 项 + 侧栏 6 项都该被滤掉；正文 3 段全保留
        self.assertGreater(len(content), 120)

    def test_empty_html_returns_empty(self):
        content, title = extract_readability("<html><body></body></html>")
        self.assertEqual(content, "")
        self.assertEqual(title, "")

    def test_short_noise_only_page_returns_empty(self):
        """纯按钮/面包屑页（无正文）不应产出内容。"""
        html = "<html><body>" + "".join(
            f'<button>按钮{i} 提交</button>' for i in range(10)
        ) + "</body></html>"
        content, _ = extract_readability(html)
        self.assertEqual(content, "")

    def test_link_heavy_nav_beats_plain_text(self):
        """链接密集的导航块分数必须低于正文块。"""
        html = """<html><body>
        <div class="nav"><a href="/1">首页 链接文字很长很长很长</a><a href="/2">产品 链接文字很长很长很长</a></div>
        <div class="body"><p>这是唯一的一篇真正的正文内容，长度足够长，
        包含足够多的有效信息量，用来验证链接密集的导航块不会被误判为正文章节。</p></div>
        </body></html>"""
        content, _ = extract_readability(html)
        self.assertIn("唯一的一篇真正的正文", content)
        self.assertNotIn("首页", content)
        self.assertNotIn("产品", content)

    def test_table_data_without_blocks_survives(self):
        """纯文本表格（td 内无 p/li）不得随容器结束被清空丢失。"""
        html = """<html><body>
        <article>
        <h2>报价对照</h2>
        <table>
        <tr><td>型号</td><td>价格</td></tr>
        <tr><td>GTX-4090</td><td>15999 元</td></tr>
        </table>
        <p>以上为电商公开报价，时效以页面为准。</p>
        </article>
        </body></html>"""
        content, _ = extract_readability(html)
        self.assertIn("GTX-4090", content)
        self.assertIn("15999 元", content)
        # 单元格按块归并（相邻同深度），表格数据仍在正文里
        self.assertIn("型号", content)

    def test_plain_container_without_block_discarded(self):
        """纯链接/无正文容器（无块产出）的累积文本仍被丢弃（防污染）。"""
        html = """<html><body>
        <div class="sidebar"><a href="/a">甲项</a><a href="/b">乙项</a></div>
        <div class="body"><p>真正的正文段落，长度足够长，用来验证纯链接容器不会污染正文组。</p></div>
        </body></html>"""
        content, _ = extract_readability(html)
        self.assertIn("真正的正文段落", content)
        self.assertNotIn("甲项", content)

    def test_inline_link_text_preserved(self):
        """段落内行内链接的文字必须保留。

        历史 bug：锚文本只写进 _current_link（仅用于算链接密度惩罚），拼正文时
        被丢弃，导致 `See the <a>docs</a> here` 输出成 `See the  here`。
        链接密集页（Wikipedia）实测召回仅为对照实现的 21.7%。
        """
        html = """<html><body><div>
        <h1>DNS 解析</h1>
        <p>The <a href="/a">DNS protocol</a> is specified in
        <a href="/b">RFC 1035</a> and the resolver uses <a href="/c">port 53</a>
        by default for every lookup request.</p>
        </div></body></html>"""
        content, _ = extract_readability(html)
        self.assertIn("DNS protocol", content)
        self.assertIn("RFC 1035", content)
        self.assertIn("port 53", content)

    def test_pure_link_block_discarded(self):
        """整块文字都来自链接的块（导航项）整体丢弃。

        与行内链接是两回事：段落里的链接要留（否则句子断裂），纯链接块要丢
        （否则变成负分块，经 _group_score 拖垮同深度正文分组，实测会让
        MDN / Astro 文档页的正文输出缩水三分之一）。
        """
        html = """<html><body>
        <ul><li><a href="/1">Documentation Overview Guide Page</a></li>
        <li><a href="/2">API Reference Section Index</a></li></ul>
        <div><p>这是真正的正文段落，长度足够长，用来验证纯链接块被整体丢弃后
        不会残留、也不会拖累正文分组的得分。</p></div>
        </body></html>"""
        content, _ = extract_readability(html)
        self.assertNotIn("Documentation Overview Guide Page", content)
        self.assertIn("这是真正的正文段落", content)

    def test_pure_link_block_dropped_at_flush(self):
        """契约锁定：整块文字全来自链接的块不得进入候选集。

        这条与「行内链接保留」是一对：段落里的链接要留（丢了句子就断），
        纯链接块要丢——它们的分数是负的，留在与正文同深度的大分组里会把
        整个分组拖下去。实测真实页面（MDN）去掉这条后正文输出从 1,136 字
        掉到 500 字，Astro / docs.python.org 同样缩水约三成。

        这里直接断言块的产出契约，是因为该退步只在「存在竞争分组」时才显形，
        合成夹具很难稳定复现；退步的真实量级记录在上方。
        """
        # 导航项必须长过短块阈值（30 字），否则会被长度规则先丢掉，
        # 这条规则就永远测不到——这正是先前那版门失效的原因。
        html = """<html><body><article>
        <ul><li><a href="/a">Syntax 语法参考与全部请求方法的完整索引条目以及补充说明</a></li>
        <li><a href="/b">Methods 请求方法一览表与语义说明汇总索引以及注意事项</a></li></ul>
        <p>这是正文段落，内容足够长，用于触发块产出并验证行内链接不受影响。</p>
        </article></body></html>"""
        ext = ReadabilityExtractor()
        ext.feed(html)
        offenders = [b.text for b in ext._blocks
                     if b.link_chars and b.link_chars >= len(b.text)]
        self.assertEqual(offenders, [], f"纯链接块混入候选集: {offenders}")
        # 反向保护：段落里的行内链接不能因此被一起丢掉
        self.assertTrue(any("这是正文段落" in b.text for b in ext._blocks))

    def test_link_only_short_block_still_dropped(self):
        """短纯链接块（面包屑）不因锚文本入正文而复活。"""
        html = """<html><body>
        <div class="crumbs"><a href="/">首页</a><a href="/a">栏目</a></div>
        <div><p>正文段落内容足够长，用于验证面包屑不会因为锚文本保留而重新出现在输出里。</p></div>
        </body></html>"""
        content, _ = extract_readability(html)
        self.assertNotIn("首页", content)

    def test_table_row_relation_restored(self):
        """同一行的单元格必须留在同一行，否则表格数据读不出行列关系。

        历史形态：td 逐格 flush，输出成「型号A / 15999元」两条独立行，
        读的人看不出它们同属一行。
        """
        pad = "这一段正文足够长，用来隔离表格行为而不是被短块阈值整段丢弃。" * 2
        html = f"""<html><body><div><p>{pad}</p><table>
        <tr><td>型号A</td><td>15999元</td></tr>
        <tr><td>型号B</td><td>8888元</td></tr></table></div></body></html>"""
        content, _ = extract_readability(html, 4000)
        # 按整行判定：每一行自己占一行，行内单元格用 ` | ` 连接
        lines = [ln.strip() for ln in content.split("\n") if ln.strip()]
        self.assertIn("型号A | 15999元", lines, f"第一行不是独立一行：{lines}")
        self.assertIn("型号B | 8888元", lines, f"第二行不是独立一行：{lines}")

    def test_single_cell_row_not_joined_to_next(self):
        """一行一格时不得与下一行粘连。"""
        pad = "这一段正文足够长，用来隔离表格行为而不是被短块阈值整段丢弃。" * 2
        html = f"""<html><body><div><p>{pad}</p><table>
        <tr><td>表头甲</td></tr><tr><td>数据乙</td></tr></table></div></body></html>"""
        content, _ = extract_readability(html, 4000)
        self.assertIn("表头甲", content)
        self.assertIn("数据乙", content)
        self.assertNotIn("表头甲 | 数据乙", content, "跨行被错误连接")

    # ── 结构还原（HTML → Markdown 结构）──────────────────────────────────
    #
    # 没有这层时输出是纯文本流：标题、列表项、代码、引用全部退化成普通行。
    # 判定一律基于「纯文本视图」——Markdown 语法会让文本明显变长，而
    # link_chars 只数锚文本，用带语法的文本做判据会让整块都是链接的导航块
    # 漏进正文（实测 Wikipedia 的导航框不是 <nav> 标签，靠的正是这条判据）。

    _PAD = "这一段正文足够长，用来隔离结构行为而不是被短块阈值整段丢弃。" * 2

    def test_headings_get_level_prefix(self):
        html = f"""<html><body><div><h1>一级标题文字</h1><h2>二级标题文字</h2>
        <p>{self._PAD}</p></div></body></html>"""
        content, _ = extract_readability(html, 8000)
        self.assertIn("# 一级标题文字", content)
        self.assertIn("## 二级标题文字", content)

    def test_unordered_and_ordered_lists(self):
        # 列表项必须明确越过 li 的 30 字下限（含后缀也叫不准容易踩线，
        # 实测已因此误判两次）
        li = "列表项的内容写得足够长，长到足以形成独立块并参与打分流程的完整说明"
        html = f"""<html><body><div><ul><li>{li}甲</li><li>{li}乙</li></ul>
        <ol><li>{li}丙</li><li>{li}丁</li></ol>
        <p>{self._PAD}</p></div></body></html>"""
        content, _ = extract_readability(html, 8000)
        self.assertIn(f"- {li}甲", content, "无序列表缺少 - 前缀")
        self.assertIn(f"1. {li}丙", content, "有序列表未编号")
        self.assertIn(f"2. {li}丁", content, "有序列表编号未递增")

    def test_code_block_gets_fence_and_language(self):
        code = "def compute(rows, seconds):\n    return sum(rows) / max(seconds, 1)"
        html = f"""<html><body><div>
        <pre><code class="language-python">{code}</code></pre>
        <p>{self._PAD}</p></div></body></html>"""
        content, _ = extract_readability(html, 8000)
        self.assertIn("```python", content, "代码围栏缺少语言标记")
        self.assertIn("def compute(rows, seconds):", content)
        self.assertIn("return sum(rows)", content, "代码内容被压缩成一行")

    def test_inline_link_keeps_href(self):
        html = f"""<html><body><div><p>见 <a href="https://e.com/doc">说明文档</a>
        以及其余补充文字，使该段长度稳定超过最小块阈值要求。</p></div></body></html>"""
        content, _ = extract_readability(html, 8000)
        self.assertIn("[说明文档](https://e.com/doc)", content)

    def test_image_becomes_markdown(self):
        html = f"""<html><body><div><p>下图给出对照：<img src="/a.png" alt="对照图">
        详细说明见后文附录章节，此处不再展开。</p></div></body></html>"""
        content, _ = extract_readability(html, 8000)
        self.assertIn("![对照图](/a.png)", content)

    def test_blockquote_prefixed(self):
        html = f"""<html><body><div><blockquote>这是一段引用文字，内容写得足够长，
        足以形成独立块并参与正文打分。</blockquote></div></body></html>"""
        content, _ = extract_readability(html, 8000)
        self.assertIn("> 这是一段引用文字", content)

    def test_link_list_without_nav_tag_still_excluded(self):
        """导航块不一定是 <nav> 标签——纯链接列表必须照样被过滤。

        这是结构层引入的真实风险：链接在输出里变成 `[文字](地址)` 之后，
        块文本远长于 link_chars，用带语法的文本做判据就判不出「整块都是链接」。
        实测 Wikipedia 的导航框（<div> 里一堆 <li><a>）因此漏进正文。
        """
        # 锚文本必须**长过**块长度下限（30 字）：短于它会被 min_chars 先丢掉，
        # 纯链接规则根本轮不到，门就测不出东西。此处按长度生成而非手写——
        # 凭眼睛数中文字数已连续数错多次，写死长度并在下方断言才可靠。
        anchor = "导航链接文字" * 6          # 36 字，明确越过 30
        assert len(anchor) >= 35, "锚文本必须明确越过长度下限"
        items = "".join(
            f'<li><a href="/p/{i}">{anchor}{i}</a></li>' for i in range(6))
        # 关键：链接列表与正文**同深度**。若把它再包一层 <div>，导航块会因深度
        # 不同被分组阈值滤掉，纯链接规则根本没轮到——那样的夹具测的是分组，
        # 不是本规则（实测第一版夹具正是如此，变异测试显示该门无牙）。
        html = f"""<html><body><div><ul>{items}</ul>
        <p>{self._PAD}</p></div></body></html>"""
        content, _ = extract_readability(html, 8000)
        self.assertNotIn("门户条目 3", content, f"纯链接导航块漏进正文：{content[:200]!r}")
        self.assertIn("这一段正文足够长", content, "正文被误伤")

    def test_short_block_still_dropped_after_prefix(self):
        """结构前缀不得让短块蒙混过关：判据看纯文本长度，不看带前缀的长度。"""
        # 纯文本远低于阈值，而带 Markdown 语法的文本（长 href 展开后）越过
        # 阈值——只有这样两种判据才会给出不同结论，门才有区分力。
        # 若两者都低于阈值，无论用哪种判据都丢弃，测不出判据是否正确。
        long_a = "/documentation/reference/very-long-path-segment"
        long_b = "/documentation/guides/another-long-path-segment"
        html = f"""<html><body><div>
        <p>说明<a href="{long_a}">甲</a>与<a href="{long_b}">乙</a></p>
        <p>{self._PAD}</p></div></body></html>"""
        content, _ = extract_readability(html, 8000)
        self.assertNotIn("[甲]", content, f"短块被链接语法救活了：{content!r}")
        self.assertIn("这一段正文足够长", content, "正文被误伤")

    def test_score_blocks_placeholder_keeps_order(self):
        """P1 占位：无 query 精排时保持原顺序。"""
        parts = ["第一段", "第二段", "第三段"]
        ranked = score_blocks("", parts)
        self.assertEqual([p for _, p in ranked], parts)


if __name__ == "__main__":
    unittest.main()
