# RELEASE_NOTES_v2.8.7

相对 v2.8.6 的改进：搜索源 168 → 218（+50，其中 184 个免配置开箱可用），业务域 72 → 89；抓取链新增三条「拿正文」的直出通道；修掉一批真实场景里会空手而归、答非所问或整串失效的问题。

## 新增能力：50 个数据源（全部免密钥或免费档）

- **金融财经**：美股筛选 Finviz + Seeking Alpha、SEC EDGAR 官方申报文件全文、基金数据东方财富、宏观时序 FRED、世界银行 300+ 国家指标、欧盟统计局、国家统计局 nbs_stats、实时汇率 fx_rate、财联社电报 + 金十快讯、东财全球资讯、同花顺热点、巨潮资讯公告与研报。
- **法律法规**（此前只有裁判文书，缺法条本身）：国家法律法规数据库 flk_law——问「民法典第 X 条」直接出官方条文；日本 e-Gov 法令、韩国法令检索；gov_regulations 补部门规章层。
- **标准与合规**：全国标准信息平台 std_samr + openstd 全文预览——问「数据安全 国标」「GB/T 45577」不再只能翻网页。
- **安全情报**：NVD 漏洞库 + crt.sh 证书透明度（子域名/钓鱼域情报）+ deps.dev 包依赖健康——「log4j 漏洞 CVSS 多少」「npm express 有没有已知风险」直查官方。
- **学术科研**：OpenAlex、EuropePMC、OpenReview、bioRxiv、Semantic Scholar、DataCite + Zenodo 数据集、Google Patents 专利、PubMed/临床试验/药品标签（ClinicalTrials/OpenFDA）、化学 PubChem、蛋白 UniProt + RCSB PDB、物种 GBIF + OBIS。
- **新闻舆情**：人民网、Google News RSS（多语言 + 时间窗 + site: 限定）、GDELT 全球事件库、知乎热榜免密钥通道（不再占开放平台配额）。
- **生活文娱**：豆瓣电影、Bangumi 番剧、网易云音乐、Met Museum 藏品、NASA 图像库、Steam 游戏、汉典字源、维基文库古文、少数派、微信读书。
- **能源/交通/空间**：电网发电结构与碳强度、航空气象 METAR、NOAA 空间天气、卫星轨道、共享单车 GBFS、NHTSA 车辆命名本体、USDA 土壤农业。
- **其他**：预测市场 Polymarket、加密货币 CoinGecko、Hugging Face 模型库、Docker Hub、Openverse 开放版权图库、OpenCorporates 全球公司注册、UN Comtrade 双边贸易、WHO 公共卫生通报。
- **twitter_syndication**：推文内容的合规直出通道。

## 抓取链新增三条直出通道（自动生效，无需配置）

- **站点根 `/llms.txt` 探测**：llms.txt 是站点为 AI 准备的自述索引（头部文档站采用中），抓文档站首页时自动探测命中。
- **页面 `.md` 直出**：开发者文档站页面 URL 加 `.md` 即返回 markdown 正文，命中即跳过整条反爬链。
- **r.jina.ai 阅读器级**：HTTP 直连和 TLS 指纹伪造都失败时，自动改走 jina 免费层（远端 JS 渲染转 markdown），省掉浏览器冷启动。只对公网 URL 生效，失败静默降级 Wayback/浏览器。

## 既有能力升级

- **stackexchange 引擎**：StackExchange 官方 API 一套接 180+ 问答站（此前 stackoverflow 引擎已支持 `site:` 站点族语法，行为不变）；编程问题类查询自动带上，匿名 300/天/IP，免费 key 可提额 10,000/天。
- **doi 引擎**：DOI 号直出结构化论文元数据（CSL JSON），免 key 免限次，`argo search "10.1145/xxx" --engine doi`。
- **知乎三源分工**：站内搜索 / 全网搜 / 个人数据（本人创作/收藏）三路语义分离，防饿死。
- **hedged 竞速**：慢源不再拖垮整体延迟。
- **路由依据可观测**：搜索输出新增 `route_reason` 讲清「为什么路由到这里」；不再报与实际执行无关的候选分。
- **Agent 消费档 `--fields agent`**：单次输出约 3.7KB（原 15KB），fetch_required 纪律字段保留。
- **本地搜索融合升级**：local-search 子技能接入主仓 RRF 融合与跨引擎去重，「同位次同分」退化修复。

## 框架修复

- **`--engine "a,b,c"` 逗号多引擎修复**：此前整串失效报「未知引擎」，现与 `--list-engines` 同口径拆分。
- **宏观数据域零结果修复**：「中国 2025 年 GDP 总量」类查询此前 0 结果收场；现在中国宏观查询由国家统计局前置（世界银行有 1-2 年数据滞后），全域零结果时恢复链自动用域声明兜底源补搜，救援引擎如实记入 engines_used。
- **HTTP 重定向中文修复**：部分站点 301 的 Location 带未编码中文（如汉典），二次请求不再报错。
- **TimeoutError 跨 Python 版本兼容**（3.9–3.14）；深度补搜的配额记账修正。
- **seek（本地文件搜索）降级链修复**：缺 rg 时正确落 grep，不再把「工具缺失」伪装成「没有结果」。
- **URL 归一化单一真源**：子技能与主仓共用一套规则，跨源去重不再发散。
- **失败显式化**：上游超时/被墙/欠费分型上报，不再「静默空结果」。

## 质量体系（本版起内置的回归门禁）

- 排序金标度量（MRR/nDCG 地板锁死）+ 融合增益消融门禁：多引擎融合相对最强单引擎的增益可量化（实测 +0.09 nDCG），排序改动「变好还是变坏」有数字可查。
- 路由负向控制矩阵：泛查询（做菜/健身/园艺）不得误入行情/漏洞/影视等垂直源。
- 上下文字节预算门禁覆盖主文档与全部子技能；可达性门 + 休眠台账防「新源接线了但永远选不中」。

## 默认关闭功能的打开方式

- **TinyFish 三引擎**（网页/新闻/论文，免费不限次）：配置 `ARGO_TINYFISH_API_KEY`（agent.tinyfish.ai/api-keys 申请）后 `--engine tinyfish[/_news/_paper]` 显式调用——设计上不进自动路由，护免费层。
- **jina 阅读器级**：默认开，`ARGO_FETCH_JINA=0` 关；`.md`/llms.txt 探测 `ARGO_FETCH_MD_VARIANT=0` 关。
- **stackexchange 提额**：免费注册 key 后在 engines/specs/stackexchange.yaml 的 extra_params 加 `key: "{ARGO_STACKEXCHANGE_API_KEY}"`。
- **专业搜索模式**（浏览器态子技能 ego-search）：默认关，需用户明确要求并执行开启指令。
- **Agent 消费档**：`argo search "..." --json --no-envelope --fields agent`。

## 相关依赖与已知边界

- 无新增第三方依赖（TLS 指纹伪造与 Chrome CDP 为可选增强，缺失自动降级）。
- r.jina.ai 免费层有速率限制，只代理公网 URL；对 Cloudflare 人机验证页无能为力，会正确升级浏览器层。
- stackexchange 匿名 300/天/IP 与全站共享（只作补位）；firecrawl 免费层 1000 credits/月（日常只当兜底）。
- reddit 内容需浏览器态（对非浏览器 UA/IP 返回 403）。
- FRED 上游偶发读超时（网络环境相关），恢复链自动换源补位。
- HKEXnews（港股公告）未收录：服务端已停返回数据，待恢复后单独立项。
