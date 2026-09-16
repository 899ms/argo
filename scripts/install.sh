#!/usr/bin/env bash
# Argo 一键安装脚本（克隆/更新真源 + 依赖 + 可选 Skill 链接）
# 用法：
#   curl -fsSL https://raw.githubusercontent.com/taxueseek/argo/main/scripts/install.sh | bash
#   ARGO_HOME=/path/to/argo bash scripts/install.sh
#   bash scripts/install.sh --link ~/.claude/skills/argo
#
# 环境变量：
#   ARGO_HOME     安装目录，默认 ~/.local/share/argo
#   ARGO_REPO     仓库地址，默认 https://github.com/taxueseek/argo.git
#   ARGO_BRANCH   分支，默认 main
#   ARGO_SKIP_PIP 设为 1 则跳过 pip 安装
#   ARGO_LINK_TARGETS  冒号分隔的 Skill 入口路径（可选）
#   ARGO_PIN      固定 commit（供应链加固：克隆后校验 HEAD，推荐生产使用）

set -euo pipefail

# 本脚本用 bash 语法（数组、[[ ]]），在 sh/dash/ksh 下会在解析期报一堆语法错误、
# 且看不出真正原因。FreeBSD/OpenBSD/精简容器里 /bin/sh 不是 bash，所以先自证运行
# 环境：不是 bash 就尝试用 bash 重跑，找不到 bash 则给出一句可执行的指引。
if [ -z "${BASH_VERSION:-}" ]; then
  if command -v bash >/dev/null 2>&1; then
    exec bash "$0" "$@"
  fi
  echo "install.sh 需要 bash（本脚本使用数组与 [[ ]] 语法）。" >&2
  echo "  Debian/Ubuntu: sudo apt-get install -y bash" >&2
  echo "  Alpine:        apk add bash" >&2
  echo "  FreeBSD:       pkg install -y bash" >&2
  echo "  OpenBSD:       pkg_add bash" >&2
  echo "  macOS/多数 Linux 自带 bash，可直接： curl -fsSL <url> | bash" >&2
  exit 1
fi

REPO="${ARGO_REPO:-https://github.com/taxueseek/argo.git}"
BRANCH="${ARGO_BRANCH:-main}"
PIN="${ARGO_PIN:-}"
HOME_DIR="${HOME:-$(eval echo ~)}"
INSTALL_DIR="${ARGO_HOME:-$HOME_DIR/.local/share/argo}"
SKIP_PIP="${ARGO_SKIP_PIP:-0}"

LINK_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --home)
      INSTALL_DIR="$2"
      shift 2
      ;;
    --link)
      LINK_ARGS+=(--to "$2")
      shift 2
      ;;
    --skip-pip)
      SKIP_PIP=1
      shift
      ;;
    --pin)
      PIN="$2"
      shift 2
      ;;
    -h|--help)
      sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "未知参数: $1" >&2
      exit 1
      ;;
  esac
done

echo "==> Argo 安装目录: $INSTALL_DIR"
echo "==> 仓库来源: $REPO (分支 $BRANCH)"
echo "    提示: 若以 curl|bash 方式执行，请确认来源为 trusted 仓库；生产环境建议 ARGO_PIN 固定 commit。"

if ! command -v git >/dev/null 2>&1; then
  echo "需要 git，请先安装后再试。" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "需要 Python 3.9+，请先安装后再试。" >&2
  exit 1
fi

# 版本下限与 bin/argo 的 MIN_PYTHON 必须一致：改一处就要改另一处。
# 为什么是 3.9：唯一的必需第三方依赖 PyYAML 早已支持 3.9，脚本也几乎都带
# `from __future__ import annotations`；早期这里写 3.10 的理由是「用了 X | None
# 语法」，但实测全仓 0 处该写法。抬高门槛只会把 macOS 原生 python3、老 Debian
# 和 python:3.9 容器这些环境挡在门外。
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 9) else 0)')
if [[ "$PY_OK" != "1" ]]; then
  echo "当前 Python 为 ${PY_VER}，需要 3.9+。" >&2
  exit 1
fi

if [[ -d "$INSTALL_DIR/.git" ]]; then
  echo "==> 已有仓库，拉取更新 ($BRANCH)…"
  git -C "$INSTALL_DIR" fetch --depth 1 origin "$BRANCH"
  git -C "$INSTALL_DIR" checkout "$BRANCH"
  git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH" || true
elif [[ -d "$INSTALL_DIR" ]] && [[ -f "$INSTALL_DIR/scripts/search.py" ]]; then
  echo "==> 目录已存在且含 Argo 源码，跳过克隆: $INSTALL_DIR"
else
  echo "==> 克隆仓库…"
  mkdir -p "$(dirname "$INSTALL_DIR")"
  git clone --depth 1 --branch "$BRANCH" "$REPO" "$INSTALL_DIR"
fi

# 供应链加固：校验 HEAD 与预期 commit 一致（ARGO_PIN 固定）
if [[ -n "$PIN" ]]; then
  ACTUAL_HEAD=$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null || echo "unknown")
  if [[ "$ACTUAL_HEAD" != "$PIN" ]]; then
    echo "!! 供应链校验失败：固定 commit 为 ${PIN}，实际 HEAD 为 ${ACTUAL_HEAD}" >&2
    echo "!! 仓库内容与预期不一致，拒绝继续。请人工核查仓库来源。" >&2
    exit 1
  fi
  echo "==> 供应链校验通过: HEAD=$PIN"
fi

if [[ "$SKIP_PIP" != "1" ]]; then
  echo "==> 安装依赖 (PyYAML)…"
  python3 -m pip install --user -q pyyaml 2>/dev/null \
    || python3 -m pip install -q pyyaml 2>/dev/null \
    || echo "[warn] pip 安装 PyYAML 失败，可手动: pip install pyyaml"
  echo "==> 安装可选增强 (curl_cffi: TLS 指纹伪造，缺失不影响核心功能)…"
  python3 -m pip install --user -q curl_cffi 2>/dev/null \
    || python3 -m pip install -q curl_cffi 2>/dev/null \
    || echo "[warn] pip 安装 curl_cffi 失败（可选依赖）。需要反爬 TLS 指纹伪造时手动: pip install curl_cffi"
fi

if [[ ${#LINK_ARGS[@]} -gt 0 ]] || [[ -n "${ARGO_LINK_TARGETS:-}" ]] || [[ -f "$INSTALL_DIR/installs.local.yaml" ]]; then
  echo "==> 链接 Skill 入口（符号链接回真源，不复制）…"
  python3 "$INSTALL_DIR/scripts/link_source.py" "${LINK_ARGS[@]+"${LINK_ARGS[@]}"}" || {
    echo "[warn] 链接未完成。可稍后手动:"
    echo "  python3 $INSTALL_DIR/scripts/link_source.py --to ~/.claude/skills/argo"
  }
fi

echo ""
echo "==> 安装完成"
echo ""
echo "快速验证:"
echo "  python3 $INSTALL_DIR/scripts/search.py \"Python asyncio\" --json"
echo "  python3 $INSTALL_DIR/scripts/search.py --list-engines"
echo ""
echo "启动 MCP（给 Claude / Kimi / Cursor 等用）:"
echo "  python3 $INSTALL_DIR/scripts/mcp_server.py"
echo ""
echo "或用 npx（需 Node.js 18+）:"
echo "  npx -y github:taxueseek/argo"
echo ""
echo "客户端 MCP 配置示例（路径请按本机替换）:"
cat <<EOF
{
  "mcpServers": {
    "argo": {
      "command": "python3",
      "args": ["$INSTALL_DIR/scripts/mcp_server.py"]
    }
  }
}
EOF
echo ""
echo "可选：把 Skill 挂到本机 Agent 目录（不复制代码）:"
echo "  python3 $INSTALL_DIR/scripts/link_source.py --to ~/.claude/skills/argo"
echo "  python3 $INSTALL_DIR/scripts/link_source.py --to ~/.agents/skills/argo"
echo ""
echo "文档: https://github.com/taxueseek/argo"
