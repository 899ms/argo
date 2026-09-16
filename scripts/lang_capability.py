#!/usr/bin/env python3
"""lang_capability.py — 语言能力画像：让路由按「引擎在某语言下真的行」来选。

## 问题（C 阶段实测）

18 语言 × 29 引擎矩阵（`scripts/lang_matrix_probe.py`）暴露两件事：

1. **语言覆盖极不均衡**：西班牙语 12 个可用引擎，泰语只有 3 个；
   日语只有 5 个——而 argo 明明有 Qiita / NDL / 萌娘百科等日文资源。
2. **「可用」不等于「被用上」**：路由此前只看 `coverage` 字段（157 个引擎里
   109 个根本没声明语言），于是「引擎在某语言下真的有内容」这个事实
   完全没进入决策。

## 机制

把矩阵实测结果固化为**能力画像**（`data/lang_matrix/lang_capability.json`），
路由据此对候选引擎打语言适配分。

设计取舍——**建议而非硬过滤**：
  · 硬过滤的风险：矩阵是单次实测，含网络抖动与查询抽样偏差；
    一旦硬砍，某次抖动会让整门语言失去检索能力。
  · 加权则两头兼顾：能力强的引擎优先，未知的保持原样（不惩罚），
    实测为噪声的降权（不禁止）。
  · 画像缺失/过期时**完全退化为原行为**——这是安全保底。

## 数据来源与新鲜度

画像由 `lang_matrix_probe.py` 生成。调用方不应假设它永远存在；
`available()` 返回 False 时所有查询按原权重走。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

_PROFILE_PATH = Path(__file__).resolve().parent.parent / "data" / "lang_matrix" / "lang_capability.json"

# 画像最大可接受年龄（秒）。超过则视为过期——语言能力会随上游改版变化，
# 陈年数据不该继续影响路由。30 天是「够用且不至于陈旧」的折中。
MAX_AGE_S = 30 * 24 * 3600

# 评分权重
BOOST_GOOD = 1.15      # 实测该语言下良好 → 提权
PENALTY_NOISE = 0.85   # 实测该语言下噪声 → 降权
NEUTRAL = 1.0          # 无数据 → 不动

_cache: dict[str, Any] = {}


def _load() -> dict[str, Any] | None:
    """读能力画像（带进程内缓存）。缺失/损坏/过期返回 None。"""
    if "profile" in _cache:
        return _cache["profile"]
    profile = None
    try:
        st = _PROFILE_PATH.stat()
        if time.time() - st.st_mtime <= MAX_AGE_S:
            data = json.loads(_PROFILE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                profile = data
    except Exception:
        profile = None
    _cache["profile"] = profile
    return profile


def available() -> bool:
    """画像是否可用。False 时调用方应完全按原行为处理。"""
    return _load() is not None


def engines_for_lang(lang: str) -> set[str]:
    """该语言下实测良好的引擎集合（画像缺失时返回空集）。"""
    p = _load()
    if not p:
        return set()
    langs = p.get("lang_engines") or {}
    # 语系级标签归一：detect_language 可能给 latin/cyrillic，
    # 而画像按具体语种建（de/fr/ru…）。此时合并该语系全部成员。
    members = _expand_lang(lang)
    out: set[str] = set()
    for m in members:
        for e in (langs.get(m) or []):
            out.add(e)
    return out


def noise_engines_for_lang(lang: str) -> set[str]:
    """该语言下实测为噪声的引擎（用于降权）。"""
    p = _load()
    if not p:
        return set()
    per_lang = ((p.get("summary") or {}).get("per_lang")) or {}
    members = _expand_lang(lang)
    out: set[str] = set()
    for m in members:
        for e in (per_lang.get(m, {}).get("noise_engines") or []):
            out.add(e)
    return out


_FAMILY = {
    "latin": ("en", "de", "fr", "es", "pt", "it", "nl", "pl", "tr", "vi"),
    "cyrillic": ("ru",),
}


def _expand_lang(lang: str) -> tuple[str, ...]:
    """**仅当输入是语系标签**时展开为具体语种成员；具体语种原样返回。

    为什么不对具体语种做同族扩展（实测教训）：
      detect_language 对语言明确的查询返回具体标签（zh/ja/de/ru…），
      只有判不出来时才返回语系标签（latin/cyrillic）。而**同语系的
      语言能力并不通用**——实测 zh_wikipedia 擅长中文（zh/en/fr/es…）
      却完全不胜任日语；ndl 擅长日语却不擅长中文。若把 ja 展开成
      (zh, ja)，中文引擎的能力会被误算到日语头上。

      故：具体语种 → 精确匹配；语系标签 → 展开（因为此时确实不知道
      是哪种语言，只能取该语系并集作为参考）。
    """
    if not lang:
        return ()
    if lang in _FAMILY:
        return _FAMILY[lang]
    return (lang,)


def score_adjust(engine: str, lang: str) -> float:
    """返回该引擎在该语言下的权重系数（1.0 表示不调整）。

    调用方把返回值乘到引擎原有评分上即可。画像缺失时恒为 1.0。
    """
    if not engine or not lang:
        return NEUTRAL
    p = _load()
    if not p:
        return NEUTRAL
    if engine in engines_for_lang(lang):
        return BOOST_GOOD
    if engine in noise_engines_for_lang(lang):
        return PENALTY_NOISE
    return NEUTRAL


def reload() -> None:
    """清空缓存（测试与画像更新后调用）。"""
    _cache.pop("profile", None)
