#!/usr/bin/env python3
"""test_host_throttle.py — 域族节流（并发桶 + 最小间隔）回归测试。

覆盖：
  1. 域族归一：同族域名（主站/API/CDN）归同一节流组
  2. 并发桶：组内同时在途请求不超过上限
  3. 最小间隔：相邻请求发起时间不小于组间隔
  4. spec 覆盖：引擎显式声明参数优先于域族默认；显式声明不限流生效
  5. 未声明域名不受影响（零开销直通）
"""

import os
import sys
import threading
import time

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import http_client as hc  # noqa: E402


class TestHostGroup:
    """域族归一：同一家源站的多个域名共享同一个桶。"""

    def test_zhihu_family(self):
        assert hc.host_group_for("https://www.zhihu.com/api/v4/x") == "zhihu"
        assert hc.host_group_for("https://api.zhihu.com/answers") == "zhihu"
        assert hc.host_group_for("https://zhuanlan.zhihu.com/p/1") == "zhihu"

    def test_x_family(self):
        assert hc.host_group_for("https://x.com/a/status/1") == "x"
        assert hc.host_group_for("https://twitter.com/a/status/1") == "x"
        assert hc.host_group_for("https://pbs.twimg.com/media/x.jpg") == "x"

    def test_bilibili_family(self):
        assert hc.host_group_for("https://www.bilibili.com/video/BV1") == "bilibili"
        assert hc.host_group_for("https://i0.hdslb.com/bfs/a.png") == "bilibili"

    def test_undeclared_domain_passes(self):
        assert hc.host_group_for("https://example.org/x") is None
        assert hc.host_group_for("https://arxiv.org/abs/1") is None

    def test_suffix_not_substring(self):
        # notzhihu.com 不应因包含 zhihu.com 子串而误判
        assert hc.host_group_for("https://notzhihu.com/x") is None

    def test_bad_url_none(self):
        assert hc.host_group_for("not a url") is None


class TestSpecOverride:
    """spec 显式声明优先于域族默认；声明即契约。"""

    def setup_method(self):
        with hc._SPEC_OVERRIDE_LOCK:
            hc._SPEC_OVERRIDE_CACHE.clear()
        with hc._BUCKETS_LOCK:
            hc._BUCKETS.clear()

    def teardown_method(self):
        with hc._SPEC_OVERRIDE_LOCK:
            hc._SPEC_OVERRIDE_CACHE.clear()
        with hc._BUCKETS_LOCK:
            hc._BUCKETS.clear()

    def test_explicit_values_win(self):
        hc.register_spec_limit("eng_a", 1, 50)
        assert hc._limits_for("https://www.zhihu.com/x", "eng_a") == (1, 50)

    def test_explicit_no_limit(self):
        hc.register_spec_limit("eng_b", None, None)
        # 显式声明不限流：即使域名在声明组内也不节流
        assert hc._limits_for("https://www.zhihu.com/x", "eng_b") is None

    def test_fallback_to_group_default(self):
        got = hc._limits_for("https://www.zhihu.com/x", "eng_c")
        assert got == hc._HOST_GROUP_DEFAULTS["zhihu"]

    def test_engine_without_group(self):
        assert hc._limits_for("https://example.org/x", "eng_d") is None


class TestConcurrencyBucket:
    """并发桶：同时在途请求受上限约束。"""

    def setup_method(self):
        with hc._SPEC_OVERRIDE_LOCK:
            hc._SPEC_OVERRIDE_CACHE.clear()
        with hc._BUCKETS_LOCK:
            hc._BUCKETS.clear()

    def teardown_method(self):
        with hc._SPEC_OVERRIDE_LOCK:
            hc._SPEC_OVERRIDE_CACHE.clear()
        with hc._BUCKETS_LOCK:
            hc._BUCKETS.clear()

    def test_concurrency_capped(self):
        # 并发上限 1：第一个线程持有租约期间，第二个必须等待
        with hc._HostBucket(1, 0).lease():
            pass
        bucket = hc._HostBucket(1, 0)
        acquired = []

        def worker():
            with bucket.lease():
                acquired.append(time.monotonic())
                time.sleep(0.1)

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        time.sleep(0.01)
        t2.start()
        t1.join()
        t2.join()
        assert len(acquired) == 2
        # 第二个租约至少晚于第一个 0.1s（串行化证明）
        assert abs(acquired[1] - acquired[0]) >= 0.09

    def test_min_interval_spread(self):
        # 最小间隔 60ms：两次获取的发起间距不小于 60ms（留时钟误差余量）
        bucket = hc._HostBucket(4, 60)
        stamps = []
        for _ in range(3):
            with bucket.lease():
                stamps.append(time.monotonic())
        assert stamps[1] - stamps[0] >= 0.055
        assert stamps[2] - stamps[1] >= 0.055

    def test_zero_interval_no_wait(self):
        bucket = hc._HostBucket(4, 0)
        t0 = time.monotonic()
        for _ in range(5):
            with bucket.lease():
                pass
        assert time.monotonic() - t0 < 0.5


class TestGroupKeying:
    """桶键 = (组名, 参数)：同组同参共享，异参自成桶。"""

    def test_same_group_shares_bucket(self):
        with hc._BUCKETS_LOCK:
            hc._BUCKETS.clear()
        with hc.host_throttle("https://www.zhihu.com/a"), \
                hc.host_throttle("https://api.zhihu.com/b"):
            keys = list(hc._BUCKETS.keys())
        assert len(keys) == 1

    def test_different_params_separate_buckets(self):
        with hc._BUCKETS_LOCK:
            hc._BUCKETS.clear()
        with hc.host_throttle("https://www.zhihu.com/a"), \
                hc.host_throttle("https://www.zhihu.com/b", engine="sp"):
            pass
        hc.register_spec_limit("sp", 1, 5)
        with hc._BUCKETS_LOCK:
            hc._BUCKETS.clear()
        with hc.host_throttle("https://www.zhihu.com/a"), \
                hc.host_throttle("https://www.zhihu.com/b", engine="sp"):
            keys = list(hc._BUCKETS.keys())
        assert len(keys) == 2


class TestHttpClientIntegration:
    """HttpClient 出口集成：get/post 接受 engine 参数且不破坏返回契约。

    conftest 默认 ARGO_ENGINE_HTTP_CLIENT=0（回退 urllib），这里显式开启。
    """

    def test_get_accepts_engine_kwarg(self, monkeypatch):
        monkeypatch.setenv("ARGO_ENGINE_HTTP_CLIENT", "1")
        client = hc.HttpClient(timeout=2, max_retries=0)
        # 本地不存在的地址：返回 status=0 + error 的统一失败形态
        resp = client.get("https://127.0.0.1:9/x", engine="zhihu")
        assert resp["status"] == 0
        assert "error" in resp

    def test_throttle_yields_none_for_undeclared(self):
        with hc.host_throttle("https://example.org/x") as group:
            assert group is None

    def test_throttle_yields_group(self):
        with hc.host_throttle("https://www.zhihu.com/x") as group:
            assert group == "zhihu"
