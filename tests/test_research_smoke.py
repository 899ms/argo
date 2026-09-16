#!/usr/bin/env python3
"""research 管线族的冒烟门（2026-09-16 盘点发现该族无任何直测引用）。

research_cli/social_research/research_expand/research_strategy 此前零测试覆盖；
完整研究链依赖网络，不适合单测。本文件只锁三件不依赖网络的事：
  1. 五个模块可导入且入口函数存在（拆分后 import 链断裂是最高频回归形态）；
  2. research_cli --help 正常退出（argparse 层语法防回归）；
  3. 报告落盘目录行为（persist 无副作用契约归 DSH 插件测试管，此处不重复）。
"""
import os
import subprocess
import sys

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)


def test_research_modules_importable():
    import research  # noqa: F401
    import research_cli  # noqa: F401
    import research_expand  # noqa: F401
    import research_report  # noqa: F401
    import research_strategy  # noqa: F401
    import social_research  # noqa: F401


def test_deep_research_entry_exists():
    import research
    assert callable(research.deep_research)
    assert callable(research.social_sentiment_research)


def test_research_cli_help_exits_zero():
    out = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "research_cli.py"), "--help"],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    assert "用法" in (out.stdout + out.stderr) or "usage" in (out.stdout + out.stderr).lower()


@pytest.mark.parametrize("mod", ["time_utils", "query_signals", "stage_timing", "engine_dispatch"])
def test_extracted_modules_importable(mod):
    """8f62952 拆出的四模块：import 链断裂防回归（按名直测，不依赖间接路径）。"""
    __import__(mod)
