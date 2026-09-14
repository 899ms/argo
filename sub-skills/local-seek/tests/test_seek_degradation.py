#!/usr/bin/env python3
"""local-seek 降级链与 --count 排序回归测试（P0-2 / P0-4，2026-09-13）。

守的两个缺陷：
  1. 降级链曾把「工具缺失」伪装成「搜索结论」：scope=code 且缺 rg 时落
     mdfind（Spotlight 不索引源码内容）→ 搜 argo 自身符号返回 24 条外部
     陈旧副本、漏掉正主，还报「未找到匹配（mdfind）」。修复后：缺 rg 直落
     grep，两者皆无才显式报错。
  2. --count 曾按 rg 原始输出顺序（目录遍历序）截断，「哪些文件命中最多」
     系统性答错。修复后：全量收集→按命中数降序→取前 N。

运行：
  cd ~/.agents/skills/argo
  python3 -m pytest sub-skills/local-seek/tests/test_seek_degradation.py -q
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import seek as s  # noqa: E402


def _run_main(argv):
    """跑 seek.main()，捕获 stdout，返回 (exit_code, stdout)。"""
    with mock.patch.object(sys, "argv", ["seek.py"] + argv):
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                code = s.main()
        except SystemExit as e:
            code = e.code or 0
    return code or 0, buf.getvalue()


class TestDegradationChain(unittest.TestCase):
    """scope=code 缺 rg 时的降级（全部 mock，不依赖本机装了什么）。"""

    def _argv(self, tmp: str) -> list[str]:
        return ["def needle", "--scope", "code", "--path", tmp, "--json"]

    def test_rg_missing_falls_to_grep(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.py").write_text("def needle(x):\n    return x\n")
            with mock.patch.object(s, "tool_exists",
                                   lambda name: name in ("grep", "mdfind")), \
                 mock.patch.object(s, "resolve_grep", lambda: "/usr/bin/grep"), \
                 mock.patch.object(s, "grep_search",
                                   lambda *a, **k: ([(str(Path(tmp) / "a.py"), 1, "def needle(x)")], None)):
                code, out = _run_main(self._argv(tmp))
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["engine"], "grep",
                         "缺 rg 时必须落 grep，不能落 mdfind")

    def test_rg_and_grep_missing_reports_tools_not_nomatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(s, "tool_exists", lambda name: False), \
                 mock.patch.object(s, "resolve_grep", lambda: None):
                code, out = _run_main(self._argv(tmp))
        self.assertEqual(code, 1)
        self.assertIn("rg", out)
        self.assertIn("grep", out)
        self.assertNotIn("未找到匹配", out,
                         "工具缺失不得伪装成搜索结论")

    def test_real_rg_still_used(self):
        """rg 在位时行为不变（防把好路径改坏）。"""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.py").write_text("def needle(x):\n    return x\n")
            code, out = _run_main(self._argv(tmp))
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["engine"], "rg")


class TestCountOrdering(unittest.TestCase):
    """--count 按命中数降序（真实 rg 端到端）。"""

    def test_most_hits_first(self):
        if not s.tool_exists("rg"):
            self.skipTest("本机无 rg")
        with tempfile.TemporaryDirectory() as tmp:
            # 遍历序（字典序）在前面的文件命中少、后面的命中多：
            # 旧实现按遍历序截断会答错。
            (Path(tmp) / "aaa.txt").write_text("needle\n")          # 1 次
            (Path(tmp) / "zzz.txt").write_text("needle\nneedle\nneedle\n")  # 3 次
            code, out = _run_main(
                ["needle", "--scope", "code", "--path", tmp, "--count",
                 "--json"])
            self.assertEqual(code, 0)
            data = json.loads(out)
            results = data.get("results") or []
            files = [r.get("path") if isinstance(r, dict) else r[0]
                     for r in results]
            self.assertTrue(files, "无结果")
            self.assertIn("zzz.txt", files[0],
                          f"命中最多的文件必须排第一，实际 {files}")


if __name__ == "__main__":
    unittest.main()
