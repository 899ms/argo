#!/usr/bin/env python3
"""lang_matrix_probe.py — 多语言 × 引擎 能力矩阵实测（C 阶段量化基线）。

## 为什么需要它

argo 的对外宣称是「多语言检测与跨语言回退」，但实测发现两个未见底的问题：
  1. 语言检测只有「语系级」精度：日语「人工知能」被判成 zh
  2. 引擎在多语言下的**噪声率未知**：juejin 在阿/俄/德查询下返回 10 条、
     相关度 0.00（纯噪声），但引擎报告 status=ok

没有完整矩阵，任何「该补哪门语言」的决策都是拍脑袋。本脚本产出
「语言 × 引擎」的返回量 + 相关度，作为 B 阶段的决策依据。

## 度量口径

对每个 (引擎, 语言) 组合：
  - count   ：返回条数（0 = 无能力）
  - rel     ：相关度均值 0~1（查询词元出现在标题/摘要的比例）
  - verdict ：相关(>=0.5) / 部分(>=0.2) / 噪声(<0.2) / 空

相关度用「查询词元命中率」而非语义相似度：廉价、可解释、无需模型，
足以区分「真结果」与「热帖噪声」——实测 juejin 噪声案例得 0.00，
真结果得 0.5~1.0，判别力充分。

用法：
  python3 scripts/lang_matrix_probe.py --json /tmp/matrix.json
  python3 scripts/lang_matrix_probe.py --langs zh,ja,de --engines wikipedia,qiita
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

logging.disable(logging.CRITICAL)

# ── 必须最先执行：把 ~/.config/argo/env 的密钥同步进 os.environ ───────────
# 实测教训：首版探针直接 `import engines` 调用，绕过了 bin/argo 入口的
# sync_envfile_to_environ()，导致所有靠文件密钥的引擎（bocha/exa/octen/
# anysearch…）全部误报失败——因为 builder 用 os.environ 直读，
# 而密钥只在文件里。修法不是改探针调用方式，而是显式补上同步这一步：
# 凡是「绕过 CLI 入口直接调库」的场景都必须自己做这件事。
try:
    from engine_env import sync_envfile_to_environ
    _SYNCED = sync_envfile_to_environ()
except Exception:
    _SYNCED = []

# ── 语料：每种语言一个「语义等价」的查询（同一个主题：AI 进展）─────────────
# 语义等价是关键：这样不同语言的横向对比才有意义（比较的是「引擎能力」
# 而不是「主题差异」）。部分语言另配一个本地化主题以覆盖文化类引擎。
QUERIES: dict[str, list[str]] = {
    "zh": ["人工智能 最新进展", "北京 旅游 攻略"],
    "en": ["artificial intelligence progress", "new york travel guide"],
    "ja": ["人工知能 最新 動向", "東京 観光 おすすめ"],
    "ko": ["인공지능 최신 동향", "서울 여행 추천"],
    "ru": ["искусственный интеллект", "москва путешествие"],
    "ar": ["الذكاء الاصطناعي", "دبي سياحة"],
    "de": ["künstliche Intelligenz", "berlin reise"],
    "fr": ["intelligence artificielle", "paris voyage"],
    "es": ["inteligencia artificial", "madrid viaje"],
    "pt": ["inteligência artificial", "lisboa viagem"],
    "it": ["intelligenza artificiale", "roma viaggio"],
    "nl": ["kunstmatige intelligentie", "amsterdam reis"],
    "pl": ["sztuczna inteligencja", "warszawa podróż"],
    "tr": ["yapay zeka", "istanbul seyahat"],
    "vi": ["trí tuệ nhân tạo", "hà nội du lịch"],
    "th": ["ปัญญาประดิษฐ์", "ท่องเที่ยว กรุงเทพ"],
    "hi": ["कृत्रिम बुद्धिमत्ता", "दिल्ली यात्रा"],
    "he": ["בינה מלאכותית", "תל אביב טיול"],
}

# ── 待测引擎：语言相关的（内容型），语言中立的（科学数据库）不测 ──────────
ENGINES = [
    # 通用搜索
    "wikipedia", "zh_wikipedia", "wikidata", "anysearch",
    # 社区
    "juejin", "qiita", "v2ex", "hackernews", "devto", "stackoverflow",
    "zhihu_global", "hatena_bookmark",
    # 新闻/媒体
    "gdelt", "byted", "you", "em_global_news",
    # 文化/书籍
    "moegirl", "ndl", "gutenberg", "dnb", "open_library", "know_your_meme",
    # 独立索引
    "marginalia", "wiby", "lieu", "searchmysite",
    # 中国源（对照组）
    "bocha", "tavily", "exa",
]


def relevance(query: str, item: dict) -> float:
    """查询词元在结果中的命中率（0~1）。口径见模块 docstring。"""
    blob = ((item.get("title") or "") + " " + (item.get("snippet") or "")).lower()
    if not blob.strip():
        return 0.0
    # 拉丁词元
    toks = [t for t in re.findall(r"[a-z]{3,}", query.lower())]
    if toks:
        return sum(1 for t in toks if t in blob) / len(toks)
    # CJK / 日文假名
    cjk = re.findall(r"[\u4e00-\u9fff\u3040-\u30ff]{2,}", query)
    if cjk:
        return sum(1 for c in cjk if c in blob) / len(cjk)
    # 韩文
    han = re.findall(r"[\uac00-\ud7af]{2,}", query)
    if han:
        return sum(1 for c in han if c in blob) / len(han)
    # 西里尔 / 阿拉伯 / 天城文 / 希伯来：整词
    words = [w for w in re.split(r"\s+", query) if len(w) >= 3]
    if words:
        return sum(1 for w in words if w in blob) / len(words)
    return 0.0


def _one_call(eng: str, q: str, n: int, timeout: float) -> dict:
    """单次 (引擎, 查询) 调用 → cell 结果。任何异常都收敛为 error 字段。"""
    import engines as eng_mod
    t0 = time.time()
    try:
        raw = eng_mod.search(q, eng, n=n, timeout=timeout) or []
        err = None
    except Exception as e:
        raw, err = [], f"{type(e).__name__}"
    # 过滤 error 占位条目：引擎失败时会返回 [{"error": "...", "source": ...}]，
    # 若不剔除，它会被当成「1 条结果、相关度 0」→ 误判为 noise。
    # 实测影响：you/parallel 缺密钥时正是这种形态，早期版本把它们记成噪声而非故障。
    results = [r for r in raw
               if isinstance(r, dict) and not r.get("error")]
    if raw and not results and not err:
        err = str(raw[0].get("error"))[:60] if isinstance(raw[0], dict) else "error-only"
    lat = round((time.time() - t0) * 1000)
    if results:
        sc = [relevance(q, x) for x in results]
        rel = round(sum(sc) / len(sc), 3)
    else:
        rel = 0.0
    return {"query": q, "count": len(results), "rel": rel,
            "latency_ms": lat, "error": err}


def _verdict(total: int, avg_rel: float) -> str:
    if total == 0:
        return "empty"
    if avg_rel >= 0.5:
        return "good"
    if avg_rel >= 0.2:
        return "partial"
    return "noise"


def probe(engines: list[str], langs: list[str], n: int = 5,
          timeout: float = 12.0, workers: int = 8,
          out_path: str | None = None, quiet: bool = False) -> dict:
    """并发探测「语言 × 引擎」矩阵。

    ## 为什么必须并发（实测教训）

    首版是三重串行循环：18 语言 × 28 引擎 × 2 查询 = 1008 次网络调用。
    实测跑 35 分钟仅消耗 12 秒 CPU —— 即 99% 时间在等网络。
    改为 8 并发后同样的工作量约 2-3 分钟。

    ## 增量落盘

    每完成一批（batch）就把当前矩阵写入 out_path。这样即使中途被中断，
    已得数据仍可用——长任务不该「要么全有要么全无」。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    tasks: list[tuple[str, str, str]] = []
    for lang in langs:
        for q in (QUERIES.get(lang) or []):
            for eng in engines:
                tasks.append((lang, eng, q))

    total_tasks = len(tasks)
    if not quiet:
        print(f"计划 {total_tasks} 次调用（{len(langs)} 语言 × {len(engines)} 引擎 "
              f"× 若干查询），并发 {workers}", flush=True)

    # cells[(lang, eng)] = [cell, ...]
    cells: dict[tuple[str, str], list[dict]] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_one_call, eng, q, n, timeout): (lang, eng, q)
                for lang, eng, q in tasks}
        for fut in as_completed(futs):
            lang, eng, q = futs[fut]
            try:
                cell = fut.result()
            except Exception as e:  # 理论上 _one_call 不抛，双保险
                cell = {"query": q, "count": 0, "rel": 0.0,
                        "latency_ms": 0, "error": f"future:{type(e).__name__}"}
            cells.setdefault((lang, eng), []).append(cell)
            done += 1
            # 增量落盘：每 50 次完成写一次
            if out_path and done % 50 == 0:
                _flush_matrix(cells, langs, engines, out_path,
                              {"langs": langs, "engines": engines, "n": n,
                               "progress": f"{done}/{total_tasks}",
                               "partial": True})
            if not quiet and done % 100 == 0:
                print(f"  进度 {done}/{total_tasks}", flush=True)

    # 组装矩阵
    matrix: dict[str, dict] = {}
    for lang in langs:
        if not (QUERIES.get(lang) or []):
            continue
        matrix[lang] = {}
        for eng in engines:
            cl = cells.get((lang, eng)) or []
            if not cl:
                continue
            total = sum(c["count"] for c in cl)
            avg_rel = (sum(c["rel"] * c["count"] for c in cl) / total
                       if total else 0.0)
            matrix[lang][eng] = {
                "total": total, "avg_rel": round(avg_rel, 3),
                "verdict": _verdict(total, avg_rel), "cells": cl,
            }
    return matrix


def _flush_matrix(cells: dict, langs: list[str], engines: list[str],
                  out_path: str, meta: dict) -> None:
    """把当前 cells 组装成矩阵并落盘（增量与最终共用）。"""
    matrix: dict[str, dict] = {}
    for lang in langs:
        row: dict[str, dict] = {}
        for eng in engines:
            cl = cells.get((lang, eng)) or []
            if not cl:
                continue
            total = sum(c["count"] for c in cl)
            avg_rel = (sum(c["rel"] * c["count"] for c in cl) / total
                       if total else 0.0)
            row[eng] = {"total": total, "avg_rel": round(avg_rel, 3),
                        "verdict": _verdict(total, avg_rel), "cells": cl}
        if row:
            matrix[lang] = row
    out = {"matrix": matrix, "summary": summarize(matrix), "meta": meta}
    Path(out_path).write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


def summarize(matrix: dict) -> dict:
    """汇总：每语言可用引擎数 / 每引擎的语言覆盖。"""
    per_lang, per_eng = {}, {}
    for lang, row in matrix.items():
        good = [e for e, m in row.items() if m["verdict"] == "good"]
        partial = [e for e, m in row.items() if m["verdict"] == "partial"]
        noise = [e for e, m in row.items() if m["verdict"] == "noise"]
        per_lang[lang] = {"good": len(good), "partial": len(partial),
                          "noise": len(noise), "good_engines": good,
                          "noise_engines": noise}
        for e, m in row.items():
            per_eng.setdefault(e, {})[lang] = m["verdict"]
    eng_summary = {}
    for e, langs in per_eng.items():
        eng_summary[e] = {
            "good_langs": [l for l, v in langs.items() if v == "good"],
            "noise_langs": [l for l, v in langs.items() if v == "noise"],
            "good_count": sum(1 for v in langs.values() if v == "good"),
        }
    return {"per_lang": per_lang, "per_engine": eng_summary}


def main() -> None:
    ap = argparse.ArgumentParser(description="多语言 × 引擎能力矩阵实测")
    ap.add_argument("--langs", default=",".join(QUERIES.keys()))
    ap.add_argument("--engines", default=",".join(ENGINES))
    ap.add_argument("-n", type=int, default=5)
    ap.add_argument("--timeout", type=float, default=12.0)
    ap.add_argument("--workers", type=int, default=8,
                    help="并发数（串行版实测 35 分钟只花 12s CPU，必须并发）")
    ap.add_argument("--json", help="结果落盘路径（每 50 次调用增量写一次）")
    args = ap.parse_args()

    langs = [x.strip() for x in args.langs.split(",") if x.strip()]
    engines = [x.strip() for x in args.engines.split(",") if x.strip()]

    t0 = time.time()
    matrix = probe(engines, langs, n=args.n, timeout=args.timeout,
                   workers=args.workers, out_path=args.json)
    elapsed = round(time.time() - t0, 1)
    summary = summarize(matrix)

    if args.json:
        _flush_matrix(
            {(l, e): m["cells"] for l, row in matrix.items() for e, m in row.items()},
            langs, engines, args.json,
            {"langs": langs, "engines": engines, "n": args.n,
             "elapsed_s": elapsed, "partial": False},
        )

    # 控制台摘要
    print(f"\n耗时 {elapsed}s · {len(langs)} 语言 × {len(engines)} 引擎\n")
    print(f"{'语言':<6}{'可用':>6}{'部分':>6}{'噪声':>6}  可用引擎")
    print("-" * 72)
    for lang, s in summary["per_lang"].items():
        print(f"{lang:<6}{s['good']:>6}{s['partial']:>6}{s['noise']:>6}  "
              f"{', '.join(s['good_engines'][:5])}")
    print()
    print(f"{'引擎':<18}{'良好语言数':>10}  良好语言")
    print("-" * 72)
    ranked = sorted(summary["per_engine"].items(),
                    key=lambda x: -x[1]["good_count"])
    for e, s in ranked:
        print(f"{e:<18}{s['good_count']:>10}  {', '.join(s['good_langs'][:8])}")


if __name__ == "__main__":
    main()
