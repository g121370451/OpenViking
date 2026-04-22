#!/usr/bin/env python3
"""
Query relations for all questions in a dataset.
Searches each question, then checks relations for search results.
Only prints questions that have relations.

Usage:
    python query_relations.py [config.yaml]
    python query_relations.py [config.yaml] "single question"
"""

import os
import sys
import json
import yaml
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(SCRIPT_DIR, "config/financebench_bot_config_build_links.yaml")


def load_store(config_path: str):
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    dataset_name = config.get('dataset_name', '')
    retrieval_topk = config.get('execution', {}).get('retrieval_topk', 5)
    store_path_template = config.get('paths', {}).get('vector_store', '')
    store_path = store_path_template.format(dataset_name=dataset_name, retrieval_topk=retrieval_topk)

    if not os.path.isabs(store_path):
        store_path = os.path.normpath(os.path.join(SCRIPT_DIR, store_path))

    ov_conf = os.path.join(SCRIPT_DIR, "ov.conf")
    if os.path.exists(ov_conf):
        os.environ["OPENVIKING_CONFIG_FILE"] = ov_conf

    import openviking as ov
    client = ov.SyncOpenViking(path=store_path)
    return client, config


def load_questions(config: dict) -> list[str]:
    """Load questions from the dataset file."""
    dataset_name = config.get('dataset_name', '')
    retrieval_topk = config.get('execution', {}).get('retrieval_topk', 5)
    dataset_path_template = config.get('paths', {}).get('dataset_path', '')
    dataset_path = dataset_path_template.format(dataset_name=dataset_name, retrieval_topk=retrieval_topk)

    if not os.path.isabs(dataset_path):
        dataset_path = os.path.normpath(os.path.join(SCRIPT_DIR, dataset_path))

    if not os.path.exists(dataset_path):
        print(f"Dataset file not found: {dataset_path}")
        return []

    with open(dataset_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    questions = []
    if isinstance(data, list):
        for item in data:
            q = item.get('question') or item.get('query') or item.get('input', '')
            if q:
                questions.append(q)
    elif isinstance(data, dict):
        for item in data.get('data', data.get('samples', [])):
            q = item.get('question') or item.get('query') or item.get('input', '')
            if q:
                questions.append(q)

    return questions


def query_by_question(client, question: str, filter_mode: str = "none", top_k: int = 5, quiet: bool = False):
    """Query relations for a question. Returns total relations found."""
    try:
        results = client.find(query=question, limit=top_k, target_uri="viking://resources", telemetry=False)
        resources = getattr(results, 'resources', []) or []
    except Exception as e:
        if not quiet:
            print(f"  Search error: {e}")
        return 0

    if not resources:
        return 0

    total_relations = 0
    relation_details = []

    for r in resources:
        uri = r.uri
        try:
            rels = client.relations(uri)
        except Exception:
            continue

        if not rels:
            continue

        for rel in rels:
            total_relations += 1
            relation_details.append({
                "from": uri,
                "to": rel.get('uri', ''),
                "reason": rel.get('reason', ''),
                "score": rel.get('score', ''),
            })

    if total_relations > 0:
        print(f"\n{'='*80}")
        print(f"Question: {question}")
        print(f"Filter mode: {filter_mode} | Search results: {len(resources)} | Relations found: {total_relations}")
        print(f"{'-'*80}")
        for d in relation_details:
            from_short = d['from'].rsplit('/', 1)[-1] if '/' in d['from'] else d['from']
            to_short = d['to'].rsplit('/', 1)[-1] if '/' in d['to'] else d['to']
            score_str = f" [score={d['score']}]" if d['score'] else ""
            reason_str = f" (reason: {d['reason'][:60]})" if d['reason'] else ""
            print(f"  {from_short} -> {to_short}{score_str}{reason_str}")

    return total_relations


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CONFIG
    print(f"Loading config: {config_path}")
    client, config = load_store(config_path)

    filter_mode = config.get('vikingbot', {}).get('relation_filter_mode', 'none')

    # Single question mode
    if len(sys.argv) > 2:
        question = " ".join(sys.argv[2:])
        query_by_question(client, question, filter_mode=filter_mode)
        return

    # Batch mode: load all questions from dataset
    questions = load_questions(config)
    if not questions:
        print("No questions found in dataset.")
        return

    print(f"Dataset: {len(questions)} questions")
    print(f"Filter mode: {filter_mode}")
    print(f"Scanning all questions for relations...\n")

    total_with_relations = 0
    total_relations = 0

    for i, question in enumerate(questions, 1):
        sys.stdout.write(f"\r  Scanning {i}/{len(questions)}...")
        sys.stdout.flush()
        count = query_by_question(client, question, filter_mode=filter_mode, quiet=True)
        if count > 0:
            total_with_relations += 1
            total_relations += count
            # Print this one (re-run with output)
            query_by_question(client, question, filter_mode=filter_mode)

    print(f"\n\n{'='*80}")
    print(f"Summary: {total_with_relations}/{len(questions)} questions have relations ({total_relations} total relations)")


if __name__ == "__main__":
    main()
