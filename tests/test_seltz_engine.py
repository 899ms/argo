#!/usr/bin/env python3
"""seltz 引擎 builder：解析与密钥边界测试（mock http_open，无网络）。

覆盖 2026-09-14 收录：
  - 响应 {documents:[{url, content, published_date}]} 的映射：无 title 字段，
    content 首行截作标题（官方响应就没有 title，不猜）
  - 无 key 必须显式报错项（不得静默返回空——「失败伪装成成功」是本仓大忌）
  - max_results 上限 10、n 截断
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

from engines_builders_search import _build_seltz_engine  # noqa: E402


class _FakeResp:
    def __init__(self, payload: dict[str, Any]):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *args: Any) -> bool:
        return False


def _engine_with(documents: list[dict], *, keyed: bool = True):
    payload = {"documents": documents}
    env = {"SELTZ_API_KEY": "test-key"} if keyed else {"SELTZ_API_KEY": ""}
    with patch.dict(os.environ, env), \
         patch("engines_builders_search.http_open") as mock_open:
        mock_open.return_value.__enter__.return_value = _FakeResp(payload)
        eng = _build_seltz_engine({"timeout": 5})
        return eng("rust async", n=5)


class TestSeltzBuilder(unittest.TestCase):
    def test_maps_documents_without_title_field(self):
        """无 title 字段：content 首行截作标题，content 截 snippet。"""
        results = _engine_with([
            {"url": "https://example.com/a",
             "content": "Async Rust guide\n\n第二段正文更长り",
             "published_date": "2026-08-01"},
            {"url": "https://example.com/b", "content": "", "published_date": None},
        ])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["title"], "Async Rust guide")
        self.assertEqual(results[0]["snippet"].startswith("Async Rust guide"), True)
        self.assertEqual(results[0]["published_at"], "2026-08-01")
        self.assertEqual(results[0]["source"], "seltz")
        # content 为空且无 title：url 仍在，title 空
        self.assertEqual(results[1]["title"], "")
        self.assertEqual(results[1]["url"], "https://example.com/b")

    def test_missing_key_reports_error_not_silent(self):
        """未配 key：显式错误项，让状态层与用户看到原因。"""
        results = _engine_with([{"url": "https://x", "content": "c"}], keyed=False)
        self.assertEqual(len(results), 1)
        self.assertIn("SELTZ_API_KEY", results[0].get("error", ""))

    def test_slices_to_n(self):
        results = _engine_with([
            {"url": f"https://example.com/{i}", "content": f"doc {i}"}
            for i in range(9)
        ])
        self.assertEqual(len(results), 5)

    def test_builder_registered(self):
        from engines import _BUILDERS
        self.assertIn("seltz", _BUILDERS)


if __name__ == "__main__":
    unittest.main()
