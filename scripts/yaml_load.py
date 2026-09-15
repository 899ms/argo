#!/usr/bin/env python3
"""yaml_load.py — YAML 解析的唯一入口（决定用哪个 loader）。

## 为什么需要这个模块

PyYAML 有两个语义相同的 SafeLoader 实现：
  - `yaml.SafeLoader`   纯 Python，解析 100 KB 配置实测 **79 ms**
  - `yaml.CSafeLoader`  libyaml C 绑定，同样内容实测 **10 ms**（快约 7.7 倍）

`yaml.safe_load()` 恒走纯 Python 那条。此前 8 个模块各自直接调它，于是
「用哪个解析器」这件事被复制了 8 份，没有任何一处能统一改进——与 fetch
的 `--focus` 参数同一种失败模式（各写一份 → 漏改无人察觉）。

本模块是 2026-09-15 冷启动优化的一部分：`import search` 曾耗时约 500 ms，
其中约 316 ms 是 4 次 `peek_cache_db_path()` 各自解析一遍 123 KB 的
config.yaml，而每次解析的绝大部分时间花在纯 Python 扫描器上。换成 C 版
后单次降到约 10 ms。

## 用法

    from yaml_load import loads, load

    data = loads(text)          # 解析字符串
    data = load(path)           # 读取并解析文件

两者在 PyYAML 缺失时抛 ImportError（与 config._require_yaml 的原契约一致，
调用方按既有 fail-open 语义自行处理），解析失败抛 yaml.YAMLError。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["loads", "load", "safe_loader"]


def _yaml():
    import yaml  # type: ignore
    return yaml


def safe_loader() -> Any:
    """返回可用的安全 loader 类：优先 libyaml C 版，不可用回落纯 Python。

    两者语义一致（C 版就是 SafeLoader 的 C 实现），差别只在速度。
    纯 Python 环境（未编译 libyaml）下行为与改动前完全相同。
    """
    yaml = _yaml()
    return getattr(yaml, "CSafeLoader", None) or yaml.SafeLoader


def loads(text: str) -> Any:
    """解析 YAML 文本（安全 loader）。非 dict/list 的顶层结构按原样返回。"""
    yaml = _yaml()
    return yaml.load(text, Loader=safe_loader())


def load(path: str | Path) -> Any:
    """读取并解析 YAML 文件（UTF-8）。"""
    text = Path(path).read_text(encoding="utf-8")
    try:
        return loads(text)
    except Exception:
        # 与 yaml.safe_load 对 file 对象的错误语义对齐：把非 YAML 内容
        # 造成的问题留给调用方判断（此前各自直接 safe_load 也是这个行为）
        raise
