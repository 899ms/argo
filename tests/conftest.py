#!/usr/bin/env python3
"""pytest 全局配置。

存量测试大量 mock urllib.request.urlopen 验证引擎 URL 构造（setlang/lang 等），
HttpClient 接入后这些 mock 不再生效。默认回退 urllib 路径，保证存量检查行为
不变；HttpClient 新行为的专项测试显式 monkeypatch.setenv 开启。

另：把 argo 状态目录隔离到临时目录。部分模块（如 v2ex_nodes 的节点表缓存）
默认写 argo_paths.state_path()，测试若不隔离会**污染生产缓存**——
实测曾把测试 fixture（1 个假节点）写进 ~/.cache/unified-search/，
导致真实调用全部路由失败。ARGO_STATE_DIR 是 argo 既有的一级开关，
在这里设一次即可覆盖所有遵循该约定的模块。
"""

import os
import tempfile

os.environ.setdefault("ARGO_ENGINE_HTTP_CLIENT", "0")

# 路由决策缓存默认关闭，理由与上面的 HTTP_CLIENT 同类：它是跨进程的持久缓存，
# 而本会话的状态目录**整轮共享**——于是「A 用例路由过 Q」会把决策留给「B 用例
# 换过夹具后再路由 Q」，用例之间互相串味，且结果与执行顺序相关。关掉后退回每次
# 实算，存量检查的行为与引入缓存前逐位一致；缓存自身的行为由
# tests/test_route_cache.py 显式打开开关验证。
os.environ["ARGO_ROUTE_CACHE"] = "0"

# 状态目录隔离（必须在任何 argo 模块 import 前设置）
_STATE_DIR = tempfile.mkdtemp(prefix="argo-test-state-")
os.environ["ARGO_STATE_DIR"] = _STATE_DIR
