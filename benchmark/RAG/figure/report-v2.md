# OpenViking RAG Benchmark 改进报告

## 日期

2026-05-19

## 模型

doubao-seed-1-8-251228（字节跳动火山引擎）

---

## 一、Relations 查询方式演变历程

Relations 是文档间的关联边，用于增强检索：当 bot 读取文档 A 时，可以通过 relations 发现与 A 相关的文档 B、C，从而减少搜索轮次、提高召回率。

以下是 relations 查询方式的迭代演变：

### 1.1 阶段1：Read 时查询，追加 L2 内容

**做法**：bot 读取文档时，自动查询该文档的 relations 节点，将关联文档的 L2（完整内容）追加到当前文档后面。

```
read doc_a → content of doc_a
             + --- Related document (from relations) ---
             + content of doc_c(L2)
             + content of doc_d(L2)
```

**问题**：token 消耗大。多轮 read 都会追加大量关联文档内容，即使 bot 并不需要这些文档。

### 1.2 阶段2：Bot 筛选有效文档

**做法**：在追加 relations 内容前，让 bot 判断哪些关联文档对当前问题有效，将有效的search_uri和有效的read_uri做绑定。

```
search query_1 → doc_a uri
             + doc_b uri
             + doc_c uri
read doc_c
link → doc_a to doc_c
```

### 1.3 阶段3：Bot 筛选有效文档作为to_uri，search工具中的全部result_uri作为from_uri

**做法**：在bot返回结果之前，让 bot 判断文档对当前问题有效，将有效的文档作为to_uri，全部的search_uri作为from_uri生成relations。

### 1.4 阶段4：Search + Read 同时查询relations

**做法**：search 和 read 阶段都查询 relations，希望通过 search 阶段提前暴露关联文档，让 bot 在 read 时减少重复查询。

**问题**：没有达到预期效果，两处同时查询反而增加了复杂度和 token 消耗。

### 1.5 阶段5：Search 查询，Read 从结果中筛选

**做法**：只在 search 阶段查询 relations，返回关联文档的 URI 和摘要（L0）。bot 在后续 read 时从 search 结果中筛选有效文档来读取。

**问题**：因为bot会更改问题，search_query的问题与 relations 返回的文档摘要（L0）语义不匹配，bot 认为这些文档不相关，不会主动读取 relations 节点。

### 1.6 阶段6：Search 查询 + 强制优先读取

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
- token 消耗降低（只在 search 阶段查询，降低轮次即降低 token）
- bot 不会跳过 relations 文档（强制优先读取），保证效果

**建边逻辑**：
- `from_uri`：bot 从 search 返回的 URI
- `to_uri`：read 中 bot 筛选对结果有帮助的文档
- reason：`"Question: X | Searched: Y, Z"`（仅记录搜了什么）
- 建立 from_uri → to_uri 的链接

**问题**：reason 只记录"搜过什么"，bot 下次遇到相似问题时不知道"怎么从 from_uri 找到 to_uri 的"，仍需重复 read、grep 等中间操作。且 relations 查询是单跳的，无法发现间接关联文档。

### 1.7 阶段7（当前方案）：BFS 多跳递归 + Per-Edge 执行路径摘要

**做法**：在阶段6基础上两项改进：

1. **Relations 查询改为 BFS 多跳递归**：每轮新发现的 URI 作为下一轮种子继续查 relations，直到没有新 URI。可发现间接关联文档（A→B→C）。

2. **Per-Edge 执行路径摘要替代旧 reason**：每条边记录 bot 从 from_uri 到 to_uri 的具体执行路径（纯字符串拼接，无额外 LLM 调用）：

```
Q: 什么是 RAG？
Path: search("RAG definition") -> read(viking://resources/doc1.md) -> grep("retrieval") -> L42: RAG combines retrieval with generation
Answer: RAG 是检索增强生成，它将检索系统与大语言模型结合...
```

Bot 下次遇到相似问题时，看到 Path 即可直接复现结果，跳过中间迭代步骤。

**效果**：
- 多跳覆盖更广（间接关联文档也能被发现）
- 建边零额外 LLM 调用
- Bot 看到执行路径后可跳过已执行步骤，进一步降低迭代轮次

---

## 二、系统架构（当前版本）

### 2.1 Relations 使用流程（BFS 多跳递归）

```
┌─────────────────────────────────────────────────────────┐
│ VikingSearchTool.execute()                              │
│  1. search搜索问题的相关文档                              │
│  2. BFS 多跳递归查询 relations 节点                      │
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

**Relations 递归展开（BFS 多跳）**：

旧版 relations 查询是单跳的——search 结果的每个 URI 查一次 `client.relations()`，只能发现直接关联的文档。新版改为 BFS 循环：每轮新发现的 URI 作为下一轮种子继续查 relations，直到没有新 URI 为止。

```
第1轮: search结果 [A, B, C] → 查relations → 发现 [D, E]
第2轮: [D, E] → 查relations → 发现 [F]
第3轮: [F] → 查relations → 无新URI → 结束
最终 PRIORITY: [D, E, F]
```

无需深度限制：`relations()` 方法已有 embedding 相似度 > 0.7 的过滤，天然防止引入无关内容。`seen_uris` 集合防止重复访问和无限循环。

Bot 模式（`ov_file.py`）和非 Bot 模式（`vector_store_with_relations.py`）均已实现 BFS 多跳。

**配置**：
- `VIKINGBOT_USE_RELATIONS=1` — 启用 relations
- `VIKINGBOT_LINK_STRATEGY` — 选择链接策略

**关键文件**：
- `bot/vikingbot/agent/tools/ov_file.py` — VikingSearchTool 中 relations 查询实现
- `bot/vikingbot/agent/loop.py` — 循环控制、强制读取逻辑
- `benchmark/RAG/src/core/vector_store_with_relations.py` — 非 bot 的 retrieve 方法

### 2.2 Post-Answer LLM Review 建边（Per-Edge 执行路径摘要）

回答完成后，系统额外调用一次 LLM 判断哪些 READ 文档对回答有用：

```
┌─────────────────────────────────────────────────────────┐
│ Post-Answer Review (loop.py)                            │
│  1. 收集所有 search URI + grep URI → from_uri 候选       │
│  2. 收集所有 read URI → to_uri 候选                      │
│  3. LLM 判断哪些 read 文档对回答有帮助                    │
│  4. 为每条边生成 per-edge 执行路径摘要作为 reason          │
│  5. 建立 from_uri → useful_to_uri 的链接                 │
│  6. 如果 not_answer=True（达到最大迭代），跳过 review建边  │
└─────────────────────────────────────────────────────────┘
```

**Per-Edge 执行路径摘要**：

每条边的 reason 记录 bot 从 from_uri 到 to_uri 的具体执行路径（纯字符串拼接，无额外 LLM 调用）：

```
Q: 什么是 RAG？
Path: search("RAG definition") -> read(viking://resources/doc1.md) -> grep("retrieval") -> L42: RAG combines retrieval with generation
Answer: RAG 是检索增强生成，它将检索系统与大语言模型结合...
```

构建逻辑：遍历 `tools_used`，提取与 `tgt_uri` 相关的工具调用（search 结果包含该 URI、read 参数包含该 URI、grep 结果包含该 URI），拼接为执行链。Bot 下次遇到相似问题时，看到 Path 即可直接复现结果，跳过中间迭代步骤。

**关键文件**：
- `bot/vikingbot/agent/loop.py` — `_build_edge_reason()` 函数 + review 逻辑
- `bot/vikingbot/agent/link_strategies.py` — `LLMReviewLinkStrategy`

### 2.3 Grep 工具（结构化输出 + 参与建边）

grep 工具返回结构化格式，按 URI 分组展示匹配行：

```
[viking://resources/doc1.md]
  L42: RAG combines retrieval with generation
  L58: The retrieval component fetches relevant documents
[viking://resources/doc2.md]
  L15: Augmented generation improves accuracy
```

同时通过 `tool_context.structured_result` 输出 list of dicts（`{uri, line, content, pattern}`），供 post_link 解析。grep 发现的 URI 参与 relations 建边（作为 `read_uris` 的一部分），确保 grep 路径上的文档也能被后续问题通过 relations 直接找到。

### 2.4 Reasoning 模式

`VIKINGBOT_ENABLE_REASONING=1` 启用推理模式，让 bot 使用 reasoning 能力提高准确率。

当 `enable_reasoning=0` 时，系统强制先执行一次 search（使用原始问题），再进入正常循环。这确保 bot 在非推理模式下也能获得初始搜索结果。

### 2.5 Memory 工具移除

删除了 `openviking_memory_commit` 工具，system prompt 中移除了 Memory 相关指令。bot 不再需要在对话中主动提交记忆，建边逻辑完全由 post-answer review 接管。

---

## 三、Benchmark 指标

### 3.1 迭代指标

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

### 3.2 Relations 指标

- `relation_edges_hit`：relations 命中率（search 阶段查询到的 relations 边数 / 总 relations 边数）
- `links_created`：本次回答建立的新链接数

---

## 四、实验结果（2026-05-12）

![alt text](charts/output/chart_accuracy.png) ![alt text](charts/output/chart_iterations.png) ![alt text](charts/output/chart_retrieval_cost.png) ![alt text](charts/output/chart_retrieval_time.png)

### 4.1 Agentic 模式对比（unlearned vs learned）

| 数据集 | unlearned 准确率 | learned 准确率 | 变化 | 迭代轮次变化 | Token 消耗变化 | 耗时变化 |
|--------|---------|---------|------|------|------|------|
| FinanceBench | 0.85 | 0.86 | +1% | 4.21→4.05 (-4%) | 39,765→29,660 (-25%) | 77s→78s (+1%) |
| Qasper | 0.835 | 0.835 | = | 3.94→3.63 (-8%) | 27,019→21,861 (-19%) | 70s→56s (-20%) |
| SyllabusQA | 0.81 | 0.88 | +9% | 4.05→3.69 (-9%) | 27,669→22,823 (-18%) | 55s→60s (+9%) |
| LocoMo | 0.74 | 0.83 | +12% | 3.56→3.35 (-6%) | 22,335→17,830 (-20%) | 43s→37s (-14%) |
| NQ | 0.90 | 0.92 | +2% | 3.30→3.22 (-2%) | 20,519→19,670 (-4%) | 50s→48s (-4%) |
| HotpotQA | 0.90 | 0.91 | +1% | 4.25→3.54 (-17%) | 33,604→24,245 (-28%) | 47s→45s (-4%) |

### 4.2 Non-Agentic 模式对比（unlearned vs learned）

| 数据集 | unlearned 准确率 | learned 准确率 | 变化 | Token 消耗变化 |
|--------|---------|---------|------|------|
| FinanceBench | 0.61 | 0.77 | +26% | 5,593→9,683 (+73%) |
| HotpotQA | 0.70 | 0.85 | +21% | 4,045→4,593 (+14%) |
| Qasper | 0.75 | 0.89 | +19% | 3,370→4,627 (+37%) |
| SyllabusQA | 0.61 | 0.83 | +36% | 4,054→7,935 (+96%) |
| LocoMo | 0.64 | 0.70 | +9% | 5,753→7,257 (+26%) |
| NQ | 0.89 | 0.90 | +1% | 4,617→5,837 (+26%) |

### 4.3 关键结论

1. **Relations 对准确率有正向提升**：所有数据集准确率持平或提升，LocoMo 提升最显著（+12% agentic / +9% non-agentic），SyllabusQA 紧随其后（+9% agentic / +36% non-agentic）
2. **迭代轮次和 Token 消耗普遍降低**：HotpotQA 迭代 -17%、Token -28%；LocoMo Token -20%；Qasper 迭代 -8%、Token -19%
3. **Non-Agentic 模式提升更显著**：准确率提升 +9%~+36%，因为非 bot 模式没有多轮搜索能力，relations 直接弥补了这一短板
4. **Non-Agentic Token 消耗增加是预期行为**：relations 额外返回了更多文档供 LLM 阅读，用 token 换准确率

---

## 五、Good Case / Bad Case 分析

### 5.1 Bad Case 1：bot自主选择from_uri，导致创建的无效Relations

**场景**：查询 "Adobe FY2015 cash from operations total current liabilities"，FinanceBench 数据集。

**旧版行为**（bot_top_1，无预建 relations）：

| 轮次 | 工具 | 结果 |
|------|------|------|
| Iter 1 | `openviking_search("Adobe FY2015 cash from operations total current liabilities", target="viking://resources/")` | 返回 12 篇文档，全是 `Summary_of_Trademarks`、`Stock_Performance_Graph` 等无关内容，`relations_found=0` |
| Iter 2 | `openviking_search("total current liabilities FY2015 balance sheet", target="viking://resources/pdfs/ADOBE_2015_10K/")` | 返回 9 篇，读到了`Stock_Performance_Graph_59`和`Stock_Performance_Graph_67`和`Stock_Performance_Graph_71`但是search还是搜索了两轮 |
| Iter 3 | `openviking_multi_read([Graph_71, Graph_67])` | 读取两篇 Stock_Performance_Graph |
| Post | `openviking_link` | 创建两条 relations：`Graph_59 → Graph_67`，`Graph_59 → Graph_71` |

**问题分析**：第二轮 targeted search 虽然自己找到了正确文档，但这轮 search 的向量结果中 `Graph_59` 和他相关的文档 `Graph_67`、`Graph_71`本来就应该在这轮被返回。两条 link 形同虚设。

### 5.2 Bad Case 2：首轮 Search 无结果——Relations 无法参与

**场景**：查询 "Amcor"，bot 第一轮使用大写 "AMCOR" 搜索返回 `"No results found"`。

链式失效：
1. **Search 无结果 → relations 对第一轮完全失效**：search 返回 0 个 URI，没有任何节点用来查询 relations
2. **第一轮无 URI → post-answer linking 对第一轮无贡献**：`from_uri` 集合为空
3. **搜索轮次至少为 2**：无论 relations 质量多高，bot 必须执行第二轮 search

### 5.3 Bad Case 3：Bot 建的 Relations 对非 Bot 模式无效——Search 结果不重合

**场景**：查询 "3M debt securities registered to trade on NYSE"，bot 和非 bot 的向量搜索返回了完全不同的文档集。非 bot 搜到的是 10K 年报（2021/2022/2019），bot 搜到的是 10Q 季报（2023Q2）。正确答案 `3M_2023Q2_10Q.md` 对非 bot 不可达：非 bot 搜不到它，relations 的入口又不是非 bot search 结果中的任何 URI。

### 5.4 Bad Case 4：Bot 只 Search 不 Read

**场景**：HotpotQA，bot 连续 3 轮 search 后直接基于 L0 摘要回答，从未调用 read。

**问题**：没有 Read → 无法建边。post-answer review 需要 `read_uris` 作为 `to_uri` 候选，Read 为空则无法产生任何 link。

### 5.5 Bad Case 5：Review LLM 解析失败——静默跳过建边

**场景**：review LLM 返回的内容无法解析为有效 JSON 数组 → `useful_docs` 为空 → 整个建边块静默跳过。没有 exception，没有 error log。

### 5.6 Good Case：Relations 将多跳问题从 6 轮降到 2 轮

**场景**：HotpotQA，"The 2017–18 Wigan Athletic F.C. season will be a year in which the team competes in the league cup known as what for sponsorship reasons?"

**无 relations**：2 次 search + 3 次 read = 6 轮迭代，32852 input tokens
**有 relations**：1 次 search + 1 次 read = 2 轮迭代，11460 input tokens（-65%）

Relations 把多跳问题的第二跳文档直接通过 PRIORITY 暴露给 bot，省去了"读完第一个文档 → 发现需要第二个文档 → 再搜索 → 再读取"的完整链路。

