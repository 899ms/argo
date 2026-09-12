# Argo v2.8.7 发布说明

**版本**：2.8.7（相对 2.8.6）
**日期**：2026-09-12
**主题**：失败看得懂、封锁不冤枉；收录的源真的接得上；派生件与真源脱钩的根因修掉——顺带让一致性校验第一次真的会失败。

## 主要改进

### 1. 失败从「空结果」变成「可执行的判断」

- **六类归因**（`engine_failure`）：dependency / auth / rate_limited / blocked / upstream / network，外加一个诚实的 unknown。每一类给出对应的下一步动作，而不是让用户自己猜是没登录、没装工具还是站点改版。
- **403 歧义仲裁**：纯 403 → auth（重登录有用）；403 叠加反爬页面特征 → blocked（重登录无用，要换请求形态）。判定顺序即裁决，a​​uth 语义先于封锁特征——把「需要登录」和「被封锁」混为一谈会把用户支去错误的修复方向。
- **归因寄存器**：失败现场（唯一能同时拿到引擎名、状态码、响应体的位置）写入，聚合层读后即清。此前 HTML 引擎被反爬拦截后静默返回空，封锁永远伪装成「没结果」，被封的引擎还会被当成故障累计进熔断——封错人。
- **熔断不再冤枉源站**：blocked / rate-limited 走 60s 短冷却，不累计 opens。被限流是「你打太快了」，不是「引擎坏了」。

### 2. 节流：同一家源站共用一个桶

- **域族归一表 18 组**：主站 / API 域 / CDN 域共享同一个节流桶（站点感受到的压力来自「这一家」，不是一个个孤立域名——按域名分桶会出现主站限流而 CDN 域继续冲锋）。
- **并发信号量与最小间隔双层**：只有并发上限、间隔为 0，请求会在瞬间齐发；只有间隔、没有并发上限，前序请求变慢时在途请求照样堆积。
- 只对声明的域族生效，未声明域名零开销直通；引擎 spec 可显式覆盖，声明即契约。

### 3. 归档写入时就脱敏

- URL 查询串里的密钥、Bearer 令牌、家目录路径在**写入时**改写为 `[REDACTED]` / `~`，覆盖 envelope 与三类 jsonl 全部下游数据。
- 为什么在写入时而不是导出时：密钥一旦以明文落盘，就已经泄漏给所有能读到该文件的人；出口脱敏依赖「有人记得调用」，不可靠。

### 4. 新能力

- **`argo preflight`（批量 URL 预检）**：开工前先给一张「什么能拿、什么拿不到、为什么」的清单。条目四类（unsupported / needs_auth / already_have / not_found），结论三档（go / go_with_skips / stop）。不探测不臆测：网络探测失败（连不上）不产生判定，不把「可能的问题」换成「确定的误杀」；只判定不抓取，与执行器分离。
- **`twitter_syndication` 引擎**：X 单条推文免登录通道。X 的网页与官方 API 对未登录请求一律拒绝，唯一常年开放的入口是面向第三方嵌入的 syndication 接口——**查询词是推文 URL 或裸 ID**，拿它搜关键词会诚实返回空（没有搜索端点，不伪造结果）。
- **A 档六数据源**：`deps_dev`（包依赖与弃用状态）、`nvd`（CVE 漏洞情报）、`endoflife`（产品生命周期）、`openreview`（顶会论文含评审）、`biorxiv`（生物医学预印本）、`un_comtrade`（双边贸易）。
- **四个窄意图域**：`security_search` / `lifecycle_search` / `package_intel` / `trade_stats`，各有明确的触发词与主引擎 + 通用源兜底。

### 5. 一致性与入口修复（本轮审查发现）

这一节的缺陷有一个共同特征：**测试全绿，能力实际不可用**。

- **`argo --help` 直接崩溃**：usage 文本的 f-string 里 `{status|inject|undo}` 漏了转义，抛 `NameError`——`--help`、无参数、未知子命令三条路径全部以 traceback 收场。已修复（`--help` 现在正常退出 0），并补 3 条入口回归门 + 1 条「f-string 里不许有裸花括号」的静态门。
- **`argo fetch` 文档里有、实现里没有**：SKILL.md 长期宣传该子命令，dispatcher 表里没有这个键。已补 `fetch → fetch_v3.py`，并加门禁：usage 里列出的每个子命令都必须能分发、映射到的脚本必须存在。
- **三份派生件与真源脱钩**（根因）：registry / quota_profiles / domain_profiles 只从 `config.yaml` 派生，而运行时真源是 `config.yaml + engines/*.yaml + engines/specs/*.yaml` 合并后的结果。后果：batch7 收录的 7 个引擎只存在于**人工改写的 registry**（违规），配额与领域画像两份整体漏侧；连同更早的外置 spec 引擎，**18 个引擎没有配额档位**，其中 `firecrawl` 声明的 1 qps / 1000 次每月限流**整体失效**（退化成 2 qps / 无限额）。
  修复：派生源改为与运行时同一入口（合并口径），三份派生件重新生成，**157 → 175 个引擎**，firecrawl 限流恢复。
- **一致性校验是同义反复**：`--check` 把「本次派生结果」与「本次派生结果」比较，永远绿——上面那批脱钩因此长期无人发现。已改为读**磁盘上的派生件**比对，并加值级比对：第一版只比引擎名，变异测试把 `firecrawl.qps` 改成 99 直接漏报，遂补到逐字段。现在四类变异（删条目 / 删引擎 / 改字段 / 改数值）全部报错。
- **能力标签写错了地方**：8 个引擎（含 4 个窄意图域的主引擎）的 `coverage` 只写在 registry（死文档）里，spec（活真源）里没有——而互补回填要求主引擎带 coverage 标签才工作，等于这 4 个域的能力回填一直没信号。已把标签搬回 spec，注册表由 spec 派生，不再需要人工维护。
- **同批次两个功能互相打架**：预检把单条推文 URL 判成「需要登录，请先补凭证」，而同一批次刚上线的 syndication 通道**恰好能免登录抓它**。已加例外：有免登录通道的推文 URL 不再判 needs_auth，主页类 URL 仍然照判。
- **syndication token 的 30 行浮点实现删掉了**：原实现自称「与服务端校验口径一致」，实测不成立——服务端只要 token 非空就返回推文（`token=a`、`token=0`、长随机串都一样，**省略 token 参数**才返回空），而原实现与它声称的公开公式在全部抽样 ID 上都不相等（固定 12 位小数 vs 最短往返表示、只去首尾 0 vs 去所有 0）。更糟的是测试把那个错误常量锁成了金标。已简化为常量并写明实测依据，测试改为锁真正的契约（必须非空）。
- **503 的三种口径统一**：HTTP 层一直把 503 当合规等待信号（带 Retry-After 则等待，见 `test_stop_signal.py`），归因寄存器说「限流」，而 `engine_failure` 说「上游改版，需更新解析实现」——同一台引擎的解释随你看哪个界面而变。现在统一为限流语义（等待重试，不改代码），408 与连接层失败归 network，归因寄存器改为直接委托 `engine_failure.classify`，200 个状态码逐一分歧清零。
- **文档口径**：`12 个 MCP 工具` → 14（2 处）、版本串 2.8.5/2.8.6 混用 → 统一 2.8.7、引擎数 `150+` → 175（165 启用）、SKILL.md 的 CLI 列表补全（fetch/extract/clarify/preflight/mcp）。新增门禁：文档声明的引擎数必须等于 `--list-engines` 的条目数，声明 MCP 工具数必须等于 `mcp_tools.TOOLS` 的长度。

### 6. 收录的源真的接得上（本轮续做）

上一节修的是「文档与实际不符」，这一节修的是「声明了但用不到」。

- **9 个源不可达，逐个处置**（可达性门禁此前只报 WARN，没有区分「有意」与「忘了」）：
  - **真接线 6 个**：新增两个窄意图域 `sec_filings`（美股申报原文，SEC EDGAR，触发词
    10-K/10-Q/8-K/招股书）+ `dataset_search`（数据集，DataCite + Zenodo，排在 `academic`
    之前——论文≠数据集）；`fxtwitter`（零密钥的 X 关键词搜索）接进 `social` 域；
    `firecrawl`（免密钥云搜索）接进通用兜底链**末位**，靠 combo 预算截断保护它每月
    1000 次的免费额度。
  - **声明显式专用 4 个**：`tinyfish`/`tinyfish_news`/`tinyfish_paper`（要自配
    `ARGO_TINYFISH_API_KEY`）与 `twitter_syndication`（输入形态是 URL，没有搜索端点）。
    新增 `explicit_only` 声明：门禁据此把它们从「死源」里分出来单独列，**没声明仍会报**——
    防止把「忘了接线」伪装成「有意显式」；同时也检查「声明了却又进了自动路由」的自相矛盾。
  - 结果：**死源 0，WARN 从 11 降到 1**（剩下那条是既有的日语汉字语言判定软告警）。
- **单条推文 URL 现在能自己找到路**：这类输入走的是 known-url 交接（搜索层不硬搜 URL），
  但交接建议里只有 `argo_fetch`/`argo_pdf`，用户看不到免登录通道。现在识别出单条推文后
  会建议 `argo_search(engine=twitter_syndication)` 并说明它不需要登录。
- **`--list-engines --routable-only` 此前静默失效**：`available_engines()` 不收这个参数，
  调用侧传进去抛 `TypeError` 被 `except` 吞掉后回退全量——用户以为筛过了，实际拿到全部
  165 个。已实现真正的过滤（本机此刻可用 156 个）。
- **新增《搜索源使用文档》** `docs/ENGINE_CATALOG.md`：175 个源逐条列出**费用档位 /
  频率上限 / 需要哪种密钥 / 什么时候会被用到 / 一句话说明**，另有「特别能力」清单
  （垂直结构化卡、抽取型取证、数据集与申报原文、深度研究、证据核验、批量预检…）与
  「默认关闭的能力怎么打开」。文档由 `scripts/gen_engine_catalog.py` 从**引擎声明**
  生成、`--check` 比对、`tests/test_engine_catalog.py` 挂门禁，**且刻意只用声明口径**
  （不写「本机此刻缺哪些密钥」这类瞬时状态——否则换台机器生成就与磁盘不符、门禁随机变红）。
- **`--help` 的引擎数也不再自己编**：原先是「启用数」（与文档的收录数对不上），配置读不出来时还会回退到写死的 87。现在只报收录数（配置不可读时不显示，不编数字），并把「本机可用数怎么查」写进帮助。
- **手写的引擎清单退位**：`references/engines.md` 原本维护着一份全量引擎表（已与事实脱节：
  写了 10 个外置引擎实际 18 个、felo 标成低价实为付费档、twitter 标「可选 nitter 兜底」
  实际缺 `tw` 后端）。现在它只管**清单生成不出来的东西**——语义分工（知乎四源、
  论文 vs 数据集 vs 申报）、URL 类查询的交接规则、兜底链层次、选源反模式。
  清单本身交给上面那份生成的文档，一处真源。

### 7. 计费口径与静默降级（第三轮）

- **按量计费的源曾被当成免费源，n 被放大**：`n` 桶化（同查询共享一次上游执行）本意只服务免费档，
  判据却是 `cost_factor >= 0.85`，而成本表**没有 api 档的分支**（fallthrough 到 1.0）——
  `exa` / `octen` / `you` / `parallel` / `zhihu_global` / `tavily` 这些按量计费的源全被当成免费：
  要 5 条被放大到 10 条，按结果计费的接口直接翻倍。现在按**声明的档位**判（只有 `free` 档才桶化），
  并加了门禁：计费档 `bucket_n(name, 5)` 必须等于 5。顺带把两张互相打架的成本表合一
  （`config` 是 low 0.7 / paid 0.3，`tfidf_router` 是 low 0.85 / paid 0.6），语义权重保持原值不变。
- **Tavily 从付费档改回按量计费档**：它和 `exa` 一样是「需密钥、有免费额度（1000/月）、超出按 credit 计费」，
  标成 `paid` 会让它在语义路由里被压权重、在文档里被当成「要花钱的源」。现在与 `exa` 同档，
  额度由本地配额表跟踪（用完降权），文档写明免费层。
- **博查结构化模态卡一直在静默降级**（发现过程值得记下来）：`modal_card` 域的主源 `bocha_ai` 实测返回 0 条，
  引擎状态却是 `ready`。根因是它的请求走 `urllib.request.urlopen` 直连、异常被 `safe_search` 吞成空列表，
  而**空列表与「这个词真没结果」在下游完全无法区分**——「上游 403」被当成「正常无结果」。
  抓出真实响应后可见：`403 {"message":"You do not have enough money or package quota"}`
  （该端点需单独套餐，本机 key 只有 web-search 权限）。现在博查两个引擎的 HTTP 失败返回带状态码与
  上游错误体的 error 记录：不抛异常、不进结果集，但会被 `search.py` 判成 `quota-exhausted`——
  实测已把 `bocha_ai` 停用至 2026-10-12 并记下原因。同批把归因层对齐：
  **403 叠加「额度/套餐」文案优先判为限流语义**，不再建议「重新登录」（登录改不了套餐）。
  对应的旧测试（`TestBochaHttp403Safe`，断言 403 应吞成空列表）同步改写为「不抛、但要留下可归因记录」，
  并新增一条断言把「error 记录 → quota-exhausted」的映射锁住。
- **观测盲区量化**：四个 builder 文件里有 **71 处直连 `urllib.request.urlopen`**，绕过带归因的
  HTTP 路径，因此它们的 HTTP 失败既不进归因寄存器、也不出现在 `--list-engines --detail`。
  博查只是被这种方式掩盖的其中一个。建议后续逐步收口到 `http_client`（本轮未做，属结构性改造）。

## 默认关闭功能的打开方式

- **MCP 14 工具**：默认关闭。DSH 用户在 profile patch 取消 `mcp-argo` 段注释后重启生效；非 DSH 用户见 README「MCP 接入」。
- **原生一等工具**：默认仅 `argo_search` / `argo_fetch`；`nativeTools` 配置可按需启用全部 13 个（`argo_research` 除外——分钟级编排不适合 60s 单发超时）。
- **需密钥的源**（19 个，清单见 `docs/ENGINE_CATALOG.md`）：把密钥写进 `~/.config/argo/env`（600 权限）或环境变量；没配也不影响搜索，路由会跳过。
- **显式专用的源**（4 个）：不进自动路由，用 `argo search "词" --engine <名>` 调用。
- **recompute 数值重算器**：默认 fail-closed，`--allow-recompute` 或 `ARGO_ALLOW_RECOMPUTE=1` 显式授权。
- **抽取型引擎 twitter_syndication**：不会由关键词查询触发（它没有搜索端点）。用 `argo search "<推文 URL 或 ID>" --engine twitter_syndication`，或 MCP `argo_search` 的 `engine` 参数指定——粘贴推文 URL 时交接提示也会告诉你这条通道。

## 相关依赖

- 无新增强制依赖；Python 3.10+，可选 `yaml` / `curl_cffi`（TLS 指纹层，缺失时自动降级到 HTTP 层）。
- `twitter_syndication` 零密钥（syndication 是面向嵌入的公开接口）；`un_comtrade` 免 key、`nvd` 免 key（5 请求/30 秒，声明在 spec 的注释里）。

## 验证

```
pytest            1533 passed / 21 skipped / 0 failed
offline 金标      132 PASS / 0 FAIL / 1 WARN（死源 0；唯一 WARN 是既有的日语汉字判定软告警）
可达性           175 收录 / 143 免密钥开箱可用 / 4 显式专用 / 10 已停用
一致性校验        sync_backends.py --check → 175 个引擎一致（幂等）
搜索源文档        gen_engine_catalog.py --check → 与声明一致（换 state dir 复核也一致）
变异测试          派生件四类篡改 / 花括号门禁 / 计费档桶化 均能报错
计费口径          api 档 bucket_n(5)=5（不再被放大）；tavily 归 api（免费层 1000/月）
静默降级          bocha_ai 403→quota-exhausted，源停用至 2026-10-12（原因已记录）
新接线实跑        「苹果 10-K」→ sec_edgar 直出申报原文；「climate dataset」→ Zenodo 数据集
```

## 升级

```bash
npx github:taxueseek/argo        # 或 install.sh / install.ps1
```

无破坏性配置变更；引擎 `coverage` / `recommended` 声明为可选项。DSH 插件用户随插件包升级自动生效。
