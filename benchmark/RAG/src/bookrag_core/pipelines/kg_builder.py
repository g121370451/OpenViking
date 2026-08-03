# Copyright (C) 2025-2026 Shu Wang
# SPDX-License-Identifier: Apache-2.0 OR AGPL-3.0-only

from bookrag_core.Index.Tree import DocumentTree, NodeType
from bookrag_core.Index.Graph import Graph
from bookrag_core.pipelines.kg_extractor import KGExtractor
from bookrag_core.pipelines.kg_refiner import KGRefiner
from bookrag_core.configs.system_config import SystemConfig

from bookrag_core.provider.llm import LLM
from bookrag_core.provider.vlm import VLM
from bookrag_core.provider.TokenTracker import TokenTracker
from bookrag_core.checkpoint import BuildCheckpoint, canonical_sha256
from bookrag_core.Index.GBCIndex import get_graph_variant

import logging

log = logging.getLogger(__name__)

# print log for test
from rich.logging import RichHandler

import os
import time
from tqdm import tqdm

# log_dir = "/home/wangshu/multimodal/GBC-RAG/test/index_qwen3/logs"
# if not os.path.exists(log_dir):
#     os.makedirs(log_dir)
# log_file = os.path.join(log_dir, f"kg_builder_{time.strftime('%Y%m%d_%H%M%S')}.log")
# logging.basicConfig(
#     level="INFO",
#     format="%(asctime)s - %(levelname)s - %(message)s",
#     datefmt="%H:%M:%S",
#     handlers=[
#         RichHandler(rich_tracebacks=True),  # RichHandler 会继续使用自己的漂亮格式
#         logging.FileHandler(
#             log_file, encoding="utf-8"
#         ),  # FileHandler 会使用上面定义的 format
#     ],
# )


def build_knowledge_graph(tree: DocumentTree, cfg: SystemConfig):
    """
    Build a knowledge graph from the given document tree.

    :param tree: DocumentTree object containing the document structure.
    :param graph_config: GraphConfig object containing configuration for the graph.
    :return: A tuple containing the KGExtractor and KGRefiner instances.
    """
    llm = LLM(cfg.llm)
    ingest_workers = max(1, int(getattr(cfg, "ingest_workers", 1)))
    vlm = VLM(cfg.vlm) if cfg.graph.image_description_force else None

    # try load_the graph if constructed before
    # graph_path = os.path.join(cfg.save_path, Graph._DATA_FILE)
    # if os.path.exists(graph_path):
    #     log.info(f"Loading existing knowledge graph from {graph_path}...")
    #     graph_index = Graph.load_from_dir(cfg.save_path)
    #     return graph_index
    # else:
    #     log.info("No existing knowledge graph found. Creating a new one...")

    g = cfg.graph.g
    variant = get_graph_variant(cfg)
    checkpoint = BuildCheckpoint(cfg.save_path)

    # Try to load the graph index if it already exists
    target_graph_file = Graph._get_filename(variant)
    target_graph_path = os.path.join(cfg.save_path, target_graph_file)

    graph_stage = checkpoint.stage("kg_refinement")
    extraction_stage = checkpoint.stage("kg_extraction")
    graph_checkpoint_valid = (
        graph_stage.get("status") == "complete"
        and graph_stage.get("config_fingerprint")
        == checkpoint.refinement_fingerprint(cfg)
        and graph_stage.get("extraction_fingerprint")
        == extraction_stage.get("result_fingerprint")
        and bool(extraction_stage.get("result_fingerprint"))
    )
    if os.path.exists(target_graph_path) and graph_checkpoint_valid:
        log.info(
            f"Existing knowledge graph detected at {target_graph_path}. Trying to load..."
        )
        try:
            graph_index = Graph.load_from_dir(cfg.save_path, variant=variant)
            log.info("Loaded existing knowledge graph successfully. Skip rebuilding.")
            return graph_index
        except Exception as e:
            log.warning(
                f"Failed to load existing graph from {target_graph_path}, will rebuild. Reason: {e}"
            )
    else:
        if os.path.exists(target_graph_path):
            log.warning(
                "Ignoring stale graph artifact without a matching complete checkpoint: %s",
                target_graph_path,
            )
        log.info(
            f"No existing knowledge graph found at {target_graph_path}. Creating a new one..."
        )

    graph_index = Graph(save_path=cfg.save_path, variant=variant)

    kg_extractor = KGExtractor(
        cfg_graph=cfg.graph, llm=llm, vlm=vlm, save_path=cfg.save_path
    )
    kg_extract_res = []
    token_tracker = TokenTracker.get_instance()
    extraction_token_baseline = token_tracker.get_usage()

    batch_process = True

    if batch_process:
        log.info("Batch processing is enabled for knowledge graph extraction.")
        batch_nodes = []
        batch_title_nodes = []
        batch_title_paths = []
        batch_sibling_nodes = []
        for node in tree.nodes:
            # for node in tree.nodes[:30]:
            if node.type == NodeType.ROOT:
                # Dataset and document roots are structural boundaries only.
                continue
            if node.type == NodeType.TITLE:
                # For title nodes, we collect the path and sibling nodes for batch processing
                title_path = tree.get_path_from_root(node.index_id)
                sibling_nodes = tree.get_sibling_nodes(node.index_id)
                batch_title_nodes.append(node)
                batch_title_paths.append(title_path)
                batch_sibling_nodes.append(sibling_nodes)
            else:
                # For other nodes, we collect them for batch processing
                batch_nodes.append(node)

        # Process title nodes in batches
        if batch_title_nodes:
            log.info("Processing title nodes in batches...")
            res_dict = kg_extractor.batch_extract_titles(
                nodes=batch_title_nodes,
                title_paths=batch_title_paths,
                sibling_nodes_list=batch_sibling_nodes,
                max_workers=ingest_workers,
            )
            kg_extract_res.extend(res_dict)

        if batch_nodes:
            log.info("Processing non-title nodes in batches...------")
            res_dict = kg_extractor.batch_extract_kg(
                nodes=batch_nodes,
                max_workers=ingest_workers,
            )
            kg_extract_res.extend(res_dict)

        # resort the results based on node index
        kg_extract_res.sort(key=lambda x: x.get("node_idx", -1))
    else:
        for node in tree.nodes[:30]:
            # Extract entities and relationships from the node
            if node.type == NodeType.ROOT:
                # Dataset and document roots are structural boundaries only.
                continue
            if node.type == NodeType.TITLE:
                title_path = tree.get_path_from_root(node.index_id)
                sibling_nodes = tree.get_sibling_nodes(node.index_id)
                res_dict = kg_extractor.extract_title(node, title_path, sibling_nodes)
            else:
                res_dict = kg_extractor.extract_kg(node)
            kg_extract_res.append(res_dict)

    log.info("Knowledge graph extraction completed.")
    log.info(f"Extracted {len(kg_extract_res)} nodes from the document tree.")

    expected_node_ids = {
        node.index_id for node in tree.nodes if node.type != NodeType.ROOT
    }
    extracted_node_ids = {result.get("node_idx") for result in kg_extract_res}
    if extracted_node_ids != expected_node_ids:
        missing = sorted(expected_node_ids - extracted_node_ids)
        unexpected = sorted(extracted_node_ids - expected_node_ids)
        raise RuntimeError(
            "KG extraction cache coverage is incomplete: "
            f"missing={missing[:10]}, unexpected={unexpected[:10]}"
        )

    kg_extraction_cost = token_tracker.record_stage_since(
        "kg_extraction",
        extraction_token_baseline,
    )
    log.info(f"Knowledge graph extraction cost: {kg_extraction_cost}")

    extraction_fingerprint = canonical_sha256(kg_extract_res)
    checkpoint.update_stage(
        "kg_extraction",
        status="complete",
        completed_nodes=len(extracted_node_ids),
        total_nodes=len(expected_node_ids),
        result_fingerprint=extraction_fingerprint,
    )

    result_node_ids = [result.get("node_idx", -1) for result in kg_extract_res]
    refinement_snapshot = checkpoint.load_refinement(
        cfg,
        result_node_ids,
        extraction_fingerprint,
    )
    if refinement_snapshot is not None:
        graph_index = refinement_snapshot["graph_index"]
        graph_index.save_dir = cfg.save_path
        graph_index.variant = variant
        graph_index.data_filename = Graph._get_filename(variant)
        start_position = int(refinement_snapshot["next_position"])
        refinement_phase = str(
            refinement_snapshot.get("phase", "entity_resolution")
        )
        log.info(
            "[Resume] KG refinement checkpoint valid: phase=%s, nodes=%d/%d.",
            refinement_phase,
            start_position,
            len(kg_extract_res),
        )
    else:
        start_position = 0
        refinement_phase = "entity_resolution"

    refinement_token_baseline = token_tracker.get_usage()
    kg_refiner = KGRefiner(
        llm=llm,
        graph_config=cfg.graph,
        graph_index=graph_index,
        save_path=cfg.save_path,
        g=cfg.graph.g,
        resume_state=refinement_snapshot,
        max_workers=ingest_workers,
    )
    checkpoint_every = max(
        1,
        int(getattr(cfg.graph, "checkpoint_every", 250)),
    )

    def save_refinement_checkpoint(next_position: int, phase: str) -> None:
        checkpoint.save_refinement(
            cfg,
            result_node_ids,
            graph_index=graph_index,
            entity_alias_map=getattr(kg_refiner, "entity_alias_map", {}),
            entity_to_vdb_id=getattr(kg_refiner, "entity_to_vdb_id", {}),
            next_position=next_position,
            phase=phase,
            extraction_fingerprint=extraction_fingerprint,
        )

    try:
        if refinement_phase == "entity_resolution":
            pending_results = kg_extract_res[start_position:]
            with tqdm(
                total=len(kg_extract_res),
                initial=start_position,
                desc="Refining KG subgraphs",
                unit="node",
                dynamic_ncols=True,
            ) as progress:
                if cfg.graph.refine_type == "basic":
                    for offset, res in enumerate(
                        pending_results,
                        start=start_position,
                    ):
                        log.info("Using basic KG refinement.")
                        kg_refiner.basic_kg_refiner(
                            entities=res.get("entities", []),
                            relationships=res.get("relations", []),
                            source_id=res.get("node_idx", -1),
                        )
                        next_position = offset + 1
                        progress.update(1)
                        if (
                            next_position % checkpoint_every == 0
                            or next_position == len(kg_extract_res)
                        ):
                            save_refinement_checkpoint(
                                next_position,
                                "entity_resolution",
                            )
                elif cfg.graph.refine_type == "advanced":
                    for offset, res in enumerate(
                        pending_results,
                        start=start_position,
                    ):
                        kg_refiner.advanced_kg_refiner(
                            entities=res.get("entities", []),
                            relationships=res.get("relations", []),
                            source_id=res.get("node_idx", -1),
                        )
                        next_position = offset + 1
                        progress.update(1)
                        if (
                            next_position % checkpoint_every == 0
                            or next_position == len(kg_extract_res)
                        ):
                            save_refinement_checkpoint(
                                next_position,
                                "entity_resolution",
                            )
                else:
                    raise ValueError(
                        f"Unsupported KG refine_type: {cfg.graph.refine_type}"
                    )
            refinement_phase = "entity_resolution_complete"
            save_refinement_checkpoint(len(kg_extract_res), refinement_phase)

        if refinement_phase == "entity_resolution_complete":
            kg_refiner.refine_entities()
            description_separator = getattr(KGRefiner, "_DESCRIPTION_SEP_", "<SEP>")
            unrefined_entities = [
                node_name
                for node_name in graph_index.kg.nodes
                if description_separator
                in str(graph_index.kg.nodes[node_name].get("description", ""))
            ]
            if unrefined_entities:
                raise RuntimeError(
                    "Entity description refinement remains incomplete for "
                    f"{len(unrefined_entities)} entities; successful per-entity results "
                    "were cached and will be reused on restart."
                )
            refinement_phase = "entity_descriptions_complete"
            save_refinement_checkpoint(len(kg_extract_res), refinement_phase)

        if refinement_phase == "entity_descriptions_complete":
            kg_refiner.refine_relation()
            refinement_phase = "complete"
            save_refinement_checkpoint(len(kg_extract_res), refinement_phase)
    except BaseException:
        kg_refiner.close()
        raise

    log.info("Knowledge graph refinement completed.")
    kg_refinement_cost = token_tracker.record_stage_since(
        "kg_refinement",
        refinement_token_baseline,
    )
    log.info(f"Knowledge graph refinement cost: {kg_refinement_cost}")

    kg_refiner.close()

    # Persist the completed graph before final entity-VDB construction.  A
    # failure in that later phase can then skip all KG work on restart.
    graph_index.save_graph()

    return graph_index
    # graph_index.save_graph()


if __name__ == "__main__":
    # We test the knowledge graph builder here
    from bookrag_core.configs.system_config import load_system_config

    cfg = load_system_config("/home/wangshu/multimodal/GBC-RAG/config/default.yaml")

    tree_index = DocumentTree.load_from_file(DocumentTree.get_save_path(cfg.save_path))

    token_tracker = TokenTracker.get_instance()
    token_tracker.reset()

    # Build the knowledge graph
    graph_index = build_knowledge_graph(tree_index, cfg)
    graph_index.save_graph()
    print("Knowledge graph built successfully.")
