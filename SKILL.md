---
name: argo
description: Argo 阿尔戈 — 统一搜索、网页抓取与证据核验。覆盖意图：搜索/查一下/核实/抓取网页/爬取/深度研究/论文检索/新闻/舆情/公众号文章/招聘聚合。多语言检测与跨语言回退；约 150+ 引擎 TF-IDF 路由 + RRF；影视/体育/地理/组织/媒体/金融/宏观/化学等垂直源；垂直结构化模态卡（火车票/油价/贵金属/万年历/星座/手机/汽车/挂号）；日常 combo 预算与深度研究 boost；recovery 防污染；Selection×Absorption。CLI：argo search|research|fetch|crawl|article|job|evidence（同引擎 MCP 14 工具按需启用，默认关）。
version: 2.8.5
triggers:
  - 搜索
  - 查一下
  - 搜一下
  - 核实
  - 查证
  - 可信度
  - 抓取
  - 爬取
  - 深度研究
  - 论文
  - 舆情
  - 公众号
  - 招聘
  - search for
  - look up
  - fact check
  - fetch
  - crawl
  - research
---

# Argo v2.8.5 — 统一搜索与证据核验

> 从「帮你搜到」升级为「帮你核到」。搜索输出自带**证据闭环**：高后果问题（金融/医疗/法律/事实核查）标 `fetch_required`，每条结果标 `fetch_suggested`；`--verify` 一键核验正文并回填「核实后证据分」，核实过的链接自动记忆，二次搜索直接显示已核实。
>
> 本版增量 / 变更日志见 `docs/RELEASE_NOTES_v2.8.5.md` 与 `docs/RELEASE_NOTES_v2.8.0.md`。

## 快速上手

```bash
python3 scripts/search.py "查询词"            # 自动路由搜索
python3 scripts/search.py "查询词" --json     # JSON 输出（供 Agent 消费）
python3 scripts/search.py "查询词" --verify 3 # 核验 top-3 并回填证据分
python3 scripts/research.py "复杂问题" --json # 取证包（扩词或多工作包 → dossier）
```

深度研究只走这一条路径。机器产出 **dossier**（来源/覆盖/缺口/门禁），不是判断稿。Agent 先读 `references/research-protocol.md`，写出工作包再取证；判断按事实/推断/建议写。不要另装「专业深度研究」skill。

## 核心命令

### search — 统一搜索

| 参数 | 说明 |
|------|------|
| `--engine <name>` | 强制引擎（anysearch/byted/bocha/exa/tavily/eastmoney/zhihu/arxiv/pypi/mdn/hackernews/v2ex/redskill…，全量见 `--list-engines`） |
| `--local-first` | 本地零成本聚合优先（local_search 33 引擎） |
| `--include-local` | 并入本机文件命中（seek 结果尾部，source=local_files；默认关） |
| `--mode fast|auto|deep|budget` | fast 免费优先 / auto 成本感知（默认）/ deep 质量优先 / budget 配额控制 |
| `--explain` | 解释路由决策（含 TF-IDF 分数） |
| `--no-cache` / `--depth fast|balanced|deep` | 跳过缓存 / 搜索深度 |
| `--since 7d|2026-08-01` `--until` `--sort relevance|newest|oldest` | 时间窗过滤 + 时间排序 |
| `--verify [N]` | 对 top-N 未核验结果 fetch 正文，回填证据分（URL→证据分缓存，同 URL 二次搜索自动复用） |
| `--domain` `--sub_domain` | 垂直域 / 子域限定 |

### 增强三工具

```bash
# research — 取证（扩词或 --work-packages → dossier + citations + 可判定门禁）
python3 scripts/research.py "查询" [--sub-queries N] [--depth deep] [--budget N] \
    [--route-strategy local_first|cost_aware|full] [--work-packages PATH|JSON] \
    [--json] [--verify N] [--allow-recompute]

# 工作包可带 file_inputs（本地一手数据入账）+ recompute（可复算脚本，fail-closed 授权）
#   [{"id":"wp-rev","question":"…","file_inputs":[{"path":"~/data/company.xlsx","role":"原始数据"}],
#     "recompute":{"script":"…","budget":{"timeout_s":30}}}]

# 社交舆情模式
python3 scripts/research.py "iPhone 16 用户评价" --mode social-sentiment --platforms xiaohongshu,reddit,twitter

# evidence — 可信度评估（Selection×Absorption）
echo '{"results": [...]}' | python3 scripts/evidence.py "查询词" --stdin --json [--high-stakes]

# clarify — 意图消歧
python3 scripts/clarify.py "有歧义的查询" --explain --json
```

### 抓取三工具（`bin/argo` 入口）

```bash
argo fetch "https://example.com" [--focus "关键词"] [--use-browser]
# {url}.md 直出探测 → HTTP（桌面/移动 UA，抖音等分流站移动优先）→ TLS 指纹 → Wayback/浏览器 自动降级 + BM25 聚焦提取 + 质量信号 + 内容安全引擎
argo screenshot "https://example.com" [--full-page] [--output /tmp/page.png]
argo pdf "https://example.com/paper.pdf" [--pages "1-5"] [--password "secret"]
```

## Agent 执行纪律

1. **高后果问题**（金融/医疗/法律/事实核查）：search → evidence（或看 `credibility_fast`）→ fetch 高分 URL → 再下结论；`fetch_required=true` 时禁止跳过核验
2. **数字**：必须标注口径（全市场/主动/持仓市值 vs 占比）；冲突时并列，禁止口径未对齐合并
3. **SERP 链**（baidu/s、sogou/link）：禁止当正文来源
4. **社交帖**：叙事/舆情，不进事实真值
5. **深度研究**：先读 `references/research-protocol.md`；有决策含义就交工作包，不要靠扩词充问题树；`quality_gate_results.passed=false` 必须降级表述

## 证据闭环（v2.8.0）

搜索输出已带证据门控，Agent 可编程判断「现在能不能下结论」：

- `fetch_required`：bool。命中高后果域时为 true，下结论前必须核验正文
- `evidence_loop.suggested / verified_count / pending_count`：建议核验 URL 列表 / 已核验 / 待核验
- 每条结果：`fetch_suggested`（是否建议核验）、`has_fetched_evidence`（是否已核验）、`post_fetch_absorption`（正文级吸收分，核验后回填）

```bash
python3 scripts/search.py "贵州茅台股价" --verify 3
# [verify] 核验 3 条，improved=2 unchanged=1 degraded=0 mean_delta=0.18
```

## 按需读取（低频操作细节）

以下内容不每次必读，按需打开对应参考。日常搜索/抓取/深度研究走上面核心命令即可。

| 场景 | 读什么 |
|------|--------|
| MCP 工具全清单 / 多客户端注入 / DSH 插件接入 / 配额·TinyFish / 子技能 / 本地打通 / 工程纪律 | `references/operations.md` |
| 参数大全、三大工具输出字段、子技能细节 | `references/usage.md` |
| 深度研究协议：契约、工作包、dossier vs 判断稿、可判定门禁 | `references/research-protocol.md` |
| 契约 / 工作包 / 判断稿骨架 | `references/research-templates.md` |
| 引擎全景：垂直域/社交/学术/本地引擎表 + 路由规则 | `references/engines.md` |
| 架构：文件结构、证据流水线、量化公式、输出 JSON Schema、内容质量信号 | `references/architecture.md` |
| MCP 多客户端注入详解 | `docs/MCP_SETUP.md` |

> 工程纪律（单一真源：代码真源=本仓库、引擎声明真源=config.yaml、宿主入口用 link_source.py symlink、新增搜索源流程）见 `references/operations.md` 末尾。
