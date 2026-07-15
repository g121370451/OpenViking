# Mixed questions input

Place each canonical mixed question file at the path below before running
`run_experiment_generated.sh`:

```text
generated_questions/FinanceBench/mixed_questions.jsonl
generated_questions/HotpotQA/mixed_questions.jsonl
generated_questions/LegalBench_ContractNLI/mixed_questions.jsonl
generated_questions/LegalBench_CUAD/mixed_questions.jsonl
generated_questions/LegalBench_MAUD/mixed_questions.jsonl
generated_questions/Qasper/mixed_questions.jsonl
generated_questions/SyllabusQA/mixed_questions.jsonl
generated_questions/VersionRAG/mixed_questions.jsonl
```

The runner validates the JSONL schema before starting the experiment. Record
count is not restricted. Source files are read in place and are never modified.

Example:

```bash
bash run_experiment_generated.sh hotpotqa 1 gen+eval ov.conf
```
