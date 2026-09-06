#!/usr/bin/env python3
"""2026-09-02 审计加固回归门。

覆盖（全部离线）：
  1. subprocess 编码 AST 门禁：全仓 subprocess.run/Popen 一旦 text=True
     必须显式 encoding（Windows 默认 GBK，中文输出/输入会 mojibake 或崩）
  2. recompute 白名单：Path/bytes 对象绕过 open 守卫已被堵 + os.open 原始
     fd 逃逸被封 + UTF-8 输出往返（-X utf8）
  3. article 重定向逐跳 SSRF 校验：内网目标拒绝、公网目标放行
  4. mcp local_search 子进程显式 UTF-8（编码 + PYTHONUTF8 注入）

运行：
  python3 -m pytest tests/test_hardening_0902.py -v
"""

from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))


# ── 1. subprocess 编码 AST 门禁 ──────────────────────────────────────────────

class TestSubprocessEncodingGate(unittest.TestCase):
    """全仓扫描：text=True 的 subprocess 调用必须显式 encoding。"""

    def _iter_subprocess_calls(self, tree: ast.AST):
        # 别名感知：import subprocess as _sp / from subprocess import run
        # 都可能被调用，只认 'subprocess' 字样会漏（search.py 曾用 _sp 逃逸）
        bound: set[str] = {"subprocess"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name == "subprocess":
                        bound.add(a.asname or a.name)
            elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
                for a in node.names:
                    bound.add(a.asname or a.name)
        func_names = ("run", "Popen", "check_output")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (isinstance(func, ast.Attribute) and func.attr in func_names
                    and isinstance(func.value, ast.Name)
                    and func.value.id in bound):
                yield node
            elif (isinstance(func, ast.Name) and func.id in func_names
                    and func.id in bound):
                yield node  # from subprocess import run 直调

    def _kw(self, node: ast.Call, name: str):
        for kw in node.keywords:
            if kw.arg == name:
                return kw
        return None

    def test_all_text_calls_have_explicit_encoding(self):
        offenders: list[str] = []
        for py in sorted(SCRIPT_DIR.glob("*.py")):
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
            for call in self._iter_subprocess_calls(tree):
                text_kw = self._kw(call, "text")
                # text 显式 False / 缺省（字节模式）不要求 encoding
                if text_kw is None:
                    continue
                if isinstance(text_kw.value, ast.Constant) and not text_kw.value.value:
                    continue
                if self._kw(call, "encoding") is None:
                    offenders.append(f"{py.name}:{call.lineno}")
        self.assertEqual(
            offenders, [],
            "text=True 的 subprocess 调用缺少显式 encoding（Windows GBK 会乱码/崩），"
            f"请补 encoding='utf-8', errors='replace'：{offenders}")

    def test_gate_detects_alias_violation(self):
        """门禁自检：`as sp` 别名 + text=True 无 encoding 必须被识别，
        防止门禁自身对别名导入失明（search.py _sp 曾逃逸的教训）。"""
        snippet = (
            "import subprocess as sp\n"
            "def f():\n"
            "    return sp.run(['ls'], capture_output=True, text=True)\n"
        )
        tree = ast.parse(snippet)
        offenders = [c.lineno for c in self._iter_subprocess_calls(tree)
                     if self._kw(c, "encoding") is None]
        self.assertEqual(offenders, [3])


# ── 2. recompute 白名单与编码 ────────────────────────────────────────────────

class TestRecomputeHardening(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.allowed = Path(self._tmp.name) / "sales.csv"
        self.allowed.write_text("year,revenue\n2024,120\n2025,148\n", encoding="utf-8")
        # 白名单外的邻居文件：PathLike 绕过若仍存在，读它即泄漏
        self.secret = Path(self._tmp.name) / "secret.txt"
        self.secret.write_text("TOPSECRET", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, code: str) -> dict:
        from recompute import run_recompute
        return run_recompute(code, [{"path": str(self.allowed)}], allow_exec=True)

    def test_path_object_outside_whitelist_blocked(self):
        code = (
            "import pathlib\n"
            "try:\n"
            f"    print(pathlib.Path({str(self.secret)!r}).read_text())\n"
            "    print('LEAK')\n"
            "except PermissionError:\n"
            "    print('BLOCKED')\n"
        )
        r = self._run(code)
        self.assertTrue(r["ok"], r)
        self.assertIn("BLOCKED", r["stdout"])
        self.assertNotIn("TOPSECRET", r["stdout"])

    def test_path_object_inside_whitelist_allowed(self):
        code = (
            "import pathlib\n"
            f"print(pathlib.Path(_ALLOWED[0]).read_text(encoding='utf-8')[:4])\n"
        )
        r = self._run(code)
        self.assertTrue(r["ok"], r)
        self.assertIn("year", r["stdout"])

    def test_bytes_path_outside_whitelist_blocked(self):
        code = (
            "try:\n"
            f"    print(open({str(self.secret)!r}.encode()).read())\n"
            "    print('LEAK')\n"
            "except (PermissionError, OSError):\n"
            "    print('BLOCKED')\n"
        )
        r = self._run(code)
        self.assertTrue(r["ok"], r)
        self.assertNotIn("TOPSECRET", r["stdout"])

    def test_os_open_raw_fd_blocked(self):
        code = (
            "import os\n"
            "try:\n"
            f"    fd = os.open({str(self.secret)!r}, os.O_RDONLY)\n"
            "    print(os.read(fd, 64).decode())\n"
            "    print('LEAK')\n"
            "except Exception as e:\n"
            "    print('BLOCKED')\n"
        )
        r = self._run(code)
        self.assertTrue(r["ok"], r)
        self.assertNotIn("TOPSECRET", r["stdout"])

    def test_utf8_output_roundtrip(self):
        code = "print('中文往返 🚦 ok')\n"
        r = self._run(code)
        self.assertTrue(r["ok"], r)
        self.assertIn("中文往返 🚦 ok", r["stdout"])


# ── 3. article 重定向逐跳 SSRF 校验 ─────────────────────────────────────────

class TestArticleRedirectGuard(unittest.TestCase):
    def _handler(self):
        from article import _SafeRedirectHandler
        return _SafeRedirectHandler()

    def test_private_redirect_rejected(self):
        h = self._handler()
        for bad in ("http://127.0.0.1:8080/x", "http://169.254.169.254/latest/meta-data/",
                    "file:///etc/passwd", "http://localhost/x"):
            with self.assertRaises(ValueError, msg=bad):
                h.redirect_request(req=None, fp=None, code=302, msg="Found",
                                   headers={}, newurl=bad)

    def test_public_redirect_allowed(self):
        import urllib.request as _ur
        h = self._handler()
        newurl = "https://mp.weixin.qq.com/s?__biz=test"
        req = _ur.Request(newurl)
        out = h.redirect_request(req=req, fp=None, code=302, msg="Found",
                                 headers={}, newurl=newurl)
        self.assertIsNotNone(out)


# ── 4. mcp local_search 子进程显式 UTF-8 ─────────────────────────────────────

class TestLocalSearchSubprocessEncoding(unittest.TestCase):
    def test_encoding_and_utf8_env_injected(self):
        import mcp_handlers
        captured: dict = {}

        class _FakeProc:
            returncode = 1
            stdout = ""
            stderr = "no"

        def _fake_run(cmd, **kwargs):
            captured.update(kwargs)
            return _FakeProc()

        with patch.object(mcp_handlers.subprocess, "run", _fake_run):
            mcp_handlers.execute_tool("argo_local_search",
                                      {"query": "中文查询", "max_results": 3})
        self.assertEqual(captured.get("encoding"), "utf-8")
        self.assertEqual(captured.get("errors"), "replace")
        self.assertEqual((captured.get("env") or {}).get("PYTHONUTF8"), "1")


if __name__ == "__main__":
    unittest.main()
