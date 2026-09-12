#!/usr/bin/env python3
"""test_consistency_gates.py — 一致性与入口门禁。

这个文件守的是一类具体缺陷：**门禁全绿但能力实际不可用**。它们各自都很低级，
却都真实发生过（2026-09-12 审查轮），共同点是「测试没往那里看一眼」：

  1. `argo --help` 直接 NameError 崩溃（f-string 里漏转义的花括号）——
     1500 个测试全绿，因为没有任何测试真的执行过 CLI 入口。
  2. `argo fetch` 写在 SKILL.md 里但 dispatcher 里不存在——文档承诺了
     一个不存在的子命令。
  3. 派生件（registry / quota / domain）与运行时真源脱钩：外置 spec 声明的
     引擎在派生件里缺席；`--check` 拿派生结果跟自己比，永远绿。
  4. 失败归因有两份实现（归因寄存器 + engine_failure），在 97/201 个状态码上
     给出不同答案，同一引擎的解释随界面而变。
  5. 同批次的两个功能互相打架：preflight 判推文 URL 需要登录，而同一批次
     刚上线的 syndication 通道能免登录抓它。

设计原则：这些门禁全部**从真实入口取事实**（执行 CLI、读磁盘派生件、
调用真实函数），不读中间变量的自述——上面第 3 条正是「断言自己」的产物。
"""

import importlib.machinery
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_DIR / "scripts"
BIN_ARGO = SKILL_DIR / "bin" / "argo"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _load_bin_argo():
    """bin/argo 无 .py 后缀，按源码加载器导入。"""
    loader = importlib.machinery.SourceFileLoader("argo_cli", str(BIN_ARGO))
    spec = importlib.util.spec_from_loader("argo_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def argo_cli():
    return _load_bin_argo()


# ── 1. CLI 入口可用性 ────────────────────────────────────────────────────────

class TestCliEntrypoint:
    """入口必须真的能跑：--help 崩过一次，且当时零覆盖。"""

    def test_help_exits_zero_and_prints_usage(self):
        r = subprocess.run([sys.executable, str(BIN_ARGO), "--help"],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"--help 退出码 {r.returncode}: {r.stderr[:400]}"
        assert "Usage:" in r.stdout
        assert "Traceback" not in r.stderr

    def test_no_args_is_usage_error_not_crash(self):
        r = subprocess.run([sys.executable, str(BIN_ARGO)],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 1
        assert "Usage:" in r.stdout
        assert "Traceback" not in r.stderr

    def test_unknown_subcommand_reports_and_prints_usage(self):
        # 旧行为：打印 usage 时抛 NameError，用户看到的是 traceback 而非提示
        r = subprocess.run([sys.executable, str(BIN_ARGO), "definitely-not-a-cmd"],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 1
        assert "Unknown subcommand" in r.stderr
        assert "NameError" not in r.stderr
        assert "Traceback" not in r.stderr

    def test_help_header_count_matches_declared(self, argo_cli):
        """--help 里的收录数必须等于声明总数（曾报启用数，还对不上文档）。"""
        from config import load_config
        declared = len([k for k, v in (load_config().get("engines") or {}).items()
                        if isinstance(v, dict)])
        usage = argo_cli._usage()
        assert f"收录 {declared} 个源" in usage, \
            f"--help 未写收录数 {declared}：{usage.splitlines()[0]!r}"

    def test_usage_has_no_unescaped_fstring_braces(self):
        """字面量花括号必须双写——这是上一版崩溃的直接成因。"""
        text = BIN_ARGO.read_text(encoding="utf-8")
        assert "{{status|inject|undo}}" in text
        call = re.search(r"def _usage\(\).*?return f\"\"\"(.*?)\"\"\"", text, re.S)
        assert call, "未找到 _usage 的 f-string"
        body = call.group(1)
        # 剥掉合法替换字段 {n} 与双写花括号后，不应再有裸花括号
        residue = re.sub(r"\{\w+\}", "", body.replace("{{", "").replace("}}", ""))
        assert "{" not in residue and "}" not in residue, \
            f"_usage 内仍有未转义花括号: {residue[:200]}"


# ── 2. CLI 子命令表 = 文档承诺 = 脚本存在 ────────────────────────────────────

class TestCliSubcommandParity:
    def test_every_advertised_subcommand_is_dispatched(self, argo_cli):
        """_usage 里列出的每个子命令都必须真能分发到脚本。"""
        advertised = set(re.findall(r"^\s{2}argo\s+([a-z][a-z_]*)\s",
                                    argo_cli._usage(), re.M))
        assert advertised, "usage 文本里没解析出子命令"
        missing = sorted(c for c in advertised if c not in _dispatch_table())
        assert not missing, f"usage 承诺了但 dispatcher 没有: {missing}"

    def test_mapped_scripts_exist(self):
        for sub, (script, _defaults) in _dispatch_table().items():
            assert (SCRIPTS_DIR / script).exists(), f"{sub} -> 缺少 {script}"

    def test_fetch_maps_to_fetch_v3(self):
        # SKILL.md 长期宣传 `argo fetch`，但 dispatcher 里没有该键（已修）
        assert _dispatch_table()["fetch"][0] == "fetch_v3.py"


def _dispatch_table() -> dict:
    """从 bin/argo 源码解析子命令 → 脚本映射（不依赖执行 main）。"""
    text = BIN_ARGO.read_text(encoding="utf-8")
    block = re.search(r"scripts = \{(.*?)\n    \}", text, re.S)
    assert block, "未找到 dispatcher 的 scripts 表"
    table = {}
    for name, script in re.findall(r'"(\w+)":\s*\("([^"]+)"', block.group(1)):
        table[name] = (script, [])
    return table


# ── 3. 文档口径 == 代码事实 ──────────────────────────────────────────────────

class TestDocNumbersMatchCode:
    def test_engine_counts_agree_with_catalog(self):
        """SKILL.md / README 的引擎数必须与搜索源文档（本身有门禁）一致。

        口径是声明口径：收录 N 个源、M 个免密钥开箱可用。运行时「此刻能路由
        几个」随密钥与熔断状态变，不写进文档。
        """
        doc = (SKILL_DIR / "docs" / "ENGINE_CATALOG.md").read_text(encoding="utf-8")
        m_doc = re.search(r"收录 (\d+) 个源", doc)
        m_usable = re.search(r"开箱可用 (\d+) 个", doc)
        assert m_doc and m_usable, "搜索源文档缺口径行"
        total, usable = m_doc.group(1), m_usable.group(1)
        for rel in ("SKILL.md", "README.md"):
            text = (SKILL_DIR / rel).read_text(encoding="utf-8")
            assert re.search(rf"{total} 个源", text), \
                f"{rel} 未写收录数 {total}"
            assert usable in text, f"{rel} 未写免密钥可用数 {usable}"

    def test_mcp_tool_count_matches_docs(self):
        from mcp_tools import TOOLS
        n = len(TOOLS)
        for rel in ("package.json", "cordis.patch.yml", "SKILL.md",
                    "packages/dsh-plugin/package.json"):
            text = (SKILL_DIR / rel).read_text(encoding="utf-8")
            claimed = re.findall(r"(\d+)\s*个?\s*MCP\s*工具", text)
            for c in claimed:
                assert int(c) == n, f"{rel} 写 {c} 个 MCP 工具，实际 {n}"

    def test_version_strings_agree(self):
        pkg = json.loads((SKILL_DIR / "package.json").read_text(encoding="utf-8"))
        plug = json.loads(
            (SKILL_DIR / "packages/dsh-plugin/package.json").read_text(encoding="utf-8"))
        skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        m = re.search(r"^version:\s*(\S+)", skill, re.M)
        assert m, "SKILL.md 缺 version"
        assert pkg["version"] == m.group(1) == plug["version"], \
            f"版本不一致: package={pkg['version']} skill={m.group(1)} plugin={plug['version']}"


# ── 4. 派生件与运行时真源一致 ────────────────────────────────────────────────

@pytest.fixture(scope="module")
def sb():
    """sync_backends 模块：派生件的唯一生成者，门禁直接用它的校验器。"""
    import sync_backends
    return sync_backends


class TestDerivedArtifactsInSync:
    """registry / quota / domain 三份派生件必须覆盖运行时可见的全部引擎。

    守的是「人工改文档件、真源没跟上」：新增引擎只写进 registry（死文档），
    配额与领域画像漏侧，且 --check 自比永远绿。
    """

    def test_no_drift_between_disk_and_truth(self, sb):
        engines = sb.load_engines()
        quota = json.loads(sb.QUOTA_PROFILES_PATH.read_text(encoding="utf-8"))
        domain = json.loads(sb.DOMAIN_PROFILES_PATH.read_text(encoding="utf-8"))
        registry = sb._load_yaml(sb.REGISTRY_PATH)
        issues = sb.collect_issues(engines, quota, registry, domain)
        assert not issues, "派生件与真源不一致：\n  - " + "\n  - ".join(issues)

    def test_check_mode_is_not_tautological(self, sb, tmp_path, monkeypatch):
        """--check 必须能失败：把 registry 改坏后应报错，而不是照绿。"""
        engines = sb.load_engines()
        quota = json.loads(sb.QUOTA_PROFILES_PATH.read_text(encoding="utf-8"))
        domain = json.loads(sb.DOMAIN_PROFILES_PATH.read_text(encoding="utf-8"))
        broken = sb._load_yaml(sb.REGISTRY_PATH)
        broken["engines"] = [e for e in broken["engines"]
                             if e["name"] != sorted(engines)[0]]
        issues = sb.collect_issues(engines, quota, broken, domain)
        assert issues, "缺失引擎未被告警——校验退化成自比"

    def test_value_tamper_is_detected(self, sb):
        """名字都在、值被改也必须报——限流/配额就是靠这些值生效的。

        （第一版校验只比引擎名，把 firecrawl.qps 改成 99 直接漏报。）
        """
        engines = sb.load_engines()
        quota = json.loads(sb.QUOTA_PROFILES_PATH.read_text(encoding="utf-8"))
        domain = json.loads(sb.DOMAIN_PROFILES_PATH.read_text(encoding="utf-8"))
        registry = sb._load_yaml(sb.REGISTRY_PATH)
        victim = "firecrawl"
        assert victim in quota, "取样引擎不存在，检查前置条件"
        quota[victim]["qps"] = 99
        issues = sb.collect_issues(engines, quota, registry, domain)
        assert any(victim in i and "qps" in i for i in issues), \
            f"值级篡改未报: {issues[:3]}"

    def test_registry_field_tamper_is_detected(self, sb):
        engines = sb.load_engines()
        quota = json.loads(sb.QUOTA_PROFILES_PATH.read_text(encoding="utf-8"))
        domain = json.loads(sb.DOMAIN_PROFILES_PATH.read_text(encoding="utf-8"))
        registry = sb._load_yaml(sb.REGISTRY_PATH)
        for e in registry["engines"]:
            if e["name"] == "nvd":
                e["coverage"] = ["general"]
        issues = sb.collect_issues(engines, quota, registry, domain)
        assert any("nvd" in i and "coverage" in i for i in issues), \
            f"registry 字段篡改未报: {issues[:3]}"

    def test_declared_engine_metadata_reaches_quota(self, sb):
        """spec 里声明的限流/配额必须出现在派生件里（firecrawl 曾整体漏侧）。"""
        engines = sb.load_engines()
        quota = json.loads(sb.QUOTA_PROFILES_PATH.read_text(encoding="utf-8"))
        declared_specs = 0
        for name, spec in engines.items():
            if "qps" in spec or "limit" in spec:
                declared_specs += 1
                assert name in quota, f"{name} 声明了配额但派生件缺失"
                for key in ("qps", "limit", "period"):
                    if key in spec:
                        assert quota[name][key] == spec[key], \
                            f"{name}.{key}: spec={spec[key]} quota={quota[name][key]}"
        assert declared_specs > 0, "没有任何引擎声明配额，检查取样逻辑"


# ── 5. 失败归因单一真源 ──────────────────────────────────────────────────────

class TestFailureAttributionSingleSource:
    def test_register_and_classifier_agree_on_all_status_codes(self):
        import engine_failure as ef
        import engines_base as eb
        for code in list(range(0, 600)):
            eb._FAIL_NOTES.clear()
            eb._note_http_failure("t", code, "")
            note = eb._FAIL_NOTES.get("t")
            assert note is not None, f"HTTP {code} 未写入归因"
            expected = ef.classify(status_code=code)["category"]
            assert note["category"] == expected, \
                f"HTTP {code}: 寄存器={note['category']} classify={expected}"

    def test_transient_statuses_do_not_claim_upstream(self):
        """503/408/0 不是「上游改版」，给错方向比不给更坏。"""
        import engine_failure as ef
        assert ef.classify(status_code=503)["category"] == ef.RATE_LIMITED
        assert ef.classify(status_code=408)["category"] == ef.NETWORK
        assert ef.classify(status_code=0)["category"] == ef.NETWORK


# ── 6. 同批次功能不得互相打架 ────────────────────────────────────────────────

class TestLoginWallExemption:
    def test_single_tweet_url_is_not_needs_auth(self):
        """syndication 免登录通道能抓的 URL，预检不该说「需要登录」。"""
        from batch_probe import classify_url, NEEDS_AUTH, UNKNOWN
        assert classify_url(
            "https://x.com/elonmusk/status/1585841080431321088") == UNKNOWN
        assert classify_url(
            "https://twitter.com/a/statuses/1585841080431321088?s=20") == UNKNOWN

    def test_non_tweet_urls_still_flagged(self):
        from batch_probe import classify_url, NEEDS_AUTH
        for u in ("https://x.com/someuser",
                  "https://www.instagram.com/p/abc",
                  "https://www.facebook.com/groups/x"):
            assert classify_url(u) == NEEDS_AUTH, u


# ── 7. syndication 通道契约 ──────────────────────────────────────────────────

class TestSyndicationChannel:
    def test_token_nonempty(self):
        """端点要求 token 参数存在（省略则返回空）；值不参与校验。"""
        from engines_builders_intl import syndication_token
        for tid in ("1585841080431321088", "100000000000000"):
            tok = syndication_token(tid)
            assert isinstance(tok, str) and tok.strip()


# ── 8. 按量计费的源不得被 n 桶化放大 ─────────────────────────────────────────

class TestMeteredSourceBilling:
    """n 桶化只为「免费档」服务；计费档被放大等于直接放大账单。

    实测（2026-09-12 修前）：成本表缺 api 档分支 → fallthrough 到 1.0 →
    exa/octen/you/parallel/zhihu_global/tavily 这类按量计费的源被当成免费，
    请求 5 条被放大到 10 条。
    """

    def test_only_free_tier_gets_bucketed(self):
        from config import cost_tier_of, get_cost_tiers
        from engines import bucket_n
        tiers = get_cost_tiers()
        free = sorted(tiers.get("free") or [])
        metered = sorted((tiers.get("low") or []) + (tiers.get("api") or [])
                         + (tiers.get("paid") or []))
        assert free and metered, "取样失败：档位表为空"
        assert bucket_n(free[0], 5) >= 5          # 免费档允许向上取桶
        for name in metered[:8]:
            assert bucket_n(name, 5) == 5, \
                f"{name}（{cost_tier_of(name)} 档）被桶化放大——按量计费不得放大"

    def test_api_tier_is_not_treated_as_free(self):
        from config import cost_tier_of
        from engines import _free_engine
        checked = 0
        for name in ("exa", "octen", "tavily", "you", "parallel", "zhihu_global"):
            if cost_tier_of(name) != "api":
                continue
            checked += 1
            assert not _free_engine(name), f"{name} 是 api 档却被当成免费源"
        assert checked, "没有任何 api 档引擎可校验，检查档位声明"
