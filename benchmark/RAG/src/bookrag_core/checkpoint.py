"""Persistent integrity checks and resume state for BookRAG indexing."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import pickle
import tempfile
import threading
from typing import Any, Iterable, Sequence


STATE_FILENAME = "bookrag_build_state.json"
STATE_FORMAT_VERSION = 1
REFINEMENT_CHECKPOINT_FILENAME = "kg_refinement_checkpoint.pkl"
TOKEN_USAGE_FILENAME = "bookrag_token_usage.json"


def _json_default(value: Any):
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, Path):
        return str(value)
    value_attr = getattr(value, "value", None)
    if value_attr is not None:
        return value_attr
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump()
    return str(value)


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write_json(path: str | Path, value: Any) -> None:
    """Write JSON beside its destination and atomically replace it."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(
                value,
                file,
                ensure_ascii=False,
                indent=2,
                default=_json_default,
            )
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_name, target)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def atomic_write_pickle(path: str | Path, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(fd, "wb") as file:
            pickle.dump(value, file, protocol=pickle.HIGHEST_PROTOCOL)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_name, target)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def source_records(
    documents: Sequence[tuple[str, str | Path]],
) -> list[dict[str, str]]:
    records = []
    for sample_id, source_path in sorted(
        ((str(sample_id), Path(path).resolve()) for sample_id, path in documents),
        key=lambda item: (item[0], str(item[1])),
    ):
        records.append(
            {
                "sample_id": sample_id,
                "source_path": str(source_path),
                "content_sha256": file_sha256(source_path),
            }
        )
    return records


def tree_config_fingerprint(cfg) -> str:
    if getattr(cfg, "source_format", None) != "pdf":
        # The legacy normalized-Markdown tree has no model/parser-dependent
        # structure. Keep its existing checkpoint semantics for focused tests
        # and for explicitly constructed legacy configs.
        return canonical_sha256({})

    mineru = cfg.mineru
    llm = cfg.llm
    tree = cfg.tree
    return canonical_sha256(
        {
            "source_format": "pdf",
            "mineru": {
                "backend": mineru.backend,
                "method": mineru.method,
                "lang": mineru.lang,
                "server_url": mineru.server_url,
            },
            # pdf_info_refiner and outline extraction use the LLM and therefore
            # affect tree structure before the summary stage begins.
            "tree_llm": {
                "model": llm.model_name,
                "api_base": llm.api_base,
                "max_tokens": llm.max_tokens,
                "temperature": llm.temperature,
            },
            "tree": {
                "node_keywords": tree.node_keywords,
            },
        }
    )


def summary_config_fingerprint(cfg) -> str:
    from bookrag_core.prompts.summary_prompt import (
        NODE_SUMMARY_PROMPT,
        SEC_SUMMARY_PROMPT,
    )

    return canonical_sha256(
        {
            "enabled": bool(cfg.tree.node_summary),
            "use_vlm": bool(cfg.tree.use_vlm),
            "model": cfg.llm.model_name,
            "max_tokens": cfg.llm.max_tokens,
            "temperature": cfg.llm.temperature,
            "node_prompt": NODE_SUMMARY_PROMPT,
            "section_prompt": SEC_SUMMARY_PROMPT,
        }
    )


def refinement_config_fingerprint(cfg) -> str:
    graph = cfg.graph
    embedding = getattr(graph, "embedding_config", None)
    reranker = getattr(graph, "reranker_config", None)
    llm = getattr(cfg, "llm", None)
    return canonical_sha256(
        {
            "refine_type": getattr(graph, "refine_type", None),
            "g": getattr(graph, "g", None),
            "embedding": {
                "backend": getattr(embedding, "backend", None),
                "model": getattr(embedding, "model_name", None),
                "max_length": getattr(embedding, "max_length", None),
            },
            "reranker": {
                "backend": getattr(reranker, "backend", None),
                "model": getattr(reranker, "model_name", None),
                "model_version": getattr(reranker, "model_version", None),
                "threshold": getattr(reranker, "threshold", None),
            },
            "llm_model": getattr(llm, "model_name", None),
        }
    )


def tree_content_fingerprint(tree) -> str:
    nodes = []
    for node in tree.nodes:
        nodes.append(
            {
                "index_id": node.index_id,
                "parent_id": node.parent.index_id if node.parent else None,
                "type": getattr(node.type, "value", str(node.type)),
                "meta_info": node.meta_info.model_dump() if node.meta_info else None,
            }
        )
    return canonical_sha256(
        {
            "nodes": nodes,
            "meta_info": tree.meta_info.model_dump() if tree.meta_info else None,
        }
    )


@dataclass
class TreeValidation:
    valid: bool
    tree: Any | None = None
    reason: str = ""
    json_repaired: bool = False


@dataclass
class SummaryValidation:
    complete: bool
    required_node_ids: set[int]
    target_node_ids: set[int]
    invalid_node_ids: set[int]
    reason: str = ""


class BuildCheckpoint:
    """Own the resumable state stored inside one stable build directory."""

    def __init__(self, save_path: str | Path):
        self.save_path = Path(save_path)
        self.state_path = self.save_path / STATE_FILENAME
        self._lock = threading.RLock()
        self.state = self._load_state()

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {
                "format_version": STATE_FORMAT_VERSION,
                "sources": [],
                "source_fingerprint": "",
                "stages": {},
                "token_usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            }
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {
                "format_version": STATE_FORMAT_VERSION,
                "sources": [],
                "source_fingerprint": "",
                "stages": {},
                "token_usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            }
        if not isinstance(value, dict) or value.get("format_version") != STATE_FORMAT_VERSION:
            raise ValueError("Unsupported BookRAG build checkpoint format")
        value.setdefault("stages", {})
        value.setdefault("token_usage", {})
        return value

    def save(self) -> None:
        with self._lock:
            atomic_write_json(self.state_path, self.state)

    def expected_context(self, documents, cfg) -> dict[str, Any]:
        records = source_records(list(documents))
        return {
            "sources": records,
            "source_fingerprint": canonical_sha256(records),
            "tree_config_fingerprint": tree_config_fingerprint(cfg),
        }

    def reset_for_context(self, documents, cfg) -> None:
        """Start a new accounting/checkpoint lineage for changed inputs."""
        with self._lock:
            self.state = {
                "format_version": STATE_FORMAT_VERSION,
                **self.expected_context(documents, cfg),
                "stages": {},
                "token_usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            }
            self.save()
        try:
            self.refinement_checkpoint_path.unlink()
        except FileNotFoundError:
            pass
        try:
            self.token_usage_path.unlink()
        except FileNotFoundError:
            pass

    def context_matches(self, documents, cfg) -> bool:
        expected = self.expected_context(documents, cfg)
        saved_source = self.state.get("source_fingerprint")
        saved_tree_config = self.state.get("tree_config_fingerprint")
        if not saved_source and not saved_tree_config:
            return True  # Legacy artifacts are adopted after structural validation.
        return (
            saved_source == expected["source_fingerprint"]
            and saved_tree_config == expected["tree_config_fingerprint"]
        )

    def adopt_context(self, documents, cfg) -> None:
        with self._lock:
            self.state.update(self.expected_context(documents, cfg))
            self.save()

    def stage(self, name: str) -> dict[str, Any]:
        value = self.state.setdefault("stages", {}).get(name, {})
        return value if isinstance(value, dict) else {}

    def update_stage(self, name: str, **values: Any) -> None:
        with self._lock:
            stage = dict(self.stage(name))
            stage.update(values)
            self.state.setdefault("stages", {})[name] = stage
            self.capture_token_usage(save=False)
            self.save()

    def capture_token_usage(self, *, save: bool = True) -> dict[str, int]:
        from bookrag_core.provider.TokenTracker import TokenTracker

        usage = TokenTracker.get_instance().get_usage()
        normalized = {
            "prompt_tokens": int(usage.get("prompt_tokens", 0)),
            "completion_tokens": int(usage.get("completion_tokens", 0)),
            "total_tokens": int(usage.get("total_tokens", 0)),
        }
        self.state["token_usage"] = normalized
        if save:
            self.save()
        return normalized

    def restore_token_usage(self, *, configure_persistence: bool = True) -> None:
        from bookrag_core.provider.TokenTracker import TokenTracker

        tracker = TokenTracker.get_instance()
        if configure_persistence:
            tracker.set_persistence_path(self.token_usage_path)
        usage = self.state.get("token_usage") or {}
        persisted_usage = tracker.read_persisted_usage()
        if persisted_usage is not None:
            usage = persisted_usage
        tracker.restore_usage(
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
        )

    @property
    def token_usage_path(self) -> Path:
        return self.save_path / TOKEN_USAGE_FILENAME

    @staticmethod
    def _normalized_source_key(sample_id: Any, path: Any) -> tuple[str, str]:
        return str(sample_id), os.path.normcase(str(Path(path).resolve()))

    def validate_tree(self, documents, cfg) -> TreeValidation:
        from bookrag_core.Index.Tree import DocumentTree, NodeType

        tree_path = Path(DocumentTree.get_save_path(str(self.save_path)))
        if not tree_path.is_file():
            return TreeValidation(False, reason="tree.pkl is missing")
        if not self.context_matches(documents, cfg):
            return TreeValidation(False, reason="source or tree configuration changed")
        try:
            tree = DocumentTree.load_from_file(str(tree_path))
        except Exception as error:
            return TreeValidation(False, reason=f"tree.pkl cannot be loaded: {error}")
        if not isinstance(tree, DocumentTree) or tree.root_node is None:
            return TreeValidation(False, reason="tree.pkl has no valid root")

        nodes = list(tree.nodes)
        node_ids = [node.index_id for node in nodes]
        id_set = set(node_ids)
        if len(node_ids) != len(id_set):
            return TreeValidation(False, reason="tree contains duplicate node IDs")
        if tree.root_node.index_id not in id_set:
            return TreeValidation(False, reason="root node is not in tree.nodes")

        reachable: set[int] = set()
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if node.index_id in reachable:
                continue
            reachable.add(node.index_id)
            for child in node.children:
                if child.parent is not node:
                    return TreeValidation(
                        False,
                        reason=f"parent/child mismatch at node {child.index_id}",
                    )
                stack.append(child)
        if reachable != id_set:
            return TreeValidation(False, reason="tree contains unreachable nodes")

        expected_sources = {
            self._normalized_source_key(sample_id, path)
            for sample_id, path in documents
        }
        actual_sources = set()
        for document_root in tree.root_node.children:
            if document_root.type != NodeType.ROOT:
                return TreeValidation(False, reason="invalid document root type")
            sample_id = document_root.meta_info.sample_id
            source_path = document_root.meta_info.file_path
            if not sample_id or not source_path:
                return TreeValidation(False, reason="document root provenance is missing")
            actual_sources.add(self._normalized_source_key(sample_id, source_path))
        if actual_sources != expected_sources:
            return TreeValidation(False, reason="tree sources do not match requested documents")

        tree.save_dir = str(self.save_path)
        json_repaired = self._repair_tree_json_if_needed(tree)
        content_fingerprint = tree_content_fingerprint(tree)
        saved_fingerprint = self.stage("tree").get("content_fingerprint")
        if saved_fingerprint and saved_fingerprint != content_fingerprint:
            return TreeValidation(False, reason="tree content fingerprint changed")

        self.adopt_context(documents, cfg)
        self.update_stage(
            "tree",
            status="complete",
            node_count=len(nodes),
            content_fingerprint=content_fingerprint,
        )
        return TreeValidation(True, tree=tree, json_repaired=json_repaired)

    def _repair_tree_json_if_needed(self, tree) -> bool:
        json_path = self.save_path / "tree.json"
        expected = tree.to_json_summary()
        try:
            current = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = None
        if current == expected:
            return False
        atomic_write_json(json_path, expected)
        return True

    def mark_tree_complete(self, tree, documents, cfg) -> None:
        content_fingerprint = tree_content_fingerprint(tree)
        previous_fingerprint = self.stage("tree").get("content_fingerprint")
        if previous_fingerprint and previous_fingerprint != content_fingerprint:
            for stage_name in (
                "summary",
                "kg_extraction",
                "kg_refinement",
                "entity_vdb",
            ):
                self.state.setdefault("stages", {}).pop(stage_name, None)
        self.adopt_context(documents, cfg)
        self.update_stage(
            "tree",
            status="complete",
            node_count=len(tree.nodes),
            content_fingerprint=content_fingerprint,
        )

    def validate_summaries(self, tree, cfg) -> SummaryValidation:
        from bookrag_core.Index.Tree import NodeType

        required_nodes = [node for node in tree.nodes if node.type != NodeType.ROOT]
        required_ids = {node.index_id for node in required_nodes}
        invalid_ids = {
            node.index_id
            for node in required_nodes
            if not str(node.summary or "").strip()
            or str(node.summary or "").lstrip().lower().startswith("error:")
        }
        expected_fingerprint = summary_config_fingerprint(cfg)

        targets = set(invalid_ids)
        for node_id in list(invalid_ids):
            node = tree.get_node_by_index_id(node_id)
            parent = node.parent if node else None
            while parent is not None:
                if parent.type != NodeType.ROOT:
                    targets.add(parent.index_id)
                parent = parent.parent

        complete = not targets
        reason = "all summaries are complete" if complete else "missing or stale summaries"
        if complete:
            # Legacy complete summaries are adopted once under the current config.
            self.update_stage(
                "summary",
                status="complete",
                completed_nodes=len(required_ids),
                total_nodes=len(required_ids),
                config_fingerprint=expected_fingerprint,
            )
        return SummaryValidation(
            complete=complete,
            required_node_ids=required_ids,
            target_node_ids=targets,
            invalid_node_ids=invalid_ids,
            reason=reason,
        )

    def mark_summary_progress(self, tree, cfg) -> SummaryValidation:
        tree.save_to_file()
        validation = self.validate_summaries(tree, cfg)
        completed = len(validation.required_node_ids - validation.invalid_node_ids)
        self.update_stage(
            "summary",
            status="complete" if validation.complete else "in_progress",
            completed_nodes=completed,
            total_nodes=len(validation.required_node_ids),
            config_fingerprint=summary_config_fingerprint(cfg),
        )
        return validation

    def refinement_fingerprint(self, cfg) -> str:
        return canonical_sha256(
            {
                "tree": self.stage("tree").get("content_fingerprint"),
                "config": refinement_config_fingerprint(cfg),
            }
        )

    @property
    def refinement_checkpoint_path(self) -> Path:
        return self.save_path / REFINEMENT_CHECKPOINT_FILENAME

    def load_refinement(
        self,
        cfg,
        result_node_ids: Iterable[int],
        extraction_fingerprint: str,
    ) -> dict | None:
        path = self.refinement_checkpoint_path
        if not path.is_file():
            return None
        expected_order = [int(node_id) for node_id in result_node_ids]
        try:
            with path.open("rb") as file:
                snapshot = pickle.load(file)
        except Exception:
            return None
        if not isinstance(snapshot, dict) or snapshot.get("format_version") != 1:
            return None
        if snapshot.get("refinement_fingerprint") != self.refinement_fingerprint(cfg):
            return None
        if snapshot.get("result_order_fingerprint") != canonical_sha256(expected_order):
            return None
        if snapshot.get("extraction_fingerprint") != extraction_fingerprint:
            return None
        next_position = snapshot.get("next_position")
        if not isinstance(next_position, int) or not 0 <= next_position <= len(expected_order):
            return None
        if snapshot.get("graph_index") is None:
            return None
        return snapshot

    def save_refinement(
        self,
        cfg,
        result_node_ids: Iterable[int],
        *,
        graph_index,
        entity_alias_map: dict,
        entity_to_vdb_id: dict,
        next_position: int,
        phase: str,
        extraction_fingerprint: str,
    ) -> None:
        order = [int(node_id) for node_id in result_node_ids]
        usage = self.capture_token_usage(save=False)
        snapshot = {
            "format_version": 1,
            "refinement_fingerprint": self.refinement_fingerprint(cfg),
            "result_order_fingerprint": canonical_sha256(order),
            "extraction_fingerprint": extraction_fingerprint,
            "next_position": int(next_position),
            "phase": phase,
            "graph_index": graph_index,
            "entity_alias_map": dict(entity_alias_map),
            "entity_to_vdb_id": dict(entity_to_vdb_id),
            "token_usage": usage,
        }
        atomic_write_pickle(self.refinement_checkpoint_path, snapshot)
        self.update_stage(
            "kg_refinement",
            status="complete" if phase == "complete" else "in_progress",
            phase=phase,
            completed_nodes=int(next_position),
            total_nodes=len(order),
            config_fingerprint=self.refinement_fingerprint(cfg),
            extraction_fingerprint=extraction_fingerprint,
        )
