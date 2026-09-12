#!/usr/bin/env python3
"""专用构建器：技术社区 / 文档 / AI 搜索"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

from engines_base import (safe_search, _run, _resolve, _get_path, _coerce_field,
                          _http_get_raw, mcp_error_of as _mcp_error_of)

logger = logging.getLogger("unified_search.engines")

# ── Exa 专用引擎 ──────────────────────────────────────────────────────────────

def _build_exa_engine(spec: dict[str, Any]) -> Any:
    """Exa 语义搜索专用引擎（embedding 匹配 + 内容摘要）"""
    timeout = spec.get("timeout", 15)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, depth: str = "fast", **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        api_key = os.environ.get("EXA_API_KEY", "")
        if not api_key:
            logger.warning("EXA_API_KEY 未设置")
            return []
        url = "https://api.exa.ai/search"
        body = json.dumps({
            "query": query,
            "type": "auto",
            "numResults": min(n, 10),
            "contents": {"text": {"maxCharacters": 400}},
        }).encode("utf-8")
        headers = {"x-api-key": api_key, "Content-Type": "application/json"}
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                results = []
                for r in data.get("results", []):
                    snippet = (r.get("text") or r.get("snippet") or "")[:300]
                    # 轻量清洗：去掉 YAML front-matter（--- 开头的内容块）与导航噪声
                    if snippet.startswith("---"):
                        idx = snippet.find("---", 3)
                        if idx != -1:
                            snippet = snippet[idx + 3:].strip()
                    results.append({
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "snippet": snippet,
                        "source": "exa",
                        # type:auto 模式不返回 score 字段（恒 0 会被 RRF 埋没），
                        # 无 score 时给固定基线分
                        "score": r.get("score") or 0.75,
                    })
                return results
        except Exception as e:
            logger.warning(f"Exa 引擎失败: {e}")
            return []
    return _engine


# ── anysearch 通用搜索（JSON-RPC / MCP，进程内 builder 替代 subprocess）───────

def _build_anysearch_engine(spec: dict[str, Any]) -> Any:
    """anysearch 通用搜索主力：POST JSON-RPC 到 api.anysearch.com/mcp。

    替换原 `type: cli` 的 subprocess 调用（每次启动 python3 解释器 ~200-300ms），
    改为进程内 builder + HttpClient（UA 轮换/重试/退避/Retry-After）：
    省启动开销 + 降低 errors（限流/网络）导致的高失败。2026-08 优化。
    """
    timeout = spec.get("timeout", 8)
    url = "https://api.anysearch.com/mcp"

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None,
                domain: str = "", sub_domain: str = "", **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        args: dict[str, Any] = {"query": query, "max_results": min(n, 10)}
        if domain:
            args["domain"] = domain
        if sub_domain:
            args["sub_domain"] = sub_domain
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "search", "arguments": args}}
        headers = {"Content-Type": "application/json"}
        api_key = os.environ.get("ANYSEARCH_API_KEY", "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            from http_client import HttpClient
            # max_retries=0：**不再做引擎内连接级重试**（2026-09-10 实测修正）。
            # 原因：重试会与编排层的引擎级重试叠乘。此前 max_retries=1 与
            # 外层 retry_count=1 组合出 2×2=4 次尝试 × 8s = 32s 的最坏耗时，
            # 用户侧表现为「查询卡半分钟」，且期间不切备选源。
            # 失败切换的职责在编排层（熔断降权 + hedged 补发备选引擎 +
            # search.py 的每引擎墙钟预算），引擎内重复尝试只会放大延迟。
            client = HttpClient(timeout=to, max_retries=0, jitter=False)
            resp = client.post(url, body=body, extra_headers=headers)
        except ImportError:
            return []
        if resp.get("status", 0) >= 400 or not resp.get("text"):
            return []
        try:
            data = json.loads(resp["text"])
        except (ValueError, TypeError):
            return []
        # ── 上游错误必须显式上报，不得静默返回空 ──────────────────────
        # 实测（2026-09-10）上游返回 HTTP 200 但 isError=true：
        #   {"result":{"content":[{"text":"Service temporarily unavailable."}],
        #              "isError":true}}
        # 旧实现忽略 isError，而该文本不含配额关键词、也没有 "### N." 结果块，
        # 于是静默返回 [] —— 用户看到「没结果」而非「上游不可用」，
        # 熔断器也拿不到失败信号。这是本仓第 N 次「失败伪装成成功」。
        mcp_err = _mcp_error_of(data)
        if mcp_err:
            logger.warning(f"anysearch 上游错误: {mcp_err}")
            return [{"error": mcp_err, "source": "anysearch"}]

        content = data.get("result", {}).get("content", []) or []
        records = [
            (i.get("text", "") if isinstance(i, dict) else str(i)) for i in content
        ]
        joined = "".join(records).lower()
        # 配额/限流：仅当无任何结果块（### N.）且文本含配额信号时判定，避免
        # 正常结果正文里出现 'quota/429/rate limit' 等词被误判为配额耗尽。
        #
        # **刻意返回 []（不是 error item）**——与上方 isError 分支处理相反，
        # 理由不同、不可混同：
        #   · isError=true 是上游**明确宣示失败**（MCP 协议级信号）→ 必须报错，
        #     让熔断器拿到信号、停止对已坏源的空转调用。
        #   · 配额耗尽是**临时**状态（按日/月重置），上游本身健康。
        #     返回 [] 让路由优雅 fallback 到其它源即可；若报 error 会驱动
        #     熔断 open、把「只是暂时没额度」的引擎禁用，反而有害。
        # 既有测试 test_quota_only_response_returns_empty 锁定此契约。
        has_result_blocks = any("### " in t for t in records)
        if (not has_result_blocks) and any(
                k in joined for k in ("quota", "exhausted", "recharge",
                                      "rate limit", "429", "daily_free_quota")):
            return []
        results = []
        for item in content:
            text = item.get("text", "") if isinstance(item, dict) else str(item)
            # 结果块以行首「### N.」分隔。用 (?m)^ 锚定行首而非 \n 前缀：
            # 首个结果块顶格开头（无前导换行）时，\n 前缀版本会把第一块
            # 并进 blocks[0] 而整块丢失（首个结果静默消失）。
            blocks = re.split(r"(?m)^### \d+\.\s", text)
            for block in blocks[1:]:
                lines = block.strip().split("\n")
                title = lines[0].strip() if lines else ""
                item_url = ""
                snippet_lines = []
                for line in lines[1:]:
                    ls = line.strip()
                    if ls.startswith("- **URL**: "):
                        item_url = ls.replace("- **URL**: ", "")
                    elif ls.startswith("**URL**: "):
                        item_url = ls.replace("**URL**: ", "")
                    else:
                        snippet_lines.append(line)
                snippet = "\n".join(snippet_lines).strip()[:500]
                if title:
                    results.append({
                        "title": title[:200], "url": item_url, "snippet": snippet,
                        "source": "anysearch", "score": 0.7,
                    })
        return results
    return _engine


# ── 搜狗微信搜索引擎 ─────────────────────────────────────────────────────────

def _build_wechat_sogou_engine(spec: dict[str, Any]) -> Any:
    """搜狗微信搜索引擎（weixin.sogou.com）

    抓取搜狗微信搜索结果页，提取公众号文章标题、链接、摘要、公众号名。
    无需登录，无需 API key，纯 HTML 解析。
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = f"https://weixin.sogou.com/weixin?type=2&query={up.quote(query)}&ie=utf8"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                html = resp.read().decode("utf-8")
            results = []
            li_pattern = re.compile(
                r'<li\s+id="sogou_vr_11002601_box_\d+"[^>]*>(.*?)</li>', re.DOTALL
            )
            for li in li_pattern.findall(html)[:n]:
                title_match = re.search(
                    r'<h3[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', li, re.DOTALL
                )
                if not title_match:
                    continue
                href = title_match.group(1).replace("&amp;", "&")
                title = re.sub(r"<[^>]+>", "", title_match.group(2)).strip()
                title = title.replace("<!--red_beg-->", "").replace("<!--red_end-->", "")

                summary_match = re.search(
                    r'<p[^>]*class="txt-info"[^>]*>(.*?)</p>', li, re.DOTALL
                )
                summary = re.sub(r"<[^>]+>", "", summary_match.group(1)).strip() if summary_match else ""
                summary = summary.replace("<!--red_beg-->", "").replace("<!--red_end-->", "")

                account_match = re.search(
                    r'<span[^>]*class="all-time-y2"[^>]*>(.*?)</span>', li, re.DOTALL
                )
                account = re.sub(r"<[^>]+>", "", account_match.group(1)).strip() if account_match else ""

                # 发布时间：结果条内嵌 script 写入 document.write(timeConvert('10位unix秒'))
                time_match = re.search(r"timeConvert\('?(\d{10})'?\)", li)
                published_at = ""
                if time_match:
                    try:
                        published_at = datetime.fromtimestamp(
                            int(time_match.group(1))
                        ).astimezone().isoformat(timespec="seconds")
                    except (ValueError, OSError):
                        published_at = ""

                result = {
                    "title": title[:80],
                    "url": "https://weixin.sogou.com" + href if href.startswith("/") else href,
                    "snippet": summary[:200],
                    "account": account,
                    "source": "wechat_sogou",
                }
                if published_at:
                    result["published_at"] = published_at
                results.append(result)
            return results
        except Exception as e:
            logger.warning(f"搜狗微信搜索失败: {e}")
            return []
    return _engine


# ── Hacker News 搜索引擎 ──────────────────────────────────────────────────────

def _build_hackernews_engine(spec: dict[str, Any]) -> Any:
    """Hacker News 搜索（Algolia API）"""
    timeout = spec.get("timeout", 8)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = f"https://hn.algolia.com/api/v1/search?query={up.quote(query)}&tags=story&hitsPerPage={min(n, 10)}"
        headers = {"User-Agent": "argo-search/1.0"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                data = json.loads(resp.read())
            results = []
            for h in data.get("hits", []):
                results.append({
                    "title": h.get("title", ""),
                    "url": h.get("url", f"https://news.ycombinator.com/item?id={h.get('objectID', '')}"),
                    "snippet": f"score: {h.get('points', 0)} | comments: {h.get('num_comments', 0)} | by: {h.get('author', '')}",
                    "source": "hackernews",
                })
            return results
        except Exception as e:
            logger.warning(f"HackerNews 引擎失败: {e}")
            return []
    return _engine


# ── Stack Overflow 搜索引擎 ───────────────────────────────────────────────────

def _build_stackoverflow_engine(spec: dict[str, Any]) -> Any:
    """Stack Overflow 搜索（Stack Exchange API）"""
    timeout = spec.get("timeout", 8)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = f"https://api.stackexchange.com/2.3/search/advanced?order=desc&sort=relevance&q={up.quote(query)}&site=stackoverflow&pagesize={min(n, 10)}"
        headers = {"User-Agent": "argo-search/1.0", "Accept-Encoding": "gzip"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                import gzip
                raw = resp.read()
                try:
                    data = json.loads(gzip.decompress(raw))
                except Exception:
                    data = json.loads(raw)
            results = []
            for item in data.get("items", []):
                tags = ", ".join(item.get("tags", [])[:3])
                results.append({
                    "title": item.get("title", ""),
                    "url": item.get("link", ""),
                    "snippet": f"score: {item.get('score', 0)} | answers: {item.get('answer_count', 0)} | tags: {tags}",
                    "source": "stackoverflow",
                })
            return results
        except Exception as e:
            logger.warning(f"StackOverflow 引擎失败: {e}")
            return []
    return _engine


# ── GitHub 搜索引擎（按结构化语法切端点）──────────────────────────────────────

# GitHub 搜索与 X 一样支持结构化字段，但不同字段属于不同端点（失配会拿到空结果或噪音）：
#   - 仓库搜索: user:/org:/lang:/in:name/in:description/stars:/topic:  → /search/repositories
#   - issue/PR: repo:/is:issue/is:pr/label:/author:/assignee:/comments:/created:/in:title/in:body
#                                                                       → /search/issues
#   - 代码搜索: in:file/filename:/extension:/path:（需认证）                → /search/code
_GH_REPO_SYNTAX = ("user:", "org:", "lang:", "in:name", "in:description", "stars:", "topic:", "size:", "pushed:")
_GH_ISSUE_SYNTAX = ("repo:", "is:issue", "is:pr", "is:open", "is:closed", "label:", "milestone:",
                    "author:", "assignee:", "comments:", "created:", "updated:", "in:title", "in:body")
_GH_CODE_SYNTAX = ("in:file", "filename:", "extension:", "path:", "in:readme", "in:path")


def _github_endpoint(query: str, has_token: bool) -> str:
    """按查询中的结构化语法选 GitHub 搜索端点。"""
    if any(s in query for s in _GH_CODE_SYNTAX):
        return "code" if has_token else "issues"  # code 需认证；无 token 尽力退到 issues
    if any(s in query for s in _GH_ISSUE_SYNTAX):
        return "issues"
    return "repositories"


def _github_url(endpoint: str, query: str, n: int) -> str:
    q = urllib.parse.quote(query)
    per = min(n, 30)
    base = {
        "repositories": "https://api.github.com/search/repositories",
        "issues": "https://api.github.com/search/issues",
        "code": "https://api.github.com/search/code",
    }[endpoint]
    return f"{base}?q={q}&per_page={per}"


def _gh_repo_result(item: dict) -> dict[str, Any] | None:
    name = item.get("full_name") or item.get("name") or ""
    url = item.get("html_url") or ""
    desc = (item.get("description") or "").strip()
    if not name and not url:
        return None
    stars = item.get("stargazers_count")
    snippet = desc or f"stars: {stars} | language: {item.get('language')}"
    return {
        "title": name or url,
        "url": url,
        "snippet": snippet[:300],
        "source": "github",
        "score": 0.7,
        "published_at": item.get("updated_at"),
        "metadata": {"stars": stars, "language": item.get("language"), "forks": item.get("forks_count")},
    }


def _gh_issue_result(item: dict) -> dict[str, Any] | None:
    title = item.get("title") or ""
    url = item.get("html_url") or ""
    repo_full = (item.get("repository_url") or "").replace("https://api.github.com/repos/", "")
    if not title and not url:
        return None
    state = item.get("state") or ""
    comments = item.get("comments")
    user = (item.get("user") or {}).get("login") or ""
    snippet = f"[{repo_full}] {state} | comments: {comments} | by @{user}" if repo_full else f"{state} | by @{user}"
    return {
        "title": title or url,
        "url": url,
        "snippet": snippet[:300],
        "source": "github",
        "score": 0.7,
        "published_at": item.get("created_at"),
        "metadata": {"repo": repo_full, "state": state, "comments": comments},
    }


def _gh_code_result(item: dict) -> dict[str, Any] | None:
    name = item.get("name") or ""
    url = item.get("html_url") or ""
    repo = (item.get("repository") or {}).get("full_name") or ""
    path = item.get("path") or ""
    if not name and not url:
        return None
    snippets = [(tm.get("fragment") or "").strip() for tm in (item.get("text_matches") or []) if tm.get("fragment")]
    snippet = " / ".join(snippets)[:300] or path
    return {
        "title": f"{repo}:{path or name}",
        "url": url,
        "snippet": snippet,
        "source": "github",
        "score": 0.7,
        "metadata": {"repo": repo, "path": path},
    }


def _build_github_engine(spec: dict[str, Any]) -> Any:
    """GitHub 搜索：按查询结构化语法自动切 repositories / issues / code 端点。

    未认证（无 GITHUB_TOKEN）时可用 repositories / issues；code 端点需认证。
    失配端点会拿到空结果或大段噪音，这里是按语法选对端点的关键修复。
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        endpoint = _github_endpoint(query, bool(token))
        url = _github_url(endpoint, query, n)
        headers = {
            "User-Agent": "argo-search/1.0 (+github)",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"token {token}"
        if endpoint == "code":
            headers["Accept"] = "application/vnd.github.v3.text-match+json"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            logger.warning(f"GitHub {endpoint} 失败 HTTP {e.code}: {e.reason}")
            return []
        except Exception as e:
            logger.warning(f"GitHub {endpoint} 失败: {e}")
            return []

        results = []
        if endpoint == "repositories":
            for item in data.get("items", [])[:n]:
                r = _gh_repo_result(item)
                if r:
                    results.append(r)
        elif endpoint == "issues":
            for item in data.get("items", [])[:n]:
                r = _gh_issue_result(item)
                if r:
                    results.append(r)
        else:
            for item in data.get("items", [])[:n]:
                r = _gh_code_result(item)
                if r:
                    results.append(r)
        return results
    return _engine


# ── Google Scholar 搜索引擎 ───────────────────────────────────────────────────

def _build_google_scholar_engine(spec: dict[str, Any]) -> Any:
    """Google Scholar 搜索（HTTP 页面解析）"""
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = f"https://scholar.google.com/scholar?q={up.quote(query)}&hl=en&as_sdt=0%2C5&num={min(n, 10)}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                html = resp.read().decode("utf-8")
            results = []
            titles = re.findall(r'<h3[^>]*class="[^"]*gs_rt[^"]*"[^>]*>(.*?)</h3>', html, re.DOTALL)
            snippets = re.findall(r'<div[^>]*class="[^"]*gs_rs[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL)
            for i, t in enumerate(titles[:n]):
                title = re.sub(r'<[^>]+>', '', t).strip()
                snippet = re.sub(r'<[^>]+>', '', snippets[i]).strip() if i < len(snippets) else ""
                if title:
                    results.append({
                        "title": title[:100],
                        "url": f"https://scholar.google.com/scholar?q={up.quote(title[:50])}",
                        "snippet": snippet[:200],
                        "source": "google_scholar",
                    })
            return results
        except Exception as e:
            logger.warning(f"Google Scholar 引擎失败: {e}")
            return []
    return _engine


# ── V2EX 搜索引擎 ─────────────────────────────────────────────────────────────

# 拉丁词最小长度：单字符不做匹配。
# 实测证据：查询 "a" 会命中 Apple/astar 等任意含该字母的主题。
# 注意门槛只到 2：词边界（见 _term_hits）已能挡住 "ab"→Avalonia/Wabou
# 这类组合匹配，若把门槛提到 3 会误伤 "ai" 这种有真实语义的双字符词
# （实测 "AI" 查询会返回 0 条）。CJK 词不受此门槛限制。
MIN_LATIN_TERM_LEN = 2


def _build_v2ex_engine(spec: dict[str, Any]) -> Any:
    """V2EX 社区搜索（官方 API + 本地相关性过滤）

    为什么不是「爬 /search 页」：V2EX 的站内搜索需要登录态，未登录访问
    `/search?q=` 会 302 到 `/go/search`——那是「搜索引擎技术研究」**节点页**，
    不是搜索结果页。旧实现用 item_title 正则直接抓该页面，于是把节点热帖
    当成了搜索结果：10 条结果的 url 全部等于查询自身的搜索页地址、
    snippet 恒为硬编码常量，标题则与该节点无关（如查「V2EX 社区」返回
    「装机 配置 预算」）。coverage 仍报 status=ok/returned=10，
    失败伪装成成功，破坏 argo「结果可核验」的证据闭环。

    现方案走官方开放 API（只读、无鉴权、配额 600 次/[窗口]）：
      - /api/topics/hot.json      热门主题
      - /api/topics/latest.json   最新主题
      - /api/replies/show.json    主题回复（按 topic_id）
    API 没有搜索端点，因此策略是「拉候选池 → 在本地按查询词做相关性过滤
    → 按命中强度排序」。这样产出的每条结果都带真实 /t/<id> 链接与真正文，
    可被 fetch 复核。若候选池内无任何条目与查询相关，**诚实返回空列表**
    （调用方据此标记该引擎无结果），而不是回落到伪造占位结果。
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout

        def _fetch_json(path: str, params: dict | None = None):
            u = f"https://www.v2ex.com{path}"
            if params:
                u += "?" + urllib.parse.urlencode(params)
            # 走 argo 统一 GET 出口（HttpClient：UA 轮换 / 重定向跟随 / 429 尊重），
            # 不自造 urllib 请求头——与「新引擎复用 argo HttpClient」纪律一致。
            raw = _http_get_raw(u, {"Accept": "application/json"}, to)
            if not raw:
                return None
            data = json.loads(raw)
            # 官方 API 会返回 {"status":"error","message":...,"rate_limit":{...}}，
            # 例如配额耗尽或参数非法。这类响应不是列表，静默当空会掩盖真实故障。
            if isinstance(data, dict) and data.get("status") == "error":
                rl = data.get("rate_limit") or {}
                logger.warning(
                    "V2EX API 返回错误: %s (rate_limit used=%s quota=%s)",
                    data.get("message"), rl.get("used"), rl.get("quota"),
                )
                return None
            return data

        # ── 候选池构建：节点路由优先，hot/latest 兜底 ──────────────────
        #
        # 第一批只用 hot+latest（20 条全站热帖），与查询无关，长尾查询
        # 必然落空。现引入节点路由（见 v2ex_nodes）：把「全站热帖过滤」
        # 升级为「相关节点内检索」。节点表本地缓存 24h，匹配阶段零 API。
        #
        # 取池策略（成本与召回权衡）：
        #   命中节点 → 并发取 Top-K 节点的帖（K=3，配额占用 3/600）
        #   未命中   → 回落 hot+latest，并如实标注 layer=none
        topics: dict[int, dict] = {}
        routed = {"nodes": [], "confidence": 0.0, "layer": "none", "scores": {}}
        try:
            from v2ex_nodes import pick_nodes
            routed = pick_nodes(query, top_k=3, fetcher=lambda u: _http_get_raw(
                u, {"Accept": "application/json"}, to))
        except Exception as e:
            logger.debug(f"V2EX 节点路由失败，回落 hot/latest: {e}")

        for node_name in routed.get("nodes") or []:
            try:
                for t in _fetch_json("/api/topics/show.json",
                                     {"node_name": node_name, "page": 1}) or []:
                    tid = t.get("id")
                    if tid and tid not in topics:
                        t["_v2ex_node_routed"] = node_name
                        topics[tid] = t
            except Exception as e:
                logger.debug(f"V2EX 节点 {node_name} 拉取失败: {e}")

        # 兜底与补充：无论是否命中节点，都并入 hot/latest
        # （节点帖可能与查询无关的部分互补；去重由 topic id 保证）
        for path in ("/api/topics/hot.json", "/api/topics/latest.json"):
            try:
                for t in _fetch_json(path) or []:
                    tid = t.get("id")
                    if tid and tid not in topics:
                        topics[tid] = t
            except Exception as e:
                logger.debug(f"V2EX {path} 拉取失败: {e}")

        if not topics:
            return []

        q_norm = (query or "").strip().lower()
        terms = [w for w in re.split(r"[\s,，、/]+", q_norm) if w]

        def _term_hits(term: str, blob: str) -> bool:
            """词命中判断：拉丁词要求词边界，CJK 走子串。

            为什么必须区分：纯子串匹配下 "ab" 会命中 Avalonia/Wabou/Workbuddy
            （实测），"a" 会命中 Apple/astar。对拉丁词加 word boundary 后
            "ai" 仍能精确命中独立的 "ai"/"AI"，但不再命中 "Avalonia"；
            CJK 没有词边界概念，"程序员" 这类子串匹配是正确的。
            单字符拉丁词（< MIN_LATIN_TERM_LEN）无边界可依，直接不匹配。
            """
            if not term:
                return False
            if re.search(r"[\u4e00-\u9fff]", term):
                return term in blob           # CJK：子串匹配
            if len(term) < MIN_LATIN_TERM_LEN:
                return False                  # 单字符拉丁词：放弃
            return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", blob) is not None

        def _relevance(t: dict) -> float:
            """命中强度：完整查询命中 > 全部词命中 > 部分词命中。0 表示不相关。

            只在标题与正文里匹配，**不匹配节点名/节点简介**：节点名多为
            「分享发现」「推广」这类通用词，参与匹配会让无关主题靠节点名
            蹭进结果（实测查询「V2EX 社区」曾命中一条推广帖）。
            部分词命中要求覆盖 ≥50% 且查询至少 2 个词，避免单个通用词
            （「社区」「工具」）把大量无关主题拉进来。
            词命中口径见 _term_hits（拉丁词边界 + CJK 子串 + 短词门槛）。
            """
            if not q_norm:
                return 0.0
            blob = f"{(t.get('title') or '').lower()} {(t.get('content') or '').lower()}"
            if _term_hits(q_norm, blob):
                return 3.0
            if not terms:
                return 0.0
            hit = sum(1 for w in terms if _term_hits(w, blob))
            if hit == 0:
                return 0.0
            if hit == len(terms):
                return 2.0
            # 部分命中：查询词数 ≥2 且覆盖率 ≥50% 才算相关
            if len(terms) >= 2 and hit / len(terms) >= 0.5:
                return 1.0 * (hit / len(terms))
            return 0.0

        scored = []
        for t in topics.values():
            rel = _relevance(t)
            if rel <= 0:
                continue
            scored.append((rel, t))

        # 相关度优先，同级按回复数（社区热度）降序
        scored.sort(key=lambda x: (-x[0], -(x[1].get("replies") or 0)))

        results = []
        for rel, t in scored[:n]:
            node = t.get("node") or {}
            member = t.get("member") or {}
            snippet = (t.get("content") or "").strip()
            if not snippet:
                snippet = re.sub(r"<[^>]+>", "", t.get("content_rendered") or "").strip()
            results.append({
                "title": (t.get("title") or "")[:120],
                "url": t.get("url") or f"https://www.v2ex.com/t/{t.get('id')}",
                "snippet": snippet[:300],
                "source": "v2ex",
                "published_at": t.get("created"),
                "social_meta": {
                    "platform": "v2ex",
                    "content_type": "topic",
                    "node": node.get("title") or "",
                    "node_name": node.get("name") or "",
                    "author": member.get("username") or "",
                    "replies": t.get("replies"),
                    "url_verifiable": True,
                    # 口径透明：结果来自官方 API 的候选池 + 本地相关性过滤，
                    # 非站内全文搜索。冷门/长尾查询命中率天然偏低，
                    # 命中 0 条是「池内无相关主题」，不等于「V2EX 上没有」。
                    "retrieval_mode": "node_routed_pool" if (
                        routed.get("nodes")) else "candidate_pool_filter",
                    "pool_size": len(topics),
                    # 节点路由可观测：命中哪些节点、置信层、节点路由本身
                    # 的置信度。layer=none 表示无匹配节点，已回落 hot/latest。
                    "routed_nodes": routed.get("nodes") or [],
                    "route_layer": routed.get("layer"),
                    "route_confidence": routed.get("confidence"),
                    "from_routed_node": bool(t.get("_v2ex_node_routed")),
                },
            })
        return results
    return _engine


