# Phase1 Fallback Judgment Metrics

本文档解释 `benchmark_metrics_report.json` 中 Phase1 fallback 研判指标的含义。

## 当前 Provider

当前只保留两个 Phase1 provider：

- `raw_context_phase1_result`：正式策略，控制 `fallback.triggered`，也控制是否进入 bot。
- `raw_context_naive_rule`：朴素 LLM baseline，只用于对比记录。

报告中主要看三块统一指标：

```text
Phase1 Provider Judgment Metrics
Phase1 Provider Judgment Comparison
Phase1 Provider Cost
```

## 统计口径

这个指标衡量的是 Phase1 的 fallback 研判是否正确。

系统先用 Phase1 生成答案，再由 provider 判断是否应该 fallback。评测完成后，用该 provider 答案的 `Accuracy` 作为事后真值：

- `Accuracy 0/1/2`：Phase1 答案质量较差，应该 fallback。
- `Accuracy 3/4`：Phase1 答案质量可接受，不应该 fallback。

provider 指标优先使用该 provider 自己生成答案的 Accuracy：

```text
fallback.provider_results.<provider>.metrics.Accuracy
```

如果 provider 没有单独评分，才回退到 `Phase1 Accuracy`。

注意：

- `fallback.triggered` 表示 primary provider 判定是否应该 fallback。
- `fallback.executed` 表示是否真的执行了 fallback/bot。
- 当前 dry-run 评估可以让 `fallback.triggered=true`，但 `fallback.executed=false`，这样可以统计研判质量，同时不消耗 bot token。

## 两个 Provider 的判断方式

`raw_context_phase1_result`：

- Stage 1 只做证据充分性判断和证据选择，不生成答案。
- Stage 1 输入原始 `context_blocks` 和问题，输出 `sufficient`、`requirements`、`selected_evidence`、`missing_info`。
- Stage 1 输出 `sufficient=false`、`missing_info` 非空、解析失败，或没有选出直接证据时判定应该 fallback。
- 只有 Stage 1 判定充分时，Stage 2 才根据 `selected_evidence` 生成 Phase1 答案。
- Stage 2 输出 fallback/insufficient/refusal-like answer 时也判定应该 fallback。
- 不再使用 evidence risk gate、supplemental audit、relation dominant 等复杂规则。

`raw_context_naive_rule`：

- 使用 `adapter.build_simple_prompt(qa, context_blocks)`。
- 一次 LLM 调用同时生成答案和充足性判断。
- 根据 simple prompt 输出的 `action/sufficient/answer` 判定是否 fallback。
- 它不是不用 LLM 的纯规则，而是更简单的 LLM baseline。

## 字段说明

### Total Records

参与统计的 fallback 模式样本总数。

### Bucketed Accuracy Records

Accuracy 能被归入明确判断区间的样本数。

当前规则中：

- `Accuracy <= 2` 属于应该 fallback。
- `Accuracy >= 3` 属于不应该 fallback。

### Unbucketed Accuracy Count

不能归入上述区间的样本数。

例如未来如果 grader 给出 `2.5` 这类非整数分数，它会计入这里，不参与错判率计算。

### Judged Trigger Count

provider 判定“应该 fallback”的样本数。

### Judged Not Trigger Count

provider 判定“不需要 fallback”的样本数。

### Expected Trigger Count (Accuracy 0-2)

根据 Accuracy 判断，实际应该 fallback 的样本数。

也就是 provider 答案最终得分为 `0/1/2` 的数量。

### Expected Not Trigger Count (Accuracy 3-4)

根据 Accuracy 判断，实际不应该 fallback 的样本数。

也就是 provider 答案最终得分为 `3/4` 的数量。

### False Positive Count (triggered but Accuracy 3-4)

误触发数量。

含义是：provider 判定应该 fallback，但 Accuracy 是 `3/4`，说明 Phase1 答案其实还可以。

这类错误主要问题是浪费 fallback/bot 调用。

### False Negative Count (not triggered but Accuracy 0-2)

漏触发数量。

含义是：provider 判定不需要 fallback，但 Accuracy 是 `0/1/2`，说明 Phase1 答案其实较差。

这类错误更危险，因为差答案会被直接保留。

### Judgment Error Count

总错判数量。

公式：

```text
False Positive Count + False Negative Count
```

### Judgment Error Rate

总错判率。

公式：

```text
Judgment Error Count / Bucketed Accuracy Records
```

### False Positive Rate Among Judged Triggered

在所有判定应该 fallback 的样本中，误触发的比例。

公式：

```text
False Positive Count / Judged Trigger Count
```

这个值越高，说明 fallback judge 太激进。

### False Negative Rate Among Judged Not Triggered

在所有判定不需要 fallback 的样本中，漏触发的比例。

公式：

```text
False Negative Count / Judged Not Trigger Count
```

这个值越高，说明 fallback judge 太保守。

### Recall for Bad Phase1 Answers

坏答案召回率。

含义是：所有实际应该 fallback 的差答案中，provider 成功抓住了多少。

公式：

```text
(Expected Trigger Count - False Negative Count) / Expected Trigger Count
```

这个指标越高，说明系统越能发现 Phase1 的低质量答案。

### Precision for Triggered Fallback

fallback 触发精度。

含义是：所有判定应该 fallback 的样本中，真正应该 fallback 的比例。

公式：

```text
(Judged Trigger Count - False Positive Count) / Judged Trigger Count
```

这个指标越高，说明触发 fallback 的判断越可靠。

## 示例

如果报告中出现：

```json
{
  "Total Records": 94,
  "Bucketed Accuracy Records": 94,
  "Judged Trigger Count": 25,
  "Judged Not Trigger Count": 69,
  "Expected Trigger Count (Accuracy 0-2)": 27,
  "Expected Not Trigger Count (Accuracy 3-4)": 67,
  "False Positive Count (triggered but Accuracy 3-4)": 7,
  "False Negative Count (not triggered but Accuracy 0-2)": 9,
  "Judgment Error Count": 16,
  "Judgment Error Rate": 0.1702127659574468,
  "Recall for Bad Phase1 Answers": 0.6666666666666666,
  "Precision for Triggered Fallback": 0.72
}
```

可以解读为：

- 一共评估 94 条样本。
- provider 判定 25 条应该 fallback。
- 实际根据 Accuracy 看，27 条应该 fallback。
- provider 误触发 7 条。
- provider 漏触发 9 条。
- 总错判率是 `16 / 94 = 17.02%`。
- 差答案召回率是 `(27 - 9) / 27 = 66.67%`。
- fallback 触发精度是 `(25 - 7) / 25 = 72%`。
