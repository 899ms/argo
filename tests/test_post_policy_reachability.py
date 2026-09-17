#!/usr/bin/env python3
"""test_post_policy_reachability.py — 「策略施加后」的部署形态门禁(2026-09-17 新增)。

## 守的是什么缺陷

GLM 推理基建文章的原则:「只在未切分条件下测 kernel,不足以覆盖真实部署行为」。
argo 的对应物是:路由为每个域声明 `engines_combo`,但真正执行的**不是**这份声明,
而是策略链改写后的集合——boost 垂直源 → tier 过滤(research_only)→ 预算截断 →
must_keep 尾位换位,再经准入(admission)与熔断(breaker)过滤。

已有多层检查,但各管一段、没有一层把「策略施加后」的形态当验收对象:

  - `test_combo_budget_coverage.py` 锁**声明顺序**(fast 档前 2 位),
    不调用策略函数、不扫其他模式;
  - `test_multilingual.py` 锁 ja/ko 的语言注入(单点单测);
  - admission / breaker 只有单元级测试,没有「全域 combo 冷状态可达」的形态级检查。

历史两笔最贵的故障都发生在路由决策**之后**:anysearch 升权被 must_keep 换位
挤出(2026-09-07,注释见 route._inject_multilingual_backup)、zhihu_global
饿死 37 天。本文件补三块:

  1. `TestPolicyPreservesBudget`     全域 × 全模式:策略链不裁掉预算内成员、
     不引入幽灵引擎、不产出空集合——策略实现的回归直接红;
  2. `TestColdEnvironmentReachable`  全新状态目录(无准入记录、无熔断历史)下,
     策略后 combo 全员放行——新装环境一上来就被拦是不成立的部署形态;
  3. `TestRoutingRegressionAnchors`  路由回归锚点:一词多义把英文技术查询劫持进
     专利域(2026-09-17 实录:「OpenAI MCP specification」→ patent_search,
     根因是专利域正则把 specification 当专利术语;修复前这条是红的)。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# ── 数据:唯一真源是 config.yaml,不复制 ──────────────────────────────────────

def _domains() -> list[dict]:
    from config import load_config, get_domains
    return [d for d in get_domains(load_config())
            if isinstance(d, dict) and (d.get("engines_combo") or [])]


@pytest.fixture(scope="session")
def domains() -> list[dict]:
    return _domains()


# 与 _apply_engine_policy 的调用方保持同一组取值:mode 来自 --mode,
# depth 来自 --depth,context 由 search/research 入口决定。
_MODES = ("fast", "auto", "deep", "budget")
_DEPTHS = ("fast", "balanced", "deep")
_CONTEXTS = ("search", "research")


# ── 预算契约:独立于被测代码声明 ─────────────────────────────────────────────
#
# 数值来源:combo_budget docstring 与 references/usage.md 承诺的用户可见语义
# (fast/budget 模式或 fast 深度 = 2,其余 = 3;research 上下文 = 不截断)。
# 这里**有意不调用** combo_budget:期望从被测函数推导时,变异实现期望跟着变,
# 门禁形同虚设。实现改预算,先改这张表——契约变更必须显式过门。

def _contract_budget(mode: str, depth: str, context: str) -> int | None:
    if context == "research" or depth == "deep" or mode == "deep":
        return None
    if mode in ("fast", "budget") or depth == "fast":
        return 2
    return 3


# ── 1. 策略层:全域 × 全模式 ─────────────────────────────────────────────────

class TestPolicyPreservesBudget:
    """策略链是纯函数:给定同一份声明,施加后该在的必须在、不该出现的不能出现。

    基准形态 = 不带 boost / must_keep(两者的变换已有专门测试:
    must_keep 见 test_multilingual,boost 见 research 相关测试)。这里锁的是
    **所有**域在**所有**模式组合下都成立的形态契约。
    """

    def _expected_keep(self, combo: list[str], *, mode: str, depth: str,
                       context: str, tier_of) -> set[str]:
        """按**独立契约**推出「应当存活」的集合,不从被测代码推导。

        期望若从 combo_budget / is_research_context 直接推导,变异实现时期望
        会跟着变、门禁永远绿(变异测试实证:`else 3`→`else 2` 后仍通过)。
        所以预算数值在这里独立声明——它是用户可见的契约,实现改预算必须
        先改这张表。research_only 引擎被裁是有意策略(日常查询不跑研究源),
        不算挤出;filter 在全裁时会回退保留原 combo,这是另一条已声明的语义。
        """
        budget = _contract_budget(mode, depth, context)
        if budget is None:
            return set(combo)
        allowed = [e for e in combo if tier_of(e) != "research_only"]
        kept = allowed if allowed else list(combo)
        return set(kept[:budget])

    def test_budget_members_survive_policy(self, domains):
        from route import _apply_engine_policy
        from engine_policy import _tier_lookup

        tier_of = _tier_lookup()
        violations: list[str] = []
        for d in domains:
            combo = list(d["engines_combo"])
            for mode in _MODES:
                for depth in _DEPTHS:
                    for context in _CONTEXTS:
                        out = _apply_engine_policy(
                            list(combo), mode=mode, depth=depth, context=context)
                        want = self._expected_keep(
                            combo, mode=mode, depth=depth, context=context,
                            tier_of=tier_of)
                        if not out:
                            violations.append(
                                f"{d['name']} [{mode}/{depth}/{context}]: "
                                f"策略后为空,原 combo={combo}")
                        elif not (want & set(out)) == want:
                            lost = sorted(want - set(out))
                            violations.append(
                                f"{d['name']} [{mode}/{depth}/{context}]: "
                                f"预算内成员被挤出 {lost},combo={combo} → {out}")
        assert not violations, (
            f"{len(violations)} 处策略后形态违约(前 10 条):\n  "
            + "\n  ".join(violations[:10]))

    def test_policy_never_invents_engines(self, domains):
        """策略只做过滤与排序,不得引入声明之外的引擎(拼写错误/表漂移的形态)。"""
        from route import _apply_engine_policy

        ghosts: list[str] = []
        for d in domains:
            combo = set(d["engines_combo"])
            for mode in _MODES:
                for depth in _DEPTHS:
                    out = _apply_engine_policy(
                        list(d["engines_combo"]), mode=mode, depth=depth,
                        context="search")
                    extra = set(out) - combo
                    if extra:
                        ghosts.append(
                            f"{d['name']} [{mode}/{depth}]: 幽灵引擎 {sorted(extra)}")
        assert not ghosts, "\n  ".join(ghosts)


# ── 2. 执行层:冷环境可达 ─────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def cold_env(tmp_path_factory):
    import engine_admission
    from circuit_breaker import CircuitBreaker

    tmp = tmp_path_factory.mktemp("cold-state")
    breaker = CircuitBreaker(state_path=str(tmp / "breaker.json"))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(engine_admission, "DEFAULT_ADMISSION_DIR", tmp / "admission")
        yield breaker


class TestColdEnvironmentReachable:
    """全新状态目录下,策略后的 combo 必须全员放行。

    准入与熔断都是**运行时状态**:有故障史才有资格拦。冷状态拦人只可能是
    两类 bug——准入表残留(隔离失效/路径漂移)或拦截判定写反。这两类都
    只能靠「对全量 combo 扫冷状态」抓出来,单测抓不住。
    """

    def test_admission_passes_everyone_when_cold(self, cold_env):
        from engine_admission import filter_routable

        stripped: list[str] = []
        for d in _domains():
            combo = list(d["engines_combo"])
            kept = filter_routable(combo)
            if kept != combo:
                stripped.append(f"{d['name']}: {combo} → {kept}")
        assert not stripped, (
            "冷状态(无准入记录)下仍有引擎被拦——拦截判定或目录隔离有问题:\n  "
            + "\n  ".join(stripped[:10]))

    def test_breaker_passes_everyone_when_cold(self, cold_env):
        blocked: list[str] = []
        for d in _domains():
            for e in d["engines_combo"]:
                ok, reason = cold_env.allow(e)
                if not ok:
                    blocked.append(f"{d['name']}/{e}: {reason}")
        assert not blocked, (
            "冷状态(无故障历史)下熔断器拦人——判据或状态文件有问题:\n  "
            + "\n  ".join(blocked[:10]))


# ── 3. 路由回归锚点 ──────────────────────────────────────────────────────────

class TestRoutingRegressionAnchors:
    """具体查询 × 错误落点的负向锚点。

    不锁「必须落到哪个域」(兜底语义会演进),只锁「不许落到明显错误的域」。
    route_query 是纯本地计算(正则 + TF-IDF),不联网。
    """

    def test_polysemous_spec_word_not_highjacked_into_patents(self):
        """specification 是专利术语也是普通技术词:一词多义不得独占路由。

        修复前「OpenAI MCP specification」被 `(?i)...|specification|...`
        命中 patent_search,只剩 google_patents 一个可跑引擎,语义上应是
        专利语境词组(patent specification)而非单词。"""
        from route import route_query

        r = route_query("OpenAI MCP specification", mode="auto")
        assert r.get("domain") != "patent_search", (
            f"英文技术查询被劫持进专利域:{r.get('domain')} / {r.get('engines')}")

    def test_real_patent_queries_still_reach_patent_domain(self):
        """修复不得过宽:真专利查询必须仍然命中专利域(两条语言各一)。"""
        from route import route_query

        for q in ("wireless charging patent", "固态电池 专利 分布"):
            r = route_query(q, mode="auto")
            assert r.get("domain") == "patent_search", (
                f"真专利查询 {q!r} 未命中专利域(落点 {r.get('domain')}),"
                "正则修复过宽")
