#!/usr/bin/env python3
"""
sync_backends.py — 注册表派生与一致性校验（唯一来源：运行时合并后的引擎声明）

设计目标：新增引擎只改一处声明，其余注册表自动派生，消灭手工同步。

来源计算方式（与运行时一致）：`config.load_config()` 合并后的 engines 段，即
  config.yaml engines + engines/*.yaml + engines/specs/*.yaml
只读 config.yaml 是不够的——外置 spec 声明的引擎运行时可见、可路由，
若派生时看不见，派生件就会与运行时事实脱钩（batch7 收录的 7 个引擎
曾因此只出现在人工维护的 registry 里，而 quota/domain 两份漏侧）。

派生关系：
  运行时合并后的 engines 段（唯一来源）
    ├── backends/quota_profiles.json    配额/成本/限频（由引擎声明的元数据派生）
    ├── backends/engine_registry.yaml   引擎注册表文档（由引擎声明派生）
    └── backends/domain_profiles.json   TF-IDF 领域文档（校验引擎名集合，缺失补空模板）

用法：
  python3 scripts/sync_backends.py             # 派生三份 backends 文件
  python3 scripts/sync_backends.py --check     # 只校验不写，不一致退出码非 0
  python3 scripts/sync_backends.py --list      # 输出引擎清单与统计

退出码：0=一致，1=校验发现不一致（--check 模式）。

--check 读的是磁盘上的三份派生件，与重新派生的结果逐项比对。比对对象必须是
「磁盘现状」而不是「本次派生结果」——后者等于拿结果和自己比，永远一致，
校验形同虚设（本文件 2026-09-12 前的实际状态）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from cli_io import dumps

# ── 路径 ──────────────────────────────────────────────────────────────────────

SKILL_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = SKILL_DIR / "config.yaml"
BACKENDS_DIR = SKILL_DIR / "backends"
QUOTA_PROFILES_PATH = BACKENDS_DIR / "quota_profiles.json"
DOMAIN_PROFILES_PATH = BACKENDS_DIR / "domain_profiles.json"
REGISTRY_PATH = BACKENDS_DIR / "engine_registry.yaml"

# ── 默认值 ────────────────────────────────────────────────────────────────────

DEFAULT_QUOTA: dict[str, Any] = {
    "qps": 2,
    "limit": None,
    "period": "second",
    "cost_per_call": 0.0,
    "cost_unit": "free",
    "cost_tier": "free",
    "priority": 50,
}

DEFAULT_COST_FACTOR = {"free": 1.0, "low": 0.7, "api": 0.5, "paid": 0.3}

# registry 里 cost 字段的计算方式（free/token/api），与 cost_tier 的映射
_COST_TIER_TO_REGISTRY_COST = {"free": "free", "low": "api", "api": "api", "paid": "api"}


def _load_yaml(path: Path) -> dict:
    from yaml_load import load as _yaml_parse
    data = _yaml_parse(path)
    return data if isinstance(data, dict) else {}


def load_engines() -> dict[str, dict[str, Any]]:
    """读取运行时合并后的全部引擎声明（含禁用）。

    与运行时同一入口（config.load_config 合并 config.yaml + engines/*.yaml +
    engines/specs/*.yaml）；config 模块不可用时（缺依赖/语法错）退回只读
    config.yaml，并在 stderr 明示——静默退回会让派生件少一批引擎却不报错。
    """
    try:
        from config import load_config
        merged = load_config().get("engines", {})
        if merged:
            return {n: s for n, s in merged.items() if isinstance(s, dict)}
    except Exception as e:  # noqa: BLE001 — 退回单源并留痕，不吞错误
        print(f"⚠️  config 模块不可用（{type(e).__name__}: {e}），"
              f"退回只读 config.yaml——派生件将不含外置 spec 引擎",
              file=sys.stderr)
    cfg = _load_yaml(CONFIG_PATH)
    engines = cfg.get("engines", {})
    return {name: spec for name, spec in engines.items() if isinstance(spec, dict)}


def engine_meta(spec: dict[str, Any], name: str) -> dict[str, Any]:
    """提取引擎声明的运营元数据，缺失用默认值保底。"""
    meta: dict[str, Any] = {}
    for key, default in DEFAULT_QUOTA.items():
        meta[key] = spec.get(key, default)
    meta["label"] = spec.get("label", name)
    return meta


# ── 派生：quota_profiles.json ────────────────────────────────────────────────

def derive_quota_profiles(engines: dict[str, dict[str, Any]]) -> dict[str, Any]:
    profiles: dict[str, Any] = {
        "_description": "各引擎的配额/成本/限频配置。由 scripts/sync_backends.py 从运行时引擎声明派生，勿手工修改。",
    }
    for name, spec in sorted(engines.items()):
        meta = engine_meta(spec, name)
        profiles[name] = {
            "label": meta["label"],
            "qps": meta["qps"],
            "limit": meta["limit"],
            "period": meta["period"],
            "cost_per_call": meta["cost_per_call"],
            "cost_unit": meta["cost_unit"],
            "cost_tier": meta["cost_tier"],
            "priority": meta["priority"],
        }
    return profiles


# ── 派生：engine_registry.yaml ───────────────────────────────────────────────

def derive_registry(engines: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """从引擎声明派生注册表文档。coverage/desc 从引擎声明读，缺省用通用标签。"""
    entries = []
    for name, spec in sorted(engines.items()):
        enabled = spec.get("enabled", True)
        etype = spec.get("type", "cli")
        cost_tier = spec.get("cost_tier", "free")
        # tier 计算方式：T1 直连 API / T2 local-search 本地引擎
        tier = "T2" if name.startswith("local_") else "T1"
        # desc 优先级：声明自述 → label 保底。spec 侧 desc 是权威自述（外置 spec
        # 只声明 engine_id/desc，没有 label），此前只认 label 导致 14 个引擎
        # 的 desc 派生为空——外置声明的引擎在注册表里集体失语。
        label = spec.get("label")
        desc = spec.get("desc") or (f"{label}（{etype}）".replace("（cli）", "") if label else "")
        entries.append({
            "name": name,
            "tier": tier,
            "type": etype,
            "coverage": spec.get("coverage", ["general"]),
            "latency_ms": spec.get("latency_ms", 2000),
            "cost": spec.get("cost", _COST_TIER_TO_REGISTRY_COST.get(cost_tier, "free")),
            "status": "ok" if enabled else "disabled",
            "recommended": spec.get("recommended", True),
            # explicit_only：设计上不进自动路由（需密钥的源 / 输入形态特殊），
            # 按 --engine 显式调用；可达性检查据此区分「有意显式」与「忘了接线」
            "explicit_only": bool(spec.get("explicit_only")),
            "desc": desc,
        })
    return {
        "_generated": "由 scripts/sync_backends.py 从运行时引擎声明派生，勿手工修改。",
        "version": 2,
        "last_updated": __import__("datetime").date.today().isoformat(),
        "engines": entries,
    }


# ── 校验/补全：domain_profiles.json ─────────────────────────────────────────

def check_domain_profiles(engines: dict[str, dict[str, Any]], profiles: dict[str, Any]) -> list[str]:
    """校验 domain_profiles 引擎名集合与 config 一致。返回问题列表。"""
    issues = []
    config_names = set(engines.keys())
    domain_names = set(k for k in profiles if not k.startswith("_"))
    missing = sorted(config_names - domain_names)
    stale = sorted(domain_names - config_names)
    if missing:
        issues.append(f"domain_profiles 缺失引擎: {missing}")
    if stale:
        issues.append(f"domain_profiles 含 config 已不存在的引擎: {stale}")
    return issues


def patch_domain_profiles(engines: dict[str, dict[str, Any]], profiles: dict[str, Any]) -> dict[str, Any]:
    """为缺失引擎补空模板（documents 留空，TF-IDF 对无文档引擎返回零向量，不影响路由）。"""
    patched = dict(profiles)
    for name, spec in engines.items():
        if name in patched:
            continue
        patched[name] = {
            "label": spec.get("label", name),
            "documents": [],
            "boost_keywords": {},
            "boost_combos": {},
        }
    # 移除 config 已不存在的引擎（孤儿条目）
    for name in [k for k in patched if not k.startswith("_") and k not in engines]:
        del patched[name]
    return patched


# ── 主流程 ───────────────────────────────────────────────────────────────────

def collect_issues(engines: dict[str, dict[str, Any]],
                   quota: dict[str, Any], registry: dict[str, Any],
                   domain: dict[str, Any]) -> list[str]:
    """汇总所有一致性检查问题（引擎名 + 字段值双层）。

    只比引擎名是不够的：名字都在、值被手改（firecrawl.qps 1→99）同样会让
    派生件与来源脱钩，而限流/配额恰恰是靠这些值生效的。第一版校验只比名字，
    把 qps 手改成 99 试一次，它直接漏报。
    """
    issues = []
    config_names = set(engines.keys())
    quota_names = set(k for k in quota if not k.startswith("_"))
    reg_entries = {e["name"]: e for e in registry.get("engines", [])}
    reg_names = set(reg_entries)

    if missing := sorted(config_names - quota_names):
        issues.append(f"quota_profiles 缺失引擎: {missing}")
    if stale := sorted(quota_names - config_names):
        issues.append(f"quota_profiles 含 config 已不存在的引擎: {stale}")
    if missing := sorted(config_names - reg_names):
        issues.append(f"engine_registry 缺失引擎: {missing}")
    if stale := sorted(reg_names - config_names):
        issues.append(f"engine_registry 含 config 已不存在的引擎: {stale}")

    # quota 值级一致性：声明的运营元数据必须逐字段落到派生件上
    expected_quota = derive_quota_profiles(engines)
    for name in sorted(config_names & quota_names):
        exp = expected_quota.get(name, {})
        cur = quota.get(name, {})
        if not isinstance(cur, dict):
            issues.append(f"quota_profiles {name} 不是对象: {type(cur).__name__}")
            continue
        for key, want in exp.items():
            if cur.get(key) != want:
                issues.append(
                    f"quota_profiles {name}.{key} 不一致: 真源={want!r} 派生件={cur.get(key)!r}")

    # registry 值级一致性（跳过生成时间戳：它不是声明派生的）
    expected_reg = {e["name"]: e for e in derive_registry(engines)["engines"]}
    for name in sorted(config_names & reg_names):
        exp = expected_reg.get(name, {})
        cur = reg_entries.get(name, {})
        for key in ("tier", "type", "coverage", "latency_ms", "cost",
                    "status", "recommended", "explicit_only", "desc"):
            if exp.get(key) != cur.get(key):
                issues.append(
                    f"engine_registry {name}.{key} 不一致: 真源={exp.get(key)!r} "
                    f"派生件={cur.get(key)!r}")

    issues.extend(check_domain_profiles(engines, domain))
    return issues


def write_quota(quota: dict[str, Any]) -> None:
    QUOTA_PROFILES_PATH.write_text(
        json.dumps(quota, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_registry(registry: dict[str, Any]) -> None:
    import yaml
    REGISTRY_PATH.write_text(
        yaml.safe_dump(registry, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8")


def write_domain(domain: dict[str, Any]) -> None:
    DOMAIN_PROFILES_PATH.write_text(
        json.dumps(domain, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="注册表派生与一致性校验（真源：运行时合并后的引擎声明）")
    parser.add_argument("--check", action="store_true", help="只校验不写，不一致退出码 1")
    parser.add_argument("--list", action="store_true", help="输出引擎清单与统计")
    args = parser.parse_args()

    engines = load_engines()
    if args.list:
        tiers: dict[str, list[str]] = {}
        for n, s in engines.items():
            tiers.setdefault(s.get("cost_tier", "free"), []).append(n)
        # 计算方式收紧（D7）：默认数字只有一个——「声明合并后的引擎数」。
        # enabled 与 disabled 由同一入口的 env 就绪判定派生，不再各自成数。
        declared = len(engines)
        try:
            from config import get_engines
            ready_names = set(get_engines() or {})
        except Exception:
            ready_names = {n for n, s in engines.items() if s.get("enabled", True)}
        enabled_declared = {n for n, s in engines.items() if s.get("enabled", True)}
        print(dumps({
            # 默认计算方式：运行时可见的引擎声明总数
            "total": declared,
            "enabled": len(ready_names),
            "disabled": declared - len(ready_names),
            "enabled_declared": len(enabled_declared),
            "by_cost_tier": {k: len(v) for k, v in tiers.items()},
            "engines": sorted(engines),
        }))
        return 0

    quota_cur = {}
    domain_cur = {}
    if QUOTA_PROFILES_PATH.exists():
        try:
            quota_cur = json.loads(QUOTA_PROFILES_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    if DOMAIN_PROFILES_PATH.exists():
        try:
            domain_cur = json.loads(DOMAIN_PROFILES_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    # registry 现状也要读：校验必须拿「磁盘上的派生件」比对，而不是拿本次
    # 派生结果比对——后者是拿结果跟自己比，恒等，校验永远绿。
    registry_cur: dict[str, Any] = {}
    if REGISTRY_PATH.exists():
        registry_cur = _load_yaml(REGISTRY_PATH)

    quota_new = derive_quota_profiles(engines)
    registry_new = derive_registry(engines)
    domain_new = patch_domain_profiles(engines, domain_cur)

    # 校验对象是「磁盘现状 vs 来源」，与本次派生了什么无关——派生结果在这里
    # 只用于写入，不用于自行验证。
    issues = collect_issues(engines, quota_cur, registry_cur, domain_cur)

    if args.check:
        if issues:
            print("❌ 一致性校验失败：", file=sys.stderr)
            for issue in issues:
                print(f"  - {issue}", file=sys.stderr)
            print("  修复：python3 scripts/sync_backends.py", file=sys.stderr)
            return 1
        print(f"✅ 一致性校验通过：{len(engines)} 个引擎，"
              f"三份派生件与运行时声明一致。")
        return 0

    write_quota(quota_new)
    write_registry(registry_new)
    write_domain(domain_new)
    print(f"已派生 {len(engines)} 个引擎的注册表：")
    print(f"  quota_profiles.json   {len(quota_new) - 1} 条")
    print(f"  engine_registry.yaml  {len(registry_new['engines'])} 条")
    print(f"  domain_profiles.json  {len([k for k in domain_new if not k.startswith('_')])} 条（缺失已补空模板）")
    if issues:
        print(f"  已修复 {len(issues)} 处此前与真源不一致的项：", file=sys.stderr)
        for issue in issues:
            print(f"  ⚠️  {issue}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
