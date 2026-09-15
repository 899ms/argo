#!/usr/bin/env python3
"""test_lowlevel_smoke.py — 曾以 NameError 形式全灭的三条链路的冒烟门。

## 守的是什么

三处「名字没接上」的同类事故，症状各不相同但都绕过了既有测试：
  1. `job.py` 五个检索后端调 `get_env(...)` 却没 import——付费后端逐个报
     `name 'get_env' is not defined`（byted 被 `except Exception: continue`
     静默吞掉），`argo job` 恒 total: 0。既有 job 测试只覆盖地区分级/排序，
     从不真调后端；
  2. `engines_builders_data_macro.py` 的 FRED 引擎用 `rank_score(...)` 却没
     import——构建器内 `safe_search` 吞掉 NameError，引擎静默恒空；
  3. `article.py` 人类可读输出分支引用了 `fetch_article()` 的内部局部名
     `text`/`imgs`——不带 `--json` 必崩。

本文件给三条链路各补一条最小冒烟：后端/构建器用 mock 走到字段映射那行，
断言结果非空且字段落位。名字没接上时这些断言必然失败。
全部 mock，不发真实请求。
"""

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import job  # noqa: E402


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self, *a):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestJobBackends:
    """五个 _search_* 后端的最小冒烟：mock _post，走到字段映射不 NameError。"""

    def _run(self, monkeypatch, fn, payload):
        monkeypatch.setattr(job, "_post", lambda *a, **k: payload)
        return fn("测试查询", 3)

    def test_exa(self, monkeypatch):
        out = self._run(monkeypatch, job._search_exa, {
            "results": [{"title": "T1", "url": "https://u1", "text": "正文",
                         "publishedDate": "2026-09-01"}]})
        assert len(out) == 1
        assert out[0]["title"] == "T1" and out[0]["url"] == "https://u1"
        assert out[0]["snippet"] == "正文" and out[0]["date"] == "2026-09-01"

    def test_tavily(self, monkeypatch):
        out = self._run(monkeypatch, job._search_tavily, {
            "results": [{"title": "T2", "url": "https://u2", "content": "内容"}]})
        assert out and out[0]["snippet"] == "内容"

    def test_byted(self, monkeypatch):
        out = self._run(monkeypatch, job._search_byted, {
            "Result": {"WebResults": [{"Title": "T3", "Url": "https://u3",
                                       "Snippet": "摘要"}]}})
        # byted 的单平台失败被 except 吞掉——本断言曾因 get_env NameError
        # 被静默吞而恒空，是唯一能抓住该后端静默死亡的形态
        assert out and out[0]["url"] == "https://u3"

    def test_bocha(self, monkeypatch):
        out = self._run(monkeypatch, job._search_bocha, {
            "data": {"webPages": {"value": [
                {"name": "T4", "url": "https://u4", "summary": "s4",
                 "datePublished": "2026-09-02"}]}}})
        assert out and out[0]["date"] == "2026-09-02"

    def test_octen(self, monkeypatch):
        out = self._run(monkeypatch, job._search_octen, {
            "data": {"results": [{"title": "T5", "url": "https://u5",
                                  "highlight": "h5"}]}})
        assert out and out[0]["snippet"] == "h5"


class TestFredBuilder:
    """FRED 构建器冒烟：mock http_open 回 CSV，结果非空且分数/趋势落位。"""

    def test_fred_maps_rows(self, monkeypatch):
        import engines_builders_data_macro as macro

        csv = "observation_date,CPIAUCSL\n2026-06-01,322.1\n2026-07-01,322.9\n"
        monkeypatch.setattr(
            macro, "http_open",
            lambda req, timeout=None, engine="": _FakeResp(csv.encode("utf-8")))
        eng = macro._build_fred_engine({"_name": "fred"})
        out = eng("美国通胀最新数据")
        assert out, "FRED 曾因 rank_score 未 import 而静默恒空"
        assert out[0]["url"].startswith("https://fred.stlouisfed.org/series/CPIAUCSL")
        assert isinstance(out[0]["score"], float)
        # 首条附趋势方向：末值 322.9 > 前值 322.1 → ↑
        assert "↑" in out[0]["snippet"]


class TestArticleHumanOutput:
    """article 非 JSON 分支曾引用 fetch_article 内部局部名，必崩 NameError。"""

    def test_human_output(self, monkeypatch, capsys):
        import article

        monkeypatch.setattr(article, "fetch_article", lambda url: {
            "ok": True, "title": "标题", "author": "作者",
            "publish_time": "2026-09-15", "char_count": 4,
            "image_count": 1, "images": ["https://img"], "content": "正文",
        })
        monkeypatch.setattr(article.sys, "argv", ["article.py", "https://x"])
        article.main()
        text = capsys.readouterr().out
        assert "标题: 标题" in text and "正文" in text
        assert "[图1] https://img" in text


class TestMinhashDataRows:
    """数据行 vs 同质网页的分流：近重复折叠不得吃掉观测期逐行条目。

    fred 构建器注释「url 带日期锚点防去重合并」的意图此前只存在于注释里：
    5 期观测文本仅差日期与数值，minhash 相似度恒过阈值，实测 5 条被折叠成
    1 条且幸存的是最旧一期。修法 = 同文档路径、查询参数不同者视为不同内容行。
    """

    @staticmethod
    def _series_rows() -> list[dict]:
        rows = [("2026-04-01", 332.407), ("2026-05-01", 333.001),
                ("2026-06-01", 332.568), ("2026-07-01", 332.813),
                ("2026-08-01", 334.131)]
        return [
            {"title": f"美国CPI · {d} = {v}",
             "url": f"https://fred.stlouisfed.org/series/CPIAUCSL?obs={d}",
             "snippet": f"FRED CPIAUCSL 最新值 {v:g}（截至 {d}）",
             "source": "fred", "score": 0.9 - i * 0.01}
            for i, (d, v) in enumerate(rows)
        ]

    def test_series_rows_survive(self):
        from search import minhash_dedupe
        kept, removed = minhash_dedupe(self._series_rows())
        assert len(kept) == 5 and removed == 0

    def test_syndicated_pages_still_collapse(self):
        from search import minhash_dedupe
        body = "同一篇报道的完整正文内容，用于验证跨站同质网页仍会被折叠。"
        rows = [
            {"title": "报道标题", "url": "https://news-a.example.com/p/123",
             "snippet": body, "source": "a", "score": 0.9},
            {"title": "报道标题", "url": "https://news-b.example.com/x/456",
             "snippet": body, "source": "b", "score": 0.8},
        ]
        kept, removed = minhash_dedupe(rows)
        assert len(kept) == 1 and removed == 1


class TestCommonFlagsContract:
    """Usage 文本把 --json 列为 Common flags，各子命令解析器就得真认。

    fetch 曾对 --json 报 unrecognized arguments——契约写在门面上、实现不认，
    Agent 按文档传参必撞墙。
    """

    def test_fetch_accepts_json(self):
        import fetch_v3
        args = fetch_v3.build_parser().parse_args(["https://x", "--json"])
        assert args.json is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
