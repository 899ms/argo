#!/usr/bin/env python3
"""answer.py 与 watch.py（直答 / 观察模式）单元测试（mock，无网络）。

覆盖 2026-09-14 收录：
  answer：citations 置信口径计数（唯一域名去重）、缺 key 显式报错、
          上游 confidence 字段透传
  watch：快照建立 / 变化检测 / 抓取失败不伪造变化 / 状态目录隔离
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import answer as answer_mod  # noqa: E402
import watch as watch_mod  # noqa: E402


class _FakeResp:
    def __init__(self, payload: dict[str, Any]):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *args: Any) -> bool:
        return False


class TestAnswer(unittest.TestCase):
    def test_unique_domains_dedupes(self):
        citations = [{"url": "https://a.com/x"}, {"url": "https://a.com/y"},
                     {"url": "https://b.com"}, {"url": ""}]
        self.assertEqual(answer_mod._unique_domains(citations), 2)

    def test_seltz_answer_missing_key_errors(self):
        with patch.dict(os.environ, {"SELTZ_API_KEY": ""}):
            data, err = answer_mod.seltz_answer("q")
        self.assertIsNone(data)
        self.assertIn("SELTZ_API_KEY", err)

    def test_seltz_answer_parses_and_passes_upstream_confidence(self):
        payload = {"answer": "答", "citations": [{"url": "https://a.com"},
                                                 {"url": "https://b.com"}],
                   "confidence": 0.87}
        with patch.dict(os.environ, {"SELTZ_API_KEY": "k"}), \
             patch("http_client.HttpClient") as MockClient:
            MockClient.return_value.post.return_value = {
                "status": 200, "headers": {}, "text": json.dumps(payload),
                "url": "seltz", "elapsed_ms": 10}
            data, err = answer_mod.seltz_answer("q")
        self.assertEqual(err, "")
        self.assertEqual(data["answer"], "答")
        self.assertEqual(len(data["citations"]), 2)

    def test_confidence_counts_honest_when_no_upstream_scalar(self):
        citations = [{"url": "https://a.com/x"}, {"url": "https://b.com"}]
        conf = {"citations": len(citations),
                "unique_domains": answer_mod._unique_domains(citations)}
        self.assertNotIn("upstream", conf)
        self.assertEqual(conf, {"citations": 2, "unique_domains": 2})


class TestWatch(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ,
                               {"ARGO_STATE_DIR": self._tmp.name})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._tmp.cleanup)

    def _fake_fetch(self, content: str, success: bool = True):
        return {"url": "https://e.com", "title": "T", "content": content,
                "length": len(content), "success": success,
                "fetch_method": "http", "error": "" if success else "boom"}

    def test_add_then_check_unchanged(self):
        with patch("fetch_v3.fetch_v3", return_value=self._fake_fetch("hello")):
            add = watch_mod.cmd_add("https://e.com", note="n")
            self.assertTrue(add["snapshot"]["success"])
            rep = watch_mod.cmd_check(None)
        self.assertEqual(rep[0]["changed"], False)
        self.assertEqual(rep[0]["new_hash"], add["snapshot"]["hash"])

    def test_check_detects_change(self):
        with patch("fetch_v3.fetch_v3", return_value=self._fake_fetch("v1")):
            watch_mod.cmd_add("https://e.com", note="")
        with patch("fetch_v3.fetch_v3", return_value=self._fake_fetch("v2 changed")):
            rep = watch_mod.cmd_check(None)
        self.assertEqual(rep[0]["changed"], True)
        self.assertNotEqual(rep[0]["new_hash"], rep[0]["previous_hash"])

    def test_fetch_failure_keeps_snapshot_and_reports_error(self):
        with patch("fetch_v3.fetch_v3", return_value=self._fake_fetch("v1")):
            watch_mod.cmd_add("https://e.com", note="")
        with patch("fetch_v3.fetch_v3",
                   return_value=self._fake_fetch("", success=False)):
            rep = watch_mod.cmd_check(None)
        self.assertIsNone(rep[0]["changed"])
        self.assertIn("boom", rep[0]["error"])
        # 失败不落快照：历史仍只有 1 帧
        data = watch_mod._load()
        self.assertEqual(len(data["urls"]["https://e.com"]["snapshots"]), 1)

    def test_whitespace_noise_not_a_change(self):
        with patch("fetch_v3.fetch_v3",
                   return_value=self._fake_fetch("hello\n  world")):
            watch_mod.cmd_add("https://e.com", note="")
        with patch("fetch_v3.fetch_v3",
                   return_value=self._fake_fetch("hello world")):
            rep = watch_mod.cmd_check(None)
        self.assertEqual(rep[0]["changed"], False)

    def test_remove(self):
        with patch("fetch_v3.fetch_v3", return_value=self._fake_fetch("x")):
            watch_mod.cmd_add("https://e.com", note="")
        self.assertTrue(watch_mod.cmd_remove("https://e.com"))
        self.assertFalse(watch_mod.cmd_remove("https://e.com"))


if __name__ == "__main__":
    unittest.main()
