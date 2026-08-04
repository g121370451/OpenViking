"""Per-document-set BookRAG storage used by per-query experiments."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Generator, Sequence

from adapters.base import StandardDoc
from bookrag_core.checkpoint import atomic_write_json
from bookrag_core.utils.utils import num_tokens
from bookrag_runner import BookRAGStoreWrapper, _redact_secrets


_DOCUMENT_MANIFEST = "document_manifest.json"
_SET_MANIFEST = "document_set_manifest.json"
_DOCUMENT_MANIFEST_VERSION = 1
_SET_MANIFEST_VERSION = 1


class _BuildQueryGate:
    """Allow concurrent queries, but never overlap an index build with a query."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._active_queries = 0
        self._building = False

    @contextmanager
    def query(self) -> Generator[None, None, None]:
        with self._condition:
            while self._building:
                self._condition.wait()
            self._active_queries += 1
        try:
            yield
        finally:
            with self._condition:
                self._active_queries -= 1
                if self._active_queries == 0:
                    self._condition.notify_all()

    @contextmanager
    def build(self) -> Generator[None, None, None]:
        with self._condition:
            while self._building or self._active_queries:
                self._condition.wait()
            self._building = True
        try:
            yield
        finally:
            with self._condition:
                self._building = False
                self._condition.notify_all()


class BookRAGDocumentSetStoreManager:
    """Route each question to an isolated, reusable BookRAG document-set index."""

    def __init__(
        self,
        config: dict[str, Any],
        llm: Any | None = None,
        *,
        wrapper_factory: Callable[..., BookRAGStoreWrapper] = BookRAGStoreWrapper,
    ) -> None:
        self.config = config
        self.llm = llm
        self.bookrag_config = config.get("bookrag") or {}
        root_value = self.bookrag_config.get("document_store_root")
        if not root_value:
            raise ValueError(
                "bookrag.document_store_root is required for index_layout=per_query"
            )
        self.root = Path(str(root_value)).expanduser().resolve()
        BookRAGStoreWrapper._validate_index_dir(self.root)
        self.documents_root = self.root / "documents"
        self.index_sets_root = self.root / "index_sets"
        self.documents_root.mkdir(parents=True, exist_ok=True)
        self.index_sets_root.mkdir(parents=True, exist_ok=True)
        self.index_dir = self.root

        self.build_policy = str(
            self.bookrag_config.get("document_build_policy", "lazy")
        ).lower()
        if self.build_policy not in {"lazy", "eager"}:
            raise ValueError("bookrag.document_build_policy must be lazy or eager")
        self.cleanup_policy = str(
            self.bookrag_config.get("document_cleanup_policy", "keep")
        ).lower()
        if self.cleanup_policy not in {"keep", "after_document_set"}:
            raise ValueError(
                "bookrag.document_cleanup_policy must be keep or after_document_set"
            )
        self.max_open_runtimes = max(
            1,
            int(self.bookrag_config.get("max_open_document_set_runtimes", 4)),
        )

        self._wrapper_factory = wrapper_factory
        self._lock = threading.RLock()
        self._build_query_gate = _BuildQueryGate()
        self._build_locks: dict[str, threading.Lock] = {}
        self._documents: dict[str, StandardDoc] = {}
        self._document_records: dict[str, dict[str, Any]] = {}
        self._configured_sets: OrderedDict[str, tuple[str, ...]] = OrderedDict()
        self._remaining_questions: dict[str, int] = {}
        self._wrappers: OrderedDict[str, BookRAGStoreWrapper] = OrderedDict()
        self._active_answers: dict[str, int] = {}
        self._load_registered_documents()

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as file:
            for block in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _safe_document_key(document_id: str) -> str:
        safe = re.sub(r"[^\w.\-]+", "_", str(document_id), flags=re.UNICODE)
        safe = safe.strip("._-")[:80] or "document"
        suffix = hashlib.sha1(str(document_id).encode("utf-8")).hexdigest()[:10]
        return f"{safe}--{suffix}"

    def _index_config_hash(self) -> str:
        payload = {
            "dataset_name": self.config.get("dataset_name"),
            "llm": {
                key: (self.config.get("llm") or {}).get(key)
                for key in (
                    "model",
                    "temperature",
                    "max_tokens",
                    "base_url",
                    "frequency_penalty",
                    "presence_penalty",
                )
            },
            "embedding": {
                key: (self.config.get("embedding") or {}).get(key)
                for key in ("model", "backend", "base_url", "max_length")
            },
            "bookrag": {
                key: copy.deepcopy(self.bookrag_config.get(key) or {})
                for key in ("mineru", "tree", "graph", "gbc", "reranker")
            },
        }
        encoded = json.dumps(
            _redact_secrets(payload),
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _load_registered_documents(self) -> None:
        for manifest_path in self.documents_root.glob(f"*/{_DOCUMENT_MANIFEST}"):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (
                not isinstance(manifest, dict)
                or manifest.get("format_version") != _DOCUMENT_MANIFEST_VERSION
                or manifest.get("status") != "complete"
            ):
                continue
            document_id = str(manifest.get("document_id") or "")
            source_path = manifest_path.parent / "source" / "document.pdf"
            expected_hash = str(manifest.get("pdf_sha256") or "")
            if (
                not document_id
                or not source_path.is_file()
                or not expected_hash
                or self._file_sha256(source_path) != expected_hash
            ):
                continue
            self._documents[document_id] = StandardDoc(
                sample_id=document_id,
                doc_path=str(source_path.resolve()),
            )
            self._document_records[document_id] = manifest

    def _install_document_pdf(self, source_path: Path, target_path: Path) -> None:
        if source_path.resolve() == target_path.resolve():
            return
        temporary = target_path.with_name(f".{target_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            try:
                os.link(source_path, temporary)
            except OSError:
                shutil.copy2(source_path, temporary)
            os.replace(temporary, target_path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def register_documents(
        self,
        documents: Sequence[StandardDoc],
    ) -> list[StandardDoc]:
        validated = BookRAGStoreWrapper._validate_samples(documents)
        registered: list[StandardDoc] = []
        with self._lock:
            for document in validated:
                document_id = str(document.sample_id)
                source_path = Path(document.doc_path).resolve()
                pdf_hash = self._file_sha256(source_path)
                document_dir = self.documents_root / self._safe_document_key(document_id)
                source_dir = document_dir / "source"
                target_path = source_dir / "document.pdf"
                source_dir.mkdir(parents=True, exist_ok=True)
                unexpected = [path for path in source_dir.iterdir() if path != target_path]
                if unexpected:
                    raise RuntimeError(
                        f"Document source directory must contain only document.pdf: "
                        f"{source_dir}; unexpected={unexpected}"
                    )
                if not target_path.is_file() or self._file_sha256(target_path) != pdf_hash:
                    self._install_document_pdf(source_path, target_path)
                manifest = {
                    "format_version": _DOCUMENT_MANIFEST_VERSION,
                    "status": "complete",
                    "document_id": document_id,
                    "source_pdf_path": str(source_path),
                    "pdf_path": str(target_path.resolve()),
                    "pdf_sha256": pdf_hash,
                    "size_bytes": target_path.stat().st_size,
                }
                atomic_write_json(document_dir / _DOCUMENT_MANIFEST, manifest)
                isolated = StandardDoc(
                    sample_id=document_id,
                    doc_path=str(target_path.resolve()),
                )
                self._documents[document_id] = isolated
                self._document_records[document_id] = manifest
                registered.append(isolated)
        return registered

    def resolve_document_set(self, document_ids: Sequence[str]) -> dict[str, Any]:
        normalized = tuple(sorted(str(value) for value in document_ids))
        if not normalized:
            raise ValueError("A per-query BookRAG task must reference at least one document")
        if len(set(normalized)) != len(normalized):
            raise ValueError(f"Duplicate document IDs in document set: {normalized}")
        with self._lock:
            missing = [value for value in normalized if value not in self._documents]
            if missing:
                raise KeyError(
                    "Per-query BookRAG documents are not registered; run import or PDF "
                    f"preparation first: {missing}"
                )
            source_records = [
                {
                    "document_id": value,
                    "pdf_sha256": self._document_records[value]["pdf_sha256"],
                }
                for value in normalized
            ]
        encoded = json.dumps(
            source_records,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        document_set_id = hashlib.sha256(encoded).hexdigest()[:24]
        set_root = self.index_sets_root / document_set_id
        return {
            "document_ids": list(normalized),
            "document_set_id": document_set_id,
            "document_set_root": str(set_root),
            "index_dir": str(set_root / "index"),
            "sources": source_records,
        }

    def configure_document_sets(self, tasks: Sequence[dict[str, Any]]) -> None:
        configured: OrderedDict[str, tuple[str, ...]] = OrderedDict()
        counts: dict[str, int] = {}
        for task in tasks:
            document_ids = task.get("document_ids") or []
            resolved = self.resolve_document_set(document_ids)
            set_id = resolved["document_set_id"]
            task["document_set_id"] = set_id
            task["document_ids"] = resolved["document_ids"]
            task["document_set_index_dir"] = resolved["index_dir"]
            configured[set_id] = tuple(resolved["document_ids"])
            counts[set_id] = counts.get(set_id, 0) + 1
        with self._lock:
            self._configured_sets = configured
            self._remaining_questions = counts

    def _set_manifest_path(self, document_set_id: str) -> Path:
        return self.index_sets_root / document_set_id / _SET_MANIFEST

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _expected_manifest(
        self,
        resolved: dict[str, Any],
        *,
        status: str,
    ) -> dict[str, Any]:
        return {
            "format_version": _SET_MANIFEST_VERSION,
            "status": status,
            "document_set_id": resolved["document_set_id"],
            "document_ids": resolved["document_ids"],
            "sources": resolved["sources"],
            "index_config_sha256": self._index_config_hash(),
            "index_dir": resolved["index_dir"],
        }

    @staticmethod
    def _manifest_inputs_match(
        manifest: dict[str, Any] | None,
        expected: dict[str, Any],
    ) -> bool:
        return bool(
            manifest
            and manifest.get("format_version") == _SET_MANIFEST_VERSION
            and manifest.get("document_set_id") == expected["document_set_id"]
            and manifest.get("document_ids") == expected["document_ids"]
            and manifest.get("sources") == expected["sources"]
            and manifest.get("index_config_sha256")
            == expected["index_config_sha256"]
        )

    def _wrapper(self, document_set_id: str, index_dir: Path) -> BookRAGStoreWrapper:
        with self._lock:
            wrapper = self._wrappers.get(document_set_id)
            if wrapper is not None:
                self._wrappers.move_to_end(document_set_id)
                return wrapper
            set_config = copy.deepcopy(self.config)
            set_config.setdefault("bookrag", {})["index_dir"] = str(index_dir)
            set_config.setdefault("paths", {})["vector_store"] = str(index_dir)
            wrapper = self._wrapper_factory(config=set_config, llm=self.llm)
            self._wrappers[document_set_id] = wrapper
            self._active_answers.setdefault(document_set_id, 0)
            return wrapper

    def _evict_idle_runtimes(self) -> None:
        with self._lock:
            open_ids = [
                set_id
                for set_id, wrapper in self._wrappers.items()
                if getattr(wrapper, "_rag_agent", None) is not None
            ]
            for set_id in open_ids:
                if len(open_ids) <= self.max_open_runtimes:
                    break
                if self._active_answers.get(set_id, 0):
                    continue
                wrapper = self._wrappers[set_id]
                wrapper.close()
                open_ids.remove(set_id)

    def _discard_invalid_index(
        self,
        wrapper: BookRAGStoreWrapper,
        index_dir: Path,
    ) -> None:
        if not index_dir.exists():
            return
        if wrapper._is_bookrag_index(index_dir):
            wrapper.clear()
            return
        if wrapper._is_resumable_build_dir(index_dir):
            wrapper.close()
            shutil.rmtree(index_dir)
            return
        if any(index_dir.iterdir()):
            raise RuntimeError(
                f"Refusing to replace unrecognized document-set index: {index_dir}"
            )
        index_dir.rmdir()

    def ensure_index(
        self,
        document_ids: Sequence[str],
        *,
        max_workers: int | None = None,
    ) -> dict[str, Any]:
        wait_started = time.monotonic()
        resolved = self.resolve_document_set(document_ids)
        set_id = resolved["document_set_id"]
        index_dir = Path(resolved["index_dir"])
        set_manifest_path = self._set_manifest_path(set_id)
        with self._lock:
            build_lock = self._build_locks.setdefault(set_id, threading.Lock())

        with build_lock:
            wait_seconds = time.monotonic() - wait_started
            expected = self._expected_manifest(resolved, status="building")
            manifest = self._read_json(set_manifest_path)
            wrapper = self._wrapper(set_id, index_dir)
            if (
                self._manifest_inputs_match(manifest, expected)
                and manifest.get("status") == "complete"
                and wrapper._is_bookrag_index(index_dir)
            ):
                historical = manifest.get("ingest_stats") or {}
                return {
                    "document_set_id": set_id,
                    "document_ids": resolved["document_ids"],
                    "index_dir": str(index_dir),
                    "cache_hit": True,
                    "wait_seconds": wait_seconds,
                    "time": 0.0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "embedding_tokens": 0,
                    "historical_ingest_stats": historical,
                }

            can_resume = (
                self._manifest_inputs_match(manifest, expected)
                and manifest.get("status") == "building"
                and wrapper._is_resumable_build_dir(index_dir)
            )
            if not can_resume:
                self._discard_invalid_index(wrapper, index_dir)

            set_manifest_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(set_manifest_path, expected)
            documents = [self._documents[value] for value in resolved["document_ids"]]
            with self._build_query_gate.build():
                stats = wrapper.ingest(
                    documents,
                    max_workers=max_workers,
                    ingest_mode="dataset",
                )
            complete = dict(expected)
            complete.update(
                {
                    "status": "complete",
                    "completed_at": time.time(),
                    "ingest_stats": stats,
                }
            )
            atomic_write_json(set_manifest_path, complete)
            return {
                "document_set_id": set_id,
                "document_ids": resolved["document_ids"],
                "index_dir": str(index_dir),
                "cache_hit": False,
                "wait_seconds": wait_seconds,
                **stats,
                "historical_ingest_stats": stats,
            }

    def ingest(
        self,
        samples: Sequence[StandardDoc],
        max_workers: int | None = None,
        monitor: Any | None = None,
        ingest_mode: str = "dataset",
    ) -> dict[str, Any]:
        del monitor, ingest_mode
        self.register_documents(samples)
        total = {
            "time": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "embedding_tokens": 0,
            "document_sets": 0,
            "cache_hits": 0,
        }
        if self.build_policy == "lazy":
            return total
        with self._lock:
            configured = list(self._configured_sets.values())
        for document_ids in configured:
            stats = self.ensure_index(document_ids, max_workers=max_workers)
            total["time"] += float(stats.get("time", 0.0) or 0.0)
            total["input_tokens"] += int(stats.get("input_tokens", 0) or 0)
            total["output_tokens"] += int(stats.get("output_tokens", 0) or 0)
            total["embedding_tokens"] += int(stats.get("embedding_tokens", 0) or 0)
            total["document_sets"] += 1
            total["cache_hits"] += int(bool(stats.get("cache_hit")))
        return total

    def answer(
        self,
        query: str,
        topk: int,
        *,
        query_output_dir: str | Path,
        document_ids: Sequence[str],
        document_set_id: str | None = None,
        target_uri: str | None = None,
    ) -> dict[str, Any]:
        resolved = self.resolve_document_set(document_ids)
        if document_set_id and document_set_id != resolved["document_set_id"]:
            raise ValueError(
                f"Document set ID mismatch: task={document_set_id}, "
                f"resolved={resolved['document_set_id']}"
            )
        set_id = resolved["document_set_id"]
        index_stats = self.ensure_index(
            resolved["document_ids"],
            max_workers=(self.config.get("execution") or {}).get("ingest_workers"),
        )
        wrapper = self._wrapper(set_id, Path(resolved["index_dir"]))
        with self._lock:
            self._active_answers[set_id] = self._active_answers.get(set_id, 0) + 1
            self._wrappers.move_to_end(set_id)
        answer_started = time.monotonic()
        try:
            with self._build_query_gate.query():
                result = wrapper.answer(
                    query=query,
                    topk=topk,
                    query_output_dir=query_output_dir,
                    target_uri=target_uri,
                )
        finally:
            with self._lock:
                self._active_answers[set_id] -= 1
        result["document_set"] = {
            **index_stats,
            "answer_time_seconds": time.monotonic() - answer_started,
        }
        self._question_finished(set_id)
        self._evict_idle_runtimes()
        return result

    def _question_finished(self, document_set_id: str) -> None:
        if self.cleanup_policy != "after_document_set":
            return
        should_delete = False
        with self._lock:
            remaining = self._remaining_questions.get(document_set_id)
            if remaining is None:
                return
            remaining -= 1
            self._remaining_questions[document_set_id] = remaining
            should_delete = remaining == 0
        if should_delete:
            self.delete_index_set(document_set_id)

    def delete_index_set(self, document_set_id: str) -> None:
        set_root = (self.index_sets_root / str(document_set_id)).resolve()
        try:
            set_root.relative_to(self.index_sets_root.resolve())
        except ValueError as error:
            raise ValueError(f"Invalid document_set_id: {document_set_id}") from error
        index_dir = set_root / "index"
        with self._lock:
            wrapper = self._wrappers.pop(document_set_id, None)
            self._active_answers.pop(document_set_id, None)
        if wrapper is not None:
            wrapper.close()
        if index_dir.exists():
            if not (
                BookRAGStoreWrapper._is_bookrag_index(index_dir)
                or BookRAGStoreWrapper._is_resumable_build_dir(index_dir)
            ):
                raise RuntimeError(
                    f"Refusing to delete unrecognized document-set index: {index_dir}"
                )
            shutil.rmtree(index_dir)
        manifest_path = set_root / _SET_MANIFEST
        manifest = self._read_json(manifest_path)
        if manifest is not None:
            manifest["status"] = "deleted"
            manifest["deleted_at"] = time.time()
            atomic_write_json(manifest_path, manifest)

    def clear(self) -> None:
        set_ids = sorted(path.name for path in self.index_sets_root.iterdir() if path.is_dir())
        for set_id in set_ids:
            self.delete_index_set(set_id)

    def close(self) -> None:
        with self._lock:
            wrappers = list(self._wrappers.values())
            self._wrappers.clear()
            self._active_answers.clear()
        for wrapper in wrappers:
            wrapper.close()

    @staticmethod
    def count_tokens(text: str) -> int:
        return num_tokens(str(text or ""))
