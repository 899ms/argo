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
        assert hasattr(yaml, "CSafeLoader"), "本环境无 libyaml，这条检查无意义"
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
    """peek 的契约：解析至多一次、失败不写记忆、损坏配置 fail-open。

    这里的用例都关掉落盘缓存（ARGO_CONFIG_CACHE=0）：要测的是**解析链**的
    记忆化与失败语义，而落盘缓存命中时压根不解析（那条路径由
    TestConfigDiskCache 覆盖）。
    """

    @pytest.fixture(autouse=True)
    def _reset_memo(self, monkeypatch):
        monkeypatch.setenv("ARGO_CONFIG_CACHE", "0")
        monkeypatch.setattr(config, "_parsed_yaml_cache", None)

    def test_parses_at_most_once_per_process(self, monkeypatch):
        n = [0]
        real = config._load_yaml

        def counting(text):
            n[0] += 1
            return real(text)

        # 计「真解析」而不是「入口被调用」：记忆化命中时入口仍会被调一次，
        # 但不会走到 _load_yaml——那才是要消灭的重复劳动。
        monkeypatch.setattr(config, "_load_yaml", counting)
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
            return False, None, None

        monkeypatch.setattr(config, "_read_config_yaml", boom)
        assert config.peek_cache_db_path() is None
        assert config.peek_cache_db_path() is None
        assert calls[0] == 2, "失败被记忆化了，后续调用不再重试"

    def test_missing_config_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "不存在.yaml")
        assert config.peek_cache_db_path() is None


class TestConfigParseSharedWithLoad:
    """peek 与 load_config 共用同一次解析——同一份 123 KB 文本此前解析两遍。"""

    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        monkeypatch.setenv("ARGO_CONFIG_CACHE", "0")
        monkeypatch.setattr(config, "_parsed_yaml_cache", None)
        monkeypatch.setattr(config, "_config_cache", None)
        monkeypatch.setattr(config, "_config_mtime", 0.0)

    def test_peek_then_load_parses_once(self, monkeypatch):
        n = [0]
        real = config._load_yaml

        def counting(text):
            n[0] += 1
            return real(text)

        monkeypatch.setattr(config, "_load_yaml", counting)
        db = config.peek_cache_db_path()
        cfg = config.load_config()
        assert n[0] == 1, (
            f"config.yaml 真解析了 {n[0]} 次（peek + load_config 应共用一份）")
        assert cfg.get("engines"), "共享解析结果后配置仍须完整"
        assert db == config.peek_cache_db_path(), "两次 peek 口径不一致"

    def test_shared_parse_does_not_leak_mutations(self, monkeypatch):
        """共享的是「原样解析结果」，load_config 的归一化必须作用在副本上。

        否则 ~ 展开、相对路径解析、外置合并会写进缓存对象，同一进程里后续
        peek 会看到被改过的值——缓存不得成为隐式的共享可变状态。
        """
        config.peek_cache_db_path()
        before = config._read_config_yaml()[1]
        snapshot = repr(before)
        config.load_config()
        after = config._read_config_yaml()[1]
        assert repr(after) == snapshot, "load_config 改写了共享的解析结果"


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
        """廉价校验必须同时卡版本与依赖存在性，否则缓存会放行坏环境。

        依赖只卡 PyYAML：它是 argo 唯一的必需第三方依赖（install.sh 装它）。
        此前这里检查必须含 requests——那是幽灵依赖（全仓无 `import requests`，
        真实可选增强是 curl_cffi）。要求一个不存在的包会让每台新机器的解释器
        探测全部失败、缓存永不命中，恰好害了它想保护的东西。
        """
        code = argo_bin._PY_CHEAP_CHECK
        flat = code.replace(" ", "")
        assert "version_info" in code and "(3,10)" in flat
        assert "find_spec" in code and "yaml" in code
        assert "requests" not in code, "幽灵依赖：仓库里没有任何 import requests"
        # 不能真 import（那正是要省掉的成本）
        assert "import yaml" not in code and "import requests" not in code

    def test_self_capable_does_not_require_requests(self, argo_bin, monkeypatch):
        """原地执行判定与廉价校验同一口径：只卡版本与 PyYAML，不卡 requests。

        重现的事故：照 install.sh 只装 pyyaml 的正常机器并没有 requests，旧判定
        因此恒为 False，每次 CLI 都被迫重选解释器并重启（实测每次多约 96ms），
        「省掉重复启动」的优化在终端用户机器上悄悄失效，只在装了 requests 的
        开发机上才生效。
        """
        import importlib.util
        import inspect
        import re
        # 源码层面：不允许再用 find_spec 去查 requests（注释里可以解释来龙去脉，
        # 但判定条件里不能真的去要这个包）
        src = inspect.getsource(argo_bin._self_capable)
        assert not re.search(r"find_spec\([\"']requests[\"']\)", src), \
            "幽灵依赖 requests 不得作为原地执行的判定条件"
        # 行为层面：PyYAML 在、requests 查不到时，当前高版本解释器仍可原地执行
        real_find = importlib.util.find_spec

        def _without_requests(name, *args, **kwargs):
            if name == "requests":
                return None
            return real_find(name, *args, **kwargs)

        monkeypatch.setattr(importlib.util, "find_spec", _without_requests)
        assert argo_bin._self_capable() is True

    def test_self_capable_still_needs_yaml(self, argo_bin, monkeypatch):
        """去掉 requests 门槛不等于没有门槛：缺 PyYAML 时仍不能原地执行。"""
        import importlib.util
        real_find = importlib.util.find_spec

        def _without_yaml(name, *args, **kwargs):
            if name == "yaml":
                return None
            return real_find(name, *args, **kwargs)

        monkeypatch.setattr(importlib.util, "find_spec", _without_yaml)
        assert argo_bin._self_capable() is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
