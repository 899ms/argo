#!/usr/bin/env python3
"""argo_paths.py — argo 本地状态目录的单一真源。

背景：此前 11 个模块各自拼 ~/.cache/unified-search，构造方式分裂成 4 种
（Path.home()/".cache"/...、expanduser("~/.cache/...")、字面量字符串、
config 默认值），导致 config.yaml 的 cache.db_path 管不住 quota.json、
health.db 等文件，测试也难以整体隔离。

现在所有状态路径统一由本模块派生：
  - 根目录可被 ARGO_STATE_DIR 覆盖（测试隔离 / 只读环境 / 显式换目录）
  - 其次认 config.yaml 的 cache.db_path 所在目录，保持向后兼容
  - 再次：历史目录已存在就继续用（不迁移），否则落**平台惯例**目录
    （Windows %LOCALAPPDATA%/unified-search；POSIX 遵循 XDG_CACHE_HOME）
  - 各模块只声明「文件名」，不再各自拼目录
  - `python3 scripts/argo_paths.py [--json]` 可打印关键路径的实际来源

注意：User-Agent 里的 unified-search@local 是邮箱标识，与状态目录无关，
不在此处管理。
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

# 环境变量覆盖：优先级最高，用于测试隔离与只读环境
ENV_STATE_DIR = "ARGO_STATE_DIR"
# 平台惯例变量（只读，不新增 argo 私有名字）
ENV_XDG_CACHE = "XDG_CACHE_HOME"
ENV_LOCALAPPDATA = "LOCALAPPDATA"

# 历史默认目录（也是 config.yaml 中 db_path 的默认前缀）
_LEGACY_ROOT = "~/.cache/unified-search"

# 缓存配置段未就绪时的兜底（config 不可用、PyYAML 缺失等场景）
_FALLBACK_ROOT = _LEGACY_ROOT

# 应用名（平台惯例目录里的子目录名）
_APP_DIRNAME = "unified-search"


def _platform_cache_root(env: Mapping[str, str] | None = None,
                         platform: str | None = None) -> Path | None:
    """平台惯例的缓存根（不含应用名）：Windows → %LOCALAPPDATA%；其余 → $XDG_CACHE_HOME。

    未设置对应变量时返回 None，由调用方回落到 Unix 惯例 ~/.cache。抽成纯函数
    （可注入 env 与 platform）是为了能在 macOS 上直接单测 Windows 分支——否则
    「Windows 上取哪个目录」永远只有真机才能验证。
    """
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if platform.startswith("win"):
        base = str(env.get(ENV_LOCALAPPDATA) or "").strip()
    else:
        base = str(env.get(ENV_XDG_CACHE) or "").strip()
    return Path(os.path.expanduser(base)) if base else None


def platform_cache_default() -> Path:
    """平台惯例的默认状态目录（不含覆盖与配置影响）。

    Windows：%LOCALAPPDATA%\\unified-search；POSIX（Linux/BSD/macOS）：遵循
    XDG_CACHE_HOME，未设置时 ~/.cache/unified-search（XDG 默认值，与历史一致）。
    """
    root = _platform_cache_root()
    if root is not None:
        return root / _APP_DIRNAME
    return Path(os.path.expanduser(_LEGACY_ROOT))


def state_root() -> Path:
    """返回 argo 本地状态根目录（已 expanduser，不保证存在）。

    优先级（**存量优先**，避免静默搬家丢掉用户的缓存与配额计数）：

      1. ARGO_STATE_DIR 环境变量（显式覆盖，测试隔离与只读环境都用它）
      2. config.yaml cache.db_path 的父目录（保证与主缓存同域）
      3. 历史目录 ~/.cache/unified-search（**已存在**时继续用；不迁移）
      4. 平台惯例目录：Windows → %LOCALAPPDATA%\\unified-search；
         POSIX → $XDG_CACHE_HOME/unified-search（该变量设置时）
      5. 兜底：~/.cache/unified-search（即 POSIX 惯例的 XDG 默认值）

    为什么「已存在的历史目录」排在平台惯例之前：平台变量（XDG_CACHE_HOME /
    LOCALAPPDATA）在 Windows 上恒有值、在部分 Linux 桌面也常被设置，若它们无条件
    优先，存量用户升级后会发现搜索缓存、配额计数、准入记录全部"归零"——那是比
    「目录不够惯例」严重得多的问题。想让 argo 换目录的用户用 ARGO_STATE_DIR
    显式指定（或直接改 config.yaml 的 cache.db_path），一步到位且可预期。
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

    legacy = Path(os.path.expanduser(_LEGACY_ROOT))
    try:
        if legacy.is_dir():
            return legacy
    except OSError:
        pass
    return platform_cache_default()


def resolved_paths() -> dict[str, str]:
    """诊断用：把关键路径的**实际来源**摊开（`python3 scripts/argo_paths.py`）。

    支持类问题的第一步永远是「你到底在读哪个文件」；此前这些路径散在
    state_root / config.peek_cache_db_path / engine_env._envfile_paths 三处，
    只能靠读代码推。这里只做只读汇报，不产生任何副作用。
    """
    out: dict[str, str] = {}
    override = os.environ.get(ENV_STATE_DIR, "").strip()
    if override:
        out["state_source"] = f"{ENV_STATE_DIR}={override}"
    elif _config_db_path():
        out["state_source"] = "config.yaml cache.db_path"
    elif Path(os.path.expanduser(_LEGACY_ROOT)).is_dir():
        out["state_source"] = "历史默认目录（已存在，未迁移）"
    else:
        out["state_source"] = "平台惯例默认"
    out["state_root"] = str(state_root())
    out["platform_cache_default"] = str(platform_cache_default())
    out["legacy_root"] = str(Path(os.path.expanduser(_LEGACY_ROOT)))
    try:
        import engine_env
        env_paths = [str(p) for p in engine_env._envfile_paths()]
        out["env_files"] = os.pathsep.join(env_paths)
        out["env_file_in_use"] = next(
            (p for p in env_paths if Path(p).is_file()), "(均不存在)")
    except Exception as e:      # engine_env 不可用不该让诊断本身失败
        out["env_files"] = f"(不可用：{e})"
    return out


def _cli() -> int:
    import argparse
    import json as _json
    parser = argparse.ArgumentParser(
        description="argo 路径诊断：状态目录与密钥文件到底解析到了哪里")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args()
    info = resolved_paths()
    if args.json:
        print(_json.dumps(info, ensure_ascii=False, indent=2))
    else:
        for k, v in info.items():
            print(f"{k:22} {v}")
    return 0


def _config_db_path() -> str | None:
    """从 config.yaml 读 cache.db_path；不可用时返回 None。

    config 模块本身可能不可用（PyYAML 缺失 / 配置文件损坏），
    此处必须 fail-open，否则路径派生会连带崩溃。

    走 peek_cache_db_path() 轻量读取：get_cache_config() 会触发 load_config()
    合并全部外置引擎 spec，而 import cache 时就会调到本函数——为一个标量付
    整轮合并不值得。

    （勘误 2026-09-15：此处的「约 1.7s」无法复现。实测 load_config() 为
    纯 Python loader 107 ms / C 版 15 ms；真实问题是 import 链上这条派生被
    连调 4 次，且每次都用慢的 loader 解析 123 KB 配置。）
    """
    try:
        from config import peek_cache_db_path
        return peek_cache_db_path()
    except Exception:
        return None


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
        import fcntl  # POSIX
    except ImportError:
        fcntl = None

    lock_impl = _make_lock_impl(fcntl)
    if lock_impl is None:
        # 两个平台都没有可用锁（不认识的系统）→ fail-open，绝不阻断主路径
        try:
            yield
        finally:
            os.close(fd)
        return

    acquire, release = lock_impl
    deadline = time.monotonic() + timeout
    acquired = False
    while True:
        try:
            acquire(fd)
            acquired = True
            break
        except OSError:
            if time.monotonic() >= deadline:
                break
            time.sleep(0.002)
    try:
        yield
    finally:
        if acquired:
            try:
                release(fd)
            except OSError:
                pass
        os.close(fd)


def _make_lock_impl(fcntl: Any):
    """选一套可用的文件锁实现 → (acquire(fd), release(fd))；都不可用返回 None。

    为什么要两套：Windows 没有 `fcntl`，此前这里直接 fail-open——于是
    「6 进程 × 60 次 record 状态丢失 77%」这类丢更新在 Windows 上原样复现，
    而调用方完全看不出来（没有任何日志，锁看起来"加了"）。Windows 上的对应
    设施是 `msvcrt.locking`：对文件的一段字节区间加锁，语义与 flock 足够接近。

    差异点（都在这里吸收掉，不让调用方感知）：
    - msvcrt 锁的是**相对当前文件位置**的字节区间 → 每次加/解锁前 seek(0)；
    - 空文件上锁区间为空，先写一个字节把它变成合法区间（内容是占位，无人读）；
    - msvcrt 的 LK_NBLCK 抢不到时抛 OSError，与 flock 的 LOCK_NB 一致，
      所以外层重试循环两种实现可以共用。
    """
    if fcntl is not None and hasattr(fcntl, "flock"):
        def _acq(fd: int) -> None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def _rel(fd: int) -> None:
            fcntl.flock(fd, fcntl.LOCK_UN)

        return _acq, _rel

    try:
        import msvcrt  # Windows
    except ImportError:
        return None

    def _acq_win(fd: int) -> None:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")      # 让锁区间非空
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _rel_win(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

    return _acq_win, _rel_win


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


if __name__ == "__main__":
    raise SystemExit(_cli())
