#!/usr/bin/env python3
"""
Debug script: manually test search + relations filtering for a single question.
Run this directly in IDE with breakpoints to debug the relations lookup flow.

Usage:
    python query_relations_question_input.py
"""

import os
import sys
import yaml

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(SCRIPT_DIR, "config/financebench_bot_config_link_keyword.yaml")


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CONFIG

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

    print(f"Config: {config_path}")
    print(f"Store: {store_path}")

    import openviking as ov
    client = ov.SyncOpenViking(path=store_path)

    filter_mode = config.get('vikingbot', {}).get('relation_filter_mode', 'none')
    question = input("Question> ").strip() if len(sys.argv) <= 2 else " ".join(sys.argv[2:])

    print(f"\nQuery: {question}")
    print(f"Filter mode: {filter_mode}")
    print(f"{'='*80}")

    # Step 1: Search
    print("\n[Step 1] Searching...")
    results = client.find(query=question, limit=5, target_uri="viking://resources", telemetry=False)
    resources = getattr(results, 'resources', []) or []
    print(f"  Found {len(resources)} results")

    for i, r in enumerate(resources, 1):
        score = getattr(r, 'score', 0)
        print(f"  {i}. [{score:.4f}] {r.uri}")

    # Step 2: Query relations for each search result
    # >>> SET BREAKPOINT HERE to step into relations() <<<
    print(f"\n[Step 2] Querying relations (filter_mode={filter_mode})...")
    for i, r in enumerate(resources, 1):
        uri = r.uri
        print(f"\n  Checking relations for: {uri}")

        rels = client.relations(uri)

        # Apply bot-layer filtering if needed
        if filter_mode == "keyword" and rels:
            sys.path.insert(0, os.path.join(SCRIPT_DIR, "../../bot"))
            from vikingbot.agent.tools.relation_utils import filter_relations_by_keyword
            rels = filter_relations_by_keyword(rels, question)

        if rels:
            print(f"  >>> FOUND {len(rels)} relation(s):")
            for rel in rels:
                rel_uri = rel.get('uri', '')
                score = rel.get('score', '')
                reason = rel.get('reason', '')
                print(f"      -> {rel_uri}  score={score}  reason={reason[:60]}")
        else:
            print(f"  (no relations)")

    print(f"\n{'='*80}")
    print("Done.")


if __name__ == "__main__":
    main()
