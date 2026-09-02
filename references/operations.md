# Argo 操作参考（按需读取）

> 本页承接 SKILL.md 已下沉的低频**操作型**细节：工具清单、MCP/DSH 接入、配额、子技能、本地打通、工程纪律。
> 只在需要「配置 MCP / 接入 DSH 插件 / 用子技能 / 改引擎」时读，日常搜索/抓取/深度研究不必读。

## MCP 工具清单（14 个）

| 工具 | 用途 |
|------|------|
| `argo_search` | 统一网络搜索（多引擎融合、时间/域名过滤） |
| `argo_local_search` | 本机文件/记录搜索（非联网） |
| `argo_local_read` | 白名单本地文本预览（`ARGO_LOCAL_READ_DIRS`，fail-closed） |
| `argo_recompute` | fail-closed 可复算执行器（受限子进程重算数值） |
| `argo_research` | 深度研究取证（含 social-sentiment） |
| `argo_evidence` | 来源可信度评估（Selection×Absorption） |
| `argo_clarify` | 意图消歧 |
| `argo_crawl` | 站点级爬取（sitemap/BFS） |
| `argo_fetch` | 智能抓取（mode=extract 结构化提取） |
| `argo_screenshot` | 网页截图 |
| `argo_pdf` | PDF 结构化提取 |
| `argo_social_search` | 社交平台搜索（mode=sentiment 舆情聚合） |
| `argo_article` | 微信公众号文章全文 |
| `argo_job` | 招聘岗位多平台聚合 |

## MCP 服务

```bash
python3 scripts/mcp_server.py [--test]
```

多客户端 MCP 一键接入（自研，注入/诊断/还原；客户端描述真源 `mcp/clients.yaml`）：

```bash
argo mcp status                 # 诊断各客户端（已安装/已配置）
argo mcp inject --all           # 注入所有已安装客户端（原子写 + 备份）
argo mcp inject --cursor        # 注入指定客户端（支持逗号分隔）
argo mcp undo --all             # 还原（精确移除 entry 或从备份回滚）
argo mcp inject --all --dry-run # 只预览不写
```

安全可逆：写入前备份到 `~/.argo/mcp-backup/`（带时间戳），atomic_write（同目录 temp+rename），含密钥配置 0600 权限；TOML 走行级 append section 不破坏手写注释。支持 Claude Code / Cursor / Windsurf / Codex / OpenCode / Cline。

细节见 `docs/MCP_SETUP.md`。

## DeepSeek Harness 插件接入

一键安装（原生 `argo_search` / `argo_fetch` 工具 + `web_search` seam + `wide_research` 编排；MCP 完整工具面默认不挂、按需在 profile patch 中开启——搜索/抓取高频路径走 CLI 单发同引擎同守卫，零常驻 token 开销）：

```bash
dsh plugin --profile web add "github:taxueseek/argo#main&path:packages/dsh-plugin"
```

`wide_research` 与 `argo_research` 共用同一套证据语义：规划互补轨道（可带 `depends_on` 依赖分阶段，默认并行）→ 有界并发子代理取证 → 来源账本（仅 http(s) URL 入账）→ 综合报告，输出自带 `quality_gate_results`（`passed` / `conclusion_cap`：failures→low、warnings→medium、干净→high）。`passed=false` 或 `conclusion_cap=low` 时禁止把报告结论当事实表述，先 `argo_fetch` / `--verify` 核验账本来源再下判断。worker 只用 argo 取证工具，不允许调用 `argo_research`（防研究套研究，硬保护不可放行）。

## 配额与引擎

```bash
python3 scripts/quota.py stats              # 配额状态
python3 scripts/search.py --list-engines    # 全量引擎清单（真源 config.yaml）
python3 scripts/search.py --list-engines --routable-only
```

TinyFish 搜索引擎（`tinyfish` / `tinyfish_news` / `tinyfish_paper`）与抓取渲染层（`_tinyfish_fetch`）**原生直连** `api.search/fetch.tinyfish.ai`，认证用 `X-API-Key`（官方标准）。配置：

```bash
export TINYFISH_API_KEY="sk-tinyfish-..."   # 去 agent.tinyfish.ai/api-keys 申请
```

未配置 `TINYFISH_API_KEY` 时，search 引擎不进路由（`env_ready=false`），抓取渲染层自动回退浏览器，不崩、不改变既有抓取行为。

## 子技能

| 子技能 | 位置 | 入口 |
|--------|------|------|
| local-search（本地零成本聚合） | `sub-skills/local-search/` | `python3 scripts/search.py "查询" --local-first` |
| local-seek（本机文件搜索） | `sub-skills/local-seek/` | `python3 sub-skills/local-seek/scripts/seek.py "查询" --path ~/notes --count`（MCP: `argo_local_search`） |
| ego-search（登录态专业搜索） | `sub-skills/ego-search/` | `python3 sub-skills/ego-search/scripts/ego_search.py search "AI 搜索" --runtime auto` |

细节见 `references/usage.md`（子技能章节）。

## 本地打通（三通道）

- **搜索体验**：`python3 scripts/search.py "查询" --include-local` —— 联网结果尾部并入本机文件命中（file:// 带行号，source=local_files，不参与融合评分）
- **本地分析**：MCP `argo_local_read`（白名单预览，`ARGO_LOCAL_READ_DIRS=~/data,~/notes` 配置；worker 侧在 wide_research 默认工具白名单）；数据计算走工作包 `recompute`（fail-closed 授权）
- **插件 wide_research 接入**：`file_inputs`（本地一手数据，登记血缘 sha256/路径，内容不入账）+ `recompute`（可复算契约，编排器侧受限执行，产出 `recomputed_values`）+ `include_local`（worker 搜索并入本机命中）；门禁 `recompute_skipped` / `recompute_conflict` 对齐核心，本地一手计入一手命中（防 no_source 假阴性）
- **成果复用**：`python3 scripts/research.py --search-archive "主题词" [--archive-since 日期]` —— 检索历史研究/搜索归档（`数据/argo-search-archive/runs/`），按主题词 + 时间窗列出历史 run 与来源统计

## 工程纪律（单一真源）

- **代码真源** = 本仓库；**引擎声明真源** = `config.yaml`（外置 `engines/specs/*.yaml` 优先覆盖同名引擎）；注册表由 `scripts/sync_backends.py` 派生到 `backends/*`
- **宿主入口** 用 `scripts/link_source.py` symlink 指回真源（目标来自 `--to` / `ARGO_LINK_TARGETS` / 本机 `installs.local.yaml`）；禁止 rsync/多副本；禁止在产品代码写死主机 skill 路径
- **新增搜索源**：只改 `config.yaml`（必要时 `scripts/engines.py` 注册 builder）→ `python3 scripts/sync_backends.py && python3 scripts/sync_backends.py --check` → 回归 `python3 -m pytest tests/ -q`
