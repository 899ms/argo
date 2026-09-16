#!/usr/bin/env python3
"""min_version_scan 的单元测试。

这个模块从「静态门禁里的一段逻辑」提出来独立成件（见 scripts/min_version_scan.py
的 docstring），所以它值得有自己的用例：判据很容易写得「看起来对」——比如用逐行
正则找 `X | Y`，实测会在 `re.I|re.S`、`os.O_CREAT | os.O_RDWR`、文档示例上
一口气误报 14 处。

因此本文件的重心是**误报**：每条规则都配「必须抓」与「必须不抓」两侧。
只在一边下功夫的门禁，最后都会因为噪音被豁免掉。

零依赖、不联网、不写盘。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import min_version_scan as mvs  # noqa: E402


class TestReadMinPython(unittest.TestCase):
    def test_reads_min_python_from_bin_argo(self):
        minver = mvs.read_min_python(ROOT / "bin" / "argo")
        self.assertEqual(len(minver), 2)
        self.assertGreaterEqual(minver, (3, 8), f"MIN_PYTHON 异常：{minver}")
        self.assertLess(minver, (4, 0))

    def test_missing_file_returns_zero(self):
        self.assertEqual(mvs.read_min_python("/nonexistent/argo"), (0, 0))

    def test_file_without_declaration_returns_zero(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "argo"
            p.write_text("#!/usr/bin/env python3\nprint('hi')\n", encoding="utf-8")
            self.assertEqual(mvs.read_min_python(p), (0, 0))


class TestPep701(unittest.TestCase):
    """f-string 嵌套同类引号（3.12+；3.9~3.11 是 SyntaxError）。"""

    def test_catches_nested_same_quote(self):
        bad = 'x = f"Bearer {get_env(["A_KEY", "B_KEY"])}"\n'
        found = mvs.scan_text(bad, (3, 9))
        self.assertTrue(found, "PEP 701 写法没被抓住")
        self.assertIn("3.12", found[0][1])

    def test_safe_refactor_not_flagged(self):
        """先取变量再拼接——这正是本次 P0 的修法，必须放行。"""
        good = 'tok = get_env(["A_KEY"])\nx = f"Bearer {tok}"\n'
        self.assertEqual(mvs.scan_text(good, (3, 9)), [])

    def test_heterogeneous_quotes_are_legal(self):
        """外层 `'` 内层 `"` 在 3.9 上合法，不得误报。"""
        for legal in ('x = f\'{"quoted"}\'\n',
                      "x = f'{d[\"k\"]}'\n"):
            self.assertEqual(mvs.scan_text(legal, (3, 9)), [],
                             f"异类引号被误报：{legal!r}")

    def test_not_flagged_when_minver_allows(self):
        """最低版本 ≥3.12 时该写法合法。

        注意：若当前解释器本身 < 3.12，它连 parse 都做不到，此时报「语法错误」
        是诚实的结论（该文件在这台机器上确实跑不了）。所以这里分流断言。
        """
        bad = 'x = f"Bearer {get_env(["A"])}"\n'
        if sys.version_info >= (3, 12):
            self.assertEqual(mvs.scan_text(bad, (3, 12)), [])
        else:
            found = mvs.scan_text(bad, (3, 12))
            self.assertTrue(found, "低版本解释器下至少应报语法错误")
            self.assertNotIn("PEP 701", found[0][1],
                             "minver≥3.12 时不该再定性为 PEP 701 越界")


class TestPep604(unittest.TestCase):
    """类型联合 `X | Y`（3.10+）。"""

    def test_catches_module_level_annotation_without_future(self):
        ann = "CACHE: dict[str, tuple[int, int] | None] = {}\n"
        found = mvs.scan_text(ann, (3, 9))
        self.assertTrue(found, "无 future import 的 PEP604 注解没被抓住")
        self.assertIn("PEP 604", found[0][1])

    def test_future_import_makes_it_legal(self):
        """有 `from __future__ import annotations` 时注解不求值，3.9 能过。"""
        src = ("from __future__ import annotations\n"
               "def f(x: int | None) -> str | None:\n    return None\n")
        self.assertEqual(mvs.scan_text(src, (3, 9)), [])

    def test_iterable_union_would_need_more_than_future(self):
        """`tuple[dict | None, ...]` 这种在 typing 里需要 3.10；有 future 时由
        注解不求职兜住，本条只断言不崩、结论稳定。"""
        src = ("from __future__ import annotations\n"
               "T = tuple[dict | None, ...]\n")
        self.assertEqual(mvs.scan_text(src, (3, 9)), [])

    def test_bit_or_not_flagged(self):
        """位或都是合法 3.9 语法，一律不得误报。"""
        for safe in (
            "flags = READ | WRITE\n",
            "frame.append(0x80 | length)\n",
            "fd = os.open(p, os.O_CREAT | os.O_RDWR, 0o600)\n",
            "mode = stat.S_IRUSR | stat.S_IXUSR\n",
            "v = a | b\n",
        ):
            self.assertEqual(mvs.scan_text(safe, (3, 9)), [],
                             f"位或被误报：{safe!r}")

    def test_regex_alternation_not_flagged(self):
        """正则里的 `(a|b)` 与 `re.I|re.S` 最容易被正则式判据误伤。"""
        for safe in (
            'p = re.compile(r"(hrss|rsj|rlsbj)")\n',
            "m = re.search(r'<title>(.*?)</title>', html, re.I|re.S)\n",
            'r"(Jan|Feb|Mar)[a-z]*"\n',
        ):
            self.assertEqual(mvs.scan_text(safe, (3, 9)), [],
                             f"正则交替被误报：{safe!r}")

    def test_set_union_not_flagged(self):
        """集合并 `{a} | {b}` 是合法 3.9 语法。"""
        for safe in (
            "s = {a, b} | {c}\n",
            'w = set(GENERAL) | {"local_search"}\n',
        ):
            self.assertEqual(mvs.scan_text(safe, (3, 9)), [],
                             f"集合并被误报：{safe!r}")

    def test_markdown_table_in_docstring_not_flagged(self):
        """docstring 里的 `A | B` 表格不是代码。"""
        src = ('"""说明\n\n| a | b |\n|---|---|\n| medical | clinicaltrials |\n"""\n'
               "x = 1\n")
        self.assertEqual(mvs.scan_text(src, (3, 9)), [],
                         "docstring 表格被误报")

    def test_str_subscript_union_flagged(self):
        """str[...] 与 X | None 组合（真类型）应报出。"""
        src = "def f():\n    x: dict[str, int | None] = {}\n    return x\n"
        self.assertTrue(mvs.scan_text(src, (3, 9)), "dict[...] 内的 PEP604 未报出")


class TestMatch(unittest.TestCase):
    def test_not_flagged_when_match_is_not_used(self):
        self.assertEqual(mvs.scan_text("x = 1\n", (3, 9)), [])

    def test_match_flagged_when_present(self):
        # 只有 3.10+ 的解析器能产出 Match 节点；在更老的解释器上这条退化为
        # 不报（并已由 ast.parse 自身的 SyntaxError 兜住）。
        src = "def f(x):\n    match x:\n        case 1:\n            return 1\n"
        if getattr(__import__("ast"), "Match", None) is None:
            self.skipTest("当前解释器 < 3.10，无法产出 Match 节点")
        found = mvs.scan_text(src, (3, 9))
        self.assertTrue(found, "match 语句没被抓住")

    def test_not_flagged_when_minver_allows(self):
        """最低版本 ≥3.10 时 match 合法。

        若当前解释器 < 3.10，它 parse 不了 match，报语法错误是诚实结论；
        但不能把它错报成「违反最低版本」。
        """
        src = "def f(x):\n    match x:\n        case 1:\n            return 1\n"
        if getattr(__import__("ast"), "Match", None) is None:
            found = mvs.scan_text(src, (3, 10))
            self.assertTrue(found, "低版本解释器下至少应报语法错误")
            self.assertNotIn("match", found[0][1],
                             "minver≥3.10 时不该定性为 match 越界")
            return
        self.assertEqual(mvs.scan_text(src, (3, 10)), [])


class TestScanPaths(unittest.TestCase):
    def test_scans_directory_and_reports_relative(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "pkg"
            d.mkdir()
            (d / "bad.py").write_text('x = f"a {d["k"]} b"\n', encoding="utf-8")
            (d / "ok.py").write_text("x = 1\n", encoding="utf-8")
            problems = mvs.scan_paths([d], (3, 9))
        self.assertEqual(len(problems), 1, f"预期只报 bad.py：{problems}")
        self.assertIn("bad.py", problems[0])

    def test_clean_tree_reports_nothing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "ok.py").write_text("import os\nx = os.O_CREAT | os.O_RDWR\n",
                                     encoding="utf-8")
            self.assertEqual(mvs.scan_paths([d], (3, 9)), [])


class TestSelfConsistency(unittest.TestCase):
    """本模块与它守护的仓库必须自洽。"""

    def test_repo_declares_min_python(self):
        self.assertGreaterEqual(mvs.read_min_python(ROOT / "bin" / "argo"), (3, 8))

    def test_repo_source_is_scannable_as_text(self):
        """元用例：扫描器本身必须能被目标版本解析，否则它守护别人时自己先崩。"""
        self.assertEqual(mvs.scan_text(
            (ROOT / "scripts" / "min_version_scan.py").read_text(encoding="utf-8"),
            (3, 9)), [])


if __name__ == "__main__":
    unittest.main()
