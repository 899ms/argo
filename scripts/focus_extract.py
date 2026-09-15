"""BM25 聚焦提取 — 从长文本中提取与查询最相关的段落。

移植自 Hound 的 focus.py，简化为独立函数。
纯 Python 实现（re + math），无外部依赖。

BM25 参数：k1=1.5, b=0.75，使用 BM25+ 风格的正 IDF，
确保单个匹配词也能获得正分数。
"""

from __future__ import annotations

import math
import re

_TOKEN_RE = re.compile(r"[a-z0-9一-鿿]+")

# BM25 参数
_K1 = 1.5
_B = 0.75
# 默认评分阈值
_THRESHOLD = 1.0
# 当没有任何段落超过阈值时，回退保留的段落数
_FALLBACK_TOP = 5


def _tokens(text: str) -> list[str]:
    """分词：提取英文/数字/中文词，过滤长度 < 2 的词。"""
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if len(t) >= 2]


def _is_heading(block: str) -> bool:
    """判断块是否为 Markdown 标题。"""
    for line in block.splitlines():
        if line.strip():
            return line.lstrip().startswith("#")
    return False


def _split_blocks(text: str) -> list[str]:
    """按空行切分文本为段落块（标题、段落、表格、列表）。"""
    blocks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip() == "":
            if current:
                blocks.append("\n".join(current))
                current = []
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def focus_extract(content: str, query: str, top_k: int = 5) -> str:
    """BM25 聚焦提取：从长文本提取与查询最相关的段落。纯 Python，无依赖。"""
    # 空查询或短文本直接返回原文
    if not query or not content or not content.strip():
        return content
    if len(content) < 2000:
        return content
    blocks = _split_blocks(content)
    if len(blocks) <= 1:
        return content
    qterms = set(_tokens(query))
    if not qterms:
        return content

    block_tokens = [_tokens(bl) for bl in blocks]
    n = len(blocks)
    avgdl = (sum(len(t) for t in block_tokens) / n) if n else 0.0 or 1.0
    if avgdl == 0:
        avgdl = 1.0

    # 文档频率（每个词出现在多少个段落中）
    df: dict[str, int] = {}
    for toks in block_tokens:
        for t in set(toks):
            df[t] = df.get(t, 0) + 1

    def idf(term: str) -> float:
        d = df.get(term, 0)
        # +1 保证 IDF 为正（BM25+ 风格）
        return math.log((n - d + 0.5) / (d + 0.5) + 1)

    def score(i: int) -> float:
        toks = block_tokens[i]
        if not toks:
            return 0.0
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        dl = len(toks)
        s = 0.0
        denom_len = _K1 * (1 - _B + _B * dl / avgdl)
        for term in qterms:
            f = tf.get(term)
            if f:
                s += idf(term) * (f * (_K1 + 1)) / (f + denom_len)
        return s

    scores = [score(i) for i in range(n)]
    keep = [i for i in range(n) if scores[i] >= _THRESHOLD]

    # 如果没有段落超过阈值，回靠取得分最高的 top_k 个
    if not keep:
        keep = sorted(range(n), key=lambda i: scores[i], reverse=True)[:top_k]

    keep_set = set(keep)
    # 保留被保留段落前的标题（提供上下文）
    for i in keep:
        if i > 0 and _is_heading(blocks[i - 1]) and not _is_heading(blocks[i]):
            keep_set.add(i - 1)

    # 按原始顺序输出
    kept = "\n\n".join(blocks[i] for i in range(n) if i in keep_set)
    header = (
        f"[Focus: {query!r}; showing {len(keep_set)} of {n} blocks "
        f"by BM25 relevance. Pass focus='' for the full page.]"
    )
    return header + "\n\n" + kept


def apply_focus(result: dict, query: str, top_k: int = 5) -> dict:
    """把 BM25 聚焦提取应用到抓取结果——CLI 与 MCP 共用的单一入口。

    背景：`--focus` / `focus` 这个能力同时暴露在 CLI（`argo fetch URL --focus`）
    与 MCP（`argo_fetch` 的 `focus` 参数）两处。两处各写一套裁剪逻辑时，
    文档描述的语义与实际行为容易分叉（历史 bug：CLI 侧压根没有该参数）。
    统一到这里，任何一侧新增调用方都自动继承同一语义。

    契约：
      - query 为空串或正文为空 → 原样返回，不写任何字段
      - 正文过短（focus_extract 内部按 2000 字符阈值判定）→ 不裁剪，
        focus_applied=False，调用方能看出「省 token 没生效」而不是静默当成功
      - 发生裁剪 → content/length 更新，focus_applied=True
    """
    if not query or not (result.get("content") or "").strip():
        return result
    original = result["content"]
    focused = focus_extract(original, query, top_k=top_k)
    result["focus_query"] = query
    if focused == original:
        result["focus_applied"] = False
        return result
    result["content"] = focused
    result["length"] = len(focused)
    result["focus_applied"] = True
    return result


if __name__ == "__main__":
    # 简单测试：构造超过 2000 字符的文本
    block = """
## 异步编程

Python asyncio 提供 async/await 语法，事件循环调度协程。
适合 IO 密集型任务，比多线程更轻量。
""".strip()
    long_text = ("# 编程指南\n\n" + block * 30)  # >2000 字符

    # 正常过滤
    result = focus_extract(long_text, "异步编程 事件循环", top_k=2)
    print(result[:600])

    # 空查询
    assert focus_extract(long_text, "") == long_text

    # 短文本直接返回
    assert focus_extract("短文本", "查询") == "短文本"

    # apply_focus：发生裁剪 → 记账为已应用
    res = {"content": long_text, "length": len(long_text)}
    out = apply_focus(res, "异步编程 事件循环")
    assert out["focus_applied"] is True
    assert out["length"] == len(out["content"])

    # apply_focus：短文本不裁剪 → 诚实记账为未应用（而非假装成功）
    short = {"content": "短文本", "length": 3}
    short_out = apply_focus(short, "查询")
    assert short_out["focus_applied"] is False
    assert short_out["content"] == "短文本" and short_out["length"] == 3

    print("\n测试通过。")
