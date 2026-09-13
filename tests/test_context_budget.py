#!/usr/bin/env python3
"""test_context_budget — 输出与文档的上下文预算门禁（离线、确定性）。

## 守的是什么

skill 的上下文开销不是「感觉大不大」，而是可以逐字节算出来的。实测（2026-09-13）：

  - 一次 `search --json` = 14.9 KB ≈ 5.0k token，其中**三个视图各说一遍**
    （candidates 4.9 KB + results 2.4 KB + sources 1.0 KB = 78%）；
  - `--no-envelope` 降到 6.8 KB ≈ 2.3k，叠 `-n 3` 降到 4.5 KB ≈ 1.5k；
  - `--list-engines --detail` 全量 = **186 KB ≈ 62k token**（216 条 × ~0.9 KB）。

这些数字本身不会自己变坏，**是字段一个两个加上去后悄悄变坏的**。所以本文件不看
「当前多大」，而是冻结三类会增长的形状：

  1. 顶层 `sources` 投影的字段集（稳定 5 字段，别往链接列表里塞东西）；
  2. `candidates` 单条的字段集（最大的一块，加字段必须显式登记）；
  3. 合成载荷的序列化字节预算（真正挡住「慢慢变大」）。

另锁两侧一致性：SKILL.md 教了 `--no-envelope`，CLI 就必须真的有这个开关；
`--list-engines --detail` 的 `--engine` 过滤必须一直有效（曾静默失效，
单引擎查询被迫付 186 KB）。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# 冻结集：改动这些集合 = 改动 agent 每次调用的上下文成本，必须有意识
SOURCES_FIELDS = {"ref", "title", "url", "engine", "score", "snippet"}
CANDIDATE_FIELDS = {
    "candidate_id", "query", "platform", "backend", "rank", "title", "url",
    "canonical_url", "snippet", "author", "published_at", "language",
    "content_type", "access", "metrics", "provenance", "verification",
    "limitations",
}

# 合成样本：5 条典型结果，序列化后不得越线（字节）
_FIVE_RESULTS = [
    {"title": f"示例结果 {i}", "url": f"https://example.org/a{i}",
     "snippet": "摘要" * 40, "source": "demo", "score": 0.8 - i * 0.01,
     "_engine": "demo", "_rrf_score": 0.02, "consensus_engines": ["demo"],
     "fetch_suggested": True}
    for i in range(5)
]

# 预算阈值：留 ~30% 余量，超了说明输出层在悄悄变胖
BUDGET_SOURCES_BYTES = 1200
BUDGET_CANDIDATES_BYTES = 6500
BUDGET_SKILL_MD_BYTES = 7600

# 子技能文档预算（触发即注入的部分）。
# ego-search 实测 18.8 KB ≈ 6.2k token。2026-09-13 两轮治理：
#   ① 维护者向/低频内容搬到 references/（heredoc 全 helper 速查）；
#   ② 自我描述内容**直接删除**（架构图与隔离表重复、能力对照是自我评价、
#      文件结构是复述文件系统且已漂移）——13.1 KB。
# local-search 的同轮治理：设计原则只留操作性判据、删文件结构树（保留两条
# 不显然的约定），6.3 KB → 5.2 KB。
BUDGET_SUBSKILL_MD_BYTES = {
    "sub-skills/ego-search/SKILL.md": 13500,
    "sub-skills/local-search/SKILL.md": 5600,
    "sub-skills/local-seek/SKILL.md": 7000,
}


def _size(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False))


class TestViewFieldSetsFrozen(unittest.TestCase):
    def test_sources_projection_fields(self):
        from search import build_sources
        got = set(build_sources(_FIVE_RESULTS)[0])
        self.assertEqual(
            got, SOURCES_FIELDS,
            f"sources 投影字段变了（{sorted(got ^ SOURCES_FIELDS)} 差异）——"
            "它是「底部相关链接」形态的稳定投影，加字段会抬高每次调用的上下文成本；"
            "确实需要就同步更新本测试与 references/usage.md 的体积表")

    def test_candidate_fields(self):
        from candidate_envelope import result_to_candidate
        got = set(result_to_candidate(_FIVE_RESULTS[0], "查询", 1))
        self.assertEqual(
            got, CANDIDATE_FIELDS,
            f"candidate 字段变了（{sorted(got ^ CANDIDATE_FIELDS)} 差异）——"
            "candidates 是输出里最大的一块（归档视图），加字段必须显式登记")


class TestSerializedSizeBudget(unittest.TestCase):
    def test_sources_within_budget(self):
        from search import build_sources
        n = _size(build_sources(_FIVE_RESULTS))
        self.assertLessEqual(n, BUDGET_SOURCES_BYTES,
                             f"5 条结果的 sources 视图 {n} B，超预算 {BUDGET_SOURCES_BYTES} B")

    def test_candidates_within_budget(self):
        from candidate_envelope import result_to_candidate
        payload = [result_to_candidate(r, "查询", i + 1)
                   for i, r in enumerate(_FIVE_RESULTS)]
        n = _size(payload)
        self.assertLessEqual(n, BUDGET_CANDIDATES_BYTES,
                             f"5 条结果的 candidates 视图 {n} B，超预算 {BUDGET_CANDIDATES_BYTES} B")

    def test_skill_md_within_budget(self):
        """常驻文档预算：SKILL.md 是技能触发即注入的部分，按字节守。"""
        n = (ROOT / "SKILL.md").stat().st_size
        self.assertLessEqual(n, BUDGET_SKILL_MD_BYTES,
                             f"SKILL.md {n} B 超预算 {BUDGET_SKILL_MD_BYTES} B——"
                             "常驻内容请挪到 references/（按需读取）")

    def test_subskill_docs_within_budget(self):
        over = []
        for rel, budget in BUDGET_SUBSKILL_MD_BYTES.items():
            p = ROOT / rel
            self.assertTrue(p.exists(), f"{rel} 不存在——预算表过期了")
            n = p.stat().st_size
            if n > budget:
                over.append(f"{rel}: {n} B > {budget} B")
        self.assertFalse(over, (
            "子技能 SKILL.md 超预算——触发即注入的内容请挪到该子技能的 "
            "references/（按需读取），别让低频细节常驻：\n  " + "\n  ".join(over)))

    def test_moved_content_still_exists(self):
        """从 SKILL.md 搬出的内容必须在 references 里有承接（防「搬走=删掉」）。"""
        moved = {
            "sub-skills/ego-search/SKILL.md": [
                "references/browser-runtime.md",
            ],
        }
        for md_rel, refs in moved.items():
            for r in refs:
                p = (ROOT / md_rel).parent / r
                self.assertTrue(p.exists(), f"{md_rel} 指向的 {r} 不存在")
                self.assertGreater(p.stat().st_size, 500,
                                   f"{r} 体量异常小，搬移时可能丢了内容")


class TestNoSelfDescriptionInSkillDocs(unittest.TestCase):
    """技能文档只写「怎么用」，不写「我是什么/我有哪些文件」。

    第一性原理：技能文档的唯一职责是**改变调用方的行为**。描述自身结构的
    段落不改变任何行为，却有三重代价——
      ① 每次触发都占上下文；
      ② 它会漂移（实测 ego-search 的「文件结构」段列了 8 个文件，漏了 tests/
         与后来的两个 reference，读者据此判断会出错）；
      ③ 它鼓励「靠文档同步」而不是靠门禁，本仓已多次被这类漂移咬到。
    该判据可机械执行：文件树是**可以从文件系统读出**的信息，凡是把 `├──`
    `└──` 这类树形画进 SKILL.md 的，一律判定为自描述冗余。
    """

    TREE_MARKERS = ("├──", "└──", "│  ")
    SELF_DESC_HEADS = (
        "文件结构", "目录结构", "完全态架构", "实现说明",
        "与原版", "与其他技能的关系", "能力基础", "版本历史",
    )

    def _skill_docs(self):
        docs = [ROOT / "SKILL.md"]
        docs += sorted((ROOT / "sub-skills").glob("*/SKILL.md"))
        return [d for d in docs if d.exists()]

    def test_no_filesystem_tree(self):
        bad = []
        for d in self._skill_docs():
            text = d.read_text(encoding="utf-8")
            for i, line in enumerate(text.splitlines(), 1):
                if any(m in line for m in self.TREE_MARKERS):
                    bad.append(f"{d.relative_to(ROOT)}:{i} {line.strip()[:48]}")
        self.assertFalse(bad, (
            "SKILL.md 里画了文件树——文件系统自己能回答，写进文档只会漂移"
            "（并每次触发都占上下文）。删掉，或把**不显然的那两条**用一句话写出来：\n  "
            + "\n  ".join(bad)))

    def test_no_self_description_headings(self):
        bad = []
        for d in self._skill_docs():
            for i, line in enumerate(d.read_text(encoding="utf-8").splitlines(), 1):
                if not line.startswith("#"):
                    continue
                head = line.lstrip("# ").strip()
                for kw in self.SELF_DESC_HEADS:
                    if kw in head:
                        bad.append(f"{d.relative_to(ROOT)}:{i} {head[:56]}")
        self.assertFalse(bad, (
            "SKILL.md 出现自描述型段落标题——技能文档只该写「怎么用」。"
            "若其中确实含操作性信息，请把它挪进对应的使用章节，其余删除：\n  "
            + "\n  ".join(bad)))

    def test_scan_surface_not_empty(self):
        """扫描面不能空——glob 失效时上面两条会静默全过。"""
        self.assertGreaterEqual(len(self._skill_docs()), 4)


class TestContextGuidanceIsReal(unittest.TestCase):
    """文档承诺的省上下文开关必须真实存在且未失效。"""

    def test_skill_md_documents_no_envelope(self):
        md = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("--no-envelope", md,
                      "SKILL.md 未教 Agent 用 --no-envelope——默认输出体积翻倍")

    def test_cli_really_supports_no_envelope(self):
        import inspect
        import search as search_mod
        src = inspect.getsource(search_mod)
        self.assertIn('"--no-envelope"', src, "CLI 不再支持 --no-envelope 但文档仍在教")

    def test_list_engines_detail_filter_by_engine(self):
        """单引擎详细查询必须真的被过滤——曾静默忽略 --engine 吐全量 186 KB。"""
        from engine_status import list_engines_detail
        one = list_engines_detail(engines=["egov_law"])
        self.assertEqual([r["engine_id"] for r in one], ["egov_law"],
                         "list_engines_detail 的 engines 过滤失效")
        many = list_engines_detail(engines=["egov_law", "kor_law"])
        self.assertEqual(sorted(r["engine_id"] for r in many), ["egov_law", "kor_law"])

    def test_filter_actually_shrinks_payload(self):
        """过滤后的体积必须小到 KB 级（防「过滤了但每行变胖」）。"""
        from engine_status import list_engines_detail
        one = _size(list_engines_detail(engines=["egov_law"]))
        self.assertLessEqual(one, 3000,
                             f"单引擎详细行 {one} B，超 3 KB——诊断字段在膨胀")

    def test_unknown_engine_yields_empty_not_full_list(self):
        """查了不存在的引擎要返回空（配合 CLI 的 stderr 提示），不能回退成全量。"""
        from engine_status import list_engines_detail
        self.assertEqual(list_engines_detail(engines=["__no_such_engine__"]), [])


if __name__ == "__main__":
    unittest.main()
