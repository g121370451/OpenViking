"""Adapter for canonical mixed questions used only by VikingBot build-link runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

from .base import BaseAdapter, StandardDoc, StandardQA, StandardSample


class BuildLinkQuestionsAdapter(BaseAdapter):
    """Load final mixed JSONL questions without enabling answer evaluation."""

    ORIGINS = {"source_generated", "rewrite"}

    def data_prepare(self, doc_dir: str) -> List[StandardDoc]:
        raise RuntimeError(
            "BuildLinkQuestionsAdapter is generation-only. Import the source dataset "
            "with its original adapter, then run this config with --step gen."
        )

    def load_and_transform(self) -> List[StandardSample]:
        path = Path(self.raw_file_path)
        if not path.exists():
            raise FileNotFoundError(f"Generated build-link questions not found: {path}")

        samples: list[StandardSample] = []
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc

                question = str(record.get("question", "") or "").strip()
                record_id = str(record.get("id", "") or "").strip()
                if not question or not record_id:
                    raise ValueError(
                        f"Each build-link record needs non-empty id and question: {path}:{line_number}"
                    )

                origin = str(record.get("question_origin", "") or "").strip()
                if origin not in self.ORIGINS:
                    raise ValueError(
                        f"Invalid or missing question_origin at {path}:{line_number}: {origin!r}"
                    )
                question_spec = record.get("question_spec")
                source_uris = record.get("source_uris")
                grounding_quotes = record.get("grounding_quotes")
                if not isinstance(question_spec, dict):
                    raise ValueError(f"question_spec must be an object: {path}:{line_number}")
                if not isinstance(source_uris, list) or not isinstance(grounding_quotes, list):
                    raise ValueError(
                        f"source_uris and grounding_quotes must be arrays: {path}:{line_number}"
                    )

                provenance = record.get("provenance")
                if origin == "rewrite":
                    if not isinstance(provenance, dict):
                        raise ValueError(
                            f"rewrite record needs provenance object: {path}:{line_number}"
                        )
                    required = ("original_question", "source_sample_id", "rewrite_index")
                    missing = [key for key in required if provenance.get(key) in (None, "")]
                    if missing:
                        raise ValueError(
                            f"rewrite provenance is missing {missing}: {path}:{line_number}"
                        )
                    try:
                        if int(provenance.get("rewrite_index", 0)) < 1:
                            raise ValueError
                    except (TypeError, ValueError) as exc:
                        raise ValueError(
                            f"rewrite_index must be a positive integer: {path}:{line_number}"
                        ) from exc
                elif provenance is not None and not isinstance(provenance, dict):
                    raise ValueError(
                        f"source_generated provenance must be null or an object: {path}:{line_number}"
                    )
                provenance = provenance or {}

                metadata = {
                    "record_id": record_id,
                    "question_origin": origin,
                    "question_provenance": provenance or None,
                    "question_spec": question_spec,
                    "source_uris": list(source_uris),
                    "original_question": str(provenance.get("original_question", "") or question),
                    "source_titles": list(provenance.get("source_titles", []) or []),
                    "source_ids": [str(provenance.get("source_sample_id", "") or "")]
                    if provenance.get("source_sample_id") else [],
                    "rewrite_index": int(provenance.get("rewrite_index", 0) or 0),
                    "rewrite_source_sample_id": str(
                        provenance.get("source_sample_id", "") or ""
                    ),
                    "rewrite_source_qa_index": int(provenance.get("rewrite_index", 0) or 0),
                    "similarity": record.get("similarity"),
                    "answer_evaluation_enabled": False,
                }
                qa = StandardQA(
                    question=question,
                    gold_answers=[],
                    evidence=list(grounding_quotes),
                    category="build_link_only",
                    metadata=metadata,
                )
                samples.append(
                    StandardSample(
                        sample_id=record_id,
                        qa_pairs=[qa],
                        metadata={
                            "answer_evaluation_enabled": False,
                            "question_origin": origin,
                            "question_provenance": provenance or None,
                        },
                    )
                )
        return samples

    def build_prompt(self, qa: StandardQA, context_blocks: List[str]):
        context = "\n\n".join(str(block) for block in context_blocks)
        return f"{context}\n\nQuestion: {qa.question}", {
            "answer_evaluation_enabled": False,
            "question_origin": (qa.metadata or {}).get("question_origin", ""),
            "question_provenance": (qa.metadata or {}).get("question_provenance"),
        }
