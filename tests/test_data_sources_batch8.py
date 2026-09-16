#!/usr/bin/env python3
"""test_data_sources_batch8.py — 批次八数据源引擎离线回归测试。

覆盖（全部 mock 网络层，不真实请求）：
  std_samr      高亮标签剥离 + 标题拼装 + hcno 直通链接
  openstd       行内容特征定位（标准号模式/日期/状态词表）+ onclick hcno
  bangumi       name_cn→name 回落 + 评分/平台拼接 + POST 请求体
  douban_movie  suggest 解析 + 类型中文化
  zdic          释义抽取 + 单条结果语义
  people_daily  <em> 剥离 + 毫秒时间戳转日期 + POST 请求体
  flk_law       orderByParam 对象体（扁平字符串会 500）+ 高亮剥离 + sxx 映射
  wikisource    标题空格编码拼 wiki 路径 + searchmatch 剥离
  google_news   RSS 2.0 解析 + 「 - 媒体名」短尾巴剥离 + 语言整组切换
  met_museum    两跳（objectIDs → objects/{id}）+ 空署名保底
  stackoverflow site: 前缀切站点族 + 前缀剥离
  http_client   Location 未编码中文重定向修复（latin-1 还原 + 补编码）
  声明式         crt_sh 顶层数组 / nasa_images 嵌套 url_template / zhihu_hot_app 无查询参数
"""

import json
import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import engines_base  # noqa: E402
from engines_base import _parse_http_payload  # noqa: E402
from engines_builders_cn import (  # noqa: E402
    _build_bangumi_engine,
    _build_douban_movie_engine,
    _build_flk_law_engine,
    _build_openstd_engine,
    _build_people_daily_engine,
    _build_std_samr_engine,
    _build_wikisource_engine,
    _build_zdic_engine,
)
from engines_builders_intl import (  # noqa: E402
    _build_google_news_engine,
    _build_met_museum_engine,
)
from engines_builders_tech import _build_stackoverflow_engine  # noqa: E402


import engines_builders_cn  # noqa: E402
import engines_builders_intl  # noqa: E402
import engines_builders_tech  # noqa: E402


def _mock_http(monkeypatch, url_marker: str, payload):
    """mock _http_get_raw：URL 含 marker 时返回 payload。

    builder 顶部 `from engines_base import _http_get_raw` 是导入期绑定，
    engines_base 与各 builder 模块的引用一并 patch 才能拦住真实请求。
    """
    def fake_raw(url, headers, timeout, engine="?"):
        if url_marker in url:
            if isinstance(payload, str):
                return payload
            return json.dumps(payload)
        return None
    for mod in (engines_base, engines_builders_cn, engines_builders_intl):
        monkeypatch.setattr(mod, "_http_get_raw", fake_raw, raising=True)


def _mock_open(monkeypatch, builder_module, handler):
    """mock 指定 builder 模块的 http_open：handler(req, timeout, engine) 返回响应。"""
    monkeypatch.setattr(builder_module, "http_open", handler, raising=True)


class TestStdSamr:
    @pytest.fixture
    def engine(self):
        return _build_std_samr_engine({"_name": "std_samr", "timeout": 2})

    def test_parse_and_hcno_link(self, engine, monkeypatch):
        _mock_http(monkeypatch, "std.samr.gov.cn", {"rows": [
            {"id": "33D40F1161195D92E06397BE0A0A5B93",
             "C_C_NAME": "<sacinfo>数据安全</sacinfo>技术 数据安全风险评估方法",
             "C_STD_CODE": "GB/T 45577-2025", "STD_NATURE": "推荐性",
             "ACT_DATE": "2025-11-01", "STATE": "现行", "ISSUE_DATE": "2025-04-25"},
        ]})
        out = engine("数据安全")
        assert len(out) == 1
        r = out[0]
        assert r["source"] == "std_samr"
        assert "sacinfo" not in r["title"]
        assert r["title"] == "GB/T 45577-2025 数据安全技术 数据安全风险评估方法"
        assert r["url"] == "https://openstd.samr.gov.cn/bzgk/gb/newGbInfo?hcno=33D40F1161195D92E06397BE0A0A5B93"
        assert "推荐性" in r["snippet"] and "现行" in r["snippet"]

    def test_empty_rows(self, engine, monkeypatch):
        _mock_http(monkeypatch, "std.samr.gov.cn", {"rows": []})
        assert engine("不存在的标准xyz") == []


class TestOpenstd:
    @pytest.fixture
    def engine(self):
        return _build_openstd_engine({"_name": "openstd", "timeout": 2})

    _HTML = """<table><tr><td>1</td><td>GB/T 47469-2026</td><td></td><td></td>
<td>数据安全技术 移动智能终端的移动互联网应用程序（App）个人信息处理活动</td>
<td>推标</td><td>即将实施</td><td>2026-04-30 00:00:00.0</td>
<td>2026-11-01 00:00:00.0</td>
<td><button onclick="showInfo('A3323548957FF3B39553E738E25EB553');">查看详细</button></td></tr></table>"""

    def test_row_parse(self, engine, monkeypatch):
        _mock_http(monkeypatch, "openstd.samr.gov.cn", self._HTML)
        out = engine("数据安全")
        assert len(out) == 1
        r = out[0]
        assert r["title"].startswith("GB/T 47469-2026")
        assert "个人信息处理活动" in r["title"]
        assert r["url"].endswith("hcno=A3323548957FF3B39553E738E25EB553")
        assert "推标" in r["snippet"] and "即将实施" in r["snippet"]

    def test_tail_word_stripped(self, engine, monkeypatch):
        captured = {}

        def fake_raw(url, headers, timeout, engine="?"):
            captured["url"] = url
            return self._HTML
        for mod in (engines_base, engines_builders_cn):
            monkeypatch.setattr(mod, "_http_get_raw", fake_raw, raising=True)
        engine("数据安全标准")
        assert "p.p2=" in captured["url"]


class TestBangumi:
    @pytest.fixture
    def engine(self):
        return _build_bangumi_engine({"_name": "bangumi", "timeout": 2})

    def test_post_body_and_parse(self, engine, monkeypatch):
        captured = {}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"data": [
                    {"id": 241088, "name": "ぼっち・ざ・ろっく！", "name_cn": "孤独摇滚！",
                     "date": "2019-02-27", "platform": "漫画",
                     "rating": {"score": 7.4}, "summary": "乐队……那是阴暗角色也能闪耀起来的唯一地方。"},
                ]}).encode("utf-8")

        def fake_open(req, timeout, engine="?"):
            captured["url"] = req.full_url
            captured["body"] = req.data
            return FakeResp()
        _mock_open(monkeypatch, engines_builders_cn, fake_open)
        out = engine("孤独摇滚")
        assert "api.bgm.tv/v0/search/subjects" in captured["url"]
        assert json.loads(captured["body"])["keyword"] == "孤独摇滚"
        assert len(out) == 1
        r = out[0]
        assert r["title"] == "孤独摇滚！"
        assert r["url"] == "https://bgm.tv/subject/241088"
        assert "漫画" in r["snippet"] and "评分 7.4" in r["snippet"]

    def test_name_cn_fallback_to_name(self, engine, monkeypatch):
        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"data": [
                    {"id": 1, "name": "Mushoku Tensei", "name_cn": "",
                     "platform": "小说", "date": "2012-01-01", "rating": None},
                ]}).encode("utf-8")

        def fake_open(req, timeout, engine="?"):
            return FakeResp()
        _mock_open(monkeypatch, engines_builders_cn, fake_open)
        out = engine("mushoku")
        assert out[0]["title"] == "Mushoku Tensei"


class TestDoubanMovie:
    @pytest.fixture
    def engine(self):
        return _build_douban_movie_engine({"_name": "douban_movie", "timeout": 2})

    def test_suggest_parse(self, engine, monkeypatch):
        _mock_http(monkeypatch, "movie.douban.com", [
            {"title": "流浪地球", "year": "2019", "type": "movie",
             "url": "https://movie.douban.com/subject/26266893/", "id": "26266893"},
        ])
        out = engine("流浪地球")
        assert len(out) == 1
        r = out[0]
        assert r["title"] == "流浪地球"
        assert "2019" in r["snippet"] and "电影" in r["snippet"]
        assert r["url"] == "https://movie.douban.com/subject/26266893/"

    def test_non_list_returns_empty(self, engine, monkeypatch):
        _mock_http(monkeypatch, "movie.douban.com", {"err": "blocked"})
        assert engine("x") == []


class TestZdic:
    @pytest.fixture
    def engine(self):
        return _build_zdic_engine({"_name": "zdic", "timeout": 2})

    def test_defs_extracted(self, engine, monkeypatch):
        _mock_http(monkeypatch, "zdic.net", """<html><title>道 dào - 汉典</title>
<div class="xxjs-item__def">道德，道义、正义 <span class="encs">[morals]</span></div>
<div class="xxjs-item__def">供行走的道路</div></html>""")
        out = engine("道")
        assert len(out) == 1
        r = out[0]
        assert r["title"] == "道 dào"
        assert "释义：" in r["snippet"]
        assert "道义、正义" in r["snippet"] and "encs" not in r["snippet"]

    def test_no_defs_honest_empty(self, engine, monkeypatch):
        _mock_http(monkeypatch, "zdic.net", "<html><title>404</title></html>")
        assert engine("qqzzxx") == []


class TestPeopleDaily:
    @pytest.fixture
    def engine(self):
        return _build_people_daily_engine({"_name": "people_daily", "timeout": 2})

    def test_post_body_parse(self, engine, monkeypatch):
        captured = {}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"code": "0", "data": {"records": [
                    {"title": "2026世界<em>人工智能</em>大会举办",
                     "content": "人民网北京7月20日电 <em>人工智能</em>分会于7月19日在上海召开",
                     "url": "http://society.people.com.cn/n1/2026/0720/c1008-40764224.html",
                     "displayTime": "1784514340000"},
                ]}}).encode("utf-8")

        def fake_open(req, timeout, engine="?"):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data)
            return FakeResp()
        _mock_open(monkeypatch, engines_builders_cn, fake_open)
        out = engine("人工智能")
        assert captured["url"].startswith("http://search.people.cn/")
        assert captured["body"]["key"] == "人工智能"
        assert captured["body"]["hasTitle"] is True
        assert len(out) == 1
        r = out[0]
        assert "<em>" not in r["title"]
        assert r["title"] == "2026世界人工智能大会举办"
        assert r["published_at"].startswith("2026-")
        assert r["url"].startswith("http://society.people.com.cn/")


class TestFlkLaw:
    @pytest.fixture
    def engine(self):
        return _build_flk_law_engine({"_name": "flk_law", "timeout": 2})

    def test_nested_orderby_body_and_parse(self, engine, monkeypatch):
        captured = {}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"code": 200, "total": 474, "rows": [
                    {"bbbs": "ff8080817703add20177373df6a43e33",
                     "title": "中华人民共和国<em class='highlight'>行政处罚</em>法",
                     "gbrq": "2021-01-22", "sxrq": "2021-07-15", "sxx": 3,
                     "zdjgName": "全国人民代表大会", "flxz": "法律"},
                    {"bbbs": "abc", "title": "旧版（已废止）", "sxx": 1, "flxz": "法律"},
                ]}).encode("utf-8")

        def fake_open(req, timeout, engine="?"):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data)
            return FakeResp()
        _mock_open(monkeypatch, engines_builders_cn, fake_open)
        out = engine("行政处罚")
        assert captured["url"] == "https://flk.npc.gov.cn/law-search/search/list"
        # orderByParam 必须是对象——扁平字符串后端 500
        assert captured["body"]["orderByParam"] == {"order": "-1", "sort": ""}
        assert captured["body"]["searchContent"] == "行政处罚"
        assert captured["body"]["searchType"] == 2
        assert len(out) == 2
        r = out[0]
        assert "<em" not in r["title"]
        assert r["title"] == "中华人民共和国行政处罚法"
        assert r["url"] == "https://flk.npc.gov.cn/detail?id=ff8080817703add20177373df6a43e33"
        assert "法律" in r["snippet"] and "有效" in r["snippet"]
        assert "公布 2021-01-22" in r["snippet"] and "施行 2021-07-15" in r["snippet"]
        assert out[1]["snippet"].find("已废止") >= 0
        assert out[1]["published_at"] == ""

    def test_code_500_honest_empty(self, engine, monkeypatch):
        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"msg": "系统异常", "code": 500}).encode("utf-8")

        def fake_open(req, timeout, engine="?"):
            return FakeResp()
        _mock_open(monkeypatch, engines_builders_cn, fake_open)
        assert engine("行政处罚") == []


class TestWikisource:
    @pytest.fixture
    def engine(self):
        return _build_wikisource_engine({"_name": "wikisource", "timeout": 2})

    def test_title_encoded_and_snippet_stripped(self, engine, monkeypatch):
        _mock_http(monkeypatch, "zh.wikisource.org", {"query": {"search": [
            {"title": "出師表", "snippet": '受命以來，<span class="searchmatch">夙夜</span>憂嘆',
             "timestamp": "2024-01-01T00:00:00Z"},
            {"title": "出师表 (诸葛亮)", "snippet": "先帝创业未半"},
        ]}})
        out = engine("出师表")
        assert len(out) == 2
        r = out[0]
        import urllib.parse as _up
        assert r["url"] == "https://zh.wikisource.org/wiki/" + _up.quote("出師表".replace(" ", "_"))
        assert "searchmatch" not in r["snippet"] and "夙夜" in r["snippet"]
        # 标题含空格 → wiki 路径下划线编码
        assert out[1]["url"].endswith("/wiki/" + _up.quote("出师表_(诸葛亮)".replace(" ", "_")))


class TestGoogleNews:
    @pytest.fixture
    def engine(self):
        return _build_google_news_engine({"_name": "google_news", "timeout": 2})

    _RSS = """<rss version="2.0"><channel>
<item><title>AI could kill all humans - The Inquirer</title>
<link>https://news.google.com/rss/articles/abc1</link><pubDate>Tue, 09 Sep 2026 10:00:00 GMT</pubDate></item>
<item><title>量子计算新突破 - 科技日报</title>
<link>https://news.google.com/rss/articles/abc2</link><pubDate>Tue, 09 Sep 2026 11:00:00 GMT</pubDate></item>
</channel></rss>"""

    def test_rss_parse_and_tail_strip(self, engine, monkeypatch):
        captured = {}

        def fake_raw(url, headers, timeout, engine="?"):
            captured["url"] = url
            return self._RSS
        for mod in (engines_base, engines_builders_intl):
            monkeypatch.setattr(mod, "_http_get_raw", fake_raw, raising=True)
        out = engine("artificial intelligence")
        assert len(out) == 2
        # 英文查询 → en-US 组
        assert "hl=en-US" in captured["url"] and "ceid=US:en" in captured["url"]
        # 「 - 媒体名」短尾巴剥离（中英同规则）
        assert out[0]["title"] == "AI could kill all humans"
        assert out[1]["title"] == "量子计算新突破"
        assert out[0]["published_at"].startswith("Tue, 09 Sep 2026")

    def test_chinese_query_locale(self, engine, monkeypatch):
        captured = {}

        def fake_raw(url, headers, timeout, engine="?"):
            captured["url"] = url
            return self._RSS
        for mod in (engines_base, engines_builders_intl):
            monkeypatch.setattr(mod, "_http_get_raw", fake_raw, raising=True)
        engine("人工智能")
        assert "hl=zh-CN" in captured["url"] and "gl=CN" in captured["url"]


class TestMetMuseum:
    @pytest.fixture
    def engine(self):
        return _build_met_museum_engine({"_name": "met_museum", "timeout": 5})

    def test_two_hop(self, engine, monkeypatch):
        calls = []

        def fake_raw(url, headers, timeout, engine="?"):
            calls.append(url)
            if "/search?" in url:
                return json.dumps({"total": 87, "objectIDs": [436535, 436533, None]})
            if "/objects/436535" in url:
                return json.dumps({"title": "Wheat Field with Cypresses",
                                   "artistDisplayName": "Vincent van Gogh",
                                   "objectDate": "1889", "medium": "Oil on canvas",
                                   "department": "European Paintings",
                                   "objectURL": "https://www.metmuseum.org/art/collection/search/436535"})
            if "/objects/436533" in url:
                return json.dumps({"title": "Cypresses", "artistDisplayName": "",
                                   "objectDate": "1889", "objectURL": "https://www.metmuseum.org/art/collection/search/436533"})
            return None
        for mod in (engines_base, engines_builders_intl):
            monkeypatch.setattr(mod, "_http_get_raw", fake_raw, raising=True)
        out = engine("van gogh")
        assert any("/search?" in c and "hasImages=true" in c for c in calls)
        assert len(out) == 2
        assert "Vincent van Gogh" in out[0]["snippet"]
        # 空署名保底：标题仍是作品名
        assert out[1]["title"] == "Cypresses"


class TestStackexchangeFamily:
    @pytest.fixture
    def engine(self):
        return _build_stackoverflow_engine({"_name": "stackoverflow", "timeout": 2})

    def test_site_prefix_switches_site(self, engine, monkeypatch):
        captured = {}

        def fake_open(req, timeout, engine="?"):
            captured["url"] = req.full_url

            class R:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return json.dumps({"items": [
                        {"title": "nginx worker_connections", "link": "https://serverfault.com/q/1",
                         "score": 69, "answer_count": 5, "tags": ["nginx", "connection"]}]})
            return R()
        _mock_open(monkeypatch, engines_builders_tech, fake_open)
        out = engine("site:serverfault nginx worker_connections")
        assert "site=serverfault" in captured["url"]
        assert "q=nginx+worker_connections" in captured["url"] or "q=nginx%20worker_connections" in captured["url"]
        assert out[0]["snippet"].startswith("[serverfault]")

    def test_default_site_untouched(self, engine, monkeypatch):
        captured = {}

        def fake_open(req, timeout, engine="?"):
            captured["url"] = req.full_url

            class R:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return json.dumps({"items": []})
            return R()
        _mock_open(monkeypatch, engines_builders_tech, fake_open)
        engine("python async")
        assert "site=stackoverflow" in captured["url"]
        assert "site%3A" not in captured["url"]


class TestDeclarativeSpecs:
    """批次八声明式 spec：crt_sh 顶层数组 / nasa_images 嵌套 / zhihu_hot_app 无查询参数。"""

    def test_crt_sh_top_level_array(self):
        spec = {"_name": "crt_sh", "output_map": {
            "items": ".", "item_title": "common_name", "item_summary": "issuer_name",
            "item_published_at": "not_before", "url_template": "https://crt.sh/?id={id}",
        }}
        data = [{"issuer_ca_id": 413868, "id": 28361996564, "issuer_name": "Cloudflare TLS ECC CA",
                 "common_name": "example.com", "name_value": "example.com", "not_before": "2026-01-01T00:00:00"}]
        out = _parse_http_payload(json.dumps(data), "", "crt_sh", 5, spec["output_map"], spec)
        assert len(out) == 1
        r = out[0]
        assert r["title"] == "example.com"
        assert r["url"] == "https://crt.sh/?id=28361996564"
        assert r["published_at"] == "2026-01-01T00:00:00"

    def test_nasa_images_nested(self):
        spec = {"_name": "nasa_images", "output_map": {
            "items": "collection.items", "item_title": "data.0.title",
            "item_summary": "data.0.description", "item_published_at": "data.0.date_created",
            "url_template": "https://images.nasa.gov/details/{data.0.nasa_id}",
        }}
        data = {"collection": {"items": [
            {"href": "x", "data": [{"nasa_id": "jsc2007e034221", "title": "Apollo 11 pre-launch",
                                    "description": "Personnel atop the MSS", "date_created": "1969-07-11T00:00:00Z"}],
             "links": []}]}}
        out = _parse_http_payload(json.dumps(data), "", "nasa_images", 5, spec["output_map"], spec)
        assert len(out) == 1
        r = out[0]
        assert r["title"] == "Apollo 11 pre-launch"
        assert r["url"] == "https://images.nasa.gov/details/jsc2007e034221"
        assert r["published_at"].startswith("1969-07-11")

    def test_zhihu_hot_app(self):
        spec = {"_name": "zhihu_hot_app", "output_map": {
            "items": "data", "item_title": "target.title", "item_summary": "detail_text",
            "url_template": "https://www.zhihu.com/question/{target.id}",
        }}
        data = {"data": [
            {"type": "hot_list_feed", "target": {"id": 2082159487167587181,
             "title": "多车队宣布永久退出China GT", "type": "question"},
             "detail_text": "513 万热度"}]}
        out = _parse_http_payload(json.dumps(data), "", "zhihu_hot_app", 5, spec["output_map"], spec)
        assert len(out) == 1
        r = out[0]
        assert r["title"] == "多车队宣布永久退出China GT"
        assert r["url"] == "https://www.zhihu.com/question/2082159487167587181"
        assert "513" in r["snippet"]


class TestHttpClientLocationFix:
    """Location 未编码中文重定向：latin-1 还原 + 非 ASCII 补编码。"""

    def test_unquoted_cjk_location(self, monkeypatch):
        import http.client as hc
        import urllib.parse as _up

        # 出口调度隔离（issue #13 配套）：本测试在连接类层面 mock，而
        # net_proxy 的标准环境变量层会让真实代理环境（本机 shell 预置
        # http_proxy 等）走隧道分支，绕开 FakeConn。测试环境强制直连。
        monkeypatch.setattr("net_proxy.resolve_proxy", lambda *a, **k: None)

        seen_paths = []

        class FakeConn:
            def __init__(self, *a, **k):
                pass

            def request(self, method, path, headers=None, **k):
                seen_paths.append(path)

            def getresponse(self):
                if len(seen_paths) == 1:
                    return _FakeResp(301, {"Location": "https://zdic.net/hans/道"})
                return _FakeResp(200, {}, b"ok")

            def close(self):
                pass

        class _FakeResp:
            def __init__(self, status, headers, body=b""):
                self.status = status
                self._headers = headers
                self._body = body

            def getheaders(self):
                return list(self._headers.items())

            def getheader(self, name, default=None):
                return self._headers.get(name, default)

            def read(self):
                return self._body

        from http_client import HttpClient
        monkeypatch.setattr(hc, "HTTPSConnection", FakeConn, raising=True)
        c = HttpClient()
        resp = c._do_get("https://www.zdic.net/hans/%E9%81%93", {"User-Agent": "t"}, True)
        assert resp["status"] == 200
        # 第二跳请求行是补编码后的 path（无裸 CJK）
        assert seen_paths[1] == "/hans/%E9%81%93"
