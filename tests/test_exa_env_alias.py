#!/usr/bin/env python3
"""exa 密钥别名回归：只配 ARGO_EXA_API_KEY（新名）也必须能出结果。

issue #12（2026-09-13）：builder 只读 EXA_API_KEY，用户按文档推荐配了
ARGO_EXA_API_KEY → env_ready=True 却静默 0 结果。本测试锁死修复：
新名/旧名/别名链任一就绪都出结果，mock http_open 无网络。
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

from engines_builders_tech import _build_exa_engine  # noqa: E402


class _FakeResp:
    def __init__(self, payload: dict[str, Any]):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *args: Any) -> bool:
        return False


def _run_with_env(env: dict[str, str]) -> list[dict[str, Any]]:
    payload = {"results": [{"title": "T1", "url": "https://a.com", "text": "内容"}]}
    with patch.dict(os.environ, env, clear=False), \
         patch("engine_env._envfile_load", return_value={}), \
         patch("engines_builders_tech.http_open") as mock_open:
        mock_open.return_value.__enter__.return_value = _FakeResp(payload)
        eng = _build_exa_engine({"timeout": 5})
        return eng("rust async", n=5)


class TestExaEnvAlias(unittest.TestCase):
    def test_prefixed_new_name_works(self):
        """只配新名（issue #12 的场景）：必须出结果，不得静默 0。"""
        results = _run_with_env({"ARGO_EXA_API_KEY": "k-new", "EXA_API_KEY": ""})
        self.assertTrue(results, "只配 ARGO_EXA_API_KEY 却拿不到结果（#12 回归）")
        self.assertNotIn("error", results[0])

    def test_legacy_name_still_works(self):
        results = _run_with_env({"EXA_API_KEY": "k-old"})
        self.assertTrue(results)

    def test_no_key_reports_error_not_silent(self):
        results = _run_with_env({"ARGO_EXA_API_KEY": "", "EXA_API_KEY": ""})
        self.assertEqual(len(results), 1)
        self.assertIn("error", results[0])


if __name__ == "__main__":
    unittest.main()
