#!/usr/bin/env python3
"""
readability_extract.py — 正文提取（readability 密度法，纯标准库）

设计（P0，自研实现，思路对标 Mozilla readability / crawl4ai fit-markdown，
不复制任何第三方源码）：
  1. 块级密度评分：score = (文本 - 2×链接文本) / (块字符数 + 1)
     链接越多的块（导航/侧栏/页脚）分数越低，无需 query 即可区分正文与噪音。
  2. 标签权重：article/main 加权，li/h1-h4 降权，div/p 基准。
  3. 容器归并：同深度的相邻块合并为同一正文段，保持文档顺序输出
     （旧实现 sort-by-density 会把页脚插进正文中间）。
  4. 可选第二级精排接口（P1）：score_blocks(query, blocks) 占位，
     query 缺失时纯第一级生效。

零外部依赖，单次调用毫秒级。供 fetch_v3 等接入。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any, Callable, Sequence
from net_proxy import open_url  # 出口调度唯一入口（issue #13 同类修复）

SKIP_TAGS = {
    "script", "style", "nav", "header", "footer", "aside", "noscript",
    "iframe", "form", "button", "dialog",
}
# 容器级标签：块归并的候选父级（出现在 starttag 时开启一个新容器层）
CONTAINER_TAGS = {"div", "article", "main", "section", "blockquote"}
# 段落级标签：正文块的边界（td 列其中：单元格是块级单元，结束即 flush，
# 否则纯文本表格（无 p/li）的数据会随容器清空逻辑整块丢失）
BLOCK_TAGS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "pre", "td"}

# 标签权重：正文候选得分乘数
TAG_WEIGHT = {
    "article": 1.5, "main": 1.5, "blockquote": 1.2, "p": 1.1,
    "td": 1.0, "div": 1.0, "section": 0.9, "pre": 1.0,
    "h1": 0.6, "h2": 0.6, "h3": 0.6, "h4": 0.6, "h5": 0.6, "h6": 0.6,
    "li": 0.5, "tr": 0.5,
}
MIN_BLOCK_CHARS = 30  # 短块直接丢弃（按钮/面包屑/导航项）
MAX_BLOCK_CHARS = 6000  # 超过视为容器包全页，不额外加分（防 div 吞全页）
# 长文本块需要的密度下限（去空字符比例）：正文段落通常 >0.6
MIN_DENSITY = 0.55


class TextBlock:
    """一个正文候选块。"""

    __slots__ = ("text", "link_chars", "depth", "weight", "start", "end",
                 "is_heading", "kind", "row_id")

    def __init__(self, text: str, link_chars: int, depth: int, weight: float,
                 is_heading: bool = False, kind: str = "text", row_id: int = -1):
        self.text = text
        self.link_chars = link_chars
        self.depth = depth
        self.weight = weight
        self.start = -1  # 文档序占位，由收集器赋值
        self.end = -1
        self.is_heading = is_heading
        # kind="cell" 的块来自表格单元格。表格是二维数据，逐格输出成多条独立
        # 行之后行列关系就没了——实测一张两行表会变成「合并表头 / 型号A /
        # 15999元」三行，读的人看不出后两者是同一行。row_id 记录它属于哪一行，
        # 供组装阶段还原成「单元格 | 单元格」的一行。
        self.kind = kind
        self.row_id = row_id

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def density(self) -> float:
        """去空格字符占比（块文本的紧凑度）。"""
        non_space = len(self.text.replace(" ", "").replace("\n", ""))
        return non_space / max(self.chars, 1)

    @property
    def score(self) -> float:
        """readability 密度分：惩罚链接文本，加权标签。"""
        base = (self.chars - 2 * self.link_chars) / max(self.chars + 1, 1)
        # 密度惩罚：导航/侧栏通常文本稀疏（链接密集），正文段落紧凑
        base *= self.density / (MIN_DENSITY * 1.2)
        base *= self.weight
        if self.chars < 80 and not self.is_heading:
            base *= 0.6  # 短块降权（可能是导航项）；标题短但信息密度高，不降权
        if self.chars > MAX_BLOCK_CHARS:
            base *= MAX_BLOCK_CHARS / self.chars  # 超长块降权（容器吞全页）
        return base


class ReadabilityExtractor(HTMLParser):
    """基于 readability 密度法的正文提取器（保持文档顺序）。"""

    def __init__(self):
        super().__init__()
        self._in_skip = 0
        self._link_depth = 0
        self._current: list[str] = []
        self._current_link: list[str] = []
        self._depth = 0
        self._blocks: list[TextBlock] = []
        self._container_stack: list[int] = []  # 当前容器层级的块索引起点
        self._row_id = 0
        self.title = ""
        self._in_title = False
        # ── 结构还原状态（HTML → Markdown 结构）──
        # 没有这层时输出是纯文本流：标题、列表项、代码、引用全部退化成普通行，
        # 表格只剩「A | B」而看不出是不是表格。实测结构保真 0/5。
        self._current_plain: list[str] = []   # 与 _current 同步，但不含 Markdown 语法
        self._inline_stack: list[dict] = []   # 行内包裹（a / code）
        self._pre_depth = 0                   # 处于 <pre> 内的深度
        self._code_buf: list[str] = []        # <pre> 内的原始文本
        self._code_lang = ""                  # code 的 language-xxx
        self._list_stack: list[str] = []      # "ul" / "ol"
        self._ol_count: list[int] = []        # 有序列表计数器
        self._quote_depth = 0                 # 引用嵌套深度

    # ── HTMLParser 回调 ──────────────────────────────────────────────

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in SKIP_TAGS:
            self._in_skip += 1
            return
        if self._in_skip:
            return
        a = dict(attrs)
        if tag == "img":
            # 图片是空元素：在起点就地成文，不依赖 endtag
            alt = (a.get("alt") or "").strip()
            src = (a.get("src") or "").strip()
            if src:
                self._current.append(f"![{alt}]({src})")
                # alt 是图片的可见文本，必须计入纯文本视图：否则「只有一句话
                # 配一张图」的段落会因文本过短被整块丢弃，图片跟着一起消失
                if alt:
                    self._current_plain.append(alt)
        if tag in CONTAINER_TAGS | BLOCK_TAGS:
            self._depth += 1
        if tag == "a":
            self._link_depth += 1
            self._inline_stack.append({"kind": "a", "href": (a.get("href") or "").strip(),
                                       "buf": []})
        elif tag == "code":
            cls = a.get("class") or ""
            m = re.search(r"language-([\w+#-]+)", cls)
            if self._pre_depth:
                # <pre><code class="language-x"> —— 语言标记取自此层
                if m:
                    self._code_lang = m.group(1)
            else:
                self._inline_stack.append({"kind": "code", "href": "", "buf": []})
        elif tag == "pre":
            self._pre_depth += 1
            self._code_buf = []
            self._code_lang = ""
        elif tag in ("ul", "ol"):
            self._list_stack.append(tag)
            if tag == "ol":
                self._ol_count.append(0)
        elif tag == "blockquote":
            self._quote_depth += 1
        if tag in CONTAINER_TAGS:
            self._container_stack.append(len(self._blocks))

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in SKIP_TAGS:
            self._in_skip = max(0, self._in_skip - 1)
            return
        if self._in_skip:
            return
        if tag == "a":
            self._link_depth = max(0, self._link_depth - 1)
        if tag in ("a", "code") and self._inline_stack:
            w = self._inline_stack.pop()
            txt = "".join(w["buf"]).strip()
            if txt:
                self._current_plain.append(txt)
            if txt:
                if w["kind"] == "a" and w["href"]:
                    self._current.append(f"[{txt}]({w['href']})")
                elif w["kind"] == "code":
                    self._current.append(f"`{txt}`")
                else:
                    self._current.append(txt)
        if tag == "pre":
            self._pre_depth = max(0, self._pre_depth - 1)
            if self._pre_depth == 0:
                raw = "".join(self._code_buf).strip("\n")
                self._code_buf = []
                if raw:
                    fence = f"```{self._code_lang}" if self._code_lang else "```"
                    self._current.append(f"{fence}\n{raw}\n```")
                self._code_lang = ""
        if tag in ("ul", "ol") and self._list_stack:
            popped = self._list_stack.pop()
            if popped == "ol" and self._ol_count:
                self._ol_count.pop()
        if tag == "blockquote":
            self._quote_depth = max(0, self._quote_depth - 1)
            # blockquote 是容器不是块标签，自身不触发 flush；不在此收尾，
            # 引文会被并进下一个块，`> ` 前缀就贴到了无关段落上
            if self._current and "".join(self._current).strip():
                self._flush_block("blockquote")
        if tag in CONTAINER_TAGS:
            start = self._container_stack.pop() if self._container_stack else None
            # 仅当容器内**没有产出任何正文块**时（纯链接侧栏/导航），丢弃累积文本，
            # 避免污染后续同深度块的 link_chars（负数 score 会拖垮正文组）。
            # 容器内已有正文块（如表格 td 数据）必须保留累积，否则 td/p 之外的
            # 剩余文本（残句、表格数据）被整个丢掉。
            if start is not None and len(self._blocks) == start:
                self._current = []
                self._current_link = []
                self._current_plain = []
        if tag == "tr":
            # 行结束：后续单元格归入下一行。必须在 _flush_block 之前递增，
            # 且 tr 自身不产块（表格数据由 td 逐格产出）。
            self._row_id += 1
        elif tag in BLOCK_TAGS:
            self._flush_block(tag)
        if tag in CONTAINER_TAGS | BLOCK_TAGS:
            self._depth = max(0, self._depth - 1)

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self._in_skip or not data.strip():
            return
        # 锚文本必须同时进正文：链接文字是句子的一部分，丢了就成了
        # 「See the  for the full harness」。_current_link 保留同样的文本
        # 只为算 link_chars（链接密度惩罚），两个用途互不替代——惩罚靠
        # 计分（见 TextBlock.score），不是靠删除。
        if self._pre_depth:
            # 代码块内原样收集：缩进与换行都是内容，不能被当作空白丢弃
            self._code_buf.append(data)
            self._current_plain.append(data)
            return
        if self._inline_stack:
            # 行内链接/行内代码：先入缓冲，收尾时整体成文（见 handle_endtag），
            # 这样 [文字](地址) 才会作为一个整体落在句子的正确位置上。
            # 此时**不得**同步记入 _current_plain：收尾时会把锚文本补记一次，
            # 两处都记等于让纯文本视图翻倍，纯链接判据随即失效（实测导航块
            # 因此漏出）。
            self._inline_stack[-1]["buf"].append(data)
        else:
            self._current.append(data)
            self._current_plain.append(data)
        if self._link_depth > 0:
            self._current_link.append(data)

    # ── 内部 ─────────────────────────────────────────────────────────

    def _struct_prefix(self, tag: str) -> str:
        """块级结构前缀（Markdown）。`li` 会推进有序列表计数。"""
        if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
            return "#" * int(tag[1]) + " "
        if tag == "li":
            if self._list_stack and self._list_stack[-1] == "ol" and self._ol_count:
                self._ol_count[-1] += 1
                return f"{self._ol_count[-1]}. "
            return "- "
        return ""

    def _flush_block(self, tag: str) -> None:
        # raw 保留不加前缀的原文：纯链接判定与长度判定都必须基于它。
        # 若先加前缀，`- ` 这两个字符会让导航块的 `link_chars >= len(text)`
        # 不再成立，之前修好的纯链接块过滤会被打回去。
        raw = "".join(self._current).strip()
        # 纯文本视图：判据一律基于它。Markdown 语法（`](href)`、`# `、`- `）
        # 会让 raw 明显变长，而 link_chars 只数锚文本——用 raw 做判据时
        # 「整块都是链接」的导航块不再被判出，实测 Wikipedia 的导航框因此
        # 漏进正文（它不是 <nav> 标签，靠的正是这条判据）。
        plain = "".join(self._current_plain).strip()
        link_chars = len("".join(self._current_link).strip())
        self._current = []
        self._current_link = []
        self._current_plain = []
        # 纯链接块（整块文字都来自 <a>，如导航项/面包屑/页脚链接）整体丢弃。
        # 与「行内链接」必须区分开：段落里的链接是句子的一部分，丢了句子就断了
        # （见 handle_data）；而整块只有链接的块是导航噪声，留着会变成负分块，
        # 经 _group_score 拖垮同深度的正文分组——实测会让 MDN / Astro 文档页
        # 的正文输出反而缩水三分之一。
        if link_chars and link_chars >= len(plain):
            return
        # 标题类标签（h1-h6）放宽短块阈值：文章标题/小节标题信息密度高，
        # 30 字符下限会把「第一段正文标题内容」这类短标题丢掉。
        # td 单元格同理：表格是原子数据单元（「型号」「15999 元」），
        # 30 字符下限会把整张表格的数据全部丢光；2 字符下限只丢单字噪声。
        is_heading = tag.startswith("h") and len(tag) == 2 and tag[1].isdigit()
        min_chars = 2 if tag == "td" else (6 if is_heading else MIN_BLOCK_CHARS)
        if len(plain) < min_chars:
            return
        if tag == "blockquote":
            # 引用逐行加前缀，否则多行引文只有首行属于引用
            text = "\n".join("> " + ln for ln in raw.split("\n") if ln.strip())
        else:
            text = self._struct_prefix(tag) + raw
        weight = TAG_WEIGHT.get(tag, 1.0)
        kind = "cell" if tag in ("td", "th") else "text"
        block = TextBlock(text, link_chars, self._depth, weight,
                          is_heading=is_heading, kind=kind, row_id=self._row_id)
        block.start = len(self._blocks)
        block.end = block.start + 1
        self._blocks.append(block)

    # ── 结果组装 ─────────────────────────────────────────────────────

    def extract(self, max_chars: int = 8000) -> list[str]:
        """按文档顺序返回正文段（容器归并后）。"""
        if not self._blocks:
            return []
        # 按容器深度分组归并：同一深度且相邻的块合并成一段
        merged: list[tuple[float, list[TextBlock]]] = []
        current_group: list[TextBlock] = []
        current_depth: int | None = None

        for block in self._blocks:
            if current_depth is None or block.depth == current_depth:
                current_group.append(block)
                current_depth = block.depth
            else:
                merged.append((self._group_score(current_group), current_group))
                current_group = [block]
                current_depth = block.depth
        if current_group:
            merged.append((self._group_score(current_group), current_group))

        # 保留与最高分组同量级的组：导航/页脚得分通常差 3-6 倍，
        # 用 max*0.6 比固定分位稳（分位会把「第二高但仍是噪音」的组带上）。
        if not merged:
            return []
        max_score = max(s for s, _ in merged)
        threshold = max_score * 0.6
        kept = [g for s, g in merged if s >= threshold]
        kept.sort(key=lambda g: g[0].start)  # 文档顺序
        parts: list[str] = []
        total = 0
        for group in kept:
            text = self._join_group(group)
            if not text.strip():
                continue
            # max_chars <= 0 表示不限量（取全文，供全文存档用）
            if max_chars > 0 and total + len(text) > max_chars:
                remaining = max_chars - total
                if remaining > 200:
                    parts.append(text[:remaining].rstrip())
                break
            parts.append(text)
            total += len(text)
        return parts

    @staticmethod
    def _join_group(blocks: Sequence[TextBlock]) -> str:
        """组装一组块：同行单元格用 ` | ` 连接，其余块逐行。

        表格不还原成 Markdown 管道表（那需要列对齐信息，而 colspan 场景下
        对齐本身就是坏的），只把**同行关系**还原出来——这是「读不出行列」
        与「能读出行列」的分界，成本极低且不引入格式依赖。
        """
        parts: list[str] = []
        cells: list[str] = []
        cur_row = None
        for b in blocks:
            if b.kind == "cell":
                if cells and b.row_id != cur_row:
                    parts.append(" | ".join(cells))
                    cells = []
                cells.append(b.text)
                cur_row = b.row_id
                continue
            if cells:
                parts.append(" | ".join(cells))
                cells = []
                cur_row = None
            parts.append(b.text)
        if cells:
            parts.append(" | ".join(cells))
        return "\n".join(parts)

    @staticmethod
    def _group_score(blocks: Sequence[TextBlock]) -> float:
        """一组块的总分：求和归一化到块数，避免容器内块数虚高。"""
        if not blocks:
            return 0.0
        return sum(b.score for b in blocks) / max(len(blocks) ** 0.5, 1)


# ── 模块级接口 ─────────────────────────────────────────────────────────


def extract_readability(html: str, max_chars: int = 8000) -> tuple[str, str]:
    """提取正文（readability 密度法）与标题。

    max_chars <= 0 取全文不截断——抓取链靠它拿到完整正文再决定交付多少，
    被截掉的部分进全文存档（见 fulltext_store）。
    """
    ext = ReadabilityExtractor()
    try:
        ext.feed(html)
    except Exception:
        pass
    parts = ext.extract(max_chars=max_chars)
    return "\n\n".join(parts), ext.title.strip()


def score_blocks(query: str, parts: Sequence[str]) -> list[tuple[float, str]]:
    """P1 预留：按 query 对正文段精排（BM25/余弦占位，未实现）。

    P0 阶段保持原顺序；接入 query 精排时在此实现并替换调用点。
    """
    _ = query
    return [(0.0, p) for p in parts]


if __name__ == "__main__":
    import sys
    import urllib.request

    url = sys.argv[1] if len(sys.argv) > 1 else "https://docs.python.org/3/"
    req = urllib.request.Request(url, headers={"User-Agent": "unified-search/2.5"})
    with open_url(req, timeout=10) as r:
        raw = r.read(400000).decode("utf-8", errors="replace")
    content, title = extract_readability(raw, max_chars=2000)
    print(f"标题: {title}")
    print(f"正文长度: {len(content)}")
    print(content[:600])
