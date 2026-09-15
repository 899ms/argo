#!/usr/bin/env python3
"""cli_io.py — CLI 标准输入判据的唯一真源。

## 为什么需要它

判断「stdin 有没有数据」最直觉的写法是 `not sys.stdin.isatty()`，但它是**错的**：
`/dev/null`、已关闭的 fd、以及**任何非交互环境下的空 stdin** 都不是 tty，
于是这个判据会把「没有数据」判成「有数据」。

后果在 argo 的主战场（脚本 / CI / cron / agent 调用）上最严重：
`argo evidence "query"` 会走进「读管道」分支、拿到空串，然后崩在
`json.load(sys.stdin)` 上（2026-09-15 实测复现，退出码 1 + Traceback）。

真正的判据是 fd 的**类型**：
  - S_ISFIFO → `... | argo ...`（管道）
  - S_ISREG  → `argo ... < data.json`（文件重定向）
  - S_ISCHR  → 终端或 /dev/null（无数据）
"""

from __future__ import annotations

import os
import stat
import sys

__all__ = ["stdin_is_piped", "read_stdin_if_piped"]


def stdin_is_piped() -> bool:
    """stdin 是否接了管道或文件重定向（而非终端 / /dev/null / 未打开）。"""
    try:
        mode = os.fstat(sys.stdin.fileno()).st_mode
    except (OSError, ValueError, AttributeError):
        return False
    return stat.S_ISFIFO(mode) or stat.S_ISREG(mode)


def read_stdin_if_piped() -> str:
    """读走 stdin 的全部内容；未接管道/重定向时返回空串。

    一次性读完而不是判空后再读——fd 只能顺序读一遍，窥探会吃掉内容。
    """
    if not stdin_is_piped():
        return ""
    try:
        return sys.stdin.read()
    except (OSError, ValueError, UnicodeDecodeError):
        return ""
