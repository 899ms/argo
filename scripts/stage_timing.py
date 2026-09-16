#!/usr/bin/env python3
"""stage_timing.py — 阶段耗时计时器（--explain-timing 的载体）。

从 search.py 中外提。此前「最大瓶颈在哪」只能靠外挂计时得出（importtime
看冷启动、cProfile 看 CPU、临时包装模块级函数看阶段），换个人、换台机器
就得重做一遍；这个类让工具自己报出钱花在哪。

零依赖纯数据结构，可被搜索之外的层（抓取、研究、评测）复用。
"""

from __future__ import annotations

import time
from typing import Any


class StageTiming:
    """各阶段墙钟耗时（毫秒）。

    **默认开启**（`--no-timing` 可关）。默认关的话，想知道瓶颈就得会外部
    计时和临时代码，等于把「自己动手优化」的门槛抬到只有维护者过得去。
    代价是每次多几百字节输出——相对它省下的排查成本可以忽略。
    """

    __slots__ = ("marks",)

    def __init__(self) -> None:
        self.marks: dict[str, float] = {}

    def add(self, stage: str, ms: float) -> None:
        # 保留到微秒：1 位小数会把亚毫秒阶段（本地融合/去重在热进程里常 <0.1ms）
        # 全压成 0.0，看起来像「没测到」。多出的两位数字对输出体积可忽略。
        self.marks[stage] = round(self.marks.get(stage, 0.0) + ms, 3)

    def summary(self, total_ms: int | None = None) -> dict[str, Any]:
        """按耗时降序返回，并附占比。

        占比才是行动依据：「rerank 213 ms」本身说明不了什么，「占 59%」才说明
        该不该动它。默认以**各阶段之和**为分母（自洽，纯 CPU 计算方式，不含进程
        启动）；调用方给出 total_ms 时以它为准。
        """
        # 不取整：热进程里各阶段合计常不足 1 ms，`int()` 会把 0.9 截成 0，
        # 占比随即全部变成 0%——看着像「没测到」，实际是被自己的取整吃掉了。
        total = float(total_ms) if total_ms else sum(self.marks.values())
        rows = [{"stage": k, "ms": v,
                 "pct": (round(v * 100.0 / total, 1) if total else 0.0)}
                for k, v in sorted(self.marks.items(), key=lambda kv: -kv[1])]
        return {"stages_ms": round(total, 3), "stages": rows}


def tick(timing: StageTiming | None) -> float | None:
    """取时点；未开计时返回 None——关闭时连 perf_counter 都不调。"""
    return time.perf_counter() if timing is not None else None


def tock(timing: StageTiming | None, stage: str, t0: float | None) -> None:
    if timing is not None and t0 is not None:
        timing.add(stage, (time.perf_counter() - t0) * 1000.0)
