#!/usr/bin/env python3
"""argo screenshot — 网页截图（CLI 入口）。

CLI 与 MCP 工具面保持一一对应（见 bin/argo 的 scripts 注册表注释）：
MCP 有 argo_screenshot 就应有 `argo screenshot`，否则该能力只能靠默认关闭的
MCP 够得着。本文件与 mcp_handlers.py 的 argo_screenshot 分支共用 chrome_cdp。
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from typing import Any
from cli_io import dumps

_SCRIPTS = os.path.dirname(os.path.realpath(__file__))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)


def capture_screenshot(url: str, output: str | None = None, full_page: bool = False,
                       wait_until: str = "networkidle") -> dict[str, Any]:
    """截图并返回与 MCP argo_screenshot 同构的结果字典。"""
    import chrome_cdp

    target = output or os.path.join(
        tempfile.gettempdir(), f"argo_screenshot_{int(time.time())}.png")
    try:
        # with 上下文：无论 navigate/screenshot 成功或抛异常，
        # __exit__ 都会 stop() 回收 Chrome 进程与临时目录
        with chrome_cdp.ChromeCDP(auto_start=True) as cdp:
            cdp.navigate(url, wait_until=wait_until)
            saved = cdp.screenshot(target, full_page=full_page)
    except Exception as e:
        return {"success": False, "url": url, "error": str(e)[:200]}

    if saved and os.path.exists(saved):
        return {"success": True, "url": url, "screenshot": saved}
    return {"success": False, "url": url, "error": "screenshot failed"}


def main() -> None:
    p = argparse.ArgumentParser(description="Argo screenshot — 网页截图（CDP）")
    p.add_argument("url", help="目标 URL")
    p.add_argument("--output", "-o", default=None, help="输出 PNG 路径（默认落临时目录）")
    p.add_argument("--full-page", action="store_true", help="整页截图（默认仅当前视口）")
    # 只暴露 navigate 真正实现的两种策略：给 domcontentloaded 等未实现的值
    # 会走 else 分支静默不等待，截到半渲染页面
    p.add_argument("--wait-until", default="networkidle",
                   choices=["networkidle", "load"],
                   help="导航等待策略（默认 networkidle）")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    args = p.parse_args()

    result = capture_screenshot(args.url, output=args.output,
                                full_page=args.full_page,
                                wait_until=args.wait_until)

    if args.json:
        print(dumps(result))
    elif result.get("success"):
        print(result["screenshot"])
    else:
        print(f"截图失败：{result.get('error')}", file=sys.stderr)

    sys.exit(0 if result.get("success") else 1)


if __name__ == "__main__":
    main()
