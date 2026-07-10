"""Migrate build-link JSONL files into the independent relation SQLite store."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from openviking.storage.build_link_relation_store import (
    RELATION_STORE_DIR,
    migrate_legacy_relations,
)


def _resolve_vikingfs_path(args: argparse.Namespace) -> Path:
    if args.vikingfs_path:
        return Path(args.vikingfs_path).expanduser().resolve()
    store_path = Path(args.store_path).expanduser().resolve()
    candidate = store_path / "viking"
    return candidate if candidate.is_dir() else store_path


def _cleanup_legacy_files(vikingfs_path: Path) -> dict[str, int]:
    resources_root = (vikingfs_path / "resources").resolve()
    relation_files = [
        path
        for path in resources_root.rglob(".relations*.jsonl")
        if RELATION_STORE_DIR not in path.parts
    ]
    reference_files = [
        path
        for path in resources_root.rglob(".reference_questions.jsonl")
        if RELATION_STORE_DIR not in path.parts
    ]
    for path in relation_files + reference_files:
        resolved = path.resolve()
        if resources_root not in resolved.parents:
            raise RuntimeError(f"Refusing to remove path outside resources root: {resolved}")
        resolved.unlink()
    return {
        "removed_relation_files": len(relation_files),
        "removed_reference_files": len(reference_files),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    location = parser.add_mutually_exclusive_group(required=True)
    location.add_argument("--store-path", help="Benchmark vector-store root containing viking/")
    location.add_argument("--vikingfs-path", help="Path to the viking/ directory")
    parser.add_argument("--model-key", default="legacy", help="Model tag for legacy embeddings")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--no-mark-complete",
        action="store_true",
        help="Keep auto mode reading both SQLite and legacy JSONL",
    )
    parser.add_argument(
        "--cleanup-legacy",
        action="store_true",
        help="Remove old JSONL only after a clean, complete migration",
    )
    args = parser.parse_args()

    vikingfs_path = _resolve_vikingfs_path(args)
    report = migrate_legacy_relations(
        vikingfs_path,
        model_key=args.model_key,
        batch_size=max(args.batch_size, 1),
        mark_complete=not args.no_mark_complete,
    )
    if args.cleanup_legacy:
        if not report["migration_complete"]:
            raise RuntimeError("Legacy cleanup requires a migration with no invalid edge lines")
        report.update(_cleanup_legacy_files(vikingfs_path))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
