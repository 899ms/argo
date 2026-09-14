#!/usr/bin/env python3
"""fetch_v3 Parallel 免费 MCP web_fetch 级（第一级D）：解析与降级测试。

覆盖 2026-09-14 收录：
  - full_content=true 的整页 markdown 映射（超长截到 max_chars）
  - 上游缺 full_content 时退回 excerpts 拼接（上游字段变更不空手）
  - MCP isError / 协议级错误：success=False 且 error 留痕（fetch 链语义=
    失败放行后级，但可归因）
  - 开关 ARGO_FETCH_PARALLEL=0 关闭本层
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fetch_v3 import _parallel_mcp_enabled, _parallel_mcp_fetch  # noqa: E402

_URL = "https://example.com/page"


def _mcp_resp(result_obj: dict, *, is_error: bool = False) -> dict:
    result: dict[str, Any] = {"content": [{"type": "text",
                                           "text": json.dumps(result_obj)}]}
    if is_error:
        result["isError"] = True
        result["content"] = [{"type": "text", "text": "upstream unavailable"}]
    return {"status": 200, "headers": {},
            "text": json.dumps({"result": result}),
            "url": "mcp", "elapsed_ms": 10}


class TestParallelMcpFetch(unittest.TestCase):
    def _with(self, payload: dict, url: str = _URL) -> dict:
        with patch("http_client.HttpClient") as MockClient:
            MockClient.return_value.post.return_value = payload
            return _parallel_mcp_fetch(url, max_chars=500)

    def test_full_content_mapped(self):
        long_md = "正文行\n\n" * 200
        r = self._with(_mcp_resp({"results": [{
            "url": _URL, "title": "Page Title", "full_content": long_md,
            "excerpts": ["ex1"]}], "errors": []}))
        self.assertTrue(r["success"])
        self.assertEqual(r["fetch_method"], "parallel_mcp")
        self.assertEqual(r["title"], "Page Title")
        self.assertEqual(r["length"], 500)  # 截到 max_chars
        self.assertTrue(r["content"].startswith("正文行"))

    def test_falls_back_to_excerpts_when_no_full_content(self):
        r = self._with(_mcp_resp({"results": [{
            "url": _URL, "title": "T", "full_content": None,
            "excerpts": ["摘录一", "摘录二"]}], "errors": []}))
        self.assertTrue(r["success"])
        self.assertEqual(r["content"], "摘录一\n\n摘录二")

    def test_is_error_reported_not_success(self):
        r = self._with(_mcp_resp({}, is_error=True))
        self.assertFalse(r["success"])
        self.assertIn("upstream unavailable", r.get("error", ""))

    def test_protocol_error_reported(self):
        payload = {"status": 200, "headers": {},
                   "text": json.dumps({"error": {"code": -32000,
                                                 "message": "rate limited"}}),
                   "url": "mcp", "elapsed_ms": 5}
        r = self._with(payload)
        self.assertFalse(r["success"])
        self.assertIn("rate limited", r.get("error", ""))

    def test_no_results_reported(self):
        r = self._with(_mcp_resp({"results": [], "errors": [
            {"url": _URL, "error_type": "http_error", "http_status_code": 503}]}))
        self.assertFalse(r["success"])
        self.assertIn("上游无结果", r.get("error", ""))

    def test_flag_off(self):
        with patch.dict("os.environ", {"ARGO_FETCH_PARALLEL": "0"}):
            self.assertFalse(_parallel_mcp_enabled())
        with patch.dict("os.environ", {"ARGO_FETCH_PARALLEL": "1"}):
            self.assertTrue(_parallel_mcp_enabled())


if __name__ == "__main__":
    unittest.main()
