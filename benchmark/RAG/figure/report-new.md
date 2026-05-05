# OpenViking RAG Benchmark 改进报告

## 日期

2026-05-04

---

## 一、Relations 从 Search 时移到 Read 时

### 问题

`VIKINGBOT_USE_RELATIONS=1` 时，relations 查询发生在 search 阶段：`VikingSearchTool` 对 top-5 搜索结果逐个查 relations，把关联 URI 拼到搜索结果末尾。

- Bot 可能不会读所有搜索结果，提前查 relations 浪费了不需要的查询
- Relations 结果以 URI 列表形式呈现，bot 需要额外一次 read 才能看到内容
- 搜索结果混杂了 relations，不够干净

### 方案

把 relations 查询从 **search 时** 移到 **read 时**：bot 读文档时，自动附带该文档的关联文档**内容**。

```
改前:
  search → [doc_a, doc_b] + [via relations] doc_c, doc_d
  read doc_a → content of doc_a
  read doc_c → content of doc_c  (bot 需要额外调用)

改后:
  search → [doc_a, doc_b]          (干净，只有向量搜索结果)
  read doc_a → content of doc_a
               + --- Related document (from relations) ---
               + content of doc_c   (直接附在 doc_a 后面)
```

### 改动文件

| 文件 | 改动 |
|---|---|
| `bot/vikingbot/agent/tools/ov_file.py` | `VikingSearchTool.execute()` 删除 relations 增强逻辑（~40行）；`VikingMultiReadTool.execute()` 在 `read_single_uri()` 中新增 relations 增强，读取关联文档内容追加到当前文档后面（最多 3 条，每条 3000 字符） |
| `bot/vikingbot/agent/loop.py` | `relations_found` 跟踪从仅 `openviking_search` 改为 `openviking_search` + `openviking_multi_read` |
| `benchmark/RAG/src/vector_store_with_relations.py` | 非 agentic 模式 relations 逻辑不变 |

### 配置

- `VIKINGBOT_USE_RELATIONS=1` — 启用 relations
- `VIKINGBOT_LINK_STRATEGY` — 选择策略 (`blind` / `cross_iteration` / `llm_review`)

---

## 二、Linking 策略改进

Linking 后处理在 `_post_answer_link()` 中触发（`VIKINGBOT_ENABLE_LINKING=1`），根据 `VIKINGBOT_LINK_STRATEGY` 选择策略。三种策略的起点和终点如下：

| 策略 | 起点（sources） | 终点（targets） | 配对方式 |
|---|---|---|---|
| `blind` / `read_blind` | 所有 read/multi_read 的 URI | 所有 read/multi_read 的 URI | O(n²) 两两配对 |
| `cross_iteration` | 跨轮次的 URI 对 + 同轮次低重叠 URI 对 | ← 对称 | 跨轮次 + Jaccard < 0.3 同轮次 |
| `llm_review` | 所有 read/multi_read 的 URI | bot 通过 `openviking_link` 标记的有用 URI | source → target 两两配对 |

### 2.1 BlindLinkStrategy（盲链接）

所有 `openviking_read` / `openviking_multi_read` 成功读过的 URI 去重后，做 O(n²) 两两链接，weight 随 iteration distance 高斯衰减。reason=`"co-referenced"`。

别名 `read_blind`，语义与 `blind` 完全一致（名称更直观）。

**文件**: `bot/vikingbot/agent/link_strategies.py` → `BlindLinkStrategy.build_links()`

### 2.2 CrossIterationLinkStrategy（跨轮次链接）

两部分组成：

1. **跨轮次链接**: 不同迭代轮次间的 URI 两两配对，weight 随 iteration distance 高斯衰减（峰值在 d=1，即相邻轮次）。reason=`"cross-iteration"`
2. **同轮次低重叠链接**: 对同一迭代轮次内的文档对，计算 Jaccard 相似度，当 Jaccard < 0.3 时建立链接。reason=`"same-iteration-low-overlap"`

**文件**: `bot/vikingbot/agent/link_strategies.py` → `CrossIterationLinkStrategy.build_links()`

### 2.3 LLMReviewLinkStrategy（Bot 自链接 + 后处理配对）

LLM Review 策略分为两个阶段：

1. **对话中**: bot 使用 `openviking_link` 工具标记有用文档（指定 `from_uri` 和 `uris`）
2. **后处理**: `build_links()` 收集所有 read/multi_read URI 作为**起点**，从 `openviking_link` 调用中提取所有有用 URI 作为**终点**，两两配对创建边（起点 ≠ 终点，pair 去重）。reason=`"bot-review"`

**文件**:
- `bot/vikingbot/agent/tools/ov_file.py` → `VikingLinkTool` — 对话中 bot 实时标记有用文档
- `bot/vikingbot/agent/link_strategies.py` → `LLMReviewLinkStrategy.build_links()` — 后处理 source→target 配对
- `bot/vikingbot/agent/tools/factory.py` — `link_strategy == "llm_review"` 时注册 `VikingLinkTool`
- `bot/vikingbot/agent/context.py` — 系统 prompt 添加链接指令

### 配置

```yaml
vikingbot:
  enable_linking: true
  link_strategy: blind          # 或 read_blind / cross_iteration / llm_review
```

---

## 三、Benchmark 指标改进

### 3.1 新增 Average Read Iterations

在 `VikingBot Iteration Metrics` 报告中新增 `Average Read Iterations` 统计，识别 `openviking_multi_read` 和 `openviking_read` 两种工具调用。

**文件**: `benchmark/RAG/src/pipeline.py`

**报告格式**:
```json
"VikingBot Iteration Metrics": {
    "Average Total Iterations": 4.04,
    "Average Retrieval Iterations (excl. link/relations)": 4.04,
    "Average Search Iterations": 1.49,
    "Average Read Iterations": 1.16,
    "Min Retrieval Iterations": 0,
    "Max Retrieval Iterations": 22
}
```