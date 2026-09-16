#!/usr/bin/env python3
"""test_platform_paths_conventions.py — 跨平台路径惯例契约（2026-09-15）。

## 守的是什么

argo 要在 macOS / Linux / BSD / Windows 上都能按**各自惯例**找到两样东西：
状态目录（缓存、配额、准入记录）与密钥文件。此前两处都是写死的：

- `argo_paths.state_root()` 保底 `~/.cache/unified-search`（Windows 上不符合
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
    """归档脱敏要认三家平台的家目录（Windows 路径此前原样写入文件）。"""

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


class TestLegacyStateMigration:
    """可选迁移：把历史状态搬到平台惯例目录（默认只报告，绝不擅自搬用户数据）。"""

    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch):
        engine_env.reset_envfile_cache()
        monkeypatch.delenv(argo_paths.ENV_STATE_DIR, raising=False)
        monkeypatch.setattr(argo_paths, "_config_db_path", lambda: None)
        yield
        engine_env.reset_envfile_cache()

    def _legacy(self, tmp_path):
        d = tmp_path / ".cache" / "unified-search"
        d.mkdir(parents=True)
        (d / "quota.json").write_text('{"used": 3}', encoding="utf-8")
        d.mkdir(exist_ok=True)
        return d

    def test_explicit_state_dir_is_never_moved(self, monkeypatch, tmp_path):
        """ARGO_STATE_DIR 是权威：用户明确指定的位置不该被"顺手搬家"。"""
        monkeypatch.setenv(argo_paths.ENV_STATE_DIR, str(tmp_path / "explicit"))
        res = argo_paths.migrate_legacy_state(yes=True)
        assert "跳过" in res["status"] and not res["moved"]

    def test_config_db_path_is_never_moved(self, monkeypatch, tmp_path):
        monkeypatch.setattr(argo_paths, "_config_db_path",
                            lambda: str(tmp_path / "custom" / "cache.db"))
        res = argo_paths.migrate_legacy_state(yes=True)
        assert "跳过" in res["status"] and not res["moved"]

    def test_noop_when_no_legacy(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
        res = argo_paths.migrate_legacy_state(yes=True)
        assert "无需迁移" in res["status"]

    def test_dry_run_is_read_only_and_needs_no_yes(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
        legacy = self._legacy(tmp_path)
        res = argo_paths.migrate_legacy_state(dry_run=True)     # 未加 yes
        assert "预演" in res["status"] and "quota.json" in res["moved"]
        assert (legacy / "quota.json").exists(), "预演不该动任何文件"

    def test_requires_explicit_yes(self, monkeypatch, tmp_path):
        """不管是不是 TTY，没有 --yes 就不动数据（不用 TTY 探测做确认）。"""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
        legacy = self._legacy(tmp_path)
        res = argo_paths.migrate_legacy_state()                 # 未加 yes
        assert "拒绝" in res["status"] and "--yes" in res["status"]
        assert (legacy / "quota.json").exists(), "未确认就不该动任何文件"

    def test_migration_moves_state_and_convention_takes_effect(self, monkeypatch, tmp_path):
        """核心断言：搬完之后 state_root 必须**真的**切到惯例目录。

        只搬文件不删空目录时，state_root 仍会判「历史存在」而停在旧路径，用户会
        以为数据丢了——所以历史目录的清理是这一步的一部分。
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
        legacy = self._legacy(tmp_path)
        res = argo_paths.migrate_legacy_state(yes=True)
        target = tmp_path / "xdg" / "unified-search"
        assert res["moved"] and not res["errors"], res
        assert (target / "quota.json").read_text(encoding="utf-8") == '{"used": 3}'
        assert not legacy.exists(), "历史目录应被清掉（否则惯例不生效）"
        assert argo_paths.state_root() == target, "迁移后 state_root 未切到惯例目录"

    def test_refuses_when_target_has_content(self, monkeypatch, tmp_path):
        """绝不覆盖：目标目录已有内容时拒绝执行（宁可让用户手工处理）。"""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
        self._legacy(tmp_path)
        target = tmp_path / "xdg" / "unified-search"
        target.mkdir(parents=True)
        (target / "existing.json").write_text("{}", encoding="utf-8")
        res = argo_paths.migrate_legacy_state(yes=True)
        assert "拒绝" in res["status"] and not res["moved"]

    def test_move_failure_keeps_legacy_dir_and_leaves_marker(self, monkeypatch, tmp_path):
        """个别文件搬不动时：不假装成功——保留历史目录、写下标记说明去处。

        （整目录整体搬走是刻意的：给状态文件维护"谁的文件"白名单会随新增状态文件
        漂移，所以这里只覆盖失败路径。）
        """
        import shutil as _shutil
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
        legacy = self._legacy(tmp_path)
        stuck = legacy / "stuck.db"
        stuck.write_text("搬不动", encoding="utf-8")
        real_move = _shutil.move

        def flaky_move(src, dst):
            if str(src).endswith("stuck.db"):
                raise OSError("模拟占用")
            return real_move(src, dst)

        monkeypatch.setattr(_shutil, "move", flaky_move)
        res = argo_paths.migrate_legacy_state(yes=True)
        assert res["errors"], f"失败未记录：{res}"
        assert res.get("legacy_removed") is False, "有文件没搬走就不该删历史目录"
        marker = legacy / "MIGRATED_TO.txt"
        assert marker.exists(), "未留下标记文件"
        assert "unified-search" in marker.read_text(encoding="utf-8")
        assert stuck.exists(), "没搬走的文件应留在原处供人工处理"
        assert "部分完成" in res["status"]


class TestPathSelfCheck:
    """`argo paths --check`：任何平台一条命令自行验证「这台机器上实际发生了什么」。"""

    def test_all_results_are_structured(self, monkeypatch, tmp_path):
        monkeypatch.setenv(argo_paths.ENV_STATE_DIR, str(tmp_path))
        checks = argo_paths.run_checks(lock_hold_s=0.2)
        names = [c["check"] for c in checks]
        for required in ("平台", "状态目录解析", "状态目录可写", "密钥文件",
                         "配置加载", "解释器", "跨进程锁"):
            assert required in names, f"自检缺少 {required}：{names}"
        for c in checks:
            assert c["status"] in ("pass", "fail", "warn", "info"), c
            assert c["detail"], f"{c['check']} 没有细节说明"
        failed = [c for c in checks if c["status"] == "fail"]
        assert not failed, f"本机自检出现失败：{failed}"

    def test_unwritable_state_dir_is_reported(self, monkeypatch, tmp_path):
        """只读挂载 / 权限不足时必须是 fail，并给出可执行的出路。"""
        ro = tmp_path / "ro"
        ro.mkdir()
        ro.chmod(0o500)
        monkeypatch.setenv(argo_paths.ENV_STATE_DIR, str(ro))
        try:
            checks = {c["check"]: c for c in argo_paths.run_checks(lock_hold_s=0.2)}
        finally:
            ro.chmod(0o700)
        assert checks["状态目录可写"]["status"] == "fail"
        assert "ARGO_STATE_DIR" in checks["状态目录可写"]["detail"]

    def test_cli_exit_code_marks_failure(self, monkeypatch):
        monkeypatch.setattr(argo_paths, "run_checks", lambda *a, **k: [
            {"check": "X", "status": "fail", "detail": "boom"}])
        monkeypatch.setattr(sys, "argv", ["argo_paths.py", "--check"])
        assert argo_paths._cli() == 1
        monkeypatch.setattr(argo_paths, "run_checks", lambda *a, **k: [
            {"check": "X", "status": "pass", "detail": "ok"}])
        assert argo_paths._cli() == 0

    def test_lock_roundtrip_detects_broken_lock(self, monkeypatch, tmp_path):
        """锁形同虚设时必须报 fail——否则这个自检在真机上会给出假绿。"""
        import contextlib
        monkeypatch.setenv(argo_paths.ENV_STATE_DIR, str(tmp_path))

        @contextlib.contextmanager
        def fake_lock(path, timeout=10.0):
            yield          # 假装拿到锁（完全无互斥）

        monkeypatch.setattr(argo_paths, "file_lock", fake_lock)
        status, detail = argo_paths._check_lock_roundtrip(0.2)
        assert status == "fail", f"锁没生效却报通过：{detail}"

    def test_lock_roundtrip_passes_with_real_lock(self, monkeypatch, tmp_path):
        monkeypatch.setenv(argo_paths.ENV_STATE_DIR, str(tmp_path))
        status, detail = argo_paths._check_lock_roundtrip(0.2)
        assert status == "pass", detail
        assert "等待" in detail


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
