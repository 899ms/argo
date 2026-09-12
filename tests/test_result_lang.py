#!/usr/bin/env python3
"""test_result_lang.py — 结果侧语言判定与噪声识别回归测试。

背景：argo 原有机制在**查询侧**猜语言，实测三类失误：
  「人工知能 最新 動向」(日) → zh；「künstliche」(德) → latin；「inteligencia」→ en
三种补法全失败（扩词表漏 / 手列字表 39% / 变音符只覆盖部分拉丁语）。

第一性转向：语言识别的目的是「选检索目标 + 筛噪声」，而「结果用了什么
文字」是确定性事实（Unicode 码位），不需要推断。故判定位置后移到结果侧。

本文件锁定四条契约：
  1. 确定性判据优先（kana→ja、hangul→ko 恒成立）
  2. 拉丁语系子语种靠功能词判别，判不出诚实返回 latin
  3. 噪声判定：语言不符 + 相关度低 → noise
  4. 语系级兼容（结果判成 cyrillic、期望 ru 仍算匹配）
"""

import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import result_lang as rl  # noqa: E402


class TestDeterministicScripts:
    """确定性判据：假名/谚文/西里尔等的判定必须 100% 可靠。"""

    def test_kana_means_japanese(self):
        """含假名必是日语——Unicode 编码事实，无例外。"""
        for t in ("人工知能の最新動向", "これはテスト", "東京タワー"):
            assert rl.detect_result_lang(t) == "ja", t

    def test_hangul_means_korean(self):
        for t in ("인공지능 최신 동향", "서울 여행"):
            assert rl.detect_result_lang(t) == "ko", t

    def test_cyrillic(self):
        assert rl.detect_result_lang("искусственный интеллект") == "ru"

    def test_arabic(self):
        assert rl.detect_result_lang("الذكاء الاصطناعي") == "ar"

    def test_hebrew(self):
        assert rl.detect_result_lang("בינה מלאכותית") == "he"

    def test_thai(self):
        assert rl.detect_result_lang("ปัญญาประดิษฐ์") == "th"

    def test_greek(self):
        assert rl.detect_result_lang("τεχνητή νοημοσύνη") == "el"

    def test_han_only_leans_chinese(self):
        assert rl.detect_result_lang("人工智能最新进展") == "zh"


class TestLatinSubtype:
    """拉丁语系子语种：靠功能词判别，判不出诚实降级。"""

    def test_german(self):
        assert rl.detect_result_lang(
            "künstliche Intelligenz ist ein Teil der Informatik und nicht neu") == "de"

    def test_french(self):
        assert rl.detect_result_lang(
            "intelligence artificielle est une discipline pour les machines") == "fr"

    def test_spanish(self):
        assert rl.detect_result_lang(
            "inteligencia artificial es una disciplina para los sistemas") == "es"

    def test_english(self):
        assert rl.detect_result_lang(
            "artificial intelligence is the field of the study and that is on") == "en"

    def test_ambiguous_returns_latin(self):
        """功能词命中不足时诚实返回语系级，不硬猜语种。"""
        assert rl.detect_result_lang("AlphaBeta GammaDelta") == "latin"

    def test_empty_and_junk(self):
        assert rl.detect_result_lang("") == "other"
        assert rl.detect_result_lang("12345 !!!") == "other"


class TestScriptProfile:
    """书写系统构成比例（确定性）。"""

    def test_japanese_mix(self):
        p = rl.script_profile("人工知能の最新動向について")
        assert p.get("han", 0) > 0.5 and p.get("kana", 0) > 0.2

    def test_chinese_pure_han(self):
        p = rl.script_profile("人工智能最新进展")
        assert p.get("han", 0) == 1.0 and "kana" not in p

    def test_empty(self):
        assert rl.script_profile("") == {}


class TestRelevance:
    """相关度：查询词元命中率。"""

    def test_high_when_title_matches(self):
        item = {"title": "artificial intelligence progress", "snippet": ""}
        assert rl.relevance("artificial intelligence progress", item) > 0.9

    def test_zero_when_unrelated(self):
        item = {"title": "Canvas 渲染教程", "snippet": "HTML-in-Canvas"}
        assert rl.relevance("الذكاء الاصطناعي", item) == 0.0

    def test_cjk_query(self):
        item = {"title": "人工智能最新进展报告", "snippet": ""}
        assert rl.relevance("人工智能 最新进展", item) > 0.9


class TestNoiseVerdict:
    """噪声判定：语言不符 + 相关度低 → noise。"""

    def test_noise_detected(self):
        """复现实测：juejin 在非中文下返回通用热帖。"""
        results = [
            {"title": "Agent开发(二) — 谈谈RAG", "snippet": ""},
            {"title": "HTML-in-Canvas：让 Canvas 渲染 HTML", "snippet": ""},
        ]
        a = rl.assess_results("الذكاء الاصطناعي", results, expected_lang="ar")
        assert a["verdict"] == "noise"
        assert a["lang_match"] is False
        assert a["relevance"] < 0.2

    def test_ok_when_relevant(self):
        results = [{"title": "künstliche Intelligenz Grundlagen", "snippet": "ist ein Teil"}]
        a = rl.assess_results("künstliche Intelligenz", results, expected_lang="de")
        assert a["verdict"] == "ok" and a["lang_match"] is True

    def test_empty(self):
        a = rl.assess_results("test", [])
        assert a["verdict"] == "empty" and a["lang"] is None

    def test_low_when_weak_relevance(self):
        results = [{"title": "something完全不搭边", "snippet": ""}]
        a = rl.assess_results("quantum computing", results, expected_lang="en")
        assert a["verdict"] in ("low", "noise")


class TestLangCompatibility:
    """语系级兼容：结果判成语系标签、期望是具体语种时仍算匹配。"""

    def test_same_script_family(self):
        assert rl._lang_compatible("cyrillic", "ru") is True
        assert rl._lang_compatible("latin", "de") is True
        assert rl._lang_compatible("ru", "cyrillic") is True

    def test_exact_match(self):
        assert rl._lang_compatible("ja", "ja") is True

    def test_different_family(self):
        assert rl._lang_compatible("ja", "ru") is False
        assert rl._lang_compatible("en", "zh") is False

    def test_zh_ja_not_compatible(self):
        """zh 与 ja 共享汉字但属不同语言，**不得**判为兼容。

        实测 bug（2026-09-10 发现）：`_SCRIPT_FAMILY` 里曾有 `cjk: {zh, ja}`，
        导致中文结果通过日语查询的语言校验——查询「人工知能」(ja) 返回
        10 条中文时 lang_match=True，噪声理由被写成「语言相符」，
        且相关度 0.2~0.5 的中文结果会被判 low 而非 noise，漏过噪声门。
        这正是「日语查询返回中文结果」的成因之一。
        """
        assert rl._lang_compatible("zh", "ja") is False
        assert rl._lang_compatible("ja", "zh") is False
        assert "cjk" not in rl._SCRIPT_FAMILY

    def test_ja_query_rejects_chinese_results(self):
        """端到端语义：日语查询 + 中文结果 → 判噪声且理由准确。"""
        items = [{"title": "AI赋能工业发展", "snippet": "中文内容"}] * 10
        a = rl.assess_results("人工知能", items, expected_lang="ja")
        assert a["verdict"] == "noise"
        assert a["lang_match"] is False
        assert any("!=" in r for r in a["reasons"])

    def test_empty_inputs(self):
        assert rl._lang_compatible("", "ja") is False
        assert rl._lang_compatible("ja", "") is False
