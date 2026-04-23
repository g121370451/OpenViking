import os
import json
import time
import uuid
import random
import re
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

    def run_generation(self):
        """Step 1: Data Preparation"""
        self.logger.info(">>> Stage: Ingestion & Generation")
        skip_ingestion = self.config['execution'].get('skip_ingestion', False)
        doc_dir = self.config['paths'].get('doc_output_dir')
        if not doc_dir:
            doc_dir = os.path.join(self.output_dir, "docs")

        if skip_ingestion:
            self.logger.info(f"Skipping ingestion. Reusing existing vector index at: {self.db.store_path}")
            self.metrics_summary["insertion"] = {"time": 0, "input_tokens": 0, "output_tokens": 0, "embedding_tokens": 0}
        else:
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
                    try:
                        with open(self.generated_file, "r", encoding="utf-8") as f:
                            saved_data = json.load(f)
                        for result in saved_data.get("results", []):
                            results_map[result["_global_index"]] = result
                    except Exception as e:
                        self.logger.warning(f"Failed to load previous generated results, continuing fresh: {e}")

        remaining_tasks = [task for task in tasks if task["id"] not in completed_tasks]
        self.logger.info(f"Total tasks: {len(tasks)}, Remaining: {len(remaining_tasks)}")
        
        use_vikingbot = self.config.get("execution", {}).get("use_vikingbot", False)
        process_fn = self._process_vikingbot_task if use_vikingbot else self._process_generation_task

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
        self.checkpoint_manager.delete_checkpoint()

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

            return {
                "_global_index": task['id'], "sample_id": task['sample_id'], "question": qa.question,
                "gold_answers": qa.gold_answers, "category": str(qa.category), "evidence": qa.evidence,
                "retrieval": {"latency_sec": total_time_sec, "uris": []},
                "llm": {"final_answer": ans},
                "vikingbot": {
                    "iterations_used": iterations_used,
                    "tools_used_names": tools_used_names,
                    "total_time_sec": total_time_sec,
                    "debug_log": vikingbot_result.get("debug_log", ""),
                    "session_id": vikingbot_result.get("session_id", ""),
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
            
            retrieved_texts = []
            retrieved_uris = []
            context_blocks = []
            
            for r in search_res.resources:
                retrieved_uris.append(r.uri)
                content = self.db.read_resource(r.uri) if getattr(r, 'level', 2) == 2 else f"{getattr(r, 'abstract', '')}\n{getattr(r, 'overview', '')}"
                retrieved_texts.append(content)
                clean = content[:8000]
                context_blocks.append(clean)
            
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
