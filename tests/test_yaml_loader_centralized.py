#!/usr/bin/env python3
"""test_yaml_loader_centralized.py — YAML 加载器真源门禁。

## 守的是什么（2026-09-15 冷启动优化）

`yaml.safe_load()` 恒走纯 Python 扫描器：解析 123 KB 的 config.yaml 实测
79 ms，而语义相同的 libyaml C 版（CSafeLoader）只要 10 ms。此前 8 个模块各自
直接调 `yaml.safe_load`，「用哪个 loader」这个决定被复制了 8 份，没有任何一处
能统一改进——与 fetch 的 `--focus` 参数同一种失败模式（各写一份 → 漏改没人
发现）。修法是把 loader 选择收口到 `yaml_load.py`，本文件把它钉住。

## 附带钉住的两个契约

`config.peek_cache_db_path()` 的文档承诺「配置损坏时 fail-open 返回 None」，
但它只 catch `(ImportError, ValueError)`——而 `yaml.YAMLError` **不是**
ValueError 的子类，损坏配置实际会抛 `yaml.parser.ParserError`（2026-09-15
实测确认）。文档说的和代码做的不一致，正是本仓最该被门禁抓住的那类漂移。

同时它曾在一进程内被连调 4 次（quota / adaptive / argo_engine_registry /
cache 各自的模块级常量），每次重解析一遍 123 KB 配置。现按
(mtime_ns, size) 记忆化，本文件锁住「解析至多一次」与「失败不写记忆」。
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import config  # noqa: E402
import yaml_load  # noqa: E402

# 允许直接调 yaml 的唯一文件（loader 选择的真源）
_LOADER_SOURCE = "yaml_load.py"


def _load_argo_bin():
    """按文件路径加载 bin/argo（无 .py 后缀，需显式指定 loader）。"""
    from importlib.machinery import SourceFileLoader
    path = ROOT / "bin" / "argo"
    spec = importlib.util.spec_from_file_location(
        "argo_bin_under_test", path, loader=SourceFileLoader("argo_bin_under_test", str(path)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestLoaderIsCentralized:
    """源码门禁：除 yaml_load.py 外不得直接调 yaml.safe_load / yaml.load。"""

    def test_no_direct_yaml_load_outside_loader(self):
        import re
        # 同时挡三种绕过：
        #   ① yaml.safe_load( / yaml.full_load( …（直接属性调用）
        #   ② from yaml import safe_load 后的裸 safe_load( （本仓共享入口叫
        #      loads/load，出现 *_load( 就是绕过了 yaml_load）
        #   ③ from yaml import safe_load as _sl（改名后调用点看不出来，
        #      只能从导入语句挡）
        pattern = re.compile(
            r"\b(?:safe_load|unsafe_load|full_load)\s*\("
            r"|yaml\.\w*load\s*\("
            r"|from\s+yaml\s+import\s+[^\n]*\b(?:safe_load|unsafe_load|full_load|load)\b"
        )
        offenders = {}
        for path in sorted(SCRIPTS.glob("*.py")):
            if path.name == _LOADER_SOURCE:
                continue
            src = path.read_text(encoding="utf-8")
            # 去注释与文档字符串后扫描，避免把说明文字判成调用
            stripped = re.sub(r'""".*?"""', "", src, flags=re.S)
            stripped = re.sub(r"^\s*#.*$", "", stripped, flags=re.M)
            hits = [
                f"L{i}: {ln.strip()[:70]}"
                for i, ln in enumerate(stripped.splitlines(), 1)
                if pattern.search(ln)
            ]
            if hits:
                offenders[path.name] = hits
        assert not offenders, (
            f"这些文件绕过了 yaml_load（loader 选择必须单一真源，"
            f"否则无法统一改进/审计）: {offenders}")


class TestLoaderChoice:
    def test_prefers_c_loader_when_available(self):
        import yaml
        assert hasattr(yaml, "CSafeLoader"), "本环境无 libyaml，本断言无意义"
        assert yaml_load.safe_loader() is yaml.CSafeLoader

    def test_falls_back_when_c_loader_missing(self, monkeypatch):
        import yaml
        fake = types.SimpleNamespace(SafeLoader=yaml.SafeLoader)
        monkeypatch.setattr(yaml_load, "_yaml", lambda: fake)
        assert yaml_load.safe_loader() is yaml.SafeLoader

    def test_parses_shapes(self):
        assert yaml_load.loads("a: 1\nb: [2, 3]\n") == {"a": 1, "b": [2, 3]}
        assert yaml_load.loads("- 1\n- 2\n") == [1, 2]
        assert yaml_load.loads("") is None

    def test_load_reads_file_utf8(self, tmp_path):
        p = tmp_path / "x.yaml"
        p.write_text("名称: 中文值\n", encoding="utf-8")
        assert yaml_load.load(p) == {"名称": "中文值"}

    def test_malformed_raises(self):
        import yaml
        with pytest.raises(yaml.YAMLError):
            yaml_load.loads("a: [未闭合\n")


class TestPeekCacheDbPathContract:
    """peek 的契约：解析至多一次、失败不写记忆、损坏配置 fail-open。"""

    @pytest.fixture(autouse=True)
    def _reset_memo(self, monkeypatch):
        monkeypatch.setattr(config, "_peek_db_path_cache", None)

    def test_parses_at_most_once_per_process(self, monkeypatch):
        n = [0]
        real = config._peek_cache_db_path_read

        def counting():
            n[0] += 1
            return real()

        monkeypatch.setattr(config, "_peek_cache_db_path_read", counting)
        vals = [config.peek_cache_db_path() for _ in range(4)]
        assert n[0] == 1, f"解析了 {n[0]} 次（应记忆化到 1 次）——冷启动链上会 ×4"
        assert len(set(vals)) == 1, "四次调用结果不一致"

    def test_memo_invalidated_on_config_change(self, monkeypatch, tmp_path):
        p = tmp_path / "c.yaml"
        p.write_text("cache:\n  db_path: /tmp/one/x.db\n", encoding="utf-8")
        monkeypatch.setattr(config, "CONFIG_PATH", p)
        assert config.peek_cache_db_path() == "/tmp/one/x.db"
        # 改内容（mtime/size 变）后必须重读，不能返回陈旧路径
        p.write_text("cache:\n  db_path: /tmp/two/longer-name.db\n", encoding="utf-8")
        assert config.peek_cache_db_path() == "/tmp/two/longer-name.db"

    def test_corrupt_config_fails_open(self, monkeypatch, tmp_path):
        """文档承诺：配置损坏时 fail-open 返回 None（此前会抛 ParserError）。"""
        p = tmp_path / "bad.yaml"
        p.write_text("cache:\n  db_path: [未闭合\n  broken: :\n", encoding="utf-8")
        monkeypatch.setattr(config, "CONFIG_PATH", p)
        assert config.peek_cache_db_path() is None

    def test_failure_is_not_memoized(self, monkeypatch):
        """解析链不可用时不写记忆——一次瞬时故障不该固化成永久 None。"""
        calls = [0]

        def boom():
            calls[0] += 1
            return False, None

        monkeypatch.setattr(config, "_peek_cache_db_path_read", boom)
        assert config.peek_cache_db_path() is None
        assert config.peek_cache_db_path() is None
        assert calls[0] == 2, "失败被记忆化了，后续调用不再重试"

    def test_missing_config_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "不存在.yaml")
        assert config.peek_cache_db_path() is None


class TestArgoInterpreterCache:
    """bin/argo 的解释器探测缓存：命中不重探、失败/异常缓存要退化为重探。"""

    @pytest.fixture
    def argo_bin(self, monkeypatch, tmp_path):
        mod = _load_argo_bin()
        monkeypatch.setattr(mod, "_PY_CACHE", str(tmp_path / ".argo_python"))
        return mod

    def test_remember_then_read(self, argo_bin, monkeypatch):
        monkeypatch.setattr(argo_bin, "_probe_python", lambda p, strict: True)
        argo_bin._remember_python(sys.executable)
        assert argo_bin._cached_python() == sys.executable

    def test_garbage_cache_is_ignored(self, argo_bin, tmp_path, monkeypatch):
        p = tmp_path / "garbage"
        p.write_text("not-a-real-interpreter", encoding="utf-8")
        monkeypatch.setattr(argo_bin, "_PY_CACHE", str(p))
        assert argo_bin._cached_python() == ""

    def test_cache_validated_before_use(self, argo_bin, monkeypatch):
        """缓存里的解释器若校验不过（被删/降级），必须回落而不是盲信。"""
        monkeypatch.setattr(argo_bin, "_probe_python", lambda p, strict: False)
        argo_bin._remember_python(sys.executable)
        assert argo_bin._cached_python() == ""

    def test_cache_hit_skips_full_deps_import(self, argo_bin, monkeypatch):
        """命中路径必须用廉价校验（strict=False），不再真 import 依赖。"""
        seen = []
        monkeypatch.setattr(argo_bin, "_probe_python",
                            lambda p, strict: (seen.append(strict), True)[1])
        argo_bin._remember_python(sys.executable)
        assert argo_bin._cached_python() == sys.executable
        assert seen == [False], f"命中路径走了严格探测（{seen}）——省不到时间"

    def test_argo_python_override_bypasses_cache(self, argo_bin, monkeypatch):
        """显式 ARGO_PYTHON 是权威：不查缓存也不写缓存。"""
        written = []
        monkeypatch.setattr(argo_bin, "_probe_python", lambda p, strict: True)
        monkeypatch.setattr(argo_bin, "_remember_python", lambda p: written.append(p))
        monkeypatch.setenv("ARGO_PYTHON", sys.executable)
        assert argo_bin._pick_python() == sys.executable
        assert written == [], "显式覆盖被写进了缓存，会遮蔽后续的覆盖"

    def test_cheap_check_code_is_version_and_dep_gated(self, argo_bin):
        """廉价校验必须同时卡版本与依赖存在性，否则缓存会放行坏环境。"""
        code = argo_bin._PY_CHEAP_CHECK
        flat = code.replace(" ", "")
        assert "version_info" in code and "(3,10)" in flat
        assert "find_spec" in code and "yaml" in code and "requests" in code
        # 不能真 import（那正是要省掉的成本）
        assert "import yaml" not in code and "import requests" not in code


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
