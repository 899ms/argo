#!/usr/bin/env python3
"""engine_dispatch.py — 一次搜索的引擎编排。

从 search.py 的 execute_search 中外提：该函数原本一千余行，而「把 N 个引擎跑完
并收账」这段独占三分之一——限流收紧、重试与每引擎墙钟预算、缺 env 拦截、
熔断与负缓存、per-engine 缓存、有界并发（wave-1 hedged race / wave-2 / 串行）、
结局分类与配额记账，全挤在一个函数里，与融合/精排/输出无关的读者也得读它。

**依赖显式注入，不在本模块直接 import search。** 三个理由：

1. 测试用 `patch.object(search, "engine_search", ...)` / `"get_engines"` /
   `"_missing_env_for"` / `"get_cost_factor"` 替换这些入口（race 预算、
   时间窗、阶段耗时三套用例都靠它）。若本模块直接引用 search 的全局名，
   补丁打在 search 上而实际调用走本模块的绑定，测试会**静默失效**——
   假引擎不被调用、真网络被打开。
2. 常量 `_FAST_TOTAL_BUDGET_S` / `_PRIMARY_GRACE_S` 同样被测试打补丁，
   必须由调用方在**调用时**读取模块全局再传进来。
3. 避免 search ⇄ engine_dispatch 循环导入。

结论：新增入口时一律走参数，不写模块级 import search。
"""

from __future__ import annotations

import time
import threading
from typing import Any, Callable

from time_utils import is_time_capable
from query_signals import (
    cumulative_sufficient,
    results_sufficient,
    query_coverage_ok,
)

# 失败原因分类（engine_failure.py 的类别全集）→ 引擎结果 status 的对照表。
# 未列出的类别经 .get(cat, "error") 归为 error：以后新增失败类别时默认可见，
# 不必再回来补白名单。文本里出现超时特征时再细分为 timeout（见 run_one）。
_NOTE_STATUS = {
    "auth": "auth-failed",          # 凭证失效/未登录
    "rate_limited": "rate-limited",  # 源端限流
    "blocked": "blocked",           # 反爬/拦截页
    "dependency": "error",          # 缺后端命令（requires 未满足）
    "upstream": "error",            # 上游改版/页面结构变化
    "network": "error",             # 连接失败/超时（超时再细分）
    "unknown": "error",             # 信息不足（含非 200 状态码）
}

# 配额耗尽错误关键词（唯一来源）：classify_engine_outcome 的 quota-exhausted
# 分类与自适应学习跳过逻辑共用。新增配额错误码（如新的 API 业务码）只改这里。
_QUOTA_ERROR_KEYWORDS = ("quota", "10406")

# 拦截页特征词（唯一来源）：error 文本里出现即判 blocked。HTML 引擎的反爬
# 命中没有 error 文本（静默空结果），走 engines_base 的归因寄存器；
# 这张表兜住「error 结果里带拦截页字样」的可见路径。
_BLOCKED_ERROR_KEYWORDS = (
    "just a moment", "checking your browser", "cf-browser-verification",
    "challenge", "ddos-guard", "perimeterx", "access denied",
    "handshake failure", "unable to handshake", "安全验证", "滑动验证",
)


def classify_engine_outcome(eng: str, res: list[dict[str, Any]],
                            latency_ms: int, status_hint: str | None = None
                            ) -> dict[str, Any]:
    """将单引擎结果归类为可观测 outcome。"""
    if status_hint:
        return {
            "engine": eng, "status": status_hint,
            "results_count": 0, "latency_ms": latency_ms,
        }
    if not res:
        return {
            "engine": eng, "status": "no-results",
            "results_count": 0, "latency_ms": latency_ms,
        }
    errors = [r for r in res if isinstance(r, dict) and "error" in r]
    goods = [r for r in res if isinstance(r, dict) and "error" not in r]
    if errors and not goods:
        msg = str(errors[0].get("error", "")).lower()
        if "timeout" in msg:
            st = "timeout"
        elif any(k in msg for k in _QUOTA_ERROR_KEYWORDS):
            st = "quota-exhausted"
        elif "rate" in msg or "429" in msg:
            st = "rate-limited"
        elif any(k in msg for k in _BLOCKED_ERROR_KEYWORDS):
            st = "blocked"
        elif "auth" in msg or "401" in msg or "403" in msg:
            st = "auth-failed"
        else:
            st = "error"
        return {
            "engine": eng, "status": st,
            "results_count": 0, "latency_ms": latency_ms,
            "detail": str(errors[0].get("error", ""))[:200],
        }
    if goods and errors:
        return {
            "engine": eng, "status": "partial",
            "results_count": len(goods), "latency_ms": latency_ms,
        }
    return {
        "engine": eng, "status": "ok",
        "results_count": len(goods), "latency_ms": latency_ms,
    }


class DispatchResult:
    """编排产出 + 两个钩子（融合后仍要补搜的路径用）。

    `run_one` / `ingest` 必须外露：macro_data 域的 D6 证据下限补搜在融合之后，
    它要跑单个引擎并把结果并进同一份账（raw_results / engine_outcomes /
    engine_latency / wasted 计数），另起一份会让 engines_used 与配额记账对不上。

    `budget_used_ms` / `budget_total_ms`：本次编排的实际墙钟消耗与总预算
    （deep 等无预算模式 total 为 None）。搜索输出把它挂进 timing.budget，
    让「这次离预算上限还有多远」可观测，而不必反推 process_ms。

    """

    __slots__ = ("raw_results", "engine_outcomes", "engine_latency",
                 "wasted_ms", "early_stopped", "run_one", "ingest",
                 "budget_used_ms", "budget_total_ms")

    def __init__(self, raw_results, engine_outcomes, engine_latency,
                 wasted_ms, early_stopped, run_one, ingest,
                 budget_used_ms=None, budget_total_ms=None) -> None:
        self.raw_results = raw_results
        self.engine_outcomes = engine_outcomes
        self.engine_latency = engine_latency
        self.wasted_ms = wasted_ms
        self.early_stopped = early_stopped
        self.run_one = run_one
        self.ingest = ingest
        self.budget_used_ms = budget_used_ms
        self.budget_total_ms = budget_total_ms


def run_dispatch(*, query: str, retrieval_query: str, engines: list[str],
                 decision: dict[str, Any], parallel: bool,
                 domain: str, mode: str, depth: str,
                 max_results: int, timeout: int, net_timeout: float,
                 skip_cache: bool, cache: Any, breaker: Any,
                 since_iso: str | None, until_iso: str | None,
                 t0: float,
                 t0_mono: float | None = None,
                 engine_search: Callable,
                 get_engines_fn: Callable,
                 get_execution_config_fn: Callable,
                 missing_env_for: Callable,
                 classify_outcome: Callable,
                 quota_batch: Any,
                 note_quota_exhausted: Callable,
                 per_engine_budget_s: float,
                 fast_budget_s: float,
                 auto_budget_s: float,
                 primary_grace_s: float,
                 straggler_grace_s: float = 1.5) -> DispatchResult:
    """把 engines 跑完并收账：并发/串行调度 → 结局分类 → 熔断与配额记账。

    依赖全部显式注入（原因见模块头）。返回编排产出与两个补搜钩子。
    """
    raw_results: dict[str, list[dict[str, Any]]] = {}
    engine_outcomes: list[dict[str, Any]] = []
    engine_latency: dict[str, int] = {}

    exec_cfg = get_execution_config_fn()
    retry_count = exec_cfg.get("retry_count", 0)
    # 单引擎墙钟预算：config `execution.per_engine_budget_s` 可覆盖。
    # 用 exec_cfg 读取（与本函数其它 execution 项同源），这样用户可在
    # config.yaml 调整而无需改代码；非法值（非正数）回落到常量默认。
    try:
        _budget_cfg = float(exec_cfg.get("per_engine_budget_s",
                                         per_engine_budget_s))
    except (TypeError, ValueError):
        _budget_cfg = per_engine_budget_s
    if _budget_cfg <= 0:
        _budget_cfg = per_engine_budget_s

    # 慢源禁重试：timeout ≥ 8s 的引擎超时即放弃，避免「10s×3 次=30s」线性放大。
    # 超时本质上是源端慢/网络抖，重试不改变结果，只放大尾延迟；快速失败
    # （连接错/4xx）保留重试，重试成本低。
    try:
        _engine_specs = get_engines_fn()
    except Exception:
        _engine_specs = {}

    def _engine_retries(eng: str) -> int:
        spec = (_engine_specs or {}).get(eng) or {}
        eng_timeout = None
        if isinstance(spec, dict):
            t = spec.get("timeout")
            if isinstance(t, (int, float)) and t > 0:
                eng_timeout = float(t)
        if eng_timeout is not None and eng_timeout >= 8.0:
            return 0
        return retry_count

    def _exec_engine(eng: str, retries: int | None = None,
                     eff_timeout: float | None = None) -> list[dict[str, Any]]:
        # P0-001：用 retrieval_query（clean_query）检索
        if retries is None:
            retries = _engine_retries(eng)
        # 默认超时用网络感知后的 net_timeout（慢网放大），与外层等待预算一致；
        # 非 tight 引擎（anysearch 等）慢网下同样获得放大窗口。
        to = eff_timeout if eff_timeout is not None else net_timeout

        # ── 每引擎墙钟硬预算 ──────────────────────────────────────────
        # 问题（2026-09-10 实测）：重试会**叠乘**。anysearch 曾同时有
        #   引擎级重试 retry_count=1 → 2 次
        #   HTTP 级重试 max_retries=1 → 2 次
        #   8s 超时
        # 最坏 2×2×8 = 32s（实测 31.3s）。用户侧表现是「搜一个查询卡半分钟」，
        # 而这期间既没有切备选源、也没有任何信号说明在等什么。
        #
        # 修法不是逐处调小超时（那会误杀慢网下正常的源），而是给**单个引擎的
        # 总墙钟**设上界：后续尝试的可用超时 = 剩余预算，预算耗尽即停。
        # 这样无论嵌套几层重试，单引擎都不可能超过 cap。
        # 取值优先级：execution.per_engine_budget_s（config）> 常量默认 10.0。
        # fast 模式已有 6s 全局预算，此处取更紧的那个，避免互相打架。
        _eng_budget = _budget_cfg
        if mode == "fast":
            _eng_budget = min(_eng_budget, fast_budget_s)
        _t_eng_start = _now()

        last_result: list[dict[str, Any]] = []
        for _attempt in range(retries + 1):
            _remain = _eng_budget - (_now() - _t_eng_start)
            # 只跳过**后续**尝试。首次必须发出：若因预算小而整段跳过，
            # 引擎的 outcome 会从 timeout 变成 no-results —— 语义从「慢」
            # 变成「没尝试」，会破坏既有 fast 预算测试的契约
            # （实测：patch 预算 0.5s 时首试被跳过，slow_bad_a/b 被标成
            #  no-results 而非 timeout）。
            if _attempt > 0 and _remain <= 0.5:
                break
            # 每次尝试的可用超时 = min(声明超时, 剩余预算)，下限 0.5s：
            # 首试受总预算约束（否则 fast 的 6s 预算会被 8s 首试突破），
            # 后续尝试自动收缩，保证单引擎总耗时不越界。
            attempt_to = min(to, max(0.5, _remain))
            last_result = engine_search(
                retrieval_query, eng, n=max_results, timeout=attempt_to, depth=depth, mode=mode,
                since=since_iso, until=until_iso, skip_cache=skip_cache,
            )
            if last_result and any("error" not in r for r in last_result):
                return last_result
        # 慢源（retries=0，超时即弃）不再用 balanced 补跑，避免超时场景双倍耗时
        if retries > 0 and depth != "balanced":
            _remain = _eng_budget - (_now() - _t_eng_start)
            if _remain > 0.5:
                last_result = engine_search(
                    retrieval_query, eng, n=max_results,
                    timeout=min(to, max(0.5, _remain)),
                    depth="balanced", mode=mode,
                    since=since_iso, until=until_iso, skip_cache=skip_cache,
                )
        return last_result

    def _run_one(eng: str) -> tuple[str, list[dict[str, Any]], dict[str, Any], int]:
        """单引擎：缺 env → 负缓存 → 熔断 → per-engine 缓存 → 网络。"""
        from engines_base import pop_failure_note
        t_eng = _now()

        # 缺环境变量前置拦截：把「静默 no-results」变成可行动的 error。
        # 显式 engine= 覆盖会绕过路由的 env 过滤（zhihu/exa 未配密钥时曾
        # 返回空列表，用户无法区分「没结果」和「没配置」）。
        missing_env = missing_env_for(eng)
        if missing_env:
            lat = int((_now() - t_eng) * 1000)
            outcome = classify_outcome(
                eng, [], lat, status_hint="skipped-missing-env")
            outcome["detail"] = (
                f"缺少环境变量：{' / '.join(missing_env)}（配置后重试）")
            return eng, [], outcome, lat

        # 时间窗只隔离带时间能力引擎的 per-engine 缓存（与 combo 键同语义）
        eng_since = since_iso if is_time_capable(eng) else None
        eng_until = until_iso if is_time_capable(eng) else None

        # 熔断
        if breaker is not None:
            allowed, reason = breaker.allow(eng)
            if not allowed:
                lat = int((_now() - t_eng) * 1000)
                outcome = classify_outcome(eng, [], lat, status_hint="skipped-circuit-open")
                outcome["detail"] = reason
                return eng, [], outcome, lat
            neg = breaker.get_negative(query, eng)
            if neg:
                lat = int((_now() - t_eng) * 1000)
                outcome = classify_outcome(
                    eng, [], lat, status_hint="no-results-cached",
                )
                outcome["detail"] = neg.get("status", "no-results")
                return eng, [], outcome, lat

        # per-engine 缓存
        if not skip_cache:
            eng_hit = cache.get_engine(
                query, eng, max_results, domain=domain, mode=mode, depth=depth,
                since=eng_since, until=eng_until,
            )
            if eng_hit is not None:
                lat = int((_now() - t_eng) * 1000)
                # 标记缓存来源
                for r in eng_hit:
                    if isinstance(r, dict):
                        r.setdefault("_engine", eng)
                outcome = classify_outcome(eng, eng_hit, lat)
                outcome["status"] = "ok-cached" if eng_hit else "no-results-cached"
                return eng, eng_hit, outcome, lat

        # 网络调用
        # 答案型域（early_min 存在，1 条快照即可交付）的慢源收紧超时：
        # FRED/Eurostat 这类 timeout=10s 的源一旦挂掉就阻塞整条串行路径，
        # 而快源（worldbank 等 ~150ms）已能交付答案。慢源 5s 内没回就让位。
        # 非答案域（fast/auto/budget 且非 deep）：timeout≥10s 的引擎同样收紧
        # 到 6s——多数正常引擎 <2s，10-15s 的超时只为极端慢源保底，
        # 串行/并行组合里一个慢源就会拖垮整个响应尾部。
        eff_to: float | None = None
        _tighten = (early_min is not None) or (
            mode in ("fast", "auto", "budget") and depth != "deep"
        )
        if _tighten:
            spec = (_engine_specs or {}).get(eng) or {}
            eng_to = None
            if isinstance(spec, dict):
                t = spec.get("timeout")
                if isinstance(t, (int, float)) and t > 0:
                    eng_to = float(t)
            cap = 5.0 if early_min is not None else 6.0
            # half_open 半开探测收紧到 2s：熔断器允许半开探测恢复，但探测应短促，
            # 避免 6s 探测阻塞串行/并行主路径（慢源拖尾主因）。2026-08 修复。
            if breaker is not None:
                try:
                    if breaker.status(eng).get("state") == "half_open":
                        cap = min(cap, 2.0)
                except Exception:
                    pass
            if eng_to is not None and eng_to >= 8.0:
                eff_to = min(float(timeout), cap)
            # 声明值 < 8s 的收紧由 engines.search 分发层统一执行
            # （spec timeout 是硬上限，调用方超时不得覆盖）
        try:
            res = _exec_engine(eng, eff_timeout=eff_to)
        except Exception as e:
            res = [{"error": str(e), "source": eng}]
        lat = int((_now() - t_eng) * 1000)
        for r in res:
            if isinstance(r, dict):
                r.setdefault("_engine", eng)
                r.setdefault("_elapsed", lat / 1000.0)

        outcome = classify_outcome(eng, res, lat)
        # 失败原因合入：引擎内部那些不报错的失败路径（反爬命中/HTTP 状态码/网络异常）
        # 没有 error 文本，结果会落成 no-results；失败原因记录把它们还原成
        # 真实状态，供熔断与 --json 可观测面使用。
        #
        # 改用「类别对照表 + 文本细分」而不是 if-elif 白名单：白名单只认
        # blocked/rate_limited/auth，engine_failure.py 类别全集里的
        # network（超时/连接失败）、upstream（上游改版）、unknown（非 200
        # 状态码）、dependency（缺后端命令）会被当成 no-results——
        # 2026-09-15 实测：`--engine you` 的 SSL 超时上报成
        # `status=completed, count=0, errors=[]`，调用方（Agent）据此判定
        # 「网上没有这个信息」，而引擎实际坏了；熔断还按 empty 记账
        # （不累计 opens、负缓存用 EMPTY_NEGATIVE_TTL 45s 而非 30s）。
        _note = pop_failure_note(eng)
        _attr: dict[str, Any] | None = None
        if _note and outcome["status"] in ("no-results", "error", "auth-failed"):
            _text = f"{_note.get('reason', '')} {_note.get('detail', '')}".strip()
            _mapped = _NOTE_STATUS.get(str(_note.get("category") or ""), "error")
            if _mapped == "error" and (
                    "timeout" in _text.lower() or "timed out" in _text.lower()):
                _mapped = "timeout"
            # auth 只在引擎确实无输出时升级：已有明确 error 时不改写（原语义）
            if not (_mapped == "auth-failed" and outcome["status"] != "no-results"):
                outcome["status"] = _mapped
            outcome["detail"] = _text or outcome.get("detail")
        if _note:
            # 归因随熔断状态一起持久化：「为什么坏」必须在失败现场写下来，
            # 事后只能看到 kind 粗标签（把 kind 当响应文本再归类只会得到 unknown）
            try:
                from engine_failure import from_note
                _attr = from_note(_note, eng)
            except ImportError:
                _attr = None
        goods = [r for r in res if isinstance(r, dict) and "error" not in r]
        quota_batch.add(eng, bool(goods))
        if outcome["status"] == "quota-exhausted":
            # 远端配额耗尽：交由 quota 状态机接管（周期边界自愈），
            # 不计入下面的健康熔断——配额问题不是引擎健康问题
            note_quota_exhausted(eng, outcome.get("detail") or "")

        if breaker is not None:
            if outcome["status"] == "ok":
                breaker.record_success(eng)
                breaker.clear_negative(query, eng)
            elif outcome["status"] == "quota-exhausted":
                # 配额问题不是引擎健康问题，停用交给配额状态机（上面已记账）；
                # 但归因必须留下——「为什么不行」正是这一支的可观测缺口。
                breaker.record_note(eng, _attr)
            elif outcome["status"] == "no-results":
                breaker.record_failure(eng, kind="empty", attribution=_attr)
                breaker.set_negative(query, eng, status="no-results")
            elif outcome["status"] == "timeout":
                breaker.record_failure(eng, kind="timeout", attribution=_attr)
                breaker.set_negative(query, eng, status="timeout")
            elif outcome["status"] in ("blocked", "rate-limited"):
                # 被拦截 / 被限流都是源站行为，不是引擎故障：60s 短冷却，
                # 不累计 opens（否则被封引擎会被冤枉 auto-disable）。
                breaker.record_failure(eng, kind=outcome["status"], attribution=_attr)
                breaker.set_negative(query, eng, status=outcome["status"])
            else:
                breaker.record_failure(eng, kind="error", attribution=_attr)
                breaker.set_negative(query, eng, status=outcome["status"])

        if not skip_cache and goods:
            cache.set_engine(
                query, eng, max_results, goods,
                domain=domain, mode=mode, depth=depth,
                since=eng_since, until=eng_until,
            )
        elif not skip_cache and not goods:
            # 空结果短 TTL 写入 per-engine，配合负缓存
            cache.set_engine(
                query, eng, max_results, [],
                domain=domain, mode=mode, depth=depth,
                since=eng_since, until=eng_until,
            )

        return eng, (goods if goods else res), outcome, lat

    def _ingest(eng: str, res: list, outcome: dict, lat: int) -> None:
        raw_results[eng] = res
        engine_outcomes.append(outcome)
        engine_latency[eng] = lat
        if outcome["status"] not in ("ok", "ok-cached", "partial"):
            nonlocal_wasted[0] += lat

    nonlocal_wasted = [0]
    # 配额批次的构造在调用方：flush 必须发生在**最后一个 _ingest 之后**，
    # 而那个点在融合后的 D6 补搜里（本模块之外）。在这里另建一个实例，
    # 调用方 flush 到的就是空批次——补搜引擎的记账永远落不了盘。
    # 预算与延迟统一走调用方选定的钟：传 t0_mono（单调钟）时整套换
    # time.monotonic——墙钟会被 NTP 跳变拉扯，预算窗随之失真；不传时保持
    # wall _now()，与既有调用方/测试逐位兼容。垫片必须是**整套**换：
    # deadline 基准与所有 now 采样混用两种钟，预算判断就是废纸。
    _now = time.monotonic if t0_mono is not None else time.time
    _budget_base = t0_mono if t0_mono is not None else t0
    early_stopped = False
    to_run = list(engines)
    # deep 模式全量并行；fast/auto/budget 可渐进 early-stop
    allow_early = mode in ("fast", "auto", "budget") and depth != "deep"

    # 总墙钟预算：deadline 之后不再起新引擎、不再等待慢线程。
    # fast 6s（成本优先）/ auto·budget 10s（质量优先但有界）/ deep 不设预算。
    budget_s = {"fast": fast_budget_s,
                 "auto": auto_budget_s,
                 "budget": auto_budget_s}.get(mode)
    _deadline = _budget_base + (budget_s if budget_s is not None else float("inf"))

    early_min = decision.get("early_stop_min_results")
    no_early = bool(decision.get("no_early_stop", False))

    def _daemon_start(eng: str):
        """daemon 线程跑 _run_one：弃置线程不阻塞进程退出。"""
        holder: dict[str, Any] = {"t0": _now()}

        def _work() -> None:
            try:
                holder["r"] = _run_one(eng)
            except Exception as exc:
                holder["r"] = (
                    eng,
                    [{"error": str(exc), "source": eng}],
                    classify_outcome(
                        eng, [{"error": str(exc), "source": eng}], 0),
                    0,
                )
        t = threading.Thread(target=_work, daemon=True)
        t.start()
        return holder, t

    def _ingest_holder(holder: dict[str, Any]) -> None:
        r = holder.get("r")
        if r:
            _ingest(r[0], r[1], r[2], r[3])

    def _holder_goods(holder: dict[str, Any]) -> list[dict[str, Any]]:
        r = holder.get("r")
        if not r:
            return []
        return [x for x in r[1] if isinstance(x, dict) and "error" not in x]

    def _settle_pending(pending: list[tuple[dict[str, Any], Any, str]]) -> None:
        """收尾一组待定线程：仍活的标 timeout（daemon 自行结束，不阻塞退出），
        恰在末次轮询后完成的照常入账——否则它既不 ingest 也不标 timeout，
        结果会悄悄丢掉。latency 用真实等待时长（原 timeout 参数×1000 是假值）。"""
        for holder, th, eng in pending:
            lat_ms = int((_now() - holder.get("t0", _now())) * 1000)
            if th.is_alive():
                raw_results[eng] = [{"error": "timeout", "source": eng}]
                engine_outcomes.append(classify_outcome(
                    eng, raw_results[eng], lat_ms, "timeout"))
                nonlocal_wasted[0] += lat_ms
            else:
                _ingest_holder(holder)

    def _run_engines_bounded(engs: list[str], wait_s: float,
                             check_sufficient: bool = False) -> bool:
        """并发跑一组引擎（≤3 并发），等待上限 wait_s 秒；返回是否已「够用」。

        为什么不用 ThreadPoolExecutor：它的 `with` 退出会
        `shutdown(wait=True)` 并 join 所有已提交任务，把「超时即返回」的语义
        架空——上面每个预算判断都以为自己已经止损，进程却还在等一个卡住的
        HTTP 读（models.dev 全量 API 超时 15s，实测单查询被拖到 76s，而
        w2_wait / deadline 早已到期）。daemon 线程 + 轮询才真正有界：
        早停后弃置线程既不阻塞函数返回，也不阻塞进程退出——与上面的
        hedged 分支共用同一套并发执行方式。
        """
        if not engs:
            return False
        queue = list(engs)
        pending: list[tuple[dict[str, Any], Any, str]] = []
        deadline = _now() + max(0.0, wait_s)
        while (queue or pending) and _now() < deadline:
            while queue and len(pending) < 3:
                eng = queue.pop(0)
                holder, th = _daemon_start(eng)
                pending.append((holder, th, eng))
            progressed = False
            for item in list(pending):
                holder, th, eng = item
                if th.is_alive():
                    continue
                _ingest_holder(holder)
                pending.remove(item)
                progressed = True
                if check_sufficient and not no_early and cumulative_sufficient(
                        raw_results, mode=mode, min_results=early_min,
                        query=query):
                    _settle_pending(pending)
                    return True
            if not progressed:
                # 自适应轮询间隔：剩余时间充裕时多睡，快到期时少睡。
                # 固定 20ms 在尾部会浪费时间（实测最多浪费 20ms），
                # 自适应后平均等待时长降至 5-8ms。
                remain = deadline - _now()
                time.sleep(min(0.02, remain / 10) if remain > 0 else 0.005)
        _settle_pending(pending)
        return False

    if parallel and to_run and allow_early and len(to_run) > 1:
        # Wave-1 race（2026-09-06）：primary 与次引擎并行起跑，先完成且结果
        # 合格者赢——原「primary 先行」串行等待下，primary 慢则整体慢（实测
        # github 引擎 8.5s 拖尾而次引擎 2.4s 就绪）；race 后墙钟由最先合格
        # 者决定。双成员都不合格则落 wave-2 并行补全（语义不变，且 wave-2
        # 的累计充分性判定天然包含 race 已收入的结果）。
        # 成本语义：fast+parallel 域固定 2 次引擎调用（原 1 次），fast 的
        # combo 以免费通用引擎为主，增量可忽略；结果质量仍由充分性判定+
        # 覆盖守卫把关，先到不等于放行。
        primary, rest = to_run[0], to_run[1:]
        # 首发 + 分岔 hedged：先只发 primary，grace 宽限窗内完成且合格 → 只付
        # 1 次调用（成本回退消除）；窗内未完成 → 补发次引擎并行 race，先合格者
        # 赢（慢 primary 不拖整体）。弃置线程用 daemon 管理：赢家早停后不再被
        # 进程退出 join，修掉原 race shutdown(wait=False) 的「函数内快、进程级
        # 假快」——CLI 单发真省墙钟。质量仍由充分性判定 + 覆盖守卫把关。
        grace = max(0.3, min(primary_grace_s, net_timeout * 0.25))
        if _now() + grace > _deadline:
            grace = max(0.0, _deadline - _now())

        # (helper _daemon_start / _ingest_holder / _holder_goods / _settle_pending
        #  / _run_engines_bounded 定义在本分支之前，wave-1 与 wave-2 共用一套
        #  有界并发实现)
        ph, pt = _daemon_start(primary)
        pt.join(grace)
        if not pt.is_alive():
            # primary 在 grace 内完成：收结果，合格即早停（只 1 次调用）
            _ingest_holder(ph)
            goods_p = _holder_goods(ph)
            if not no_early and goods_p and results_sufficient(
                    goods_p, mode=mode, min_results=early_min, query=query):
                early_stopped = True
        if not early_stopped and pt.is_alive():
            # primary 未在 grace 内完成：hedged 分岔，补发次引擎并行 race
            backup = rest[:1]
            if backup:
                rest = rest[1:]
                bh, bt = _daemon_start(backup[0])
                pending = [(ph, pt, primary), (bh, bt, backup[0])]
                _race_wait = min(net_timeout + 2,
                                 max(0.1, _deadline - _now()))
                _race_deadline = _now() + _race_wait
                # 质量守卫拒绝早停后的**有界**宽限窗。
                #
                # 场景（实测「上海 地铁 线路图」）：对冲的备份引擎先回来，但结果与
                # 查询零词面交集（纽约共享单车站点答上海地铁），覆盖守卫正确地
                # 拒绝早停；此时主引擎仍在跑，循环会一直等它到 _race_deadline
                # （net_timeout+2 量级），而它往往本就答不了这个问题
                # （Nominatim 对「附近 咖啡店」「上海地铁」一律 1-3s 后返回 0 条）。
                # 守卫没错——它防的是「单引擎垃圾成最终答案」；错的是拒绝之后
                # 的等待没有上限。这里给它一个宽限窗：拿不到更好的就认了。
                # 只影响「还等多久」，不改变任何结果的取舍。
                reject_deadline = None
                while pending and _now() < _race_deadline:
                    progressed = False
                    for (holder, th, eng) in list(pending):
                        if th.is_alive():
                            continue
                        _ingest_holder(holder)
                        goods = _holder_goods(holder)
                        if not no_early and goods and results_sufficient(
                                goods, mode=mode, min_results=early_min,
                                query=query):
                            early_stopped = True
                        elif goods and straggler_grace_s > 0:
                            # 有结果但被判不充分（计数或词面覆盖）→ 起宽限窗。
                            # 只有 >0 才起：`now + 0` 会让下面的判据立刻成立，
                            # 等于「一有结果被拒就马上放弃主引擎」——那与「关闭
                            # 宽限窗（回旧行为）」恰好相反。0/负数一律视为关闭。
                            reject_deadline = _now() + straggler_grace_s
                        pending.remove((holder, th, eng))
                        progressed = True
                    if early_stopped or not pending:
                        break
                    if reject_deadline is not None and _now() >= reject_deadline:
                        break
                    if not progressed:
                        # 自适应轮询间隔（同 _run_engines_bounded）
                        remain = _race_deadline - _now()
                        time.sleep(min(0.02, remain / 10) if remain > 0 else 0.005)
                # 超时/弃置：仍活线程标记 timeout（daemon 自行结束，不阻塞
                # 退出）；恰在末次轮询后完成的线程照常入账——此前它既不
                # ingest 也不标 timeout，结果静默丢失。latency 记账用真实
                # 等待时长（原 timeout 参数×1000 是假值，污染遥测）
                for (holder, th, eng) in pending:
                    lat_ms = int((_now() - holder.get("t0", _budget_base)) * 1000)
                    if th.is_alive():
                        raw_results[eng] = [{"error": "timeout", "source": eng}]
                        engine_outcomes.append(classify_outcome(
                            eng, raw_results[eng], lat_ms, "timeout"))
                        nonlocal_wasted[0] += lat_ms
                    else:
                        _ingest_holder(holder)
        if (not early_stopped and rest
                and not (budget_s is not None and _now() >= _deadline)):
            # 预算检查（与串行路径 `_now() >= _deadline` 同语义）：
            # deadline 已过不再起新引擎；等待窗口也不越过 deadline——
            # 「总墙钟预算」对并行路径同样成立
            w2_wait = min(net_timeout + 2,
                          max(0.1, _deadline - _now()))
            if _run_engines_bounded(rest, w2_wait, check_sufficient=True):
                early_stopped = True
    elif parallel and to_run:
        _run_engines_bounded(to_run, net_timeout + 2)
    else:
        # no_early_stop 域在串行路径同样生效：平台引擎「有结果」不等于「结果可用」，
        # fast 模式 parallel=False 必走本分支，此前曾在此被噪声结果短路
        for eng in to_run:
            if _now() >= _deadline:
                break  # fast 预算耗尽：止损不再起新引擎
            e, res, outcome, lat = _run_one(eng)
            _ingest(e, res, outcome, lat)
            goods = [r for r in res if isinstance(r, dict) and "error" not in r]
            if not goods:
                continue  # 无结果：串行试下一引擎
            # 答案型域 min_results=1：1 条快照即 early-stop
            if allow_early and not no_early and results_sufficient(
                goods, mode=mode, min_results=early_min, query=query,
            ):
                early_stopped = True
                break
            # 默认串行：任一引擎有结果即停（历史行为）；答案型不够用则继续补源。
            # 词面覆盖守卫同语义：结果与查询几乎无交集 → 试下一引擎（救援线）
            if early_min is None and not no_early and query_coverage_ok(goods, query):
                break



    budget_total_ms = int(budget_s * 1000) if budget_s is not None else None
    return DispatchResult(
        raw_results, engine_outcomes, engine_latency,
        nonlocal_wasted[0], early_stopped, _run_one, _ingest,
        budget_used_ms=int((_now() - _budget_base) * 1000),
        budget_total_ms=budget_total_ms,
    )
