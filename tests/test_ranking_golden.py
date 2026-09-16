#!/usr/bin/env python3
"""test_ranking_golden.py — 排序质量分级度量检查（P1-4，2026-09-13 新增）。

## 守的是什么缺陷

此前本仓对排序只有「过/不过」布尔检查（route 金标 12 条 + 离线矩阵），
没有任何分级度量——全仓 grep ndcg|mrr 零命中。后果：共识信号被重复计分
（① 加法先验 + ② 乘法 boost）、五维权重与维度动态范围错配这类**排序质量**
缺陷，没有任何检查能看见，只能靠人读代码发现。

## 判据

`scripts/ranking_eval.py` 对 `tests/golden/ranking_golden.json` 的 18 条
合成金标跑 rrf_merge + local_five_dim_rerank，逐条计算 MRR@10 / nDCG@10 /
共识位次 / rerank_dims 维度期望。floor_* 是 2026-09-13 在 P1-1（删乘法
boost）/P1-2（保底分外移）改造**之前**冻结的实测基线：

  - 任何排序改动让任一 case 低于 floor → 检查红（不许盲改）
  - 若改动确实变好，把 floor 更新为新值并在 _meta.baseline_note 记一笔

维度期望（expect.dims）单独锁 floor 类机制的精确取值——保底分外移到
config 后，这些维度值必须逐位不变。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def test_golden_metrics_at_or_above_floor():
    import ranking_eval

    report = ranking_eval.evaluate_all()
    golden_doc = json.loads(
        (ROOT / "tests" / "golden" / "ranking_golden.json").read_text(
            encoding="utf-8"))
    bad = ranking_eval.check_floors(report, golden_doc)
    assert not bad, (
        "排序金标低于基线（排序改动有回归，先量化再动刀）：\n"
        + "\n".join(bad)
    )


def test_golden_file_health():
    golden_doc = json.loads(
        (ROOT / "tests" / "golden" / "ranking_golden.json").read_text(
            encoding="utf-8"))
    cases = golden_doc.get("cases") or []
    assert len(cases) >= 18, f"金标 case 数不足：{len(cases)}"
    ids = [c["id"] for c in cases]
    assert len(set(ids)) == len(ids), "金标 case id 重复"
    for c in cases:
        assert c.get("engines"), f"{c['id']} 缺 engines"
        assert c.get("relevant") is not None, f"{c['id']} 缺 relevant"
        assert c.get("floor_mrr") is not None, f"{c['id']} 缺 floor_mrr（基线未冻结）"
        assert c.get("floor_ndcg") is not None, f"{c['id']} 缺 floor_ndcg"
    assert golden_doc.get("_aggregate_floor"), "_aggregate_floor 缺失（基线未冻结）"
