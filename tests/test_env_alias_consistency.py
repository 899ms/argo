#!/usr/bin/env python3
"""环境变量别名一致性门禁：os.environ 直读旧密钥名必须同文件认识新名。

背景（issue #12，2026-09-13）：exa 专用 builder 只读 EXA_API_KEY，而
engine_env.KNOWN_ENV_ALIASES 的主推荐名是 ARGO_EXA_API_KEY。状态层按别名表
判 env_ready=True，builder 却取不到值 → 静默 0 结果（失败伪装成成功）。

门禁规则：scripts/*.py 中凡直读（os.environ.get / os.environ[..]）别名表里
的兼容旧名，同文件必须出现对应主推荐名（get_env([...]) 或显式双读均算）。
静态源码扫描，无网络。
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from engine_env import KNOWN_ENV_ALIASES  # noqa: E402

_DIRECT_READ = re.compile(
    r'os\.environ(?:\.get\(|\[)\s*["\']([A-Z][A-Z0-9_]{3,})["\']')


class TestEnvAliasConsistency(unittest.TestCase):
    def test_legacy_direct_reads_know_prefixed_primary(self):
        legacy = {}
        for chain in KNOWN_ENV_ALIASES.values():
            for n in chain[1:]:
                legacy.setdefault(n, chain[0])
        problems = []
        for p in sorted(SCRIPTS.glob("*.py")):
            src = p.read_text(encoding="utf-8")
            for m in _DIRECT_READ.finditer(src):
                name = m.group(1)
                primary = legacy.get(name)
                if primary and primary not in src:
                    problems.append(f"{p.name}: 直读旧名 {name} 但未认主推荐名 {primary}")
        self.assertEqual(problems, [], "别名不一致（#12 同类）:\n  " + "\n  ".join(problems))

    def test_gate_has_teeth(self):
        """变异验证：把 exa 行改回只读旧名，门禁必须报红。"""
        exa = SCRIPTS / "engines_builders_tech.py"
        src = exa.read_text(encoding="utf-8")
        patched = src.replace(
            'get_env(["ARGO_EXA_API_KEY", "EXA_API_KEY"])',
            'os.environ.get("EXA_API_KEY", "")').replace(
            "ARGO_EXA_API_KEY", "")  # 文案/注释里的新名提及一并抹掉
        self.assertNotEqual(patched, src, "变异源未生效，测试本身失效")
        exa.write_text(patched, encoding="utf-8")
        try:
            found = []
            s = exa.read_text(encoding="utf-8")
            for m in _DIRECT_READ.finditer(s):
                if m.group(1) == "EXA_API_KEY" and "ARGO_EXA_API_KEY" not in s:
                    found.append(m.group(1))
            self.assertTrue(found, "变异后门禁未捕获——门禁无牙")
        finally:
            exa.write_text(src, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
