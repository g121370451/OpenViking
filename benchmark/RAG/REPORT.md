# RAG Benchmark 实验报告

## 1. 实验概述

基于 OpenViking 的 RAG Benchmark 框架，在多个数据集上对比不同检索增强策略的效果。所有实验使用相同的 LLM：`doubao-seed-1-8-251228`（字节跳动火山引擎）。

### 四组实验设计

| # | 实验名称 | 模式 | 核心差异 |
|---|---------|------|---------|
| 1 | **Standard RAG** | 非 Agentic | 原始问题 → 向量检索 top-k → LLM 生成答案 |
| 2 | **Standard RAG + Relations + keyword** | 非 Agentic | 在 Standard RAG 基础上，利用 `.relations.jsonl` 边关系扩展检索结果 |
| 2 | **Standard RAG + Relations + vector** | 非 Agentic | 在 Standard RAG 基础上，利用 `.relations.jsonl` 边关系扩展检索结果 |
| 3 | **VikingBot Agentic RAG + Relations** | Agentic | 在 Relations 基础上，bot 利用 `.relations.jsonl` 减少迭代轮次 |
| 4 | **VikingBot Agentic RAG** | Agentic | agentic unlearned |

### 构建的 Relations 示例

单条 `.relations.jsonl` 记录格式：

```json
{
  "uri1": "viking://resources/HotpotQA_processed_docs/Winnie_the_Pooh_and_the_Blustery_Day_doc/.../Winnie_the_Pooh_and_the_Blustery_Day_1.md",
  "uri2": "viking://resources/HotpotQA_processed_docs/Jim_Cummings_doc/Jim_Cummings_doc.md",
  "query_question": "This singer of A Rather Blustery Day also voiced what hedgehog?" 判断相似度 关键词,
  "vector":""
}
```

- `uri1` / `uri2`：两个关联文档的 Viking URI
- `query_question`：触发关联的原始问题，用于后续精确匹配检索

### 具体流程

| 步骤 | 操作 | 产出 |
|------|------|------|
| **Step 1** | 初始化数据集存储库，生成 Standard RAG 实验结果 | Standard RAG baseline 数据 |
| **Step 2** | VikingBot Agentic RAG 实验（无链接） | VikingBot unlearned 结果 |
| **Step 3** | VikingBot Build Relations 实验，运行过程中根据问题构建文档间链接 | `.relations.jsonl` 关系文件 |
| **Step 4** | 基于 Step 3 的 relations，分别运行 VikingBot + Relations 和 Standard RAG + Relations | 两组 Relations 增强结果 |
| **Step 5** | 汇总四组实验结果，对比分析 | 最终实验报告 |

### 实验配置说明

- **Standard RAG**：`use_vikingbot: false`，`retrieval_topk` 控制检索数量
- **Relations 增强**：`use_relations: true`，`VikingStoreWithRelations` 在向量检索后读取 `.relations.jsonl` 扩展上下文
- **VikingBot + Relations**：`use_vikingbot: true, relation_filter_mode: keyword(现在是问题的原文,确保一定能找到并且找的正确)`，agent在search工具的execute执行函数中添加relations方法调用，查询jsonl中的索引关系
- **VikingBot**：`use_vikingbot: true, enable_linking: false, relation_filter_mode: none`，纯 agentic 模式
- **VikingBot + build_relations**：`use_vikingbot: true, enable_linking: true, relation_filter_mode: none`，纯 agentic 模式+并且使用link工具在bot返回时根据output构建relations

---

## 2. 已完成实验结果（HotpotQA，100 条查询）

### 2.1 性能对比总表

| 实验 | F1 Score | Recall | Accuracy (归一化) | 检索时间 | 检索tokens |
|------|----------|--------|------------------|---------|------------------|
| **Standard RAG (top-5)** | 0.23 | 0.81 | **0.72** | 0.37s | 3164 |
| Standard RAG + Relations | 0.29 | 0.87 | **0.81** | 40s(分析为什么这么慢) | 3,389 |
| VikingBot + Relations | 0.18 | 0.000 | **0.9** | 56s | 113，794(分析为什么高) |
| VikingBot baseline | 0.14 | 0.000 | **0.9** | 65.4s | 56,572 |

### 2.3 补充实验（非核心对比组）
- VikingBot + question_extend
