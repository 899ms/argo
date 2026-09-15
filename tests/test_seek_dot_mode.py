#!/usr/bin/env python3
"""检查 seek.py 的 --dot 开关：以 . 开头的目录和软链能不能被搜到

为什么要检查这个：本机的 skill 都放在 ~/.agents、~/.zcode 这类以 . 开头的
目录里，而且很多条目是软链（指向别处的真身）。rg 默认既不进这类目录，也不跟
软链，所以不加 --dot 时搜技能库永远是零结果，容易把「搜不到」当成「没有」。

这里守住两件事：

  ① 不加 --dot：以 . 开头的目录和软链里的文件搜不到；加了以后都搜得到。
  ② 加了 --dot 后，如果目录里有指向不存在文件的软链（rg 会因此报错返回 2），
     结构搜索仍然要给出结果，不能变成「没找到」。

不联网，只在本机临时目录里建几个文件来测。

运行：
  cd ~/.agents/skills/argo
  python3 -m pytest tests/test_seek_dot_mode.py -q
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import seek_locator  # noqa: E402

SEEK = seek_locator.resolve_seek_py()
TERM = "商单"  # 用来搜的中文词，内容本身不重要，能被搜到就行


def run_seek(*args):
    return subprocess.run([sys.executable, str(SEEK), *args],
                          capture_output=True, text=True, timeout=120)


@unittest.skipUnless(shutil.which("rg"), "本机没装 rg，跳过这项检查")
class TestSeekDotMode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 先解析成真实路径：macOS 上 /var 本身就是个软链，避免受它影响
        cls.root = Path(tempfile.mkdtemp(prefix="seek-dot-")).resolve()
        cls.scan = cls.root / "scan"
        cls.scan.mkdir()
        (cls.scan / "visible").mkdir()
        (cls.scan / "visible" / "plain.md").write_text(TERM, encoding="utf-8")
        (cls.scan / ".dotdir").mkdir()
        (cls.scan / ".dotdir" / "hidden.md").write_text(TERM, encoding="utf-8")
        # 软链指向搜索范围之外，保证这个文件只能靠「跟随软链」找到；
        # 如果指向范围里面，直接走真实路径就能搜到，这条就测不出什么了。
        outside = cls.root / "outside"
        outside.mkdir()
        (outside / "linked.md").write_text(TERM, encoding="utf-8")
        (cls.scan / "linksym").symlink_to(outside)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "root", None), ignore_errors=True)

    def test_dot_off_skips_hidden_and_symlink(self):
        """不加 --dot：普通目录里的文件搜得到，以 . 开头的目录和软链里的搜不到。"""
        p = run_seek(TERM, "--path", str(self.scan), "--count")
        self.assertIn("plain.md", p.stdout, f"普通文件应该搜得到：{p.stdout!r}")
        self.assertNotIn("hidden.md", p.stdout, f"以 . 开头的目录不该被搜进去：{p.stdout!r}")
        self.assertNotIn("linked.md", p.stdout, f"软链不该被跟进去：{p.stdout!r}")

    def test_dot_on_sees_hidden_and_symlink(self):
        """加上 --dot：三种文件都要搜得到。"""
        p = run_seek(TERM, "--path", str(self.scan), "--count", "--dot")
        self.assertIn("plain.md", p.stdout, p.stdout)
        self.assertIn("hidden.md", p.stdout, f"加了 --dot 应该能搜到 .dotdir 里的文件：{p.stdout!r}")
        self.assertIn("linked.md", p.stdout, f"加了 --dot 应该能跟软链：{p.stdout!r}")

    def test_dot_on_structural_survives_broken_symlink(self):
        """目录里有指向不存在文件的软链时，结构搜索仍要给出结果。"""
        d = self.root / "struct"
        d.mkdir(exist_ok=True)
        (d / "sample.py").write_text("try:\n    pass\nexcept:\n    pass\n",
                                     encoding="utf-8")
        (d / "dangling").symlink_to(d / "nope")
        for extra in ([], ["--dot"]):
            p = run_seek("裸except", "--path", str(d), "--structural", *extra)
            label = "加上 --dot" if extra else "不加 --dot"
            self.assertNotIn("未找到匹配", p.stdout,
                             f"{label} 时结构搜索变成了没找到：{p.stdout!r}")
            self.assertIn("sample.py", p.stdout, p.stdout)


if __name__ == "__main__":
    unittest.main()
