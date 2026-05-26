import os
import json
import time
import uuid
import random
import re
import copy
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
from pathlib import Path
import sys
from typing import Set

sys.path.append(str(Path(__file__).parent))

from adapters.base import BaseAdapter
from core.logger import get_logger
from core.vector_store import VikingStoreWrapper
from core.monitor import BenchmarkMonitor
from core.metrics import MetricsCalculator
from core.judge_util import llm_grader
from core.checkpoint import CheckpointManager
from vikingbot_runner import run_vikingbot_query
from nanobot_runner import run_nanobot_query


class BenchmarkPipeline:
    def __init__(self, config, adapter: BaseAdapter, vector_db: VikingStoreWrapper, llm, resume: bool = False):
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

        # Checkpoint + incremental saving for resume support.
        self.checkpoint_manager = CheckpointManager(self.output_dir, self.config)
        self._file_lock = threading.Lock()
        self.save_frequency = 10
        
        self.metrics_summary = {
            "insertion": {"time": 0, "input_tokens": 0, "output_tokens": 0, "embedding_tokens": 0},
            "deletion": {"time": 0, "input_tokens": 0, "output_tokens": 0, "embedding_tokens": 0}
        }

    def _get_record_input_tokens(self, record: dict) -> int:
        usage = (record or {}).get("token_usage", {}) or {}
        if "prompt_tokens" in usage:
            return int(usage.get("prompt_tokens", 0) or 0)
        return int(usage.get("total_input_tokens", 0) or 0)

    def _get_record_output_tokens(self, record: dict) -> int:
        usage = (record or {}).get("token_usage", {}) or {}
        if "completion_tokens" in usage:
            return int(usage.get("completion_tokens", 0) or 0)
        return int(usage.get("llm_output_tokens", 0) or 0)

    def _save_partial_results(self, results_map: dict):
        # Persist partial generation results so we can resume safely after interruption.
        with self._file_lock:
            sorted_results = [results_map[i] for i in sorted(results_map.keys())]
            dataset_name = self.config.get('dataset_name', 'Unknown_Dataset')
            save_data = {
                "summary": {"dataset": dataset_name, "total_queries": len(sorted_results)},
                "results": sorted_results
            }
            with open(self.generated_file, "w", encoding="utf-8") as f:
                json.dump(save_data, f, indent=2, ensure_ascii=False)

    def _save_partial_eval_results(self, eval_results_map: dict):
        # Persist partial evaluation results so we can resume safely after interruption.
        with self._file_lock:
            eval_records = list(eval_results_map.values())
            with open(self.eval_file, "w", encoding="utf-8") as f:
                json.dump({"results": eval_records}, f, indent=2, ensure_ascii=False)

    def run_import(self):
        """Stage: Import documents into OV store"""
        self.logger.info(">>> Stage: Import (Data Prepare + Ingest)")

        if not self.db:
            raise RuntimeError("Cannot ingest without a vector store. Disable use_nanobot to use import.")

        doc_dir = self.config['paths'].get('doc_output_dir')
        if not doc_dir:
            doc_dir = os.path.join(self.output_dir, "docs")

        try:
            doc_info = self.adapter.data_prepare(doc_dir)
        except Exception as e:
            self.logger.exception(f"Data preparation failed: {e}")
            exit(1)

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
        self.logger.info(f"Import finished. Time: {ingest_stats['time']:.2f}s")

        if self.db:
            self.db.close()

        self._update_report({
            "Insertion Efficiency (Total Dataset)": {
                "Total Insertion Time (s)": self.metrics_summary["insertion"]["time"],
                "Total Input Tokens": self.metrics_summary["insertion"]["input_tokens"],
                "Total Output Tokens": self.metrics_summary["insertion"]["output_tokens"],
                "Total Embedding Tokens": self.metrics_summary["insertion"].get("embedding_tokens", 0)
            }
        })

    def run_generation(self):
        """Stage: Generate answers for QA queries"""
        self.logger.info(">>> Stage: Generation (Retrieve + Generate)")
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
                    try:
                        with open(self.generated_file, "r", encoding="utf-8") as f:
                            saved_data = json.load(f)
                        for result in saved_data.get("results", []):
                            results_map[result["_global_index"]] = result
                    except Exception as e:
                        self.logger.warning(f"Failed to load previous generated results, continuing fresh: {e}")

        remaining_tasks = [task for task in tasks if task["id"] not in completed_tasks]
        self.logger.info(f"Total tasks: {len(tasks)}, Remaining: {len(remaining_tasks)}")
        
        mode = self.config.get("execution", {}).get("mode")
        if mode is None:
            if self.config.get("execution", {}).get("use_nanobot", False):
                mode = "nanobot"
            elif self.config.get("execution", {}).get("use_vikingbot", False):
                mode = "vikingbot"
            else:
                mode = "standard"

        mode_dispatch = {
            "standard": self._process_generation_task,
            "vikingbot": self._process_vikingbot_task,
            "nanobot": self._process_nanobot_task,
            "ov_fallback_bot": self._process_ov_fallback_bot_task,
            "ov_fallback_bot_relations": self._process_ov_fallback_bot_relations_task,
        }
        process_fn = mode_dispatch[mode]

        if remaining_tasks:
            initial_completed = len(completed_tasks)
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_task = {
                    executor.submit(process_fn, task): task
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
                        "Average Input Tokens": sum(self._get_record_input_tokens(r) for r in sorted_results) / total,
                        "Average Output Tokens": sum(self._get_record_output_tokens(r) for r in sorted_results) / total,
                    }
                }
            )
        with open(self.generated_file, "w", encoding="utf-8") as f:
            json.dump(save_data, f, indent=2, ensure_ascii=False)
        self.checkpoint_manager.delete_checkpoint()

    def run_evaluation(self):
        """Step 4: Evaluation"""
        self.logger.info(">>> Stage: Evaluation")

        if not os.path.exists(self.generated_file):
            self.logger.error("Generated answers file not found.")
            return

        with open(self.generated_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            items = data.get("results", [])

        # Recompute generation-stage efficiency metrics from generated answers file.
        # This keeps report consistent even if generation was resumed/partially updated.
        total_items = len(items)
        if total_items > 0:
            avg_latency = sum((i.get("retrieval", {}) or {}).get("latency_sec", 0) for i in items) / total_items
            avg_in_tokens = (
                sum(self._get_record_input_tokens(i) for i in items) / total_items
            )
            avg_out_tokens = (
                sum(self._get_record_output_tokens(i) for i in items) / total_items
            )
            self._update_report(
                {
                    "Query Efficiency (Average Per Query)": {
                        "Average Retrieval Time (s)": avg_latency,
                        "Average Input Tokens": avg_in_tokens,
                        "Average Output Tokens": avg_out_tokens,
                    }
                }
            )

        eval_items = items
        eval_results_map = {}

        completed_eval_tasks: Set[int] = set()
        if self.resume:
            completed_eval_tasks = self.checkpoint_manager.get_completed_tasks("evaluation")
            if completed_eval_tasks:
                self.logger.info(f"Resuming from checkpoint. {len(completed_eval_tasks)} evaluations already completed.")
                if os.path.exists(self.eval_file):
                    try:
                        with open(self.eval_file, "r", encoding="utf-8") as f:
                            saved_eval_data = json.load(f)
                        for result in saved_eval_data.get("results", []):
                            eval_results_map[result["_global_index"]] = result
                    except Exception as e:
                        self.logger.warning(f"Failed to load previous eval results, continuing fresh: {e}")

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
            self._update_report({
                "Dataset": self.config.get('dataset_name', 'Unknown_Dataset'),
                "Total Queries Evaluated": total,
                "Performance Metrics": {
                    "Average F1 Score": sum(r['metrics']['F1'] for r in eval_records) / total,
                    "Average Recall": sum(r['metrics']['Recall'] for r in eval_records) / total,
                    "Average Accuracy (Hit 0-4)": sum(r['metrics']['Accuracy'] for r in eval_records) / total,
                    "Average Accuracy (normalization)": (sum(r['metrics']['Accuracy'] for r in eval_records) / total)/4,
                }
            })

            # Relations Usage report from vikingbot records
            vb_records = [r for r in eval_records if 'vikingbot' in r]
            if vb_records:
                relations_hits_list = [r['vikingbot'].get('relations_hits', 0) for r in vb_records]
                total_relations_list = [r['vikingbot'].get('total_relations_found', 0) for r in vb_records]
                report_relations = {
                    "Total Questions with Relations Hits": sum(1 for h in relations_hits_list if h > 0),
                    "Total Relations Found": sum(total_relations_list),
                    "Average Relations Found per Query": sum(total_relations_list) / len(total_relations_list),
                    "Relations Utilization Rate": sum(1 for h in relations_hits_list if h > 0) / len(relations_hits_list),
                }

                # Link Construction Rate
                enable_linking = self.config.get('vikingbot', {}).get('enable_linking', False)
                if enable_linking:
                    queries_with_links = 0
                    total_links = 0
                    queries_parse_failure = 0
                    queries_link_parse_failed = 0
                    for r in vb_records:
                        tc_list = r['vikingbot'].get('tool_calls', [])
                        if isinstance(tc_list, str):
                            try:
                                tc_list = json.loads(tc_list, strict=False)
                            except (json.JSONDecodeError, TypeError):
                                queries_parse_failure += 1
                                continue
                        if not isinstance(tc_list, list):
                            queries_parse_failure += 1
                            continue
                        query_links = 0
                        for tc in tc_list:
                            if not isinstance(tc, dict):
                                continue
                            if tc.get('tool_name') == 'openviking_link':
                                if not tc.get('execute_success', True):
                                    queries_link_parse_failed += 1
                                    continue
                                args_data = tc.get('args', {})
                                if isinstance(args_data, str):
                                    try:
                                        args_data = json.loads(args_data)
                                    except (json.JSONDecodeError, TypeError):
                                        queries_link_parse_failed += 1
                                        continue
                                if isinstance(args_data, dict):
                                    to_uris = args_data.get('to_uris', [])
                                    from_uris = args_data.get('from_uris', [])
                                    query_links += len(from_uris) * len(to_uris)
                        if query_links > 0:
                            queries_with_links += 1
                        total_links += query_links
                    report_relations["Link Construction Rate"] = queries_with_links / len(vb_records)
                    report_relations["Queries With Links Created"] = queries_with_links
                    report_relations["Queries Without Links"] = len(vb_records) - queries_with_links
                    report_relations["Total Links Created"] = total_links
                    if queries_parse_failure > 0:
                        report_relations["Queries Parse Failure"] = queries_parse_failure
                    if queries_link_parse_failed > 0:
                        report_relations["Link Parse Failed"] = queries_link_parse_failed

                # Edge Coverage Rate
                use_relations = self.config.get('vikingbot', {}).get('use_relations', False)
                if use_relations:
                    strategy = self.config.get('vikingbot', {}).get('link_strategy', 'llm_review')
                    total_edges_count, all_edges = self._count_total_relations(strategy)
                    hit_edges = set()
                    for r in vb_records:
                        tc_list = r['vikingbot'].get('tool_calls', [])
                        if isinstance(tc_list, str):
                            try:
                                tc_list = json.loads(tc_list)
                            except (json.JSONDecodeError, TypeError):
                                tc_list = []
                        for tc in (tc_list if isinstance(tc_list, list) else []):
                            if not isinstance(tc, dict) or tc.get('tool_name') != 'openviking_search':
                                continue
                            result_data = tc.get('result')
                            if not isinstance(result_data, list):
                                continue
                            for item in result_data:
                                if not isinstance(item, dict):
                                    continue
                                mr = item.get('match_reason', '')
                                if mr.startswith('relation_from:'):
                                    src = mr.replace('relation_from:', '').strip()
                                    tgt = item.get('uri', '')
                                    if src and tgt:
                                        hit_edges.add((min(src, tgt), max(src, tgt)))
                    hit_count = len(hit_edges & all_edges) if all_edges else 0
                    coverage = hit_count / total_edges_count if total_edges_count > 0 else 0.0
                    report_relations["Total Edges in Store"] = total_edges_count
                    report_relations["Unique Edges Hit"] = hit_count
                    report_relations["Edge Coverage Rate"] = round(coverage, 4)

                self._update_report({"Relations Usage": report_relations})

                # VikingBot Iteration Metrics
                vb_valid = [r for r in vb_records if r['vikingbot'].get('tool_calls')]
                records_for_iters = vb_valid if vb_valid else vb_records

                iters_total = [r['vikingbot'].get('iterations_used', 0) for r in records_for_iters]
                iters_search = [r['vikingbot'].get('search_iterations', 0) for r in records_for_iters]
                iters_read = [r['vikingbot'].get('read_iterations', 0) for r in records_for_iters]
                iters_retrieval = [r['vikingbot'].get('retrieval_iterations', r['vikingbot'].get('iterations_used', 0)) for r in records_for_iters]

                if records_for_iters:
                    self._update_report({
                        "VikingBot Iteration Metrics": {
                            "Average Total Iterations": sum(iters_total) / len(iters_total),
                            "Average Retrieval Iterations (excl. link/relations)": sum(iters_retrieval) / len(iters_retrieval),
                            "Average Search Iterations": sum(iters_search) / len(iters_search),
                            "Average Read Iterations": sum(iters_read) / len(iters_read),
                            "Min Retrieval Iterations": min(iters_retrieval),
                            "Max Retrieval Iterations": max(iters_retrieval),
                            "Excluded Anomalous Records (tc=0)": len(vb_records) - len(vb_valid),
                        }
                    })

            # Fallback mode statistics
            fallback_records = [r for r in eval_records if 'fallback' in r]
            if fallback_records:
                triggered = [r for r in fallback_records if r['fallback'].get('triggered')]
                not_triggered = [r for r in fallback_records if not r['fallback'].get('triggered')]
                fb_total = len(fallback_records)

                avg_total_latency = sum(r['fallback']['total_latency_sec'] for r in fallback_records) / fb_total
                avg_total_input = sum(r['fallback']['total_input_tokens'] for r in fallback_records) / fb_total
                avg_total_output = sum(r['fallback']['total_output_tokens'] for r in fallback_records) / fb_total

                avg_ov_latency = sum(r['fallback']['ov_retrieval_sec'] for r in fallback_records) / fb_total
                avg_judge_latency = sum(r['fallback']['judge_latency_sec'] for r in fallback_records) / fb_total
                avg_judge_tokens = sum(r['fallback']['judge_input_tokens'] + r['fallback']['judge_output_tokens'] for r in fallback_records) / fb_total

                fallback_report = {
                    "Total Queries": fb_total,
                    "Fallback Trigger Rate": len(triggered) / fb_total,
                    "Triggered Count": len(triggered),
                    "Not Triggered Count": len(not_triggered),
                    "Average Total Latency (s)": avg_total_latency,
                    "Average Total Input Tokens": avg_total_input,
                    "Average Total Output Tokens": avg_total_output,
                    "Average OV Retrieval Latency (s)": avg_ov_latency,
                    "Average Judge Latency (s)": avg_judge_latency,
                    "Average Judge Tokens": avg_judge_tokens,
                }

                if triggered:
                    avg_bot_latency = sum(r['fallback']['bot_latency_sec'] for r in triggered) / len(triggered)
                    avg_bot_tokens = sum(r['fallback']['bot_input_tokens'] + r['fallback']['bot_output_tokens'] for r in triggered) / len(triggered)
                    fallback_report["Average Bot Latency (triggered) (s)"] = avg_bot_latency
                    fallback_report["Average Bot Tokens (triggered)"] = avg_bot_tokens
                    fallback_report["Average Accuracy (triggered)"] = sum(r['metrics']['Accuracy'] for r in triggered) / len(triggered)

                if not_triggered:
                    fallback_report["Average Accuracy (not triggered)"] = sum(r['metrics']['Accuracy'] for r in not_triggered) / len(not_triggered)

                fallback_report["Average Accuracy (overall)"] = sum(r['metrics']['Accuracy'] for r in fallback_records) / fb_total

                self._update_report({"Fallback Mode Statistics": fallback_report})
        self.checkpoint_manager.delete_checkpoint()

    def run_deletion(self):
        """Step 5: Cleanup"""
        self.logger.info(">>> Stage: Deletion")
        if not self.db:
            self.logger.info("No vector store to delete (nanobot mode)")
            return
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
        env_max = os.environ.get("RAG_MAX_QUERIES")
        if env_max is not None:
            env_max_stripped = env_max.strip().lower()
            if env_max_stripped in ("", "null", "none"):
                max_queries = None
            else:
                max_queries = int(env_max_stripped)
        for sample in samples:
            for qa in sample.qa_pairs:
                if max_queries is not None and global_idx >= max_queries:
                    break
                tasks.append({"id": global_idx, "sample_id": sample.sample_id, "qa": qa})
                global_idx += 1
            if max_queries is not None and global_idx >= max_queries:
                break
        return tasks

    def _process_vikingbot_task(self, task):
        self.monitor.worker_start()
        try:
            qa = task['qa']
            self.logger.info(f"[Query-{task['id']}] Using VikingBot for agentic RAG")

            session_id = f"query_{uuid.uuid4().hex}"

            restrict_to_qa_doc = bool(self.config.get("execution", {}).get("restrict_to_qa_doc", False))
            allowed_target_uris = self._resolve_target_uris(task, qa) if restrict_to_qa_doc else None

            vikingbot_result = run_vikingbot_query(
                question=qa.question,
                config=self.config,
                session_id=session_id,
                allowed_target_uris=allowed_target_uris,
            )

            ans = vikingbot_result.get("answer", "")
            total_time_sec = vikingbot_result.get("total_time_sec", 0)
            token_usage = vikingbot_result.get("token_usage", {})
            tools_used_names = vikingbot_result.get("tools_used_names", [])
            tools_used_raw = vikingbot_result.get("tools_used", [])
            iterations_used = vikingbot_result.get("iterations_used", 0)

            # Align with current benchmark: VikingBot reports prompt/completion token usage.
            # Keep backward compatibility if upstream ever returns input/output keys.
            prompt_tokens = int(
                token_usage.get("prompt_tokens", token_usage.get("input_tokens", 0)) or 0
            )
            completion_tokens = int(
                token_usage.get("completion_tokens", token_usage.get("output_tokens", 0)) or 0
            )
            total_tokens = int(token_usage.get("total_tokens") or (prompt_tokens + completion_tokens))
            self.monitor.worker_end(tokens=prompt_tokens + completion_tokens)

            self.logger.info(f"[Query-{task['id']}] VikingBot | Iterations: {iterations_used} | Time: {total_time_sec:.1f}s")

            trace = vikingbot_result.get("trace", "")
            trace_file = ""
            if trace:
                trace_dir = os.path.join(self.output_dir, "traces")
                os.makedirs(trace_dir, exist_ok=True)
                trace_file = os.path.join(trace_dir, f"query_{task['id']}_trace.txt")
                try:
                    trace_data = json.loads(trace, strict=False)
                    with open(trace_file, "w", encoding="utf-8") as f:
                        json.dump(trace_data, f, ensure_ascii=False, indent=2, default=str)
                except json.JSONDecodeError:
                    with open(trace_file, "w", encoding="utf-8") as f:
                        f.write(trace)

            # Compute per-query relations/link metrics from tool_calls
            search_iterations = 0
            read_iterations = 0
            relations_hits = 0
            total_relations_found = 0
            links_created = 0
            relation_edges_hit = []
            read_tool_names = {"openviking_multi_read", "openviking_read"}
            tc_list = tools_used_raw if isinstance(tools_used_raw, list) else []
            if isinstance(tools_used_raw, str):
                try:
                    tc_list = json.loads(tools_used_raw)
                except (json.JSONDecodeError, TypeError):
                    tc_list = []
            for tc in tc_list:
                if not isinstance(tc, dict):
                    continue
                tn = tc.get('tool_name', '')
                if tn == 'openviking_search':
                    search_iterations += 1
                    rf = tc.get('relations_found', 0) or 0
                    total_relations_found += rf
                    if rf > 0:
                        relations_hits += 1
                    result_data = tc.get('result')
                    if isinstance(result_data, list):
                        for item in result_data:
                            if not isinstance(item, dict):
                                continue
                            mr = item.get('match_reason', '')
                            if mr.startswith('relation_from:'):
                                src = mr.replace('relation_from:', '').strip()
                                tgt = item.get('uri', '')
                                if src and tgt:
                                    relation_edges_hit.append((min(src, tgt), max(src, tgt)))
                elif tn in read_tool_names:
                    read_iterations += 1
                elif tn == 'openviking_link':
                    args_data = tc.get('args', {})
                    if isinstance(args_data, str):
                        try:
                            args_data = json.loads(args_data)
                        except (json.JSONDecodeError, TypeError):
                            args_data = {}
                    to_uris = args_data.get('to_uris', []) if isinstance(args_data, dict) else []
                    if to_uris:
                        from_uris = args_data.get('from_uris', [])
                        links_created += len(from_uris) * len(to_uris)

            # Calculate retrieval-only iterations (excluding link tool calls)
            link_tools = {'openviking_link', 'openviking_relations'}
            total_calls = len(tc_list) if tc_list else 1
            non_link_call_count = sum(1 for tc in tc_list if isinstance(tc, dict) and tc.get('tool_name', '') not in link_tools)
            retrieval_iterations = max(1, round(iterations_used * non_link_call_count / total_calls)) if iterations_used > 0 else iterations_used

            return {
                "_global_index": task['id'], "sample_id": task['sample_id'], "question": qa.question,
                "gold_answers": qa.gold_answers, "category": str(qa.category), "evidence": qa.evidence,
                "retrieval": {"latency_sec": total_time_sec, "uris": []},
                "llm": {"final_answer": ans},
                "vikingbot": {
                    "iterations_used": iterations_used,
                    "retrieval_iterations": retrieval_iterations,
                    "search_iterations": search_iterations,
                    "read_iterations": read_iterations,
                    "tools_used_names": tools_used_names,
                    "tool_calls": tc_list,
                    "total_time_sec": total_time_sec,
                    "debug_log": vikingbot_result.get("debug_log", ""),
                    "session_id": vikingbot_result.get("session_id", ""),
                    "trace_file": trace_file,
                    "relations_hits": relations_hits,
                    "total_relations_found": total_relations_found,
                    "links_created": links_created,
                    "relation_edges_hit": relation_edges_hit,
                },
                "metrics": {"Recall": 0.0},
                "token_usage": {
                    # Keep legacy simple-RAG fields present for schema compatibility,
                    # but set them to 0 for bot mode to avoid mixing semantics.
                    "total_input_tokens": 0,
                    "llm_output_tokens": 0,
                    "retrieval_embedding_tokens": 0,
                    # VikingBot-native names aligned with vikingbot JSON output.
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                },
            }
        except Exception:
            self.monitor.worker_end(success=False)
            raise

    def _resolve_target_uris(self, task, qa):
        evidence = getattr(qa, 'evidence', []) or []
        if not evidence:
            return None
        uris = []
        for ev in evidence:
            if isinstance(ev, str) and ev.startswith("viking://"):
                parts = ev.rstrip("/").split("/")
                if len(parts) >= 5:
                    uris.append("/".join(parts[:5]))
        return list(set(uris)) if uris else None

    def _process_nanobot_task(self, task):
        self.monitor.worker_start()
        try:
            qa = task['qa']
            self.logger.info(f"[Query-{task['id']}] Using Nanobot (grep/glob) for RAG")

            session_id = f"query_{uuid.uuid4().hex}"

            nanobot_result = run_nanobot_query(
                question=qa.question,
                config=self.config,
                session_id=session_id,
            )

            ans = nanobot_result.get("answer", "")
            total_time_sec = nanobot_result.get("total_time_sec", 0)
            token_usage = nanobot_result.get("token_usage", {})
            tools_used_names = nanobot_result.get("tools_used_names", [])
            iterations_used = nanobot_result.get("iterations_used", 0)

            prompt_tokens = int(
                token_usage.get("prompt_tokens", token_usage.get("input_tokens", 0)) or 0
            )
            completion_tokens = int(
                token_usage.get("completion_tokens", token_usage.get("output_tokens", 0)) or 0
            )
            total_tokens = int(token_usage.get("total_tokens") or (prompt_tokens + completion_tokens))
            self.monitor.worker_end(tokens=prompt_tokens + completion_tokens)

            self.logger.info(f"[Query-{task['id']}] Nanobot | Time: {total_time_sec:.1f}s")

            return {
                "_global_index": task['id'], "sample_id": task['sample_id'], "question": qa.question,
                "gold_answers": qa.gold_answers, "category": str(qa.category), "evidence": qa.evidence,
                "retrieval": {"latency_sec": total_time_sec, "uris": []},
                "llm": {"final_answer": ans},
                "nanobot": {
                    "iterations_used": iterations_used,
                    "tools_used_names": tools_used_names,
                    "total_time_sec": total_time_sec,
                    "session_id": nanobot_result.get("session_id", ""),
                },
                "metrics": {"Recall": 0.0},
                "token_usage": {
                    "total_input_tokens": 0,
                    "llm_output_tokens": 0,
                    "retrieval_embedding_tokens": 0,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                },
            }
        except Exception:
            self.monitor.worker_end(success=False)
            raise

    def _process_generation_task(self, task):
        self.monitor.worker_start()
        try:
            qa = task['qa']
            
            t0 = time.time()
            # Get retrieval instruction from config, default to empty
            retrieval_instruction = self.config['execution'].get('retrieval_instruction', '')
            # Build enhanced query with instruction if provided
            if retrieval_instruction:
                enhanced_query = f"{retrieval_instruction} {qa.question}"
                self.logger.debug(f"[Query-{task['id']}] Using retrieval instruction: {retrieval_instruction}")
                self.logger.debug(f"[Query-{task['id']}] Enhanced query: {enhanced_query}")
            else:
                enhanced_query = qa.question
                self.logger.debug(f"[Query-{task['id']}] No retrieval instruction, using raw query")
            search_res = self.db.retrieve(query=enhanced_query, topk=self.config['execution']['retrieval_topk'])
            latency = time.time() - t0

            recall_texts = search_res["recall_texts"]
            context_blocks = search_res["context_blocks"]
            retrieved_uris = search_res["retrieved_uris"]

            retrieved_texts = list(recall_texts.values())
            recall = MetricsCalculator.check_recall(retrieved_texts, qa.evidence)
            
            full_prompt, meta = self.adapter.build_prompt(qa, context_blocks)
            
            ans_raw = self.llm.generate(full_prompt)

            ans = self.adapter.post_process_answer(qa, ans_raw, meta)

            in_tokens = self.db.count_tokens(full_prompt) + self.db.count_tokens(qa.question)
            out_tokens = self.db.count_tokens(ans)
            self.monitor.worker_end(tokens=in_tokens + out_tokens)
            
            self.logger.info(f"[Query-{task['id']}] Q: {qa.question[:30]}... | Recall: {recall:.2f} | Latency: {latency:.2f}s")

            return {
                "_global_index": task['id'], "sample_id": task['sample_id'], "question": qa.question,
                "gold_answers": qa.gold_answers, "category": str(qa.category), "evidence": qa.evidence,
                "retrieval": {"latency_sec": latency, "uris": retrieved_uris},
                "llm": {"final_answer": ans},
                "metrics": {"Recall": recall}, "token_usage": {"total_input_tokens": in_tokens, "llm_output_tokens": out_tokens}
            }
        except Exception:
            self.monitor.worker_end(success=False)
            raise

    def _process_ov_fallback_bot_task(self, task):
        return self._process_fallback_task(task, bot_use_relations=False)

    def _process_ov_fallback_bot_relations_task(self, task):
        return self._process_fallback_task(task, bot_use_relations=True)

    def _process_fallback_task(self, task, bot_use_relations: bool):
        self.monitor.worker_start()
        try:
            qa = task['qa']
            fallback_config = self.config.get("execution", {}).get("fallback", {})
            score_threshold = fallback_config.get("score_threshold", 2)

            # --- Phase 1: OV retrieval + generation ---
            t0 = time.time()
            retrieval_instruction = self.config['execution'].get('retrieval_instruction', '')
            enhanced_query = f"{retrieval_instruction} {qa.question}" if retrieval_instruction else qa.question
            search_res = self.db.retrieve(query=enhanced_query, topk=self.config['execution']['retrieval_topk'])
            ov_retrieval_sec = time.time() - t0

            recall_texts = search_res["recall_texts"]
            context_blocks = search_res["context_blocks"]
            retrieved_uris = search_res["retrieved_uris"]

            retrieved_texts = list(recall_texts.values())
            recall = MetricsCalculator.check_recall(retrieved_texts, qa.evidence)

            full_prompt, meta = self.adapter.build_prompt(qa, context_blocks)

            t1 = time.time()
            ans_raw = self.llm.generate(full_prompt)
            ov_generation_sec = time.time() - t1

            ov_answer = self.adapter.post_process_answer(qa, ans_raw, meta)

            ov_in_tokens = self.db.count_tokens(full_prompt) + self.db.count_tokens(qa.question)
            ov_out_tokens = self.db.count_tokens(ov_answer)

            # --- Phase 2: LLM Judge ---
            dataset_name = self.config.get('dataset_name', 'Unknown_Dataset')
            judge_result = llm_grader(
                self.llm.llm,
                self.config['llm']['model'],
                qa.question,
                qa.gold_answers,
                ov_answer,
                dataset_name=dataset_name,
            )
            judge_score = judge_result["score"]
            judge_input_tokens = judge_result.get("judge_input_tokens", 0)
            judge_output_tokens = judge_result.get("judge_output_tokens", 0)
            judge_latency_sec = judge_result.get("judge_latency_sec", 0)

            # --- Phase 3: Fallback decision ---
            fallback_triggered = judge_score < score_threshold
            final_answer = ov_answer
            bot_latency_sec = 0
            bot_input_tokens = 0
            bot_output_tokens = 0
            bot_detail = None

            if fallback_triggered:
                self.logger.info(
                    f"[Query-{task['id']}] Fallback triggered (score={judge_score} < {score_threshold}), "
                    f"calling bot (relations={bot_use_relations})"
                )
                bot_config = copy.deepcopy(self.config)
                bot_config.setdefault('vikingbot', {})['use_relations'] = bot_use_relations

                session_id = f"fallback_{uuid.uuid4().hex}"
                restrict_to_qa_doc = bool(self.config.get("execution", {}).get("restrict_to_qa_doc", False))
                allowed_target_uris = self._resolve_target_uris(task, qa) if restrict_to_qa_doc else None

                vikingbot_result = run_vikingbot_query(
                    question=qa.question,
                    config=bot_config,
                    session_id=session_id,
                    allowed_target_uris=allowed_target_uris,
                )

                final_answer = vikingbot_result.get("answer", "")
                bot_latency_sec = vikingbot_result.get("total_time_sec", 0)
                bot_token_usage = vikingbot_result.get("token_usage", {})
                bot_input_tokens = int(bot_token_usage.get("prompt_tokens", bot_token_usage.get("input_tokens", 0)) or 0)
                bot_output_tokens = int(bot_token_usage.get("completion_tokens", bot_token_usage.get("output_tokens", 0)) or 0)

                # Extract bot detailed info for reporting
                bot_tools_used_raw = vikingbot_result.get("tools_used", [])
                bot_tc_list = bot_tools_used_raw if isinstance(bot_tools_used_raw, list) else []
                if isinstance(bot_tools_used_raw, str):
                    try:
                        bot_tc_list = json.loads(bot_tools_used_raw)
                    except (json.JSONDecodeError, TypeError):
                        bot_tc_list = []

                bot_search_iterations = 0
                bot_read_iterations = 0
                bot_relations_hits = 0
                bot_total_relations_found = 0
                bot_links_created = 0
                bot_relation_edges_hit = []
                read_tool_names = {"openviking_multi_read", "openviking_read"}
                for tc in bot_tc_list:
                    if not isinstance(tc, dict):
                        continue
                    tn = tc.get('tool_name', '')
                    if tn == 'openviking_search':
                        bot_search_iterations += 1
                        rf = tc.get('relations_found', 0) or 0
                        bot_total_relations_found += rf
                        if rf > 0:
                            bot_relations_hits += 1
                        result_data = tc.get('result')
                        if isinstance(result_data, list):
                            for item in result_data:
                                if not isinstance(item, dict):
                                    continue
                                mr = item.get('match_reason', '')
                                if mr.startswith('relation_from:'):
                                    src = mr.replace('relation_from:', '').strip()
                                    tgt = item.get('uri', '')
                                    if src and tgt:
                                        bot_relation_edges_hit.append((min(src, tgt), max(src, tgt)))
                    elif tn in read_tool_names:
                        bot_read_iterations += 1
                    elif tn == 'openviking_link':
                        args_data = tc.get('args', {})
                        if isinstance(args_data, str):
                            try:
                                args_data = json.loads(args_data)
                            except (json.JSONDecodeError, TypeError):
                                args_data = {}
                        to_uris = args_data.get('to_uris', []) if isinstance(args_data, dict) else []
                        if to_uris:
                            from_uris = args_data.get('from_uris', [])
                            bot_links_created += len(from_uris) * len(to_uris)

                bot_iterations_used = vikingbot_result.get("iterations_used", 0)
                link_tools = {'openviking_link', 'openviking_relations'}
                total_calls = len(bot_tc_list) if bot_tc_list else 1
                non_link_call_count = sum(1 for tc in bot_tc_list if isinstance(tc, dict) and tc.get('tool_name', '') not in link_tools)
                bot_retrieval_iterations = max(1, round(bot_iterations_used * non_link_call_count / total_calls)) if bot_iterations_used > 0 else bot_iterations_used

                # Save trace
                bot_trace = vikingbot_result.get("trace", "")
                bot_trace_file = ""
                if bot_trace:
                    trace_dir = os.path.join(self.output_dir, "traces")
                    os.makedirs(trace_dir, exist_ok=True)
                    bot_trace_file = os.path.join(trace_dir, f"query_{task['id']}_fallback_trace.txt")
                    try:
                        trace_data = json.loads(bot_trace, strict=False)
                        with open(bot_trace_file, "w", encoding="utf-8") as f:
                            json.dump(trace_data, f, ensure_ascii=False, indent=2, default=str)
                    except json.JSONDecodeError:
                        with open(bot_trace_file, "w", encoding="utf-8") as f:
                            f.write(bot_trace)

                bot_detail = {
                    "iterations_used": bot_iterations_used,
                    "retrieval_iterations": bot_retrieval_iterations,
                    "search_iterations": bot_search_iterations,
                    "read_iterations": bot_read_iterations,
                    "tools_used_names": vikingbot_result.get("tools_used_names", []),
                    "tool_calls": bot_tc_list,
                    "total_time_sec": bot_latency_sec,
                    "debug_log": vikingbot_result.get("debug_log", ""),
                    "session_id": vikingbot_result.get("session_id", ""),
                    "trace_file": bot_trace_file,
                    "relations_hits": bot_relations_hits,
                    "total_relations_found": bot_total_relations_found,
                    "links_created": bot_links_created,
                    "relation_edges_hit": bot_relation_edges_hit,
                }

            # --- Aggregate metrics ---
            total_input_tokens = ov_in_tokens + bot_input_tokens
            total_output_tokens = ov_out_tokens + bot_output_tokens
            total_latency_sec = ov_retrieval_sec + bot_latency_sec

            self.monitor.worker_end(tokens=total_input_tokens + total_output_tokens)
            self.logger.info(
                f"[Query-{task['id']}] Fallback={'YES' if fallback_triggered else 'NO'} | "
                f"JudgeScore={judge_score} | Total: {total_latency_sec:.1f}s"
            )

            result = {
                "_global_index": task['id'], "sample_id": task['sample_id'], "question": qa.question,
                "gold_answers": qa.gold_answers, "category": str(qa.category), "evidence": qa.evidence,
                "retrieval": {"latency_sec": total_latency_sec, "uris": retrieved_uris},
                "llm": {"final_answer": final_answer},
                "metrics": {"Recall": recall},
                "token_usage": {
                    "total_input_tokens": total_input_tokens,
                    "llm_output_tokens": total_output_tokens,
                    "retrieval_embedding_tokens": 0,
                    "prompt_tokens": total_input_tokens,
                    "completion_tokens": total_output_tokens,
                    "total_tokens": total_input_tokens + total_output_tokens,
                },
                "fallback": {
                    "triggered": fallback_triggered,
                    "bot_use_relations": bot_use_relations,
                    "ov_judge_score": judge_score,
                    "ov_judge_reasoning": judge_result.get("reasoning", ""),
                    "score_threshold": score_threshold,
                    "ov_answer": ov_answer,
                    "ov_retrieval_sec": ov_retrieval_sec,
                    "ov_generation_sec": ov_generation_sec,
                    "judge_latency_sec": judge_latency_sec,
                    "bot_latency_sec": bot_latency_sec,
                    "ov_input_tokens": ov_in_tokens,
                    "ov_output_tokens": ov_out_tokens,
                    "judge_input_tokens": judge_input_tokens,
                    "judge_output_tokens": judge_output_tokens,
                    "bot_input_tokens": bot_input_tokens,
                    "bot_output_tokens": bot_output_tokens,
                    "total_latency_sec": total_latency_sec,
                    "total_input_tokens": total_input_tokens,
                    "total_output_tokens": total_output_tokens,
                },
            }
            if bot_detail:
                result["vikingbot"] = bot_detail
            return result
        except Exception:
            self.monitor.worker_end(success=False)
            raise

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
            f"\n[VikingBot Trace File]: {item.get('vikingbot', {}).get('trace_file', '')}"
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

    def _count_total_relations(self, strategy: str):
        """Count total unique edge pairs in all .relations_{strategy}.jsonl files."""
        vector_store_path = self.config.get('paths', {}).get('vector_store', '')
        if not vector_store_path or not os.path.isdir(vector_store_path):
            return 0, set()
        viking_dir = os.path.join(vector_store_path, "viking")
        if not os.path.isdir(viking_dir):
            return 0, set()
        filename = ".relations.jsonl" if strategy == "blind" else f".relations_{strategy}.jsonl"
        all_edges = set()
        for root, _dirs, files in os.walk(viking_dir):
            if filename in files:
                fpath = os.path.join(root, filename)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                rec = json.loads(line)
                                uri1 = rec.get("uri1", "")
                                uri2 = rec.get("uri2", "")
                                if uri1 and uri2:
                                    all_edges.add((min(uri1, uri2), max(uri1, uri2)))
                            except json.JSONDecodeError:
                                continue
                except Exception:
                    continue
        return len(all_edges), all_edges
