#!/usr/bin/env python3
"""
fetch_v3.py — 抓取降级链（零外部依赖，纯 stdlib + 系统 Chrome）

吸收 Hound 的页面交互能力，但不引入 Playwright/Patchright 依赖：
  第零级：AI 友好变体探测（{url}.md 直出 + 站点根 /llms.txt，诚实身份；
          ARGO_FETCH_MD_VARIANT=0 关闭）
  第一级：增强 HTTP（UA 轮换 + Cookie 积累 + 重试弹性）
  第一级A2：移动端 UA 分支（客户端形态分流型反爬；门控站单次直连 +
            per-host 身份记忆；ARGO_FETCH_MOBILE=0 关闭）
  第一级B：TLS 指纹伪造（curl_cffi impersonate，多指纹轮换，免起浏览器）
  第一级C：r.jina.ai 阅读器（keyless 免费层，远端 JS 渲染转 markdown；
            仅公网 URL，ARGO_FETCH_JINA=0 关闭）
  第二级：Chrome CDP 驱动（页面交互/JS 渲染/CF 绕过）
  第三级：内容质量评估（content_ok/page_type/quality_score）

对比 fetch_v2：
- fetch_v2: urllib + Hound subprocess（需要 master_fetch 包）
- fetch_v3: http_client(stdlib) + curl_cffi(可选) + chrome_cdp(stdlib+系统Chrome) → 完全自主

TLS 指纹伪造层（第一级B）：
  指纹检测型反爬（Cloudflare 等）凭 TLS ClientHello 判 bot，urllib 直接 403。
  curl_cffi 可逐字节模拟 Chrome/Safari/Firefox 指纹，免起浏览器即可通过。
  开关：ARGO_FETCH_IMPERSONATE=0 关闭，默认开启。

用法：
    from fetch_v3 import fetch_v3, fetch_page_v3
    result = fetch_v3("https://example.com", actions=[{"click": "#btn"}])
    # 兼容旧接口
    result = fetch_page_v3("https://example.com", max_chars=3000)
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import time
import traceback
import urllib.parse
import uuid
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# 确保能导入同目录模块
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

# 渲染层（第二级A）：TinyFish 直连渲染，独立模块以保持本文件聚焦主链编排
import fetch_render_tinyfish as _render_tinyfish

# 质量信号层（第三级）：来源分类 / 页面类型 / 质量分 / 内容安全
import fetch_quality as _quality

# 本地状态目录单一真源（env ARGO_STATE_DIR → config cache.db_path 父目录 → 旧路径）
import argo_paths as _paths
from net_proxy import open_url  # 出口调度唯一入口（issue #13 同类修复）
from engine_env import env_flag  # 布尔开关统一判断（见 env_flag 的说明）


# ─── 内容提取器（复用 fetch.py 的逻辑，增强版）──────────────────────────────

_SKIP_TAGS = {"script", "style", "nav", "header", "footer", "aside", "noscript", "iframe"}

class ContentExtractor(HTMLParser):
    """从 HTML 提取正文文本（基于文本密度排序）。"""

    def __init__(self):
        super().__init__()
        self._in_skip = 0
        self._blocks: list[tuple[float, str]] = []
        self._current: list[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._in_skip += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._in_skip = max(0, self._in_skip - 1)
        if tag == "title":
            self._in_title = False
        if tag in ("p", "div", "article", "section", "li", "h1", "h2", "h3", "h4", "td", "blockquote"):
            text = "".join(self._current).strip()
            if len(text) > 20:
                density = len(text.replace(" ", "")) / max(len(text), 1)
                self._blocks.append((density, text))
            self._current = []

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_skip == 0:
            self._current.append(data)


def extract_content(html: str, max_chars: int = 8000) -> tuple[str, str]:
    """从 HTML 提取正文和标题。

    P0 增强：readability 密度法为主（链接密度惩罚 + 标签权重 + 容器归并，
    保持文档顺序）；返回空时回退旧密度排序实现（链接列表页等无正文场景
    不至于丢掉旧行为兜底的结果）。
    """
    try:
        from readability_extract import extract_readability
        content, title = extract_readability(html, max_chars=max_chars)
        if content.strip():
            return content[:max_chars], title.strip()
    except Exception:
        pass
    ext = ContentExtractor()
    try:
        ext.feed(html)
    except Exception:
        pass
    ext._blocks.sort(key=lambda x: x[0], reverse=True)
    content = "\n\n".join(text for _, text in ext._blocks[:10])
    return content[:max_chars], ext.title.strip()


# ─── 降级检测 ────────────────────────────────────────────────────────────────

_CF_MARKERS = re.compile(
    r"checking your browser|cf-browser-verification|cf_chl_opt|ray id|"
    r"challenge-platform|please verify you are a human|cloudflare",
    re.IGNORECASE,
)

_JS_MARKERS = re.compile(
    r"enable javascript|javascript is required|javascript to run this app|"
    r"you need to enable javascript|requires javascript",
    re.IGNORECASE,
)

_EMPTY_SHELL = re.compile(
    r"^[\s\n]*<html[^>]*>[\s\n]*<head>.*?</head>[\s\n]*<body>[\s\n]*</body>[\s\n]*</html>[\s\n]*$",
    re.IGNORECASE | re.DOTALL,
)


def _needs_browser(result: dict) -> bool:
    """判断是否需要升级到浏览器抓取。"""
    if not result.get("success"):
        return True
    content = result.get("content", "")
    html = result.get("html", "")
    # 内容过少
    if not content or len(content.strip()) < 100:
        return True
    # 空壳 HTML
    if html and _EMPTY_SHELL.search(html):
        return True
    # CF 挑战
    if _CF_MARKERS.search(html) or _CF_MARKERS.search(content):
        return True
    # JS 要求
    if len(content) < 300 and _JS_MARKERS.search(html):
        return True
    return False


def _impersonate_enabled() -> bool:
    """TLS 指纹伪造层开关：ARGO_FETCH_IMPERSONATE=0 关闭，默认开启。"""
    return env_flag("ARGO_FETCH_IMPERSONATE")


# ─── 第零级：AI 友好变体探测（{url}.md 直出）────────────────────────────────

_MD_SKIP_EXTS = {".html", ".htm", ".php", ".jsp", ".asp", ".aspx", ".shtml",
                 ".pdf", ".xml", ".json", ".png", ".jpg", ".jpeg", ".gif",
                 ".webp", ".svg", ".css", ".js", ".zip", ".gz", ".mp4", ".mp3"}

_MD_SNIFF_HTML = re.compile(r"<\s*(!doctype|html|head|body|div|script)\b",
                            re.IGNORECASE)


def _md_variant_enabled() -> bool:
    """.md 变体探测开关：ARGO_FETCH_MD_VARIANT=0 关闭，默认开启。"""
    return env_flag("ARGO_FETCH_MD_VARIANT")


def _tinyfish_enabled() -> bool:
    """tinyfish 免费渲染层开关（直连 api.fetch.tinyfish.ai，需 TINYFISH_API_KEY）：
    ARGO_FETCH_TINYFISH=0 关闭，默认开启。

    仅在http/指纹均失败或命中 JS/反爬壳时启用，结果失败自动回退，
    不改变正常抓取路径。

    实现委托给 fetch_render_tinyfish（渲染层已独立成模块）。
    """
    return _render_tinyfish.enabled()


def _md_variant_url(url: str) -> str | None:
    """返回可探测的 .md 变体 URL；不适合探测时返回 None。

    只对无扩展名路径（文档站页面形态）追加 .md；站点根（/）不放 .md 探测
    （根级 AI 友好文件是 llms.txt，由 _llms_txt_url 负责），已带扩展名或
    带查询串的动态地址跳过，避免无意义请求。
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    path = parsed.path or "/"
    ext = os.path.splitext(path)[1].lower()
    if ext or parsed.query or path in ("", "/"):
        return None
    return url.rstrip("/") + ".md"


def _llms_txt_url(url: str) -> str | None:
    """返回站点根 /llms.txt 候选；仅对站点根路径（文档站门户形态）探测。

    llms.txt（2024 起、规范 v1.8.0）是站点自述的 markdown 索引：H1 站名 +
    分节资源清单，头部开发者文档站采用中。非根路径不探测——该文件只存在于
    站点根，对深层页面探测是无意义请求。
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    if parsed.path not in ("", "/") or parsed.query:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/llms.txt"


def _ai_variant_candidates(url: str) -> list[tuple[str, str]]:
    """第零级探测候选清单（按优先序）：页面 .md 直出 → 站点根 llms.txt。"""
    out: list[tuple[str, str]] = []
    md = _md_variant_url(url)
    if md:
        out.append((md, "md_variant"))
    llms = _llms_txt_url(url)
    if llms:
        out.append((llms, "llms_txt"))
    return out


def _looks_like_markdown(text: str) -> bool:
    """嗅探响应体是否为可用 Markdown 正文（排除伪装成 .md 的 HTML 壳）。"""
    if not text or len(text.strip()) < 120:
        return False
    if _MD_SNIFF_HTML.search(text[:2048]):
        return False
    return True


def _md_variant_fetch(url: str, max_chars: int = 8000,
                      timeout: float = 8.0) -> dict | None:
    """探测 AI 友好变体（{url}.md 直出 / 站点根 llms.txt）。命中返回结果，未命中 None。

    背景：头部开发者文档站自发提供 .md 直出与 llms.txt，对 Agent 返回干净
    正文（诚实身份即可获取）。探测只多一两个 GET，命中即跳过整个反爬降级链；
    未命中静默放弃，走原有链路。返回的 result.url 保持为原始请求 URL
    （llms.txt 描述的是站点，不是探测端点自身）。
    """
    candidates = _ai_variant_candidates(url)
    if not candidates:
        return None
    for probe_url, kind in candidates:
        try:
            from http_client import HttpClient
            client = HttpClient(timeout=min(timeout, 5.0), max_retries=0,
                                jitter=False)
            resp = client.get(probe_url, extra_headers={
                "User-Agent": "argo-fetch-v3/1.0 (+local-research; md-variant)",
                "Accept": "text/markdown, text/plain;q=0.9, */*;q=0.8",
            })
        except Exception:
            continue
        if resp.get("status", 0) != 200 or not resp.get("text"):
            continue
        headers = resp.get("headers") or {}
        ctype = ""
        for k, v in headers.items():
            if str(k).lower() == "content-type":
                ctype = str(v).lower()
                break
        if "html" in ctype:
            continue
        text = resp["text"]
        if not _looks_like_markdown(text):
            continue
        result = _make_result(url, "", max_chars, kind)
        result["content"] = text[:max_chars]
        result["length"] = len(result["content"])
        result["title"] = ""
        m = re.match(r"^#\s+(.+)$", result["content"].lstrip(), re.MULTILINE)
        if m:
            result["title"] = m.group(1).strip()[:200]
        return result
    return None


# ─── 第一级A2：移动端 UA 分支（客户端形态分流）──────────────────────────────

_MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
              "Mobile/15E148 Safari/604.1")


def _mobile_branch_enabled() -> bool:
    """移动端 UA 分支开关：ARGO_FETCH_MOBILE=0 关闭，默认开启。"""
    return env_flag("ARGO_FETCH_MOBILE")


# 客户端形态分流已知站点：这些站对真机 UA 直接返回 SSR 数据，而桌面 UA 首发
# 会触发风控、连坐紧随其后的移动端请求（2026-08 实测冷却 ≥8s），因此必须
# 移动优先而非失败后重试。可用 ARGO_MOBILE_FIRST_HOSTS 追加（逗号分隔）。
_MOBILE_FIRST_HOSTS = ("douyin.com", "iesdouyin.com")


def _mobile_first_host(url: str) -> bool:
    """URL 是否命中「移动优先」主机清单。"""
    extra = [h.strip().lower() for h in os.environ.get(
        "ARGO_MOBILE_FIRST_HOSTS", "").split(",") if h.strip()]
    host = (urlparse(url).hostname or "").lower()
    for marker in list(_MOBILE_FIRST_HOSTS) + extra:
        if host == marker or host.endswith("." + marker):
            return True
    return False


# ─── 身份记忆（per-host 成功档位，跨进程小文件）─────────────────────────────
#
# 首次访问未知分流站：桌面失败 → 移动成功（2 次触碰）；记忆生效后（TTL 24h）
# 直接移动首发（1 次触碰）。实测依据：抖音类站点按 IP 递进限速，请求节奏比
# 身份选择重要一个数量级，省掉的每次试错都是真实的封禁风险。
_IDENTITY_TTL = 86400
_IDENTITY_PATH = os.environ.get(
    "ARGO_IDENTITY_MEMORY",
    str(_paths.state_path("fetch-identity.json")))
_identity_mem: dict[str, float] = {}
_identity_loaded = False


def _identity_load() -> None:
    global _identity_loaded, _identity_mem
    if _identity_loaded:
        return
    _identity_loaded = True
    try:
        with open(_IDENTITY_PATH) as f:
            raw = json.load(f)
        now = time.time()
        mem: dict[str, float] = {}
        for h, t in raw.items():
            try:
                exp = float(t)
                if exp > now:
                    mem[str(h)] = exp
            except (TypeError, ValueError):
                continue  # 单条脏数据不拖垮整表（旧版本/手改字段）
        _identity_mem = mem
    except Exception:
        _identity_mem = {}


def _identity_remember_mobile(host: str) -> None:
    """记录「该 host 移动端身份成功过」，原子写小文件。失败静默（纯增益层）。"""
    if not host:
        return
    _identity_load()
    _identity_mem[host] = time.time() + _IDENTITY_TTL
    try:
        # 原子写走 argo_paths 单一真源（唯一 tmp 名）；旧实现固定 `.tmp` 名，
        # 并发抓取进程会互相搬走临时文件，身份记忆静默丢失。
        _paths.atomic_write_json(Path(_IDENTITY_PATH), _identity_mem, indent=None)
    except Exception:
        pass


def _identity_is_mobile(host: str) -> bool:
    """该 host 近 24h 内移动身份是否成功过。"""
    if not host:
        return False
    _identity_load()
    return _identity_mem.get(host, 0) > time.time()


def _mobile_http_fetch(url: str, max_chars: int = 8000,
                       timeout: float = 8.0) -> dict:
    """移动端 UA 抓取：客户端形态分流型站点对真机 UA 返回 SSR 数据。

    实测（2026-08）：抖音 iesdouyin 分享页对 iPhone UA 返回含 _ROUTER_DATA
    的服务端渲染数据页；桌面/AI bot UA 一律收到 acrawler 风控 JS 壳。
    该类分流与 TLS 指纹无关，stdlib 即可通过，放在 TLS 伪造层之前。
    """
    try:
        from url_safety import check_url
        ok, reason = check_url(url)
        if not ok:
            return _make_result(url, "", 0, "http_mobile", ok=False,
                                error=f"URL 被 SSRF 防护拦截: {reason}")
    except ImportError:
        pass
    try:
        from http_client import HttpClient
        client = HttpClient(timeout=min(timeout, 5.0), max_retries=1,
                            jitter=False)
        resp = client.get(url, extra_headers={"User-Agent": _MOBILE_UA})
    except ImportError:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": _MOBILE_UA})
        try:
            with open_url(req, timeout=timeout) as r:
                text = r.read().decode("utf-8", errors="replace")
                return _make_result(url, text, max_chars, "http_mobile")
        except Exception as e:
            return _make_result(url, "", 0, "http_mobile", ok=False,
                                error=str(e)[:100])
    if resp.get("status", 0) >= 400:
        result = _make_result(url, "", 0, "http_mobile", ok=False,
                              error=f"HTTP {resp.get('status')}")
        _mark_stop_signal(result, resp)
        return result
    if not resp.get("text"):
        return _make_result(url, "", 0, "http_mobile", ok=False,
                            error="empty response")
    result = _make_result(url, resp["text"], max_chars, "http_mobile")
    _mark_stop_signal(result, resp)
    return result


def _mark_stop_signal(result: dict, resp: dict) -> dict:
    """把 429/503 明确停止信号记录到结果，供主链门禁使用。

    429（速率限制）与 503（服务过载）是服务器明确的「请停止」信号，
    与请求方式（UA/TLS 指纹/浏览器）无关。收到后不应升级重链，
    否则等于无视服务器指示、持续放大目标站点负载。
    """
    status = resp.get("status", 0)
    result["status"] = status
    if status in (429, 503):
        result["stop_signal"] = True
    return result


# ─── 第一级：增强 HTTP ───────────────────────────────────────────────────────

def _http_fetch(url: str, max_chars: int = 8000, timeout: float = 8.0) -> dict:
    """使用 http_client（UA 轮换 + Cookie 积累）抓取。"""
    try:
        from url_safety import check_url
        ok, reason = check_url(url)
        if not ok:
            return _make_result(url, "", 0, "http", ok=False,
                                error=f"URL 被 SSRF 防护拦截: {reason}")
    except ImportError:
        pass
    try:
        from http_client import HttpClient
        client = HttpClient(timeout=timeout, max_retries=1, jitter=False)
        resp = client.get(url)
    except ImportError:
        # fallback 到 urllib
        import urllib.request
        req = urllib.request.Request(url, headers={
            "User-Agent": "argo-fetch-v3/1.0 (+local-research)",
        })
        try:
            with open_url(req, timeout=timeout) as r:
                text = r.read().decode("utf-8", errors="replace")
                return _make_result(url, text, max_chars, "http")
        except Exception as e:
            return _make_result(url, "", 0, "http", ok=False, error=str(e)[:100])

    if resp.get("status", 0) >= 400:
        result = _make_result(url, "", 0, "http", ok=False,
                              error=f"HTTP {resp.get('status')}")
        _mark_stop_signal(result, resp)
        return result
    if not resp.get("text"):
        return _make_result(url, "", 0, "http", ok=False, error="empty response")

    return _make_result(url, resp["text"], max_chars, "http")


def _tls_spoof_fetch(url: str, max_chars: int = 8000, timeout: float = 8.0) -> dict:
    """TLS 指纹伪造层：curl_cffi impersonate 多指纹轮换抓取。

    针对指纹检测型反爬（Cloudflare 等直接按 TLS ClientHello 判 bot），
    urllib/curl 原生指纹与真实浏览器不同会被 403。本层用 curl_cffi
    逐字节模拟 Chrome/Safari/Firefox 指纹，免起浏览器即可通过。

    指纹轮换顺序：chrome → safari → firefox（safari 对部分站点更友好）。
    """
    try:
        from url_safety import check_url
        ok, reason = check_url(url)
        if not ok:
            return _make_result(url, "", 0, "tls_spoof", ok=False,
                                error=f"URL 被 SSRF 防护拦截: {reason}")
    except ImportError:
        pass

    try:
        from http_client import HttpClient
        client = HttpClient(timeout=timeout, max_retries=1, jitter=False)
        resp = client.get_impersonated(url, timeout=timeout)
    except ImportError:
        return _make_result(url, "", 0, "tls_spoof", ok=False,
                            error="http_client not available")

    if resp.get("status", 0) >= 400:
        result = _make_result(url, "", 0, "tls_spoof", ok=False,
                              error=f"HTTP {resp.get('status')}")
        _mark_stop_signal(result, resp)
        return result
    if not resp.get("text"):
        return _make_result(url, "", 0, "tls_spoof", ok=False,
                            error=resp.get("error", "empty response"))

    result = _make_result(url, resp["text"], max_chars, "tls_spoof")
    result["impersonate"] = resp.get("impersonate", "")
    return result


def _wayback_fetch(url: str, max_chars: int = 8000, timeout: float = 12.0) -> dict:
    """Wayback Machine 快照回退：CDX API 查最新快照 → 抓取。

    用于 HTTP 失败 / 空内容 / 疑似被删页面的兜底，返回统一输出格式。
    """
    try:
        from http_client import HttpClient
        client = HttpClient(timeout=timeout, max_retries=1, jitter=False)
        # CDX 查询最新快照
        cdx_url = (
            "https://web.archive.org/cdx/search/cdx"
            f"?url={urllib.parse.quote(url, safe='')}"
            "&output=json&limit=1&sort=reverse"
        )
        resp = client.get(cdx_url)
        if not resp.get("text"):
            return _make_result(url, "", 0, "wayback", ok=False,
                                error="wayback cdx empty")
        data = json.loads(resp["text"])
        if not data or len(data) < 2:
            return _make_result(url, "", 0, "wayback", ok=False,
                                error="wayback no snapshot")
        timestamp = data[1][1]
        snapshot_url = f"https://web.archive.org/web/{timestamp}/{url}"
        snap = client.get(snapshot_url)
        if not snap.get("text"):
            return _make_result(url, "", 0, "wayback", ok=False,
                                error="wayback snapshot empty")
        result = _make_result(url, snap["text"], max_chars, "wayback")
        result["snapshot_url"] = snapshot_url
        result["snapshot_ts"] = timestamp
        return result
    except Exception as e:
        return _make_result(url, "", 0, "wayback", ok=False,
                            error=f"wayback error: {str(e)[:100]}")


def _make_result(url: str, html: str, max_chars: int,
                 method: str, ok: bool = True, error: str | None = None,
                 title: str = "") -> dict:
    """构建统一输出格式。"""
    content, extracted_title = extract_content(html, max_chars) if ok and html else ("", "")
    if not title:
        title = extracted_title
    return {
        "url": url,
        "content": content,
        "html": html[:max_chars * 2] if html else "",
        "title": title,
        "length": len(content),
        "success": ok,
        "error": error,
        "fetch_method": method,
    }


# ─── 第一级C：r.jina.ai 阅读器（keyless 免费层，远端 JS 渲染转 markdown）─────

_JINA_PRIVATE_HOST = re.compile(
    r"^(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.|0\.)")


def _jina_enabled() -> bool:
    """r.jina.ai 阅读器级开关：ARGO_FETCH_JINA=0 关闭，默认开启。"""
    return env_flag("ARGO_FETCH_JINA")


def _is_public_host(host: str) -> bool:
    """r.jina.ai 是第三方代理：只把公网 URL 交给它。

    内网/本机/裸 IP 一律跳过——把内部地址送出第三方 = 拓扑泄露；
    域名形 IP（全数字）与含冒号的 IPv6 字面量同判为裸地址。
    """
    if not host:
        return False
    if host == "localhost" or host.endswith((".local", ".internal", ".lan")):
        return False
    if _JINA_PRIVATE_HOST.match(host):
        return False
    if host.replace(".", "").isdigit() or ":" in host:
        return False
    return "." in host


def _jina_reader_fetch(url: str, max_chars: int = 8000,
                       timeout: float = 8.0) -> dict | None:
    """r.jina.ai 阅读器（keyless 免费层）。

    定位：HTTP/TLS 直连全败后的免浏览器快速路径——jina 在远端渲染并返回
    markdown，命中则免去 CDP 冷启动。免费层有速率限制，失败/429 返回 None
    静默放行走 Wayback/浏览器。调用方须先过 _is_public_host（第三方代理边界）。
    """
    try:
        from http_client import HttpClient
        client = HttpClient(timeout=min(timeout, 10.0), max_retries=0,
                            jitter=False)
        resp = client.get(f"https://r.jina.ai/{url}", extra_headers={
            "User-Agent": "argo-fetch-v3/1.0 (+local-research; jina-reader)",
            "Accept": "text/plain",
        })
    except Exception:
        return None
    if resp.get("status", 0) != 200 or not resp.get("text"):
        return None
    raw = resp["text"]
    title = ""
    m = re.search(r"^Title:\s*(.+)$", raw[:2000], re.MULTILINE)
    if m:
        title = m.group(1).strip()[:200]
    if "Markdown Content:" in raw:
        raw = raw.split("Markdown Content:", 1)[1].lstrip()
    if not _looks_like_markdown(raw):
        return None
    result = _make_result(url, "", max_chars, "jina_reader")
    result["content"] = raw[:max_chars]
    result["length"] = len(result["content"])
    result["title"] = title
    return result


# ─── 第一级D：Parallel 免费 MCP web_fetch（keyless 云端渲染+提取）────────────

_PARALLEL_MCP_URL = "https://search.parallel.ai/mcp"


def _parallel_mcp_enabled() -> bool:
    return env_flag("ARGO_FETCH_PARALLEL")


def _parallel_session_id() -> str:
    """免费层限流按 session_id 关联：一次生成、持久复用（官方建议）。

    状态目录不可写时退化为随机 id——仍唯一但不稳定，只损失限流关联的
    连续性，不影响功能。
    """
    try:
        from argo_paths import state_path
        p = state_path("parallel_session_id.txt")
        if p.exists() and p.read_text(encoding="utf-8").strip():
            return p.read_text(encoding="utf-8").strip()
        sid = uuid.uuid4().hex
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(sid, encoding="utf-8")
        return sid
    except Exception:
        return uuid.uuid4().hex


def _parallel_mcp_fetch(url: str, max_chars: int = 8000,
                        timeout: float = 25.0) -> dict:
    """Parallel 免费 MCP web_fetch（keyless，full_content markdown）。

    定位：jina 同级的免浏览器快速路径——云端 JS 渲染 + 正文提取，
    full_content=true 拿整页 markdown（官方警告长文可达数万 token，
    故截到 max_chars）。无账号无 key（2026-09-14 实测无状态直调成立），
    免费层限流按 session_id 关联。失败静默返回 success=False 放行后级。
    调用方须先过 _is_public_host（第三方代理边界）。
    """
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "web_fetch", "arguments": {
                "urls": [url],
                "objective": "Extract the main page content",
                "full_content": True,
                "session_id": _parallel_session_id(),
            }}}
    try:
        from http_client import HttpClient
        client = HttpClient(timeout=min(timeout, 25.0), max_retries=0,
                            jitter=False)
        resp = client.post(_PARALLEL_MCP_URL, body=body, extra_headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        })
    except Exception as e:
        return {"url": url, "title": "", "content": "", "html": "", "length": 0,
                "success": False, "error": f"parallel_mcp: {e}",
                "fetch_method": "parallel_mcp"}
    if resp.get("status", 0) != 200 or not resp.get("text"):
        return {"url": url, "title": "", "content": "", "html": "", "length": 0,
                "success": False, "error": f"parallel_mcp: HTTP {resp.get('status')}",
                "fetch_method": "parallel_mcp"}
    try:
        data = json.loads(resp["text"])
    except (ValueError, TypeError):
        return {"url": url, "title": "", "content": "", "html": "", "length": 0,
                "success": False, "error": "parallel_mcp: 响应非 JSON",
                "fetch_method": "parallel_mcp"}
    # MCP 错误显式记录（fetch 链语义：失败放行后级，但 error 留痕可归因）
    r = data.get("result") or {}
    mcp_err = None
    if r.get("isError"):
        blk = (r.get("content") or [{}])
        mcp_err = (blk[0].get("text", "")[:200] if isinstance(blk[0], dict)
                   else str(blk[0])[:200])
    elif isinstance(data.get("error"), dict):
        mcp_err = str(data["error"].get("message") or data["error"])[:200]
    inner: dict = {}
    for block in (r.get("content") or []):
        text = block.get("text", "") if isinstance(block, dict) else str(block)
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict) and parsed.get("results"):
            inner = parsed
            break
    items = inner.get("results") or []
    if not items:
        return {"url": url, "title": "", "content": "", "html": "", "length": 0,
                "success": False,
                "error": f"parallel_mcp: {mcp_err or '上游无结果'}",
                "fetch_method": "parallel_mcp"}
    item = items[0]
    # full_content=true → 整页 markdown；缺省（上游变更/未开启）退回 excerpts 拼接
    raw = str(item.get("full_content") or "") or "\n\n".join(
        str(e) for e in (item.get("excerpts") or []))
    result = _make_result(url, "", max_chars, "parallel_mcp")
    result["content"] = raw[:max_chars]
    result["length"] = len(result["content"])
    result["title"] = str(item.get("title") or "")[:200]
    if mcp_err:
        result["parallel_mcp_note"] = mcp_err
    return result


# ─── 第二级A：tinyfish 直连渲染（Markdown 直出，含 JS 执行）─────────────
# 实现见 fetch_render_tinyfish（独立模块）：markdown-only 渲染，
# 不产 raw html，故 need_html 场景由主链跳过本层。

def _tinyfish_fetch(url: str, max_chars: int = 8000, timeout: float = 8.0) -> dict:
    """委托给 fetch_render_tinyfish.fetch（渲染层已拆为独立模块）。"""
    return _render_tinyfish.fetch(url, max_chars=max_chars, timeout=timeout)


# ─── 第二级：Chrome CDP 浏览器 ───────────────────────────────────────────────

def _browser_fetch(url: str, max_chars: int = 8000, timeout: float = 15.0,
                   actions: list[dict] | None = None) -> dict:
    """使用 Chrome CDP 驱动抓取（支持页面交互）。"""
    try:
        from chrome_cdp import ChromeCDP
    except ImportError:
        return _make_result(url, "", 0, "browser", ok=False,
                            error="chrome_cdp not available")

    try:
        cdp = ChromeCDP(auto_start=True)
    except Exception as e:
        return _make_result(url, "", 0, "browser", ok=False,
                            error=f"Chrome failed to start: {str(e)[:100]}")

    try:
        # 导航
        cdp.navigate(url, wait_until="networkidle")

        # 执行页面交互序列（Hound actions 等价能力）
        if actions:
            cdp.execute_actions(actions)

        # 提取内容
        html = cdp.get_html()
        text = cdp.get_text()
        title = cdp.get_title()

        return {
            "url": url,
            "content": text[:max_chars] if text else "",
            "html": html[:max_chars * 2] if html else "",
            "title": title or "",
            "length": len(text) if text else 0,
            "success": bool(text),
            "error": None if text else "empty content",
            "fetch_method": "chrome_cdp",
        }
    except Exception as e:
        return _make_result(url, "", 0, "browser", ok=False,
                            error=f"CDP error: {str(e)[:100]}")
    finally:
        try:
            cdp.stop()
        except Exception:
            pass


# ─── 第三级：质量评估 ─────────────────────────────────────────────────────────
# 实现见 fetch_quality（独立模块）：来源分类 / 页面类型 / 质量分 / 内容安全。

def _assess_quality(result: dict) -> dict:
    """计算内容质量信号（委托 fetch_quality.assess）。"""
    return _quality.assess(result)


# 兼容别名：存量测试与调用方直接引用这些私有名，委托到 fetch_quality 保留。
_detect_page_type = _quality._detect_page_type
_compute_quality = _quality._compute_quality
_classify_domain = _quality._classify_domain


# ─── 主入口 ──────────────────────────────────────────────────────────────────

def _optimize_url(url: str) -> str:
    """URL 优化：Reddit 重写、追踪参数清理等。

    - reddit.com → old.reddit.com（7× 更小、无 JS 渲染要求）
    - 清理常见追踪参数（utm_source, fbclid, gclid 等）
    """
    parsed = urlparse(url)
    host = parsed.netloc.lower()

    # Reddit 优化
    if host in ("reddit.com", "www.reddit.com", "new.reddit.com"):
        # 重写为 old.reddit（纯 HTML，无需 JS，体积更小）
        url = url.replace("://www.reddit.com", "://old.reddit.com")
        url = url.replace("://reddit.com", "://old.reddit.com")
        url = url.replace("://new.reddit.com", "://old.reddit.com")

    # 清理追踪参数
    tracking_params = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                       "utm_content", "fbclid", "gclid", "ref", "ref_src"}
    qs = urllib.parse.parse_qs(parsed.query)
    filtered = {k: v for k, v in qs.items() if k.lower() not in tracking_params}
    if len(filtered) < len(qs):
        new_qs = urllib.parse.urlencode(filtered, doseq=True)
        parsed = parsed._replace(query=new_qs)
        url = urllib.parse.urlunparse(parsed)

    return url


def fetch_v3(url: str, max_chars: int = 8000, timeout: float = 8.0,
             use_browser_fallback: bool = True,
             actions: list[dict] | None = None,
             force_browser: bool = False,
             skip_cache: bool = False,
             need_html: bool = False) -> dict:
    """多级抓取降级链主函数（逐级升级，受全局 deadline 约束）。

    执行顺序：
      第零级：{url}.md 变体 + 站点根 /llms.txt 探测（AI 友好直出，命中即跳过整条反爬链）
      第一级：增强 HTTP（UA 轮换 + Cookie 积累）；客户端形态分流型站点移动 UA 首发
      第一级B：TLS 指纹伪造（curl_cffi impersonate，指纹检测型反爬）
      第一级C：r.jina.ai 阅读器（keyless 免费层，仅公网 URL，markdown-only）
      第一级D：Parallel 免费 MCP web_fetch（keyless 免费层，仅公网 URL，markdown-only）
      第二级A：tinyfish 直连渲染（markdown-only，需 TINYFISH_API_KEY；need_html 或开关关闭时跳过）
      第二级B：Wayback 快照 + Chrome CDP 浏览器（自动降级或 actions 触发）
      第三级：质量评估（content_ok/page_type/quality_score）

    全局 deadline：单次 fetch_v3 总耗时上限 = ARGO_FETCH_DEADLINE_S（默认 60，
    可设 0 关闭）。降级是「延迟换成功率」的交易，延迟必须有一等公民约束——
    逐级独立超时的加法无上限（8+8+8+12+8+15≈59s+），会击穿 MCP 客户端
    工具超时。每级升级前检查剩余预算，耗尽即停链返回当前最优结果
    （失败结果 + deadline_exhausted 标记），不再无限叠加。

    URL 级缓存：无 actions 的成功结果写入 SearchCache（L1+L2）。
    need_html：调用方需要原始 HTML（如爬取提取链接）时置 True，跳过 tinyfish
    （仅产 markdown 无 raw html），避免降级链在爬取场景行为漂移。
    """
    try:
        from url_safety import check_url
        ok, reason = check_url(url)
        if not ok:
            return {"url": url, "title": "", "content": "",
                    "html": "", "length": 0, "success": False,
                    "error": f"URL 被 SSRF 防护拦截: {reason}",
                    "fetch_method": "blocked"}
    except ImportError:
        pass

    # URL 优化（Reddit 重写、追踪参数清理）
    url = _optimize_url(url)

    # robots.txt 尊重（合规门禁）：被禁路径直接拒绝，抓取失败放行
    try:
        from robots_guard import robots_blocked
        if robots_blocked(url, timeout=min(timeout, 5.0)):
            result = _make_result(url, "", 0, "robots_blocked", ok=False,
                                  error="robots.txt 禁止抓取")
            result = _assess_quality(result)
            result["cached"] = False
            return result
    except ImportError:
        pass

    # 有 actions → 强制浏览器模式，且不读缓存
    if actions:
        force_browser = True
        skip_cache = True

    # 读 URL 缓存
    if not skip_cache and not force_browser:
        try:
            from cache import SearchCache
            hit = SearchCache().get_fetch(url)
            if hit and hit.get("success"):
                # 缓存内容可能比本次 max_chars 更长 → 截断
                out = {k: v for k, v in hit.items() if not str(k).startswith("_")}
                content = out.get("content") or ""
                if max_chars and len(content) > max_chars:
                    out["content"] = content[:max_chars]
                    out["length"] = len(out["content"])
                out["cached"] = True
                out["cache_level"] = hit.get("_cache_level", "L?")
                out["url"] = url
                return out
        except Exception:
            pass

    if force_browser:
        result = _browser_fetch(url, max_chars, timeout=15.0, actions=actions)
    else:
        # 全局 deadline：所有降级升级动作共用的总预算（秒）。
        # ARGO_FETCH_DEADLINE_S=0 关闭；默认 60s（MCP 客户端工具超时的安全下限）。
        try:
            deadline_s = float(os.environ.get("ARGO_FETCH_DEADLINE_S", "60") or 60)
        except ValueError:
            deadline_s = 60.0
        t_chain0 = time.monotonic()
        deadline_hit = {"flag": False}

        def _budget_left() -> float:
            """剩余降级预算；耗尽时置标记并返回 -1（调用方停止升级）。"""
            if deadline_s <= 0:
                return 1.0
            left = deadline_s - (time.monotonic() - t_chain0)
            if left <= 0:
                deadline_hit["flag"] = True
                return -1.0
            return left

        host = (urlparse(url).hostname or "").lower()
        gated = (_mobile_branch_enabled()
                 and (_mobile_first_host(url) or _identity_is_mobile(host)))
        result = None
        # 第零级：AI 友好变体探测（{url}.md 直出）——命中即跳过整个反爬降级链。
        # 门控站跳过：少一次主机触碰，保住单次直连窗口（实测 .md 探测会触发
        # 抖音连坐限速）。
        if not gated and _md_variant_enabled():
            md = _md_variant_fetch(url, max_chars, timeout)
            if md is not None:
                md["md_variant"] = True
                result = md
        if result is None:
            # 第一级：客户端形态分流型站点（如抖音）直接以移动端 UA 首发——
            # 桌面 UA 首发会触发风控并连坐后续移动请求，顺序不可颠倒。
            if gated:
                result = _mobile_http_fetch(url, max_chars, timeout)
                if result.get("success") and not _needs_browser(result):
                    result["ua_profile"] = "mobile"
                    _identity_remember_mobile(host)
            else:
                # 第一级：HTTP
                result = _http_fetch(url, max_chars, timeout)

            # 明确停止信号（429/503）→ 不再升级 TLS/wayback/CDP。
            # 限速/过载与请求方式无关，继续升级重链 = 无视服务器指示放大负载。
            if not result.get("stop_signal") and _budget_left() > 0:
                # 第一级A2：移动端 UA 分支（客户端形态分流型反爬）。实测抖音
                # iesdouyin 对真机 UA 返回 SSR 数据、对桌面/AI UA 一律风控壳；
                # 该类分流与 TLS 指纹无关，stdlib 免费尝试即可，命中则免去
                # TLS 伪造与浏览器冷启动。ARGO_FETCH_MOBILE=0 关闭。
                if (_mobile_branch_enabled() and not gated
                        and result.get("fetch_method") != "http_mobile"
                        and (not result.get("success")
                             or _needs_browser(result))):
                    mob = _mobile_http_fetch(url, max_chars, timeout)
                    if (mob.get("success") and not mob.get("stop_signal")
                            and not _needs_browser(mob)):
                        mob["ua_profile"] = "mobile"
                        mob["http_fallback"] = True
                        result = mob
                        _identity_remember_mobile(host)

            if not result.get("stop_signal") and _budget_left() > 0:
                # 第一级B：TLS 指纹伪造（HTTP 失败或疑似指纹拦截时）
                # 指纹检测型反爬对 urllib 直接 403，TLS 层免起浏览器即可通过，
                # 避免不必要的 CDP 冷启动。门控站跳过：单次直连原则，
                # 连击直连只会加重按 IP 的递进限速，失败直接交 Wayback/浏览器。
                if (not gated and _impersonate_enabled()
                        and (not result.get("success")
                             or _needs_browser(result))):
                    spoof = _tls_spoof_fetch(url, max_chars, timeout)
                    if spoof.get("stop_signal"):
                        result = spoof
                    elif spoof.get("success"):
                        spoof["http_fallback"] = True
                        result = spoof

            if not result.get("stop_signal") and _budget_left() > 0:
                # 第一级C：r.jina.ai 阅读器（keyless 免费层，远端 JS 渲染）。
                # HTTP/TLS 直连全败时的免浏览器快速路径；markdown-only 同
                # tinyfish，need_html 场景跳过；第三方代理只接公网 URL。
                if (not gated and not need_html and _jina_enabled()
                        and _is_public_host(host)
                        and (not result.get("success")
                             or _needs_browser(result))):
                    # 超时受剩余预算约束：deadline 场景本级不得击穿总预算
                    jina = _jina_reader_fetch(
                        url, max_chars,
                        timeout=min(timeout, max(_budget_left(), 1.0)))
                    if jina is not None and jina.get("success"):
                        jina["http_fallback"] = True
                        result = jina

            if not result.get("stop_signal") and _budget_left() > 0:
                # 第一级D：Parallel 免费 MCP web_fetch（keyless 云端渲染+提取）。
                # jina 同级的免浏览器快速路径，markdown-only 同 tinyfish，
                # need_html 场景跳过；免费层限流按持久 session_id 关联；
                # full_content 整页较慢，超时受剩余预算约束。ARGO_FETCH_PARALLEL=0 关闭。
                if (not gated and not need_html and _parallel_mcp_enabled()
                        and _is_public_host(host)
                        and (not result.get("success")
                             or _needs_browser(result))):
                    pm = _parallel_mcp_fetch(
                        url, max_chars,
                        timeout=min(max(_budget_left(), 1.0), 25.0))
                    if pm.get("success") and not _needs_browser(pm):
                        pm["http_fallback"] = True
                        result = pm

            if not result.get("stop_signal") and _budget_left() > 0:
                # 第二级：Wayback 快照回退（HTTP 失败 / 内容空 / 疑似被删页面）
                if not result.get("success"):
                    wb = _wayback_fetch(url, max_chars,
                                        timeout=min(timeout * 1.5, 12.0))
                    if wb.get("success"):
                        wb["http_fallback"] = True
                        result = wb

                # 第三级：浏览器降级（HTTP 失败或疑似 CF/JS 壳）；预算耗尽不再升级
                if use_browser_fallback and _needs_browser(result) and _budget_left() > 0:
                    if _tinyfish_enabled() and not need_html:
                        # tinyfish 免费渲染（返回 clean Markdown，含 JS 执行）优先于本地 Chrome；
                        # 只产 markdown 无 raw html，爬取（need_html）跳过，失败自动回退。
                        tf = _tinyfish_fetch(url, max_chars, timeout)
                        if tf.get("success") and len(
                                (tf.get("content") or "").strip()) >= 100:
                            tf["http_fallback"] = True
                            result = tf
                    # tinyfish 未命中（关闭/缺 key/失败/短内容）才起本地 Chrome——
                    # 内容过短的成功响应与失败同样需要继续降级
                    if result.get("fetch_method") != _render_tinyfish.TINYFISH_METHOD:
                        browser_result = _browser_fetch(url, max_chars, timeout=15.0)
                        if browser_result.get("success") or not result.get("success"):
                            browser_result["http_fallback"] = True
                            result = browser_result

        if deadline_hit["flag"]:
            result["deadline_exhausted"] = True

    # 第三级：质量评估
    result = _assess_quality(result)

    # 写 URL 缓存（不存大块 html，省空间）
    if not skip_cache and result.get("success"):
        try:
            from cache import SearchCache, FETCH_DEFAULT_TTL
            to_store = {
                k: v for k, v in result.items()
                if k not in ("html",) and not str(k).startswith("_")
            }
            # 按内容类型粗略 TTL：新闻短、文档长
            ttl = FETCH_DEFAULT_TTL
            st = (result.get("source_type") or result.get("page_type") or "")
            if st in ("news", "realtime"):
                ttl = 600
            elif st in ("docs", "documentation", "reference"):
                ttl = 86400
            SearchCache().set_fetch(url, to_store, ttl=ttl)
        except Exception:
            pass
        # 证据闭环 P0：同步写 URL → 正文级证据分缓存（独立 kind，轻量）
        try:
            from evidence_loop import (
                extract_fetch_evidence, store_fetch_evidence, ttl_for_fetch_result,
            )
            ev = extract_fetch_evidence(result)
            if ev:
                store_fetch_evidence(url, ev, ttl=ttl_for_fetch_result(result))
        except Exception:
            pass

    result["cached"] = False
    return result


def fetch_page_v3(url: str, max_chars: int = 3000,
                  timeout: int = 8, raw: bool = False) -> dict:
    """兼容 fetch.py 的 fetch_page() 签名，支持透明替换。

    raw=True 时跳过 URL 缓存：缓存写入时有意丢弃 html 大字段（省空间），
    缓存命中只会返回空 html，结构化提取（tables/meta/jsonld）会全空。
    需要原始 HTML 的场景必须重新抓取，才能拿到完整页面。
    """
    result = fetch_v3(url, max_chars=max_chars, timeout=float(timeout),
                      skip_cache=raw, need_html=raw)
    out = {
        "url": result["url"],
        "content": result["content"],
        "length": result["length"],
        "success": result["success"],
        "error": result.get("error", ""),
    }
    if raw:
        out["html"] = result.get("html", "")
    return out


# ─── 聚焦提取（--focus：BM25 段落聚焦，省 token）──────────────────────────────
# 语义真源在 focus_extract.apply_focus（CLI 与 MCP 的 argo_fetch 共用同一份
# 裁剪契约），此处只做接线。历史 bug：文档（SKILL.md / references/usage.md）
# 一直写着 `argo fetch URL --focus 关键词`，但本文件的 CLI 没有该参数，
# 调用方拿到的是 argparse 的 unrecognized arguments——文档承诺的能力只在
# MCP 侧存在。加了参数还不够，两处必须走同一实现，否则迟早再次分叉。

def _apply_focus_to_result(result: dict, query: str,
                           top_k: int = 5) -> dict:
    """对成功结果做 BM25 聚焦裁剪（失败结果不动，语义与 MCP 侧一致）。"""
    if not query or not result.get("success"):
        return result
    try:
        from focus_extract import apply_focus
    except ImportError:
        return result
    return apply_focus(result, query, top_k=top_k)


# ─── CLI ─────────────────────────────────────────────────────────────────────

def build_parser():
    """CLI 参数表（独立成函数以便测试校验旗标契约）。

    `--use-browser` 与 `--browser` 同义：文档两处都写过前者，入口若只认后者，
    用户照文档敲命令即报错。两个名字都收，避免再出现「文档有、代码无」。
    """
    import argparse
    p = argparse.ArgumentParser(description="Argo fetch v3 — 四级抓取（零依赖）")
    p.add_argument("url", help="目标 URL")
    p.add_argument("--max-chars", type=int, default=8000)
    p.add_argument("--timeout", type=float, default=8.0)
    p.add_argument("--browser", "--use-browser", dest="browser",
                   action="store_true",
                   help="强制使用浏览器（--use-browser 同义）")
    p.add_argument("--no-fallback", action="store_true", help="禁用浏览器降级")
    p.add_argument("--actions", type=str,
                   help="页面交互 JSON（如 '[{\"click\":\"#btn\"}]'）")
    p.add_argument("--focus", type=str, default="",
                   help="BM25 聚焦关键词：只返回相关段落，省 token")
    p.add_argument("--focus-top", type=int, default=5,
                   help="--focus 无段落超阈值时的回退保留段落数（默认 5）")
    p.add_argument("--json", action="store_true",
                   help="只输出 JSON 摘要（缺省还会附人类可读的正文段；"
                        "Usage 一直把 --json 列为 Common flags，此前却报"
                        " unrecognized arguments）")
    return p


if __name__ == "__main__":
    args = build_parser().parse_args()

    actions = None
    if args.actions:
        actions = json.loads(args.actions)

    r = fetch_v3(args.url, max_chars=args.max_chars, timeout=args.timeout,
                 force_browser=args.browser,
                 use_browser_fallback=not args.no_fallback,
                 actions=actions)

    focus_requested = bool(args.focus)
    pre_focus_len = r.get("length", 0)
    r = _apply_focus_to_result(r, args.focus, top_k=args.focus_top)

    # 输出摘要
    summary = {k: r[k] for k in ("success", "fetch_method", "content_ok",
                                  "quality_score", "page_type", "source_type",
                                  "is_official", "length", "url")}
    if focus_requested:
        # 显式回报聚焦是否真的生效：正文过短时 focus_applied=False，
        # 不谎报「已省 token」
        summary["focus_applied"] = bool(r.get("focus_applied"))
    if r.get("error"):
        summary["error"] = r["error"]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    # __main__ 块是模块级代码，不能用 return 短路——用 if 包住人类可读段
    if not args.json:
        if r.get("title"):
            print(f"\nTitle: {r['title']}")
        if focus_requested:
            print(f"\n[focus] query={args.focus!r} applied={bool(r.get('focus_applied'))} "
                  f"chars={pre_focus_len} → {r.get('length', 0)}")
        print(f"\n--- CONTENT ({r['length']} chars) ---")
        print(r.get("content", "")[:2000])
