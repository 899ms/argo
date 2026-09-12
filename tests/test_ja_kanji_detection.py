#!/usr/bin/env python3
"""test_ja_kanji_detection.py — 纯汉字日语查询识别（修复「日语误判为中文」）。

## 背景

实测：`人工知能 最新 動向`（无假名的日语查询）被判成 `zh`，于是路由进
`chinese_general` 域、返回**全中文结果**：

    route_query("人工知能 最新 動向")
      -> domain=chinese_general, engines=[bocha, anysearch]   # 全中文源

而带假名的 `人工知能の最新動向` 判对（ja）。说明缺口在「无假名日文」。

## 修法与判据

加「日文独有字形」判据：这类字形在 ja / 简体 / 繁体 **三态互异**
（如 観(ja)/观(简)/觀(繁)、経(ja)/经(简)/經(繁)、鉄(ja)/铁(简)/鐵(繁)），
出现即可确定是日文，不会误伤任何中文变体。

**为什么不用「動/報/學」这类字形**：它们是日文新字体，但**同时是繁体
中文用字**，拿它们判日文会把繁体中文误判成日文。故只收三态互异的严格子集。

## 已知且**有界**的限制（本文件显式记录，不隐藏）

纯粹由中日共用汉字组成的短查询在字符层面无从区分。典型例：
「人工知能 最新 動向」的「動」繁体亦用，不能作判据——该例靠**词汇表**
（「人工知能」已补入 _JA_KANJI_ONLY_SIGNALS）命中解决。
若某查询既无独有字形、又不在词汇表，会落到 zh。**不做硬猜**。
"""

import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from lang_detect import (  # noqa: E402
    _JA_SHINJITAI_ONLY,
    _has_ja_only_kanji,
    detect_language,
)


class TestJaOnlyKanjiSet:
    """字形集本身的性质：必须是三态互异，不得混入中文用字。"""

    def test_set_not_empty(self):
        assert len(_JA_SHINJITAI_ONLY) >= 80

    @pytest.mark.parametrize("ch", ["観", "経", "鉄", "図", "駅", "産", "応", "実", "発"])
    def test_known_ja_only_chars_present(self, ch):
        assert ch in _JA_SHINJITAI_ONLY

    @pytest.mark.parametrize("ch", ["动", "报", "学", "国", "经", "济", "观"])
    def test_simplified_forms_excluded(self, ch):
        """简体字形绝不在集合里（否则简体中文查询会被判日文）。"""
        assert ch not in _JA_SHINJITAI_ONLY

    @pytest.mark.parametrize("ch", ["動", "報", "學", "國", "經", "觀"])
    def test_traditional_forms_excluded(self, ch):
        """繁体用字也必须排除——这类字形日文与繁体共用，不能作日文判据。"""
        assert ch not in _JA_SHINJITAI_ONLY


class TestHasJaOnlyKanji:
    def test_detects_ja_only(self):
        assert _has_ja_only_kanji("東京 観光") is True
        assert _has_ja_only_kanji("経済 ニュース") is True
        assert _has_ja_only_kanji("鉄道 運賃") is True

    def test_chinese_text_not_matched(self):
        assert _has_ja_only_kanji("北京 旅游") is False
        assert _has_ja_only_kanji("经济 新闻") is False
        assert _has_ja_only_kanji("繁體中文測試") is False


class TestDetectLanguageJapanese:
    """核心回归：无假名日语查询不得被判成中文。"""

    @pytest.mark.parametrize("q", [
        "人工知能 最新 動向",   # 实测曾误判为 zh（靠词汇表修复）
        "人工知能の最新動向",     # 带假名，本来就对
        "東京 観光",
        "経済 ニュース",
        "鉄道 運賃",
        "観光 地図",
        "企業 経営",
        "大学院 受験",
    ])
    def test_pure_kanji_japanese_detected(self, q):
        assert detect_language(q) == "ja", f"{q!r} 应判日文"

    @pytest.mark.parametrize("q", [
        "北京 旅游", "经济 新闻", "上海 天气", "视频直播",
        "铁路运费", "繁體中文測試", "人工智能 最新进展",
    ])
    def test_chinese_still_chinese(self, q):
        """不得因新增日文判据而把中文查询误判成日文。"""
        assert detect_language(q) == "zh", f"{q!r} 应判中文"

    def test_kana_still_works(self):
        assert detect_language("アニメ おすすめ") == "ja"

    def test_korean_unaffected(self):
        assert detect_language("한국 영화 추천") == "ko"
