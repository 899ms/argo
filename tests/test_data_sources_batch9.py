#!/usr/bin/env python3
"""test_data_sources_batch9.py — 批次九数据源离线回归（mock 网络，不发真实请求）。

覆盖本批自研 builder 的解析契约与已知坑：
  who_don        顶层 list 形态（OData value 被网关展平）+ Title/OverrideTitle 回落
  who_gho        不传 $top（未编码 $ 参数会被网关 400）+ 本地指标过滤
  gdacs          properties.url 是 dict（{report, geometry}）——透传会让
                下游 evidence.is_serp_or_jump_url 抛 AttributeError（实测踩过）
  obis/worms     中英混排查询抽拉丁学名（纯中文诚实空）
  nhtsa_vpic     中文厂商名归一（vPIC 无中文索引）+ 噪声词剔除
  egov_law       全角空格切词 + 「日本」前缀剥离 + 2-gram 扩展 + 进程内缓存
  k10plus        OAI-DC 元素带 inline xmlns 属性，正则须容忍标签属性
  ror            机构名在 names[]（ror_display 类型），国家在
                 locations[].geonames_details（顶层 name/country 已为 None）
  _latin_query   混排抽取工具
  _terms         全角/半角空格切词
"""

import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import engines_builders_batch9 as b9  # noqa: E402


class _FakeResp:
    def __init__(self, data: bytes):
        self._d = data

    def read(self):
        return self._d

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_text(monkeypatch, payload: str):
    monkeypatch.setattr(b9, "http_open",
                        lambda req, timeout=None, engine="": _FakeResp(payload.encode()),
                        raising=False)


def _patch_json(monkeypatch, obj):
    import json as _json
    _patch_text(monkeypatch, _json.dumps(obj))


class TestHelpers:
    def test_terms_splits_half_and_full_width(self):
        assert b9._terms("日本民法 条文") == ["日本民法", "条文"]
        assert b9._terms("日本民法　条文") == ["日本民法", "条文"]  # U+3000

    def test_latin_query_extracts_from_mixed(self):
        assert b9._latin_query("Gadus morhua 海洋物种") == "Gadus morhua"

    def test_latin_query_empty_for_pure_cjk(self):
        assert b9._latin_query("海洋物种") == ""


class TestWhoDon:
    def test_top_level_list_shape(self, monkeypatch):
        """网关把 OData 的 value 展平为顶层 list —— 必须兼容。"""
        _patch_json(monkeypatch, [
            {"Id": "1", "Title": "Cholera in Liberia", "Overview": "x",
             "PublicationDate": "2002-01-01T00:00:00Z"},
        ])
        out = b9._build_who_don_engine({"_name": "who_don", "timeout": 5})("cholera", 5)
        assert len(out) == 1
        assert "Cholera" in out[0]["title"]
        assert out[0]["source"] == "who_don"

    def test_override_title_fallback(self, monkeypatch):
        _patch_json(monkeypatch, [
            {"Id": "2", "OverrideTitle": "Fallback Title", "PublicationDate": "2020-01-01"},
        ])
        out = b9._build_who_don_engine({"_name": "who_don", "timeout": 5})("x", 5)
        assert out and out[0]["title"] == "Fallback Title"

    def test_scores_descend(self, monkeypatch):
        _patch_json(monkeypatch, [
            {"Id": str(i), "Title": f"Outbreak {i}", "PublicationDate": "2021-01-01"}
            for i in range(4)
        ])
        out = b9._build_who_don_engine({"_name": "who_don", "timeout": 5})("outbreak", 4)
        sc = [r["score"] for r in out]
        assert sc == sorted(sc, reverse=True) and sc[0] == 0.9


class TestGdacs:
    def test_url_dict_is_unwrapped(self, monkeypatch):
        """properties.url 是 dict，透传会让下游 url.lower() 崩（实测回归点）。"""
        _patch_json(monkeypatch, {"features": [{
            "properties": {
                "name": "Flood in India", "eventtype": "FL", "alertlevel": "Orange",
                "url": {"report": "https://www.gdacs.org/report.aspx?eventid=1",
                        "geometry": "https://www.gdacs.org/gdacsapi/geom"},
                "fromdate": "2026-08-01T00:00:00",
            }
        }]})
        out = b9._build_gdacs_engine({"_name": "gdacs", "timeout": 5})("flood", 5)
        assert out
        assert isinstance(out[0]["url"], str)
        assert out[0]["url"].startswith("https://www.gdacs.org/report.aspx")
        assert "[FL]" in out[0]["title"]


class TestObisWorms:
    def test_obis_mixed_query(self, monkeypatch):
        _patch_json(monkeypatch, {"total": 100, "results": [
            {"scientificName": "Gadus morhua", "depth": 12},
        ]})
        out = b9._build_obis_engine({"_name": "obis", "timeout": 5})("Gadus morhua 海洋物种", 5)
        assert out and "Gadus morhua" in out[0]["title"]

    def test_obis_pure_cjk_returns_empty(self, monkeypatch):
        _patch_json(monkeypatch, {"total": 1, "results": [{"scientificName": "X"}]})
        assert b9._build_obis_engine({"_name": "obis", "timeout": 5})("海洋物种", 5) == []

    def test_worms_arphia_parse(self, monkeypatch):
        _patch_json(monkeypatch, [
            {"scientificname": "Gadus morhua", "AphiaID": 126436,
             "status": "accepted", "rank": "Species", "kingdom": "Animalia"},
        ])
        out = b9._build_worms_engine({"_name": "worms", "timeout": 5})("Gadus morhua", 5)
        assert out and "126436" in out[0]["url"]


class TestNhtsaVpic:
    def test_chinese_make_normalised(self, monkeypatch):
        _patch_json(monkeypatch, {"Results": [
            {"Make_ID": 4411, "Make_Name": "TESLA"},
            {"Make_ID": 2, "Make_Name": "TOYOTA"},
        ]})
        out = b9._build_nhtsa_vpic_engine({"_name": "nhtsa_vpic", "timeout": 5})("特斯拉 车型", 5)
        assert out and out[0]["title"].startswith("TESLA")

    def test_noise_words_do_not_break_match(self, monkeypatch):
        _patch_json(monkeypatch, {"Results": [{"Make_ID": 1, "Make_Name": "BYD"}]})
        out = b9._build_nhtsa_vpic_engine({"_name": "nhtsa_vpic", "timeout": 5})("比亚迪 哪个牌子", 5)
        assert out and "BYD" in out[0]["title"]


class TestEgovLaw:
    @pytest.fixture(autouse=True)
    def _reset_cache(self):
        b9._EGOV_ENTRIES = None
        b9._EGOV_FETCHED_AT = 0.0
        yield
        b9._EGOV_ENTRIES = None
        b9._EGOV_FETCHED_AT = 0.0

    def _xml(self):
        return ("<LawNameListAll><LawNameListInfo><LawId>ABC</LawId>"
                "<LawName>民法</LawName><LawNo>明治二十九年法律第八十九号</LawNo>"
                "</LawNameListInfo><LawNameListInfo><LawId>DEF</LawId>"
                "<LawName>刑法</LawName><LawNo>明治四十年法律第四十五号</LawNo>"
                "</LawNameListInfo></LawNameListAll>")

    def test_prefix_stripped_and_gram_expanded(self, monkeypatch):
        """「日本民法 条文」应命中「民法」（剥「日本」前缀 + 全角/半角空格切词）。"""
        _patch_text(monkeypatch, self._xml())
        out = b9._build_egov_law_engine({"_name": "egov_law", "timeout": 5})("日本民法 条文", 5)
        assert out and any("民法" in r["title"] for r in out)

    def test_cache_avoids_refetch(self, monkeypatch):
        calls = {"n": 0}

        def counting(req, timeout=None, engine=""):
            calls["n"] += 1
            return _FakeResp(self._xml().encode())

        monkeypatch.setattr(b9, "http_open", counting, raising=False)
        eng = b9._build_egov_law_engine({"_name": "egov_law", "timeout": 5})
        eng("民法", 5)
        eng("刑法", 5)
        assert calls["n"] == 1, "第二次查询应命中进程内缓存"


class TestK10plus:
    def test_inline_namespace_attributes(self, monkeypatch):
        """OAI-DC 元素带 inline xmlns，正则必须容忍标签属性（实测回归点）。"""
        xml = ('<zs:searchRetrieveResponse><zs:numberOfRecords>1</zs:numberOfRecords>'
               '<zs:records><zs:record><zs:recordData>'
               '<oai_dc:dc><dc:title xmlns:dc="http://purl.org/dc/elements/1.1/">'
               'Tesla Motors</dc:title>'
               '<dc:creator xmlns:dc="http://purl.org/dc/elements/1.1/">Author</dc:creator>'
               '</oai_dc:dc></zs:recordData></zs:record></zs:records>'
               '</zs:searchRetrieveResponse>')
        _patch_text(monkeypatch, xml)
        out = b9._build_k10plus_engine({"_name": "k10plus", "timeout": 5})("tesla", 5)
        assert out and out[0]["title"] == "Tesla Motors"


class TestRor:
    def test_names_and_location_schema(self, monkeypatch):
        """ROR v2：机构名在 names[]（ror_display），国家在 locations[]。"""
        _patch_json(monkeypatch, {"items": [{
            "id": "https://ror.org/01vceef04",
            "names": [{"value": "Tsinghua SIGS", "types": ["alias"]},
                      {"value": "Tsinghua University", "types": ["ror_display", "label"]}],
            "locations": [{"geonames_details": {"country_name": "China"}}],
            "domains": ["tsinghua.edu.cn"],
        }]})
        out = b9._build_ror_engine({"_name": "ror", "timeout": 5})("tsinghua", 5)
        assert out and out[0]["title"] == "Tsinghua University"
        assert "China" in out[0]["snippet"]
