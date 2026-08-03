"""Format-neutral aggregation of independently constructed document trees."""

from __future__ import annotations

import copy
import logging
from typing import Iterable

from bookrag_core.Index.Tree import DocumentTree

log = logging.getLogger(__name__)


def aggregate_document_trees(
    document_trees: Iterable[DocumentTree],
    *,
    cfg=None,
    dataset_name: str = "dataset",
) -> DocumentTree:
    """Add one structural dataset root without changing document-local depths."""
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
    # PDF-local pdf_id values intentionally overlap across documents and are
    # only needed while each source tree is being constructed.
    aggregate.pdf_id_to_index_id = {}
    aggregate.max_depth = max((node.depth for node in nodes), default=-1)
    if cfg is None and source_trees:
        aggregate.save_dir = source_trees[0].save_dir

    log.info(
        "Aggregated %d document trees into one dataset tree with %d nodes.",
        len(source_trees),
        len(aggregate.nodes),
    )
    return aggregate
