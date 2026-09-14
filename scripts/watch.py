#!/usr/bin/env python3
"""watch.py — 观察模式：网页快照与变化检测（Monitor 形态）。

吸纳自 2026-09 对标研究：Parallel Monitor / Seltz Monitor / Exa Monitors
三家同方向，已是 agent 搜索的标配能力。MVP 落点=本地快照 + 内容指纹比对：
抓取走 fetch_v3 降级链（含 Parallel 免费 web_fetch 级），比对用规范化文本的
sha256（空白折叠，消除抓取批次间的排版噪声），不依赖上游 Monitor 付费端点。
cron 可用：argo watch check --json。

用法：
  argo watch add <url> [--note 注释]    建立观察并抓首帧快照
  argo watch check [--url URL] [--json] 复检（默认全部已观察 URL）
  argo watch list [--json]              列出观察目标与最近状态
  argo watch remove <url>               移除观察
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from typing import Any

_SNAP_CAP = 10  # 每个 URL 保留的快照历史上限（旧的丢弃）


# ── 存储 ─────────────────────────────────────────────────────────────────────

def _store_path():
    from argo_paths import ensure_state_dir
    return ensure_state_dir("watch") / "watch.json"


def _load() -> dict[str, Any]:
    p = _store_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("urls"), dict):
            return data
    except Exception:
        pass
    return {"_v": 1, "urls": {}}


def _save(data: dict[str, Any]) -> None:
    p = _store_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(p)


# ── 快照 ─────────────────────────────────────────────────────────────────────

def _norm(text: str) -> str:
    """规范化：折叠空白。抓取批次间排版噪声不触发假变化。"""
    return re.sub(r"\s+", " ", text or "").strip()


def _content_hash(text: str) -> str:
    return hashlib.sha256(_norm(text).encode("utf-8")).hexdigest()[:16]


def _snapshot(url: str, skip_cache: bool = True) -> dict:
    """抓一帧快照；skip_cache=True 观察必须拿新鲜内容（fetch 缓存会掩盖变化）。"""
    from fetch_v3 import fetch_v3
    r = fetch_v3(url, max_chars=20000, skip_cache=skip_cache)
    content = r.get("content") or ""
    return {
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "hash": _content_hash(content) if content else "",
        "length": len(content),
        "title": str(r.get("title") or "")[:200],
        "method": str(r.get("fetch_method") or ""),
        "head": _norm(content)[:200],
        "success": bool(r.get("success")) and bool(content),
        "error": str(r.get("error") or "")[:200],
    }


def _latest(url: str, data: dict) -> dict | None:
    snaps = (data["urls"].get(url) or {}).get("snapshots") or []
    return snaps[-1] if snaps else None


# ── 子命令 ───────────────────────────────────────────────────────────────────

def cmd_add(url: str, note: str) -> dict:
    data = _load()
    entry = data["urls"].get(url) or {"note": note, "added_at":
                                      time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                      "snapshots": []}
    entry["note"] = note or entry.get("note", "")
    snap = _snapshot(url)
    entry["snapshots"] = (entry["snapshots"] + [snap])[-_SNAP_CAP:]
    data["urls"][url] = entry
    _save(data)
    return {"url": url, "snapshot": snap}


def cmd_check(only_url: str | None) -> list[dict]:
    data = _load()
    urls = [only_url] if only_url else sorted(data["urls"].keys())
    if only_url and only_url not in data["urls"]:
        return [{"url": only_url, "error": "未在观察列表中（先 argo watch add）"}]
    reports = []
    for url in urls:
        prev = _latest(url, data)
        snap = _snapshot(url)
        report: dict[str, Any] = {"url": url}
        if not snap["success"]:
            report.update({"changed": None, "error": snap["error"] or "抓取失败",
                           "kept_snapshot": prev is not None})
            # 抓取失败不改快照：网络抖动不该伪造「无变化」或「变化」
            reports.append(report)
            continue
        if prev is None:
            report.update({"changed": None, "note": "无历史快照（首帧已记录）"})
        else:
            report.update({
                "changed": snap["hash"] != prev.get("hash"),
                "previous_hash": prev.get("hash"),
                "new_hash": snap["hash"],
                "length_delta": snap["length"] - prev.get("length", 0),
                "previous_fetched_at": prev.get("fetched_at"),
            })
        entry = data["urls"][url]
        entry["snapshots"] = (entry["snapshots"] + [snap])[-_SNAP_CAP:]
        entry["last_checked"] = snap["fetched_at"]
        reports.append(report)
    _save(data)
    return reports


def cmd_list() -> list[dict]:
    data = _load()
    out = []
    for url, entry in sorted(data["urls"].items()):
        latest = (entry.get("snapshots") or [{}])[-1]
        out.append({"url": url, "note": entry.get("note", ""),
                    "added_at": entry.get("added_at"),
                    "last_checked": entry.get("last_checked"),
                    "snapshots": len(entry.get("snapshots") or []),
                    "last_hash": latest.get("hash"),
                    "last_method": latest.get("method")})
    return out


def cmd_remove(url: str) -> bool:
    data = _load()
    if url in data["urls"]:
        del data["urls"][url]
        _save(data)
        return True
    return False


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="观察模式：网页快照与变化检测")
    ap.add_argument("action", choices=["add", "check", "list", "remove"])
    ap.add_argument("url", nargs="?", default="", help="目标 URL")
    ap.add_argument("--note", default="", help="add 时的备注")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.action == "add":
        if not args.url:
            print(json.dumps({"status": "error", "error": "用法: argo watch add <url>"},
                             ensure_ascii=False))
            sys.exit(1)
        r = cmd_add(args.url, args.note)
        payload = {"status": "completed", **r}
    elif args.action == "check":
        payload = {"status": "completed", "results": cmd_check(args.url or None)}
    elif args.action == "list":
        payload = {"status": "completed", "results": cmd_list()}
    else:
        if not args.url or not cmd_remove(args.url):
            print(json.dumps({"status": "error",
                              "error": f"未找到观察目标: {args.url}"},
                             ensure_ascii=False))
            sys.exit(1)
        payload = {"status": "completed", "removed": args.url}

    if args.json or args.action in ("check", "list"):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    # 人类可读（add / remove）
    if args.action == "add":
        s = payload["snapshot"]
        if s["success"]:
            print(f"已观察 {payload['url']}")
            print(f"  首帧: hash={s['hash']} length={s['length']} method={s['method']}")
            print(f"  开头: {s['head'][:100]}")
        else:
            print(f"已记录 {payload['url']}，但首帧抓取失败: {s['error']}")
    else:
        print(f"已移除 {payload['removed']}")


if __name__ == "__main__":
    main()
