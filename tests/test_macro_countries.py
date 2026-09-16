#!/usr/bin/env python3
"""macro_countries 的国家词判定 + 路由层的导入隔离回归护栏。

## 为什么需要它

两件事各自出过问题：

1. **判定有两套实现**。`is_foreign_macro_query` 用朴素子串（`name in query`），
   而同一模块的 `_match_country` 对 ≤3 字符别名做了词边界保护。后果是 `us`
   命中任何含该子串的英文词——`focus on growth`、`consensus 预测`、
   `business survey`、`status report`、`thus far` 全被判成「外国宏观查询」，
   FRED 被无谓排除；更糟的是 `us inflation` / `usa gdp` 这类**真正的美国查询**
   也被判成外国（旧实现把 `us` 当外国别名匹配到了），方向完全反了。
   现在两者共用 `match_country`，一张表 + 一套匹配规则。

2. **路由层被迫背 HTTP 栈**。这张表和两个纯文本谓词原住在
   `engines_builders_data_macro.py`，而那个模块连带来 `engines_base` →
   `http_client` → urllib/ssl/cookiejar。`route.py` 为了调一个 8 行谓词在模块
   顶层 import 它——实测路由因此多付 ~33 ms，而路由是**每次调用**的必经路径
   （缓存命中也要走），HTTP 栈却只有真正打网才需要。拆到本模块后
   `import route` 不再加载 `http_client`；下面用子进程 `sys.modules` 断言把它
   钉住，防止哪天有人又把重依赖引回这条路径。

运行：
  python3 -m pytest tests/test_macro_countries.py -v
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = ROOT / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


@pytest.fixture(scope="module")
def mc():
    import macro_countries
    return macro_countries


class TestForeignMacroQuery:
    """方向不能反：问的是「是否指向**外国**」，美国查询必须返回 False。"""

    @pytest.mark.parametrize("query", [
        "中国GDP", "日本通胀", "德国制造业", "英国失业率", "印度 通胀",
        "uk gdp", "china inflation",
    ])
    def test_foreign_queries_yield(self, mc, query):
        assert mc.is_foreign_macro_query(query) is True, \
            f"{query!r} 指向外国，应让位给 worldbank"

    @pytest.mark.parametrize("query", [
        "美国GDP", "usa gdp", "us inflation", "united states cpi",
    ])
    def test_us_queries_do_not_yield(self, mc, query):
        assert mc.is_foreign_macro_query(query) is False, \
            f"{query!r} 是美国查询，FRED 正是该应答的源，不该让位"

    @pytest.mark.parametrize("query", [
        # 都含 "us" 子串，但都不是美国：词边界必须挡住
        "focus on growth", "consensus 预测", "business survey",
        "status report", "thus far", "just in time", "user retention",
    ])
    def test_short_alias_needs_word_boundary(self, mc, query):
        assert mc.is_foreign_macro_query(query) is False, (
            f"{query!r} 只是含 us 子串，不是国家词——朴素子串匹配会把 FRED "
            "从这些查询里错误排除")

    def test_wildcard_regions_still_yield(self, mc):
        """世界/全球口径：worldbank 同样覆盖，保持既有让位行为不变。"""
        for q in ("world bank data", "global inflation", "欧元区 通胀"):
            assert mc.is_foreign_macro_query(q) is True, q


class TestSingleDefinition:
    """一张表、一套匹配规则——不再有第二份行为不同的实现。"""

    def test_macro_builder_reexports_same_object(self, mc):
        import engines_builders_data_macro as macro
        assert macro.is_foreign_macro_query is mc.is_foreign_macro_query, \
            "宏观构建器应转出同一个函数对象，而不是自己再定义一份"
        assert macro.WORLDBANK_COUNTRIES is mc.WORLDBANK_COUNTRIES, \
            "国家表必须同源，否则两张表会各自漂移"

    def test_route_uses_same_object(self, mc):
        import route
        assert route.is_foreign_macro_query is mc.is_foreign_macro_query

    def test_non_us_table_is_derived(self, mc):
        """非美国子集从主表派生，不另抄一份。"""
        assert "us" not in mc._NON_US_COUNTRIES
        assert "usa" not in mc._NON_US_COUNTRIES
        assert "美国" not in mc._NON_US_COUNTRIES
        assert "中国" in mc._NON_US_COUNTRIES


class TestRoutingPathStaysLight:
    """路由是每次调用的必经路径，不得再背 HTTP 栈。"""

    _CODE = (
        "import sys; sys.path.insert(0, {scripts!r});"
        "import route;"
        "heavy=[m for m in ('http_client','engines_base',"
        "'engines_builders_data_macro','urllib.request','ssl') if m in sys.modules];"
        "print('HEAVY:'+','.join(heavy))"
    )

    def test_import_route_does_not_pull_http_stack(self):
        r = subprocess.run(
            [sys.executable, "-c", self._CODE.format(scripts=str(SCRIPT_DIR))],
            capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr[-400:]
        heavy = r.stdout.strip().removeprefix("HEAVY:")
        assert heavy == "", (
            "import route 又拉起了重依赖（%s）——路由是每次调用的必经路径，"
            "缓存命中也要走一遍；国家词这类纯文本判定请留在 macro_countries"
            % heavy)

    def test_macro_countries_is_dependency_free(self):
        """本模块必须保持零重依赖——它能被路由直接引用的前提。"""
        r = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, %r);"
             "import macro_countries" % str(SCRIPT_DIR)],
            capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr[-400:]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
