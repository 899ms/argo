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

import os
import re
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from engine_env import KNOWN_ENV_ALIASES, env_flag  # noqa: E402

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
        """故意造错验证：把 exa 行改回只读旧名，门禁必须报红。"""
        exa = SCRIPTS / "engines_builders_tech.py"
        src = exa.read_text(encoding="utf-8")
        patched = src.replace(
            'get_env(["ARGO_EXA_API_KEY", "EXA_API_KEY"])',
            'os.environ.get("EXA_API_KEY", "")').replace(
            "ARGO_EXA_API_KEY", "")  # 文案/注释里的新名提及一并抹掉
        self.assertNotEqual(patched, src, "造错样本没生效，测试本身失效")
        exa.write_text(patched, encoding="utf-8")
        try:
            found = []
            s = exa.read_text(encoding="utf-8")
            for m in _DIRECT_READ.finditer(s):
                if m.group(1) == "EXA_API_KEY" and "ARGO_EXA_API_KEY" not in s:
                    found.append(m.group(1))
            self.assertTrue(found, "造错之后检查没抓住——等于没检查")
        finally:
            exa.write_text(src, encoding="utf-8")


class TestAuthorizationFlagExactName(unittest.TestCase):
    """授权开关只认字面名：裸名不得授权（别名展开是给密钥的便利，不是给授权位的）。

    缺陷形态（2026-09-15 审查发现）：ARGO_ALLOW_RECOMPUTE 是「允许在受限子进程
    里执行脚本」的放行位，而 env_flag 会把单名展开成「原名 + 去前缀裸名」，
    于是环境里任何一个工具随手设的 `ALLOW_RECOMPUTE=1` 就等于替用户放行。
    """

    AUTH = "ARGO_ALLOW_RECOMPUTE"
    BARE = "ALLOW_RECOMPUTE"

    def _set(self, **kv):
        saved = {k: os.environ.get(k) for k in kv}
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return saved

    def _restore(self, saved):
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_bare_name_does_not_authorize(self):
        saved = self._set(**{self.AUTH: None, self.BARE: "1"})
        try:
            self.assertFalse(
                env_flag(self.AUTH, default=False, expand=False, strict=True),
                "裸名 ALLOW_RECOMPUTE=1 竟然放行了授权动作——授权只认 ARGO_ 前缀名")
        finally:
            self._restore(saved)

    def test_prefixed_name_authorizes(self):
        saved = self._set(**{self.AUTH: "1", self.BARE: None})
        try:
            self.assertTrue(env_flag(self.AUTH, default=False, expand=False,
                                     strict=True))
        finally:
            self._restore(saved)

    def test_unparseable_values_do_not_authorize(self):
        """授权位只认明确真值：拼错/无法解释的写法一律算关。

        默认口径「非关即开」对能力开关没问题（最坏换个行为），对授权位就是
        「任何拼错的值都放行」——`0x0`、`maybe`、`ture` 都曾等于授权。
        """
        for v in ("0x0", "maybe", "ture", "2", "on?", "是的"):
            saved = self._set(**{self.AUTH: v})
            try:
                self.assertFalse(
                    env_flag(self.AUTH, default=False, expand=False, strict=True),
                    f"{self.AUTH}={v!r} 竟然放行了授权动作")
            finally:
                self._restore(saved)

    def test_off_values_still_respected(self):
        """口径统一：授权位也要认 off/no/false（env_flag 的统一关值集合）。"""
        for v in ("0", "false", "off", "no", "disabled", ""):
            saved = self._set(**{self.AUTH: v})
            try:
                self.assertFalse(
                    env_flag(self.AUTH, default=False, expand=False, strict=True),
                    f"{self.AUTH}={v!r} 应算关")
            finally:
                self._restore(saved)

    def test_capability_switch_keeps_alias_tolerance(self):
        """能力开关不受影响：裸名写法继续生效（这是既有约定，别顺手一起收紧）。"""
        saved = self._set(ARGO_FETCH_JINA=None, FETCH_JINA="0")
        try:
            self.assertFalse(env_flag("ARGO_FETCH_JINA"),
                             "能力开关的裸名写法被误伤——它只影响降级行为，不是授权")
        finally:
            self._restore(saved)


if __name__ == "__main__":
    unittest.main()
