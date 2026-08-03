# BookRAG benchmark backend

`execution.mode: bookrag` uses the embedded official BookRAG Core. All
Markdown files returned by the dataset Adapter are fused into one
dataset-level GBC index, so retrieval can cross document boundaries.

BookRAG is a direct-answer backend, like VikingBot. Generation calls the
official `GBCRAG.generation()` method, including its planner, retrieval and
`AnswerAgent`; the benchmark pipeline records that answer and does not run a
second answer-generation LLM pass. Retrieved node IDs are converted to stable
`bookrag://` URIs for Recall and diagnostics.

## Install

Use Python 3.10 or newer and install the benchmark and BookRAG extras:

```bash
uv pip install -e ".[benchmark,bookrag]"
```

The backend consumes Adapter-produced Markdown. It does not install or invoke
MinerU and rejects PDF or other source formats at the BookRAG boundary.

## VersionRAG configuration

The ready-to-run configuration is
`config/versionrag/versionrag_bookrag_config.yaml`. Credentials remain in
`benchmark/RAG/.env`:

```dotenv
DOUBAO_LLM_API_KEY=...
VOLCENGINE_API_KEY=...
VIKINGDB_RERANK_AK=...
VIKINGDB_RERANK_SK=...
```

The configured online services are:

- LLM and AnswerAgent: `doubao-seed-2.0-lite` through the Ark
  OpenAI-compatible endpoint.
- Embedding: `doubao-embedding-vision-250615` through the Ark multimodal
  embedding API with text inputs.
- Reranker: `doubao-seed-rerank`, version `251028`, through the signed VikingDB
  rerank API.

The relevant BookRAG fields are:

```yaml
execution:
  mode: bookrag
  ingest_mode: dataset
  retrieval_topk: 5

bookrag:
  index_dir: ov_storage/{dataset_name}/{dataset_name}_bookrag_index
  index:
    chunk_size: 512
    overlap: 50
    tokenizer: cl100k_base
  tree:
    node_summary: true
    node_keywords: true
  graph:
    extractor_type: llm
    refine_type: advanced
    similarity_threshold: 0.6
  gbc:
    variant: standard
    topk: 5
    select_depth: 2
    topk_ent: 5
    sim_threshold_e: 0.3
  reranker:
    backend: vikingdb
    ak: ${VIKINGDB_RERANK_AK}
    sk: ${VIKINGDB_RERANK_SK}
    host: api-vikingdb.vikingdb.cn-beijing.volces.com
    model: doubao-seed-rerank
    model_version: "251028"
    threshold: 0.1
    batch_size: 100
```

## Run

From `benchmark/RAG`:

```bash
python run.py --config config/versionrag/versionrag_bookrag_config.yaml --step import
python run.py --config config/versionrag/versionrag_bookrag_config.yaml --step gen+eval
python run.py --config config/versionrag/versionrag_bookrag_config.yaml --step del
```

VS Code also provides Import, Gen + Eval, All and Delete launch entries. The
Gen + Eval and All entries prompt for `RAG_MAX_QUERIES`; leave it empty to run
every query.

Import is transactional. Core builds in a temporary sibling directory, closes
ChromaDB file handles, and only then replaces the previous recognized BookRAG
index. A failed build keeps the previous index. The manifest records source
hashes, a credential-redacted configuration hash, node/entity counts, build
time and LLM token use.

`del` is idempotent and removes only the configured directory when its manifest
identifies it as a BookRAG GBC index. Markdown inputs, benchmark outputs and
other backend stores are not touched.
