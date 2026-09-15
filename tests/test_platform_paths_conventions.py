#!/usr/bin/env python3
"""test_platform_paths_conventions.py — 跨平台路径惯例契约（2026-09-15）。

## 守的是什么

argo 要在 macOS / Linux / BSD / Windows 上都能按**各自惯例**找到两样东西：
状态目录（缓存、配额、准入记录）与密钥文件。此前两处都是写死的：

- `argo_paths.state_root()` 兜底 `~/.cache/unified-search`（Windows 上不符合
  `%LOCALAPPDATA%` 惯例；Linux 上无视 `XDG_CACHE_HOME`）；
- `engine_env._envfile_path()` 写死 `~/.config/argo/env`（Windows 该用
  `%APPDATA%`，自定义 `XDG_CONFIG_HOME` 的用户无处安放密钥）。

本文件锁三件事：

1. **平台惯例被遵守**：Windows 用 `%LOCALAPPDATA%` / `%APPDATA%`；POSIX 用
   `XDG_CACHE_HOME` / `XDG_CONFIG_HOME`（未设置时回落 XDG 默认值，即历史路径）。
2. **存量优先**：历史目录已存在就继续用，绝不静默搬家——`LOCALAPPDATA` 在
   Windows 上恒有值、`XDG_*` 在部分 Linux 桌面也常被设置，若无条件优先，存量
   用户升级后会发现缓存与配额计数「归零」。
3. **密钥不分裂**：多个候选文件**合并**读取（靠前者逐键优先），而不是只认第一个
   存在的文件——「一份密钥在 A 目录、另一份在 B 目录」会让一部分引擎静默拿不到
   key（本仓踩过的「装了没通电」形态）。

平台分支用纯函数 + 注入 env/platform 覆盖，因此在 macOS 上也能测 Windows 行为。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import argo_paths  # noqa: E402
import engine_env  # noqa: E402


class TestPlatformCacheRoot:
    """平台惯例缓存根：Windows → %LOCALAPPDATA%，POSIX → $XDG_CACHE_HOME。"""

    def test_windows_uses_localappdata(self):
        got = argo_paths._platform_cache_root(
            {"LOCALAPPDATA": r"C:\Users\me\AppData\Local"}, "win32")
        assert got is not None and str(got).endswith(r"AppData\Local".replace("/", os.sep)) \
            or "AppData" in str(got)

    def test_windows_without_env_is_none(self):
        assert argo_paths._platform_cache_root({}, "win32") is None

    def test_posix_uses_xdg_cache_home(self):
        got = argo_paths._platform_cache_root({"XDG_CACHE_HOME": "/var/tmp/xdg"}, "linux")
        assert got == Path("/var/tmp/xdg")

    def test_macos_without_xdg_is_none(self):
        """macOS 默认不设 XDG 变量 → None（调用方回落到 Unix 惯例 ~/.cache）。"""
        assert argo_paths._platform_cache_root({}, "darwin") is None

    def test_platform_default_shape(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ARGO_STATE_DIR", str(tmp_path))
        assert argo_paths.platform_cache_default().name == "unified-search"


class TestStateRootConventions:
    """状态目录解析：显式覆盖 > 配置 > 存量历史目录 > 平台惯例。"""

    def _clean(self, monkeypatch, tmp_path):
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.delenv(argo_paths.ENV_STATE_DIR, raising=False)
        monkeypatch.setattr(argo_paths, "_config_db_path", lambda: None)
        # `~` 展开走 HOME（POSIX 的 expanduser 语义），不是 Path.home()
        monkeypatch.setenv("HOME", str(tmp_path))

    def test_explicit_env_beats_everything(self, monkeypatch, tmp_path):
        self._clean(monkeypatch, tmp_path)
        monkeypatch.setenv(argo_paths.ENV_STATE_DIR, str(tmp_path / "explicit"))
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
        assert argo_paths.state_root() == tmp_path / "explicit"

    def test_existing_legacy_is_not_migrated(self, monkeypatch, tmp_path):
        """存量优先：历史目录存在时，即便 XDG/LOCALAPPDATA 有值也不搬家。"""
        self._clean(monkeypatch, tmp_path)
        legacy = tmp_path / ".cache" / "unified-search"
        legacy.mkdir(parents=True)
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
        assert argo_paths.state_root() == legacy

    def test_xdg_used_when_no_legacy(self, monkeypatch, tmp_path):
        """全新环境（无历史目录）：遵守 XDG_CACHE_HOME。"""
        self._clean(monkeypatch, tmp_path)
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
        assert argo_paths.state_root() == tmp_path / "xdg-cache" / "unified-search"

    def test_fallback_is_xdg_default(self, monkeypatch, tmp_path):
        """没有任何惯例变量时落到 ~/.cache/unified-search（XDG 的默认值）。"""
        self._clean(monkeypatch, tmp_path)
        assert argo_paths.state_root() == tmp_path / ".cache" / "unified-search"

    def test_resolved_paths_reports_source(self, monkeypatch, tmp_path):
        """诊断输出必须说明「为什么是这个目录」——支持类问题的第一问。"""
        self._clean(monkeypatch, tmp_path)
        (tmp_path / ".cache" / "unified-search").mkdir(parents=True)
        info = argo_paths.resolved_paths()
        assert "未迁移" in info["state_source"] or "历史" in info["state_source"]
        assert info["state_root"].endswith("unified-search")
        assert "env_files" in info and "env_file_in_use" in info


class TestEnvFileConventions:
    """密钥文件：平台惯例候选 + 合并读取 + 显式覆盖。"""

    @pytest.fixture(autouse=True)
    def _reset(self):
        engine_env.reset_envfile_cache()
        yield
        engine_env.reset_envfile_cache()

    def test_default_is_legacy_xdg_path(self, monkeypatch):
        monkeypatch.delenv(engine_env.ENV_ENV_FILE, raising=False)
        monkeypatch.delenv(engine_env.ENV_XDG_CONFIG, raising=False)
        monkeypatch.setattr(engine_env, "_platform_config_root", lambda *a, **k: None)
        assert engine_env._envfile_paths() == [Path.home() / ".config" / "argo" / "env"]

    def test_xdg_config_home_preferred_then_legacy(self, monkeypatch, tmp_path):
        monkeypatch.delenv(engine_env.ENV_ENV_FILE, raising=False)
        monkeypatch.setenv(engine_env.ENV_XDG_CONFIG, str(tmp_path / "xdg"))
        paths = engine_env._envfile_paths()
        assert paths[0] == tmp_path / "xdg" / "argo" / "env", "XDG 候选应排在历史路径之前"
        assert paths[1] == Path.home() / ".config" / "argo" / "env"

    def test_explicit_override_is_single(self, monkeypatch, tmp_path):
        monkeypatch.setenv(engine_env.ENV_ENV_FILE, str(tmp_path / "myenv"))
        assert engine_env._envfile_paths() == [tmp_path / "myenv"]

    def test_windows_root_uses_appdata(self):
        got = engine_env._platform_config_root({"APPDATA": r"C:\Users\me\AppData\Roaming"},
                                               "win32")
        assert got is not None and "AppData" in str(got)

    def test_candidates_are_merged_first_wins(self, monkeypatch, tmp_path):
        """合并读取：靠前候选逐键优先，且另一份文件的键不会丢。

        只认第一个存在的文件时，「密钥一部分在 XDG、一部分在历史路径」会让
        一部分引擎静默拿不到 key。
        """
        a = tmp_path / "xdg-env"
        b = tmp_path / "legacy-env"
        a.write_text("ARGO_BOTH=from_xdg\nARGO_ONLY_A=1\n", encoding="utf-8")
        b.write_text("ARGO_BOTH=from_legacy\nARGO_ONLY_B=2\n", encoding="utf-8")
        monkeypatch.setattr(engine_env, "_envfile_paths", lambda: [a, b])
        data = engine_env._envfile_load()
        assert data["ARGO_BOTH"] == "from_xdg", "同名键应取靠前候选"
        assert data["ARGO_ONLY_A"] == "1" and data["ARGO_ONLY_B"] == "2", \
            f"两份文件的键都应被合并：{sorted(data)}"

    def test_cache_invalidated_when_second_candidate_changes(self, monkeypatch, tmp_path):
        """签名要覆盖**全部**候选：改第二份文件也必须重读，否则新密钥看不到。"""
        a = tmp_path / "a-env"
        b = tmp_path / "b-env"
        a.write_text("ARGO_A=1\n", encoding="utf-8")
        b.write_text("ARGO_B=1\n", encoding="utf-8")
        monkeypatch.setattr(engine_env, "_envfile_paths", lambda: [a, b])
        assert engine_env._envfile_load()["ARGO_B"] == "1"
        b.write_text("ARGO_B=2\n", encoding="utf-8")       # 改第二份
        assert engine_env._envfile_load()["ARGO_B"] == "2", \
            "第二份候选变更未触发重读（签名没覆盖全部候选）"

    def test_missing_files_are_tolerated(self, monkeypatch, tmp_path):
        monkeypatch.setattr(engine_env, "_envfile_paths",
                            lambda: [tmp_path / "nope", tmp_path / "also-nope"])
        assert engine_env._envfile_load() == {}


class TestArchiveRedactionCrossPlatform:
    """归档脱敏要认三家平台的家目录（Windows 路径此前原样落盘）。"""

    @pytest.mark.parametrize("path", [
        r"C:\Users\alice\Documents\secret.txt",
        "/Users/alice/Documents/secret.txt",
        "/home/alice/documents/secret.txt",
    ])
    def test_home_paths_redacted(self, path):
        import archive_run
        out = archive_run.redact_secrets(f"读取 {path} 失败")
        assert "alice" not in out, f"{path} 未被脱敏：{out}"
        assert "~" in out

    def test_non_home_windows_path_untouched(self):
        """只脱家目录，不要把普通盘符路径也一起抹掉（误伤会让日志没法看）。"""
        import archive_run
        out = archive_run.redact_secrets(r"构建输出在 D:\build\out.bin")
        assert r"D:\build\out.bin" in out


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
