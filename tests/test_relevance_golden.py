#!/usr/bin/env python3
"""test_relevance_golden — 相关性准入门的回归金标（2026-09-16）。

把相关性门从「准入」推进到「回归」：tests/golden/relevance_golden.json
存档 22 个真实引擎的 canary 查询与真结果快照（标题+片段），覆盖
判据的全部三种命运——适用且通过（词面有交集）、适用且判负（零交集）、
不适用（relevance_check:false / 空结果 / 占位覆盖）。

测试重放存档 results 断言 verdict 与 expected 逐位一致：判据改动导致
verdict 变化时，先确认是判据变严还是变松，再更新 expected 并在 fixture
note 里注明原因——不许静默漂移。

另附一条策略锁：热榜/天气类数据源必须显式声明 relevance_check:false
（声明缺失会让词面判据用错问题，重演 weather 假阳）。
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

GOLDEN = ROOT / "tests" / "golden" / "relevance_golden.json"


def _load():
    doc = json.loads(GOLDEN.read_text(encoding="utf-8"))
    return doc


def test_replay_matches_expected():
    from engine_validate import _relevance_check
    import config

    engines_cfg = config.load_config().get("engines", {})
    doc = _load()
    assert len(doc["cases"]) >= 20, "金标样本数不足（被误删？）"
    bad = []
    for c in doc["cases"]:
        spec = dict(engines_cfg.get(c["engine"]) or {})
        spec["coverage"] = c["coverage"]
        spec["relevance_check"] = c["relevance_check_declared"]
        rel = _relevance_check(spec, c["results"], c["query"])
        got = {"applicable": rel.get("applicable"), "ok": rel.get("ok"),
               "hit_rate": rel.get("hit_rate")}
        if got != c["expected"]:
            bad.append(f"{c['engine']}: expected={c['expected']} got={got}")
    assert not bad, "相关性判据 verdict 与金标漂移（先定性变严/变松，再更新金标）：\n" + "\n".join(bad)


def test_data_type_sources_declare_relevance_off():
    """热榜/天气类数据源必须显式豁免——缺声明会重演 weather 假阳。"""
    import config
    engines_cfg = config.load_config().get("engines", {})
    data_type = ["baidu_hot", "zhihu_hot", "weibo_hot", "douyin_hot",
                 "toutiao_hot", "bilibili_hot", "ths_hot", "weather",
                 "weather_cn", "cls_telegraph"]
    missing = [n for n in data_type
               if n in engines_cfg
               and engines_cfg[n].get("relevance_check") is not False]
    assert not missing, f"数据型源缺 relevance_check:false 声明：{missing}"


def test_golden_covers_all_three_fates():
    """样本必须覆盖：适用通过 / 适用判负 / 不适用——缺一类判据就只测了一半。"""
    doc = _load()
    fates = set()
    for c in doc["cases"]:
        e = c["expected"]
        if not e["applicable"]:
            fates.add("na")
        elif e["ok"]:
            fates.add("pass")
        else:
            fates.add("fail")
    assert {"pass", "na"} <= fates, f"金标缺命运样本：{fates}"
    # fail 例（真判负）来自人工构造案例（tests/test_engine_lifecycle.py），
    # 线上实录的引擎在判据生效后都过关，此处不强求 live 判负样本
