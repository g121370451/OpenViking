import os
import json
import time
import uuid
import random
import re
import threading
import signal
import atexit
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
from pathlib import Path
import sys
from typing import Set, List

sys.path.append(str(Path(__file__).parent))

from adapters_no_prompt.base import BaseAdapter
from core.logger import get_logger
from core.vector_store import VikingStoreWrapper
from core.monitor import BenchmarkMonitor
from core.metrics import MetricsCalculator
from core.judge_util import llm_grader
from core.checkpoint import CheckpointManager
from vikingbot_runner import run_vikingbot_query, stop_openviking_server


class BenchmarkPipeline:
    def __init__(self, config, adapter: BaseAdapter, vector_db: VikingStoreWrapper = None, llm = None, resume: bool = False):
        self.config = config
        self.adapter = adapter
        self.db = vector_db
        self.llm = llm
        self.logger = get_logger()
        self.monitor = BenchmarkMonitor()
        self.resume = resume
        
        self.output_dir = self.config['paths']['output_dir']
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir, exist_ok=True)
        self.generated_file = os.path.join(self.output_dir, "generated_answers.json")
        self.eval_file = os.path.join(self.output_dir, "qa_eval_detailed_results.json")
        self.report_file = os.path.join(self.output_dir, "benchmark_metrics_report.json")
        
        self.checkpoint_manager = CheckpointManager(self.output_dir, self.config)
        self._file_lock = threading.Lock()
        self.save_frequency = 10
        self._bot_contexts_cache = None  # lazy-loaded for use_bot_contexts mode
        
        self.metrics_summary = {
            "insertion": {"time": 0, "input_tokens": 0, "output_tokens": 0, "embedding_tokens": 0},
            "deletion": {"time": 0, "input_tokens": 0, "output_tokens": 0, "embedding_tokens": 0}
        }
        
        # 设置信号处理器，确保 Ctrl+C 时正确停止 ov 服务
        self._setup_signal_handlers()
    
    def _setup_signal_handlers(self):
        """设置信号处理器，确保在测试被中断时正确停止 ov 服务"""
        def handle_signal(signum, frame):
            self.logger.info(f"Received signal {signum}, stopping OpenViking server...")
            stop_openviking_server()
            sys.exit(1)
        
        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)
        
        # 注册 atexit 处理器，确保程序正常退出时也停止 ov 服务
        atexit.register(stop_openviking_server)

    def run_generation(self):
        """Step 1: Data Preparation"""
        self.logger.info(">>> Stage: Ingestion & Generation")
        try:
            doc_dir = self.config['paths'].get('doc_output_dir')
            if not doc_dir:
                doc_dir = os.path.join(self.output_dir, "docs")
            
            try:
                doc_info = self.adapter.data_prepare(doc_dir)
            except Exception as e:
                self.logger.exception(f"Data preparation failed: {e}")
                exit(1)
            
            skip_ingestion = self.config['execution'].get('skip_ingestion', False)

            if skip_ingestion:
                self.logger.info(f"Skipping Ingestion. Using existing docs at: {doc_dir}")
                if not os.path.exists(doc_dir):
                     self.logger.warning(f"Warning: Doc directory {doc_dir} not found, but ingestion is skipped.")
                self.metrics_summary["insertion"] = {"time": 0, "input_tokens": 0, "output_tokens": 0, "embedding_tokens": 0}
            else:
                ingest_workers = self.config['execution'].get('ingest_workers', 10)
                ingest_mode = self.config['execution'].get('ingest_mode', 'per_file')
                
                mode_desc = {
                    'directory': 'Unified directory mode',
                    'per_file': 'Per-file mode'
                }
                self.logger.info(f"Ingestion mode: {ingest_mode} ({mode_desc.get(ingest_mode, 'Unknown mode')})")
                self.logger.info(f"Number of documents: {len(doc_info)}")
                
                ingest_stats = self.db.ingest(
                    doc_info, 
                    max_workers=ingest_workers, 
                    monitor=self.monitor,
                    ingest_mode=ingest_mode
                )
                self.metrics_summary["insertion"] = ingest_stats
                self.logger.info(f"Insertion finished. Time: {ingest_stats['time']:.2f}s")

                self._update_report({
                    "Insertion Efficiency (Total Dataset)": {
                        "Total Insertion Time (s)": self.metrics_summary["insertion"]["time"],
                        "Total Input Tokens": self.metrics_summary["insertion"]["input_tokens"],
                        "Total Output Tokens": self.metrics_summary["insertion"]["output_tokens"],
                        "Total Embedding Tokens": self.metrics_summary["insertion"].get("embedding_tokens", 0)
                    }
                })
            
            samples = self.adapter.load_and_transform()    
            tasks = self._prepare_tasks(samples)
            results_map = {}
            max_workers = self.config['execution']['max_workers']
            
            completed_tasks: Set[int] = set()
            if self.resume:
                completed_tasks = self.checkpoint_manager.get_completed_tasks("generation")
                if completed_tasks:
                    self.logger.info(f"Resuming from checkpoint. {len(completed_tasks)} tasks already completed.")
                    if os.path.exists(self.generated_file):
                        with open(self.generated_file, "r", encoding="utf-8") as f:
                            saved_data = json.load(f)
                            for result in saved_data.get("results", []):
                                results_map[result["_global_index"]] = result
            
            remaining_tasks = [task for task in tasks if task["id"] not in completed_tasks]
            self.logger.info(f"Total tasks: {len(tasks)}, Remaining: {len(remaining_tasks)}")
            
            if remaining_tasks:
                initial_completed = len(completed_tasks)
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    future_to_task = {
                        executor.submit(self._process_generation_task, task): task 
                        for task in remaining_tasks
                    }
                    
                    pbar = tqdm(total=len(tasks), desc="Generating Answers", unit="task", initial=len(completed_tasks))
                    for future in as_completed(future_to_task):
                        task = future_to_task[future]
                        try:
                            res = future.result()
                            results_map[res['_global_index']] = res
                            completed_tasks.add(res['_global_index'])
                            
                            newly_completed = len(completed_tasks) - initial_completed
                            if newly_completed % self.save_frequency == 0 or len(completed_tasks) == len(tasks):
                                self.checkpoint_manager.update_completed_tasks("generation", completed_tasks, len(tasks))
                                self._save_partial_results(results_map)
                        except Exception as e:
                            self.logger.error(f"Generation failed for task {task['id']}: {e}")
                            self.monitor.worker_end(success=False)
                        pbar.set_postfix(self.monitor.get_status_dict())
                        pbar.update(1)
                    pbar.close()
            else:
                self.logger.info("All tasks already completed!")
            
            sorted_results = [results_map[i] for i in sorted(results_map.keys())]
            dataset_name = self.config.get('dataset_name', 'Unknown_Dataset')
            save_data = {
                "summary": {"dataset": dataset_name, "total_queries": len(sorted_results)},
                "results": sorted_results
            }
            total = len(sorted_results)
            if total > 0:
                self._update_report({
                        "Query Efficiency (Average Per Query)": {
                            "Average Retrieval Time (s)": sum(r['retrieval']['latency_sec'] for r in sorted_results) / total,
                            "Average Input Tokens": sum(r['token_usage'].get('total_input_tokens', 0) for r in sorted_results) / total,
                            "Average Output Tokens": sum(r['token_usage'].get('llm_output_tokens', 0) for r in sorted_results) / total,
                            "Average Retrieval Embedding Tokens": sum(r['token_usage'].get('retrieval_embedding_tokens', 0) for r in sorted_results) / total,
                        }
                    }
                )
            self.logger.info(f"[Save] Final write: {len(sorted_results)} results to {self.generated_file}")
            try:
                with open(self.generated_file, "w", encoding="utf-8") as f:
                    json.dump(save_data, f, indent=2, ensure_ascii=False, default=str)
                self.logger.info(f"[Save] Final write successful: {len(sorted_results)} results")
            except Exception as e:
                self.logger.error(f"[Save] Final write FAILED to {self.generated_file}: {e}")
                self.logger.error(f"[Save] Data preview: dataset={dataset_name}, results_count={len(sorted_results)}")
                # Log first result for debugging
                if sorted_results:
                    try:
                        preview = json.dumps(sorted_results[0], ensure_ascii=False, default=str)[:500]
                        self.logger.error(f"[Save] First result preview: {preview}")
                    except Exception:
                        self.logger.error(f"[Save] First result (raw): {sorted_results[0]}")
                raise

            self.checkpoint_manager.delete_checkpoint()
        finally:
            # 确保在 generation 阶段结束后停止 ov 服务
            self.logger.info("Generation stage completed, stopping OpenViking server...")
            stop_openviking_server()
    
    def _save_partial_results(self, results_map: dict):
        with self._file_lock:
            sorted_results = [results_map[i] for i in sorted(results_map.keys())]
            dataset_name = self.config.get('dataset_name', 'Unknown_Dataset')
            save_data = {
                "summary": {"dataset": dataset_name, "total_queries": len(sorted_results)},
                "results": sorted_results
            }
            self.logger.info(f"[Save] Writing {len(sorted_results)} results to {self.generated_file}")
            try:
                with open(self.generated_file, "w", encoding="utf-8") as f:
                    json.dump(save_data, f, indent=2, ensure_ascii=False, default=str)
                self.logger.info(f"[Save] Successfully wrote {len(sorted_results)} results")
            except Exception as e:
                self.logger.error(f"[Save] Failed to write {self.generated_file}: {e}")
                self.logger.error(f"[Save] Data preview: dataset={dataset_name}, results_count={len(sorted_results)}")
                raise

    def run_evaluation(self):
        """Step 4: Evaluation"""
        self.logger.info(">>> Stage: Evaluation")

        if not os.path.exists(self.generated_file):
            self.logger.error("Generated answers file not found.")
            return

        with open(self.generated_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            items = data.get("results", [])

        eval_items = items
        eval_results_map = {}
        
        completed_eval_tasks: Set[int] = set()
        if self.resume:
            completed_eval_tasks = self.checkpoint_manager.get_completed_tasks("evaluation")
            if completed_eval_tasks:
                self.logger.info(f"Resuming from checkpoint. {len(completed_eval_tasks)} evaluations already completed.")
                if os.path.exists(self.eval_file):
                    with open(self.eval_file, "r", encoding="utf-8") as f:
                        saved_eval_data = json.load(f)
                        for result in saved_eval_data.get("results", []):
                            eval_results_map[result["_global_index"]] = result
        
        remaining_eval_items = [item for item in eval_items if item["_global_index"] not in completed_eval_tasks]
        self.logger.info(f"Total evaluations: {len(eval_items)}, Remaining: {len(remaining_eval_items)}")
        
        if remaining_eval_items:
            initial_completed_eval = len(completed_eval_tasks)
            with ThreadPoolExecutor(max_workers=self.config['execution']['max_workers']) as executor:
                future_to_item = {
                    executor.submit(self._process_evaluation_task, item): item 
                    for item in remaining_eval_items
                }
                
                pbar = tqdm(total=len(eval_items), desc="Evaluating", unit="item", initial=len(completed_eval_tasks))
                for future in as_completed(future_to_item):
                    try:
                        res = future.result()
                        eval_results_map[res['_global_index']] = res
                        completed_eval_tasks.add(res['_global_index'])
                        
                        newly_completed_eval = len(completed_eval_tasks) - initial_completed_eval
                        if newly_completed_eval % self.save_frequency == 0 or len(completed_eval_tasks) == len(eval_items):
                            self.checkpoint_manager.update_completed_tasks("evaluation", completed_eval_tasks, len(eval_items))
                            self._save_partial_eval_results(eval_results_map)
                    except Exception as e:
                        self.logger.error(f"Evaluation failed: {e}")
                    pbar.update(1)
                pbar.close()
        else:
            self.logger.info("All evaluations already completed!")

        eval_records = list(eval_results_map.values())
        total = len(eval_records)

        with open(self.eval_file, "w", encoding="utf-8") as f:
            json.dump({"results": eval_records}, f, indent=2, ensure_ascii=False)

        if total > 0:
            report = {
                "Dataset": self.config.get('dataset_name', 'Unknown_Dataset'),
                "Total Queries Evaluated": total,
                "Performance Metrics": {
                    "Average F1 Score": sum(r['metrics']['F1'] for r in eval_records) / total,
                    "Average Recall": sum(r['metrics']['Recall'] for r in eval_records) / total,
                    "Average Accuracy (Hit 0-4)": sum(r['metrics']['Accuracy'] for r in eval_records) / total,
                    "Average Accuracy (normalization)": (sum(r['metrics']['Accuracy'] for r in eval_records) / total)/4,
                }
            }

            # Add vikingbot iteration metrics if available
            vb_records = [r for r in eval_records if 'vikingbot' in r]
            if vb_records:
                # 过滤 tc=0 的异常记录（JSON 解析失败），排除对迭代次数统计的干扰
                vb_valid = [r for r in vb_records if r['vikingbot'].get('tool_calls')]
                records_for_iters = vb_valid if vb_valid else vb_records  # fallback to all if all are tc=0

                iters_total = [r['vikingbot'].get('iterations_used', 0) for r in records_for_iters]
                iters_retrieval = [r['vikingbot'].get('retrieval_iterations', r['vikingbot'].get('iterations_used', 0)) for r in records_for_iters]
                iters_search = [r['vikingbot'].get('search_iterations', 0) for r in records_for_iters]
                iters_read = [r['vikingbot'].get('read_iterations', 0) for r in records_for_iters]
                report["VikingBot Iteration Metrics"] = {
                    "Average Total Iterations": sum(iters_total) / len(iters_total),
                    "Average Retrieval Iterations (excl. link/relations)": sum(iters_retrieval) / len(iters_retrieval),
                    "Average Search Iterations": sum(iters_search) / len(iters_search),
                    "Average Read Iterations": sum(iters_read) / len(iters_read),
                    "Min Retrieval Iterations": min(iters_retrieval),
                    "Max Retrieval Iterations": max(iters_retrieval),
                    "Excluded Anomalous Records (tc=0)": len(vb_records) - len(vb_valid),
                }

                # Relations Usage 统计使用全量 vb_records
                relations_hits_list = [r['vikingbot'].get('relations_hits', 0) for r in vb_records]
                total_relations_list = [r['vikingbot'].get('total_relations_found', 0) for r in vb_records]
                report["Relations Usage"] = {
                    "Total Questions with Relations Hits": sum(1 for h in relations_hits_list if h > 0),
                    "Total Relations Found": sum(total_relations_list),
                    "Average Relations Found per Query": sum(total_relations_list) / len(total_relations_list),
                    "Relations Hit Rate": sum(1 for h in relations_hits_list if h > 0) / len(relations_hits_list),
                }

            self._update_report(report)
        
        self.checkpoint_manager.delete_checkpoint()
    
    def _save_partial_eval_results(self, eval_results_map: dict):
        with self._file_lock:
            eval_records = list(eval_results_map.values())
            with open(self.eval_file, "w", encoding="utf-8") as f:
                json.dump({"results": eval_records}, f, indent=2, ensure_ascii=False)

    def run_deletion(self):
        """Step 5: Cleanup"""
        self.logger.info(">>> Stage: Deletion")
        start_time = time.time()
        self.db.clear()
        duration = time.time() - start_time
        self.metrics_summary["deletion"] = {"time": duration, "input_tokens": 0, "output_tokens": 0}
        self.logger.info(f"Deletion finished. Time: {duration:.2f}s")

        self._update_report({
            "Deletion Efficiency (Total Dataset)": {
                "Total Deletion Time (s)": duration,
                "Total Input Tokens": 0,
                "Total Output Tokens": 0
            }
        })

    def _prepare_tasks(self, samples):
        tasks = []
        global_idx = 0
        max_queries = self.config['execution'].get('max_queries')
        for sample in samples:
            for qa in sample.qa_pairs:
                if max_queries is not None and global_idx >= max_queries:
                    break
                tasks.append({"id": global_idx, "sample_id": sample.sample_id, "qa": qa})
                global_idx += 1
            if max_queries is not None and global_idx >= max_queries:
                break
        return tasks

    def _process_generation_task(self, task):
        self.monitor.worker_start()
        try:
            qa = task['qa']
            
            use_vikingbot = self.config['execution'].get('use_vikingbot', False)
            
            if use_vikingbot:
                return self._process_vikingbot_task(task, qa)
            else:
                return self._process_standard_rag_task(task, qa)
        except Exception as e:
            self.monitor.worker_end(success=False)
            raise e
    
    def _process_standard_rag_task(self, task, qa):
        t0 = time.time()
        use_bot_contexts = self.config['execution'].get('use_bot_contexts', False)

        # Bot-contexts mode: use saved bot-read documents instead of vector retrieval
        if use_bot_contexts:
            if self._bot_contexts_cache is None:
                with self._file_lock:
                    if self._bot_contexts_cache is None:  # double-check
                        self._bot_contexts_cache = self._load_bot_contexts()
                        self.logger.info(f"Loaded bot contexts cache: {len(self._bot_contexts_cache)} questions")

            bot_ctx = self._bot_contexts_cache.get(qa.question, {})
            if bot_ctx:
                retrieved_uris = list(bot_ctx.keys())
                context_blocks = list(bot_ctx.values())
                recall_texts = dict(bot_ctx)
                retrieval_tokens = 0
                agentic_metadata = None
                latency = time.time() - t0
                self.logger.info(f"[Query-{task['id']}] Using {len(bot_ctx)} bot-read contexts")
            else:
                self.logger.warning(f"[Query-{task['id']}] No bot contexts found, falling back to retrieval")
                use_bot_contexts = False  # fall through to normal retrieval

        # Normal retrieval mode
        if not use_bot_contexts:
            retrieval_instruction = self.config['execution'].get('retrieval_instruction', '')
            if retrieval_instruction:
                enhanced_query = f"{retrieval_instruction} {qa.question}"
            else:
                enhanced_query = qa.question

            topk = int(self.config['execution']['retrieval_topk'])

            # retrieve() now returns a unified dict from both store types
            # VikingStoreWithRelations handles query expansion internally
            ret = self.db.retrieve(query=enhanced_query, topk=topk)

            latency = time.time() - t0

            recall_texts = ret["recall_texts"]           # {uri: full_content}
            context_blocks = ret["context_blocks"]       # [truncated, ...]
            retrieved_uris = ret["retrieved_uris"]       # [uri, ...]
            retrieval_tokens = ret["retrieval_tokens"]   # int
            agentic_metadata = ret.get("agentic_metadata")  # None for standard RAG

        retrieved_texts = list(recall_texts.values())
        recall = MetricsCalculator.check_recall(retrieved_texts, qa.evidence)

        full_prompt, meta = self.adapter.build_prompt(qa, context_blocks)

        ans_raw = self.llm.generate(full_prompt)
        ans = self.adapter.post_process_answer(qa, ans_raw, meta)

        # Generate refusal reason if enabled and answer is a refusal
        refusal_reason = ""
        use_refusal_reason = self.config['execution'].get('generate_refusal_reason', False)
        if use_refusal_reason and MetricsCalculator.check_refusal(ans):
            reason_prompt = (
                "Based on the following context, briefly explain in one sentence "
                "why the question cannot be answered.\n\n"
                f"CONTEXT:\n{chr(10).join(context_blocks)}\n\n"
                f"QUESTION: {qa.question}\n\n"
                f"The system answered: \"{ans}\"\n\n"
                "Reason why this question cannot be answered from the context:"
            )
            refusal_reason = self.llm.generate(reason_prompt).strip()

        in_tokens = self.db.count_tokens(full_prompt) + self.db.count_tokens(qa.question)
        out_tokens = self.db.count_tokens(ans)
        if refusal_reason:
            in_tokens += self.db.count_tokens(reason_prompt)
            out_tokens += self.db.count_tokens(refusal_reason)
        self.monitor.worker_end(tokens=in_tokens + out_tokens + retrieval_tokens)

        self.logger.info(f"[Query-{task['id']}] Q: {qa.question[:30]}... | Recall: {recall:.2f} | Latency: {latency:.2f}s")

        result = {
            "_global_index": task['id'], "sample_id": task['sample_id'], "question": qa.question,
            "gold_answers": qa.gold_answers, "category": str(qa.category), "evidence": qa.evidence,
            "retrieval": {"latency_sec": latency, "uris": retrieved_uris},
            "llm": {"final_answer": ans},
            "metrics": {"Recall": recall},
            "token_usage": {"total_input_tokens": in_tokens, "llm_output_tokens": out_tokens, "retrieval_embedding_tokens": retrieval_tokens}
        }

        if use_bot_contexts and bot_ctx:
            result["retrieval"]["mode"] = "bot_contexts"
            result["retrieval"]["bot_contexts_count"] = len(bot_ctx)

        # Query expansion stats (from VikingStoreWithRelations)
        if not use_bot_contexts:
            expanded_queries = ret.get("expanded_queries")
            if expanded_queries is not None:
                result["retrieval"]["expanded_queries"] = expanded_queries

        # Relations enhancement stats (from VikingStoreWithRelations)
        relations_uris = ret.get("relations_uris", []) if not use_bot_contexts else []
        relations_found = ret.get("relations_found", 0) if not use_bot_contexts else 0
        relations_added = ret.get("relations_added", 0) if not use_bot_contexts else 0
        if relations_found > 0:
            result["retrieval"]["relations_uris"] = relations_uris
            result["retrieval"]["relations_found"] = relations_found
            result["retrieval"]["relations_added"] = relations_added
            self.logger.info(f"[Query-{task['id']}] Relations: found={relations_found}, added={relations_added}")

        if refusal_reason:
            result["retrieval"]["refusal_reason"] = refusal_reason
            self.logger.info(f"[Query-{task['id']}] Refusal reason: {refusal_reason[:80]}...")

        if agentic_metadata:
            result["agentic_rag"] = {
                "iterations_used": agentic_metadata.get("iterations_used", 0),
                "tool_calls": agentic_metadata.get("tool_calls", []),
                "recall_texts": recall_texts,
            }
            result["retrieval"]["mode"] = "agentic"

        return result
    
    def _process_vikingbot_task(self, task, qa):
        self.logger.info(f"[Query-{task['id']}] Using VikingBot for agentic RAG")
        
        session_id = f"query_{uuid.uuid4().hex}"
        
        vikingbot_result = run_vikingbot_query(
            question=qa.question,
            config=self.config,
            session_id=session_id
        )
        
        ans = vikingbot_result['answer']
        total_time = vikingbot_result['total_time_sec']
        
        recall = 0.0
        vb_usage = (vikingbot_result.get("vikingbot", {}) or {}).get("token_usage", {}) or {}
        in_tokens = int(vb_usage.get("prompt_tokens", 0) or 0)
        out_tokens = int(vb_usage.get("completion_tokens", 0) or 0)
        
        self.monitor.worker_end(tokens=in_tokens + out_tokens)

        # Calculate retrieval-only iterations (excluding link/relations tool calls)
        vb_meta = vikingbot_result.get('vikingbot', {})
        total_iterations = vb_meta.get('iterations_used', 0)
        tool_calls_raw = vb_meta.get('tool_calls', '')
        link_tools = {'openviking_link', 'openviking_relations'}
        retrieval_iterations = total_iterations
        if tool_calls_raw:
            try:
                tc_list = json.loads(tool_calls_raw) if isinstance(tool_calls_raw, str) else tool_calls_raw
                if isinstance(tc_list, list):
                    link_only_iters = set()
                    for tc in tc_list:
                        name = tc.get('tool_name', '') if isinstance(tc, dict) else ''
                        if name in link_tools:
                            # Track which iterations had ONLY link tools
                            link_only_iters.add(id(tc))  # placeholder, need iteration info
                    # Count iterations where ALL tool calls are link-related
                    # Group by iteration: tool_calls don't have iteration field,
                    # so count how many individual link tool calls there are
                    link_call_count = sum(1 for tc in tc_list if isinstance(tc, dict) and tc.get('tool_name', '') in link_tools)
                    non_link_call_count = sum(1 for tc in tc_list if isinstance(tc, dict) and tc.get('tool_name', '') not in link_tools)
                    # Estimate: subtract ratio of link calls from total iterations
                    total_calls = len(tc_list) if tc_list else 1
                    retrieval_iterations = max(1, round(total_iterations * non_link_call_count / total_calls))
            except (json.JSONDecodeError, TypeError):
                pass

        vb_meta_out = dict(vb_meta)
        vb_meta_out['retrieval_iterations'] = retrieval_iterations

        # Calculate search/read iterations and relations statistics
        search_iterations = 0
        read_iterations = 0
        read_tool_names = {"openviking_multi_read", "openviking_read"}
        relations_hits = 0
        total_relations_found = 0
        if tool_calls_raw:
            try:
                tc_list_for_stats = json.loads(tool_calls_raw) if isinstance(tool_calls_raw, str) else tool_calls_raw
                if isinstance(tc_list_for_stats, list):
                    for tc in tc_list_for_stats:
                        if not isinstance(tc, dict):
                            continue
                        tn = tc.get('tool_name', '')
                        if tn == 'openviking_search':
                            search_iterations += 1
                            rf = tc.get('relations_found', 0) or 0
                            total_relations_found += rf
                            if rf > 0:
                                relations_hits += 1
                        elif tn in read_tool_names:
                            read_iterations += 1
            except (json.JSONDecodeError, TypeError):
                pass
        vb_meta_out['search_iterations'] = search_iterations
        vb_meta_out['read_iterations'] = read_iterations
        vb_meta_out['relations_hits'] = relations_hits
        vb_meta_out['total_relations_found'] = total_relations_found

        self.logger.info(f"[Query-{task['id']}] Q: {qa.question[:30]}... | Latency: {total_time:.2f}s | Iters: {total_iterations} (retrieval: {retrieval_iterations}) | Mode: Agentic RAG")

        # Save bot-read contexts if configured
        if self.config['execution'].get('export_bot_contexts', False):
            tc_for_export = vb_meta_out.get('tool_calls', [])
            if isinstance(tc_for_export, str):
                try:
                    tc_for_export = json.loads(tc_for_export)
                except json.JSONDecodeError:
                    tc_for_export = []
            bot_contexts = self._extract_bot_read_contexts(tc_for_export)
            if bot_contexts:
                self._save_bot_contexts(qa.question, bot_contexts)
                self.logger.info(f"[Query-{task['id']}] Saved {len(bot_contexts)} bot-read contexts")

        return {
            "_global_index": task['id'], "sample_id": task['sample_id'], "question": qa.question,
            "gold_answers": qa.gold_answers, "category": str(qa.category), "evidence": qa.evidence,
            "retrieval": {"latency_sec": total_time, "uris": [], "mode": "agentic"},
            "llm": {"final_answer": ans},
            "metrics": {"Recall": recall},
            "token_usage": {"total_input_tokens": in_tokens, "llm_output_tokens": out_tokens},
            "vikingbot": vb_meta_out
        }

    # ===== Bot context export / standard RAG import =====

    @staticmethod
    def _extract_bot_read_contexts(tool_calls: list) -> dict:
        """Parse openviking_multi_read tool calls, return {uri: content}."""
        import re
        contexts = {}
        for tc in tool_calls:
            if not isinstance(tc, dict) or tc.get("tool_name") != "openviking_multi_read":
                continue
            result = tc.get("result", "")
            if not result:
                continue
            # Parse: --- START OF {uri} ---\n{content}\n--- END OF {uri} ---
            pattern = r'--- START OF (viking://\S+) ---\n(.*?)\n--- END OF \1 ---'
            for match in re.finditer(pattern, result, re.DOTALL):
                uri = match.group(1)
                content = match.group(2).strip()
                if content and not content.startswith("ERROR"):
                    contexts[uri] = content
        return contexts

    def _save_bot_contexts(self, question: str, contexts: dict):
        """Append one question's bot-read contexts to JSONL file."""
        bot_contexts_file = os.path.join(self.output_dir, "bot_read_contexts.jsonl")
        record = {"question": question, "contexts": contexts}
        with self._file_lock:
            with open(bot_contexts_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _load_bot_contexts(self) -> dict:
        """Load bot_read_contexts.jsonl into {question: {uri: content}}."""
        bot_contexts_file = os.path.join(self.output_dir, "bot_read_contexts.jsonl")
        if not os.path.exists(bot_contexts_file):
            return {}
        mapping = {}
        with open(bot_contexts_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    mapping[rec["question"]] = rec.get("contexts", {})
                except (json.JSONDecodeError, KeyError):
                    continue
        return mapping

    def _process_evaluation_task(self, item):
        """
        Process a single evaluation task, computing F1 and Accuracy metrics.
        
        For multi-annotator scenarios (like Qasper dataset), a question may have multiple gold answers.
        Evaluation logic:
        - F1: Compute for each gold answer separately and take the maximum
        - Accuracy: Pass all gold answers to LLM at once for comprehensive judgment
        
        This correctly handles multi-annotator scenarios while maintaining compatibility with single-answer datasets (like Locomo).
        """
        ans, golds = item['llm']['final_answer'], item['gold_answers']
        
        f1 = max((MetricsCalculator.calculate_f1(ans, gt) for gt in golds), default=0.0)
        
        dataset_name = self.config.get('dataset_name', 'Unknown_Dataset')
        
        eval_record = {
            "score": 0.0,
            "reasoning": "",
            "prompt_type": ""
        }
        
        try:
            eval_res = llm_grader(
                self.llm.llm, 
                self.config['llm']['model'], 
                item['question'], 
                golds,
                ans,
                dataset_name=dataset_name
            )
            eval_record = eval_res
                
        except Exception as e:
            self.logger.error(f"Grader error: {e}")
            
        if MetricsCalculator.check_refusal(ans) and any(MetricsCalculator.check_refusal(gt) for gt in golds):
            f1 = 1.0
            eval_record["score"] = 4.0
            eval_record["reasoning"] = "System successfully identified Unanswerable/Refusal condition."
            eval_record["prompt_type"] = "Heuristic_Refusal_Check"

        acc = eval_record["score"]

        item["metrics"].update({"F1": f1, "Accuracy": acc})
        
        item["llm_evaluation"] = {
            "prompt_used": eval_record["prompt_type"],
            "reasoning": eval_record["reasoning"],
            "normalized_score": acc
        }

        detailed_info = (
            f"\n" + "="*60 +
            f"\n[Query ID]: {item['_global_index']}"
            f"\n[Question]: {item['question']}"
            f"\n[Retrieved URIs]: {item['retrieval'].get('uris', [])}"
            f"\n[LLM Answer]: {ans}"
            f"\n[Gold Answer]: {golds}"
            f"\n[Metrics]: {item['metrics']}"
            f"\n[LLM Judge Reasoning]: {eval_record['reasoning']}"
            f"\n" + "="*60
        )
        self.logger.info(detailed_info)
        return item

    def _update_report(self, data):
        """Read existing report, merge new data, and write back"""
        report = {}
        if os.path.exists(self.report_file):
            with open(self.report_file, "r", encoding="utf-8") as f:
                try:
                    report = json.load(f)
                except json.JSONDecodeError:
                    report = {}
        report.update(data)
        with open(self.report_file, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=4, ensure_ascii=False)
        self.logger.info(f"Report updated -> {self.report_file}")
