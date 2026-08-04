"""Durable per-node journal for resumable BookRAG summary generation."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import threading
from typing import Any

from bookrag_core.checkpoint import canonical_sha256, tree_content_fingerprint


log = logging.getLogger(__name__)

SUMMARY_JOURNAL_FILENAME = "summary_results.jsonl"
SUMMARY_JOURNAL_FORMAT_VERSION = 1


def is_valid_summary(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text) and not text.lower().startswith("error:")


def summary_prompt_fingerprint(prompt: str) -> str:
    return canonical_sha256({"prompt": str(prompt)})


class SummaryJournal:
    """Append and replay summaries without rewriting the full tree per result."""

    def __init__(self, save_path: str | Path, tree) -> None:
        self.path = Path(save_path) / SUMMARY_JOURNAL_FILENAME
        self.tree_fingerprint = tree_content_fingerprint(tree)
        self._lock = threading.RLock()

    def append(self, *, node_id: int, prompt: str, summary: Any) -> str:
        text = str(summary or "").strip()
        if not is_valid_summary(text):
            raise ValueError(f"Refusing to persist an invalid summary for node {node_id}")

        record = {
            "format_version": SUMMARY_JOURNAL_FORMAT_VERSION,
            "tree_fingerprint": self.tree_fingerprint,
            "node_id": int(node_id),
            "prompt_fingerprint": summary_prompt_fingerprint(prompt),
            "summary": text,
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="") as file:
                file.write(line)
                file.flush()
                os.fsync(file.fileno())
        return text

    def records_by_node(self) -> dict[int, list[dict[str, Any]]]:
        records: dict[int, list[dict[str, Any]]] = {}
        if not self.path.is_file():
            return records

        with self._lock:
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()
            except OSError as error:
                log.warning("Cannot read summary journal %s: %s", self.path, error)
                return records

        malformed = 0
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if (
                    not isinstance(record, dict)
                    or record.get("format_version")
                    != SUMMARY_JOURNAL_FORMAT_VERSION
                    or record.get("tree_fingerprint") != self.tree_fingerprint
                    or not is_valid_summary(record.get("summary"))
                ):
                    continue
                node_id = int(record["node_id"])
                prompt_fingerprint = str(record["prompt_fingerprint"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                malformed += 1
                continue
            record["node_id"] = node_id
            record["prompt_fingerprint"] = prompt_fingerprint
            records.setdefault(node_id, []).append(record)

        if malformed:
            log.warning(
                "Ignored %d malformed lines in summary journal %s.",
                malformed,
                self.path,
            )
        return records
