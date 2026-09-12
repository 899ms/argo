#!/usr/bin/env python3
"""result_lang.py — 结果侧语言判定与噪声识别（语言能力的第一性机制）。

## 为什么是「结果侧」

argo 原有机制在**查询侧**猜语言（`lang_detect.detect_language`），实测有三类失误：

    「人工知能 最新 動向」（日语）→ 判成 zh      # 纯汉字短查询，无假名信号
    「künstliche」（德语）        → 判成 latin   # 拉丁语系内不分语种
    「inteligencia」（西语）      → 判成 en

根因是结构性的：**查询文本短、信号稀疏，且汉字在 Unicode 里跨语言共享**
（「動」「动」都是 CJK 统一表意文字），单看字符无法定语言。

修这条路时我试过三种补法，全部失败：
    ① 扩特征词表      → 漏「人工知能」（穷举注定漏，表已有 70+ 词）
    ② 字符集判别      → 手列字表准确率仅 39%（漏「観」「経」「鉄」）
    ③ 大小写/变音符   → 只能区分部分拉丁语言

**共同失败原因：都在试图从「短查询」反推「语言」，而这是信息不足的推断。**

## 第一性转向

语言识别的目的不是「给查询分类」，而是「**选择检索目标并筛掉噪声**」。
而「结果用了什么文字」是**确定性事实**（Unicode 编码），不需要推断：

    含假名(kana)   → 必是日语     100% 确定
    含谚文(hangul) → 必是韩语     100% 确定
    纯 Han         → 中日文之一   （此时再看是否含日文特有字形/词汇）
    纯西里尔       → 俄/乌/保等    （语种级不确定，但语系级确定）

于是把判定位置从「查询侧」后移到「结果侧」：**不猜，直接读**。

## 顺带解决噪声问题

实测发现引擎在非支持语言下会返回「成功但不相关」的结果：juejin 在
阿拉伯语查询下返回 10 条、相关度 **0.00**（全是通用热帖），而引擎报告
status=ok。这与第一批 V2EX「失败伪装成成功」同构。

本模块的 `assess_results` 同时给出：
  - lang          结果的主语言（确定性判据）
  - lang_match    结果语言与期望语言是否一致
  - relevance     查询词元在结果中的命中率
  - noise         noise / low / ok —— 噪声判定

噪声判定不依赖语言识别精度：**结果语言不符 且 相关度低** 即为噪声。
"""

from __future__ import annotations

import re
from typing import Any

# ── 书写系统检测（确定性，基于 Unicode 码位区间）──────────────────────────
_RANGES = (
    ("han",       r"[\u4e00-\u9fff\u3400-\u4dbf]"),
    ("kana",      r"[\u3040-\u309f\u30a0-\u30ff]"),
    ("hangul",    r"[\uac00-\ud7af\u1100-\u11ff]"),
    ("cyrillic",  r"[\u0400-\u04ff]"),
    ("arabic",    r"[\u0600-\u06ff\u0750-\u077f]"),
    ("hebrew",    r"[\u0590-\u05ff]"),
    ("greek",     r"[\u0370-\u03ff]"),
    ("thai",      r"[\u0e00-\u0e7f]"),
    ("devanagari", r"[\u0900-\u097f]"),
    ("latin",     r"[A-Za-z\u00c0-\u024f]"),
)
_COMPILED = [(name, re.compile(pat)) for name, pat in _RANGES]

# 书写系统 → 语言标签（确定性映射部分）
_SCRIPT_TO_LANG = {
    "kana": "ja",       # 含假名必是日语
    "hangul": "ko",     # 含谚文必是韩语
    "cyrillic": "ru",   # 语系级（俄/乌/保共用西里尔）
    "arabic": "ar",
    "hebrew": "he",
    "greek": "el",
    "thai": "th",
    "devanagari": "hi",
}

# 抽样条数：判定语言只需前若干条，避免长文本全量扫描
_SAMPLE_ITEMS = 5

# 噪声判定阈值
NOISE_RELEVANCE = 0.2      # 相关度低于此 + 语言不符 → 噪声
OK_RELEVANCE = 0.5         # 相关度高于此 → 直接判 ok


def script_profile(text: str) -> dict[str, float]:
    """返回文本的书写系统构成比例（确定性，非概率）。"""
    if not text:
        return {}
    total = len([c for c in text if not c.isspace()])
    if total == 0:
        return {}
    counts: dict[str, int] = {}
    for name, rx in _COMPILED:
        n = len(rx.findall(text))
        if n:
            counts[name] = n
    return {k: round(v / total, 3) for k, v in counts.items()}


def detect_result_lang(text: str) -> str:
    """判定文本语言（确定性判据优先，不确定时如实返回语系级标签）。

    与 lang_detect.detect_language 的区别：
      - 本函数用于**结果文本**（长、信号足），可直接读出语言
      - 前者用于**查询**（短、信号少），只能给语系级近似
    返回值可能是语系级标签（latin/cyrillic），调用方不应假设总是语种级。
    """
    prof = script_profile(text)
    if not prof:
        return "other"
    # 假名/谚文是 100% 确定的语种信号，优先判
    for script, lang in _SCRIPT_TO_LANG.items():
        if prof.get(script, 0) >= 0.1:
            return lang
    han = prof.get("han", 0)
    latin = prof.get("latin", 0)
    if han >= 0.3:
        # 纯汉字：中日文共享，看是否含日文特有字形
        if _has_japanese_kanji(text):
            return "ja"
        return "zh"
    if latin >= 0.5:
        return _latin_subtype(text)
    if han and latin:
        return "mixed"
    return "other"


# 日文新字体：这些字形在简体中文里不使用（或极罕见）。
# 注意：这是**辅助判据**而非主判据——实测纯靠它准确率不足（手列字表
# 只有 39%），故仅在「已知是纯汉字」时作为中日之间的 tie-breaker。
_JA_SHINJITAI = frozenset(
    "動報圓學國經濟實會體樂醫藥藝廣齒圖團營覺觀權嚴驗邊賣讀續斷歸歳"
    "發變驛鐵價劍戰戲戶掛採擴據擔敎廳氣淨獵獸現畫盡硏碎祕莊藏虛號觸"
    "訓設證評譯護譽豐貨費資質贊贈轉輕載農遲遺鄕醫釋鍾鎖陸險雜靈靜響"
    "頁順預領頭題顏願類顯風飛餘飯館駐驚骨髮鬧鬥魚鳥麗黃點黨齊龍龜"
)
# 日文假名标点与助词（纯汉字句中偶尔保留的日语标记）
_JA_MARKERS = ("の", "・", "､")


def _has_japanese_kanji(text: str) -> bool:
    # 假名标点（如「の」其实是假名，会被 script_profile 计入 kana，
    # 这里作为兜底处理可能的漏网情况）
    if any(m in text for m in _JA_MARKERS):
        return True
    hits = sum(1 for c in text if c in _JA_SHINJITAI)
    # 单字命中不足以判定（可能是个别巧合），要求 ≥2 或占比可观
    han_count = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    if hits >= 2:
        return True
    return han_count > 0 and hits / han_count >= 0.15


# 拉丁语系子语种：用高频功能词（stopword）判别。
# 覆盖 de/fr/es/pt/it/nl 等主要语言；无法判定时返回 "latin"（诚实降级）。
_LATIN_STOPWORDS = {
    "de": ("der", "die", "und", "ist", "nicht", "ein", "eine", "mit", "für", "auf"),
    "fr": ("le", "la", "les", "est", "une", "pour", "avec", "dans", "sur", "des"),
    "es": ("el", "la", "los", "es", "una", "para", "con", "por", "que", "del"),
    "pt": ("o", "a", "os", "as", "é", "uma", "para", "com", "por", "do"),
    "it": ("il", "la", "è", "una", "per", "con", "che", "del", "sono", "non"),
    "nl": ("de", "het", "een", "van", "is", "niet", "met", "voor", "op", "dat"),
    "en": ("the", "and", "is", "of", "to", "in", "for", "with", "that", "on"),
}


def _latin_subtype(text: str) -> str:
    """拉丁字母文本 → 子语种（best-effort，判不出返回 latin）。"""
    words = set(re.findall(r"[a-zà-ÿ]+", text.lower()))
    if not words:
        return "latin"
    scores = {lang: sum(1 for w in sw if w in words)
              for lang, sw in _LATIN_STOPWORDS.items()}
    best = max(scores, key=lambda k: scores[k])
    # 需要至少 2 个功能词命中才敢下结论，否则诚实返回语系级
    return best if scores[best] >= 2 else "latin"


# ── 相关度与噪声判定 ─────────────────────────────────────────────────────

def relevance(query: str, item: dict[str, Any]) -> float:
    """查询词元在结果中的命中率（0~1）。

    廉价、可解释、无需模型。实测判别力充分：
    真结果得 0.5~1.0，热帖噪声得 0.00。
    """
    blob = ((item.get("title") or "") + " " + (item.get("snippet") or "")).lower()
    if not blob.strip():
        return 0.0
    toks = [t for t in re.findall(r"[a-z]{3,}", query.lower())]
    if toks:
        return sum(1 for t in toks if t in blob) / len(toks)
    cjk = re.findall(r"[\u4e00-\u9fff\u3040-\u30ff]{2,}", query)
    if cjk:
        return sum(1 for c in cjk if c in blob) / len(cjk)
    han = re.findall(r"[\uac00-\ud7af]{2,}", query)
    if han:
        return sum(1 for c in han if c in blob) / len(han)
    words = [w for w in re.split(r"\s+", query) if len(w) >= 3]
    if words:
        return sum(1 for w in words if w in blob) / len(words)
    return 0.0


def assess_results(query: str, results: list[dict[str, Any]], *,
                   expected_lang: str | None = None,
                   sample: int = _SAMPLE_ITEMS) -> dict[str, Any]:
    """评估一组结果：主语言、语言匹配、相关度、噪声判定。

    返回 {
      lang, lang_profile, lang_match, relevance, verdict, sampled, reasons
    }
    verdict: ok | low | noise | empty
    """
    if not results:
        return {"lang": None, "lang_profile": {}, "lang_match": None,
                "relevance": 0.0, "verdict": "empty", "sampled": 0,
                "reasons": ["no results"]}

    head = results[:sample]
    blob = " ".join(
        ((r.get("title") or "") + " " + (r.get("snippet") or ""))
        for r in head
    )
    lang = detect_result_lang(blob)
    prof = script_profile(blob)

    rels = [relevance(query, r) for r in head]
    avg_rel = round(sum(rels) / len(rels), 3) if rels else 0.0

    reasons: list[str] = []
    lang_match: bool | None = None
    if expected_lang:
        # 语系级标签与语种级标签的兼容判断：期望 latin 时，结果判成 de/fr
        # 也算匹配（语系相同）；期望具体语种时要求精确一致或语系一致
        lang_match = _lang_compatible(lang, expected_lang)
        if not lang_match:
            reasons.append(f"结果语言 {lang} != 期望 {expected_lang}")

    if avg_rel >= OK_RELEVANCE:
        verdict = "ok"
    elif avg_rel < NOISE_RELEVANCE:
        # 低相关度本身就是「引擎没在回答这个查询」的证据，与语言是否匹配无关。
        # 实测两类同构案例：
        #   · juejin 查阿语 → 返回中文通用热帖（语言不符 + rel 0.00）
        #   · qiita 查「東京 観光 おすすめ」→ 返回「生成AI技术」（语言相符 ja
        #     但 rel 0.067）——引擎返回的是热榜而非检索结果
        # 只看语言会漏掉第二类，故以相关度为主判据。
        verdict = "noise"
        if lang_match is False:
            reasons.append("语言不符且相关度低 → 噪声")
        else:
            reasons.append("语言相符但相关度极低 → 引擎未按查询检索（静默降级）")
    else:
        # 0.2 ~ 0.5：部分相关，可能是分词口径导致命中率偏低，不武断判噪声
        verdict = "low"
        reasons.append("相关度偏低")

    return {
        "lang": lang,
        "lang_profile": prof,
        "lang_match": lang_match,
        "relevance": avg_rel,
        "verdict": verdict,
        "sampled": len(head),
        "reasons": reasons,
    }


# 语系成员表：用于「语系级兼容」判断。
#
# **刻意不含 cjk**（2026-09-10 修正）：汉字在中日文之间共享，但 zh 与 ja 是
# 不同语言——把二者视为「兼容」会让中文结果通过日语查询的语言校验。
# 实测影响：查询「人工知能」（判为 ja）返回 10 条中文时，`_lang_compatible`
# 曾返回 True，于是噪声理由被写成「语言相符」，且相关度处于 0.2~0.5 的中文
# 结果会被判 `low` 而非 `noise`，从而漏过噪声门——这正是「日语查询返回中文
# 结果」的成因之一。
#
# 保留 latin / cyrillic 是因为**结果侧检测器对这两类只能给到语系级**
# （纯西里尔文本分不出 ru/uk/bg），此时把 "cyrillic" 与期望 "ru" 判为兼容
# 是必要的；而中日文检测器能给到具体语种，就不该再合并。
_SCRIPT_FAMILY = {
    "latin": {"en", "de", "fr", "es", "pt", "it", "nl", "pl", "tr", "vi", "latin"},
    "cyrillic": {"ru", "uk", "bg", "cyrillic"},
}


def _family_of(lang: str) -> str | None:
    for fam, members in _SCRIPT_FAMILY.items():
        if lang in members:
            return fam
    return None


def _lang_compatible(actual: str, expected: str) -> bool:
    """语言兼容判断：精确一致，或同属一个书写系统族。

    为什么允许语系级兼容：结果侧判定有时只能到语系级（纯西里尔文本
    分不出俄/乌），此时判成 "cyrillic" 而期望是 "ru" 仍应视为匹配。
    """
    if not actual or not expected:
        return False
    if actual == expected:
        return True
    fa, fe = _family_of(actual), _family_of(expected)
    return fa is not None and fa == fe
