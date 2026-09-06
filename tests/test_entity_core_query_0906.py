#!/usr/bin/env python3
"""test_entity_core_query_0906 — 实体型引擎查询规范化 单测。

2026-09-06 live 金标教训（4 FAIL 中 3 个同根因）：自然语言句（疑问前缀+
属性词）直达实体搜索接口全部空结果——「NASA founding year」wikidata 0 条
（裸「NASA」7 条）、「where is Eiffel Tower」OSM+wikidata 双空且拖满 11s、
「Cristiano Ronaldo club」thesportsdb 抖动放大。分发层统一剥前缀与属性词。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from engines import _entity_core_query, _ENTITY_QUERY_ENGINES  # noqa: E402


class TestEntityCoreQuery(unittest.TestCase):
    def test_live_golden_cases(self):
        """live 四个失败查询 → 必须还原为可用实体名。"""
        self.assertEqual(_entity_core_query("NASA founding year"), "NASA")
        self.assertEqual(_entity_core_query("where is Eiffel Tower"), "Eiffel Tower")
        self.assertEqual(
            _entity_core_query("Cristiano Ronaldo club"), "Cristiano Ronaldo")
        # 中文同型
        self.assertEqual(_entity_core_query("埃菲尔铁塔 在哪"), "埃菲尔铁塔")
        self.assertEqual(_entity_core_query("国务院 职能"), "国务院")

    def test_question_prefix_variants(self):
        for q, want in [
            ("Where's the Louvre", "the Louvre"),
            ("who is Elon Musk", "Elon Musk"),
            ("What is CRISPR", "CRISPR"),
            ("when was NASA founded", "NASA"),
            ("清华大学 在哪里", "清华大学"),
        ]:
            self.assertEqual(_entity_core_query(q), want, q)

    def test_attribute_words_stripped(self):
        for q, want in [
            ("Apple headquarters", "Apple"),
            ("Real Madrid stadium address", "Real Madrid"),
            ("周杰伦 专辑", "周杰伦"),
            ("Michael Jordan team", "Michael Jordan"),
        ]:
            self.assertEqual(_entity_core_query(q), want, q)

    def test_plain_entity_untouched(self):
        """裸实体 / 单词查询必须原样（不做过度改写）。"""
        for q in ("NASA", "Eiffel Tower", "周杰伦", "Stephen Curry"):
            self.assertEqual(_entity_core_query(q), q)

    def test_stripped_to_empty_falls_back_to_raw(self):
        """全部被剥空（极端）→ 回退原查询，不返回空串。"""
        # 单个属性词被剥尽 → 回退原词
        self.assertEqual(_entity_core_query("founded"), "founded")
        self.assertEqual(_entity_core_query("who is who"), "who")

    def test_entity_engine_set(self):
        """实体型引擎集合：wikidata / thesportsdb / local_openstreetmap。"""
        self.assertEqual(
            _ENTITY_QUERY_ENGINES,
            {"wikidata", "thesportsdb", "local_openstreetmap"})


if __name__ == "__main__":
    unittest.main()
