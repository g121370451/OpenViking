#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Analyze "explicit" L0/L1 usage for bot benchmark runs by scanning vikingbot debug logs.

Strict rule:
- L0 used iff the QA's log slice contains "/.abstract.md"
- L1 used iff the QA's log slice contains "/.overview.md"

Inputs are taken from benchmark outputs:
  benchmark/RAG/Output/<Dataset>/<Experiment>/generated_answers.json
and debug logs are found under:
  benchmark/RAG/ov_storage/<Dataset>/<Dataset>_viking_store_index/bot/log/<debug_log>
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


DATASETS = ["ClapNQ", "FinanceBench", "HotpotQA", "Locomo", "Qasper", "SyllabusQA"]

# These are the experiment dirs observed in the repo for the 5 datasets.
DEFAULT_EXPERIMENT_BY_DATASET = {
    "ClapNQ": "experiment_test_bot_top_1_base",
    "FinanceBench": "experiment_test_bot_top_1_base",
    "HotpotQA": "experiment_test_bot_top_1_base",
    "Locomo": "experiment_test_bot_top_1_L2",
    "Qasper": "experiment_test_bot_top_1_L2",
    "SyllabusQA": "experiment_test_bot_top_1_base",
}


@dataclass(frozen=True)
class QARecord:
    dataset: str
    index: int
    sample_id: str
    question: str
    session_id: str
    debug_log: str


@dataclass
class QAResult:
    dataset: str
    index: int
    sample_id: str
    question: str
    session_id: str
    debug_log: str
    debug_log_path: str
    # "Hit" is URI evidence (what appears in logs, e.g. due to search result display URI)
    hit_l0: bool
    hit_l1: bool
    # "Consumed" tries to approximate whether L0/L1 textual content was actually produced
    # by tools (and thus could be injected into the next LLM call). This is stricter.
    consumed_l0: bool
    consumed_l1: bool
    # Even stricter: did the agent explicitly read/grep the L0/L1 file itself.
    # This excludes "search returned abstract summary" cases.
    read_l0_file: bool
    read_l1_file: bool
    missing_log: bool = False
    unmatched_session: bool = False
    # Context snippets for report examples
    l0_hit_snippet: Optional[str] = None
    l1_hit_snippet: Optional[str] = None
    l0_consumed_snippet: Optional[str] = None
    l1_consumed_snippet: Optional[str] = None


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _ensure_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    return str(v)


def load_generated_answers(path: Path, dataset: str) -> List[QARecord]:
    data = _read_json(path)
    # Current benchmark output format is:
    #   {"summary": {...}, "results": [ ... ]}
    # Keep backward compatibility for older dumps that might be a raw list.
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        data = data["results"]
    if not isinstance(data, list):
        raise ValueError(
            f"Expected list or dict{{results:[]}} in {path}, got {type(data)}"
        )
    records: List[QARecord] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        vikingbot = item.get("vikingbot") or {}
        session_id = _ensure_str(vikingbot.get("session_id"))
        debug_log = _ensure_str(vikingbot.get("debug_log"))
        records.append(
            QARecord(
                dataset=dataset,
                index=i,
                sample_id=_ensure_str(item.get("sample_id")),
                question=_ensure_str(item.get("question")),
                session_id=session_id,
                debug_log=debug_log,
            )
        )
    return records


def _compile_session_markers(session_id: str) -> Tuple[re.Pattern, re.Pattern]:
    # In debug logs we observed patterns like:
    #   Processing message ... chat_id='query_xxx'
    #   Response to ... chat_id='query_xxx'
    start = re.compile(rf"Processing message.*chat_id='{re.escape(session_id)}'")
    end = re.compile(rf"Response to.*chat_id='{re.escape(session_id)}'")
    return start, end


def extract_session_slice(
    log_text: str, session_id: str
) -> Tuple[Optional[str], Optional[int], Optional[int]]:
    """Return (slice_text, start_offset, end_offset) for the given session_id."""
    start_pat, end_pat = _compile_session_markers(session_id)

    start_m = start_pat.search(log_text)
    if not start_m:
        return None, None, None

    # Find the end marker after the start, otherwise slice to EOF.
    end_m = end_pat.search(log_text, start_m.end())
    if end_m:
        return log_text[start_m.start() : end_m.end()], start_m.start(), end_m.end()
    return log_text[start_m.start() :], start_m.start(), len(log_text)


def _snippet_around(text: str, needle: str, ctx_chars: int = 400) -> Optional[str]:
    idx = text.find(needle)
    if idx < 0:
        return None
    start = max(0, idx - ctx_chars)
    end = min(len(text), idx + len(needle) + ctx_chars)
    snippet = text[start:end]
    # Make snippet stable for markdown rendering
    return snippet.strip()


def analyze_one(
    rec: QARecord,
    logs_dir: Path,
    cache: Dict[Path, str],
    ctx_chars: int,
) -> QAResult:
    log_path = logs_dir / rec.debug_log if rec.debug_log else logs_dir / ""
    res = QAResult(
        dataset=rec.dataset,
        index=rec.index,
        sample_id=rec.sample_id,
        question=rec.question,
        session_id=rec.session_id,
        debug_log=rec.debug_log,
        debug_log_path=str(log_path),
        hit_l0=False,
        hit_l1=False,
        consumed_l0=False,
        consumed_l1=False,
        read_l0_file=False,
        read_l1_file=False,
    )

    if not rec.debug_log or not log_path.exists():
        res.missing_log = True
        return res

    if log_path not in cache:
        cache[log_path] = log_path.read_text(encoding="utf-8", errors="replace")

    log_text = cache[log_path]
    if not rec.session_id:
        res.unmatched_session = True
        return res

    slice_text, _, _ = extract_session_slice(log_text, rec.session_id)
    if slice_text is None:
        res.unmatched_session = True
        return res

    # --- Hit metrics (URI evidence) ---
    res.hit_l0 = "/.abstract.md" in slice_text
    res.hit_l1 = "/.overview.md" in slice_text

    # --- Consumed metrics (text content evidence) ---
    #
    # Motivation: a search hit returning a ".overview.md" display URI does NOT imply
    # the overview content was read. In observed logs, search results often include
    # only 'abstract' (summary) and 'overview': None.
    #
    # We approximate "consumed":
    # - L0 consumed if:
    #   - slice contains a ".abstract.md" URI AND we see a non-empty "abstract" field near it, OR
    #   - a multi_read/grep explicitly touches ".abstract.md"
    # - L1 consumed if:
    #   - a multi_read/grep explicitly touches ".overview.md", OR
    #   - we see a non-empty "overview" field in tool output (rare in current logs)
    #
    # This is still evidence-based (log-derived), not a guarantee the model used it.

    has_nonempty_overview_field = bool(
        re.search(r"['\"]overview['\"]\s*:\s*['\"][^'\"]+", slice_text)
    )
    has_nonempty_abstract_field = bool(
        re.search(r"['\"]abstract['\"]\s*:\s*['\"][^'\"]+", slice_text)
    )

    touched_l0_file = "/.abstract.md" in slice_text
    touched_l1_file = "/.overview.md" in slice_text

    # Direct file touch via tool call output text (multi_read) or grep result lines.
    # Note: multi_read logs the full URI list; if that list includes .abstract/.overview, we treat as read-from-file.
    explicit_read_l0 = bool(re.search(r"openviking_multi_read\([^\n]*?\.abstract\.md", slice_text))
    explicit_read_l1 = bool(re.search(r"openviking_multi_read\([^\n]*?\.overview\.md", slice_text))
    explicit_grep_l0 = ("/.abstract.md" in slice_text) and ("openviking_grep" in slice_text)
    explicit_grep_l1 = ("/.overview.md" in slice_text) and ("openviking_grep" in slice_text)

    res.read_l0_file = explicit_read_l0 or explicit_grep_l0
    res.read_l1_file = explicit_read_l1 or explicit_grep_l1

    # L0: search results typically carry a non-empty 'abstract' string (stored in index),
    # so L0 can be "consumed" even without reading /.abstract.md as a file.
    res.consumed_l0 = (touched_l0_file and has_nonempty_abstract_field) or res.read_l0_file
    # L1: search results often have overview=None, so "consumed" usually requires reading the file.
    res.consumed_l1 = has_nonempty_overview_field or res.read_l1_file

    if res.hit_l0:
        res.l0_hit_snippet = _snippet_around(slice_text, "/.abstract.md", ctx_chars=ctx_chars)
    if res.hit_l1:
        res.l1_hit_snippet = _snippet_around(slice_text, "/.overview.md", ctx_chars=ctx_chars)
    if res.consumed_l0:
        res.l0_consumed_snippet = _snippet_around(slice_text, "abstract", ctx_chars=ctx_chars)
    if res.consumed_l1:
        # Prefer the actual overview field; fall back to overview file URI touch.
        res.l1_consumed_snippet = _snippet_around(
            slice_text,
            "overview",
            ctx_chars=ctx_chars,
        ) or _snippet_around(slice_text, "/.overview.md", ctx_chars=ctx_chars)
    return res


def summarize(results: Iterable[QAResult]) -> Dict[str, Any]:
    by_dataset: Dict[str, Dict[str, int]] = {}
    for r in results:
        ds = r.dataset
        d = by_dataset.setdefault(
            ds,
            {
                "total": 0,
                # URI evidence (hit)
                "l0_hit": 0,
                "l1_hit": 0,
                "both_hit": 0,
                "neither_hit": 0,
                # Text evidence (consumed)
                "l0_consumed": 0,
                "l1_consumed": 0,
                "both_consumed": 0,
                "neither_consumed": 0,
                "l0_read_file": 0,
                "l1_read_file": 0,
                "missing_log": 0,
                "unmatched_session": 0,
            },
        )
        d["total"] += 1
        if r.missing_log:
            d["missing_log"] += 1
            continue
        if r.unmatched_session:
            d["unmatched_session"] += 1
            continue

        # Hit
        if r.hit_l0:
            d["l0_hit"] += 1
        if r.hit_l1:
            d["l1_hit"] += 1
        if r.hit_l0 and r.hit_l1:
            d["both_hit"] += 1
        if (not r.hit_l0) and (not r.hit_l1):
            d["neither_hit"] += 1

        # Consumed
        if r.consumed_l0:
            d["l0_consumed"] += 1
        if r.consumed_l1:
            d["l1_consumed"] += 1
        if r.consumed_l0 and r.consumed_l1:
            d["both_consumed"] += 1
        if (not r.consumed_l0) and (not r.consumed_l1):
            d["neither_consumed"] += 1
        if r.read_l0_file:
            d["l0_read_file"] += 1
        if r.read_l1_file:
            d["l1_read_file"] += 1

    overall = {
        "total": 0,
        "l0_hit": 0,
        "l1_hit": 0,
        "both_hit": 0,
        "neither_hit": 0,
        "l0_consumed": 0,
        "l1_consumed": 0,
        "both_consumed": 0,
        "neither_consumed": 0,
        "l0_read_file": 0,
        "l1_read_file": 0,
        "missing_log": 0,
        "unmatched_session": 0,
    }
    for ds in by_dataset.values():
        for k, v in ds.items():
            overall[k] += v

    # Keep the old shape but expand to both check types.
    checks = {}
    for ds, c in by_dataset.items():
        total = c["total"]
        missing = c["missing_log"]
        unmatched = c["unmatched_session"]
        checks[ds] = {
            "hit": {
                "ok": (c["l0_hit"] + c["l1_hit"] - c["both_hit"] + c["neither_hit"] + missing + unmatched)
                == total,
            },
            "consumed": {
                "ok": (
                    c["l0_consumed"]
                    + c["l1_consumed"]
                    - c["both_consumed"]
                    + c["neither_consumed"]
                    + missing
                    + unmatched
                )
                == total,
            },
        }
    checks["overall"] = {
        "hit": {
            "ok": (
                overall["l0_hit"]
                + overall["l1_hit"]
                - overall["both_hit"]
                + overall["neither_hit"]
                + overall["missing_log"]
                + overall["unmatched_session"]
            )
            == overall["total"],
        },
        "consumed": {
            "ok": (
                overall["l0_consumed"]
                + overall["l1_consumed"]
                - overall["both_consumed"]
                + overall["neither_consumed"]
                + overall["missing_log"]
                + overall["unmatched_session"]
            )
            == overall["total"],
        },
    }

    return {"by_dataset": by_dataset, "overall": overall, "checks": checks}


def pick_examples(results: List[QAResult]) -> Dict[str, Optional[QAResult]]:
    # Prefer examples with available snippet and not missing/unmatched.
    def _eligible(r: QAResult) -> bool:
        return (not r.missing_log) and (not r.unmatched_session)

    # Keep two sets: hit-based and consumed-based
    hit_l0_only = next((r for r in results if _eligible(r) and r.hit_l0 and not r.hit_l1), None)
    hit_l1_only = next((r for r in results if _eligible(r) and (not r.hit_l0) and r.hit_l1), None)
    hit_both = next((r for r in results if _eligible(r) and r.hit_l0 and r.hit_l1), None)
    hit_neither = next((r for r in results if _eligible(r) and (not r.hit_l0) and (not r.hit_l1)), None)

    consumed_l0_only = next(
        (r for r in results if _eligible(r) and r.consumed_l0 and not r.consumed_l1),
        None,
    )
    consumed_l1_only = next(
        (r for r in results if _eligible(r) and (not r.consumed_l0) and r.consumed_l1),
        None,
    )
    consumed_both = next(
        (r for r in results if _eligible(r) and r.consumed_l0 and r.consumed_l1),
        None,
    )
    consumed_neither = next(
        (r for r in results if _eligible(r) and (not r.consumed_l0) and (not r.consumed_l1)),
        None,
    )
    return {
        "hit_l0_only": hit_l0_only,
        "hit_l1_only": hit_l1_only,
        "hit_both": hit_both,
        "hit_neither": hit_neither,
        "consumed_l0_only": consumed_l0_only,
        "consumed_l1_only": consumed_l1_only,
        "consumed_both": consumed_both,
        "consumed_neither": consumed_neither,
    }


def _dataset_paths(repo_root: Path, dataset: str, experiment: str) -> Tuple[Path, Path]:
    out_dir = repo_root / "OpenViking-v0.3.9" / "benchmark" / "RAG" / "Output" / dataset / experiment
    answers = out_dir / "generated_answers.json"
    logs_dir = (
        repo_root
        / "OpenViking-v0.3.9"
        / "benchmark"
        / "RAG"
        / "ov_storage"
        / dataset
        / f"{dataset}_viking_store_index"
        / "bot"
        / "log"
    )
    return answers, logs_dir


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repo-root",
        default="/Users/bytedance/PR",
        help="Repo root containing OpenViking-v0.3.9/",
    )
    parser.add_argument(
        "--datasets",
        default=",".join(DATASETS),
        help="Comma-separated datasets to analyze (default: 5 datasets excluding SyllabusQA)",
    )
    parser.add_argument(
        "--ctx-chars",
        type=int,
        default=360,
        help="Context chars around first hit to capture as snippet",
    )
    parser.add_argument(
        "--out-json",
        default="/Users/bytedance/PR/.trae/documents/OpenViking_RAG_L0L1_usage_report.json",
        help="Output JSON path",
    )
    args = parser.parse_args()

    repo_root = Path(args.repo_root)
    datasets = [d.strip() for d in str(args.datasets).split(",") if d.strip()]

    all_results: List[QAResult] = []
    cache: Dict[Path, str] = {}

    for dataset in datasets:
        if dataset not in DEFAULT_EXPERIMENT_BY_DATASET:
            raise ValueError(f"Unknown dataset {dataset!r}. Known: {sorted(DEFAULT_EXPERIMENT_BY_DATASET)}")
        experiment = DEFAULT_EXPERIMENT_BY_DATASET[dataset]
        answers_path, logs_dir = _dataset_paths(repo_root, dataset, experiment)
        if not answers_path.exists():
            raise FileNotFoundError(f"Missing generated_answers.json: {answers_path}")

        records = load_generated_answers(answers_path, dataset)
        for rec in records:
            all_results.append(analyze_one(rec, logs_dir, cache, ctx_chars=int(args.ctx_chars)))

    summary_obj = summarize(all_results)
    examples_obj = pick_examples(all_results)

    out = {
        "rule": {
            "l0_used_if_contains": "/.abstract.md",
            "l1_used_if_contains": "/.overview.md",
            "scope": datasets,
        },
        "summary": summary_obj,
        "examples": {
            k: (None if v is None else v.__dict__)
            for k, v in examples_obj.items()
        },
        "results": [r.__dict__ for r in all_results],
    }

    out_path = Path(args.out_json)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
