"""2026-09-16 代码审查后的 7 补丁回归测试。

分两部分：
- TestRegression：直接锁死每个已修复的 P0 bug/perf 点，防回潮。
- TestRatchet：CI 棘轮，扫描源码禁止新增「已收敛问题模式」（存量入白名单）。

审查报告见会话记录；回滚锚点：git reset --hard pre-7patches-20260916
"""
from __future__ import annotations

import ast
import io
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


# ── TestRegression：直接锁死 4 个已修的 P0 ────────────────────────────────


class TestCacheP0s:
    """补丁 1（size_mb O(1)）+ 补丁 2（evict 不阻断写入）+ 补丁 3（O(N)）"""

    def test_size_mb_uses_pragma_not_blob_sum(self, tmp_path):
        """size_mb 走 pragma 页统计，不再全表 SUM(LENGTH(value_blob))。"""
        import cache as _c
        sc = _c.SQLiteCache(db_path=str(tmp_path / "k.db"))
        src = Path(_c.__file__).read_text(encoding="utf-8")
        # size_mb 属性体内**不能**出现 SUM(LENGTH(...))
        # 用行定位而非全局 grep：_evict 里保留 LENGTH(value_blob) 做批内扣减，合法
        prop_start = src.index("    def size_mb(self)")
        prop_end = src.index("\nclass ", prop_start) if "\nclass " in src[prop_start:] else len(src)
        # 简单点：找下一个 @property 或 def 作终止
        nxt = src.find("\n    @property", prop_start + 1)
        nxt2 = src.find("\n    def ", prop_start + 1)
        prop_end = min(x for x in (nxt, nxt2, len(src)) if x > 0)
        prop_body = src[prop_start:prop_end]
        assert "pragma_page_count" in prop_body, \
            "size_mb 已回潮成 blob 扫描——性能回退 200–400 倍"
        assert "SUM(LENGTH(value_blob))" not in prop_body

    def test_evict_failure_does_not_bubble(self, tmp_path):
        """DB 损坏 / WAL 异常时驱逐失败必须**静默**，不能让 set() 抛给搜索主路径。"""
        import cache as _c
        sc = _c.SQLiteCache(db_path=str(tmp_path / "k.db"))
        sc._init_db()
        with patch.object(sc, "_evict_if_needed",
                          side_effect=sqlite3.OperationalError("database is locked")):
            # 不应抛
            sc.set("k1", "q1", "eng", 10, {"results": []}, "general")
        # 记录仍然写入成功
        assert sc.get("k1") is not None

    def test_evict_loop_is_linear_not_quadratic(self, tmp_path):
        """驱逐一次性读总量 + 批量 LIMIT 50 删除；不允许 while 条件里每次读 self.size_mb。
        用 AST 判定，避免字符串匹配被 docstring/注释里对旧形态的说明误伤。
        """
        src = (SCRIPTS / "cache.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        target_fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_evict_if_needed":
                target_fn = node
                break
        assert target_fn is not None, "_evict_if_needed 函数不存在？"
        # 遍历 while 节点，检查 test 表达式里是否读 self.size_mb
        for wh in ast.walk(target_fn):
            if isinstance(wh, ast.While):
                for sub in ast.walk(wh.test):
                    if (isinstance(sub, ast.Attribute)
                            and sub.attr == "size_mb"
                            and isinstance(sub.value, ast.Name)
                            and sub.value.id == "self"):
                        pytest.fail(
                            f"_evict_if_needed 的 while 条件回潮读 self.size_mb（行 {wh.lineno}）："
                            "每次触发都是新连接 + 全表扫，O(N²) 化驱逐"
                        )
        # 新形态：批量删除——AST 里应能观察到 LIMIT 常量或 fetchall 批处理
        src_body = ast.get_source_segment(src, target_fn) or ""
        assert "LIMIT" in src_body.upper(), \
            "_evict_if_needed 应批量 LIMIT 删除而非逐行"


class TestFileLockP0:
    """补丁 4：file_lock 超时保留 fail-open 语义（作者有意），但**必须可观测**。"""

    def test_timeout_logs_warning(self, tmp_path):
        """超时放行时 log warning；不能纯静默让运维看不到。"""
        import argo_paths
        log_path = tmp_path / "state.json"
        # 用真实持有者挡路：另开一个 fd 抢锁不释放
        lock_file = log_path.parent / f".{log_path.name}.lock"
        lock_file.touch()
        fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            buf = io.StringIO()
            handler = logging.StreamHandler(buf)
            logger = logging.getLogger("argo.file_lock")
            logger.addHandler(handler)
            old_level = logger.level
            logger.setLevel(logging.WARNING)
            try:
                t0 = time.monotonic()
                # 抢不到 → 50ms 后超时 → fail-open 放行 + log warning
                with argo_paths.file_lock(log_path, timeout=0.05):
                    pass
                elapsed = time.monotonic() - t0
            finally:
                logger.removeHandler(handler)
                logger.setLevel(old_level)
            assert elapsed >= 0.05, "应在 timeout 之后才放行"
            assert "timed out" in buf.getvalue(), \
                "file_lock 超时未 log warning，运维看不见「无锁执行」发生过"
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:
                pass
            os.close(fd)


class TestRegistryUtf8:
    """补丁 5：argo_engine_registry 的 health 写盘必须是 utf-8（Windows 兼容）。"""

    def test_health_with_non_ascii_roundtrips_as_utf8(self, tmp_path, monkeypatch):
        """写入含中文 error 的 health → 文件字节必须是合法 utf-8。"""
        import argo_engine_registry as reg
        target = tmp_path / "health.json"
        monkeypatch.setattr(reg, "HEALTH_STATE_PATH", target)
        r = reg.EngineRegistry.__new__(reg.EngineRegistry)
        r._health = {"github": {"error": "中文错误：连接被重置"}}
        r._save_health()
        assert target.exists()
        raw = target.read_bytes()
        # 关键：若 write_text 无 encoding 且 locale=cp1252（Windows 默认），
        # 这里要么写不进（异常被吞），要么写成 mojibake。utf-8 才能反解出中文。
        back = json.loads(raw.decode("utf-8"))
        assert back["github"]["error"] == "中文错误：连接被重置"


# ── TestRatchet：CI 棘轮，锁死一类 bug 永不再犯 ──────────────────────────


# 存量豁免（跨行调用会让 grep 型审查误报，AST 才是可靠判据；已核实这些
# 位置其实**都有** encoding= 参数，是 grep 单行匹配漏掉了下一行的参数）
_WRITE_TEXT_GRANDFATHER: set[str] = set()  # 空集：全仓现在都合规


class TestRatchet:
    """棘轮：新增代码不得引入已收敛的三类问题。存量入白名单，只减不增。"""

    _SKIP_DIRS = {"__pycache__", ".trash", "node_modules", ".pytest_cache",
                 ".ruff_cache", "dist", "docs", "docs-local", "references"}

    def _iter_scripts(self):
        for p in SCRIPTS.rglob("*.py"):
            if any(part in self._SKIP_DIRS for part in p.parts):
                continue
            yield p

    def test_no_write_text_without_encoding(self):
        """Path.write_text(...) 必须显式 encoding——Windows 上默认 cp1252 会
        把中文塞进 ensure_ascii=False 的 JSON 时炸，被 `except Exception: pass`
        吞掉 → 静默丢状态。审查报告 §2.2 P0-3 类。"""
        offenders: list[str] = []
        for path in self._iter_scripts():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            rel = path.relative_to(SCRIPTS).as_posix()
            if rel in _WRITE_TEXT_GRANDFATHER:
                continue
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "write_text"
                        and len(node.args) >= 1):
                    # kwargs 里有 encoding 就算合规
                    has_enc = any(kw.arg == "encoding" for kw in node.keywords)
                    # 位置参数 (data, encoding, ...) 也算
                    if not has_enc and len(node.args) < 2:
                        offenders.append(f"{rel}:{node.lineno}")
        assert not offenders, (
            f"发现 {len(offenders)} 处 Path.write_text 缺 encoding：\n  "
            + "\n  ".join(offenders)
            + "\n请改成 write_text(..., encoding='utf-8')，"
              "或走 argo_paths.atomic_write_text。"
        )

    def test_sqlite_cache_size_helper_not_blob_sum(self):
        """棘轮 cache.py 的驱逐/统计不能回潮成 SUM(LENGTH(value_blob))。
        驱逐判定用 pragma 页统计（O(1)），批内扣减可以用 LENGTH(value_blob)
        但必须包在 LIMIT 50 的批量里——不能出现在 size_mb 或 while 条件里。
        """
        src = (SCRIPTS / "cache.py").read_text(encoding="utf-8")
        # 定位 size_mb 属性体
        idx = src.index("def size_mb")
        nxt = src.find("\n    @property", idx + 1)
        nxt2 = src.find("\n    def ", idx + 1)
        prop_end = min(x for x in (nxt, nxt2, len(src)) if x > 0)
        body = src[idx:prop_end]
        assert "SUM(LENGTH(value_blob))" not in body, (
            "size_mb 属性内不得出现 SUM(LENGTH(value_blob))——"
            "P0-A perf fix 已改走 pragma_page_count；"
            "回潮会让每次 set() 多付 10–20ms、驱逐段退化到秒级"
        )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
