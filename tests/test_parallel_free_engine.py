#!/usr/bin/env python3
"""parallel_free 免费通道 builder：解析与分立语义测试（mock HttpClient，无网络）。

覆盖 2026-09-14 收录：
  - 免 key 直调 search.parallel.ai/mcp（无状态 tools/call web_search）
  - content[0].text 内嵌 JSON 的结果映射（url/title/publish_date/excerpts 同构 REST）
  - isError / 协议级错误必须显式上报（复用 _mcp_error_of，不得静默返回空）
  - 与计费通道分立：不看 key，恒走免费通道（key 的有无是 parallel 的事）
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from engines_builders_search import _build_parallel_free_engine  # noqa: E402

_SEARCH_ID = "search_1fee93a9f31b937d160b2a92db31459b"


def _results_inner(results: list[dict]) -> dict:
    """content[0].text 内嵌的结果 JSON（与 REST 响应同构）。"""
    return {"search_id": _SEARCH_ID, "results": results}


def _mcp_envelope(*, text: str, is_error: bool = False) -> dict:
    """MCP tools/call 响应信封：result.content[0].text 携带 body_text。"""
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return {"jsonrpc": "2.0", "id": 1, "result": result}


def _engine_with(*, inner: dict | None = None, text: str | None = None,
                 is_error: bool = False, status: int = 200, keyed: bool = False):
    """构造 builder 并 mock HttpClient.post 返回固定 MCP 响应。

    inner = content[0].text 内嵌的结果 JSON；text = 原始文本（配合 is_error
    或「非 JSON 正文」用例）。二者都不传时 content 为空对象文本。
    """
    body_text = text if text is not None else json.dumps(inner or {},
                                                         ensure_ascii=False)
    payload = {
        "status": status,
        "headers": {"content-type": "application/json"},
        "text": json.dumps(_mcp_envelope(text=body_text, is_error=is_error)),
        "url": "https://search.parallel.ai/mcp",
        "elapsed_ms": 10,
    }
    env = {"PARALLEL_API_KEY": "test-key"} if keyed else {"PARALLEL_API_KEY": ""}
    with patch.dict(os.environ, env), patch("http_client.HttpClient") as MockClient:
        MockClient.return_value.post.return_value = payload
        eng = _build_parallel_free_engine({"timeout": 5})
        return eng("rust async", n=5)


class TestParallelFreeBuilder(unittest.TestCase):
    def test_maps_results_without_key(self):
        """免 key：results 映射 title/url/snippet/published_at，source 标 parallel_free。"""
        results = _engine_with(inner=_results_inner([
            {"url": "https://zhuanlan.zhihu.com/p/1",
             "title": "AI Agent 技术全景 - 知乎",
             "publish_date": "2026-04-11",
             "excerpts": ["AI Agent 是能够感知环境的软件系统。", "第二段摘录"]},
            {"url": "https://example.com/review", "title": "评测汇总",
             "publish_date": None, "excerpts": []},
        ]))
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["title"], "AI Agent 技术全景 - 知乎")
        self.assertEqual(results[0]["url"], "https://zhuanlan.zhihu.com/p/1")
        self.assertEqual(results[0]["snippet"], "AI Agent 是能够感知环境的软件系统。")
        self.assertEqual(results[0]["published_at"], "2026-04-11")
        self.assertEqual(results[0]["source"], "parallel_free")
        self.assertEqual(results[1]["snippet"], "")
        self.assertEqual(results[1]["published_at"], "")

    def test_slices_to_n(self):
        """n 截断：上游无 max_results 参数，客户端按 limit 截断。"""
        results = _engine_with(inner=_results_inner(
            [{"url": f"https://example.com/{i}", "title": f"t{i}", "excerpts": []}
             for i in range(10)]))
        self.assertEqual(len(results), 5)

    def test_is_error_reported_not_silent(self):
        """isError=true 必须显式上报错误项（失败不得伪装成无结果）。"""
        results = _engine_with(text="Service temporarily unavailable.",
                               is_error=True)
        self.assertEqual(len(results), 1)
        self.assertIn("error", results[0])
        self.assertEqual(results[0]["source"], "parallel_free")

    def test_protocol_error_reported(self):
        """JSON-RPC 协议级 error 也须上报。"""
        payload = {"jsonrpc": "2.0", "id": 1,
                   "error": {"code": -32000, "message": "too many requests"}}
        resp = {"status": 200, "headers": {}, "text": json.dumps(payload),
                "url": "https://search.parallel.ai/mcp", "elapsed_ms": 10}
        with patch.dict(os.environ, {"PARALLEL_API_KEY": ""}), \
             patch("http_client.HttpClient") as MockClient:
            MockClient.return_value.post.return_value = resp
            eng = _build_parallel_free_engine({"timeout": 5})
            results = eng("rust async", n=5)
        self.assertEqual(len(results), 1)
        self.assertIn("error", results[0])

    def test_non_json_content_text_ignored(self):
        """content 文本非结果 JSON（如纯提示文本）→ 不产出幻觉结果，返回空。"""
        results = _engine_with(text="Please describe your objective.")
        self.assertEqual(results, [])

    def test_ignores_key_always_free_channel(self):
        """不看 key：配置 PARALLEL_API_KEY 也照走免费通道（key 的有无是
        parallel 计费通道的事；本引擎带 key 反而可能混淆计费归属）。"""
        results = _engine_with(inner=_results_inner(
            [{"url": "https://example.com", "title": "t", "excerpts": []}]),
            keyed=True)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source"], "parallel_free")

    def test_builder_registered(self):
        """_BUILDERS 注册 + config 声明可被 type 路由（防 import 笔误）。"""
        from engines import _BUILDERS
        self.assertIn("parallel_free", _BUILDERS)


if __name__ == "__main__":
    unittest.main()
