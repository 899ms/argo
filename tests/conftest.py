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

# 状态目录隔离（必须在任何 argo 模块 import 前设置）
_STATE_DIR = tempfile.mkdtemp(prefix="argo-test-state-")
os.environ["ARGO_STATE_DIR"] = _STATE_DIR
