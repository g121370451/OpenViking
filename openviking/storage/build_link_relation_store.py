# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Independent, lossless storage for build-link relation data.

This store deliberately does not use OpenViking's vector database. Relation
edges, their historical questions, and question embeddings live in one SQLite
database under ``viking/resources/.relations_store``.
"""

from __future__ import annotations

import array
import hashlib
import json
import math
import os
import sqlite3
import sys
import threading
import time
import zlib
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Optional


RELATION_STORE_DIR = ".relations_store"
RELATION_DB_FILENAME = "relations.sqlite3"
RELATION_MANIFEST_FILENAME = "manifest.json"
RELATION_SCHEMA_VERSION = 1

_KNOWN_EDGE_FIELDS = {
    "uri1",
    "uri2",
    "question_id",
    "query_question",
    "query_embedding",
    "reason",
    "weight",
}


def compute_question_id(question: str) -> str:
    """Return the legacy-compatible deterministic question ID."""
    return hashlib.sha256(question.lower().strip().encode()).hexdigest()[:16]


def relation_store_path(vikingfs_path: str | os.PathLike[str]) -> Path:
    return Path(vikingfs_path) / "resources" / RELATION_STORE_DIR


def _encode_text(value: str) -> bytes:
    return value.encode("utf-8", errors="surrogatepass")


def _decode_text(value: bytes) -> str:
    return value.decode("utf-8", errors="surrogatepass")


def _pack_embedding(embedding: Optional[Iterable[float]]) -> tuple[Optional[bytes], int, float]:
    if embedding is None:
        return None, 0, 0.0
    values = array.array("d", (float(value) for value in embedding))
    dimension = len(values)
    norm = math.sqrt(sum(value * value for value in values))
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes(), dimension, norm


def _unpack_embedding(payload: Optional[bytes], dimension: int) -> Optional[list[float]]:
    if payload is None:
        return None
    values = array.array("d")
    values.frombytes(payload)
    if sys.byteorder != "little":
        values.byteswap()
    if len(values) != dimension:
        raise ValueError(
            f"Invalid relation embedding payload: expected {dimension} values, got {len(values)}"
        )
    return values.tolist()


def _compress_bytes(payload: bytes) -> tuple[str, bytes]:
    if not payload:
        return "raw", payload
    compressed = zlib.compress(payload, level=3)
    if len(compressed) >= len(payload):
        return "raw", payload
    return "zlib", compressed


def _decompress_bytes(codec: str, payload: bytes, raw_size: int) -> bytes:
    if codec == "raw":
        result = payload
    elif codec == "zlib":
        result = zlib.decompress(payload)
    else:
        raise ValueError(f"Unsupported relation payload codec: {codec}")
    if len(result) != raw_size:
        raise ValueError(
            f"Corrupt relation payload: expected {raw_size} bytes, got {len(result)}"
        )
    return result


class SQLiteRelationStore:
    """Central relation store backed by a standalone SQLite database."""

    def __init__(self, vikingfs_path: str | os.PathLike[str], *, create: bool = False):
        self.vikingfs_path = Path(vikingfs_path)
        self.root = relation_store_path(self.vikingfs_path)
        self.db_path = self.root / RELATION_DB_FILENAME
        self._local = threading.local()
        self._schema_lock = threading.RLock()
        if create:
            self._initialize()

    @property
    def exists(self) -> bool:
        return self.db_path.is_file()

    def _connect(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            return connection
        if not self.exists:
            raise FileNotFoundError(self.db_path)
        connection = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            isolation_level="DEFERRED",
            check_same_thread=True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA temp_store = MEMORY")
        connection.execute("PRAGMA mmap_size = 268435456")
        self._local.connection = connection
        return connection

    def _initialize(self) -> None:
        with self._schema_lock:
            self.root.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = NORMAL")
                connection.execute("PRAGMA foreign_keys = ON")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS resources (
                        id INTEGER PRIMARY KEY,
                        uri TEXT NOT NULL UNIQUE
                    );

                    CREATE TABLE IF NOT EXISTS questions (
                        id INTEGER PRIMARY KEY,
                        external_id TEXT NOT NULL,
                        question_utf8 BLOB NOT NULL,
                        model_key TEXT NOT NULL,
                        dimension INTEGER NOT NULL,
                        embedding_f64 BLOB,
                        norm REAL NOT NULL,
                        content_hash BLOB NOT NULL UNIQUE
                    );

                    CREATE INDEX IF NOT EXISTS idx_questions_external_model
                    ON questions(external_id, model_key);

                    CREATE TABLE IF NOT EXISTS reasons (
                        id INTEGER PRIMARY KEY,
                        content_hash BLOB NOT NULL UNIQUE,
                        value_type TEXT NOT NULL,
                        codec TEXT NOT NULL,
                        raw_size INTEGER NOT NULL,
                        payload BLOB NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS edges (
                        id INTEGER PRIMARY KEY,
                        source_id INTEGER NOT NULL REFERENCES resources(id),
                        target_id INTEGER NOT NULL REFERENCES resources(id),
                        question_id INTEGER REFERENCES questions(id),
                        question_key TEXT NOT NULL,
                        reason_id INTEGER NOT NULL REFERENCES reasons(id),
                        strategy TEXT NOT NULL,
                        weight REAL NOT NULL,
                        extra_codec TEXT,
                        extra_raw_size INTEGER,
                        extra_payload BLOB,
                        created_at INTEGER NOT NULL,
                        UNIQUE(source_id, target_id, question_key, strategy)
                    );

                    CREATE INDEX IF NOT EXISTS idx_edges_source_strategy
                    ON edges(source_id, strategy);

                    CREATE INDEX IF NOT EXISTS idx_edges_question
                    ON edges(question_id);
                    """
                )
                connection.execute(
                    "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
                    (str(RELATION_SCHEMA_VERSION),),
                )
            finally:
                connection.close()
            self._write_manifest()

    def _write_manifest(self) -> None:
        manifest_path = self.root / RELATION_MANIFEST_FILENAME
        if manifest_path.exists():
            return
        manifest = {
            "format": "openviking-build-link-relations",
            "schema_version": RELATION_SCHEMA_VERSION,
            "database": RELATION_DB_FILENAME,
            "embedding_encoding": "ieee754-float64-little-endian",
            "reason_encoding": "lossless-per-content",
        }
        temp_path = manifest_path.with_name(
            f".{manifest_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        temp_path.write_text(
            json.dumps(manifest, ensure_ascii=True, indent=2) + "\n",
            encoding="ascii",
        )
        os.replace(temp_path, manifest_path)

    def close(self) -> None:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
            self._local.connection = None
        self._load_embedding.cache_clear()

    def get_metadata(self, key: str, default: Optional[str] = None) -> Optional[str]:
        if not self.exists:
            return default
        row = self._connect().execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_metadata(self, key: str, value: str) -> None:
        if not self.exists:
            self._initialize()
        with self._connect():
            self._connect().execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES(?, ?)",
                (key, value),
            )

    @property
    def migration_complete(self) -> bool:
        return self.get_metadata("legacy_migration_complete", "0") == "1"

    def mark_migration_complete(self) -> None:
        self.set_metadata("legacy_migration_complete", "1")

    @staticmethod
    def _get_resource_id(connection: sqlite3.Connection, uri: str) -> int:
        connection.execute("INSERT OR IGNORE INTO resources(uri) VALUES(?)", (uri,))
        row = connection.execute("SELECT id FROM resources WHERE uri = ?", (uri,)).fetchone()
        if row is None:
            raise RuntimeError(f"Failed to resolve relation resource URI: {uri}")
        return int(row["id"])

    @staticmethod
    def _get_reason_id(connection: sqlite3.Connection, reason: Any) -> int:
        if isinstance(reason, str):
            value_type = "text"
            raw = _encode_text(reason)
        else:
            value_type = "json"
            raw = json.dumps(reason, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8", errors="surrogatepass"
            )
        content_hash = hashlib.sha256(value_type.encode("ascii") + b"\0" + raw).digest()
        row = connection.execute(
            "SELECT id FROM reasons WHERE content_hash = ?", (content_hash,)
        ).fetchone()
        if row is not None:
            return int(row["id"])
        codec, payload = _compress_bytes(raw)
        connection.execute(
            """
            INSERT OR IGNORE INTO reasons(content_hash, value_type, codec, raw_size, payload)
            VALUES(?, ?, ?, ?, ?)
            """,
            (content_hash, value_type, codec, len(raw), payload),
        )
        row = connection.execute(
            "SELECT id FROM reasons WHERE content_hash = ?", (content_hash,)
        ).fetchone()
        if row is None:
            raise RuntimeError("Failed to persist relation reason")
        return int(row["id"])

    @staticmethod
    def _get_question_id(
        connection: sqlite3.Connection,
        *,
        external_id: str,
        question: str,
        embedding: Optional[Iterable[float]],
        model_key: str,
    ) -> Optional[int]:
        embedding_blob, dimension, norm = _pack_embedding(embedding)
        if not external_id and not question and embedding_blob is None:
            return None
        question_raw = _encode_text(question)
        hasher = hashlib.sha256()
        for value in (external_id.encode("utf-8"), question_raw, model_key.encode("utf-8")):
            hasher.update(len(value).to_bytes(8, "little"))
            hasher.update(value)
        hasher.update(dimension.to_bytes(8, "little"))
        hasher.update(embedding_blob or b"")
        content_hash = hasher.digest()
        row = connection.execute(
            "SELECT id FROM questions WHERE content_hash = ?", (content_hash,)
        ).fetchone()
        if row is not None:
            return int(row["id"])
        connection.execute(
            """
            INSERT OR IGNORE INTO questions(
                external_id, question_utf8, model_key, dimension,
                embedding_f64, norm, content_hash
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                external_id,
                question_raw,
                model_key,
                dimension,
                embedding_blob,
                norm,
                content_hash,
            ),
        )
        row = connection.execute(
            "SELECT id FROM questions WHERE content_hash = ?", (content_hash,)
        ).fetchone()
        if row is None:
            raise RuntimeError("Failed to persist relation question")
        return int(row["id"])

    @staticmethod
    def _encode_extra(
        extra: Optional[dict[str, Any]],
    ) -> tuple[Optional[str], Optional[int], Optional[bytes]]:
        if not extra:
            return None, None, None
        raw = json.dumps(extra, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8", errors="surrogatepass"
        )
        codec, payload = _compress_bytes(raw)
        return codec, len(raw), payload

    @staticmethod
    def _insert_edge(
        connection: sqlite3.Connection,
        *,
        source_uri: str,
        target_uri: str,
        external_question_id: str,
        question: str,
        embedding: Optional[Iterable[float]],
        model_key: str,
        reason: Any,
        strategy: str,
        weight: float,
        extra: Optional[dict[str, Any]],
        created_at: Optional[int] = None,
    ) -> bool:
        source_id = SQLiteRelationStore._get_resource_id(connection, source_uri)
        target_id = SQLiteRelationStore._get_resource_id(connection, target_uri)
        question_row_id = SQLiteRelationStore._get_question_id(
            connection,
            external_id=external_question_id,
            question=question,
            embedding=embedding,
            model_key=model_key,
        )
        reason_id = SQLiteRelationStore._get_reason_id(connection, reason)
        extra_codec, extra_raw_size, extra_payload = SQLiteRelationStore._encode_extra(extra)
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO edges(
                source_id, target_id, question_id, question_key, reason_id,
                strategy, weight, extra_codec, extra_raw_size, extra_payload, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                target_id,
                question_row_id,
                external_question_id,
                reason_id,
                strategy,
                float(weight),
                extra_codec,
                extra_raw_size,
                extra_payload,
                int(created_at if created_at is not None else time.time_ns()),
            ),
        )
        return cursor.rowcount > 0

    def add_edges(
        self,
        *,
        source_uri: str,
        target_uris: Iterable[str],
        external_question_id: str,
        question: str,
        embedding: Optional[Iterable[float]],
        model_key: str,
        reason: Any,
        strategy: str,
        weight: float,
    ) -> int:
        if not self.exists:
            self._initialize()
        connection = self._connect()
        created = 0
        with connection:
            for target_uri in target_uris:
                if target_uri == source_uri:
                    continue
                if self._insert_edge(
                    connection,
                    source_uri=source_uri,
                    target_uri=target_uri,
                    external_question_id=external_question_id,
                    question=question,
                    embedding=embedding,
                    model_key=model_key,
                    reason=reason,
                    strategy=strategy,
                    weight=weight,
                    extra=None,
                ):
                    created += 1
        return created

    def import_records(
        self, records: Iterable[dict[str, Any]], *, model_key: str
    ) -> tuple[int, int]:
        if not self.exists:
            self._initialize()
        connection = self._connect()
        created = 0
        skipped = 0
        with connection:
            for record in records:
                inserted = self._insert_edge(
                    connection,
                    source_uri=str(record.get("uri1", "")),
                    target_uri=str(record.get("uri2", "")),
                    external_question_id=str(record.get("question_id", "")),
                    question=str(record.get("_question", "")),
                    embedding=record.get("_embedding"),
                    model_key=model_key,
                    reason=record.get("_reason", ""),
                    strategy=str(record.get("_strategy", "llm_review")),
                    weight=float(record.get("weight", 1.0)),
                    extra=record.get("_extra"),
                )
                if inserted:
                    created += 1
                else:
                    skipped += 1
        return created, skipped

    @lru_cache(maxsize=512)
    def _load_embedding(
        self, question_id: int, dimension: int, payload: bytes
    ) -> Optional[list[float]]:
        return _unpack_embedding(payload, dimension)

    @staticmethod
    def _decode_extra(
        codec: Optional[str], raw_size: Optional[int], payload: Optional[bytes]
    ) -> dict[str, Any]:
        if codec is None or raw_size is None or payload is None:
            return {}
        raw = _decompress_bytes(codec, payload, raw_size)
        value = json.loads(raw.decode("utf-8", errors="surrogatepass"))
        return value if isinstance(value, dict) else {}

    def get_edges(self, source_uri: str, strategy: str) -> list[dict[str, Any]]:
        if not self.exists:
            return []
        rows = self._connect().execute(
            """
            SELECT
                e.id,
                target.uri AS target_uri,
                e.question_key,
                e.reason_id,
                e.weight,
                e.extra_codec,
                e.extra_raw_size,
                e.extra_payload,
                q.id AS question_row_id,
                q.question_utf8,
                q.dimension,
                q.embedding_f64,
                reason.value_type AS reason_value_type,
                reason.codec AS reason_codec,
                reason.raw_size AS reason_raw_size,
                reason.payload AS reason_payload
            FROM resources source
            JOIN edges e ON e.source_id = source.id
            JOIN resources target ON target.id = e.target_id
            LEFT JOIN questions q ON q.id = e.question_id
            JOIN reasons reason ON reason.id = e.reason_id
            WHERE source.uri = ? AND e.strategy = ?
            ORDER BY e.id
            """,
            (source_uri, strategy),
        ).fetchall()
        result: list[dict[str, Any]] = []
        reasons: dict[int, Any] = {}
        for row in rows:
            question = (
                _decode_text(bytes(row["question_utf8"]))
                if row["question_utf8"] is not None
                else ""
            )
            embedding = None
            if row["embedding_f64"] is not None and row["question_row_id"] is not None:
                embedding = self._load_embedding(
                    int(row["question_row_id"]),
                    int(row["dimension"]),
                    bytes(row["embedding_f64"]),
                )
            record = self._decode_extra(
                row["extra_codec"],
                row["extra_raw_size"],
                bytes(row["extra_payload"]) if row["extra_payload"] is not None else None,
            )
            reason_id = int(row["reason_id"])
            if reason_id not in reasons:
                reason_raw = _decompress_bytes(
                    str(row["reason_codec"]),
                    bytes(row["reason_payload"]),
                    int(row["reason_raw_size"]),
                )
                if row["reason_value_type"] == "json":
                    reasons[reason_id] = json.loads(
                        reason_raw.decode("utf-8", errors="surrogatepass")
                    )
                else:
                    reasons[reason_id] = _decode_text(reason_raw)
            record.update(
                {
                    "uri1": source_uri,
                    "uri2": str(row["target_uri"]),
                    "question_id": str(row["question_key"]),
                    "question": question,
                    "embedding": embedding,
                    "reason": reasons[reason_id],
                    "weight": float(row["weight"]),
                }
            )
            result.append(record)
        return result

    def count_edges(self) -> int:
        if not self.exists:
            return 0
        row = self._connect().execute("SELECT COUNT(*) AS count FROM edges").fetchone()
        return int(row["count"]) if row else 0

    def get_all_edge_pairs(self, strategy: str) -> set[tuple[str, str]]:
        if not self.exists:
            return set()
        rows = self._connect().execute(
            """
            SELECT source.uri AS source_uri, target.uri AS target_uri
            FROM edges e
            JOIN resources source ON source.id = e.source_id
            JOIN resources target ON target.id = e.target_id
            WHERE e.strategy = ?
            """,
            (strategy,),
        ).fetchall()
        return {(str(row["source_uri"]), str(row["target_uri"])) for row in rows}


class LegacyJsonlRelationStore:
    """Read compatibility for the old per-directory JSONL layout."""

    def __init__(self, vikingfs_path: str | os.PathLike[str]):
        self.vikingfs_path = Path(vikingfs_path)
        self._ref_caches: dict[Path, dict[str, dict[str, Any]]] = {}

    def _parent_path(self, uri: str) -> Path:
        relative = uri[len("viking://") :] if uri.startswith("viking://") else uri
        local_path = self.vikingfs_path / Path(relative)
        return local_path if local_path.is_dir() else local_path.parent

    @staticmethod
    def _filename(strategy: str) -> str:
        return ".relations.jsonl" if strategy == "blind" else f".relations_{strategy}.jsonl"

    def _references(self, parent: Path) -> dict[str, dict[str, Any]]:
        cached = self._ref_caches.get(parent)
        if cached is not None:
            return cached
        references: dict[str, dict[str, Any]] = {}
        path = parent / ".reference_questions.jsonl"
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    question_id = str(record.get("id", ""))
                    if question_id:
                        references[question_id] = {
                            "question": record.get("question", ""),
                            "embedding": record.get("embedding"),
                        }
        self._ref_caches[parent] = references
        return references

    def get_edges(self, source_uri: str, strategy: str) -> list[dict[str, Any]]:
        parent = self._parent_path(source_uri)
        path = parent / self._filename(strategy)
        if not path.exists():
            return []
        references = self._references(parent)
        result: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("uri1", "") != source_uri:
                    continue
                question_id = str(record.get("question_id", ""))
                reference = references.get(question_id, {})
                question = reference.get("question", record.get("query_question", ""))
                embedding = reference.get("embedding", record.get("query_embedding"))
                normalized = dict(record)
                normalized["question_id"] = question_id
                normalized["question"] = question
                normalized["embedding"] = embedding
                normalized["reason"] = record.get("reason", question)
                result.append(normalized)
        return result

    def get_all_edge_pairs(self, strategy: str) -> set[tuple[str, str]]:
        filename = self._filename(strategy)
        pairs: set[tuple[str, str]] = set()
        resources_root = self.vikingfs_path / "resources"
        if not resources_root.exists():
            return pairs
        for path in resources_root.rglob(filename):
            if RELATION_STORE_DIR in path.parts:
                continue
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        source_uri = str(record.get("uri1", ""))
                        target_uri = str(record.get("uri2", ""))
                        if source_uri and target_uri and source_uri != target_uri:
                            pairs.add((source_uri, target_uri))
            except OSError:
                continue
        return pairs


class RelationRepository:
    """Select SQLite storage and retain legacy reads until migration completes."""

    def __init__(self, vikingfs_path: str | os.PathLike[str], mode: Optional[str] = None):
        self.vikingfs_path = Path(vikingfs_path)
        self.mode = (mode or os.environ.get("VIKINGBOT_RELATION_STORE", "auto")).lower()
        if self.mode not in {"auto", "sqlite", "jsonl"}:
            raise ValueError(f"Unsupported relation store mode: {self.mode}")
        self.sqlite = SQLiteRelationStore(self.vikingfs_path, create=False)
        self.legacy = LegacyJsonlRelationStore(self.vikingfs_path)

    @property
    def database_path(self) -> Path:
        return self.sqlite.db_path

    def get_edges(self, source_uri: str, strategy: str) -> list[dict[str, Any]]:
        if self.mode == "jsonl":
            return self.legacy.get_edges(source_uri, strategy)
        sqlite_edges = self.sqlite.get_edges(source_uri, strategy)
        if self.mode == "sqlite" or self.sqlite.migration_complete:
            return sqlite_edges
        legacy_edges = self.legacy.get_edges(source_uri, strategy)
        existing = {
            (edge.get("uri1", ""), edge.get("uri2", ""), edge.get("question_id", ""))
            for edge in legacy_edges
        }
        for edge in sqlite_edges:
            key = (
                edge.get("uri1", ""),
                edge.get("uri2", ""),
                edge.get("question_id", ""),
            )
            if key not in existing:
                legacy_edges.append(edge)
                existing.add(key)
        return legacy_edges

    def add_edges(
        self,
        *,
        source_uri: str,
        target_uris: Iterable[str],
        question: str,
        embedding: Optional[Iterable[float]],
        model_key: str,
        reason: str,
        strategy: str,
        weight: float,
    ) -> int:
        if self.mode == "jsonl":
            raise RuntimeError("JSONL relation store is read-only; use auto or sqlite for writes")
        external_question_id = compute_question_id(question) if question else ""
        return self.sqlite.add_edges(
            source_uri=source_uri,
            target_uris=target_uris,
            external_question_id=external_question_id,
            question=question,
            embedding=embedding,
            model_key=model_key,
            reason=reason,
            strategy=strategy,
            weight=weight,
        )

    def get_all_edge_pairs(self, strategy: str) -> set[tuple[str, str]]:
        if self.mode == "jsonl":
            return self.legacy.get_all_edge_pairs(strategy)
        pairs = self.sqlite.get_all_edge_pairs(strategy)
        if self.mode != "sqlite" and not self.sqlite.migration_complete:
            pairs.update(self.legacy.get_all_edge_pairs(strategy))
        return pairs

    def close(self) -> None:
        self.sqlite.close()


def _strategy_from_legacy_filename(filename: str) -> Optional[str]:
    if filename == ".relations.jsonl":
        return "blind"
    prefix = ".relations_"
    suffix = ".jsonl"
    if filename.startswith(prefix) and filename.endswith(suffix):
        return filename[len(prefix) : -len(suffix)]
    return None


def _load_legacy_references(path: Path, report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    references: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return references
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                report["invalid_reference_lines"] += 1
                continue
            question_id = str(record.get("id", ""))
            if question_id:
                references[question_id] = record
    return references


def migrate_legacy_relations(
    vikingfs_path: str | os.PathLike[str],
    *,
    model_key: str = "legacy",
    batch_size: int = 500,
    mark_complete: bool = True,
) -> dict[str, Any]:
    """Migrate scattered relation JSONL files into the central SQLite store.

    The operation is idempotent. Legacy files are never removed by this function.
    """
    vikingfs = Path(vikingfs_path).resolve()
    resources_root = vikingfs / "resources"
    if not resources_root.is_dir():
        raise FileNotFoundError(f"Viking resources directory not found: {resources_root}")
    store = SQLiteRelationStore(vikingfs, create=True)
    report: dict[str, Any] = {
        "schema_version": RELATION_SCHEMA_VERSION,
        "vikingfs_path": str(vikingfs),
        "database_path": str(store.db_path),
        "model_key": model_key,
        "relation_files": 0,
        "reference_files": 0,
        "source_bytes": 0,
        "edge_lines": 0,
        "created_edges": 0,
        "duplicate_edges": 0,
        "invalid_edge_lines": 0,
        "invalid_reference_lines": 0,
        "missing_references": 0,
        "migration_complete": False,
    }
    batch: list[dict[str, Any]] = []

    def flush() -> None:
        if not batch:
            return
        created, skipped = store.import_records(batch, model_key=model_key)
        report["created_edges"] += created
        report["duplicate_edges"] += skipped
        batch.clear()

    for relation_path in sorted(resources_root.rglob(".relations*.jsonl")):
        if RELATION_STORE_DIR in relation_path.parts:
            continue
        strategy = _strategy_from_legacy_filename(relation_path.name)
        if strategy is None:
            continue
        report["relation_files"] += 1
        report["source_bytes"] += relation_path.stat().st_size
        reference_path = relation_path.parent / ".reference_questions.jsonl"
        if reference_path.exists():
            report["reference_files"] += 1
            report["source_bytes"] += reference_path.stat().st_size
        references = _load_legacy_references(reference_path, report)
        with relation_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                report["edge_lines"] += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    report["invalid_edge_lines"] += 1
                    continue
                question_id = str(record.get("question_id", ""))
                reference = references.get(question_id)
                if question_id and reference is None:
                    report["missing_references"] += 1
                question = (
                    reference.get("question", "")
                    if reference is not None
                    else record.get("query_question", "")
                )
                embedding = (
                    reference.get("embedding")
                    if reference is not None
                    else record.get("query_embedding")
                )
                reason = record.get("reason", question)
                extra = {
                    key: value
                    for key, value in record.items()
                    if key not in _KNOWN_EDGE_FIELDS
                }
                batch.append(
                    {
                        "uri1": record.get("uri1", ""),
                        "uri2": record.get("uri2", ""),
                        "question_id": question_id,
                        "weight": record.get("weight", 1.0),
                        "_question": question,
                        "_embedding": embedding,
                        "_reason": reason,
                        "_strategy": strategy,
                        "_extra": extra,
                    }
                )
                if len(batch) >= batch_size:
                    flush()
    flush()
    if mark_complete and not report["invalid_edge_lines"]:
        store.mark_migration_complete()
        report["migration_complete"] = True
    report["database_edges"] = store.count_edges()
    store._connect().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    report["database_bytes"] = store.db_path.stat().st_size
    report_path = store.root / "migration-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    store.close()
    return report
