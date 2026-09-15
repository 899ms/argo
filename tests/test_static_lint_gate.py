#!/usr/bin/env python3
"""静态缺陷门禁：拦住「会让代码跑错或静默失效」的写法。

## 为什么需要这道门禁

本仓已有一批很讲究的一致性门禁（版本四端对账、CommonFlags 契约、环境变量
别名一致性、原生工具 schema 漂移），但**没有任何一道在检查代码本身的静态
缺陷**。2026-09-15 的审查用 ruff F 规则扫 108 个模块，扫出 5 处真缺陷：

- `mcp_transport.py` / `quota.py`：类型注解用了 `Any` 却从未导入。两文件都有
  `from __future__ import annotations`，注解不求值，所以**运行时一直没崩**——
  代价是 `typing.get_type_hints()` 直接抛 NameError、类型检查器对这个模块
  全线失效。这正是它能潜伏至今的原因。
- `cache.py`：`_DOMAIN_MAP` 里 `"english_tech"` 写了两遍。两次值相同，当前
  无行为差异，但日后只改其中一处就会静默失效——在 4522 行 config + 61 个
  spec 的规模下，这类重复靠肉眼几乎不可能发现。

这类缺陷的共同点：**不报错、不崩溃、只是悄悄不生效**。靠 code review 发现
它们的概率极低，应该由门禁兜住。

## 收了哪些规则

只收「会跑错 / 会静默失效」的，不收风格类：

| 规则 | 危害 |
|------|------|
| E9 | 语法与 IO 错误 |
| F821 | 未定义名 → 运行时 NameError |
| F822 | `__all__` 里的未定义名 |
| F823 | 局部变量引用前赋值 |
| F811 | 重定义（静默覆盖前一个） |
| F601 | 字典字面量重复键（静默覆盖） |
| F631 | `assert` 一个元组（恒为真，断言形同虚设） |
| F632 | 用 `==` 比较字面量（多为 `is` 笔误） |
| F701 / F702 | `break` / `continue` 在循环外 |
| F704 / F706 / F707 | `yield` / `return` / `except` 位置非法 |

**刻意不收** F401（未使用导入，存量 106 处）与 F841（未使用变量，17 处）：
`scripts/` 不是包，ruff 无法区分「真未使用」与「被其他模块 re-export」，
一刀切会打断导入链。已抽查确认 F841 多为无害冗余（算了未用），非逻辑错误，
这两类留待人工复核后另行决定。

## 扫描范围

`scripts/` + `tests/` + `bin/argo`。把 `tests/` 纳入是有实际收益的：扩展范围
当天就在 `test_tinyfish_fallback.py` 抓到一处 F821——`_isolate_envfile` 用了
`Path` 却没导入，因为写在 `lambda` 体内而一直没被求值（24 项测试全绿），一旦
有代码路径真的调用 `_envfile_path()` 就会当场 NameError。

## 双引擎设计

- **ruff**（环境有 uv / ruff 时）：覆盖上表全部规则，判定最准。
- **内建 ast**（零依赖）：**总会跑**，至少覆盖字典重复键。

两者都跑，任一报红即失败。内建引擎的意义是：即便在没装 ruff 的机器上，
门禁也不是形同虚设——它仍有牙齿，只是牙口浅一些。

两个引擎各自带变异验证（`test_gate_has_teeth_*`）：故意造一个坏样本，
断言检测逻辑确实抓得住。没有变异验证的门禁很容易写成恒真的摆设。

静态源码扫描，无网络。
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
TESTS = ROOT / "tests"
BIN = ROOT / "bin" / "argo"
TARGETS = [str(SCRIPTS), str(TESTS), str(BIN)]

# 只列「会跑错 / 会静默失效」的规则。基线：本文件落地时全仓零违规。
RULES = "E9,F821,F822,F823,F811,F601,F631,F632,F701,F702,F704,F706,F707"

# ruff 探测顺序：uvx（uv 自带）→ 模块方式 → PATH 上的可执行文件
RUFF_CANDIDATES = (
    ("uvx", "ruff"),
    (sys.executable, "-m", "ruff"),
    ("ruff",),
)


def _resolve_ruff() -> list[str] | None:
    """返回可用的 ruff 调用前缀；都不可用返回 None。"""
    for cand in RUFF_CANDIDATES:
        exe = shutil.which(cand[0]) or (cand[0] if os.path.isabs(cand[0]) else None)
        if not exe:
            continue
        try:
            r = subprocess.run([exe, *cand[1:], "--version"],
                               capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0:
            return [exe, *cand[1:]]
    return None


def _ruff_findings(targets: list[str]) -> list[str] | None:
    """跑 ruff，返回违规行列表；ruff 不可用返回 None。

    用 JSON 输出而非 concise：concise 在「无违规」时会打印
    "All checks passed!"，按行解析会把这句话当成一条发现——本门禁首次
    落地时就是这么红的（等于把「全绿」误判成「有问题」）。
    """
    prefix = _resolve_ruff()
    if prefix is None:
        return None
    r = subprocess.run(
        [*prefix, "check", *targets, "--select", RULES,
         "--no-cache", "--output-format", "json"],
        capture_output=True, text=True, timeout=300,
    )
    # ruff 无违规时 exit 0；有违规 exit 1；调用异常 exit 2（配置/用法错误）
    if r.returncode not in (0, 1):
        raise AssertionError(f"ruff 调用异常（rc={r.returncode}）：{r.stderr[:400]}")
    try:
        data = json.loads(r.stdout or "[]")
    except json.JSONDecodeError as e:
        raise AssertionError(f"ruff 输出无法解析：{e}；stdout={r.stdout[:200]!r}")
    return [
        "{}:{}:{} {} {}".format(
            Path(d.get("filename", "?")).name,
            d.get("location", {}).get("row", "?"),
            d.get("code", "?"),
            d.get("message", ""),
            f"(col {d['location']['column']})" if d.get("location", {}).get("column") else "",
        ).strip()
        for d in data
    ]


def _duplicate_dict_keys(paths: list[Path]) -> list[str]:
    """内建 ast 检测：字典字面量里的重复常量键。

    只认字面量常量键——`{k: 1, **other, k2: 2}` 这类动态键不在静态可判范围。
    零依赖引擎：没有任何外部工具时，这道门禁依然拦得住最典型的「静默覆盖」。
    """
    problems: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as e:  # 语法错误本身也是缺陷，交由 ruff/E9 报
            problems.append(f"{path.name}:{e.lineno} 语法错误：{e.msg}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            seen: set[object] = set()
            for key in node.keys:
                if not isinstance(key, ast.Constant):
                    continue
                if key.value in seen:
                    problems.append(
                        f"{path.name}:{node.lineno} 字典重复键 {key.value!r}"
                        f"（后者静默覆盖前者）")
                seen.add(key.value)
    return problems


def _iter_target_files() -> list[Path]:
    files = sorted(SCRIPTS.glob("*.py")) + sorted(TESTS.glob("*.py"))
    if BIN.is_file():
        files.append(BIN)
    return files


class TestStaticLintGate(unittest.TestCase):
    """静态缺陷门禁本体。"""

    def test_no_duplicate_dict_keys(self):
        """零依赖引擎：字典重复键（不依赖 ruff，任何环境都跑）。"""
        problems = _duplicate_dict_keys(_iter_target_files())
        self.assertEqual(
            problems, [],
            "字典字面量存在重复键（后者静默覆盖前者）：\n  "
            + "\n  ".join(problems))

    def test_no_ruff_findings(self):
        """ruff 引擎：覆盖 RULES 全量规则。ruff 不可用时跳过（不阻塞）。"""
        findings = _ruff_findings(TARGETS)
        if findings is None:
            self.skipTest("环境无 ruff（uv / ruff 均不可用），仅内建 ast 引擎生效")
        self.assertEqual(
            findings, [],
            "静态缺陷（会让代码跑错或静默失效）：\n  " + "\n  ".join(findings))

    # ── 变异验证：证明两个引擎都不是恒真的摆设 ────────────────────────────

    def test_gate_has_teeth_ast_engine(self):
        """造一个含重复键的样本，ast 引擎必须抓住。"""
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad_sample.py"
            bad.write_text(
                "MAP = {\n"
                "    'alpha': 'x',\n"
                "    'beta': 'y',\n"
                "    'alpha': 'z',\n"
                "}\n",
                encoding="utf-8")
            problems = _duplicate_dict_keys([bad])
        self.assertEqual(len(problems), 1, f"变异样本未被抓住：{problems}")
        self.assertIn("alpha", problems[0])

    def test_gate_has_teeth_ruff_engine(self):
        """造一个用未导入 `Any` 的样本，ruff 必须抓住（对应本次修复的缺陷）。"""
        if _resolve_ruff() is None:
            self.skipTest("环境无 ruff")
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad_annotation.py"
            bad.write_text(
                "from __future__ import annotations\n\n\n"
                "def f(x: dict[str, Any]) -> None:\n"
                "    pass\n",
                encoding="utf-8")
            findings = _ruff_findings([str(bad)])
        self.assertIsNotNone(findings)
        self.assertTrue(
            any("F821" in ln for ln in findings),
            f"变异样本未被抓住（应报 F821）：{findings}")


if __name__ == "__main__":
    unittest.main()
