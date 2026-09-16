#!/usr/bin/env python3
"""负向路由控制门（2026-09-14）：泛查询不得误触发垂直域/垂直引擎。

对照 OpenAI《Testing Agent Skills Systematically with Evals》的 test-04
负向控制（should_trigger=false）：skill 的 description 太宽会误吃相邻
请求；路由层同理——不含垂直意图词的日常查询若被路由进行情/漏洞/影视
等垂直引擎，返回的全是不相关结果，且早停会让误路由难以自愈。

用例唯一来源在 scripts/matrix_search_eval.py ROUTE_MATRIX
（scenario="negative"），本文件把它们钉进 pytest 套件，防止离线矩阵
脚本与单测漂移。全部离线（mock 熔断/配额，无网络）。

运行：
  python3 -m pytest tests/test_route_negative.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from matrix_search_eval import ROUTE_MATRIX, _route_ok  # noqa: E402
from route import route_query  # noqa: E402


def _negative_cases() -> list[dict[str, Any]]:
    return [c for c in ROUTE_MATRIX if c.get("scenario") == "negative"]


class TestRouteNegativeControls(unittest.TestCase):
    """泛查询 → 垂直域/垂直引擎 = 负向失败。"""

    def test_negative_case_inventory(self):
        cases = _negative_cases()
        self.assertGreaterEqual(
            len(cases), 5,
            "负向控制用例被清空——test-04 纪律要求泛查询至少 5 条钉住")
        ids = {c["id"] for c in cases}
        # 中英文各至少一条（语言检测两侧都要守住）
        self.assertTrue(any(i.startswith("N_en_") for i in ids),
                        "缺英文负向用例")
        self.assertTrue(any(i.startswith("N_zh_") for i in ids),
                        "缺中文负向用例")

    def test_general_queries_avoid_vertical_domains_and_engines(self):
        cases = _negative_cases()
        self.assertTrue(cases, "ROUTE_MATRIX 缺 scenario=negative 用例")
        for case in cases:
            with self.subTest(id=case["id"], q=case["q"]):
                with patch("circuit_breaker.get_breaker",
                           return_value=MagicMock()), \
                     patch("quota.get_quota_manager",
                           return_value=MagicMock()):
                    d = route_query(case["q"], mode="auto", depth="fast",
                                    context="search")
                ok, detail = _route_ok(case, d)
                self.assertTrue(
                    ok,
                    f"{case['id']} 负向控制失败（泛查询被垂直域/引擎误抢）: "
                    f"{detail}")


if __name__ == "__main__":
    unittest.main()
