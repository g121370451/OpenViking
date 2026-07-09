# OV Fallback Bot Flow

本文档记录当前 `ov_fallback_bot` / `ov_fallback_bot_relations` 的执行链路。

当前版本只保留两个 Phase1 provider：

- `raw_context_phase1_result`：正式策略，控制是否进入 bot。
- `raw_context_naive_rule`：朴素 LLM baseline，只用于对比记录。

旧的 Phase1 草稿透传配置已删除。fallback bot 永远只接收原始问题，不再接收 Phase1 草稿答案、证据分析或风险标记。

## 总览

```text
run_generation
  -> 根据 execution.mode 选择任务处理函数
     -> _process_fallback_task
     -> Phase1: OV retrieve
     -> Phase1 providers: 对同一个 qa + search_res 跑两种方法
        -> raw_context_phase1_result
        -> raw_context_naive_rule
     -> primary provider 决定 fallback.triggered / fallback.executed
     -> 如果触发，用原始问题调用 VikingBot
     -> final_answer = bot answer 或 primary provider answer
  -> run_evaluation
     -> 评估 final answer
     -> 评估 primary Phase1 answer
     -> 评估每个 provider 自己的 answer
     -> 按 provider 统计 fallback 判断质量
```

## Provider 配置

默认 primary provider 是：

```text
raw_context_phase1_result
```

默认启用：

```text
raw_context_phase1_result
raw_context_naive_rule
```

配置示例：

```yaml
execution:
  phase1_providers:
    primary: raw_context_phase1_result
    enabled:
      - raw_context_phase1_result
      - raw_context_naive_rule
    configs:
      raw_context_phase1_result:
        fallback_on_action_fallback: true
        fallback_on_insufficient: true
        fallback_on_missing_info: true
        fallback_on_stage2_insufficient: true
      raw_context_naive_rule:
        fallback_on_action_fallback: true
        fallback_on_insufficient: true
        fallback_on_refusal_like_answer: true
```

## Provider 语义

`raw_context_phase1_result`：

- Stage 1 只判断检索上下文是否含有足够直接证据，并选择 `selected_evidence`，不生成答案。
- Stage 1 输出 `sufficient=false`、`missing_info` 非空、解析失败，或没有选出直接证据时触发 fallback。
- 只有 Stage 1 判定充分时才进入 Stage 2。
- Stage 2 只根据 `selected_evidence` 生成 Phase1 answer。
- Stage 2 输出 fallback/insufficient/refusal-like answer 时也触发 fallback。
- 这是当前正式 primary，控制是否真的进入 bot。

`raw_context_naive_rule`：

- 使用 `adapter.build_simple_prompt(qa, context_blocks)`。
- 一次 LLM 调用同时生成 answer 和 sufficient。
- `should_fallback` 由 simple prompt 输出的 `sufficient/action/answer` 决定。
- 只记录指标，用来作为简单 baseline，不控制 bot。

## 输出字段

兼容字段：

```json
{
  "fallback": {
    "triggered": true,
    "executed": true,
    "phase1_judge_should_fallback": true,
    "ov_answer": "...",
    "naive_rule_triggered": false
  }
}
```

统一 provider 结构：

```json
{
  "fallback": {
    "primary_provider": "raw_context_phase1_result",
    "provider_results": {
      "raw_context_phase1_result": {
        "answer": "...",
        "should_fallback": true,
        "reasoning": "...",
        "input_tokens": 123,
        "output_tokens": 45,
        "latency_sec": 1.2,
        "metrics": {
          "F1": 0.0,
          "Accuracy": 2.0
        }
      },
      "raw_context_naive_rule": {
        "answer": "...",
        "should_fallback": false,
        "reasoning": "...",
        "metrics": {
          "F1": 0.0,
          "Accuracy": 3.0
        }
      }
    }
  }
}
```

注意：`provider_results.*.metrics` 在 evaluation 阶段写入，所以只在 `qa_eval_detailed_results.json` 中稳定存在。

## 报表

报告保留统一指标：

```text
Phase1 Provider Judgment Metrics
Phase1 Provider Judgment Comparison
Phase1 Provider Cost
```

判断正确性口径：

```text
Provider Accuracy <= 2: 应该 fallback
Provider Accuracy >= 3: 不应该 fallback
```

如果某个 provider 没有单独评分，会回退到 `Phase1 Accuracy`。
