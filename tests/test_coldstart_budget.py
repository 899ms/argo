#!/usr/bin/env python3
"""冷启动预算门禁（功能性断言，非计时，不会在慢机器上抖动）。

背景（2026-09-15 实测）：`import search` 曾要 ~2.1s，其中 ~1.9s 是纯浪费——
  1. cache.py 模块级 DEFAULT_DB_PATH = argo_paths.db_path()
     → get_cache_config() → load_config() 合并全部外置引擎 spec（~1.7s），
     只为读 cache.db_path 一个标量（改走 config.peek_cache_db_path 轻量读取）；
  2. route.py 模块级 _ENGINE_NAMES = _build_engine_names()
     → get_engines() → 同一次 load_config()，为了一张路由 reason 显示名表
     （改为惰性 _engine_display + PEP 562 __getattr__ 兼容旧入口）。
修复后 `import search` ≈ 0.55s（约 4×）。这两个 import 若回潮，每一次
CLI/MCP 调用（Agent 场景 = 每次都起新进程）都要白付这笔钱。

门禁思路：不断言耗时（慢机器/沙箱会误伤），断言**状态**——
import 完成后 config.load_config 不得被触发过（_config_cache 仍为 None）。
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _run_fresh(code: str) -> subprocess.CompletedProcess:
    """在干净子进程里执行代码（cwd=scripts，与 CLI 调用形态一致）。"""
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=SCRIPTS, capture_output=True, text=True, timeout=60,
    )


class TestColdStartBudget(unittest.TestCase):
    def test_import_route_does_not_load_full_config(self):
        r = _run_fresh(
            "import route, config; "
            "assert config._config_cache is None, "
            "'import route 不应触发 load_config（引擎显示名表必须惰性）'"
        )
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_cache_does_not_load_full_config(self):
        r = _run_fresh(
            "import cache, config; "
            "assert config._config_cache is None, "
            "'import cache 不应触发 load_config（db_path 必须走 peek 轻量读取）'"
        )
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_search_does_not_load_full_config(self):
        r = _run_fresh(
            "import search, config; "
            "assert config._config_cache is None, "
            "'import search 不应在 import 期合并外置引擎 spec'"
        )
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_peek_db_path_matches_full_load(self):
        """轻量读取与全量 load_config 的 db_path 语义必须一致（防分叉）。"""
        r = _run_fresh(
            "import config; "
            "lite = config.peek_cache_db_path(); "
            "full = config.get_cache_config().get('db_path'); "
            "assert (lite or None) == (full or None) or "
            "config.os.path.expanduser(lite or '') == (full or ''), "
            "f'peek 与全量加载的 db_path 不一致: {lite!r} vs {full!r}'"
        )
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_engine_names_pep562_compat(self):
        """旧入口 `from route import _ENGINE_NAMES` 惰性可用且非 None。"""
        r = _run_fresh(
            "from route import _ENGINE_NAMES; "
            "assert isinstance(_ENGINE_NAMES, dict) and _ENGINE_NAMES, "
            "'_ENGINE_NAMES 兼容入口必须惰性返回完整字典（PEP 562），拿到 None 即回归'"
        )
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
