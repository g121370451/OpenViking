# OpenViking RAG Benchmark 改进报告

## 日期

2026-05-12

## 模型

doubao-seed-1-8-251228（字节跳动火山引擎）

---

## 一、Relations 查询方式演变历程

Relations 是文档间的关联边，用于增强检索：当 bot 读取文档 A 时，可以通过 relations 发现与 A 相关的文档 B、C，从而减少搜索轮次、提高召回率。

以下是 relations 查询方式的 5 次迭代演变：

### 1.1 阶段1：Read 时查询，追加 L2 内容，

**做法**：bot 读取文档时，自动查询该文档的 relations 节点，将关联文档的 L2（完整内容）追加到当前文档后面。

```
read doc_a → content of doc_a
             + --- Related document (from relations) ---
             + content of doc_c(L2)
             + content of doc_d(L2)
```

**问题**：token 消耗大。多轮 read 都会追加大量关联文档内容，即使 bot 并不需要这些文档。去重即可修复。

### 1.2 阶段2：Bot 筛选有效文档

**做法**：在追加 relations 内容前，让 bot 判断哪些关联文档对当前问题有效，将有效的search_uri和有效的read_uri做绑定，
```
search query_1 → doc_a uri
             + doc_b uri
             + doc_c uri
read doc_c
link → doc_a to doc_c
```
### 1.3 阶段3：Bot 筛选有效文档作为to_uri，search工具中的全部result_uri作为from_uri

**做法**：在bot返回结果之前，让 bot 判断文档对当前问题有效，将有效的文档作为to_uri，全部的search_uri作为from_uri生成relations，

### 1.4 阶段4：Search + Read 同时查询relations

**做法**：search 和 read 阶段都查询 relations，希望通过 search 阶段提前暴露关联文档，让 bot 在 read 时减少重复查询。

**问题**：没有达到预期效果，两处同时查询反而增加了复杂度和 token 消耗。

### 1.5 阶段6：Search 查询，Read 从结果中筛选

**做法**：只在 search 阶段查询 relations，返回关联文档的 URI 和摘要（L0）。bot 在后续 read 时从 search 结果中筛选有效文档来读取。

**问题**：因为bot会更改问题， search_query的 问题与 relations 返回的文档摘要（L0）语义不匹配，bot 认为这些文档不相关，不会主动读取 relations 节点。

### 1.6 阶段6（最终方案）：Search 查询 + 强制优先读取

**做法**：search 阶段查询 relations，搜索结果分为 **PRIORITY** 和 **SEARCH RESULTS** 两组。bot 被强制优先读取 PRIORITY 组中的 relations 文档。

```
search "question" →
  ┌─ PRIORITY (from relations) ─────────────────┐
  │  doc_c (related to doc_a via relations)      │
  │  doc_d (related to doc_b via relations)      │
  └─────────────────────────────────────────────┘
  ┌─ SEARCH RESULTS (from vector search) ───────┐
  │  doc_a (top-1)                               │
  │  doc_b (top-2)                               │
  │  ...                                         │
  └─────────────────────────────────────────────┘
```

**效果**：
- 有效降低迭代轮次（bot 快速获取关联上下文）
- token 消耗降低, （只在 search 阶段查询，并且如果因为降低了轮次，tokens消耗会降低)
- bot 不会跳过 relations 文档（强制优先读取）, 保证效果

**建边逻辑**：
- `from_uri`：bot 从 search 返回的 URI 中
- `to_uri`：read 中 bot 筛选对结果有帮助的文档
- 建立 from_uri → to_uri 的链接

---

## 二、系统架构（当前版本）

### 2.1 Relations 使用流程

```
┌─────────────────────────────────────────────────────────┐
│ VikingSearchTool.execute()                              │
│  1. search搜索问题的相关文档                              │
│  2. 对 top-5 结果查询 relations 节点                     │
│  3. 输出分组：PRIORITY (relations) + SEARCH RESULTS      │
└─────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────┐
│ VikingMultiReadTool.execute()                           │
│  1. bot 强制优先读取 PRIORITY 组文档                     │
│  2. 读取其他需要的文档                                   │
│  3. 纯文档读取，不再查询 relations                       │
└─────────────────────────────────────────────────────────┘
```

**配置**：
- `VIKINGBOT_USE_RELATIONS=1` — 启用 relations
- `VIKINGBOT_LINK_STRATEGY` — 选择链接策略

**关键文件**：
- `bot/vikingbot/agent/tools/ov_file.py` — VikingSearchTool 中 relations 查询实现
- `bot/vikingbot/agent/loop.py` — 循环控制、强制读取逻辑

### 2.2 Post-Answer LLM Review 建边

回答完成后，系统额外调用一次 LLM 判断哪些 READ 文档对回答有用：

```
┌─────────────────────────────────────────────────────────┐
│ Post-Answer Review (loop.py)                            │
│  1. 收集所有 search URI → from_uri 候选                  │
│  2. 收集所有 read URI → to_uri 候选                      │
│  3. LLM 判断哪些 read 文档对回答有帮助                    │
│  4. 建立 from_uri → useful_to_uri 的链接                 │
│  5. 如果 not_answer=True（达到最大迭代），跳过 review建边  │
└─────────────────────────────────────────────────────────┘
```

**关键文件**：
- `bot/vikingbot/agent/loop.py` — `_post_answer_link()` 中的 review 逻辑
- `bot/vikingbot/agent/link_strategies.py` — `LLMReviewLinkStrategy`

### 2.3 Reasoning 模式

`VIKINGBOT_ENABLE_REASONING=1` 启用推理模式，让 bot 使用 reasoning 能力提高准确率。

当 `enable_reasoning=0` 时，系统强制先执行一次 search（使用原始问题），再进入正常循环。这确保 bot 在非推理模式下也能获得初始搜索结果。

**配置**：
```yaml
vikingbot:
  enable_reasoning: true
```

### 2.4 Memory 工具移除

删除了 `openviking_memory_commit` 工具，system prompt 中移除了 Memory 相关指令。bot 不再需要在对话中主动提交记忆，建边逻辑完全由 post-answer review 接管。 

### 2.5 删除了grep list glob等工具

删除了 `grep list glob` 工具，system prompt 中移除了 Memory 相关指令。发现这些工具对结果没有影响。

---

## 四、Benchmark 指标

### 4.1 迭代指标

```json
"VikingBot Iteration Metrics": {
    "Average Total Iterations": 4.04,
    "Average Retrieval Iterations (excl. link/relations)": 4.04,
    "Average Search Iterations": 1.49,
    "Average Read Iterations": 1.16,
    "Min Retrieval Iterations": 3,
    "Max Retrieval Iterations": 22
}
```

### 4.2 Relations 指标

- `relation_edges_hit`：relations 命中率（search 阶段查询到的 relations 边数 / 总 relations 边数）
- `links_created`：本次回答建立的新链接数

---

## 五、实验结果（2026-05-12）

![alt text](charts/output/chart_accuracy.png) ![alt text](charts/output/chart_iterations.png) ![alt text](charts/output/chart_retrieval_cost.png) ![alt text](charts/output/chart_retrieval_time.png)

### 5.1 Agentic 模式对比（unlearned vs learned）

| 数据集 | unlearned 准确率 | learned 准确率 | 变化 | 迭代轮次变化 | Token 消耗变化 | 耗时变化 |
|--------|---------|---------|------|------|------|------|
| FinanceBench | 0.85 | 0.86 | +1% | 4.21→4.05 (-4%) | 39,765→29,660 (-25%) | 77s→78s (+1%) |
| HotpotQA | 0.90 | 0.91 | +1% | 4.25→3.54 (-17%) | 33,604→24,245 (-28%) | 47s→45s (-4%) |
| Qasper | 0.835 | 0.835 | = | 3.94→3.63 (-8%) | 27,019→21,861 (-19%) | 70s→56s (-20%) |
| SyllabusQA | 0.81 | 0.88 | +9% | 4.05→3.69 (-9%) | 27,669→22,823 (-18%) | 55s→60s (+9%) |
| NQ | 0.90 | 0.92 | +2% | 3.30→3.22 (-2%) | 20,519→19,670 (-4%) | 50s→48s (-4%) |

### 5.2 Non-Agentic 模式对比（unlearned vs learned）

| 数据集 | unlearned 准确率 | learned 准确率 | 变化 | Token 消耗变化 |
|--------|---------|---------|------|------|
| FinanceBench | 0.61 | 0.77 | +26% | 5,593→9,683 (+73%) |
| HotpotQA | 0.70 | 0.85 | +21% | 4,045→4,593 (+14%) |
| Qasper | 0.75 | 0.89 | +19% | 3,370→4,627 (+37%) |
| SyllabusQA | 0.61 | 0.83 | +36% | 4,054→7,935 (+96%) |
| LocoMo | 0.64 | 0.70 | +9% | 5,753→7,257 (+26%) |
| NQ | 0.89 | 0.90 | +1% | 4,617→5,837 (+26%) |

### 5.3 关键结论

1. **Relations 对准确率有正向提升**：所有数据集准确率持平或提升，SyllabusQA 提升最显著（+9% agentic / +36% non-agentic）
2. **迭代轮次和 Token 消耗普遍降低**：HotpotQA 迭代 -17%、Token -28%；Qasper 迭代 -8%、Token -19%
3. **Non-Agentic 模式提升更显著**：准确率提升 +9%~+36%，因为非 bot 模式没有多轮搜索能力，relations 直接弥补了这一短板
4. **Non-Agentic Token 消耗增加是预期行为**：relations 额外返回了更多文档供 LLM 阅读，用 token 换准确率

---

## 六、Good Case / Bad Case 分析

### 6.1 Bad Case 1：bot自主选择from_uri, 导致创建的无效Relations

**场景**：查询 "Adobe FY2015 cash from operations total current liabilities"，FinanceBench 数据集。

**旧版行为**（bot_top_1，无预建 relations）：

| 轮次 | 工具 | 结果 |
|------|------|------|
| Iter 1 | `openviking_search("Adobe FY2015 cash from operations total current liabilities", target="viking://resources/")` | 返回 12 篇文档，全是 `Summary_of_Trademarks`、`Stock_Performance_Graph` 等无关内容，`relations_found=0` |
| Iter 2 | `openviking_search("total current liabilities FY2015 balance sheet", target="viking://resources/pdfs/ADOBE_2015_10K/")` | 返回 9 篇，读到了`Stock_Performan ce_Graph_59`和`Stock_Performan ce_Graph_67`和`Stock_Performan ce_Graph_71`但是search还是搜索了两轮 |
| Iter 3 | `openviking_multi_read([Graph_71, Graph_67])` | 读取两篇 Stock_Performance_Graph |
| Post | `openviking_link` | 创建两条 relations：`Graph_59 → Graph_67`，`Graph_59 → Graph_71` |

**问题分析**：第二轮 targeted search 虽然自己找到了正确文档，但这轮 search 的向量结果中 `Graph_59` 和他相关的文档 `Graph_67`、 `Graph_71`本来就应该在这轮被返回。

两条 link 形同虚设：
```
Graph_59 → Graph_67
Graph_59 → Graph_71
```

**新版行为**（bot_relations_review，有预建 relations）：

| 轮次 | 工具 | 结果 |
|------|------|------|
| Iter 1 | `openviking_search("Adobe FY2015 cash from operations total current liabilities", target="viking://resources/")` | `relations_found=2`，PRIORITY 组直接标记了 `Graph_67`（match_reason: "contains FY2015 cash from operations and total current liabilities data"）和 `Graph_71` |
| Iter 2 | `openviking_multi_read([Graph_67, Graph_71, ...])` | 一次 multi_read 读到正确文档，无需再次 search |

**关键差异**：

| 维度 | 旧版 | 新版 |
|------|------|------|
| 找到正确答案 | 需要 2 次 search | 1 次 search 命中预建 relations |
| 后续查询价值 | 无效入口，from和to在同一轮 | 第一轮的search_result就指向`Graph_67`和`Graph_71` |

### 6.2 Bad Case 2：首轮 Search 无结果——Relations 无法参与，搜索轮次无法降到 1

**场景**：查询 "Amcor"，bot 第一轮使用大写 "AMCOR" 搜索。

```json
[
  {
    "tool_name": "openviking_search",
    "args": {"query": "AMCOR", "target_uri": "viking://resources/"},
    "result": "No results found for query: AMCOR"
  },
  {
    "tool_name": "openviking_search",
    "args": {"query": "Amcor", "target_uri": "viking://resources/"},
    "relations_found": 3,
    "result": [
      {"uri": "...AMCOR_2019_10K/AMCOR_PLC/AMCOR_PLC_1.md"},
      {"uri": "...AMCOR_2020_10K/AMCOR_2020_10K_3more.md"},
      {"uri": "...AMCOR_2019_10K/AMCOR_PLC/AMCOR_PLC_4.md",
       "match_reason": "relation_from: AMCOR_PLC_1.md, ...contains the Business section..."},
      {"uri": "...AMCOR_2022_10K/FORM_10-K/FORM_10-K_51.md",
       "match_reason": "relation_from: AMCOR_2020_10K_3more.md, ...provides FY2022 figures..."},
      {"uri": "...AMCOR_2023_10K/FORM_10-K/FORM_10-K_74.md",
       "match_reason": "relation_from: AMCOR_2020_10K_3more.md, ...provides FY2023 figures..."}
    ]
  }
]
```

**问题分析**：

第一轮 `openviking_search("AMCOR")` 返回 `"No results found"`——命中不到任何文档。

链式失效：
1. **Search 无结果 → relations 对第一轮完全失效**：search 返回 0 个 URI，没有任何节点用来查询 relations，`relations_found` 无法被填充
2. **第一轮无 URI → post-answer linking 对第一轮无贡献**：`from_uri` 集合为空，第一轮 search 不产生任何 link，对后续查询无帮助
3. **搜索轮次至少为 2**：无论 relations 质量多高，bot 必须执行第二轮 search。体系的轮次下限被锁死在 2

### 6.3 Bad Case 3：Bot 建的 Relations 对非 Bot 模式无效——Search 结果不重合

**场景**：查询 "3M debt securities registered to trade on NYSE"，bot 和非 bot 的向量搜索返回了完全不同的文档集。

**非 bot 的 search 结果**：
```json
[
  "viking://resources/pdfs/3M_2022_10K/3M_COMPANY/3M_COMPANY_60.md",
  "viking://resources/pdfs/3M_2019_10K/3M_COMPANY/3M_COMPANY_44.md",
  "viking://resources/pdfs/3M_2022_10K/3M_COMPANY/3M_COMPANY_37.md",
  "viking://resources/pdfs/3M_2021_10K/3M_COMPANY/3M_COMPANY_176.md",
  "viking://resources/pdfs/3M_2022_10K/3M_COMPANY/3M_COMPANY_166.md"
]
```

**Bot 的 search 结果**：
```json
[
  "viking://resources/pdfs/3M_2023Q2_10Q/3M_2023Q2_10Q.md",
  "viking://resources/pdfs/3M_2018_10K/3M_COMPANY/3M_COMPANY_13.md",
  "viking://resources/pdfs/3M_2017_10K/3M_COMPANY/3M_COMPANY_109.md",
  "viking://resources/pdfs/3M_2023Q2_10Q/3M_COMPANY/3M_COMPANY_20.md",
  "viking://resources/pdfs/3M_2022_10K/3M_COMPANY/3M_COMPANY_94.md",
  "viking://resources/pdfs/3M_2023Q2_10Q/3M_COMPANY/3M_COMPANY_111.md",
  "viking://resources/pdfs/3M_2023Q2_10Q/3M_COMPANY/3M_COMPANY_66.md"
]
```

**Bot 建的 relations**：
```json
[
  {
    "from": "*",
    "to": "viking://resources/pdfs/3M_2023Q2_10Q/3M_2023Q2_10Q.md",
    "reason": "...contains the list of 3M's debt securities registered to trade on NYSE as of Q2 2023..."
  }
]
```

**问题分析**：

两个集合几乎不重叠——非 bot 搜到的是 10K 年报（2021/2022/2019），bot 搜到的是 10Q 季报（2023Q2）和更早的年报（2017/2018）。

1. **非 bot 的 search 返回的是 2021/2022/2019 10K 文档**——这些不在 bot 建的 relations 入口中
2. **正确答案 `3M_2023Q2_10Q.md` 对非 bot 不可达**：非 bot 搜不到它，relations 的入口又不是非 bot search 结果中的任何 URI

### 6.4 Bad Case 4：Bot 只 Search 不 Read

**场景**：HotpotQA index=15，bot 连续 3 轮 search 后直接回答，从未调用 read。

```json
[
  {
    "tool_name": "openviking_search",
    "args": {"query": "Brown State Fishing Lake", "target_uri": "viking://resources/"},
    "relations_found": 0,
    "result": [
      {"uri": "...Brown_State_Fishing_Lake_doc.md"},
      {"uri": "...Osage_State_Fishing_Lake_doc.md"},
      ...
    ]
  },
  {
    "tool_name": "openviking_search",
    "args": {"query": "United States population", "target_uri": "viking://resources/"},
    "result": [{"uri": "...Office_of_Population_Affairs_1.md"}, ...]
  },
  {
    "tool_name": "openviking_search",
    "args": {"query": "United States total population", "target_uri": "viking://resources/"},
    "result": [{"uri": "...Office_of_Population_Affairs_1.md"}, ...]
  }
]
```

bot 基于 search 的 L0 摘要直接回答，从未调用 `openviking_read`。

**问题分析**：

1. **没有 Read → 无法建边**：post-answer review 需要 `read_uris` 作为 `to_uri` 候选。Read 为空，review 步骤无文档可筛选，无法产生任何 link
2. **Coverage 永远达不到 100%**：只要存在只 search 不 read 的样本，`build_links` 就会漏掉它们
3. **与工具集无关**：早期 `list`、`grep` 等工具还在时，bot 遇到这类问题也只调 search。不是缺少工具，是 bot 认为 L0 摘要足够回答，但是实际上它没答对。

### 6.5 Bad Case 5：Review LLM 解析失败——静默跳过建边

**场景**：Locomo conv-26，"When did Melanie paint a sunrise?"

Bot 正常执行了 search → multi_read → 回答，`iterations_used=3`。但最终 `tool_calls` 中没有 `openviking_link`，同批次其他样本均成功建边。

**根因**：`_run_agent_loop` 中的 review LLM 步骤（`loop.py:557-568`）：

```python
useful_docs = []
if json_match:
    try:
        useful_docs = json.loads(json_match.group())
    except json.JSONDecodeError:
        logger.warning(...)

if useful_docs:  # 只有非空才建边
    ...  # 创建 link 并 append 到 tools_used
```

review LLM 返回的内容无法解析为有效 JSON 数组 → `useful_docs` 为空 → 整个建边块静默跳过。没有 exception，没有 error log。LLM 调用本身成功（无异常），但输出格式不符合预期，代码对此没有任何日志。

**影响**：这个样本 search 和 read 都执行了，但不产出任何 relations。后续类似问题的查询无法受益。

### 6.6 Good Case：Relations 将多跳问题从 6 轮降到 2 轮

**场景**：HotpotQA，"The 2017–18 Wigan Athletic F.C. season will be a year in which the team competes in the league cup known as what for sponsorship reasons?"

这是一个典型的多跳问题：先找到 Wigan Athletic 2017-18 赛季参加了 EFL Cup，再找到 EFL Cup 的赞助商名称是 Carabao Cup。

**无 relations 时的行为**（6 轮迭代，32852 input tokens）：

```json
[
  {"tool_name": "openviking_search", "args": {"query": "2017–18 Wigan Athletic F.C. season league cup sponsorship name"}},
  {"tool_name": "openviking_multi_read", "args": {"uris": ["...2017_18_Wigan_Athletic_F_C__season_doc.md"]}},
  {"tool_name": "openviking_search", "args": {"query": "2017–18 EFL Cup sponsorship name"}},
  {"tool_name": "openviking_multi_read", "args": {"uris": ["...EFL_Cup/EFL_Cup_5.md"]}},
  {"tool_name": "openviking_multi_read", "args": {"uris": ["...EFL_Cup/EFL_Cup_1.md"]}}
]
```

Bot 需要 2 次 search + 3 次 read = 6 轮迭代才能完成多跳推理。

**有 relations 时的行为**（2 轮迭代，11460 input tokens）：

在之前的问答中，系统建立了 relations：
```json
{
  "from_uris": ["...Ed_Wood_film_1.md", "...Ed_Wood_film_2.md", "...Scott_Derrickson_doc.md", ...],
  "to_uris": ["...Ed_Wood/Ed_Wood_1.md", "...Scott_Derrickson_doc.md"]
}
```

新问题搜索时，`relations_found=9`，PRIORITY 组直接包含了关键文档：

```json
[
  {"tool_name": "openviking_search", "args": {"query": "Scott Derrickson Ed Wood nationality"},
   "relations_found": 9,
   "result": [
     {"uri": "...Ed_Wood_film_2.md"},
     {"uri": "...Ed_Wood_film_1.md", "match_reason": "relation_from: ...Ed_Wood_film_2.md"},
     {"uri": "...Scott_Derrickson_doc.md", "match_reason": "relation_from: ...Ed_Wood_film_2.md"},
     ...
   ]},
  {"tool_name": "openviking_multi_read", "args": {"uris": ["...Scott_Derrickson_doc.md", "...Ed_Wood_1.md"]}}
]
```

1 次 search + 1 次 read = 2 轮迭代，直接完成。

**对比**：

| 维度 | 无 relations | 有 relations |
|------|------|------|
| 迭代轮次 | 6 | 2 |
| Search 次数 | 2 | 1 |
| Read 次数 | 3 | 1 |
| Input tokens | 32,852 | 11,460 (-65%) |
| Output tokens | 1,284 | 677 (-47%) |

**为什么有效**：Relations 把多跳问题的第二跳文档直接通过 PRIORITY 暴露给 bot，省去了"读完第一个文档 → 发现需要第二个文档 → 再搜索 → 再读取"的完整链路。


下一步要做的事 消除relations建边无效的问题，让relations的覆盖率除了全search之外100%, 或者这里加一个判断 如果全search就让bot在search中挑选有用的abstract，归根结底就是要降低轮次嘛！

