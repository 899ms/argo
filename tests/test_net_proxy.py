#!/usr/bin/env python3
"""net_proxy 出口调度单元测试：优先级矩阵 / 隧道构造 / 绝对 URL 选择器。

issue #13（2026-09-14）：http_client 直用 http.client，不认标准代理环境变量，
需代理站点（GitHub）抓取必然失败。修复=统一出口决策点 net_proxy，本测试
锁死解析优先级与连接构造契约。全 mock，无网络。
"""

from __future__ import annotations

import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import net_proxy  # noqa: E402

_PROXY = "http://127.0.0.1:7890"


def _cfg(rules=None, url=""):
    return {"url": url, "rules": rules or {}}


class TestResolvePriority(unittest.TestCase):
    def setUp(self):
        self._cfg_patch = patch.object(net_proxy, "_network_cfg",
                                       return_value=_cfg())
        self._cfg_patch.start()
        self.addCleanup(self._cfg_patch.stop)

    def test_override_wins_and_direct_sentinel(self):
        self.assertEqual(net_proxy.resolve_proxy("https://a.com", override=_PROXY), _PROXY)
        self.assertIsNone(net_proxy.resolve_proxy("https://a.com", override="direct"))

    def test_argo_env_used_without_rules(self):
        with patch.dict("os.environ", {"ARGO_PROXY": _PROXY}):
            self.assertEqual(net_proxy.resolve_proxy("https://bochaai.com"), _PROXY)

    def test_rules_direct_beats_argo_env(self):
        with patch.object(net_proxy, "_network_cfg",
                          return_value=_cfg(rules={"bochaai.com": "direct"})), \
             patch.dict("os.environ", {"ARGO_PROXY": _PROXY}):
            self.assertIsNone(net_proxy.resolve_proxy("https://open.bochaai.com/x"))

    def test_rules_suffix_match_with_proxy(self):
        with patch.object(net_proxy, "_network_cfg",
                          return_value=_cfg(rules={"github.com": _PROXY})), \
             patch.dict("os.environ", {"ARGO_PROXY": "http://other:1"}):
            # 更具体的域规则优先于全局 env
            self.assertEqual(net_proxy.resolve_proxy("https://api.github.com/z"), _PROXY)

    def test_argo_env_beats_config_url(self):
        with patch.object(net_proxy, "_network_cfg",
                          return_value=_cfg(url="http://cfg:2")), \
             patch.dict("os.environ", {"ARGO_PROXY": _PROXY}):
            self.assertEqual(net_proxy.resolve_proxy("https://a.com"), _PROXY)

    def test_config_url_beats_standard_env(self):
        with patch.object(net_proxy, "_network_cfg",
                          return_value=_cfg(url="http://cfg:2")), \
             patch.dict("os.environ",
                        {"ARGO_PROXY": "", "HTTPS_PROXY": "http://env:3",
                         "https_proxy": ""}):
            self.assertEqual(net_proxy.resolve_proxy("https://a.com"), "http://cfg:2")

    def test_standard_env_used_as_last_resort(self):
        """标准环境变量是最后兜底。直接 patch getproxies/proxy_bypass，
        规避同名大小写变量在本机的真实串扰（3.14 后写者胜）。"""
        with patch("urllib.request.getproxies", return_value={"https": _PROXY}), \
             patch("urllib.request.proxy_bypass", return_value=False), \
             patch.dict("os.environ", {"ARGO_PROXY": ""}):
            self.assertEqual(net_proxy.resolve_proxy("https://a.com"), _PROXY)

    def test_no_proxy_bypass(self):
        with patch("urllib.request.getproxies", return_value={"https": _PROXY}), \
             patch("urllib.request.proxy_bypass", return_value=True):
            self.assertIsNone(net_proxy.resolve_proxy("https://a.com"))

    def test_no_config_no_env_direct(self):
        with patch.dict("os.environ",
                        {"ARGO_PROXY": "", "HTTPS_PROXY": "", "https_proxy": "",
                         "ALL_PROXY": "", "all_proxy": "", "HTTP_PROXY": "",
                         "http_proxy": ""}, clear=False):
            self.assertIsNone(net_proxy.resolve_proxy("https://a.com"))


class TestOpenConnection(unittest.TestCase):
    def test_https_via_proxy_sets_tunnel(self):
        parsed = urllib.parse.urlparse("https://github.com/x")
        seen = {}

        class FakeTunnelHTTPS:
            def __init__(self, host, port, timeout=None):
                seen["conn"] = (host, port)

            def set_tunnel(self, host, port):
                seen["tunnel"] = (host, port)

        with patch.object(net_proxy.http.client, "HTTPSConnection", FakeTunnelHTTPS):
            conn, via = net_proxy.open_connection(parsed, 5.0, _PROXY)
        self.assertTrue(via)
        self.assertEqual(seen["conn"], ("127.0.0.1", 7890))
        self.assertEqual(seen["tunnel"], ("github.com", 443))

    def test_http_via_proxy(self):
        parsed = urllib.parse.urlparse("http://example.com/a")
        conn, via = net_proxy.open_connection(parsed, 5.0, _PROXY)
        self.assertTrue(via)
        self.assertEqual(net_proxy.request_selector(parsed, "/a", via),
                         "http://example.com/a")

    def test_no_proxy_direct_https(self):
        parsed = urllib.parse.urlparse("https://github.com/x")
        conn, via = net_proxy.open_connection(parsed, 5.0, None)
        self.assertFalse(via)
        self.assertEqual(net_proxy.request_selector(parsed, "/x", via), "/x")


if __name__ == "__main__":
    unittest.main()
