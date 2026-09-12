#!/usr/bin/env python3
"""test_mcp_error.py — MCP JSON-RPC 响应错误检查回归测试。

## 背景：实测发现的真 bug

anysearch 引擎 POST 到 `api.anysearch.com/mcp`，上游返回：

    HTTP 200
    {"jsonrpc":"2.0","id":1,
     "result":{"content":[{"type":"text","text":"Service temporarily unavailable."}],
               "isError":true}}

旧实现**忽略 `isError`**，只做两件事：① 在文本里找配额关键词
（quota/exhausted/rate limit/429…）② 按 `### N.` 解析结果块。
「Service temporarily unavailable.」不含配额词、也没有结果块
→ **静默返回 `[]`**。

后果（实测）：
  · 用户看到「没有结果」，而非「上游不可用」
  · 熔断器拿不到失败信号（空结果走 `empty` 分支，不驱动 open），
    于是持续空转调用一个已坏的源
  · `quota.json` 里 anysearch 累计 444 次错误，但没有任何地方能说出「为什么」

这是本仓反复出现的「**失败伪装成成功**」模式的又一例：
V2EX 旧实现产出幻觉 → 缓存软命中跨引擎串味 → juejin/qiita 返回热榜
→ you/parallel 状态说谎 → anysearch 忽略 isError。

## 讽刺之处

argo **自己**在服务端输出 `isError`（见 mcp_handlers.py），
却在**消费**上游 MCP 服务时完全忽略它。全仓 `grep isError` 只有
服务端写法，零消费者。

本文件锁定 `engines_base.mcp_error_of` 的三类错误判定。
"""

import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from engines_base import mcp_error_of  # noqa: E402


class TestToolLevelError:
    """MCP 工具级错误：result.isError == true。"""

    def test_real_anysearch_payload(self):
        """实测报文的原样复现（本测试的来源）。"""
        data = {
            "jsonrpc": "2.0", "id": 1,
            "result": {
                "_meta": {"request_id": "95dd8944"},
                "content": [{"type": "text",
                             "text": "Service temporarily unavailable."}],
                "isError": True,
            },
        }
        err = mcp_error_of(data)
        assert err is not None
        assert "Service temporarily unavailable" in err

    def test_iserror_without_text(self):
        err = mcp_error_of({"result": {"isError": True, "content": []}})
        assert err is not None and "isError" in err

    def test_iserror_string_content(self):
        err = mcp_error_of({"result": {"isError": True, "content": ["boom"]}})
        assert err is not None and "boom" in err

    def test_iserror_false_is_not_error(self):
        assert mcp_error_of({"result": {"isError": False, "content": []}}) is None

    def test_iserror_truthy_non_true(self):
        """只有 True 算错误（MCP 规范用布尔 true；"false" 字符串不算）。"""
        assert mcp_error_of({"result": {"isError": "true"}}) is None


class TestProtocolLevelError:
    """JSON-RPC 协议级错误：顶层 error 对象。"""

    def test_error_dict(self):
        err = mcp_error_of({"error": {"code": -32600, "message": "Invalid Request"}})
        assert err is not None and "Invalid Request" in err

    def test_error_without_message(self):
        err = mcp_error_of({"error": {"code": -32000}})
        assert err is not None and "-32000" in err

    def test_error_string(self):
        err = mcp_error_of({"error": "server exploded"})
        assert err is not None and "server exploded" in err

    def test_empty_error_string_ignored(self):
        assert mcp_error_of({"error": ""}) is None


class TestHealthyResponse:
    """正常响应不得被误判为错误（防假阳性拖垮健康引擎）。"""

    def test_normal_result(self):
        data = {"result": {"content": [
            {"type": "text", "text": "### 1. Some Title\n- **URL**: https://x"}]}}
        assert mcp_error_of(data) is None

    def test_result_without_iserror_key(self):
        assert mcp_error_of({"result": {"content": []}}) is None

    def test_missing_result_key(self):
        """result 缺失时返回 None——交回调用方按业务语义判断。"""
        assert mcp_error_of({"jsonrpc": "2.0", "id": 1}) is None

    def test_non_dict_input(self):
        for bad in ("oops", None, 42, []):
            err = mcp_error_of(bad)
            assert err is not None, f"{bad!r} 非 JSON 对象应报错"


class TestSourcePrefix:
    def test_default_source(self):
        err = mcp_error_of({"error": "x"})
        assert err.startswith("mcp:")

    def test_custom_source(self):
        err = mcp_error_of({"error": "x"}, source="anysearch")
        assert err.startswith("anysearch:")
