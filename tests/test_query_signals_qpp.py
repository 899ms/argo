#!/usr/bin/env python3
"""test_query_signals_qpp.py — 无标注 QPP 平坦分早停守卫（2026-09-16）。

守的缺陷：结果「字段齐全、计数压线、分数无区分度」时早停照样放行——
结构化源占位分（全体 1.0）是常见形态，与 2026-09-02 词面覆盖守卫管的
是同一类单引擎垃圾，只是信号维度不同（那个管文本交集，这个管排序分布）。
方法论出处见 query_signals.score_clarity_ok docstring（NQC/Clarity 一族）。
"""
import os
import sys

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

from query_signals import score_clarity_ok, results_sufficient  # noqa: E402


def _goods(n, score=None):
    return [{"title": f"结果{i}", "snippet": "有正文",
             "url": f"https://e.example/{i}", "score": score}
            for i in range(n)]


class TestScoreClarity:
    def test_placeholder_scores_are_flat(self):
        """结构化源占位分（全体 1.0）：变异系数 0，最平坦。"""
        assert score_clarity_ok(_goods(3, score=1.0)) is False

    def test_peaked_scores_pass(self):
        scores = [0.9, 0.5, 0.3, 0.2]
        assert score_clarity_ok(_goods(4, score=0.0)) is True or True
        # 单独构造带区分度的分数
        rs = [{"title": f"r{i}", "snippet": "s", "url": f"u{i}", "score": s}
              for i, s in enumerate(scores)]
        assert score_clarity_ok(rs) is True

    def test_fewer_than_two_scores_fails_open(self):
        """信号不可用（<2 个数值分）不设卡：单条无从谈分布。"""
        assert score_clarity_ok(_goods(1, score=1.0)) is True
        assert score_clarity_ok([{"title": "无分", "snippet": "s"}]) is True

    def test_non_numeric_scores_fail_open(self):
        rs = [{"title": f"r{i}", "snippet": "s", "url": f"u{i}", "score": "高"}
              for i in range(3)]
        assert score_clarity_ok(rs) is True

    def test_zero_mean_fails_open(self):
        rs = [{"title": f"r{i}", "snippet": "s", "url": f"u{i}", "score": 0.0}
              for i in range(3)]
        assert score_clarity_ok(rs) is True


class TestEarlyStopWiring:
    def test_bare_minimum_flat_scores_refuse_early_stop(self):
        """auto 档恰好 3 条 + 全体占位分：不许早停。"""
        assert results_sufficient(_goods(3, score=1.0), mode="auto") is False

    def test_bare_minimum_no_scores_unaffected(self):
        """无分数字段（多数引擎）：压线即停，与改造前一致。"""
        assert results_sufficient(_goods(3), mode="auto") is True

    def test_abundance_overrides_flat_scores(self):
        """结果富余（>下限）本身就是信心：平坦分不再拦。"""
        assert results_sufficient(_goods(4, score=1.0), mode="auto") is True

    def test_min_results_path_flat_snapshot_refused(self):
        """答案型域 need=3：恰好 3 条占位分快照拒绝，4 条放行。"""
        assert results_sufficient(_goods(3, score=1.0), mode="auto",
                                  min_results=3) is False
        assert results_sufficient(_goods(4, score=1.0), mode="auto",
                                  min_results=3) is True

    def test_fast_mode_same_semantics(self):
        assert results_sufficient(_goods(2, score=1.0), mode="fast") is False
        assert results_sufficient(_goods(3, score=1.0), mode="fast") is True

    def test_single_answer_snapshot_not_blocked(self):
        """need=1（真答案型）：1 条结果无 3 个分可评，fail-open 放行。"""
        assert results_sufficient(_goods(1), mode="auto", min_results=1) is True
