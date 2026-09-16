# 学术检索：查询构造与证据分级

> 机器负责取到，本文件负责「怎么构造查询、怎么判断哪篇算数」。
> 与 `research-protocol.md` 的取证/判断分工一致：**检索归 argo，判断归 Agent**。

适用引擎：`arxiv` `semantic_scholar` `google_scholar` `crossref` `openalex` `dblp` `europepmc`
`doaj` `clinicaltrials` `hal` `cnii` `dnb` `local_arxiv` `local_pubmed` `local_crossref`
`local_semantic_scholar`（共 17 个学术源）。

## 1. 查询构造

### 研究问题五分解

搜索前先把请求拆成：核心主题 / 子议题 / 方法偏好（实证·理论·综述）/ 时间窗 / 学科。

### 术语映射（通用词 → 学术词）

| 通用说法 | 学术等价词 |
|---|---|
| AI 聊天机器人 | large language model, conversational agent, dialogue system |
| 图像识别 | visual recognition, image classification, object detection |
| 数据隐私 | differential privacy, privacy-preserving, data protection |
| 脑部扫描 | neuroimaging, fMRI, MRI, EEG |
| 药物发现 | pharmacological screening, molecular docking, compound identification |
| 自动驾驶 | autonomous vehicle, automated driving |
| 假新闻 | misinformation detection, claim verification, fact-checking |

**原则**：用目标研究社群的术语。拿不准时，先取一篇已知相关论文的 Keywords 段反推。

### 各库语法要点

| 库 | 关键语法 |
|---|---|
| arXiv | 分类码 `cat:cs.LG`/`cat:cs.CL`；`ti:` 高精度、`abs:` 广召回、`all:` 仅保底；`ANDNOT` 排除；`sortBy=submittedDate`（求新）/ `relevance`（求准）；`max_results=15` |
| Semantic Scholar | `fieldsOfStudy` 学科过滤；`year=2022-2025` 区间；取 `influentialCitationCount`、`tldr`、`openAccessPdf`；`/paper/{id}/citations`、`/paper/{id}/references` |
| Google Scholar | `intitle:"..."`、`author:"..."`、`source:"..."`；`as_ylo`/`as_yhi` 年份；`"exact phrase"` |

## 2. 多库协议与去重

顺序：**Semantic Scholar 起步**（结构化元数据 + 引用图）→ **arXiv 补预印本**（48h 内新稿）→ **Google Scholar 保底**（小会/学位论文/技术报告）→ 交叉去重。

去重优先级：DOI → arXiv ID → 标题+第一作者+年份（模糊）。版本冲突时保留已发表版为主条目，附 arXiv 开放获取链接。

## 3. 相关性排序（五因子加权）

| 因子 | 权重 | 判据 |
|---|---|---|
| 主题相关性 | 35% | 是否正面回答用户问题；标题/摘要关键词重叠、方法匹配 |
| 方法严谨性 | 20% | 基线对比、标准数据集、显著性检验、消融、代码/数据可得性 |
| 发表场所 | 15% | 会议/期刊档次、是否同行评审 |
| 时效性 | 15% | 相对该领域迭代速度；快领域更重新 |
| 影响力 | 15% | **引用速度**（每年）与**高影响力引用数**，非总引用数 |

单因子 0–5 分，加权：`0.35×相关性 + 0.20×严谨 + 0.15×场所 + 0.15×时效 + 0.15×影响`。

调整项：综述在用户要"概览"时加权；奠基性论文在探索新领域时加权；预印本场所分 −1（知名团队或高引除外）；需全文且有免费 PDF 时 +0.5。

## 4. 引用网络挖掘（关键词搜不到时用）

- **前向** `/citations`：谁引了它 → 找最新延伸
- **后向** `/references`：它引了谁 → 找奠基论文、数据集与基准论文
- **文献耦合**：两文参考文献重叠 ≥30% → 高度相关
- **共引**：常被共同引用的两文相关（可用 S2 recommended 近似）
- **作者网络**：追踪高产作者及其合作者近期工作

## 5. 反模式（硬约束）

### 检索阶段

1. **禁自然语言提问** — 学术库按关键词匹配，禁用"什么是最好的…"这类句式
2. **禁单库依赖** — 至少查 2 个库；只查 arXiv 会漏已发表论文，只查 GS 会漏最新预印本
3. **禁过宽** — "machine learning" 无约束等于无结果
4. **禁过窄** — 过度术语化返回 0 篇时，用同义词 OR 逐步放宽
5. **禁术语串场** — CS 的 feature ≠ 医学的 biomarker，用 `fieldsOfStudy` 消歧

### 排序阶段

6. **禁唯引用数** — 用引用速度与高影响力引用数；2024 年 15 引可能胜过 2018 年 500 引
7. **禁只看声明结果** — 查基线、显著性、消融、可复现性
8. **禁唯新** — 探索新领域时至少纳入 1 篇奠基/综述
9. **禁唯 venue** — 突破性工作常先出现在 workshop 或 arXiv
10. **禁回声室** — 主动纳入不同团队/地域/方法传统的 1–2 篇

### 呈现阶段

11. **必须标发表状态** — `[预印本]` / `[同行评审-会议]` / `[同行评审-期刊]` / `[workshop]` / `[学位论文]`
12. **必须含完整元数据** — 作者（第一作者 et al.）、年份、venue、至少一个持久标识（DOI / arXiv ID / S2 Corpus ID）
13. **禁摘要堆砌** — 必须给 1–2 句关键发现 + 主题综合
14. **禁编造** — 只返回 API 实际召回的论文；不足 5 篇就如实说明，并附上所用查询供核验
15. **必须查开放获取** — arXiv 版 / `openAccessPdf` / 作者主页，无则标注付费墙

### 流程阶段

16. **禁不迭代** — top5 不足 3 篇直接相关时，用同义词扩展或换分类码重试，最多 2 轮
17. **禁忽略引用图** — top1–2 必做前向+后向滚雪球
18. **禁重复计数** — 预印本与已发表版按同一篇处理

## 6. 与 argo 证据体系的保持一致

| 学术检索情形 | argo 归类 |
|---|---|
| 已发表的同行评审结论 | **事实**（可回源核验） |
| 预印本结论 | **推断**——须显式标注 `[预印本]`，未同行评审 |
| 引用数/venue 推断影响力 | **推断**，写清计算方式（总引用 vs 引用速度 vs 高影响力引用） |
| 单篇论文的结论外推到领域共识 | **未知**——除非多篇独立复现 |

- 论文类问题命中高后果域时，`fetch_required=true`，先 `argo_fetch` 取正文（或摘要页）再下判断。
- 冲突处理沿用研究协议：**并列不同计算方式，禁止未保持一致计算方式就合并**；不靠"多数来源"定真值。
- 不得把 SERP 链（baidu/s、sogou/link）当作论文正文来源。

## 7. 输出条目模板

```
[N]. 标题
作者: 第一作者 et al. (年份)
Venue: 会议/期刊名 [同行评审 / 预印本]
引用: X 总, Y 高影响力 | 引用速度: Z/年
标识: arXiv:XXXX.XXXXX | DOI:10.XXXX/XXXXX
开放获取: [是-链接] / [否-付费墙]
关键发现: 1–2 句
相关性: 为何回应用户问题
```

综合部分：主题分组 / 共识 / 矛盾 / 研究空白 / 建议阅读顺序。
