# BookRAG 接入待办

> 本文件是临时工作清单。按编号一次只处理一个问题；每项完成后勾选并记录结论。全部项目验收通过后，删除本文件。

## 约束

- `benchmark/RAG/src/bookrag_core` 以 `D:\project\postgraduate\BookRAG\Core` 为算法实现来源；按已确认方案，仅将其内部 Python 包命名空间由 `Core` 改为 `bookrag_core`。
- 不在 benchmark 中重写 BookRAG 的建树、摘要、知识图谱、检索、规划或重排算法。
- benchmark 适配层仅负责配置转换、调用 BookRAG 的公开入口，以及转换 benchmark 所需的输入输出格式；BookRAG 查询答案由其官方 `AnswerAgent` 生成。
- benchmark pipeline 负责把各类源文档统一转换为 Markdown；BookRAG 不负责源文档解析，也不引入 Markdown 解析器依赖。
- 如果官方 Core 缺少能力，先在本清单记录并确认扩展方案，再修改代码。
- 首个验证数据集使用 VersionRAG，不使用 FinanceBench。
- 密钥只通过环境变量或本地 `.env` 注入，不写入本文件或提交到仓库。

## 待办顺序

### 1. 解决 Core 包名与依赖加载

状态：已完成。

- [x] 确定 `bookrag_core` 与官方 `from Core...` 导入的兼容方式。
- [x] 确定采用完整官方依赖，还是维护只覆盖实际调用链的 BookRAG optional dependency。
- [x] 在 Python 3.10 及以上环境中完成 `construct_index` 和 `inference` 导入冒烟测试。

已确认：保留 `benchmark/RAG/src/bookrag_core` 目录，并将目录内的 `Core...` Python 包引用统一替换为 `bookrag_core...`，不使用包映射。

验收标准：两个官方入口均可导入，且非 BookRAG benchmark 模式不受影响。

### 2. 确定 VersionRAG 的入库输入与索引粒度

- [x] 确定使用 Adapter 生成的 Markdown，还是回到原始 PDF。
- [x] 确定 34 个文档分别建索引，还是聚合成一个数据集级 GBC 索引。
- [x] 如果使用 Markdown 或聚合索引，明确应在官方 Core 中增加的公开构建入口。

原始缺口：官方 `construct_GBC_index()` 只接收单个 `cfg.pdf_path`，并调用 MinerU 的 PDF 构建链路。

已确认并实现：VersionRAG 使用 benchmark pipeline 产出的 Markdown；BookRAG 不接入 MinerU/PDF，也不直接依赖 `markdown-it-py`。`bookrag_core` 负责将已经归一化的 Markdown 标题和内容块映射为官方 `DocumentTree`，但不负责 PDF、Word 等源格式解析。

已确认：一个 benchmark 数据集构建一个数据集级 GBC 索引；VersionRAG 的 34 个 Markdown 文档必须进入同一个 GBC，并支持跨文档检索。

公开入口：

- `build_tree_from_markdown(cfg, markdown_path, sample_id)`：单 Markdown 构造官方文档树。
- `build_dataset_tree_from_markdown(cfg, documents, dataset_name=...)`：确定性排序并聚合全部文档树。
- `aggregate_document_trees(...)`：增加深度为 `-1` 的数据集 root，保留文档 root 深度 `0` 和所有原节点深度。
- `construct_GBC_index_from_tree(...)`：复用官方摘要、知识图谱、GBC 和 entity VDB 构建链路。
- `construct_GBC_index_from_markdown(...)`：数据集级 Markdown 入库公开入口。

结构 root 统一按 `NodeType.ROOT` 识别；数据集 root 和各文档 root 不进入摘要、KG 抽取、VDB、候选召回、rerank、global filter、路径和 evidence 数据。

验收标准：使用确定的公开入口完成 VersionRAG 文档建树和索引持久化，不在 benchmark 适配层实现建树算法。

### 3. 接通 embedding 服务

- [x] 确认继续使用在线火山方舟 `doubao-embedding-vision-250615`。
- [x] 在官方 Core 的 `TextEmbeddingProvider` 中增加 `volcengine` backend、API key 和并发配置。
- [x] 完成单条和批量 embedding 冒烟测试。

已实现：通过方舟 Ark SDK 调用图文向量化接口；每个 Markdown 文本块作为 text input 请求，批量输入通过受控线程池并发，保持输入输出顺序并沿用 Core 的向量归一化。配置中的密钥必须在构建 provider 前完成环境变量解析。

实网验收：`doubao-embedding-vision-250615` 成功返回 1 条、2048 维且全部为有限数值的向量；离线测试覆盖批量数量、顺序、归一化、客户端关闭及未解析密钥拒绝。

验收标准：官方 Core 能生成索引所需向量，维度和批量返回数量正确。

### 4. 接通 VikingDB reranker 服务

- [x] 在官方 Core 的 `TextRerankerProvider` 中增加 `vikingdb` backend。
- [x] 支持 AK/SK、host、model name、model version、threshold 和 batch size 配置。
- [x] 使用环境变量读取 AK/SK。
- [x] 完成单次 rerank 冒烟测试，并验证分数排序与 threshold 过滤。

已实现：复用 OpenViking 已有的 VikingDB HMAC 签名 `RerankClient`，不在 BookRAG 中复制签名协议；BookRAG provider 负责最多 200 条一批的调用、返回数量校验和 threshold 处理。AK/SK 只从已解析配置注入，不写入仓库或日志。

实网验收：`doubao-seed-rerank` 版本 `251028` 成功返回 2 个 `[0, 1]` 范围内的分数，相关文档排序在前；离线测试覆盖签名请求体、分批、阈值和服务失败传播。

验收标准：GBC retrieval 使用 `doubao-seed-rerank` 返回有效排序结果。

### 5. 确认 BookRAG 查询与答案入口

- [x] 使用官方公开的 `GBCRAG.generation()`，不调用私有 `_retrieve()`。
- [x] 沿用官方 planner 对 simple、complex 和 global 三种结果的处理。
- [x] 使用 `generation()` 返回的节点 ID，从 GBC TreeIndex 补充节点正文、来源文档和标题路径。
- [x] 允许并要求调用官方 `AnswerAgent` 生成最终答案。

已确认：BookRAG 与 OpenViking Bot 一样属于后端直接回答模式。`GBCRAG.generation()` 已完整执行 planner → retrieval → `AnswerAgent`，并返回 `(answer, retrieved_node_ids)`；适配层不得复制该链路，也不需要新增 evidence-only retriever。对于 global `COUNT`，继续保留官方无需 AnswerAgent 的直接计数语义；其他 global 操作沿用 `answer_global_question()`。

验收标准：BookRAG 答案直接写入 benchmark 的 `llm.final_answer`，pipeline 不再用通用 LLM 对同一证据二次生成；返回节点仍用于 Recall、URI 和检索延迟统计。

### 6. 替换旧 BookRAG benchmark 适配实现

- [x] 移除 `bookrag_runner.py` 对旧自研 `bookrag_core.config/index/markdown_tree/providers/retrieval` 的引用。
- [x] `ingest()` 只转换配置并调用已确认的官方入库入口。
- [x] 查询只调用官方 `GBCRAG.generation()`，获得最终答案和检索节点 ID。
- [x] 将官方结果转换为 `answer`、`recall_texts`、`context_blocks`、`retrieved_uris` 和 token usage。
- [x] 增加 BookRAG 专用 generation task，使 pipeline 直接采用 AnswerAgent 答案而不二次生成。
- [x] 实现事务式 `ingest()`、幂等 `clear()`、`close()` 和查询锁，但不实现 BookRAG 算法。

验收标准：适配层中不存在自定义建树、摘要、图抽取、检索、规划或 rerank 算法。

### 7. 整理配置与启动项

- [x] 更新 VersionRAG BookRAG 配置，使字段与官方 Core 及新增 provider 一致。
- [x] 保留豆包 LLM、embedding 和 VikingDB reranker 的环境变量配置。
- [x] 检查 `.vscode/launch.json` 的 VersionRAG Import、Gen+Eval、All 和 Delete 启动项。
- [x] 将 `bookrag_core` 阶段日志桥接到 benchmark 日志，显示建树、摘要、KG、重排和持久化进度。

验收标准：通过 launch 配置能分别执行导入、生成评测和全流程。

### 8. 完整验证与清理

- [x] 运行官方 Core 的导入和 provider 冒烟测试。
- [ ] 使用 VersionRAG 完成 ingest → retrieve → generation → evaluation → delete。
- [x] 使用两份真实 Markdown 和在线服务完成 GBC ingest → reload → retrieval → AnswerAgent → delete。
- [x] 验证 BookRAG 查询调用 `AnswerAgent`，且 pipeline 未二次生成答案。
- [ ] 验证 standard、vikingbot 和 fallback 等原有模式未回归。
- [x] 删除旧自研 BookRAG 测试或将其改为官方 Core 接口测试。
- [ ] 所有项目完成后删除 `BOOKRAG_TODO.md`。

验收标准：VersionRAG 全流程成功，相关测试通过，工作区不再保留旧自研 BookRAG 实现和本临时清单。

## 决策记录

在逐项解决时记录最终选择、对应提交文件和验证命令。

- 2026-07-31：保留 `bookrag_core` 包名；已将 Core 内部 39 个 Python 文件的 221 处 `Core.` 命名空间引用替换为 `bookrag_core.`，未修改算法逻辑。
- 2026-07-31：BookRAG 只声明实际 GBC benchmark 调用链使用的依赖；本地模型、Ollama、VLM 和 MinerU/PDF 分支的依赖不纳入默认 BookRAG optional dependency，相关模块需要采用延迟导入。
- 2026-07-31：文档格式归一化属于 benchmark pipeline；其输出统一为 Markdown。BookRAG 不负责解析源文档，不引入 `markdown-it-py`。
- 2026-07-31：最小依赖 extra 和锁文件已更新；Python 3.10 语法检查覆盖 73 个 Core 文件，Python 3.13 环境中 `construct_index`、`inference` 导入成功，`uv pip check` 通过。未安装 `torch`、`ollama`、`modelscope`、`spacy`、`textacy` 或 `mineru`。
- 2026-07-31：VersionRAG 的 34 个文档聚合为一个数据集级 GBC 索引，不采用每文档一个索引。
- 2026-07-31：在 `bookrag_core` 内新增 Markdown → 官方 `DocumentTree`、多树聚合、Tree → GBC 公开入口；pipeline 和 `StandardDoc` 数据模型不变。聚合 root 深度为 `-1`，文档原 depth 不变，全局 ID 确定性重排并保留 `local_index_id`、`sample_id`、来源路径和标题路径。
- 2026-07-31：结构 root 排除逻辑由“仅跳过 `tree.root_node` 对象”改为统一跳过全部 `NodeType.ROOT`；Markdown/聚合/Core 构建测试共 10 项通过。
- 2026-08-01：embedding 与 reranker 均确定使用在线火山服务。Core provider 增加 `volcengine` embedding 和 `vikingdb` reranker backend，并由统一的 `from_config()` 工厂注入，GBC 算法调用点不感知具体服务。
- 2026-08-01：方舟 embedding 实网返回 2048 维向量；VikingDB reranker 实网返回有效排序。provider、Markdown 建树和 Store 离线测试合计 15 项通过，凭证仍只由环境变量注入。
- 2026-08-01：BookRAG 查询确定采用与 OpenViking Bot 相同的后端直接回答模式。直接使用官方 `GBCRAG.generation()` 及 `AnswerAgent`，不再规划 evidence-only 查询入口；其返回的节点 ID 继续用于 benchmark Recall 和检索记录。
- 2026-08-01：旧 runner 已替换为官方 Core 适配器；pipeline 增加 BookRAG 直答分支。适配器只负责配置转换、事务安装、节点结果转换、并发与生命周期，不包含 BookRAG 算法。
- 2026-08-01：修复在线 embedding 图 variant 的分阶段重载、NetworkX 3.5 node-link `links` 兼容、Markdown 无页码进入 AnswerAgent，以及 Windows ChromaDB 原子改名所需的关闭生命周期。
- 2026-08-01：两文档在线实测构建 10 个树节点和 8 个实体，跨进程重载后检索 3 个节点，AnswerAgent 正确回答 `42 million` 和 `10 percent`，随后索引删除成功。
- 2026-08-01：VersionRAG 34 文档会展开为 17,534 个节点，其中 17,499 个节点进入摘要及 KG LLM 调用；为避免上万次付费请求，终止了全量容量测试，正式索引未被替换。
- 2026-08-01：LLM 配置已按要求切换为 `doubao-seed-2.0-lite`。用户提供的新 Ark Key 在该端点返回 401；本地已验证凭证访问该模型返回 404 无权限，因此新模型组合尚未通过实网验收，凭证未写入仓库。
