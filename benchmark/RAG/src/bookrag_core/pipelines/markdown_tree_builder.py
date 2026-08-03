# Copyright (C) 2025-2026 Shu Wang
# SPDX-License-Identifier: Apache-2.0 OR AGPL-3.0-only

"""Build BookRAG document trees from pipeline-normalized Markdown."""

from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from bookrag_core.configs.system_config import SystemConfig
from bookrag_core.Index.Tree import DocumentTree, NodeType, TreeNode

log = logging.getLogger(__name__)

_ATX_HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*$")
_SETEXT_HEADING = re.compile(r"^ {0,3}(=+|-+)\s*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_LIST_ITEM = re.compile(r"^ {0,3}(?:[-+*]|\d+[.)])\s+")


@dataclass(frozen=True)
class MarkdownBlock:
    kind: str
    text: str
    heading_level: int | None = None


def _is_table_delimiter(line: str) -> bool:
    stripped = line.strip().strip("|")
    cells = [cell.strip() for cell in stripped.split("|")]
    return len(cells) >= 2 and all(re.fullmatch(r":?-{3,}:?", cell) is not None for cell in cells)


def _is_table_start(lines: list[str], index: int) -> bool:
    return index + 1 < len(lines) and "|" in lines[index] and _is_table_delimiter(lines[index + 1])


def _heading(line: str) -> tuple[int, str] | None:
    match = _ATX_HEADING.match(line)
    if not match:
        return None
    title = re.sub(r"\s+#+\s*$", "", match.group(2)).strip()
    return len(match.group(1)), title


def parse_markdown_blocks(markdown: str) -> list[MarkdownBlock]:
    """Parse structural Markdown blocks without adding a parser dependency."""
    lines = (markdown or "").lstrip("\ufeff").splitlines()
    blocks: list[MarkdownBlock] = []
    index = 0

    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue

        heading = _heading(line)
        if heading:
            level, title = heading
            if title:
                blocks.append(MarkdownBlock("heading", title, level))
            index += 1
            continue

        if index + 1 < len(lines) and _SETEXT_HEADING.match(lines[index + 1]):
            title = line.strip()
            if title:
                level = 1 if lines[index + 1].lstrip().startswith("=") else 2
                blocks.append(MarkdownBlock("heading", title, level))
            index += 2
            continue

        fence = _FENCE.match(line)
        if fence:
            marker = fence.group(1)
            fence_lines = [line]
            index += 1
            while index < len(lines):
                current = lines[index]
                fence_lines.append(current)
                index += 1
                closing = current.lstrip()
                if closing.startswith(marker[0] * len(marker)):
                    break
            blocks.append(MarkdownBlock("code", "\n".join(fence_lines)))
            continue

        if _is_table_start(lines, index):
            table_lines = [lines[index], lines[index + 1]]
            index += 2
            while index < len(lines) and lines[index].strip() and "|" in lines[index]:
                table_lines.append(lines[index])
                index += 1
            blocks.append(MarkdownBlock("table", "\n".join(table_lines)))
            continue

        if _LIST_ITEM.match(line):
            list_lines = [line]
            index += 1
            while index < len(lines):
                current = lines[index]
                if not current.strip():
                    break
                if _heading(current) or _FENCE.match(current) or _is_table_start(lines, index):
                    break
                if _LIST_ITEM.match(current) or current.startswith(("  ", "\t")):
                    list_lines.append(current)
                    index += 1
                    continue
                break
            blocks.append(MarkdownBlock("list", "\n".join(list_lines)))
            continue

        paragraph_lines = [line]
        index += 1
        while index < len(lines) and lines[index].strip():
            current = lines[index]
            if (
                _heading(current)
                or _FENCE.match(current)
                or _LIST_ITEM.match(current)
                or _is_table_start(lines, index)
            ):
                break
            if index + 1 < len(lines) and _SETEXT_HEADING.match(lines[index + 1]):
                break
            paragraph_lines.append(current)
            index += 1
        blocks.append(MarkdownBlock("paragraph", "\n".join(paragraph_lines)))

    return blocks


class MarkdownTreeBuilder:
    """Map normalized Markdown blocks into the official BookRAG tree model."""

    @staticmethod
    def _title_path(node: TreeNode | None) -> list[str]:
        titles = []
        while node is not None:
            if node.type == NodeType.TITLE and node.meta_info.content:
                titles.append(node.meta_info.content)
            node = node.parent
        return list(reversed(titles))

    @staticmethod
    def _add_title(
        tree: DocumentTree,
        parent: TreeNode,
        title: str,
        markdown_level: int,
        sample_id: str,
        source_path: str,
    ) -> TreeNode:
        node = TreeNode(
            {
                "file_name": Path(source_path).name,
                "file_path": source_path,
                "sample_id": sample_id,
                "content": title,
                "title_level": markdown_level - 1,
                "block_type": "heading",
            }
        )
        node.type = NodeType.TITLE
        node.outline_node = True
        tree.add_node(node)
        parent.add_child(node)
        node.meta_info.local_index_id = node.index_id
        node.meta_info.title_path = MarkdownTreeBuilder._title_path(node)
        return node

    def build(
        self,
        markdown: str,
        sample_id: str,
        source_path: str,
        *,
        cfg: SystemConfig | None = None,
    ) -> DocumentTree:
        source_path = str(Path(source_path).resolve())
        meta = {
            "file_name": Path(source_path).name,
            "file_path": source_path,
            "sample_id": str(sample_id),
        }
        tree = DocumentTree(meta_dict=meta, cfg=cfg)
        tree.root_node.meta_info.local_index_id = 0

        blocks = parse_markdown_blocks(markdown)
        heading_stack: dict[int, TreeNode] = {}
        fallback_title = Path(source_path).stem or str(sample_id)

        def ensure_document_title() -> TreeNode:
            if 1 not in heading_stack:
                heading_stack[1] = self._add_title(
                    tree,
                    tree.root_node,
                    fallback_title,
                    1,
                    str(sample_id),
                    source_path,
                )
            return heading_stack[1]

        for block in blocks:
            if block.kind == "heading":
                level = block.heading_level or 1
                possible_parents = [item for item in heading_stack if item < level]
                parent = (
                    heading_stack[max(possible_parents)] if possible_parents else tree.root_node
                )
                node = self._add_title(
                    tree,
                    parent,
                    block.text,
                    level,
                    str(sample_id),
                    source_path,
                )
                heading_stack = {
                    item_level: item_node
                    for item_level, item_node in heading_stack.items()
                    if item_level < level
                }
                heading_stack[level] = node
                continue

            parent = heading_stack[max(heading_stack)] if heading_stack else ensure_document_title()
            meta_dict = {
                "file_name": Path(source_path).name,
                "file_path": source_path,
                "sample_id": str(sample_id),
                "content": block.text,
                "title_path": self._title_path(parent),
                "block_type": block.kind,
            }
            if block.kind == "table":
                meta_dict["table_body"] = block.text
            node = TreeNode(meta_dict)
            node.type = NodeType.TABLE if block.kind == "table" else NodeType.TEXT
            tree.add_node(node)
            parent.add_child(node)
            node.meta_info.local_index_id = node.index_id

        if not blocks or not any(node.type == NodeType.TITLE for node in tree.nodes):
            ensure_document_title()

        tree.max_depth = max((node.depth for node in tree.nodes), default=0)
        log.info(
            "Built Markdown tree for sample %s with %d nodes.",
            sample_id,
            len(tree.nodes),
        )
        return tree

    def build_file(
        self,
        markdown_path: str | Path,
        sample_id: str,
        *,
        cfg: SystemConfig | None = None,
    ) -> DocumentTree:
        path = Path(markdown_path)
        if path.suffix.lower() not in {".md", ".markdown"}:
            raise ValueError(f"BookRAG only accepts normalized Markdown: {path}")
        if not path.is_file():
            raise FileNotFoundError(f"Markdown source does not exist: {path}")
        return self.build(
            path.read_text(encoding="utf-8"),
            sample_id=str(sample_id),
            source_path=str(path),
            cfg=cfg,
        )


def build_tree_from_markdown(
    cfg: SystemConfig,
    markdown_path: str | Path,
    sample_id: str,
) -> DocumentTree:
    """Public Core entry for constructing one document tree from Markdown."""
    builder = MarkdownTreeBuilder()
    return builder.build_file(markdown_path, sample_id=str(sample_id), cfg=cfg)


def aggregate_document_trees(
    document_trees: Iterable[DocumentTree],
    *,
    cfg: SystemConfig | None = None,
    dataset_name: str = "dataset",
) -> DocumentTree:
    """Fuse document trees without changing their original node depths."""
    source_trees = list(document_trees)
    aggregate = DocumentTree(
        meta_dict={"file_name": dataset_name, "sample_id": dataset_name},
        cfg=cfg,
    )
    aggregate.root_node.depth = -1
    aggregate.root_node.meta_info.local_index_id = None
    aggregate.root_node.meta_info.block_type = "dataset_root"

    nodes = [aggregate.root_node]
    for source_tree in source_trees:
        cloned_tree = copy.deepcopy(source_tree)
        document_root = cloned_tree.root_node
        if document_root is None:
            continue
        document_root.parent = aggregate.root_node
        aggregate.root_node.children.append(document_root)

        for node in cloned_tree.nodes:
            if node.meta_info.local_index_id is None:
                node.meta_info.local_index_id = node.index_id
            node.index_id = len(nodes)
            nodes.append(node)

    aggregate.nodes = nodes
    aggregate.pdf_id_to_index_id = {}
    aggregate.max_depth = max((node.depth for node in nodes), default=-1)
    if cfg is None and source_trees:
        aggregate.save_dir = source_trees[0].save_dir

    log.info(
        "Aggregated %d Markdown documents into one tree with %d nodes.",
        len(source_trees),
        len(aggregate.nodes),
    )
    return aggregate


def build_dataset_tree_from_markdown(
    cfg: SystemConfig,
    documents: Sequence[tuple[str, str | Path]],
    *,
    dataset_name: str = "dataset",
) -> DocumentTree:
    """Build one deterministic dataset-level tree from Markdown documents."""
    ordered = sorted(
        ((str(sample_id), Path(path)) for sample_id, path in documents),
        key=lambda item: (item[0], str(item[1].resolve())),
    )
    trees = [build_tree_from_markdown(cfg, path, sample_id) for sample_id, path in ordered]
    return aggregate_document_trees(trees, cfg=cfg, dataset_name=dataset_name)
