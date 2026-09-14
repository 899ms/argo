#!/usr/bin/env python3
"""
search.py — Unified Search v2 CLI 主入口 & 执行编排

职责：
  - 解析命令行参数
  - 通过 route.py 做路由决策（含预算模式）
  - 通过 cache.py 做双层缓存
  - 通过 engines.py 执行引擎搜索
  - RRF 融合 + Bocha Reranker 精排
  - 通过 adaptive.py 记录引擎表现
  - 输出统一 JSON / 文本格式
"""

from __future__ import annotations

import argparse

from engine_env import get_env
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from cache import SearchCache
try:
    from cache import query_similarity as _query_similarity
except ImportError:
    _query_similarity = None  # type: ignore
from route import route_query
from engines import search as engine_search, available_engines
from config import get_execution_config, get_cost_factor, get_engines
try:
    from telemetry import emit as _emit_telemetry
except ImportError:
    _emit_telemetry = None  # type: ignore


# ── 时间辅助（时间窗归一化 / published_at 解析 / 后过滤 / 排序）──────────────
#
# 时间窗三层语义：
#   1. 下推（since/until → 引擎）：入口统一归一化为绝对 ISO，引擎收到确定值
#   2. 后过滤（结果层兜底）：引擎不带时间窗能力时，按 published_at 剔除超窗
#   3. 排序（--sort）：仅展示顺序，不影响召回
#
# 归一化规则：相对量（Nd/Nh/Nw）→ 绝对日期；绝对时间无时区按本地时区解释
# （与 _published_ts 一致）；非法输入保持原样下推、不参与后过滤，不阻断搜索。

# published_at 常见形态：YYYY-MM-DD、YYYY-MM-DD HH:MM[:SS]、ISO(YYYY-MM-DDTHH:MM:SS)
_DATE_RE = re.compile(
    r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?"
)
# 相对时间：Nd / Nh / Nw（不区分大小写）
_REL_TIME_RE = re.compile(r"^(\d+)\s*([dhw])$", re.IGNORECASE)
# 纯日期 YYYY-MM-DD（until 边界含当天；下推保留日期形态）
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")


def _published_ts(r: dict[str, Any]) -> float | None:
    """解析结果的 published_at → epoch 秒；无法解析返回 None（恒排最后）。

    ISO 优先（fromisoformat 支持 T/空格分隔、Z、±HH:MM 时区），
    带时区正确换算 epoch，无时区按本地时区解释；回退 YYYY-MM-DD 手工解析。
    """
    raw = r.get("published_at")
    if not raw:
        return None
    text = str(raw).strip()
    if text.isdigit():  # 部分引擎给 epoch 秒时间戳
        try:
            return float(text)
        except ValueError:
            return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        # aware datetime 正确换算 UTC epoch；naive 按本地时区解释
        return dt.timestamp()
    except ValueError:
        pass
    m = _DATE_RE.match(text)
    if not m:
        return None
    try:
        return datetime(
            int(m.group(1)), int(m.group(2)), int(m.group(3)),
            int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0),
        ).timestamp()
    except ValueError:
        return None


def _parse_time_value(value: Any) -> tuple[str | None, float | None]:
    """解析单边时间窗 → (归一化 ISO 字符串, epoch 秒)。

    支持：相对量（Nd/Nh/Nw）、YYYY-MM-DD、YYYY-MM-DD HH:MM[:SS]、
    ISO 8601（含 Z / ±HH:MM）、纯数字 epoch 秒。
    相对量归一化为绝对日期（YYYY-MM-DD），语义确定、可入缓存键；
    无法解析返回 (None, None)，调用方保持原样下推、不参与后过滤。
    """
    if value in (None, ""):
        return None, None
    text = str(value).strip()
    # epoch 秒
    if text.isdigit():
        try:
            ts = float(text)
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            return dt.isoformat(timespec="seconds"), ts
        except (ValueError, OSError):
            return None, None
    # 相对量：Nd / Nh / Nw → 绝对日期（本地时区零点）
    m = _REL_TIME_RE.match(text)
    if m:
        amount = int(m.group(1))
        unit = m.group(2).lower()
        now = datetime.now()
        if unit == "h":
            dt = now - timedelta(hours=amount)
        elif unit == "w":
            dt = now - timedelta(weeks=amount)
        else:
            dt = now - timedelta(days=amount)
        d = dt.date()
        return d.isoformat(), datetime(d.year, d.month, d.day).timestamp()
    # 绝对时间：fromisoformat 优先（T/空格分隔、Z、±HH:MM）
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None, None
    if dt.tzinfo is not None:
        return dt.isoformat(timespec="seconds"), dt.timestamp()
    # 无时区：按本地时区（与 _published_ts 无时区行为一致）；
    # 纯日期（时间为零点）保留 YYYY-MM-DD 形态下推，兼容引擎既有解析
    if (dt.hour, dt.minute, dt.second, dt.microsecond) == (0, 0, 0, 0):
        return dt.date().isoformat(), dt.timestamp()
    return dt.isoformat(timespec="seconds"), dt.timestamp()


def _normalize_time_window(
    since: str | None, until: str | None
) -> tuple[str | None, str | None, float | None, float | None]:
    """归一化时间窗 → (since_iso, until_iso, since_ts, until_ts)。

    下推与缓存键使用归一化 ISO（相对值转绝对日期，消除 7d 与绝对日期的
    缓存碎片）；后过滤使用 epoch 秒。非法输入 iso 保留原始字符串、
    ts 为 None：仍会下推原值、缓存键仍区分，但不参与后过滤，不阻断搜索。
    """
    s_iso, s_ts = _parse_time_value(since)
    u_iso, u_ts = _parse_time_value(until)
    # 纯日期 until 语义为「含当天」：边界取当天最后一刻，
    # 使次日零点及之后的结果被剔除、当天 23:59:59 保留
    if u_iso and _DATE_ONLY_RE.fullmatch(u_iso):
        try:
            d = datetime.fromisoformat(u_iso).date()
            u_ts = datetime(d.year, d.month, d.day, 23, 59, 59, 999999).timestamp()
        except ValueError:
            pass
    s_raw = str(since).strip() if since not in (None, "") else None
    u_raw = str(until).strip() if until not in (None, "") else None
    return (s_iso or s_raw, u_iso or u_raw, s_ts, u_ts)


def _apply_time_window(
    results: list[dict[str, Any]], since_ts: float | None, until_ts: float | None
) -> tuple[list[dict[str, Any]], int]:
    """结果后过滤兜底：剔除「有 published_at 且明确超窗」的条目。

    宽松策略：无时间字段的结果无法判断、予以保留（避免大多数引擎清空）；
    只有时间明确落在窗口外的才剔除。返回 (保留列表, 剔除数) 供 envelope 上报。
    """
    if since_ts is None and until_ts is None:
        return results, 0
    kept: list[dict[str, Any]] = []
    dropped = 0
    for r in results:
        ts = _published_ts(r)
        if ts is not None:
            if since_ts is not None and ts < since_ts:
                dropped += 1
                continue
            if until_ts is not None and ts > until_ts:
                dropped += 1
                continue
        kept.append(r)
    return kept, dropped


# 带发布时间能力（published_at）的引擎集合。时间窗的缓存键隔离与后过滤
# 只对含这些引擎的组合生效：不带时间字段的引擎会忽略时间窗、结果相同，
# 隔离缓存键只会白白降低命中率（7d/30d 查同一引擎本可共享缓存）。
# local_search 聚合内部可能选中 news 类子引擎（带日期），保守纳入。
_TIME_CAPABLE_ENGINES: frozenset[str] = frozenset({
    "realtime_index", "wayback_cdx", "local_search",
    "local_bing_news", "local_google_news", "local_duckduckgo_news",
    "local_ddgs_news",
    # parallel（after_date 下推）/ you（freshness 动态化 + page_age）
    "parallel", "you",
})


def _is_time_capable(eng: str) -> bool:
    """引擎是否可能返回 published_at（决定时间窗是否参与缓存键/后过滤）。"""
    return eng in _TIME_CAPABLE_ENGINES


def _sort_results(results: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    """按时间重排结果集：oldest 升序 / newest 降序 / relevance 原序。

    排序是纯本地展示顺序：不改变结果集、不进入缓存键、不影响缓存内容；
    无日期条目恒排最后；同时间保持原相对顺序（稳定排序，结果可复现）。
    """
    if sort not in ("oldest", "newest"):
        return results
    if len(results) <= 1:
        return results

    def _key(r: dict[str, Any]) -> tuple[int, float]:
        ts = _published_ts(r)
        if ts is None:
            return (1, 0.0)  # 无日期恒排最后
        return (0, ts if sort == "oldest" else -ts)

    return sorted(results, key=_key)


# ── 查询改写辅助 ────────────────────────────────────────────────────────────────

def _apply_query_rewrite(query: str) -> tuple[str, dict | None]:
    """统一查询改写逻辑，返回 (改写后的查询, 改写结果字典)。

    改写失败时静默返回原查询，不影响搜索流程。
    """
    try:
        from query_rewriter import rewrite_query as do_rewrite
        result = do_rewrite(query)
        if result["rewritten"] and result["confidence"] >= 0.7:
            return result["rewritten"], result
    except ImportError:
        pass  # query_rewriter 模块不可用，使用原查询
    except Exception as e:
        import logging
        logging.getLogger("unified_search").debug(f"查询改写跳过: {type(e).__name__}")
    return query, None


def _results_sufficient(
    results: list[dict[str, Any]],
    mode: str = "auto",
    min_results: int | None = None,
    query: str = "",
) -> bool:
    return _sufficient_internal(results, mode, min_results, query)


def _cumulative_sufficient(raw_results: dict[str, list[dict[str, Any]]],
                           mode: str = "auto",
                           min_results: int | None = None,
                           query: str = "") -> bool:
    """跨引擎累计结果是否已够（wave-2 提前终止判定）。

    与 _results_sufficient 同阈值，但把 raw_results 里所有已完成的
    引擎结果合并成一条列表再判，避免单一引擎不足时重复等待慢源。
    """
    merged: list[dict[str, Any]] = []
    for res in raw_results.values():
        if not res:
            continue
        merged.extend(
            r for r in res if isinstance(r, dict) and "error" not in r
        )
    return _sufficient_internal(merged, mode, min_results, query)


def _query_coverage_ok(results: list[dict[str, Any]], query: str) -> bool:
    """查询-结果词面覆盖守卫（fast/auto 早停质量门槛）。

    2026-09-02 实测教训：fast 早停原只看计数+snippet，首引擎上游波动时
    返回 5 条高计数但不相关结果（查询 Crawl4AI 却返回无关 MDN 页），
    早停吞掉 wave-2，单引擎垃圾即成最终答案。本守卫只在极端场景（多数
    结果与查询零词面交集）拒绝早停，让既有 wave-2/串行次引擎补跑——
    只影响「是否停」，不丢弃任何结果，最坏代价是多跑一个引擎。

    CJK 说明：中文查询分词为单字，覆盖判定在字符级——结果与查询零字符
    交集同样会被拒早停（弱信号但非空）；同义词改写场景（如「电脑」vs
    「计算机」）可能多跑一个引擎，属可接受代价：宁可多一次调用，不放走
    单引擎垃圾。
    """
    if not query or not query.strip():
        return True
    try:
        from tfidf_router import tokenize
    except ImportError:
        return True  # 分词不可用不设卡（fail-open）
    q_set = set(tokenize(query))
    if not q_set:
        return True
    # 同构垃圾检测：结构性包索引被域外查询误抢时，会把查询切词后逐词
    # 返回单 token 包名（title≈1 词、URL=/project/<word>），词面覆盖因此
    # 虚高（垃圾包名恰好是查询关键词）→ 覆盖守卫被骗过、垃圾即成最终
    # 答案。多数结果标题 ≤1 token 且查询有 ≥3 token 时，视为索引噪声，
    # 拒绝早停、放行串行次引擎补跑。
    single_token_titles = sum(
        1 for r in results if len(set(tokenize(r.get("title") or ""))) <= 1
    )
    if len(q_set) >= 3 and single_token_titles * 2 > len(results):
        return False
    covered = 0
    for r in results:
        text = f"{r.get('title') or ''} {r.get('snippet') or ''}"
        if q_set & set(tokenize(text)):
            covered += 1
    return covered >= max(1, (len(results) + 1) // 2)


def _sufficient_internal(
    results: list[dict[str, Any]],
    mode: str,
    min_results: int | None,
    query: str = "",
) -> bool:
    """渐进检索 early-stop：结果是否已够用。

    轻量启发式（不依赖网络/LLM）：
      - 默认 auto：至少 3 条非错误 + 2 条有 snippet
      - 默认 fast：至少 2 条 + 1 个 snippet
      - min_results：域配置覆盖（答案型源 1 条快照即够用，计数语义不动）
      - 通用路径叠加词面覆盖守卫（_query_coverage_ok）：结果与查询几乎
        无交集时不许早停
    """
    goods = [r for r in results if isinstance(r, dict) and "error" not in r]
    if not goods:
        return False
    with_snippet = sum(
        1 for r in goods
        if (r.get("snippet") or r.get("title") or "").strip()
    )
    if min_results is not None:
        try:
            need = max(1, int(min_results))
        except (TypeError, ValueError):
            need = 1
        # 答案型 1 条要求有可展示正文；≥3 条时至少 2 条有 snippet
        need_snip = 1 if need <= 2 else max(2, need - 1)
        return len(goods) >= need and with_snippet >= min(need_snip, len(goods))
    if mode == "fast":
        ok = len(goods) >= 2 and with_snippet >= 1
    else:
        ok = len(goods) >= 3 and with_snippet >= 2
    return ok and _query_coverage_ok(goods, query)


def _missing_env_for(eng: str) -> list[str]:
    """返回引擎缺失的环境变量名列表；检测不可用时返回空（不阻断搜索）。

    与路由层 env_ready(spec) 同口径：查当前注册表拿 spec，否则
    声明里自定义 required_env 的引擎在此拦截不到（仅 KNOWN_ENV_ALIASES
    成员能命中）。注册表值是 callable（spec 在闭包里）时退化为原名检测。
    """
    try:
        from engine_env import missing_env_for as _missing
        spec = None
        try:
            from engines import get_engine_spec
            spec = get_engine_spec(eng)
        except Exception:
            spec = None
        return _missing(eng, spec)
    except Exception:
        return []


class _QuotaBatch:
    """一次搜索的配额记账收集器（累积 → 一次性落盘）。

    为什么不是每引擎各写一次：每次 record 都是「全量状态序列化 + rename」，
    一次 5 引擎搜索即 5 次全量写。合并后写盘次数从 N 降到 1，且整批在
    同一个跨进程文件锁内完成（`QuotaManager.record_many`）。

    失败静默：记账属于观测层，任何异常都不得拖累搜索主路径。
    """

    def __init__(self) -> None:
        self._entries: list[tuple[str, bool]] = []

    def add(self, engine: str, success: bool) -> None:
        self._entries.append((engine, success))

    def flush(self) -> None:
        entries, self._entries = self._entries, []
        if not entries:
            return
        try:
            from quota import get_quota_manager
            get_quota_manager().record_many(entries)
        except Exception:
            pass


def _record_quota(engine: str, success: bool) -> None:
    """单条配额记账（真实打网后写）；失败静默。

    批量路径请用 `_QuotaBatch`——它把同一次搜索的 N 条合成一次落盘。
    """
    try:
        from quota import get_quota_manager
        get_quota_manager().record(engine, success=success)
    except Exception:
        pass


# 配额耗尽错误关键词（单一真源）：_classify_engine_outcome 的 quota-exhausted
# 分类与自适应学习跳过逻辑共用。新增配额错误码（如新的 API 业务码）只改这里。
_QUOTA_ERROR_KEYWORDS = ("quota", "10406")

# 拦截页特征词（单一真源）：error 文本里出现即判 blocked。HTML 引擎的反爬
# 命中没有 error 文本（静默空结果），走 engines_base 的归因寄存器；
# 这张表兜住「error 结果里带拦截页字样」的可见路径。
_BLOCKED_ERROR_KEYWORDS = (
    "just a moment", "checking your browser", "cf-browser-verification",
    "challenge", "ddos-guard", "perimeterx", "access denied",
    "handshake failure", "unable to handshake", "安全验证", "滑动验证",
)


def _note_remote_quota_exhausted(engine: str, detail: str) -> None:
    """远端明示配额耗尽（如 byted 10406）→ 标记到周期边界自动恢复。

    标记后路由组合层全模式排除该引擎、备用源自然接管；恢复无需人工干预。
    """
    try:
        from quota import get_quota_manager
        get_quota_manager().mark_remote_exhausted(engine, reason=detail)
    except Exception:
        pass


# ── 进度阶段 ──────────────────────────────────────────────────────────────────

class Stage(str, Enum):
    START = "start"
    CACHE_HIT = "cache_hit"
    ROUTING = "routing"
    SEARCHING = "searching"
    MERGING = "merging"
    DONE = "done"
    ERROR = "error"


# ── RRF 融合 ───────────────────────────────────────────────────────────────────

# URL 归一化的单一真源在 url_canon：本仓曾有四份各自实现的「URL 归一」
# （search / plan / candidate_envelope / research_dossier，追踪参数表与
# 大小写规则各不相同），导致同一链接在融合层与 dossier 层归一成不同键。
# 此处只做薄转发，规则改动一律进 url_canon。
from url_canon import canonical_url as _canonical_url_impl  # noqa: E402


def _canonical_url(url: str) -> str:
    """URL 归一化（薄转发到 url_canon 单一真源）。"""
    return _canonical_url_impl(url)


# 引擎融合权重（WG-RRF：按来源质量加权，权威源提权、社交/低质源降权）
_ENGINE_FUSION_WEIGHTS: dict[str, float] = {
    # 权威百科/学术/官方
    "wikipedia": 1.4, "wikidata": 1.4, "zh_wikipedia": 1.4, "baidu_baike": 1.3,
    "arxiv": 1.3, "openalex": 1.3, "crossref": 1.3, "semantic_scholar": 1.3,
    "dblp": 1.3, "europepmc": 1.3, "pubmed": 1.3, "google_scholar": 1.3,
    "pubchem": 1.3, "uniprot": 1.3, "rcsb_pdb": 1.3,
    "github": 1.2, "pypi": 1.2, "npm": 1.2, "crates": 1.2, "mdn": 1.2,
    "stackoverflow": 1.1, "imdb": 1.2, "thesportsdb": 1.2, "itunes": 1.2,
    "finviz": 1.2, "sina_quote": 1.2, "tencent_quote": 1.2, "eastmoney": 1.2,
    "fred": 1.3, "worldbank": 1.3, "nbs_stats": 1.3, "eurostat": 1.3,
    # 通用引擎（基线）
    "duckduckgo": 1.0, "local_bing": 1.0, "local_duckduckgo": 1.0,
    "local_google": 1.0, "anysearch": 1.05, "byted": 1.1, "bocha": 1.0,
    "bocha_ai": 1.3,  # 垂直结构化模态卡（实时值）
    "brave": 1.0, "uapi": 1.0, "local_search": 1.0, "octen": 1.0,
    "gdelt": 1.0, "opencorporates": 1.2, "google_patents": 1.2,
    # 社交/低质（降权）
    "twitter": 0.7, "reddit": 0.7, "xiaohongshu": 0.7, "bilibili": 0.7,
    "weibo": 0.7, "v2ex": 0.8, "zhihu": 0.8, "hackernews": 0.8, "zhihu_hot": 0.8,
    "baidu_hot": 0.8, "toutiao_hot": 0.8, "bilibili_hot": 0.8,
}


# 动态可靠性因子（weakest-link，论文 arxiv 2508.01405）：熔断/高错误引擎降权，
# 避免「弱检索路径」在融合时拖垮整体精度。带 30s TTL 缓存，避免热路径重复查询。
_rel_factor_cache: dict[str, tuple[float, float]] = {}
_REL_FACTOR_TTL = 30.0


def _single_reliability(engine: str) -> float:
    now = time.time()
    cached = _rel_factor_cache.get(engine)
    if cached and cached[1] > now:
        return cached[0]
    factor = 1.0
    try:
        from circuit_breaker import get_breaker
        st = get_breaker().status(engine)
        state = st.get("state")
        if state == "disabled":
            factor = 0.5
        elif state == "open":
            factor = 0.7
        elif state == "half_open":
            factor = 0.85
        failures = int(st.get("failures") or 0)
        if failures >= 5:
            factor = min(factor, 0.8)
    except Exception:
        factor = 1.0
    _rel_factor_cache[engine] = (factor, now + _REL_FACTOR_TTL)
    return factor


def _engine_weight(source: str, lang: str | None = None) -> float:
    """按引擎来源返回融合权重（source 可能含 'local_bing/sina_quote' 合并形式）。

    静态基础权重（权威/学术提权、社交降权）× 动态可靠性因子（weakest-link）：
    熔断/高错误源降权，健康权威源维持提权。论文 2508.01405 的路径质量评估落地。

    lang（可选）启用**语言能力加权**：由 18语言×29引擎 矩阵实测得到的
    能力画像（data/lang_matrix/lang_capability.json）决定——该语言下实测
    良好的引擎提权、实测噪声的降权、无数据的保持中性。画像缺失/过期时
    完全退化为原行为（见 lang_capability 的安全降级契约）。
    """
    if not source:
        return 1.0
    # 合并来源：静态权重取最高源，可靠性取最低源（weakest-link：任一路径弱即降权）
    parts = [p.strip() for p in str(source).split("/") if p.strip()]
    if not parts:
        return 1.0
    static = max([_ENGINE_FUSION_WEIGHTS.get(p, 1.0) for p in parts])
    rel = min([_single_reliability(p) for p in parts])
    out = static * rel
    if lang:
        try:
            from lang_capability import score_adjust
            # 多来源取最高：某个来源在该语言下有能力即可（不因合并源里
            # 混入一个未知引擎而失去提权）
            adj = max([score_adjust(p, lang) for p in parts] or [1.0])
            out *= adj
        except Exception:
            pass
    return round(out, 3)


def rrf_merge(ranked_lists: list[list[dict[str, Any]]], k: int = 60,
              weighted: bool = True,
              lang: str | None = None) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion 合并多引擎结果，保留 consensus_engines。

    键用归一化 URL（http/https、www、utm 变体合并）；RRF 分单独存 _rrf_score，
    首次遇到的结果保留完整字段，后续同 URL 只累加共识、择优补充 snippet，
    避免「score 字段赢家通吃」覆盖共识内容。

    weighted（WG-RRF）：按引擎来源加权（权威源提权、社交源降权），
    默认开启；传 False 回到经典 RRF 行为。
    """
    scores: dict[str, float] = {}
    items: dict[str, dict[str, Any]] = {}

    for _li, results in enumerate(ranked_lists):
        for i, r in enumerate(results):
            # 无 URL 时用 title 兜底；模态卡再退到 card_type（避免空 title 互撞）。
            # 最后兜底必须带**列表身份**：此前用裸 `i`（单列表内的局部索引），
            # 跨引擎必然同值 —— 两条都没有 url/title/card_type 的不同结果会在
            # `__idx__:0` 处相撞，表现为 ①丢结果 ②伪造 consensus_engines
            # （两个引擎"都投了"同一条，其实各是各的）③字段错配（_engine 留 A、
            # snippet 被 B 覆盖）。同文件 deduplicate_by_url 用全局递增计数器
            # `anon:{len(out)}` 就没有这个问题，此处对齐该写法。
            key = (
                _canonical_url(r.get("url", ""))
                or (f"__title__:{r.get('title', '')}" if r.get("title") else "")
                or (f"__card__:{r.get('card_type', '')}" if r.get("card_type") else "")
                or f"__idx__:{_li}:{i}"
            )
            w = _engine_weight(r.get("_engine") or r.get("source") or "",
                               lang=lang) if weighted else 1.0
            scores[key] = scores.get(key, 0.0) + w / (k + i + 1)
            eng = r.get("_engine") or r.get("source", "") or ""
            if key not in items:
                item = dict(r)
                item["_rrf_score"] = 0.0  # 排序后统一写回
                cons: list[str] = []
                if eng:
                    cons.append(eng)
                item["consensus_engines"] = cons
                items[key] = item
            else:
                cur = items[key]
                # 择优保留内容更完整的版本（title+snippet 更长者胜），不覆盖其余字段
                new_txt = f"{r.get('title', '')} {r.get('snippet', '')}"
                cur_txt = f"{cur.get('title', '')} {cur.get('snippet', '')}"
                if len(new_txt) > len(cur_txt):
                    cur["title"] = r.get("title", cur.get("title"))
                    cur["snippet"] = r.get("snippet", cur.get("snippet"))
                sources = {cur.get("source", ""), r.get("source", "")}
                cur["source"] = "/".join(s for s in sources if s)
                cons = list(cur.get("consensus_engines") or [])
                if eng and eng not in cons:
                    cons.append(eng)
                cur["consensus_engines"] = cons

    ranked = sorted(scores.items(), key=lambda x: -x[1])
    out = []
    for key, _ in ranked:
        item = items[key]
        item["_rrf_score"] = round(scores[key], 6)
        out.append(item)
    return out


def _content_similarity(a: str, b: str) -> float:
    """标题+片段的 minhash 相似度（复用 cache.query_similarity，失败回退 Jaccard）。"""
    if not a or not b:
        return 0.0
    if _query_similarity is not None:
        try:
            return float(_query_similarity(a, b))
        except Exception:
            pass
    import re as _re
    sa, sb = set(_re.findall(r"[\u4e00-\u9fff]|[a-zA-Z0-9]+", a.lower())), set(_re.findall(r"[\u4e00-\u9fff]|[a-zA-Z0-9]+", b.lower()))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb) if sa | sb else 0.0


def _content_sig(r: dict[str, Any]) -> str:
    return f"{r.get('title', '') or ''} {r.get('snippet', '') or ''}".strip()


def minhash_dedupe(
    results: list[dict[str, Any]], threshold: float = 0.85, enabled: bool | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """结果级近重复去重（MMR 前置）：同一事件多引擎同质网页堆叠时去重。

    流程：URL 归一键已去重 → 剩余按(score, selection)降序贪心，content_similarity ≥ threshold 视为近重复，仅留首条。
    开关：ARGO_MINHASH_DEDUPE=0 时关闭；默认开启（阈值可由 ARGO_MINHASH_THRESHOLD 覆盖，默认 0.85）。
    返回 (deduped, removed_count)，每条被移除的结果记 `_near_dup=True`。
    """
    if enabled is None:
        enabled = os.environ.get("ARGO_MINHASH_DEDUPE", "1").strip() not in ("0", "false", "False", "no")
    if not enabled or not results or len(results) <= 1:
        return results, 0
    try:
        thr = float(os.environ.get("ARGO_MINHASH_THRESHOLD", str(threshold)))
        threshold = max(0.5, min(0.98, thr))
    except Exception:
        pass
    pool = sorted(
        results,
        key=lambda r: (float(r.get("score", 0) or 0), float(r.get("selection", 0) or 0)),
        reverse=True,
    )
    kept: list[dict[str, Any]] = []
    kept_sigs: list[str] = []
    removed = 0
    for r in pool:
        sig = _content_sig(r)
        is_dup = False
        for ks in kept_sigs:
            if _content_similarity(sig, ks) >= threshold:
                is_dup = True
                break
        if not is_dup:
            kept.append(r)
            kept_sigs.append(sig)
        else:
            removed += 1
            r["_near_dup"] = True
    return kept, removed


def deduplicate_by_url(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """URL 去重（归一化键）。"""
    seen: set[str] = set()
    out = []
    for r in results:
        key = (
            _canonical_url(r.get("url", ""))
            or (f"title:{r.get('title', '')}" if r.get("title") else "")
            or (f"card:{r.get('card_type', '')}" if r.get("card_type") else "")
            or f"anon:{len(out)}"
        )
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


# ── 多语言结果语言偏好软排序（P2-覆盖，2026-08 新增）────────────────────────
# ja/ko 明确主语言查询：把含目标语言字符（假名/谚文）的结果前移，纯相反语言
# 结果后移。**软排序不删除**（避免误删混合/技术结果），其余语言零开销返回。
def _lang_prefer_rerank(results: list[dict[str, Any]],
                        primary_lang: str | None) -> list[dict[str, Any]]:
    if not results or primary_lang not in ("ja", "ko"):
        return results
    if primary_lang == "ja":
        _pat = re.compile(r"[\u3040-\u30ff]")
    else:
        _pat = re.compile(r"[\uac00-\ud7af]")

    def _key(r: dict[str, Any]) -> int:
        hay = f"{r.get('title', '')} {r.get('snippet', '')}"
        return 0 if _pat.search(hay) else 1

    # stable sort：含目标语言字符在前，其余保持原 RRF 顺序
    return sorted(results, key=_key)


# ── Bocha Reranker ──────────────────────────────────────────────────────────────

def rerank_results(query: str, results: list[dict[str, Any]],
                   top_n: int = 10, timeout: float = 5
                   ) -> tuple[list[dict[str, Any]], str]:
    """使用博查语义排序模型对搜索结果二次精排。

    返回 (results, status)：status ∈ ok | skipped_no_key | skipped_short |
    skipped_fast | fallback
    """
    if not results or len(results) <= 1:
        return results, "skipped_short"

    api_key = get_env(["ARGO_BOCHA_API_KEY", "BOCHA_API_KEY"])
    if not api_key:
        return results, "skipped_no_key"

    documents = []
    for r in results:
        doc_text = f"{r.get('title', '')} {r.get('snippet', '')}".strip()
        documents.append(doc_text or "empty")

    import urllib.request
    payload = json.dumps({
        "model": "gte-rerank", "query": query,
        "documents": documents[:50],
        "top_n": min(top_n, len(documents)),
        "return_documents": False,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.bocha.cn/v1/rerank", data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            rerank_results_list = data.get("data", {}).get("results", [])
            if not rerank_results_list:
                return results, "fallback"
            scored = []
            for rr in rerank_results_list:
                idx = rr.get("index", -1)
                score = rr.get("relevance_score", 0)
                if 0 <= idx < len(results):
                    item = dict(results[idx])
                    orig_score = item.get("score", 0) or 0
                    item["score"] = round(score * 0.7 + orig_score * 0.3, 4)
                    scored.append(item)
            if scored:
                scored.sort(key=lambda x: x.get("score", 0), reverse=True)
                return scored[:top_n], "ok"
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError):
        return results, "fallback"
    return results, "fallback"


# ── P0-003：本地五维 Rerank 兜底 ──────────────────────────────────────────────

_CJK_OR_WORD = None  # 延迟编译


def _tokens(text: str) -> list[str]:
    """轻量分词：中文单字 + 英文单词，统一小写（复用 tfidf 风格）。"""
    global _CJK_OR_WORD
    if _CJK_OR_WORD is None:
        import re as _re
        _CJK_OR_WORD = _re.compile(r"[\u4e00-\u9fff]|[a-zA-Z0-9]+")
    return [t for t in _CJK_OR_WORD.findall((text or "").lower())]


def _bigrams(tokens: list[str]) -> set[str]:
    return {f"{tokens[i]}_{tokens[i+1]}" for i in range(len(tokens) - 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _score_relevance(query_tokens: set[str], title: str, snippet: str) -> float:
    """相关性：查询 token 在 title+snippet 的覆盖率（title 权重更高）。"""
    if not query_tokens:
        return 0.5
    t_tokens = set(_tokens(title))
    s_tokens = set(_tokens(snippet))
    title_cov = len(query_tokens & t_tokens) / len(query_tokens)
    snip_cov = len(query_tokens & s_tokens) / len(query_tokens)
    return round(min(1.0, 0.65 * title_cov + 0.35 * snip_cov), 4)


def _score_completeness(title: str, snippet: str) -> float:
    """完整性：snippet 长度 + 是否含数字/结构信号，归一到 0-1。"""
    length = len(snippet or "")
    length_score = min(length / 200.0, 1.0)
    has_digit = 1.0 if any(c.isdigit() for c in (snippet or "")) else 0.0
    has_title = 1.0 if (title or "").strip() else 0.0
    return round(min(1.0, 0.6 * length_score + 0.2 * has_digit + 0.2 * has_title), 4)


def _consensus_prior(results: list[dict[str, Any]]) -> list[float]:
    """融合先验：把 RRF 分与跨引擎共识数归一化成 [0,1]（逐条对齐 results）。

    为什么需要它（这是本函数存在的唯一理由）：
    `local_five_dim_rerank` 用 relevance(token 覆盖率)/completeness(文本长度)
    等**文本自身**的维度重新打分，这在结构上偏爱「啰嗦的长网页」而压制
    「多个引擎都认同但摘要简短」的结果。实测：一条 3 引擎共识条目
    (RRF 0.0418) 会被单源长文本条目 (RRF 0.0164) 反超——融合层的核心产出
    在最终排序中丢失。

    这里**不改五维公式**，只把融合信号作为一个独立先验维度加进来，避免与
    `score` 字段竞争（`score` 已被本函数覆写，拿它当输入是循环依赖）。

    归一化口径：RRF 分取最大值归一到 1（保序，不放大）；共识数按
    `min(n-1, 3)/3` 计（封顶 3，避免 5 源共识把量纲压过其他维度）。
    共识在排序路径**只有这一个入口**：旧版此处之外还有一次乘法共识
    boost（×(1+0.05·min(n-1,3))，2026-09-13 移除）——同一信号被重复
    计分，3 引擎共识合计被放大约 19%。evidence selection 阶段的
    `selection` 乘法是「先核验哪条」的独立信号，不影响本排序。
    """
    priors: list[float] = []
    raw = [float(r.get("_rrf_score", 0.0) or 0.0) for r in results]
    peak = max(raw) if raw else 0.0
    for r, rrf in zip(results, raw):
        rrf_norm = (rrf / peak) if peak > 0 else 0.0
        cons = len(r.get("consensus_engines") or [])
        cons_norm = min(max(cons - 1, 0), 3) / 3.0
        # 两个子信号取均值：单纯多引擎重复 != 更可信，RRF 分还含引擎权重
        priors.append(0.5 * rrf_norm + 0.5 * cons_norm)
    return priors


_SCORE_FLOORS_CACHE: dict[str, dict[str, dict[str, float]]] | None = None


def _domain_score_floors() -> dict[str, dict[str, dict[str, float]]]:
    """域级源保底分（config.yaml 各域的 score_floors），进程内缓存。

    这些分值是「域对源的先验信任」，属于引擎/域声明而非排序算法——
    此前硬编码在 local_five_dim_rerank 里，每接一个新源都可能要改排序
    代码（2026-09-13 审查 P1-2）。声明形态：

      score_floors:
        sina_quote: {relevance: 1.0, authority: 0.85, freshness: 0.85}

    生效时机：relevance 在相关性评分后立即生效；authority/freshness 仅在
    evidence 评分可用时生效（无 evidence 时两维本就恒 0.5，保底无意义，
    与旧实现逐位一致）。源匹配按「/」切分成员判断（rrf 合并源
    "local_bing/sina_quote" 也要吃到保底）。
    """
    global _SCORE_FLOORS_CACHE
    if _SCORE_FLOORS_CACHE is None:
        try:
            from config import load_config
            floors: dict[str, dict[str, dict[str, float]]] = {}
            for d in (load_config().get("domains") or []):
                if isinstance(d, dict) and d.get("score_floors"):
                    floors[d["name"]] = {
                        str(src): dict(fl)
                        for src, fl in d["score_floors"].items()
                        if isinstance(fl, dict)
                    }
            _SCORE_FLOORS_CACHE = floors
        except Exception:
            _SCORE_FLOORS_CACHE = {}
    return _SCORE_FLOORS_CACHE


def local_five_dim_rerank(query: str, results: list[dict[str, Any]],
                          domain: str = "general", top_n: int = 10
                          ) -> list[dict[str, Any]]:
    """本地五维精排（无 Bocha Key / fallback 时兜底）。

    维度权重（通用，前五维和为 0.88，余下 0.12 给融合先验）：
      相关性 0.26 + 权威性 0.26 + 时效性 0.18 + 完整性 0.13 + 新颖性 0.05
      + **融合先验 0.12**（RRF 分 + 跨引擎共识，见 `_consensus_prior`）
    tech/code 域：权威 0.18、相关 0.35（技术查询更看内容匹配）。

    无融合信息时（单引擎路径）先验恒为 0，与旧行为等价：此时五维按 0.88
    整体折算，是原权重比例的等比缩放，排序结果不变。

    新颖性：标题 bigram 与「已排更高结果」的 Jaccard 互补（1 − overlap），
    奖励信息增量，抑制近重复堆叠。

    每个结果写入 rerank_dims 明细（含 prior），供可观测。
    """
    if not results:
        return results

    def _src_has(source: str, name: str) -> bool:
        # rrf_merge 会把同 URL 结果的 source 合并成 "local_bing/sina_quote"，
        # 精确匹配会漏掉合并后的结果，这里按「/」切分做成员判断。
        return name in str(source).split("/")

    # 权重表（MECE）。前五维整体缩放 (1 - W_PRIOR)，把余量留给融合先验。
    # 关键性质：prior 缺席时（单引擎路径）五维被同一常数缩放，是原权重比例的
    # 等比变换 —— 排序结果与改造前逐位一致。该等价性由测试锁定。
    W_PRIOR = 0.12
    _BASE = 1.0 - W_PRIOR
    is_tech = domain in ("tech_deep", "code_search", "local_code", "academic")
    if is_tech:
        # 原 tech 权重：rel .40 / auth .20 / fresh .20 / comp .15 / nov .05
        w = {"relevance": 0.40 * _BASE, "authority": 0.20 * _BASE,
             "freshness": 0.20 * _BASE, "completeness": 0.15 * _BASE,
             "novelty": 0.05 * _BASE}
    else:
        # 原通用权重：rel .30 / auth .30 / fresh .20 / comp .15 / nov .05
        w = {"relevance": 0.30 * _BASE, "authority": 0.30 * _BASE,
             "freshness": 0.20 * _BASE, "completeness": 0.15 * _BASE,
             "novelty": 0.05 * _BASE}
    _priors = _consensus_prior(results)

    # 复用 evidence 的权威/时效评分（若可用）
    try:
        from evidence import score_authority, score_freshness
        _has_evidence = True
    except ImportError:
        _has_evidence = False

    query_tokens = set(_tokens(query))
    floors = _domain_score_floors().get(domain, {})

    # 先计算前四维静态分
    enriched = []
    for _i, r in enumerate(results):
        title = r.get("title", "") or ""
        snippet = r.get("snippet", "") or ""
        url = r.get("url", "") or ""
        source = r.get("source", "") or ""
        relevance = _score_relevance(query_tokens, title, snippet)
        # 域级源保底分（答案型源：书目/行情/汇率/官方公告），声明在
        # config.yaml 各域 score_floors——排序代码对源类型无知
        for _src, fl in floors.items():
            if "relevance" in fl and _src_has(source, _src):
                relevance = max(relevance, fl["relevance"])
        if _has_evidence:
            try:
                authority = float(score_authority(url, source).get("score", 0.5))
            except Exception:
                authority = 0.5
            try:
                freshness = float(score_freshness(r).get("score", 0.5))
            except Exception:
                freshness = 0.5
            # 权威/时效保底（行情快照/官方公告源在 evidence 域名表里
            # 偏低，实为高可信答案源），声明同上
            for _src, fl in floors.items():
                if not _src_has(source, _src):
                    continue
                if "authority" in fl:
                    authority = max(authority, fl["authority"])
                if "freshness" in fl:
                    freshness = max(freshness, fl["freshness"])
        else:
            authority, freshness = 0.5, 0.5
        completeness = _score_completeness(title, snippet)
        enriched.append({
            "r": r, "title": title,
            "relevance": relevance, "authority": authority,
            "freshness": freshness, "completeness": completeness,
            # _priors 与 results 等长且同步推进（循环内无 continue），
            # 原先的 `if len(enriched) < len(_priors)` 是恒真守卫，
            # 会让「下标错位」这种真缺陷静默退化为 prior=0。
            "prior": _priors[_i],
        })

    # 贪心排序：每步选边际得分最高者，novelty 相对已选集合动态计算
    ranked: list[dict[str, Any]] = []
    selected_bigrams: set[str] = set()
    pool = enriched[:]
    while pool:
        best_idx, best_score, best_novelty = 0, -1.0, 1.0
        for i, e in enumerate(pool):
            bg = _bigrams(_tokens(e["title"]))
            novelty = 1.0 - _jaccard(bg, selected_bigrams)
            score = (w["relevance"] * e["relevance"]
                     + w["authority"] * e["authority"]
                     + w["freshness"] * e["freshness"]
                     + w["completeness"] * e["completeness"]
                     + w["novelty"] * novelty
                     + W_PRIOR * e["prior"])
            if score > best_score:
                best_idx, best_score, best_novelty = i, score, novelty
        chosen = pool.pop(best_idx)
        r = chosen["r"]
        r["score"] = round(best_score, 4)
        r["rerank_dims"] = {
            "relevance": chosen["relevance"],
            "authority": round(chosen["authority"], 4),
            "freshness": round(chosen["freshness"], 4),
            "completeness": chosen["completeness"],
            "novelty": round(best_novelty, 4),
            "prior": round(chosen["prior"], 4),
        }
        selected_bigrams |= _bigrams(_tokens(chosen["title"]))
        ranked.append(r)

    return ranked[:top_n]


# ── 融合后段（execute_search 的可独立测试单元）────────────────────────────────

def _apply_consensus_and_sort(merged: list[dict[str, Any]],
                              max_results: int) -> list[dict[str, Any]]:
    """融合层最终排序：按五维 rerank 的 score 降序并截断。

    排序只认 rerank 写入的 score（含 ① 融合先验维度）。此处曾有第二道乘法
    共识 boost（×(1+0.05·min(n-1,3))），与 ① 对同一信号重复计分：3 引擎
    共识合计被放大 ~19%（1.08×1.10），2026-09-13 移除（金标 18 条对拍
    无序位回归）。共识的排序影响由 ① 表达，可观测面由 `consensus_engines`
    与 evidence selection 的 `selection` 字段表达。
    """
    merged.sort(key=lambda r: abs(r.get("score", 0) or 0), reverse=True)
    return merged[:max_results]


def _attach_selection_signals(merged: list[dict[str, Any]], mode: str,
                              depth: str) -> None:
    """两阶段 selection 信号（authority/freshness/selection/absorption/…）。

    这是 evidence「先核验哪条」的依据，独立于排序 score——共识在此阶段
    合法地参与（提高待核验优先级），不属于排序重复计分。
    fast 模式跳过（MCP 默认紧凑也不返回这些字段）。失败静默：观测层
    不得拖累搜索主路径。
    """
    if not merged or mode == "fast" or depth == "fast":
        return
    try:
        from evidence import score_authority, score_freshness
        from content_signals import score_evidence_density
        for r in merged:
            url = r.get("url", "")
            source = r.get("source", "")
            title = r.get("title", "") or ""
            snippet = r.get("snippet", "") or ""
            auth = score_authority(url, source)
            fresh = score_freshness(r)
            dens = score_evidence_density(snippet, title)
            selection = auth["score"]
            if auth.get("is_serp"):
                selection = min(selection, 0.15)
            cons = r.get("consensus_engines") or []
            if len(cons) >= 2 and not auth.get("is_serp"):
                selection = min(1.0, selection * (1.0 + 0.1 * min(len(cons) - 1, 2)))
            absorption = dens["absorption_score"]
            orig = float(r.get("score", 0.5) or 0.5)
            r["authority"] = auth["score"]
            r["authority_tier"] = auth["tier"]
            r["freshness"] = fresh["score"]
            r["selection"] = round(selection, 3)
            r["absorption"] = round(absorption, 3)
            r["evidence_flags"] = {
                "has_numbers": dens["has_numbers"],
                "has_comparison": dens["has_comparison"],
                "has_definition": dens["has_definition"],
                "is_serp": bool(auth.get("is_serp")),
                "consensus": len(cons),
            }
            r["credibility_fast"] = round(
                selection * 0.40 + absorption * 0.35 + fresh["score"] * 0.15 + orig * 0.10,
                3,
            )
    except ImportError:
        pass
    except Exception as e:
        import logging
        logging.getLogger("unified_search").debug(f"可信度评分跳过: {type(e).__name__}")


def _align_facts_safe(merged: list[dict[str, Any]], mode: str,
                      depth: str) -> dict[str, Any] | None:
    """关键事实交叉标记（P0-004）。仅 deep/auto 且结果 ≥3；fast 跳过。"""
    if not merged:
        return None
    try:
        from fact_align import align_facts
        return align_facts(merged, min_results=3, mode=mode, depth=depth)
    except ImportError:
        return None  # fact_align 模块不可用
    except Exception as e:
        import logging
        logging.getLogger("unified_search").debug(
            f"事实交叉标记跳过: {type(e).__name__}")
        return None


# ── 执行层 ─────────────────────────────────────────────────────────────────────

def _classify_engine_outcome(eng: str, res: list[dict[str, Any]],
                             latency_ms: int, status_hint: str | None = None
                             ) -> dict[str, Any]:
    """将单引擎结果归类为可观测 outcome。"""
    if status_hint:
        return {
            "engine": eng, "status": status_hint,
            "results_count": 0, "latency_ms": latency_ms,
        }
    if not res:
        return {
            "engine": eng, "status": "no-results",
            "results_count": 0, "latency_ms": latency_ms,
        }
    errors = [r for r in res if isinstance(r, dict) and "error" in r]
    goods = [r for r in res if isinstance(r, dict) and "error" not in r]
    if errors and not goods:
        msg = str(errors[0].get("error", "")).lower()
        if "timeout" in msg:
            st = "timeout"
        elif any(k in msg for k in _QUOTA_ERROR_KEYWORDS):
            st = "quota-exhausted"
        elif "rate" in msg or "429" in msg:
            st = "rate-limited"
        elif any(k in msg for k in _BLOCKED_ERROR_KEYWORDS):
            st = "blocked"
        elif "auth" in msg or "401" in msg or "403" in msg:
            st = "auth-failed"
        else:
            st = "error"
        return {
            "engine": eng, "status": st,
            "results_count": 0, "latency_ms": latency_ms,
            "detail": str(errors[0].get("error", ""))[:200],
        }
    if goods and errors:
        return {
            "engine": eng, "status": "partial",
            "results_count": len(goods), "latency_ms": latency_ms,
        }
    return {
        "engine": eng, "status": "ok",
        "results_count": len(goods), "latency_ms": latency_ms,
    }


# fast 单发总墙钟预算（秒）。引擎级超时收紧（≥8s 源 cap 6s、half_open 2s）之外
# 的整条路径兜底：实测引擎 P95 跨度 1.5-8.5s，6s 预算覆盖绝大多数快引擎，
# 只砍 github 类拖尾——「等待剩余时间」与「是否再起新引擎」都受它约束。
# 仅 fast 模式生效；auto/budget/deep 宁可等待不截断（研究场景语义）。
_FAST_TOTAL_BUDGET_S = 6.0

# primary 独享启动宽限窗（秒）：主引擎在这个时间内完成且合格就免掉 hedge，
# 只付 1 次调用；窗口未完成才补发次引擎并行 race。实测引擎 P95 1.5-8.5s，
# 2.0s 覆盖「快引擎」子集（anysearch/local_bing 等），慢引擎（github 8.5s）
# 在窗口后补发 backup，避免拖尾。可调：偏快取小值（省墙钟）、偏省取大值（省成本）。
_PRIMARY_GRACE_S = 2.0

# 单引擎墙钟硬预算（秒）：含该引擎的**全部**重试尝试，超预算即停、不再发起
# 新尝试，由编排层切备选源。
#
# 为什么要这个上界（2026-09-10 实测）：重试会叠乘。anysearch 曾同时具备
#   引擎级 retry_count=1（2 次）× HTTP 级 max_retries=1（2 次）× 8s
#   = 最坏 32s（实测 31.3s）
# 用户侧表现是「一个查询卡半分钟」，而这期间既没切备选源、也没有任何
# 信号说明在等什么。逐处调小超时不是好解法——那会误杀慢网下正常的源；
# 给**单引擎总墙钟**设上界才是根本的，且对未来新增的重试层同样生效。
#
# 取值 10s：用户明确的体感阈值（「10 秒以内也应该能够解决」），
# 也 ≥ execution.default_timeout(8s)，保证单次正常尝试不被截断。
_PER_ENGINE_BUDGET_S = 10.0


def execute_search(query: str, decision: dict[str, Any], max_results: int,
                   timeout: int, depth: str, cache: SearchCache, skip_cache: bool,
                   mode: str = "auto",
                   since: str | None = None, until: str | None = None,
                   sort: str = "relevance",
                   on_progress: Optional[Callable[[Stage, dict[str, Any]], None]] = None) -> dict[str, Any]:
    """执行搜索：缓存 → 熔断/负缓存 → 引擎 → 融合 → 精排 → 过滤 → 写缓存。"""
    domain = decision.get("domain") or "general"
    engine_label = decision.get("engine", "auto")
    engines_combo = decision.get("engines_combo", decision.get("engines", [engine_label]))
    # 防御：过滤空引擎名（空串会在 registry 查无 → 空结果 → 熔断空键 ''）。
    # 全空时兜底 anysearch，防止下方 engines[0] IndexError（route 层已保证
    # combo 非空，此处仅防畸形 decision 直接调用 execute_search）。
    engines = [e for e in engines_combo if e] or ["anysearch"]
    parallel = decision.get("parallel", False) and len(engines) > 1

    # P0-001：查询理解 — clean_query 用于检索，exclude_terms 用于融合后过滤
    exclude_terms: list[str] = []
    retrieval_query = query
    qu = None
    try:
        from query_understanding import _understand_cached as understand
        qu = understand(query)
        exclude_terms = qu.exclude_terms
        # 仅当去否定片段后仍有实义内容时才替换检索词，避免空检索
        if qu.clean_query and qu.clean_query.strip():
            retrieval_query = qu.clean_query
    except ImportError:
        pass  # query_understanding 不可用
    except Exception as e:
        import logging
        logging.getLogger("unified_search").debug(f"查询理解跳过: {type(e).__name__}")

    # 词形规范化：全角→半角、拆斜杠、压多余空格（提升精确源命中，治型号/日期分隔符）
    try:
        from query_enhance import normalize_query
        retrieval_query = normalize_query(retrieval_query)
    except ImportError:
        pass

    if on_progress:
        on_progress(Stage.START, {"query": query})

    # 网络环境感知：慢网放大超时预算（避免误杀），快网收紧（更快响应）
    _eff_timeout = timeout
    try:
        from network_aware import adjusted_timeout, network_profile
        _eff_timeout = adjusted_timeout(timeout, engines)
        if _eff_timeout != timeout:
            import logging
            logging.getLogger("unified_search").debug(
                f"网络感知超时: {timeout}s → {_eff_timeout}s "
                f"({network_profile(engines).get('network')})",
            )
    except ImportError:
        pass

    # 时间窗归一化：下推/缓存键用归一化 ISO（相对值转绝对日期），
    # 后过滤用 epoch 秒；非法输入保持原样下推、不参与后过滤。
    since_iso, until_iso, since_ts, until_ts = _normalize_time_window(since, until)

    cache_engine_key = "+".join(sorted(engines)) if len(engines) > 1 else engines[0]
    # 时间窗并入缓存键：同一 query 不同 since/until 不串缓存；
    # 用归一化 ISO（7d 与等价绝对日期共享缓存；相对窗跨天自然过期不串旧数据）。
    # 仅当组合内含带时间能力引擎时隔离：无时间字段引擎忽略时间窗、结果相同，
    # 隔离只会降低命中率（7d/30d 查 octen/anysearch 命中同一缓存）。
    time_aware = any(_is_time_capable(e) for e in engines)
    if since_iso and time_aware:
        cache_engine_key += f"|since={since_iso}"
    if until_iso and time_aware:
        cache_engine_key += f"|until={until_iso}"

    if on_progress:
        on_progress(Stage.ROUTING, {"domain": domain, "engine": engine_label, "engines": engines})

    # combo 缓存命中（含 depth + 柔性命中）
    if not skip_cache:
        t_cache_start = time.time()
        hit = cache.get(query, cache_engine_key, max_results, domain=domain,
                        mode=mode, depth=depth)
        if hit:
            cache_elapsed = int((time.time() - t_cache_start) * 1000)
            if on_progress:
                on_progress(Stage.CACHE_HIT, {"cache_level": hit.get("_cache_level", "L?")})
            tfidf_scores = decision.get("tfidf_scores", [])
            if tfidf_scores and all(s.get("score", 0) == 0 for s in tfidf_scores):
                tfidf_scores = []
            # 排序在缓存读出后、返回前：缓存内容保持 score 序，sort 只改展示顺序
            hit_results = _sort_results(hit.get("results", []), sort)
            return {
                "query": query, "engine": engine_label, "engines": engines,
                "engines_combo": engines_combo, "cached": True,
                "cache_level": hit.get("_cache_level", "L?"),
                "domain": domain, "elapsed_ms": cache_elapsed,
                "tfidf_scores": tfidf_scores,
                "route_reason": decision.get("reason"),
                "login_hint": decision.get("login_hint"),
                "results": hit_results,
                "count": len(hit_results),
                "engines_used": hit.get("engines_used") or engines,
                "mode": mode, "depth": depth,
                "reranker": "skipped_cache",
                "engine_outcomes": hit.get("engine_outcomes") or [],
                "time_filtered": 0,
            }

    if on_progress:
        on_progress(Stage.SEARCHING, {"engines": engines})

    try:
        from circuit_breaker import get_breaker
        breaker = get_breaker()
    except ImportError:
        breaker = None

    t0 = time.time()
    raw_results: dict[str, list[dict[str, Any]]] = {}
    engine_outcomes: list[dict[str, Any]] = []
    engine_latency: dict[str, int] = {}
    wasted_ms = 0

    exec_cfg = get_execution_config()
    retry_count = exec_cfg.get("retry_count", 0)
    # 单引擎墙钟预算：config `execution.per_engine_budget_s` 可覆盖。
    # 用 exec_cfg 读取（与本函数其它 execution 项同源），这样用户可在
    # config.yaml 调整而无需改代码；非法值（非正数）回落到常量默认。
    try:
        _budget_cfg = float(exec_cfg.get("per_engine_budget_s",
                                         _PER_ENGINE_BUDGET_S))
    except (TypeError, ValueError):
        _budget_cfg = _PER_ENGINE_BUDGET_S
    if _budget_cfg <= 0:
        _budget_cfg = _PER_ENGINE_BUDGET_S

    # 慢源禁重试：timeout ≥ 8s 的引擎超时即放弃，避免「10s×3 次=30s」线性放大。
    # 超时本质上是源端慢/网络抖，重试不改变结果，只放大尾延迟；快速失败
    # （连接错/4xx）保留重试，重试成本低。
    try:
        _engine_specs = get_engines()
    except Exception:
        _engine_specs = {}

    def _engine_retries(eng: str) -> int:
        spec = (_engine_specs or {}).get(eng) or {}
        eng_timeout = None
        if isinstance(spec, dict):
            t = spec.get("timeout")
            if isinstance(t, (int, float)) and t > 0:
                eng_timeout = float(t)
        if eng_timeout is not None and eng_timeout >= 8.0:
            return 0
        return retry_count

    def _exec_engine(eng: str, retries: int | None = None,
                     eff_timeout: float | None = None) -> list[dict[str, Any]]:
        # P0-001：用 retrieval_query（clean_query）检索
        if retries is None:
            retries = _engine_retries(eng)
        # 默认超时用网络感知后的 _eff_timeout（慢网放大），与外层 as_completed
        # 等待预算一致；非 tight 引擎（anysearch 等）慢网下同样获得放大窗口。
        to = eff_timeout if eff_timeout is not None else _eff_timeout

        # ── 每引擎墙钟硬预算 ──────────────────────────────────────────
        # 问题（2026-09-10 实测）：重试会**叠乘**。anysearch 曾同时有
        #   引擎级重试 retry_count=1 → 2 次
        #   HTTP 级重试 max_retries=1 → 2 次
        #   8s 超时
        # 最坏 2×2×8 = 32s（实测 31.3s）。用户侧表现是「搜一个查询卡半分钟」，
        # 而这期间既没有切备选源、也没有任何信号说明在等什么。
        #
        # 修法不是逐处调小超时（那会误杀慢网下正常的源），而是给**单个引擎的
        # 总墙钟**设上界：后续尝试的可用超时 = 剩余预算，预算耗尽即停。
        # 这样无论嵌套几层重试，单引擎都不可能超过 cap。
        # 取值优先级：execution.per_engine_budget_s（config）> 常量默认 10.0。
        # fast 模式已有 6s 全局预算，此处取更紧的那个，避免互相打架。
        _eng_budget = _budget_cfg
        if mode == "fast":
            _eng_budget = min(_eng_budget, _FAST_TOTAL_BUDGET_S)
        _t_eng_start = time.time()

        last_result: list[dict[str, Any]] = []
        for _attempt in range(retries + 1):
            _remain = _eng_budget - (time.time() - _t_eng_start)
            # 只跳过**后续**尝试。首次必须发出：若因预算小而整段跳过，
            # 引擎的 outcome 会从 timeout 变成 no-results —— 语义从「慢」
            # 变成「没尝试」，会破坏既有 fast 预算测试的契约
            # （实测：patch 预算 0.5s 时首试被跳过，slow_bad_a/b 被标成
            #  no-results 而非 timeout）。
            if _attempt > 0 and _remain <= 0.5:
                break
            # 每次尝试的可用超时 = min(声明超时, 剩余预算)，下限 0.5s：
            # 首试受总预算约束（否则 fast 的 6s 预算会被 8s 首试突破），
            # 后续尝试自动收缩，保证单引擎总耗时不越界。
            attempt_to = min(to, max(0.5, _remain))
            last_result = engine_search(
                retrieval_query, eng, n=max_results, timeout=attempt_to, depth=depth, mode=mode,
                since=since_iso, until=until_iso, skip_cache=skip_cache,
            )
            if last_result and any("error" not in r for r in last_result):
                return last_result
        # 慢源（retries=0，超时即弃）不再用 balanced 补跑，避免超时场景双倍耗时
        if retries > 0 and depth != "balanced":
            _remain = _eng_budget - (time.time() - _t_eng_start)
            if _remain > 0.5:
                last_result = engine_search(
                    retrieval_query, eng, n=max_results,
                    timeout=min(to, max(0.5, _remain)),
                    depth="balanced", mode=mode,
                    since=since_iso, until=until_iso, skip_cache=skip_cache,
                )
        return last_result

    def _run_one(eng: str) -> tuple[str, list[dict[str, Any]], dict[str, Any], int]:
        """单引擎：缺 env → 负缓存 → 熔断 → per-engine 缓存 → 网络。"""
        from engines_base import pop_failure_note
        t_eng = time.time()

        # 缺环境变量前置拦截：把「静默 no-results」变成可行动的 error。
        # 显式 engine= 覆盖会绕过路由的 env 过滤（zhihu/exa 未配密钥时曾
        # 返回空列表，用户无法区分「没结果」和「没配置」）。
        missing_env = _missing_env_for(eng)
        if missing_env:
            lat = int((time.time() - t_eng) * 1000)
            outcome = _classify_engine_outcome(
                eng, [], lat, status_hint="skipped-missing-env")
            outcome["detail"] = (
                f"缺少环境变量：{' / '.join(missing_env)}（配置后重试）")
            return eng, [], outcome, lat

        # 时间窗只隔离带时间能力引擎的 per-engine 缓存（与 combo 键同语义）
        eng_since = since_iso if _is_time_capable(eng) else None
        eng_until = until_iso if _is_time_capable(eng) else None

        # 熔断
        if breaker is not None:
            allowed, reason = breaker.allow(eng)
            if not allowed:
                lat = int((time.time() - t_eng) * 1000)
                outcome = _classify_engine_outcome(eng, [], lat, status_hint="skipped-circuit-open")
                outcome["detail"] = reason
                return eng, [], outcome, lat
            neg = breaker.get_negative(query, eng)
            if neg:
                lat = int((time.time() - t_eng) * 1000)
                outcome = _classify_engine_outcome(
                    eng, [], lat, status_hint="no-results-cached",
                )
                outcome["detail"] = neg.get("status", "no-results")
                return eng, [], outcome, lat

        # per-engine 缓存
        if not skip_cache:
            eng_hit = cache.get_engine(
                query, eng, max_results, domain=domain, mode=mode, depth=depth,
                since=eng_since, until=eng_until,
            )
            if eng_hit is not None:
                lat = int((time.time() - t_eng) * 1000)
                # 标记缓存来源
                for r in eng_hit:
                    if isinstance(r, dict):
                        r.setdefault("_engine", eng)
                outcome = _classify_engine_outcome(eng, eng_hit, lat)
                outcome["status"] = "ok-cached" if eng_hit else "no-results-cached"
                return eng, eng_hit, outcome, lat

        # 网络调用
        # 答案型域（early_min 存在，1 条快照即可交付）的慢源收紧超时：
        # FRED/Eurostat 这类 timeout=10s 的源一旦挂掉就阻塞整条串行路径，
        # 而快源（worldbank 等 ~150ms）已能交付答案。慢源 5s 内没回就让位。
        # 非答案域（fast/auto/budget 且非 deep）：timeout≥10s 的引擎同样收紧
        # 到 6s——多数正常引擎 <2s，10-15s 的超时只为极端慢源兜底，
        # 串行/并行组合里一个慢源就会拖垮整个响应尾部。
        eff_to: float | None = None
        _tighten = (early_min is not None) or (
            mode in ("fast", "auto", "budget") and depth != "deep"
        )
        if _tighten:
            spec = (_engine_specs or {}).get(eng) or {}
            eng_to = None
            if isinstance(spec, dict):
                t = spec.get("timeout")
                if isinstance(t, (int, float)) and t > 0:
                    eng_to = float(t)
            cap = 5.0 if early_min is not None else 6.0
            # half_open 半开探测收紧到 2s：熔断器允许半开探测恢复，但探测应短促，
            # 避免 6s 探测阻塞串行/并行主路径（慢源拖尾主因）。2026-08 修复。
            if breaker is not None:
                try:
                    if breaker.status(eng).get("state") == "half_open":
                        cap = min(cap, 2.0)
                except Exception:
                    pass
            if eng_to is not None and eng_to >= 8.0:
                eff_to = min(float(timeout), cap)
            # 声明值 < 8s 的收紧由 engines.search 分发层统一执行
            # （spec timeout 是硬上限，调用方超时不得覆盖）
        try:
            res = _exec_engine(eng, eff_timeout=eff_to)
        except Exception as e:
            res = [{"error": str(e), "source": eng}]
        lat = int((time.time() - t_eng) * 1000)
        for r in res:
            if isinstance(r, dict):
                r.setdefault("_engine", eng)
                r.setdefault("_elapsed", lat / 1000.0)

        outcome = _classify_engine_outcome(eng, res, lat)
        # 归因寄存器合入：引擎内的静默失败路径（反爬命中/HTTP 状态码）没有
        # error 文本，outcome 会落成 no-results；归因寄存器把它们还原成
        # blocked / rate-limited 等真实状态，供熔断与 --json 可观测面使用。
        _note = pop_failure_note(eng)
        _attr: dict[str, Any] | None = None
        if _note and outcome["status"] in ("no-results", "error", "auth-failed"):
            if _note.get("category") == "blocked":
                outcome["status"] = "blocked"
            elif _note.get("category") == "rate_limited":
                outcome["status"] = "rate-limited"
            elif _note.get("category") == "auth" and outcome["status"] == "no-results":
                outcome["status"] = "auth-failed"
            outcome["detail"] = (
                f"{_note.get('reason', '')} {_note.get('detail', '')}".strip()
                or outcome.get("detail"))
        if _note:
            # 归因随熔断状态一起持久化：「为什么坏」必须在失败现场写下来，
            # 事后只能看到 kind 粗标签（把 kind 当响应文本再归类只会得到 unknown）
            try:
                from engine_failure import from_note
                _attr = from_note(_note, eng)
            except ImportError:
                _attr = None
        goods = [r for r in res if isinstance(r, dict) and "error" not in r]
        quota_batch.add(eng, bool(goods))
        if outcome["status"] == "quota-exhausted":
            # 远端配额耗尽：交由 quota 状态机接管（周期边界自愈），
            # 不计入下面的健康熔断——配额问题不是引擎健康问题
            _note_remote_quota_exhausted(eng, outcome.get("detail") or "")

        if breaker is not None:
            if outcome["status"] == "ok":
                breaker.record_success(eng)
                breaker.clear_negative(query, eng)
            elif outcome["status"] == "quota-exhausted":
                # 配额问题不是引擎健康问题，停用交给配额状态机（上面已记账）；
                # 但归因必须留下——「为什么不行」正是这一支的可观测缺口。
                breaker.record_note(eng, _attr)
            elif outcome["status"] == "no-results":
                breaker.record_failure(eng, kind="empty", attribution=_attr)
                breaker.set_negative(query, eng, status="no-results")
            elif outcome["status"] == "timeout":
                breaker.record_failure(eng, kind="timeout", attribution=_attr)
                breaker.set_negative(query, eng, status="timeout")
            elif outcome["status"] in ("blocked", "rate-limited"):
                # 被拦截 / 被限流都是源站行为，不是引擎故障：60s 短冷却，
                # 不累计 opens（否则被封引擎会被冤枉 auto-disable）。
                breaker.record_failure(eng, kind=outcome["status"], attribution=_attr)
                breaker.set_negative(query, eng, status=outcome["status"])
            else:
                breaker.record_failure(eng, kind="error", attribution=_attr)
                breaker.set_negative(query, eng, status=outcome["status"])

        if not skip_cache and goods:
            cache.set_engine(
                query, eng, max_results, goods,
                domain=domain, mode=mode, depth=depth,
                since=eng_since, until=eng_until,
            )
        elif not skip_cache and not goods:
            # 空结果短 TTL 写入 per-engine，配合负缓存
            cache.set_engine(
                query, eng, max_results, [],
                domain=domain, mode=mode, depth=depth,
                since=eng_since, until=eng_until,
            )

        return eng, (goods if goods else res), outcome, lat

    def _ingest(eng: str, res: list, outcome: dict, lat: int) -> None:
        raw_results[eng] = res
        engine_outcomes.append(outcome)
        engine_latency[eng] = lat
        if outcome["status"] not in ("ok", "ok-cached", "partial"):
            nonlocal_wasted[0] += lat

    nonlocal_wasted = [0]
    quota_batch = _QuotaBatch()
    early_stopped = False
    to_run = list(engines)
    # deep 模式全量并行；fast/auto/budget 可渐进 early-stop
    allow_early = mode in ("fast", "auto", "budget") and depth != "deep"

    # fast 总墙钟预算：deadline 之后不再起新引擎、不再等待慢线程
    _deadline = t0 + (_FAST_TOTAL_BUDGET_S if mode == "fast" else float("inf"))

    early_min = decision.get("early_stop_min_results")
    if parallel and to_run and allow_early and len(to_run) > 1:
        # Wave-1 race（2026-09-06）：primary 与次引擎并行起跑，先完成且结果
        # 合格者赢——原「primary 先行」串行等待下，primary 慢则整体慢（实测
        # github 引擎 8.5s 拖尾而次引擎 2.4s 就绪）；race 后墙钟由最先合格
        # 者决定。双成员都不合格则落 wave-2 并行补全（语义不变，且 wave-2
        # 的累计充分性判定天然包含 race 已收入的结果）。
        # 成本语义：fast+parallel 域固定 2 次引擎调用（原 1 次），fast 的
        # combo 以免费通用引擎为主，增量可忽略；结果质量仍由充分性判定+
        # 覆盖守卫把关，先到不等于放行。
        no_early = bool(decision.get("no_early_stop", False))
        primary, rest = to_run[0], to_run[1:]
        # 首发 + 分岔 hedged：先只发 primary，grace 宽限窗内完成且合格 → 只付
        # 1 次调用（成本回退消除）；窗内未完成 → 补发次引擎并行 race，先合格者
        # 赢（慢 primary 不拖整体）。弃置线程用 daemon 管理：赢家早停后不再被
        # 进程退出 join，修掉原 race shutdown(wait=False) 的「函数内快、进程级
        # 假快」——CLI 单发真省墙钟。质量仍由充分性判定 + 覆盖守卫把关。
        grace = max(0.3, min(_PRIMARY_GRACE_S, _eff_timeout * 0.25))
        if time.time() + grace > _deadline:
            grace = max(0.0, _deadline - time.time())

        def _daemon_start(eng: str):
            """daemon 线程跑 _run_one：弃置线程不阻塞进程退出。"""
            holder: dict[str, Any] = {"t0": time.time()}

            def _work() -> None:
                try:
                    holder["r"] = _run_one(eng)
                except Exception as exc:
                    holder["r"] = (
                        eng,
                        [{"error": str(exc), "source": eng}],
                        _classify_engine_outcome(
                            eng, [{"error": str(exc), "source": eng}], 0),
                        0,
                    )
            t = threading.Thread(target=_work, daemon=True)
            t.start()
            return holder, t

        def _ingest_holder(holder: dict[str, Any]) -> None:
            r = holder.get("r")
            if r:
                _ingest(r[0], r[1], r[2], r[3])

        def _holder_goods(holder: dict[str, Any]) -> list[dict[str, Any]]:
            r = holder.get("r")
            if not r:
                return []
            return [x for x in r[1] if isinstance(x, dict) and "error" not in x]

        ph, pt = _daemon_start(primary)
        pt.join(grace)
        if not pt.is_alive():
            # primary 在 grace 内完成：收结果，合格即早停（只 1 次调用）
            _ingest_holder(ph)
            goods_p = _holder_goods(ph)
            if not no_early and goods_p and _results_sufficient(
                    goods_p, mode=mode, min_results=early_min, query=query):
                early_stopped = True
        if not early_stopped and pt.is_alive():
            # primary 未在 grace 内完成：hedged 分岔，补发次引擎并行 race
            backup = rest[:1]
            if backup:
                rest = rest[1:]
                bh, bt = _daemon_start(backup[0])
                pending = [(ph, pt, primary), (bh, bt, backup[0])]
                _race_wait = min(_eff_timeout + 2,
                                 max(0.1, _deadline - time.time()))
                _race_deadline = time.time() + _race_wait
                while pending and time.time() < _race_deadline:
                    progressed = False
                    for (holder, th, eng) in list(pending):
                        if th.is_alive():
                            continue
                        _ingest_holder(holder)
                        goods = _holder_goods(holder)
                        if not no_early and goods and _results_sufficient(
                                goods, mode=mode, min_results=early_min,
                                query=query):
                            early_stopped = True
                        pending.remove((holder, th, eng))
                        progressed = True
                    if early_stopped or not pending:
                        break
                    if not progressed:
                        time.sleep(0.02)
                # 超时/弃置：仍活线程标记 timeout（daemon 自行结束，不阻塞
                # 退出）；恰在末次轮询后完成的线程照常入账——此前它既不
                # ingest 也不标 timeout，结果静默丢失。latency 记账用真实
                # 等待时长（原 timeout 参数×1000 是假值，污染遥测）
                for (holder, th, eng) in pending:
                    lat_ms = int((time.time() - holder.get("t0", t0)) * 1000)
                    if th.is_alive():
                        raw_results[eng] = [{"error": "timeout", "source": eng}]
                        engine_outcomes.append(_classify_engine_outcome(
                            eng, raw_results[eng], lat_ms, "timeout"))
                        nonlocal_wasted[0] += lat_ms
                    else:
                        _ingest_holder(holder)
        if (not early_stopped and rest
                and not (mode == "fast" and time.time() >= _deadline)):
            # fast 预算收口（与串行路径 `time.time() >= _deadline` 同语义）：
            # deadline 已过不再起新引擎；等待窗口也不越过 deadline——
            # 「fast 总墙钟预算」对并行路径同样成立
            w2_wait = min(_eff_timeout + 2,
                          max(0.1, _deadline - time.time()))
            t_w2 = time.time()
            with ThreadPoolExecutor(max_workers=min(len(rest), 3)) as ex:
                futures = {ex.submit(_run_one, eng): eng for eng in rest}
                try:
                    for fut in as_completed(futures, timeout=w2_wait):
                        eng = futures[fut]
                        try:
                            e2, res2, outcome2, lat2 = fut.result()
                            _ingest(e2, res2, outcome2, lat2)
                        except Exception as exc:
                            raw_results[eng] = [{"error": str(exc), "source": eng}]
                            engine_outcomes.append(_classify_engine_outcome(
                                eng, raw_results[eng], 0,
                            ))
                        # 累计结果已足够 → 提前终止剩余 wave-2（不白等慢源拖尾）
                        if not no_early and _cumulative_sufficient(
                                raw_results, mode=mode, min_results=early_min,
                                query=query):
                            for fut2 in futures:
                                if not fut2.done():
                                    fut2.cancel()
                            early_stopped = True
                            break
                except TimeoutError:
                    lat_ms = int((time.time() - t_w2) * 1000)
                    for fut, eng in futures.items():
                        if not fut.done():
                            fut.cancel()
                            raw_results[eng] = [{"error": "timeout", "source": eng}]
                            engine_outcomes.append(_classify_engine_outcome(
                                eng, raw_results[eng], lat_ms, "timeout",
                            ))
                            nonlocal_wasted[0] += lat_ms
                for fut in futures:
                    if not fut.done():
                        fut.cancel()
    elif parallel and to_run:
        with ThreadPoolExecutor(max_workers=min(len(to_run), 3)) as ex:
            futures = {ex.submit(_run_one, eng): eng for eng in to_run}
            try:
                for fut in as_completed(futures, timeout=_eff_timeout + 2):
                    eng = futures[fut]
                    try:
                        e, res, outcome, lat = fut.result()
                        _ingest(e, res, outcome, lat)
                    except Exception as e:
                        raw_results[eng] = [{"error": str(e), "source": eng}]
                        engine_outcomes.append(_classify_engine_outcome(
                            eng, raw_results[eng], 0,
                        ))
            except TimeoutError:
                for fut, eng in futures.items():
                    if not fut.done():
                        fut.cancel()
                        raw_results[eng] = [{"error": "timeout", "source": eng}]
                        engine_outcomes.append(_classify_engine_outcome(
                            eng, raw_results[eng], timeout * 1000, "timeout",
                        ))
                        nonlocal_wasted[0] += timeout * 1000
            for fut in futures:
                if not fut.done():
                    fut.cancel()
    else:
        # no_early_stop 域在串行路径同样生效：平台引擎「有结果」不等于「结果可用」，
        # fast 模式 parallel=False 必走本分支，此前曾在此被噪声结果短路
        no_early = bool(decision.get("no_early_stop", False))
        for eng in to_run:
            if time.time() >= _deadline:
                break  # fast 预算耗尽：止损不再起新引擎
            e, res, outcome, lat = _run_one(eng)
            _ingest(e, res, outcome, lat)
            goods = [r for r in res if isinstance(r, dict) and "error" not in r]
            if not goods:
                continue  # 无结果：串行试下一引擎
            # 答案型域 min_results=1：1 条快照即 early-stop
            if allow_early and not no_early and _results_sufficient(
                goods, mode=mode, min_results=early_min, query=query,
            ):
                early_stopped = True
                break
            # 默认串行：任一引擎有结果即停（历史行为）；答案型不够用则继续补源。
            # 词面覆盖守卫同语义：结果与查询几乎无交集 → 试下一引擎（救援线）
            if early_min is None and not no_early and _query_coverage_ok(goods, query):
                break

    wasted_ms = nonlocal_wasted[0]
    elapsed = int((time.time() - t0) * 1000)

    # 融合
    valid_lists = [
        res for res in raw_results.values()
        if res and any(isinstance(r, dict) and "error" not in r for r in res)
    ]
    # 去掉 error-only 列表中的 error 条目
    clean_lists = []
    for res in valid_lists:
        clean = [r for r in res if isinstance(r, dict) and "error" not in r]
        if clean:
            clean_lists.append(clean)

    # 查询主语言：噪声门与语言能力加权共用同一个判定（单一真源，
    # 避免两处各自算导致行为漂移）。
    _q_lang_for_fusion = (
        ((decision or {}).get("features") or {}).get("primary_lang") or None
    )

    # ── 多语言噪声门（result_lang）─────────────────────────────────────
    # 实测问题：引擎在非支持语言下会返回「成功但不相关」的结果——
    # juejin 在阿拉伯语查询下返回 10 条、相关度 0.00（全是通用热帖），
    # 却报告 status=ok。这类噪声混进 RRF 融合会污染最终结果。
    #
    # 判定不依赖「猜测查询语言」：用结果自身的书写系统（Unicode 码位，
    # 确定性）+ 查询词元命中率。语言不符且相关度低 → 判为噪声并剔除。
    #
    # 只在「查询语言可判定且非中英」时启用：中英是 argo 的主战场，
    # 现有引擎面足够宽，过早过滤会误伤（如英文查询命中中文优质内容）。
    _noise_dropped: list[dict[str, Any]] = []
    try:
        from result_lang import assess_results as _assess_lang
        _q_lang = _q_lang_for_fusion
        if _q_lang and _q_lang not in ("zh", "en", "mixed", "other", ""):
            _kept = []
            for _lst in clean_lists:
                if not _lst:
                    continue
                _eng = _lst[0].get("_engine") or _lst[0].get("source") or "?"
                _a = _assess_lang(query, _lst, expected_lang=_q_lang)
                if _a["verdict"] == "noise":
                    _noise_dropped.append({
                        "engine": _eng, "lang": _a["lang"],
                        "relevance": _a["relevance"],
                        "reason": (_a["reasons"][0] if _a.get("reasons")
                                   else "low relevance"),
                    })
                    continue
                _kept.append(_lst)
            clean_lists = _kept
    except Exception as _e:  # 噪声门是质量增强，失败不得拖垮主流程
        import logging as _lg
        _lg.getLogger("unified_search.search").debug(f"噪声门跳过: {_e}")

    if len(clean_lists) > 1:
        merged = rrf_merge(clean_lists, lang=_q_lang_for_fusion)
    elif clean_lists:
        merged = deduplicate_by_url(clean_lists[0])
        # 单引擎也补 consensus
        for r in merged:
            eng = r.get("_engine") or r.get("source") or ""
            if eng:
                r.setdefault("consensus_engines", [eng])
    else:
        merged = []

    # ── D6：macro_data 域证据下限（事实核查防单源）─────────────────────
    # deep 研究场景下结果 <2 条说明结构化源未覆盖该查询：追加通用兜底引擎
    # 补证据，避免「单引擎单结果」被事实核查 / 融合阶段当作答案；补搜结果
    # 一并进 RRF，consensus 维度天然加权。
    if (domain == "macro_data" and merged and len(merged) < 2
            and depth in ("deep", "research")):
        _done = set(raw_results.keys())
        _cands = [
            e for e in ("anysearch", "duckduckgo", "local_bing")
            if e not in _done
            and e in set(available_engines())
            and (breaker is None or breaker.allow(e)[0])
        ]
        _extra_lists = []
        for _eng in _cands[:2]:
            _e, _res, _out, _lat = _run_one(_eng)
            _ingest(_e, _res, _out, _lat)
            _goods = [r for r in _res if isinstance(r, dict) and "error" not in r]
            if _goods:
                _extra_lists.append(_goods)
        if _extra_lists:
            merged = rrf_merge([merged] + _extra_lists)

    # 配额记账：整批一次落盘（同一次搜索的 N 个引擎合并为一次写）。
    # 必须放在 D6 补搜之后：那是最后一个 _ingest 调用点，flush 提前会让
    # 补搜引擎的记账永远落不了盘（2026-09-13 审查实锤）。
    quota_batch.flush()

    # ── P0：过滤 SERP/跳转 URL（搜索结果页、baidu.com/link 等不可当信源正文）──
    if merged:
        try:
            from evidence import is_serp_or_jump_url as _is_serp
            merged = [r for r in merged if not _is_serp(r.get("url", ""))]
        except ImportError:
            pass  # evidence 不可用时跳过（本地五维 rerank 已对 SERP 降权）

    # ── minhash 近重复去重（结果级，RRF 后 / SERP 后）─────────────────────
    minhash_removed = 0
    if merged and len(merged) > 1:
        try:
            deduped, minhash_removed = minhash_dedupe(merged)
            merged = deduped
        except Exception as _e:
            import logging
            logging.getLogger("unified_search").debug(f"minhash 去重跳过: {type(_e).__name__}")

    # ── P2：多语言语言偏好软排序（ja/ko 前置含目标语言字符结果，软排不删除）──
    try:
        _p_lang = (decision or {}).get("features", {}).get("primary_lang")
        if _p_lang in ("ja", "ko"):
            merged = _lang_prefer_rerank(merged, _p_lang)
    except Exception:
        pass

    # 放宽截断：rerank 阶段看到 max_results*3 条，最终输出再截断
    merged = merged[:max(max_results * 3, 15)]

    # ── P0-001：按 exclude_terms 过滤（否定约束）──
    excluded_count = 0
    if merged and exclude_terms:
        kept = []
        low_terms = [t.lower() for t in exclude_terms if t]
        for r in merged:
            hay = f"{r.get('title', '')} {r.get('snippet', '')} {r.get('url', '')}".lower()
            if any(t in hay for t in low_terms):
                excluded_count += 1
                continue
            kept.append(r)
        merged = kept

    # ── 时间窗结果后过滤兜底 ──
    # 仅当组合内含带时间能力引擎时执行：无时间字段引擎的结果没有可滤对象，
    # 跳过遍历省开销；语义上与缓存键隔离保持一致（7d/30d 共享同一缓存内容）。
    time_filtered = 0
    if time_aware and (since_ts is not None or until_ts is not None) and merged:
        merged, time_filtered = _apply_time_window(merged, since_ts, until_ts)
        if time_filtered:
            import logging
            logging.getLogger("unified_search").debug(
                f"时间窗后过滤剔除 {time_filtered} 条（since={since_iso}, until={until_iso}）")

    # D5：时间窗空操作告警——用户指定了时间窗，组合内含时间能力引擎，
    # 但结果没有任何 published_at（下推缺失/源端未返回）：
    # `--since/--until` 实际未生效（宽松策略保留无时间字段条目，
    # time_filtered 恒 0）。透传 warning 而非静默降级。
    # 组合内不含时间能力引擎时是已知常态，不重复告警。
    time_filter_warning: str | None = None
    if time_aware and (since_ts is not None or until_ts is not None) and merged:
        if not any(r.get("published_at") for r in merged):
            time_filter_warning = (
                f"引擎未返回 published_at，时间窗 {since_iso or '任意'} ~ "
                f"{until_iso or '任意'} 未实际过滤"
            )

    # ── P0-002：空结果错误恢复决策树 ──
    recovery_info: dict[str, Any] | None = None
    # 复杂度门控：低复杂度查询只允许低成本放宽（L1/L2），
    # 禁用高价多源/跨语言（L3/L4）——简单问题不搞多轮，省 token。
    _max_rec_level: str | None = None
    try:
        from query_enhance import complexity_gate
        if qu is not None and complexity_gate(query, qu) == "low":
            _max_rec_level = "L2"
    except Exception:
        pass
    if not merged:
        try:
            from recovery import run_recovery
            tried = list(raw_results.keys()) or list(engines)
            fallback_engines = decision.get("engines_fallback") or []
            # 域路由零结果：恢复链放行 L3 换引擎。复杂度门此前把简单查询
            # 压到 L2——L3 被禁 + 全域零结果 = 域命中查询无解（实测
            # macro_data「中国GDP」零结果、恢复链空转）。engines_fallback
            # 里是路由的定向兜底声明（域未试成员优先），代价可控。
            rec_level = _max_rec_level
            if fallback_engines and decision.get("domain") not in (
                    None, "", "general", "general_search") \
                    and (rec_level is None or rec_level < "L3"):
                rec_level = "L3"
            try:
                enabled_set = set(available_engines())
            except Exception:
                enabled_set = None

            def _recovery_executor(rq: str, rengines: list[str]) -> list[dict[str, Any]]:
                """恢复执行器：串行跑候选引擎，取首个非空。跳过缓存避免污染。"""
                out: list[dict[str, Any]] = []
                for eng in rengines:
                    try:
                        # 恢复路径同样携带时间窗，避免恢复时丢弃用户约束
                        res = engine_search(rq, eng, n=max_results,
                                            timeout=timeout, depth=depth, mode=mode,
                                            since=since_iso, until=until_iso)
                    except Exception:
                        res = []
                    goods = [r for r in (res or [])
                             if isinstance(r, dict) and "error" not in r]
                    if goods:
                        for r in goods:
                            r.setdefault("_engine", eng)
                            r.setdefault("_recovered", True)
                        out.extend(goods)
                        break
                return out

            rec_results, rec_result = run_recovery(
                query, tried, _recovery_executor,
                engines_fallback=fallback_engines, enabled=enabled_set, mode=mode,
                max_level=rec_level)
            recovery_info = rec_result.to_dict()
            # P2-6：恢复遥测——query 截断脱敏，只记概览不记明细
            if _emit_telemetry is not None:
                try:
                    _emit_telemetry("recovery", {
                        "query": (query[:60] if query else query),
                        "triggered": recovery_info.get("triggered"),
                        "recovered": recovery_info.get("recovered"),
                        "level_used": recovery_info.get("level_used"),
                        "strategy_used": recovery_info.get("strategy_used"),
                        "steps_tried": len(recovery_info.get("steps_tried") or []),
                        "final_query": (recovery_info.get("final_query") or "")[:60],
                        "note": recovery_info.get("note", ""),
                    })
                except Exception:
                    pass
            if rec_results:
                merged = deduplicate_by_url(rec_results)[:max_results]
                # 恢复引擎按引擎分组记回 raw_results：engines_used 此前不含
                # 救援引擎（provenance 断链，实测恢复成功后 engines_used 仍
                # 只列原 combo），自适应学习也看不到恢复成功信号。
                _rec_by_eng: dict[str, list] = {}
                for r in merged:
                    eng = r.get("_engine") or r.get("source") or ""
                    if eng:
                        r.setdefault("consensus_engines", [eng])
                        _rec_by_eng.setdefault(eng, []).append(r)
                for _eng, _lst in _rec_by_eng.items():
                    raw_results.setdefault(_eng, _lst)
        except ImportError:
            pass  # recovery 模块不可用
        except Exception as e:
            import logging
            logging.getLogger("unified_search").debug(
                f"错误恢复跳过: {type(e).__name__}")

    # Reranker：ARGO_LOCAL_RERANK 开关（0 关闭本地五维兜底；默认 1 开启）
    local_rerank_on = os.environ.get("ARGO_LOCAL_RERANK", "1").strip() not in ("0", "false", "False", "no")
    reranker_status = "skipped_short"
    rank_method = "none"
    if mode == "fast" or depth == "fast":
        reranker_status = "skipped_fast"
    elif merged and len(merged) > 1:
        # 全量重排（top_n=len），由最终输出统一截断 max_results
        merged, reranker_status = rerank_results(query, merged, top_n=len(merged))
        if reranker_status == "ok":
            rank_method = "bocha"

    # 本地五维 rerank 兜底：受 ARGO_LOCAL_RERANK 开关控制（可观测 rank_method）
    if local_rerank_on and merged and len(merged) > 1 and reranker_status in (
            "skipped_no_key", "fallback", "skipped_fast", "skipped_short"):
        try:
            merged = local_five_dim_rerank(query, merged, domain=domain,
                                           top_n=len(merged))
            rank_method = "local_five_dim"
        except Exception as e:
            import logging
            logging.getLogger("unified_search").debug(
                f"本地五维 rerank 跳过: {type(e).__name__}")
    elif not local_rerank_on and reranker_status in ("skipped_no_key", "fallback", "skipped_fast", "skipped_short"):
        rank_method = "none"

    if merged:
        merged = _apply_consensus_and_sort(merged, max_results)

    _attach_selection_signals(merged, mode, depth)

    # ── P0-004：关键事实交叉标记（仅 deep/auto 且结果 ≥3；fast 跳过）──
    fact_alignment: dict[str, Any] | None = _align_facts_safe(merged, mode, depth)

    if on_progress:
        on_progress(Stage.MERGING, {"count": len(merged)})

    result_payload = {
        "results": merged,
        "engines_used": list(raw_results.keys()),
        "domain": domain,
        "engine_outcomes": engine_outcomes,
        "time_filtered": time_filtered,
        "time_filter_warning": time_filter_warning,
        "noise_dropped": _noise_dropped,
    }

    # 写 combo 缓存：空结果短 TTL / 时效 cap 由 cache.set 处理
    if not skip_cache:
        effective_ttl = None
        if merged and elapsed > 2000:
            # 慢查询略延长；时效域最多 2×，且仍受 resolve_ttl cap
            base_ttl = cache.resolve_ttl(domain, query=query)
            multiplier = min(2 ** (elapsed // 2000), 8)
            if base_ttl <= 900:
                effective_ttl = min(base_ttl * min(multiplier, 2), base_ttl * 2)
            else:
                effective_ttl = base_ttl * multiplier
        cache.set(
            query, cache_engine_key, max_results, result_payload,
            domain=domain, ttl=effective_ttl, mode=mode, depth=depth,
        )

    # 自适应学习
    try:
        from adaptive import get_learner
        learner = get_learner()
        for eng, res in raw_results.items():
            errors = [str(r.get("error", "")) for r in res if isinstance(r, dict) and "error" in r]
            # 配额/鉴权类是配置态故障，不是引擎质量信号：计入会把恢复后的
            # 引擎分数毒化在历史失败里（byted 配额期 38 连败 → 分数 0.072，
            # 配额自愈后无流量刷正分，死锁）。此类错误不计入，保持中性。
            # 配额关键词走 _QUOTA_ERROR_KEYWORDS 单一真源；鉴权类仅此处有。
            if errors and all(
                any(k in msg.lower() for k in
                    (*_QUOTA_ERROR_KEYWORDS, "unauthorized", "api key",
                     "forbidden", "401", "403"))
                for msg in errors
            ):
                continue
            success = bool(res and any(isinstance(r, dict) and "error" not in r for r in res))
            latency = engine_latency.get(eng, elapsed / max(len(raw_results), 1))
            cost = get_cost_factor(eng)
            learner.record(eng, success=success, latency_ms=latency, cost=0.0 if cost >= 0.85 else 0.001)
    except ImportError:
        pass
    except Exception as e:
        import logging
        logging.getLogger("unified_search").debug(f"自适应学习记录跳过: {type(e).__name__}")

    # 语言偏好：记录本轮查询语 + 输出观测快照（默认中英 + 系统 + 习惯）
    lang_pref_info: dict[str, Any] | None = None
    try:
        from lang_pref import record_query_lang, lang_pref_snapshot
        feats = decision.get("features") or {}
        q_lang = feats.get("primary_lang") or ""
        if not q_lang:
            try:
                from lang_detect import detect_language
                q_lang = detect_language(query)
            except ImportError:
                q_lang = ""
        if q_lang:
            record_query_lang(q_lang)
        lang_pref_info = lang_pref_snapshot(query_lang=q_lang)
    except ImportError:
        pass
    except Exception as e:
        import logging
        logging.getLogger("unified_search").debug(
            f"语言偏好记录跳过: {type(e).__name__}")

    if on_progress:
        on_progress(Stage.DONE, {"count": len(merged), "elapsed_ms": elapsed})

    tfidf_scores = decision.get("tfidf_scores", [])
    if tfidf_scores and all(s.get("score", 0) == 0 for s in tfidf_scores):
        tfidf_scores = []

    # 排序在返回前、写缓存后：缓存内容保持 score 序（缓存键/内容不受 sort 影响），
    # sort 只改变本次展示顺序；缓存命中路径在 return 前同样处理，两路径行为一致。
    out_results = _sort_results(merged, sort)

    out: dict[str, Any] = {
        "query": query, "engine": engine_label, "engines": engines,
        "engines_combo": engines_combo, "cached": False,
        "domain": domain, "elapsed_ms": elapsed,
        "tfidf_scores": tfidf_scores,
        "route_reason": decision.get("reason"),
        "results": out_results,
        "count": len(out_results), "engines_used": list(raw_results.keys()),
        "errors": _collect_errors(raw_results),
        "engine_outcomes": engine_outcomes,
        # 多语言噪声门：被剔除的引擎及原因（可观测，便于定位「为什么少了几个源」）
        "noise_dropped": _noise_dropped,
        "wasted_engine_ms": wasted_ms,
        "early_stopped": early_stopped,
        "reranker": reranker_status,
        "rank_method": rank_method,
        "minhash_removed": minhash_removed,
        "local_rerank_on": local_rerank_on,
        "recovery": recovery_info,
        "fact_alignment": fact_alignment,
        "exclude_terms": exclude_terms,
        "excluded_count": excluded_count,
        "time_filtered": time_filtered,
        "time_filter_warning": time_filter_warning,
        "mode": mode, "depth": depth,
        "login_hint": decision.get("login_hint"),
    }
    if lang_pref_info is not None:
        out["lang_pref"] = lang_pref_info
    return out


def _domain_matches(host: str, domain: str) -> bool:
    """host 等于域或是其子域（github.com 命中 api.github.com）。"""
    return host == domain or host.endswith("." + domain)


def filter_results_by_domains(
    results: list[Any] | None,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
) -> tuple[list[Any], str | None]:
    """域名后置过滤（引擎无关，融合排序之后执行）。

    include：仅保留命中域名（含子域）的结果；exclude：剔除命中域名的结果。
    返回 (保留列表, 说明文本)；两组过滤都为空时原样返回。
    """
    inc = [str(d).strip().lower() for d in (include_domains or []) if str(d).strip()]
    exc = [str(d).strip().lower() for d in (exclude_domains or []) if str(d).strip()]
    if not inc and not exc:
        return results or [], None
    kept: list[Any] = []
    dropped = 0
    for r in results or []:
        host = ""
        if isinstance(r, dict):
            try:
                from urllib.parse import urlparse as _up
                host = (_up(r.get("url", "") or "").hostname or "").lower()
            except Exception:
                host = ""
        if inc and not any(_domain_matches(host, d) for d in inc):
            dropped += 1
            continue
        if any(_domain_matches(host, d) for d in exc):
            dropped += 1
            continue
        kept.append(r)
    note = f"domain filter: kept {len(kept)}, dropped {dropped}"
    return kept, note


def _collect_errors(raw_results: dict[str, list[dict[str, Any]]]) -> list[str]:
    errors = []
    for eng, res in raw_results.items():
        for r in res:
            if isinstance(r, dict) and "error" in r:
                errors.append(f"{eng}: {r['error']}")
    return errors


# ── 统一入口 ──────────────────────────────────────────────────────────────────

def super_search(query: str, engine: str = "auto", n: int = 5, explain: bool = False,
                 skip_cache: bool = False, timeout: int = 10,
                 depth: str = "fast", mode: str = "auto", local_first: bool = False,
                 rewrite: bool = True, cache: Any = None,
                 on_progress: Optional[Callable[[Stage, dict[str, Any]], None]] = None,
                 input_kind: str = "auto",
                 plan_only: bool = False,
                 force_search: bool = False,
                 envelope: bool = True,
                 context: str = "search",
                 engines_boost: list[str] | None = None,
                 since: str | None = None,
                 until: str | None = None,
                 sort: str = "relevance",
                 include_local: bool = False,
                 include_domains: list[str] | None = None,
                 exclude_domains: list[str] | None = None) -> dict[str, Any]:
    """统一搜索便捷入口。

    执行分层（不阻塞日常）：
      - daily（默认 auto/fast）：直搜，不挂 plan，不要求用户确认
      - professional（mode=deep 或 depth=deep）：直搜 + 附加 plan 元数据
      - plan_only：仅离线计划（显式开关，不进热路径默认）
      - known-url：工具分流 handoff（不是「请确认后再搜」）

    Args:
        query: 搜索查询词
        engine: 指定引擎（默认 auto）
        n: 最大结果数
        explain: 是否输出路由解释
        skip_cache: 是否跳过缓存
        timeout: 超时
        depth: 搜索深度
        mode: 预算模式
        local_first: 强制本地优先
        rewrite: 是否自动改写查询（默认 True）
        on_progress: 可选进度回调 (stage, data)
        input_kind: auto|keyword|url-seed|known-url
        plan_only: 仅离线计划，不联网
        force_search: 即使判定 known-url 也强制多引擎搜索
        envelope: 附加 candidates/coverage/limitations
        context: search | research
        engines_boost: 垂直引擎前置列表（研究路径 boost，不锁死单引擎）
        since/until: 发布时间时间窗（如 7d / 2026-08-01），下推到支持时间窗的引擎

    注意：路由永远基于原始 query。改写词只用于引擎检索，避免
    「Python → 追加 pip/库」之类改写污染 package_search 等域规则。
    """
    cache = cache if cache is not None else SearchCache()
    original_query = query

    # 查询改写：仅影响检索串，不影响路由（在执行引擎前应用）
    rewrite_result = None
    search_query = original_query

    # ── 离线计划 / URL 分流（离线计划 / 输入分流）──
    # 纪律：build_plan 无网络、不回调本函数 → 无 plan↔search 死循环
    kind = "keyword"
    tier = "daily"
    plan_info: dict[str, Any] | None = None
    try:
        from plan import (
            build_plan, classify_input_kind, execution_tier, should_attach_plan,
        )
        kind = classify_input_kind(query, input_kind)
        tier = execution_tier(mode, depth, context)
        if plan_only:
            return build_plan(
                query, mode=mode, depth=depth, max_results=n,
                engine=engine if not local_first else "local_search",
                input_kind=input_kind,
                context=context,
            )
        if kind == "known-url" and not force_search:
            plan_info = build_plan(
                query, mode=mode, depth=depth, max_results=n,
                engine=engine, input_kind="known-url", context=context,
            )
            # 不发起多引擎搜索；返回 handoff 形态，避免把读链接当热搜
            out = {
                "query": query,
                "engine": None,
                "engines": [],
                "engines_combo": [],
                "cached": False,
                "domain": None,
                "elapsed_ms": 0,
                "results": [],
                "count": 0,
                "errors": [],
                "engine_outcomes": [],
                "wasted_engine_ms": 0,
                "early_stopped": False,
                "mode": mode,
                "depth": depth,
                "status": "handoff_required",
                "input_kind": "known-url",
                "execution_tier": tier,
                "requires_confirmation": False,
                "plan": plan_info,
                "handoff": plan_info.get("handoff"),
                "limitations": plan_info.get("limitations") or [],
                "schema_version": "1.0",
                "candidates": [],
                "coverage": [],
            }
            return out
    except ImportError:
        kind = "keyword"
        tier = "daily"
    except Exception as e:
        import logging
        logging.getLogger("unified_search").debug(f"plan 分流跳过: {type(e).__name__}")

    # 查询改写：追加领域关键词提升搜索质量
    # local_first 路径跳过改写：改写词面向 web 引擎召回设计，套到本地
    # 聚合（search_v3 智能路由）上会稀释查询、收窄引擎选择、扩大失败面
    #（实测改写词把「Python 异步编程」扩为 5 词长句后本地聚合返回空）。
    rewrite_result = None
    original_query = query
    if rewrite and not local_first:
        rewritten, rewrite_result = _apply_query_rewrite(original_query)
        if rewrite_result and rewrite_result.get("rewritten"):
            search_query = rewritten

    if local_first:
        decision = route_query(
            original_query, engine_override="local_search", mode=mode,
            depth=depth, context=context, engines_boost=engines_boost,
        )
    else:
        decision = route_query(
            original_query, engine_override=engine, mode=mode,
            depth=depth, context=context, engines_boost=engines_boost,
        )
    if context == "research":
        # 研究子查询禁早停：第一个「有结果」的垂直目录（如 models_dev 的
        # 模型规格页）不等于研究证据齐了，跑满 combo 再 RRF 融合。
        # 复用 no_early_stop 通道，串行/并行两条执行路径均已消费该标志。
        decision["no_early_stop"] = True
    if explain:
        combo = decision.get('engines_combo', decision.get('engines', []))
        print(
            f"[路由] {decision['reason']} → engine={decision['engine']} "
            f"combo={combo} domain={decision.get('domain')} "
            f"tfidf={decision.get('tfidf_scores', [])} mode={mode} kind={kind} tier={tier}",
            file=sys.stderr,
        )
        if search_query != original_query:
            print(f"[改写] {original_query} → {search_query}", file=sys.stderr)
    result = execute_search(
        query=search_query, decision=decision, max_results=n,
        timeout=timeout, depth=depth, cache=cache,
        skip_cache=skip_cache, mode=mode, on_progress=on_progress,
        since=since, until=until, sort=sort,
    )
    # 对外仍报告用户原始 query
    result["query"] = original_query
    if since:
        result["since"] = since
    if until:
        result["until"] = until
    if sort and sort != "relevance":
        result["sort"] = sort
    if rewrite_result and rewrite_result.get("rewritten"):
        result["rewritten_query"] = {
            "original": rewrite_result["original"],
            "rewritten": rewrite_result["rewritten"],
            "confidence": rewrite_result["confidence"],
            "reason": rewrite_result["reason"],
        }
    result["input_kind"] = kind
    result["status"] = "completed"
    result["execution_tier"] = tier
    result["requires_confirmation"] = False  # 日常/专业热路径永不阻塞等确认
    if original_query != query:
        result["query_original"] = original_query

    # professional：附加离线 plan 元数据（不阻断、不二次搜索）
    try:
        from plan import build_plan, should_attach_plan
        if should_attach_plan(mode, depth, context, plan_only=False):
            result["plan"] = build_plan(
                original_query, mode=mode, depth=depth, max_results=n,
                engine=engine if not local_first else "local_search",
                input_kind=kind if kind != "auto" else "auto",
                context=context,
            )
    except Exception:
        pass

    # 候选交接包（附加字段，不改 results 排序）
    if envelope:
        try:
            from candidate_envelope import attach_envelope
            extra_lim = []
            if kind == "url-seed":
                extra_lim.append(
                    "url-seed: seed URL was not fetched; results are related discovery only"
                )
            if result.get("recovery"):
                extra_lim.append("recovery used; engine fallback may differ from primary route")
            if tier == "daily":
                extra_lim.append(
                    "daily tier: direct search; no pre-confirm gate"
                )
            elif tier == "professional":
                extra_lim.append(
                    "professional tier: plan metadata attached; verify top-k before hard claims"
                )
            attach_envelope(
                result,
                query=query,
                input_kind=kind,
                route_reason=decision.get("reason"),
                extra_limitations=extra_lim,
            )
        except Exception as e:
            import logging
            logging.getLogger("unified_search").debug(
                f"envelope 跳过: {type(e).__name__}")
            result.setdefault("schema_version", "1.0")
            result.setdefault("limitations", [])

    # 证据闭环 P0：回填已核验证据分 + 高后果门控（finance/health/legal）
    # 输出 fetch_required / evidence_loop 汇总，每条结果带 fetch_suggested
    # 与 has_fetched_evidence / post_fetch_absorption（若此前 fetch 过）。
    try:
        from evidence_loop import gate_results
        gate = gate_results(result.get("results") or [], result.get("domain"))
        result["fetch_required"] = gate["fetch_required"]
        result["evidence_loop"] = {
            "high_consequence_domain": gate["high_consequence_domain"],
            "suggested": gate["suggested"],
            "verified_count": gate["verified_count"],
            "pending_count": gate["pending_count"],
        }
    except Exception as e:
        import logging
        logging.getLogger("unified_search").debug(f"证据门控跳过: {type(e).__name__}")

    # 域过滤（后置，引擎无关）：融合排序之后裁剪，sources 与 results 保持一致。
    # 裁剪导致不足 n 条是调用方过滤条件的诚实结果，不回填。
    if include_domains or exclude_domains:
        try:
            kept, note = filter_results_by_domains(
                result.get("results"), include_domains, exclude_domains)
            result["results"] = kept
            if note:
                result["domain_filter"] = note
        except Exception as e:
            logging.getLogger("unified_search").debug(
                f"[domain-filter] {type(e).__name__}: {e}")

    # 相关信源标准化（日常搜索底部引用列表；与 results 顺序一致）。
    # sources 是 results 的降级投影（URL 100% 重叠，实测零信息增量），
    # --no-envelope（Agent 默认输出）下不再生成：每次调用省 ~0.9KB，
    # 要 provenance 时用 envelope 模式或 --archive（2026-09-13 审查 P2-1）。
    if envelope:
        result["sources"] = build_sources(result.get("results") or [])

    # 本地命中并入（默认关）：seek 结果尾部拼入，来源 local_files，
    # 不参与融合评分。仅显式开启（--include-local / MCP include_local）才触发。
    if include_local:
        try:
            local_hits = _run_local_seek(query, n)
        except Exception as e:
            local_hits = []
            logging.getLogger("unified_search").debug(
                f"[include-local] {type(e).__name__}: {e}")
        if local_hits:
            result.setdefault("results", []).extend(local_hits)
            result["local_results"] = local_hits
        result["include_local"] = True

    return result


# ── 信源标准化 ─────────────────────────────────────────────────────────────────

# --fields agent：每条 result 保留的答案字段（P2-2）。
_AGENT_RESULT_FIELDS = (
    "title", "url", "snippet", "source", "score", "ref",
    "published_at", "fetch_suggested", "full_text_url",
    "image_url", "image_license",
)


def _strip_for_agent(payload: dict[str, Any]) -> dict[str, Any]:
    """--fields agent：输出只留答案内容（P2-2，2026-09-13）。

    在 --no-envelope 之上再剥遥测标量（tfidf_scores/lang_pref/engine_outcomes
    等）与 null/空键。fetch_required 必须保留——SKILL.md 的高后果门控纪律
    依赖它，不能被瘦身掉。
    """
    keep_top = (
        "query", "engine", "engines", "engines_used", "domain", "count",
        "mode", "depth", "status", "fetch_required", "evidence_loop",
        "errors", "login_hint",
    )
    out: dict[str, Any] = {k: payload[k] for k in keep_top
                           if payload.get(k) is not None}
    slim_results = []
    for r in payload.get("results") or []:
        if not isinstance(r, dict):
            continue
        slim = {k: r[k] for k in _AGENT_RESULT_FIELDS if r.get(k) is not None}
        slim_results.append(slim)
    out["results"] = slim_results
    out["count"] = len(slim_results)
    return out


def build_sources(results: list[Any] | None) -> list[dict[str, Any]]:
    """将 results 投影为编号信源列表（传统搜索引擎底部「相关链接」形态）。

    规则：
      - ref 与列表序号一致，从 1 起
      - 无 URL 的条目跳过（不占号？——保留占位会错位；跳过并重编号）
      - 字段齐全便于 Agent/归档复用，不伪造 metrics
    """
    sources: list[dict[str, Any]] = []
    ref = 0
    for r in results or []:
        if not isinstance(r, dict):
            continue
        url = (r.get("url") or "").strip()
        if not url:
            continue
        ref += 1
        sources.append({
            "ref": ref,
            "title": (r.get("title") or "")[:160],
            "url": url,
            "engine": r.get("source") or r.get("_engine") or r.get("engine"),
            "score": r.get("score"),
            "snippet": ((r.get("snippet") or "")[:160] or None),
        })
    return sources


# ── 输出格式化 ─────────────────────────────────────────────────────────────────

def format_text_output(results: dict[str, Any]) -> str:
    """日常搜索人读格式：条目正文 + 底部「相关信源」链接（类传统 SERP）。"""
    lines = []
    if results.get("status") == "handoff_required":
        ho = results.get("handoff") or {}
        lines.append("=== HANDOFF (known-url, search skipped) ===")
        lines.append(f"  url: {ho.get('url')}")
        lines.append(f"  suggest: {', '.join(ho.get('suggested_tools') or [])}")
        for lim in (results.get("limitations") or [])[:4]:
            lines.append(f"  ! {lim}")
        return "\n".join(lines)
    if results.get("status") in ("ready",) and results.get("steps") and not results.get("results"):
        # plan-only
        lines.append(f"=== PLAN {results.get('status')} kind={results.get('input_kind')} ===")
        route = results.get("route") or {}
        lines.append(f"  engine={route.get('backend')} domain={route.get('domain')} combo={route.get('engines_combo')}")
        for lim in (results.get("limitations") or [])[:5]:
            lines.append(f"  ! {lim}")
        return "\n".join(lines)

    count = results.get("count", 0)
    elapsed = results.get("elapsed_ms", 0)
    engine = results.get("engine", "?")
    cached = results.get("cached", False)
    cache_level = results.get("cache_level", "")
    domain = results.get("domain", "")
    mode = results.get("mode", "auto")

    header = f"=== {count} results ({elapsed}ms via {engine})"
    if cached:
        header += f" [CACHE {cache_level}]"
    elif domain:
        header += f" [domain:{domain}]"
    if mode != "auto":
        header += f" [mode:{mode}]"
    if results.get("input_kind"):
        header += f" [kind:{results.get('input_kind')}]"
    lines.append(header)

    for err in results.get("errors", [])[:3]:
        lines.append(f"  [ERROR] {err}")

    # 正文区：编号 + 标题 + 摘要（链接沉底，避免噪声）
    sources = results.get("sources")
    if not isinstance(sources, list) or not sources:
        sources = build_sources(results.get("results") or [])

    # 用 URL 对齐 ref
    url_to_ref = {s.get("url"): s.get("ref") for s in sources if isinstance(s, dict)}
    body_items = [r for r in (results.get("results") or []) if isinstance(r, dict)]
    for r in body_items:
        url = (r.get("url") or "").strip()
        ref = url_to_ref.get(url)
        if ref is None and url:
            # 未进 sources 时临时编号
            ref = "?"
        score = r.get("score", 0)
        title = (r.get("title") or "?")[:80]
        score_s = f"{score:.2f}" if isinstance(score, (int, float)) and score else "—"
        lines.append(f"  [{ref}] {title}")
        snippet = (r.get("snippet") or "").strip()
        if snippet:
            lines.append(f"      {snippet[:140]}")
        elif score:
            lines.append(f"      (score={score_s})")

    # 底部相关信源（传统搜索引擎形态）
    if sources:
        lines.append("")
        lines.append("── 相关信源 ──")
        for s in sources:
            if not isinstance(s, dict):
                continue
            ref = s.get("ref", "?")
            eng = s.get("engine") or ""
            title = (s.get("title") or "")[:60]
            url = s.get("url") or ""
            eng_s = f" · {eng}" if eng else ""
            if title:
                lines.append(f"  [{ref}] {title}{eng_s}")
                if url:
                    lines.append(f"      {url}")
            elif url:
                lines.append(f"  [{ref}] {url}{eng_s}")

    if results.get("limitations"):
        lines.append("")
        lines.append("── limitations ──")
        for lim in results["limitations"][:4]:
            lines.append(f"  ! {lim}")

    return "\n".join(lines)


# ── CLI 主入口 ─────────────────────────────────────────────────────────────────

def _run_local_seek(query: str, max_n: int = 5) -> list[dict[str, Any]]:
    """本机文件命中（--include-local 用）：调 local-seek 子技能，JSON 并入。

    仅在显式开启时调用（默认零开销）；结果不参与融合评分，
    仅作尾部来源（source=local_files）。
    """
    import subprocess as _sp

    # 安装感知 + 单一真源：委托 seek_locator 统一发现 local-seek/scripts/seek.py
    # （打包子技能优先，ARGO_LOCAL_SEEK_PATH / ARGO_LOCAL_SEEK_ROOTS 承载自定义/遗留）。
    # 不硬编码 ~/.agents/skills|~/.claude/skills 主机路径（SKILL.md 明令禁止）。
    from seek_locator import resolve_seek_py
    seek_py = resolve_seek_py()
    if not seek_py or not os.path.isfile(seek_py):
        return []
    r = _sp.run(
        [sys.executable, seek_py, query, "--json", "--max", str(max(max_n, 1))],
        capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=20,
        env={**os.environ, "PYTHONUTF8": "1"},  # 子进程是自家 seek.py，双向显式 UTF-8
    )
    if r.returncode != 0 or not r.stdout.strip():
        return []
    try:
        payload = json.loads(r.stdout)
    except ValueError:
        return []
    hits = payload.get("results") or payload.get("files") or []
    out = []
    for h in hits[:max_n]:
        if not isinstance(h, dict):
            continue
        path = h.get("path") or h.get("file") or ""
        line = h.get("line") or h.get("lineno") or 1
        url = f"file://{path}" + (f"#{line}" if str(line).isdigit() else "")
        out.append({
            "title": path,
            "url": url,
            "snippet": (h.get("snippet") or h.get("text") or h.get("line_text") or "")[:160],
            "source": "local_files",
            "score": 0.0,
            "kind": "local",
        })
    return out


def main():
    parser = argparse.ArgumentParser(
        description="Unified Search v2 — 统一搜索 CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python3 search.py "python async"
  python3 search.py "英伟达财报" --explain --json
  python3 search.py "基金推荐" --mode fast
  python3 search.py "AAPL" --engine anysearch --domain finance --sub_domain finance.us_stock
        """,
    )
    parser.add_argument("query", nargs="?")
    parser.add_argument("--engine", "-e", default="auto")
    parser.add_argument("--max-results", "-n", type=int, default=5)
    parser.add_argument("--depth", "-d", default="fast",
                        choices=["fast", "balanced", "deep"])
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--explain", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--timeout", "-t", type=int, default=10)
    parser.add_argument("--list-engines", action="store_true",
                        help="列出引擎；加 --detail 看 env/准入/routable 状态")
    parser.add_argument("--detail", action="store_true",
                        help="与 --list-engines 联用：输出详细状态")
    parser.add_argument("--routable-only", action="store_true",
                        help="与 --list-engines 联用：仅可自动路由的引擎")
    parser.add_argument("--mode", default="auto",
                        choices=["fast", "auto", "deep", "budget"],
                        help="预算模式: fast=免费优先, auto=成本感知, deep=质量优先, budget=配额控制")
    parser.add_argument("--since", default=None,
                        help="发布时间下限（7d / 2026-08-01），下推到支持时间窗的引擎")
    parser.add_argument("--until", default=None,
                        help="发布时间上限（7d / 2026-08-01），下推到支持时间窗的引擎")
    parser.add_argument("--sort", default="relevance",
                        choices=["relevance", "oldest", "newest"],
                        help="时间排序：relevance=相关度（默认）, oldest=最早在前（溯源）, newest=最新在前")
    parser.add_argument("--include-domains", default="",
                        help="仅保留这些域名（含子域），逗号分隔，如 github.com,arxiv.org")
    parser.add_argument("--exclude-domains", default="",
                        help="排除这些域名（含子域），逗号分隔，如 pinterest.com")
    parser.add_argument("--local-first", action="store_true",
                        help="强制优先使用 local_search 零成本聚合引擎")
    parser.add_argument(
        "--include-local", action="store_true",
        help="并入本机文件命中（seek 结果尾部，source=local_files；默认关）",
    )
    parser.add_argument("--domain", default="", help="AnySearch 垂直域")
    parser.add_argument("--sub_domain", default="", help="AnySearch 子域")
    parser.add_argument("--progress", action="store_true")
    parser.add_argument(
        "--input-kind", default="auto",
        choices=["auto", "keyword", "url-seed", "known-url"],
        help="输入类型：known-url 默认不热搜；url-seed 只作发现线索",
    )
    parser.add_argument("--plan-only", action="store_true",
                        help="仅输出离线计划（不联网）")
    parser.add_argument("--force-search", action="store_true",
                        help="known-url 也强制多引擎搜索（不推荐）")
    parser.add_argument("--no-envelope", action="store_true",
                        help="不附加 candidates/coverage/limitations")
    parser.add_argument(
        "--fields", choices=("full", "agent"), default="full",
        help="JSON 字段档位：full=全量（默认）；agent=只留答案内容（剥遥测标量"
             "与 null 键，每条 result 留 title/url/snippet/source/score 等；"
             "fetch_required 保留），配合 --no-envelope 供 Agent 消费",)
    parser.add_argument(
        "--archive",
        action="store_true",
        help="将本次搜索 envelope 落盘到工作区归档（不抓正文/不下载）",
    )
    parser.add_argument(
        "--archive-dir",
        type=str,
        default=None,
        help="归档根目录（默认 ARGO_ARCHIVE_ROOT 或 工作区/数据/argo-search-archive）",
    )
    parser.add_argument("--archive-tag", default=None, help="归档标签，便于 list 过滤")
    parser.add_argument("--archive-note", default=None, help="归档备注")
    parser.add_argument(
        "--verify",
        nargs="?",
        const=3,
        type=int,
        default=None,
        metavar="TOP_K",
        help="证据核验：对 top-k 未核验结果 fetch 正文、回填证据分、输出 evidence_revision 分布（默认 3）",
    )

    args = parser.parse_args()

    if args.list_engines:
        # --engine 兼作清单过滤（`--engine auto` 是搜索默认值，不算过滤）。
        # 全量详细行实测约 22 KB（2026-09-13 复测；此前误记 186 KB），Agent 查单个引擎状态时不该付这个
        # 代价——实测此前 `--list-engines --detail --engine egov_law` 忽略过滤、
        # 照样吐全量。
        _wanted = None
        if args.engine and args.engine != "auto":
            _wanted = [e.strip() for e in args.engine.split(",") if e.strip()] or None
        if args.detail:
            try:
                from engine_status import list_engines_detail, format_engines_table
                rows = list_engines_detail(routable_only=args.routable_only,
                                           engines=_wanted)
                if _wanted:
                    # 未命中的名字要显式报出（走 stderr，保持 stdout 是纯 JSON）——
                    # 否则「查了没输出」会被误读成「该引擎状态为空」。
                    _got = {r.get("engine_id") for r in rows}
                    _missing = [e for e in _wanted if e not in _got]
                    if _missing:
                        print(f"[list-engines] 未收录的引擎: {', '.join(_missing)}",
                              file=sys.stderr)
                if args.json_output:
                    print(json.dumps(rows, ensure_ascii=False, indent=2))
                else:
                    print(format_engines_table(rows))
            except Exception as e:
                print(json.dumps({"error": str(e), "engines": available_engines()}, ensure_ascii=False, indent=2))
        else:
            try:
                names = available_engines(routable_only=args.routable_only)
            except TypeError:
                names = available_engines()
            if _wanted:
                names = [n for n in names if n in set(_wanted)]
            print(json.dumps(names, ensure_ascii=False, indent=2))
        return

    if not args.query:
        parser.error("必须提供搜索关键词")

    # 归档需要 envelope；--archive 时强制保留
    use_envelope = (not args.no_envelope) or args.archive
    results = super_search(
        query=args.query,
        engine=args.engine,
        n=args.max_results,
        explain=args.explain,
        skip_cache=args.no_cache,
        timeout=args.timeout,
        depth=args.depth,
        mode=args.mode,
        local_first=args.local_first,
        input_kind=args.input_kind,
        plan_only=args.plan_only,
        force_search=args.force_search,
        envelope=use_envelope,
        since=args.since,
        until=args.until,
        sort=args.sort,
        include_domains=[d for d in args.include_domains.split(",") if d.strip()] or None,
        exclude_domains=[d for d in args.exclude_domains.split(",") if d.strip()] or None,
    )
    results["query"] = args.query

    # 本地命中并入（默认关）：seek 结果尾部拼入，来源 local_files，不参与融合评分
    if args.include_local:
        try:
            local_hits = _run_local_seek(args.query, args.max_results)
        except Exception as e:
            local_hits = []
            sys.stderr.write(f"  [include-local] {type(e).__name__}: {e}\n")
        if local_hits:
            results.setdefault("results", []).extend(local_hits)
            results["local_results"] = local_hits
        results["include_local"] = True

    if args.archive and results.get("status") != "handoff_required":
        try:
            from archive_run import write_search_archive, resolve_archive_root
            root = resolve_archive_root(args.archive_dir) if args.archive_dir else None
            if args.archive_dir:
                root = resolve_archive_root(args.archive_dir)
            meta = write_search_archive(
                results,
                root=root,
                tag=args.archive_tag,
                note=args.archive_note,
                source="argo_search",
            )
            if not args.json_output:
                print(
                    f"  [archive] {meta.get('run_id')} → {meta.get('run_dir')}",
                    file=sys.stderr,
                )
        except Exception as e:
            print(f"  [archive error] {type(e).__name__}: {e}", file=sys.stderr)

    # 证据闭环 P0：--verify 显式核验 top-k 未核验结果（fetch + 回填 + revision 分布）
    if args.verify:
        try:
            from evidence_loop import verify_results
            v = verify_results(results.get("results") or [], args.query, top_k=args.verify)
            results["verify"] = v
            results["fetch_required"] = bool(results.get("fetch_required"))
            # verify 已回填/核验结果 → 刷新门控汇总，避免 suggested 含已核验 URL
            try:
                from evidence_loop import gate_results
                gate = gate_results(results.get("results") or [], results.get("domain"))
                results["evidence_loop"] = {
                    "high_consequence_domain": gate["high_consequence_domain"],
                    "suggested": gate["suggested"],
                    "verified_count": gate["verified_count"],
                    "pending_count": gate["pending_count"],
                }
            except Exception:
                pass
            if not args.json_output:
                rs = v.get("revision_summary") or {}
                print(
                    f"  [verify] 核验 {rs.get('n', 0)} 条，"
                    f"improved={rs.get('improved', 0)} unchanged={rs.get('unchanged', 0)} "
                    f"degraded={rs.get('degraded', 0)} mean_delta={rs.get('mean_delta', 0)}",
                    file=sys.stderr,
                )
        except Exception as e:
            print(f"  [verify error] {type(e).__name__}: {e}", file=sys.stderr)

    if args.json_output:
        public = {k: v for k, v in results.items() if not k.startswith("_")}
        if args.fields == "agent":
            public = _strip_for_agent(public)
        print(json.dumps(public, ensure_ascii=False, indent=2))
    else:
        print(format_text_output(results))
        if results.get("archive"):
            ar = results["archive"]
            print(f"  archived → {ar.get('run_dir')}")


if __name__ == "__main__":
    main()
