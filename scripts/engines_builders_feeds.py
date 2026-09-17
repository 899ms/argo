#!/usr/bin/env python3
"""静态 RSS/Atom 种子源构建器：实时流按「热榜」用，不混进通用搜索。

覆盖：
  guardian_rss   卫报（世界版，摘要密度最高的一手 RSS）
  france24       法国 24（英/法双语国际新闻）
  dw_news        德国之声（英语版，条目数最多）
  factcheck_org  FactCheck.org（美国事实核查）
  full_fact      Full Fact（英国事实核查）

## 两种形态，按数据性质分（spec 的 feed_mode 决定）

**hot（热榜语义）** —— 新闻 RSS 走这条。种子是固定的最近 N 条，本质是
实时流而不是检索索引，所以它**忽略 query、返回最新条目**，归入 hot_trending
族。这是仓内既有分层的要求：engine_families._REFILL_EXCLUDED_FAMILIES 把
hot_trending 排除在通用 combo 回填之外，理由是「产出非『查询相关』内容，
回填只会引入噪声」。实时流应当由「热搜/今日要闻」这类域按需取用，
不参与话题检索的槽位竞争。

**filter（本地过滤）** —— 事实核查源走这条。核查文不是实时流，而是
「某个声明被判定为真伪」的查询型内容，用户问的是具体说法，所以本地关键词
过滤是对的。种子里没有就返回空。

## 匹配口径（filter 模式）

拉丁词走词边界，CJK 走子串——汉字全属 \\w，\\b 对中文无效（「俄乌冲突」
用 \\b 包起来永远不匹配）。纯 CJK 查询打到声明了 langs 且不含 zh 的英文种子
时直接放弃，避免空跑占掉路由槽位（同 GBIF 的 ASCII 守卫）。
"""

from __future__ import annotations

import logging
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

from engines_base import http_open, rank_score, safe_search

logger = logging.getLogger("unified_search.engines")

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15")

# 种子正文里的 HTML 实体与标签清理。先剥 CDATA 标记再剥标签——顺序反了会把
# `<![CDATA[...]]>` 整体当成一个标签吃掉（实测把 BBC 的摘要从 106 字清成 0）。
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_LATIN_RE = re.compile(r"[A-Za-z]")

# 种子进程内短缓存：同一 URL 被多个域引用时不重复下载（RSS 单份 50–110KB）
_FEED_TTL_S = 300.0
_feed_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _clean(text: str) -> str:
    """种子字段 → 纯文本（CDATA 安全）。"""
    if not text:
        return ""
    out = _CDATA_RE.sub(r"\1", text)
    out = _TAG_RE.sub(" ", out)
    return _WS_RE.sub(" ", out).strip()


def _local_name(tag: str) -> str:
    """去命名空间取标签本地名（RSS 2.0 无命名空间，Atom 有）。"""
    return tag.rsplit("}", 1)[-1].lower()


def _namespace(tag: str) -> str:
    """取元素命名空间 URI（无命名空间返回空串）。"""
    if tag.startswith("{"):
        return tag[1:].split("}", 1)[0]
    return ""


# 要忽略的命名空间。Media RSS 的 <media:content> 本地名同样是 content，且文本
# 常为纯空白——实测它会顶掉卫报真正的 <description>（1028 字的正文导语），
# 表现是摘要恒为空。作者/日期这类元数据命名空间同理不参与正文提取。
_SKIP_NS = frozenset({
    "http://search.yahoo.com/mrss/",
    "http://purl.org/dc/elements/1.1/",
    "http://purl.org/rss/1.0/modules/syndication/",
})


def _parse_feed(xml_text: str) -> list[dict[str, Any]]:
    """RSS 2.0 <item> 与 Atom <entry> 统一解析成 (title, url, text, published)。

    不用 ET 的 findall 固定路径：两种格式、有无命名空间、字段命名差异
    （pubDate/published/updated、link 文本节点/link@href）都要兼容。
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    items: list[dict[str, Any]] = []
    for node in root.iter():
        if _local_name(node.tag) not in ("item", "entry"):
            continue
        fields: dict[str, str] = {}
        links: list[tuple[str, str]] = []
        for child in node:
            if _namespace(child.tag) in _SKIP_NS:
                continue
            name = _local_name(child.tag)
            if name == "link":
                href = (child.get("href") or "").strip()
                if href:
                    links.append((child.get("rel") or "", href))
                elif (child.text or "").strip():
                    links.append(("", child.text.strip()))
            elif name in ("title", "description", "summary", "content",
                          "encoded", "pubdate", "published", "updated"):
                # 空白文本不算命中：空元素与缩进换行都会走到这里
                if child.text and child.text.strip() and name not in fields:
                    fields[name] = child.text
        # Atom：优先 rel=alternate，避开 rel=self 的种子自指链接
        url = ""
        for rel, href in links:
            if rel in ("alternate", ""):
                url = href
                break
        if not url and links:
            url = links[0][1]
        title = _clean(fields.get("title", ""))
        if not title:
            continue
        body = _clean(fields.get("encoded") or fields.get("content")
                      or fields.get("description") or fields.get("summary") or "")
        items.append({
            "title": title[:200],
            "url": url,
            "text": body,
            "published_at": _clean(fields.get("pubdate") or fields.get("published")
                                   or fields.get("updated") or ""),
        })
    return items


def _term_hit(term: str, low_text: str) -> bool:
    """单词命中判定：CJK 子串 / 拉丁词边界。"""
    if _CJK_RE.search(term):
        return term in low_text
    return re.search(rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])",
                     low_text) is not None


def _is_pure_cjk(query: str) -> bool:
    return bool(_CJK_RE.search(query)) and not _LATIN_RE.search(query)


# 英文虚词：不承载主题信息，却会命中几乎每一条种子。实测「the fed」若不滤，
# 只含 the 的条目也能拿到 1/2 命中比而通过阈值——虚词把阈值架空了。
#
# 新闻体虚词同理：latest / news / breaking 这类是时效与体裁标记而非主题词，
# 在任何一份新闻种子里的命中率接近 100%（实测「latest news」两词全中三条），
# 留着它们等于给查询发了一张「整份种子都算命中」的通行证。
_STOPWORDS = frozenset({
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or", "but",
    "is", "are", "was", "were", "be", "been", "being", "by", "with", "from",
    "as", "that", "this", "these", "those", "it", "its", "into", "over", "after",
    "about", "than", "then", "so", "if", "not", "no", "up", "out", "how", "why",
    "latest", "news", "update", "updates", "breaking",
})


def _query_terms(query: str) -> list[str]:
    """切检索词并滤掉英文虚词（CJK 无虚词表，原样保留）。"""
    raw = [t for t in re.split(r"[\s\u3000]+", (query or "").strip()) if t]
    return [t for t in raw if t.lower() not in _STOPWORDS]


def _build_rss_feed_engine(spec: dict[str, Any]) -> Any:
    """静态 RSS/Atom 种子引擎（spec 驱动，新增源只需一个 YAML）。

    feed_mode 决定语义，默认 hot——种子是实时流，按实时流处理才是诚实默认；
    需要查询式行为的源（事实核查）显式声明 filter。
    """
    timeout = float(spec.get("timeout", 12))
    feed_url = spec.get("feed_url") or spec.get("url", "")
    name = spec.get("_name", "") or spec.get("engine_id", "") or "rss_feed"
    langs = set(spec.get("langs") or [])
    mode = str(spec.get("feed_mode") or "hot").lower()
    min_ratio = float(spec.get("match_min_ratio", 0.34))
    snippet_max = int(spec.get("snippet_max", 300))
    hot_base = float(spec.get("hot_base_score", 0.55))

    def _row(it: dict[str, Any], rank: int, score: float) -> dict[str, Any]:
        return {
            "title": it["title"],
            "url": it["url"],
            "snippet": it["text"][:snippet_max],
            "source": name,
            "published_at": it["published_at"],
            "score": rank_score(score, rank),
        }

    def _load() -> list[dict[str, Any]]:
        hit = _feed_cache.get(feed_url)
        if hit and (time.monotonic() - hit[0]) < _FEED_TTL_S:
            return hit[1]
        req = urllib.request.Request(feed_url, headers={"User-Agent": _UA, "Accept": "*/*"})
        with http_open(req, timeout=timeout, engine=name) as resp:
            raw = resp.read().decode("utf-8", "replace")
        items = _parse_feed(raw)
        _feed_cache[feed_url] = (time.monotonic(), items)
        return items

    @safe_search
    def _engine(query: str, n: int = 10, _timeout: float | None = None,
                **kwargs) -> list[dict[str, Any]]:
        items = _load()
        if not items:
            return []

        # 热榜语义：种子即最新流，query 不参与过滤——返回的就是「此刻的最近条目」。
        # 不在这里做关键词筛选，是因为「今日要闻」这类请求本身没有主题词，
        # 硬筛只会返回空；话题检索应由查询驱动的引擎承担。
        if mode == "hot":
            return [_row(it, i, hot_base) for i, it in enumerate(items[:max(1, n)])]

        # 纯 CJK 查询打到不含 zh 的英文种子：本地过滤必然全空，直接放弃，
        # 把路由槽位让给中文源（同 GBIF 的 ASCII 守卫）。
        if langs and "zh" not in langs and _is_pure_cjk(query):
            return []
        terms = _query_terms(query)
        if not terms:
            # 全是虚词（"the" / "latest news" 这类无主题词）：本地过滤没有
            # 可依据的信号，返回空而不是把整份种子当结果交出去。
            return []

        scored: list[tuple[float, int, dict[str, Any]]] = []
        for rank, it in enumerate(items):
            low = f"{it['title']} {it['text']}".lower()
            hits = sum(1 for t in terms if _term_hit(t, low))
            if not hits:
                continue
            ratio = hits / len(terms)
            if ratio < min_ratio:
                continue
            # 标题命中额外加权：正文顺带提到的词不如标题直指主题
            title_bonus = 0.15 if any(_term_hit(t, it["title"].lower()) for t in terms) else 0.0
            scored.append((ratio + title_bonus, rank, it))

        scored.sort(key=lambda x: (-x[0], x[1]))
        return [_row(it, rank_i, min(0.6 + score * 0.2, 0.85))
                for score, rank_i, it in scored[:n]]

    return _engine
