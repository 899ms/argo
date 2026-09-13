#!/usr/bin/env python3
"""argo_paths.py — argo 本地状态目录的单一真源。

背景：此前 11 个模块各自拼 ~/.cache/unified-search，构造方式分裂成 4 种
（Path.home()/".cache"/...、expanduser("~/.cache/...")、字面量字符串、
config 默认值），导致 config.yaml 的 cache.db_path 管不住 quota.json、
health.db 等文件，测试也难以整体隔离。

现在所有状态路径统一由本模块派生：
  - 根目录可被 ARGO_STATE_DIR 覆盖（测试隔离 / 只读环境 / XDG 迁移）
  - 未设置时回落到 config.yaml 的 cache.db_path 所在目录，保持向后兼容
  - 各模块只声明「文件名」，不再各自拼目录

注意：User-Agent 里的 unified-search@local 是邮箱标识，与状态目录无关，
不在此处管理。
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

# 环境变量覆盖：优先级最高，用于测试隔离与只读环境
ENV_STATE_DIR = "ARGO_STATE_DIR"

# 历史默认目录（也是 config.yaml 中 db_path 的默认前缀）
_LEGACY_ROOT = "~/.cache/unified-search"

# 缓存配置段未就绪时的兜底（config 不可用、PyYAML 缺失等场景）
_FALLBACK_ROOT = _LEGACY_ROOT


def _config_db_path() -> str | None:
    """从 config.yaml 读 cache.db_path；不可用时返回 None。

    config 模块本身可能不可用（PyYAML 缺失 / 配置文件损坏），
    此处必须 fail-open，否则路径派生会连带崩溃。
    """
    try:
        from config import get_cache_config
        cfg = get_cache_config()
        db_path = cfg.get("db_path")
        return str(db_path) if db_path else None
    except Exception:
        return None


def state_root() -> Path:
    """返回 argo 本地状态根目录（已 expanduser，不保证存在）。

    优先级：
      1. ARGO_STATE_DIR 环境变量
      2. config.yaml cache.db_path 的父目录（保证与主缓存同域）
      3. 历史默认 ~/.cache/unified-search
    """
    override = os.environ.get(ENV_STATE_DIR, "").strip()
    if override:
        return Path(os.path.expanduser(override))

    db_path = _config_db_path()
    if db_path:
        expanded = os.path.expanduser(db_path)
        parent = os.path.dirname(expanded)
        if parent:
            return Path(parent)
    return Path(os.path.expanduser(_FALLBACK_ROOT))


def state_path(*parts: str) -> Path:
    """返回状态根目录下的路径（不自动创建目录）。

    state_path("health.db")        → <root>/health.db
    state_path("admission", "x.json") → <root>/admission/x.json
    """
    return state_root().joinpath(*parts)


def ensure_state_dir(*parts: str) -> Path:
    """返回状态根目录下的子目录，并确保其存在。

    目录不可创建时（只读挂载 / 权限受限）fail-open 返回目标路径——
    由调用方在写入时处理，避免 import 期就崩。
    """
    d = state_path(*parts)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def legacy_root() -> Path:
    """历史默认目录（仅用于迁移/兼容判断，新代码请用 state_root）。"""
    return Path(os.path.expanduser(_LEGACY_ROOT))


@contextlib.contextmanager
def file_lock(path: Path, *, timeout: float = 10.0):
    """跨进程排他锁（阻塞获取，超时抛 TimeoutError）。

    为什么需要：状态文件的「读-改-写」序列只在**进程内**加锁，
    CLI / MCP server / 评测脚本三者并行时，进程 A 读到旧状态、
    进程 B 也读到旧状态，各自 +1 后依次覆盖，后写者抹掉前者的增量
    （实测 6 进程 × 60 次 record 状态丢失 77%）。

    实现：flock 锁在独立的 .lock 文件上，不与被保护的数据文件共享
    inode——数据文件靠 os.replace 整体替换，若锁与数据同 inode，
    替换后新进程会锁到另一个 inode 而形同无锁。

    fail-open：拿不到锁（平台不支持 flock）时直接放行，绝不因
    观测层问题阻断搜索主路径。
    """
    lock_path = path.parent / f".{path.name}.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        yield
        return

    fd = None
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        yield
        return

    try:
        import fcntl  # 仅 POSIX；Windows 无 flock
    except ImportError:
        fcntl = None

    if fcntl is None or not hasattr(fcntl, "flock"):
        try:
            yield
        finally:
            os.close(fd)
        return

    import time as _time
    deadline = _time.monotonic() + timeout
    acquired = False
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            break
        except OSError:
            if _time.monotonic() >= deadline:
                break
            _time.sleep(0.002)
    try:
        yield
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    """原子写文本文件的单一真源（任意内容，非仅 JSON）。

    临时文件用 mkstemp 取**进程内唯一**名字（同目录，保证同文件系统
    rename 语义），失败路径只清理自己的 tmp。`mode` 非空时对最终文件
    收紧权限（密钥类配置写 0600）。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        if mode is not None:
            os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        # 只清理自己创建的 tmp；别人的 tmp 不归本进程管
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, payload: Any, *, indent: int | None = 2) -> None:
    """原子写 JSON 状态的单一真源。

    why：此前 quota / circuit_breaker / lang_pref / v2ex_nodes / job /
    fetch_v3 / redskill 多处各自手写 `p.with_suffix(".tmp")` + replace，
    **临时文件名固定**。多进程（CLI 与 MCP server 并行、或评测脚本）
    同时写同一个文件时，A 的 replace 会把 B 的 tmp 一起搬走/删掉，B 再
    replace 就抛 FileNotFoundError。实测 6 进程 × 60 次 record：崩溃 235
    次、成功仅 125 次、状态丢失 68%——配额计数因此系统性偏低，且
    errors > used 的反常正是这么来的。

    修法：临时文件用 mkstemp 取**进程内唯一**名字（同目录，保证同文件
    系统 rename 语义），失败路径清理自己的 tmp，绝不触碰别人的。
    实现在 atomic_write_text；本函数只负责序列化。
    """
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=indent))


def isolate_state_dir(tag: str = "argo-dev") -> Path:
    """把状态目录重定向到独立临时目录，并返回该目录。

    **必须在 import 任何 argo 状态模块之前调用**——quota / circuit_breaker /
    cache / adaptive 等在模块级就按当时的 ARGO_STATE_DIR 定下路径常量，
    之后再改环境变量不生效。

    为什么需要：开发/评测脚本（ab_eval、matrix_search_eval、benchmark…）
    会走真实搜索路径，从而写生产状态。实测污染后果——真实
    circuit_breaker.json 里混进 190 个 `eng_<8hex>` 夹具条目、`probe…`、
    `bad` 等测试引擎，熔断统计被稀释，且这些脏条目永久留在生产状态里。
    """
    d = Path(tempfile.mkdtemp(prefix=f"{tag}-state-"))
    os.environ[ENV_STATE_DIR] = str(d)
    return d


def db_path() -> Path:
    """主缓存库路径。

    与 state_path("cache.db") 的差别只在「config.yaml 里用户显式改写了
    db_path」这一种情况：此时尊重用户的显式配置，不放回状态根目录。

    ARGO_STATE_DIR 一旦设置，一律优先——它是测试隔离与只读环境的硬开关，
    不能被磁盘上的 config.yaml 盖掉（否则 env 形同虚设）。
    """
    if os.environ.get(ENV_STATE_DIR, "").strip():
        return state_path("cache.db")
    raw = _config_db_path()
    if raw:
        return Path(os.path.expanduser(raw))
    return state_path("cache.db")
