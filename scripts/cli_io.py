#!/usr/bin/env python3
"""cli_io.py — CLI 标准输入/输出判据的唯一来源。

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

## stdout 序列化同理

`--json` 输出是给 Agent / 脚本读的，缩进只增加传输体积与 token，不增加任何
信息。MCP 侧早已如此（`mcp_handlers._dumps` 默认 `separators=(",", ":")`），
但 CLI 侧此前在 45 处各写一遍 `json.dumps(..., indent=2)`——同一份载荷两套
计算方式，实测多占 22% 体积。这里给 stdout 一个唯一入口。
"""

from __future__ import annotations

import json
import os
import stat
import sys
from typing import Any

__all__ = ["stdin_is_piped", "read_stdin_if_piped", "dumps", "dumps_pretty"]

# stdout 的紧凑分隔符（无冗余空格）：与 mcp_handlers._dumps 的默认计算方式一致。
_COMPACT = (",", ":")


def dumps(obj: Any) -> str:
    """CLI stdout 的 JSON 序列化唯一来源（默认紧凑）。

    用途边界：**stdout**。写进磁盘的归档文件（`archive_run` 的 public.json /
    coverage.json 等）是给人翻的，仍用 `dumps_pretty`——「机器读 stdout、
    人读文件」这条线让两种格式各归其位，而不是按文件拍脑袋。
    """
    return json.dumps(obj, ensure_ascii=False, separators=_COMPACT)


def dumps_pretty(obj: Any) -> str:
    """人读场景（归档文件、诊断输出）的缩进序列化。"""
    return json.dumps(obj, ensure_ascii=False, indent=2)


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
