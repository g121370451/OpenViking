"""VersionRAG adapter for question-scoped BookRAG document sets.

The regular :class:`VersionRAGAdapter` intentionally keeps its original
dataset-level semantics.  This adapter adds the strict ``qa_doc_mapping.json``
contract required by the per-query experiment without changing that baseline.
"""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any, Dict, List

from .base import StandardQA, StandardSample
from .versionrag_adapter import VersionRAGAdapter


class VersionRAGPerQueryAdapter(VersionRAGAdapter):
    """Attach the authoritative document set to every VersionRAG question."""

    def __init__(self, raw_file_path: str):
        super().__init__(raw_file_path)
        self.dataset_root = Path(self.raw_file_path).resolve().parents[2]
        self.qa_doc_mapping_path = (
            Path(self.raw_file_path).resolve().parent / "qa_doc_mapping.json"
        )

    def configure_qa_doc_mapping(self, mapping_path: str | os.PathLike[str]) -> None:
        path = Path(mapping_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(
                f"VersionRAG per-query QA document mapping not found: {path}"
            )
        self.qa_doc_mapping_path = path

    def _load_qa_doc_mapping(self, row_count: int) -> dict[int, dict[str, Any]]:
        path = self.qa_doc_mapping_path.resolve()
        if not path.is_file():
            raise FileNotFoundError(
                f"VersionRAG per-query QA document mapping not found: {path}"
            )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid VersionRAG QA document mapping JSON: {path}") from error
        if not isinstance(raw, list):
            raise ValueError("VersionRAG QA document mapping must be a JSON list")

        dataset_root = self.dataset_root.resolve()
        raw_doc_root = Path(self.raw_doc_dir).resolve()
        available_sources: dict[str, Path] = {}
        for candidate in raw_doc_root.iterdir():
            if not candidate.is_file() or candidate.suffix.lower() not in {
                ".md",
                ".markdown",
                ".pdf",
            }:
                continue
            existing = available_sources.get(candidate.stem)
            if existing is not None:
                raise ValueError(
                    "VersionRAG source files resolve to the same document ID: "
                    f"{existing}, {candidate}"
                )
            available_sources[candidate.stem] = candidate.resolve()
        by_index: dict[int, dict[str, Any]] = {}
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("Each VersionRAG QA mapping entry must be an object")
            row_index = item.get("row_index")
            if not isinstance(row_index, int) or isinstance(row_index, bool):
                raise ValueError(f"Invalid mapping row_index: {row_index!r}")
            if row_index in by_index:
                raise ValueError(f"Duplicate mapping row_index: {row_index}")

            source_values = item.get("source_files")
            if not isinstance(source_values, list) or not source_values:
                raise ValueError(
                    f"Mapping row {row_index} must contain non-empty source_files"
                )

            source_files: list[str] = []
            document_ids: list[str] = []
            seen_paths: set[Path] = set()
            for source_value in source_values:
                relative = Path(str(source_value))
                source_path = (
                    relative.resolve()
                    if relative.is_absolute()
                    else (dataset_root / relative).resolve()
                )
                try:
                    source_path.relative_to(raw_doc_root)
                except ValueError as error:
                    raise ValueError(
                        f"Mapping row {row_index} source escapes VersionRAG data/raw: "
                        f"{source_path}"
                    ) from error
                if source_path in seen_paths:
                    raise ValueError(
                        f"Mapping row {row_index} contains duplicate source: {source_path}"
                    )
                if not source_path.is_file():
                    raise FileNotFoundError(
                        f"Mapping row {row_index} source file not found: {source_path}"
                    )
                if available_sources.get(source_path.stem) != source_path:
                    raise ValueError(
                        f"Mapping row {row_index} source cannot resolve to a "
                        f"VersionRAG StandardDoc: {source_path}"
                    )
                seen_paths.add(source_path)
                source_files.append(str(source_path))
                document_ids.append(source_path.stem)

            if len(set(document_ids)) != len(document_ids):
                raise ValueError(
                    f"Mapping row {row_index} resolves multiple files to the same "
                    f"document ID: {document_ids}"
                )
            normalized = dict(item)
            normalized["source_files"] = source_files
            normalized["document_ids"] = sorted(document_ids)
            by_index[row_index] = normalized

        expected = set(range(row_count))
        actual = set(by_index)
        if actual != expected:
            missing = sorted(expected - actual)
            unexpected = sorted(actual - expected)
            raise ValueError(
                "VersionRAG QA mapping row_index values must continuously cover the "
                f"evaluation CSV: missing={missing[:10]}, unexpected={unexpected[:10]}"
            )
        return by_index

    def load_and_transform(self) -> List[StandardSample]:
        if not os.path.exists(self.raw_file_path):
            raise FileNotFoundError(f"Evaluation set not found: {self.raw_file_path}")

        with open(self.raw_file_path, "r", encoding="utf-8-sig") as file:
            csv_rows = list(csv.DictReader(file))
        mapping_by_index = self._load_qa_doc_mapping(len(csv_rows))

        groups: Dict[str, List[tuple[int, Dict[str, str]]]] = {}
        for row_index, row in enumerate(csv_rows):
            q_type = row.get("Type", "Unknown").strip()
            groups.setdefault(q_type, []).append((row_index, row))

        samples: List[StandardSample] = []
        for q_type, rows in groups.items():
            qa_pairs: list[StandardQA] = []
            for row_index, row in rows:
                question = row.get("Question", "").strip()
                answer = row.get("Answer", "").strip()
                mapping = mapping_by_index[row_index]
                mapped_question = str(mapping.get("question", "")).strip()
                if mapped_question != question:
                    raise ValueError(
                        "VersionRAG QA mapping question mismatch at row "
                        f"{row_index}: mapping={mapped_question!r}, csv={question!r}"
                    )
                mapped_type = str(mapping.get("type", q_type)).strip()
                if mapped_type != q_type:
                    raise ValueError(
                        "VersionRAG QA mapping type mismatch at row "
                        f"{row_index}: mapping={mapped_type!r}, csv={q_type!r}"
                    )
                qa_pairs.append(
                    StandardQA(
                        question=question,
                        gold_answers=[answer] if answer else [],
                        evidence=[],
                        category=q_type,
                        metadata={
                            "row_index": row_index,
                            "dataset_row_index": row_index,
                            "source_files": list(mapping["source_files"]),
                            "document_ids": list(mapping["document_ids"]),
                        },
                    )
                )

            samples.append(
                StandardSample(
                    sample_id=self._slugify(q_type),
                    qa_pairs=qa_pairs,
                    metadata={
                        "question_type": q_type,
                        "num_questions": len(qa_pairs),
                    },
                )
            )

        total_questions = sum(len(sample.qa_pairs) for sample in samples)
        self.logger.info(
            "[VersionRAG per-query] Loaded %d mapped questions across %d type groups",
            total_questions,
            len(samples),
        )
        return samples
