#!/usr/bin/env python3
"""`argo fetch --focus` 旗标契约 + 文档/实现一致性检查。

背景（2026-09-14 实测）：SKILL.md、references/usage.md、docs/ARGO_INTRO.md
三处都把 `argo fetch URL --focus 关键词` 写成现成能力（usage.md 还承诺
「BM25 聚焦，省 token」），但入口 scripts/fetch_v3.py 的 argparse 没有该参数，
照文档执行直接报 `unrecognized arguments: --focus`——能力只实现于 MCP 侧。
`--use-browser` 同样只活在文档里（入口只认 `--browser`）。

本测试锁三件事：
  1. 文档里出现的 fetch 旗标必须都能被解析（防同类漂移复发）
  2. 聚焦语义在两处入口共用同一实现（focus_extract.apply_focus），
     不允许再各自写一份——历史上「各写一份」正是漏参数无人察觉的原因
  3. apply_focus 的记账契约：真裁剪才 focus_applied=True，
     正文过短时如实标注 False，不谎报已省 token
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_v3  # noqa: E402
from focus_extract import apply_focus, focus_extract  # noqa: E402

# 文档来源：这几处写的 fetch 用法就是对外承诺
DOC_FILES = ("SKILL.md", "references/usage.md", "docs/ARGO_INTRO.md")

# 每一轮都带唯一序号：整篇逐字重复会被收成样板块（运行页眉/页脚那一类），
# 聚焦遂无从选起——那样的夹具测的是置样规则，不是聚焦本身。
def _long_doc(rounds: int = 12) -> str:
    sections = (
        ("组合再平衡", "再平衡是控制风险敞口的手段，季度或阈值触发均可，代价是换手成本。"),
        ("债券", "利率敏感度由久期决定，短久期品种在加息周期更抗跌。"),
        ("久期", "久期衡量利率变动一单位时价格的变化幅度。"),
        ("估值", "自由现金流折现对假设极其敏感，安全边际来自买入价的折扣。"),
        ("交易成本", "换手率上升会吃掉超额收益，税费与冲击成本都要计入组合。"),
    )
    return "".join(
        f"# {title}（第 {i} 轮）\n\n{body}轮次 {i} 的补充说明。\n\n"
        for i in range(rounds) for title, body in sections
    )


_LONG = _long_doc()


def _documented_fetch_flags() -> set[str]:
    """从文档里抽出所有 `argo fetch ...` 行上声明的旗标。"""
    flags: set[str] = set()
    for rel in DOC_FILES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        for line in text.splitlines():
            if "argo fetch" not in line:
                continue
            flags.update(re.findall(r"--[a-z][a-z0-9-]*", line))
    return flags


class TestDocumentedFlagsExist(unittest.TestCase):
    """文档承诺的旗标必须真能被 argparse 接受。"""

    @staticmethod
    def _value_placeholders(parser) -> dict:
        """从 parser 推出「需要取值」的旗标及其合法占位值。

        两处都从 parser 推导，不维护手写清单：
          - 手写清单每加一个取值旗标就要同步，漏了就误报「旗标解析失败」
            （实测新增 --offset/--limit 时正是如此）
          - 占位值还必须与参数类型相容：`type=int` 的旗标喂字符串同样会
            解析失败，而这与文档是否正确毫无关系——门禁要测的是「文档写的
            旗标存在且可解析」，不是「占位值选得对不对」。
        """
        import argparse
        switch = (argparse._StoreTrueAction, argparse._StoreFalseAction,
                  argparse._CountAction, argparse._HelpAction)
        out: dict = {}
        for action in parser._actions:
            if not action.option_strings or isinstance(action, switch):
                continue
            ph = "1" if getattr(action, "type", None) is int else "占位值"
            for opt in action.option_strings:
                out[opt] = ph
        return out

    def test_documented_flags_parse(self):
        parser = fetch_v3.build_parser()
        documented = _documented_fetch_flags()
        self.assertTrue(documented, "未从文档抽到任何 argo fetch 旗标，门禁失效")
        value_flags = self._value_placeholders(parser)
        for flag in sorted(documented):
            with self.subTest(flag=flag):
                # 需要取值的旗标补一个类型相容的占位值；开关型旗标直接跟 URL
                argv = ([flag, value_flags[flag], "https://example.com/"]
                        if flag in value_flags
                        else [flag, "https://example.com/"])
                self.assertIsNotNone(parser.parse_args(argv))

    def test_use_browser_alias(self):
        parser = fetch_v3.build_parser()
        self.assertTrue(parser.parse_args(
            ["--use-browser", "https://example.com/"]).browser)
        self.assertTrue(parser.parse_args(
            ["--browser", "https://example.com/"]).browser)

    def test_focus_defaults(self):
        args = fetch_v3.build_parser().parse_args(["https://example.com/"])
        self.assertEqual(args.focus, "")
        self.assertEqual(args.focus_top, 5)


class TestApplyFocusContract(unittest.TestCase):
    """apply_focus 的记账契约。"""

    def test_long_content_is_trimmed_and_flagged(self):
        result = {"content": _LONG, "length": len(_LONG)}
        out = apply_focus(result, "债券 久期")
        self.assertTrue(out["focus_applied"])
        self.assertEqual(out["focus_query"], "债券 久期")
        self.assertEqual(out["length"], len(out["content"]))
        self.assertLess(out["length"], len(_LONG))

    def test_short_content_not_trimmed_and_flagged_false(self):
        result = {"content": "很短的正文。", "length": 6}
        out = apply_focus(result, "债券")
        self.assertFalse(out["focus_applied"])
        self.assertEqual(out["content"], "很短的正文。")
        self.assertEqual(out["length"], 6)

    def test_empty_query_noop(self):
        result = {"content": _LONG, "length": len(_LONG)}
        out = apply_focus(result, "")
        self.assertNotIn("focus_applied", out)
        self.assertEqual(out["content"], _LONG)

    def test_fetch_v3_helper_skips_failed_result(self):
        failed = {"content": _LONG, "length": len(_LONG), "success": False}
        out = fetch_v3._apply_focus_to_result(failed, "债券")
        self.assertNotIn("focus_applied", out)
        self.assertEqual(out["content"], _LONG)

    def test_fetch_v3_helper_applies_on_success(self):
        ok = {"content": _LONG, "length": len(_LONG), "success": True}
        out = fetch_v3._apply_focus_to_result(ok, "债券 久期")
        self.assertTrue(out["focus_applied"])

    def test_focus_extract_still_public(self):
        """apply_focus 是新增入口，focus_extract 本身仍是公开函数。"""
        self.assertTrue(focus_extract(_LONG, "债券") != _LONG)


class TestFocusSingleSource(unittest.TestCase):
    """CLI 与 MCP 必须共用同一份裁剪实现（源码级检查）。"""

    def _source(self, rel: str) -> str:
        return (ROOT / rel).read_text(encoding="utf-8")

    def test_cli_uses_shared_helper(self):
        self.assertIn("apply_focus", self._source("scripts/fetch_v3.py"),
                      "CLI 侧未接 focus_extract.apply_focus")

    def test_mcp_uses_shared_helper(self):
        src = self._source("scripts/mcp_handlers.py")
        self.assertIn("apply_focus", src,
                      "MCP 侧未接 focus_extract.apply_focus（两处会再次分叉）")
        self.assertNotIn(
            'focus_mod.focus_extract(result["content"], focus_query)', src,
            "MCP 侧仍保留自写的裁剪逻辑，应改为调用 apply_focus")


if __name__ == "__main__":
    unittest.main()
