#!/usr/bin/env python3
"""test_archive_redact.py — 归档凭证脱敏（写入时生效）回归测试。

覆盖：
  1. redact_secrets 按类别：URL query 密钥 / Bearer 头 / 家目录路径
  2. 无害内容原样透传（脱敏不过度破坏日志可用性）
  3. 写入路径集成：write_search_archive 写入文件文件里找不到注入的密钥
"""

import json
import os
import sys
import tempfile

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from archive_run import redact_secrets, _redact_deep, write_search_archive  # noqa: E402


class TestRedactSecrets:
    """按秘密类别逐类锁定，每类一条。"""

    def test_url_query_api_key(self):
        out = redact_secrets("https://api.x.com/v1?q=hello&api_key=sk-live-123&page=2")
        assert "sk-live-123" not in out
        assert "api_key=[REDACTED]" in out
        assert "q=hello" in out  # 其余参数存活，日志仍有用

    def test_url_query_token(self):
        out = redact_secrets("https://x.com/search?token=abc123&q=test")
        assert "abc123" not in out

    def test_bare_cookie_credential(self):
        out = redact_secrets("SESSDATA=deadbeef-value bili_jct=abc99")
        assert "deadbeef-value" not in out
        assert "abc99" not in out

    def test_bearer_header(self):
        out = redact_secrets("Authorization: Bearer eyJhbGciOiJIUz")
        assert "eyJhbGciOiJIUz" not in out
        assert "Bearer [REDACTED]" in out

    def test_home_path(self):
        out = redact_secrets("saved at /Users/someone/Downloads/x.md and /home/other/y")
        assert "someone" not in out
        assert "other" not in out
        assert "~/Downloads/x.md" in out

    def test_clean_content_untouched(self):
        clean = "https://argo.dev/query?q=张三&lang=zh 引用 [1] https://example.com/a?b=1"
        assert redact_secrets(clean) == clean


class TestRedactDeep:
    def test_nested_structures(self):
        obj = {
            "url": "https://a.com/?key=secret1",
            "items": [{"note": "token=secret2"}],
            "count": 3,
            "raw": None,
        }
        out = _redact_deep(obj)
        assert "secret1" not in out["url"]
        assert "secret2" not in out["items"][0]["note"]
        assert out["count"] == 3
        assert out["raw"] is None


class TestWritePathIntegration:
    """写入时生效：写入文件文件里找不到密钥（核心契约）。"""

    def test_archive_files_redacted(self, tmp_path):
        secret = "sk-ARGO-TEST-SECRET-VALUE"
        result = {
            "query": "脱敏测试",
            "results": [{
                "title": "t",
                "url": f"https://api.x.com/v1?q=a&api_key={secret}",
                "snippet": f"登录态 token={secret}",
                "source": "http",
            }],
            "candidates": [{
                "candidate_id": "web:rank-1",
                "url": f"https://api.x.com/v1?q=a&api_key={secret}",
            }],
            "sources": [{
                "ref": 1,
                "title": "t",
                "url": f"https://api.x.com/v1?q=a&api_key={secret}",
            }],
        }
        summary = write_search_archive(result, root=tmp_path)
        run_dir = summary["run_dir"] if "run_dir" in summary else None
        if run_dir is None:
            # 兼容：从 paths 字段取
            run_dir = list(summary["paths"].values())[0]
        run_dir = os.path.dirname(str(run_dir)) or str(run_dir)
        # 遍历 run 目录全部文件，任何文件里都不允许出现明文密钥
        for root, _dirs, files in os.walk(tmp_path):
            for name in files:
                path = os.path.join(root, name)
                with open(path, encoding="utf-8") as f:
                    body = f.read()
                assert secret not in body, f"泄漏于 {path}"
