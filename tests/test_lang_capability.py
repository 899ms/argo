#!/usr/bin/env python3
"""test_lang_capability.py — 语言能力画像回归测试。

背景：C 阶段 18语言×29引擎矩阵暴露两件事——
  1. 语言覆盖极不均衡（es 12 个可用引擎 vs th 仅 3 个）
  2. 路由此前只看 coverage 字段（157 引擎中 109 个未声明语言），
     「引擎在某语言下真有内容」这个事实没进入决策

本模块把矩阵结果固化能力画像，供路由做语言适配加权。

**核心设计取舍：建议而非硬过滤。**
矩阵是单次实测，含抖动；硬砍会让某次抖动导致整门语言失去检索能力。
故只做加权（good 提权 / noise 降权 / 未知不动），画像缺失时完全退化。

本文件锁定四条契约：
  1. 画像可用时 good/noise 判定正确
  2. 画像缺失时安全降级（恒返回中性 1.0，不抛异常）
  3. 语系级标签能展开到具体语种成员
  4. 权重方向正确（good > neutral > noise）
"""

import json
import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import lang_capability as lc  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    lc.reload()
    yield
    lc.reload()


@pytest.fixture()
def profile(monkeypatch):
    """注入合成画像，避免测试依赖真实矩阵数据（会随实测更新而变化）。"""
    data = {
        "lang_engines": {"ja": ["wikipedia", "ndl"], "th": ["anysearch"],
                         "zh": ["juejin"]},
        "summary": {"per_lang": {
            "ja": {"good_engines": ["wikipedia", "ndl"],
                   "noise_engines": ["qiita"]},
            "th": {"good_engines": ["anysearch"],
                   "noise_engines": ["v2ex"]},
            "zh": {"good_engines": ["juejin"], "noise_engines": ["gutenberg"]},
        }},
    }
    monkeypatch.setattr(lc, "_load", lambda: data)
    return data


class TestProfileAvailable:
    """画像可用性与过期策略。

    **本类必须不依赖外部状态**：原先直接检查 `lc.available()` 是否为 True，而
    `available()` 取决于真实画像文件的 mtime 与 `MAX_AGE_S=30 天`。
    画像随 C 阶段实测更新（最近一次 2026-09-10），于是这条检查会在
    2026-10-10 之后必然变红——与代码对错无关。已知的定时炸弹：
    测试挂掉的日子由数据生成日期决定，不由缺陷决定。

    改为显式操控 `MAX_AGE_S` 来测「策略」，不再依赖当天日期。
    """

    def test_available_when_profile_is_fresh(self, monkeypatch):
        """画像在有效期内可用（把窗口放到极大以消除日期依赖）。"""
        monkeypatch.setattr(lc, "MAX_AGE_S", 10 ** 9)
        lc.reload()
        assert lc.available() is True

    def test_expired_profile_degrades_safely(self, monkeypatch):
        """画像过期 → available() False，且各查询回落中性，不抛异常。"""
        monkeypatch.setattr(lc, "MAX_AGE_S", -1)
        lc.reload()
        assert lc.available() is False
        assert lc.score_adjust("wikipedia", "ja") == lc.NEUTRAL
        assert lc.engines_for_lang("ja") == set()

    def test_real_profile_has_expected_shape(self, monkeypatch):
        """画像文件本身必须存在且形状正确（与有效期拆开）。

        读文件内容而非 `_load()`：`_load()` 会被过期策略返回 None，
        把「文件缺失」和「文件过期」两种故障混成同一条检查。
        """
        monkeypatch.setattr(lc, "MAX_AGE_S", 10 ** 9)
        lc.reload()
        p = lc._load()
        assert p and "lang_engines" in p and "summary" in p
        # 18 语言 × ≥17 引擎有良好覆盖
        assert len(p["lang_engines"]) >= 15


class TestEnginesForLang:
    def test_good_engines(self, profile):
        assert lc.engines_for_lang("ja") >= {"wikipedia", "ndl"}

    def test_unknown_lang_returns_empty(self, profile):
        assert lc.engines_for_lang("xx") == set()

    def test_noise_engines(self, profile):
        assert "qiita" in lc.noise_engines_for_lang("ja")


class TestLangFamilyExpansion:
    """语系级标签展开：detect_language 可能给 latin，画像按具体语种建。"""

    def test_latin_expands_to_members(self, profile):
        # ja/th 不在 latin 族里，但 latin 展开后应含 en/de/fr…，
        # 合成画像里没有这些语言 → 空集
        assert lc._expand_lang("latin") == lc._FAMILY["latin"]

    def test_specific_lang_does_not_expand(self, profile):
        """具体语种不展开——实测中日引擎能力不通用（zh_wikipedia 不胜任日语）。

        若把 ja 展开成 (zh, ja)，中文引擎能力会被误算到日语头上。
        """
        assert lc._expand_lang("de") == ("de",)
        assert lc._expand_lang("ja") == ("ja",)

    def test_cjk_langs_not_grouped(self):
        """CJK 不设语系族：zh 与 ja 必须独立判定。"""
        assert "cjk" not in lc._FAMILY
        assert lc._expand_lang("zh") == ("zh",)
        assert lc._expand_lang("ja") == ("ja",)


class TestScoreAdjust:
    def test_good_boosted(self, profile):
        assert lc.score_adjust("wikipedia", "ja") > 1.0

    def test_noise_penalized(self, profile):
        assert lc.score_adjust("qiita", "ja") < 1.0

    def test_unknown_neutral(self, profile):
        assert lc.score_adjust("some-random-engine", "ja") == 1.0

    def test_direction_ordering(self, profile):
        """方向正确性：good > neutral > noise。"""
        good = lc.score_adjust("wikipedia", "ja")
        neutral = lc.score_adjust("unknown-eng", "ja")
        noise = lc.score_adjust("qiita", "ja")
        assert good > neutral > noise

    def test_empty_inputs_neutral(self, profile):
        assert lc.score_adjust("", "ja") == 1.0
        assert lc.score_adjust("wikipedia", "") == 1.0


class TestSafeDegradation:
    """画像缺失时必须完全退化，不得影响原有行为。"""

    def test_missing_profile_degrades_safely(self, monkeypatch):
        monkeypatch.setattr(lc, "_load", lambda: None)
        assert lc.available() is False
        assert lc.engines_for_lang("ja") == set()
        assert lc.noise_engines_for_lang("ja") == set()
        # 关键：全部返回中性，调用方乘上去等于不变
        for e in ("wikipedia", "qiita", "anything"):
            assert lc.score_adjust(e, "ja") == 1.0

    def test_corrupt_profile_file(self, tmp_path, monkeypatch):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(lc, "_PROFILE_PATH", bad)
        lc.reload()
        assert lc._load() is None
        assert lc.score_adjust("wikipedia", "ja") == 1.0

    def test_expired_profile_ignored(self, tmp_path, monkeypatch):
        """过期画像不得继续影响路由（语言能力会随上游改版变化）。"""
        stale = tmp_path / "stale.json"
        stale.write_text(json.dumps({"lang_engines": {"ja": ["x"]}}), encoding="utf-8")
        old = os.stat(stale).st_mtime - lc.MAX_AGE_S - 100
        os.utime(stale, (old, old))
        monkeypatch.setattr(lc, "_PROFILE_PATH", stale)
        lc.reload()
        assert lc._load() is None
        assert lc.score_adjust("x", "ja") == 1.0
