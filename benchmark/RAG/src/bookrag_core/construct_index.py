# Copyright (C) 2025-2026 Shu Wang
# SPDX-License-Identifier: Apache-2.0 OR AGPL-3.0-only

import os
import logging
import time
from typing import Callable, Optional, Set
import pandas as pd

from bookrag_core.configs.vdb_config import VDBConfig

log = logging.getLogger(__name__)

from bookrag_core.Index.GBCIndex import GBC
from bookrag_core.configs.system_config import SystemConfig
from bookrag_core.provider.TokenTracker import TokenTracker
from bookrag_core.utils.file_utils import save_indexing_stats

def construct_GBC_index_from_tree(
    cfg: SystemConfig,
    tree_index,
    tree_only: bool = False,
    graph_only: bool = False,
    summary_node_ids: Optional[Set[int]] = None,
    summary_checkpoint_callback: Optional[Callable] = None,
):
    """Construct the official GBC index from an already normalized tree.

    This is the format-neutral Core entry used by Markdown ingestion.  It keeps
    BookRAG's summary, KG refinement, persistence, and entity-VDB construction
    unchanged; only the PDF-specific tree construction step is bypassed.
    """
    from bookrag_core.Index.Tree import DocumentTree
    from bookrag_core.pipelines.kg_builder import build_knowledge_graph

    if not isinstance(tree_index, DocumentTree) or tree_index.root_node is None:
        raise ValueError("A non-empty DocumentTree is required to construct GBC")
    if tree_only and graph_only:
        raise ValueError("tree_only and graph_only cannot both be enabled")

    os.makedirs(cfg.save_path, exist_ok=True)
    tree_index.save_dir = cfg.save_path
    token_tracker = TokenTracker.get_instance()
    token_tracker.reset()
    from bookrag_core.checkpoint import BuildCheckpoint

    checkpoint = BuildCheckpoint(cfg.save_path)
    checkpoint.restore_token_usage(configure_persistence=False)
    current_run_stats = {"build_tree_time": 0.0}

    should_generate_summaries = cfg.tree.node_summary and (
        summary_node_ids is None or bool(summary_node_ids)
    )
    if should_generate_summaries:
        from bookrag_core.pipelines.tree_node_summary import generate_tree_node_summary
        from bookrag_core.provider.llm import LLM
        from bookrag_core.provider.vlm import VLM

        summary_start = time.time()
        llm = LLM(cfg.llm)
        vlm = VLM(cfg.vlm) if cfg.tree.use_vlm else None
        tree_index = generate_tree_node_summary(
            tree_index=tree_index,
            llm=llm,
            use_VLM=cfg.tree.use_vlm,
            vlm=vlm,
            target_node_ids=summary_node_ids,
            checkpoint_callback=summary_checkpoint_callback,
        )
        if summary_checkpoint_callback is not None:
            summary_checkpoint_callback(tree_index)
        current_run_stats["build_summary_time"] = round(
            time.time() - summary_start, 2
        )
        summary_cost = token_tracker.record_stage("tree_node_summary")
        log.info("Tree node summary generation cost: %s", summary_cost)

    tree_index.save_to_file()
    if tree_only:
        current_run_stats["token_stage_history"] = token_tracker.stage_history
        save_indexing_stats(save_path=cfg.save_path, new_stats=current_run_stats)
        return tree_index

    kg_start_time = time.time()
    graph_index = build_knowledge_graph(tree_index, cfg)
    kg_duration = time.time() - kg_start_time
    current_run_stats["build_kg_time"] = round(kg_duration, 2)
    log.info(
        "Knowledge graph constructed from normalized tree in %.2f seconds.",
        kg_duration,
    )

    if graph_only:
        graph_index.save_graph()
        current_run_stats["token_stage_history"] = token_tracker.stage_history
        save_indexing_stats(save_path=cfg.save_path, new_stats=current_run_stats)
        return graph_index

    gbc_index = GBC(config=cfg, graph_index=graph_index, TreeIndex=tree_index)
    gbc_index.save_gbc_index()
    gbc_index.rebuild_vdb()

    current_run_stats["token_stage_history"] = token_tracker.stage_history
    save_indexing_stats(save_path=cfg.save_path, new_stats=current_run_stats)
    return gbc_index


def construct_GBC_index_from_markdown(
    cfg: SystemConfig,
    documents,
    *,
    dataset_name: str = "dataset",
    tree_only: bool = False,
    graph_only: bool = False,
):
    """Build one dataset-level GBC index from normalized Markdown documents."""
    from bookrag_core.pipelines.markdown_tree_builder import (
        build_dataset_tree_from_markdown,
    )
    from bookrag_core.checkpoint import BuildCheckpoint, TreeValidation

    documents = list(documents)
    checkpoint = BuildCheckpoint(cfg.save_path)
    token_tracker = TokenTracker.get_instance()
    token_tracker.reset()
    context_matches = checkpoint.context_matches(documents, cfg)
    if context_matches:
        checkpoint.restore_token_usage()
    else:
        log.info("[Resume] Source files or tree configuration changed; resetting checkpoints.")
        checkpoint.reset_for_context(documents, cfg)
        checkpoint.restore_token_usage()
    tree_validation = (
        checkpoint.validate_tree(documents, cfg)
        if context_matches
        else TreeValidation(False, reason="source or tree configuration changed")
    )
    if tree_validation.valid:
        tree_index = tree_validation.tree
        log.info(
            "[Resume] Tree valid: %d nodes%s.",
            len(tree_index.nodes),
            "; tree.json repaired" if tree_validation.json_repaired else "",
        )
    else:
        log.info("[Resume] Rebuilding Markdown tree: %s.", tree_validation.reason)
        tree_start_time = time.time()
        tree_index = build_dataset_tree_from_markdown(
            cfg,
            documents,
            dataset_name=dataset_name,
        )
        tree_duration = time.time() - tree_start_time
        tree_index.save_dir = cfg.save_path
        tree_index.save_to_file()
        checkpoint.mark_tree_complete(tree_index, documents, cfg)
        log.info(
            "Dataset tree constructed from %d Markdown documents in %.2f seconds.",
            len(documents),
            tree_duration,
        )

    if cfg.tree.node_summary:
        summary_validation = checkpoint.validate_summaries(tree_index, cfg)
        summary_targets = summary_validation.target_node_ids
        if summary_validation.complete:
            log.info(
                "[Resume] Summary valid: %d/%d. Skipping summary generation.",
                len(summary_validation.required_node_ids),
                len(summary_validation.required_node_ids),
            )
        else:
            # An ancestor summary depends on its descendants.  Clear every
            # affected ancestor before starting so an interruption after the
            # child batch cannot make a stale, non-empty ancestor look valid.
            for node_id in summary_targets:
                node = tree_index.get_node_by_index_id(node_id)
                if node is not None:
                    node.summary = ""
            checkpoint.mark_summary_progress(tree_index, cfg)
            log.info(
                "[Resume] Summary incomplete: regenerating %d nodes (%d directly invalid).",
                len(summary_targets),
                len(summary_validation.invalid_node_ids),
            )
    else:
        summary_targets = set()
        checkpoint.update_stage("summary", status="disabled")

    def save_summary_checkpoint(updated_tree):
        checkpoint.mark_summary_progress(updated_tree, cfg)

    try:
        return construct_GBC_index_from_tree(
            cfg,
            tree_index,
            tree_only=tree_only,
            graph_only=graph_only,
            summary_node_ids=summary_targets,
            summary_checkpoint_callback=save_summary_checkpoint,
        )
    finally:
        TokenTracker.get_instance().set_persistence_path(None)


def construct_GBC_index(cfg: SystemConfig, tree_only: bool = False, graph_only: bool = False):
    """
    Construct the GBC index from the document tree and knowledge graph.

    :param cfg: Configuration object containing settings for the index construction.
    :return: A tuple containing the DocumentTree and Graph objects.
    """
    from bookrag_core.pipelines.doc_tree_builder import build_tree_from_pdf
    from bookrag_core.pipelines.kg_builder import build_knowledge_graph

    log.info("Starting GBC index construction...")

    token_tracker = TokenTracker.get_instance()
    token_tracker.reset()

    # This dictionary will hold all stats for the CURRENT run
    current_run_stats = {}

    # --- Measure Tree Building ---
    tree_start_time = time.time()
    tree_index = build_tree_from_pdf(cfg)
    tree_duration = time.time() - tree_start_time
    log.info(f"Document tree constructed in {tree_duration:.2f} seconds.")
    current_run_stats["build_tree_time"] = round(tree_duration, 2)

    if tree_only:
        log.info("Only build tree index. Finished.")
        # Add final token usage to our stats dictionary
        current_run_stats["token_stage_history"] = token_tracker.stage_history

        # Save all collected stats and exit
        save_indexing_stats(save_path=cfg.save_path, new_stats=current_run_stats)
        return

    # --- Measure Knowledge Graph Building ---
    kg_start_time = time.time()
    graph_index = build_knowledge_graph(tree_index, cfg)

    kg_duration = time.time() - kg_start_time
    log.info(f"Knowledge graph constructed and saved in {kg_duration:.2f} seconds.")
    
    if graph_only:
        log.info("Only build graph index. Finished.")
        graph_index.save_graph()
        return
    
    # The 'kg_extraction' stage is recorded inside build_knowledge_graph
    gbc_index = GBC(config=cfg, graph_index=graph_index, TreeIndex=tree_index)
    gbc_index.save_gbc_index()

    # rebuild vdb
    gbc_index.rebuild_vdb()

    current_run_stats["build_kg_time"] = round(kg_duration, 2)

    # --- Finalize and Save All Stats for the Full Run ---
    log.info("Full GBC index construction finished. Saving final stats...")
    current_run_stats["token_stage_history"] = token_tracker.stage_history

    save_indexing_stats(save_path=cfg.save_path, new_stats=current_run_stats)

    return

def rebuild_graph_vdb(cfg: SystemConfig):
    gbc_index = GBC.load_gbc_index(cfg)
    gbc_index.rebuild_vdb()
    log.info("Rebuilt graph VDB successfully.")


def construct_vdb(cfg: SystemConfig):
    from bookrag_core.pipelines.doc_tree_builder import build_tree_from_pdf
    from bookrag_core.pipelines.vdb_index import build_other_vdb_index, build_vdb_index

    token_tracker = TokenTracker.get_instance()
    token_tracker.reset()

    log.info("Starting vector database construction...")

    if cfg.index_type in ["vanilla", "bm25", "raptor"]:
        log.info(f"Index type is {cfg.index_type}. Start building other vdb index...")
        build_other_vdb_index(cfg)
        return

    current_run_stats = {}

    tree_start_time = time.time()
    tree_index = build_tree_from_pdf(cfg)
    tree_duration = time.time() - tree_start_time
    log.info(f"Document tree constructed in {tree_duration:.2f} seconds.")
    current_run_stats["build_tree_time"] = round(tree_duration, 2)

    log.info("Document tree constructed successfully for vector database.")

    current_run_stats["token_stage_history"] = token_tracker.stage_history

    # Save all collected stats and exit
    save_indexing_stats(save_path=cfg.save_path, new_stats=current_run_stats)

    vdb_cfg: VDBConfig = cfg.vdb
    if cfg.save_path not in vdb_cfg.vdb_dir_name:
        vdb_cfg.vdb_dir_name = os.path.join(cfg.save_path, vdb_cfg.vdb_dir_name)
    log.info(f"Vector database path set to: {vdb_cfg.vdb_dir_name}")

    # if exist the dir, remove and rebuild vdb
    if os.path.exists(vdb_cfg.vdb_dir_name) and not vdb_cfg.force_rebuild:
        log.info(f"Vector database path already exists: {vdb_cfg.vdb_dir_name}. Skip")
        return

    if vdb_cfg.force_rebuild and os.path.exists(vdb_cfg.vdb_dir_name):
        log.info(
            f"Vector database path already exists: {vdb_cfg.vdb_dir_name}. Remove and rebuild"
        )
        import shutil

        shutil.rmtree(vdb_cfg.vdb_dir_name)

    os.makedirs(os.path.dirname(vdb_cfg.vdb_dir_name), exist_ok=True)

    vbd_start_time = time.time()
    build_vdb_index(tree_index, vdb_cfg)
    vdb_duration = time.time() - vbd_start_time
    log.info(f"Vector database constructed in {vdb_duration:.2f} seconds.")

    current_run_stats["build_vdb_time"] = round(vdb_duration, 2)

    # Save all collected stats and exit
    save_indexing_stats(save_path=cfg.save_path, new_stats=current_run_stats)


def compute_mm_reranker(cfg: SystemConfig, group: pd.DataFrame):
    from bookrag_core.pipelines.doc_tree_builder import build_tree_from_pdf
    from bookrag_core.pipelines.vdb_index import (
        compute_mm_embedding,
        compute_mm_embedding_question,
    )

    tree_index = build_tree_from_pdf(cfg)

    compute_mm_embedding(cfg, tree_index)
    
    compute_mm_embedding_question(cfg, group)


if __name__ == "__main__":
    print("test")

    # parser = argparse.ArgumentParser(description="Extract text content from PDF files.")
    # parser.add_argument(
    #     "--config_path",
    #     type=str,
    #     default="/home/wangshu/multimodal/GBC-RAG/config/gbc.yaml",
    #     help="Path to the configuration file.",
    # )

    # args = parser.parse_args()

    # cfg = load_system_config(args.config_path)

    # if not os.path.exists(cfg.save_path):
    #     os.makedirs(cfg.save_path)
    #     log.info(f"Created directory: {cfg.save_path}")
    # else:
    #     log.info(f"Directory already exists: {cfg.save_path}")

    # construct_vdb(cfg)

    # gbc_index = construct_GBC_index(cfg)
    # log.info("GBC index construction completed successfully.")
