# Copyright (C) 2025-2026 Shu Wang
# SPDX-License-Identifier: Apache-2.0 OR AGPL-3.0-only

import json
import os

from bookrag_core.Index.Tree import *
from bookrag_core.configs.system_config import SystemConfig
from bookrag_core.provider.llm import LLM
from bookrag_core.Index.Graph import Graph
from bookrag_core.provider.embedding import TextEmbeddingProvider
from bookrag_core.provider.vdb import VectorStore
from bookrag_core.checkpoint import atomic_write_json, canonical_sha256, file_sha256


def get_graph_variant(config: SystemConfig) -> str | None:
    """Return the graph persistence variant used by KG construction."""
    if config.graph.refine_type == "basic":
        return "basic"

    reranker_model_name = config.graph.reranker_config.model_name
    if reranker_model_name.lower().startswith("bge"):
        safe_model_name = reranker_model_name.replace("/", "_").replace(" ", "_")
        return f"{config.graph.g}_{safe_model_name}"

    embedding_model_name = config.graph.embedding_config.model_name
    is_default_g = abs(config.graph.g - 0.6) < 1e-9
    is_default_qwen_model = embedding_model_name.lower().startswith("qwen")
    if is_default_g and is_default_qwen_model:
        return None

    safe_model_name = embedding_model_name.replace("/", "_").replace(" ", "_")
    return f"{config.graph.g}_{safe_model_name}"


class GBC:
    """
    A class representing the index combining graph and tree structures.
    This class allows for the creation and management of a tree index, which can be used
    to organize and retrieve information in multimodal applications.
    """

    def __init__(
        self,
        config: SystemConfig,
        graph_index: Optional[Graph] = None,
        TreeIndex: Optional[DocumentTree] = None,
    ):
        """
        Initializes the TreeIndex with an optional index.

        :param index: Optional initial index for the tree.
        """
        self.save_dir = config.save_path
        self.config = config
        self.llm = LLM(config.llm)
        self.TreeIndex: DocumentTree = TreeIndex
        self.GraphIndex: Graph = graph_index

        # load the vdb of entities
        if config.graph.refine_type == "basic":
            self.entity_vdb_path = os.path.join(self.save_dir, "kg_vdb_basic")
        else:
            self.entity_vdb_path = os.path.join(self.save_dir, "kg_vdb")
        
        self.embedder = TextEmbeddingProvider.from_config(
            config.graph.embedding_config
        )
        self.entity_vdb: VectorStore = VectorStore(
            db_path=self.entity_vdb_path,
            embedding_model=self.embedder,
            collection_name="kg_collection",
        )
        log.info(f"Entity VDB loaded from {self.entity_vdb_path}")

    def save_gbc_index(self):
        """
        Saves the GBC index to the specified path.

        :param save_path: The path where the index will be saved.
        """
        if self.TreeIndex:
            self.TreeIndex.save_to_file()
        if self.GraphIndex:
            self.GraphIndex.save_graph()

        # vdb is saved automatically when the entity_vdb is created

        log.info(f"GBC index saved")

    def rebuild_vdb(self):
        """
        Rebuilds the vector database for entities using the current graph index.
        """
        if not self.GraphIndex:
            raise ValueError("GraphIndex is not set. Cannot rebuild VDB.")

        nodes = self.GraphIndex.get_all_nodes()
        if self._entity_vdb_is_complete(nodes):
            log.info(
                "[Resume] Final entity VDB is complete: %d/%d. Skipping rebuild.",
                self.entity_vdb.collection.count(),
                len(nodes),
            )
            return

        self.entity_vdb.reset()

        texts = []
        meta_datas = []

        for node in nodes:
            texts.append(node)

            entity = self.GraphIndex.get_entity_by_node_name(node)
            tmp_dict = {
                "entity_name": entity.entity_name,
                "entity_type": entity.entity_type,
                "description": entity.description,
            }
            meta_datas.append(tmp_dict)

        self.entity_vdb.add_texts(texts=texts, metadatas=meta_datas)
        atomic_write_json(
            self._entity_vdb_manifest_path(),
            {
                "format_version": 1,
                "status": "complete",
                "graph_sha256": self._graph_sha256(),
                "embedding_fingerprint": self._embedding_fingerprint(),
                "entity_count": len(texts),
            },
        )
        log.info(f"Rebuilt entity VDB with {len(texts)} entries.")

    def _entity_vdb_manifest_path(self) -> str:
        basename = os.path.basename(self.entity_vdb_path.rstrip(os.sep))
        return os.path.join(self.save_dir, f"{basename}_manifest.json")

    def _graph_sha256(self) -> str:
        graph_path = os.path.join(self.save_dir, self.GraphIndex.data_filename)
        if not os.path.isfile(graph_path):
            return ""
        return file_sha256(graph_path)

    def _embedding_fingerprint(self) -> str:
        config = self.config.graph.embedding_config
        return canonical_sha256(
            {
                "backend": config.backend,
                "model": config.model_name,
                "max_length": config.max_length,
            }
        )

    def _entity_vdb_is_complete(self, graph_nodes) -> bool:
        manifest_path = self._entity_vdb_manifest_path()
        if not os.path.isfile(manifest_path):
            return False
        try:
            with open(manifest_path, "r", encoding="utf-8") as file:
                manifest = json.load(file)
            if (
                manifest.get("status") != "complete"
                or manifest.get("graph_sha256") != self._graph_sha256()
                or manifest.get("embedding_fingerprint")
                != self._embedding_fingerprint()
                or int(manifest.get("entity_count", -1)) != len(graph_nodes)
                or self.entity_vdb.collection.count() != len(graph_nodes)
            ):
                return False
            if not graph_nodes:
                return True
            stored = self.entity_vdb.collection.get(include=["documents"])
            return set(stored.get("documents") or []) == set(graph_nodes)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return False

    @classmethod
    def load_gbc_index(cls, config: SystemConfig):
        """
        Loads the GBC index from the specified path.

        :param config: The configuration object containing the save path.
        :return: An instance of GBC with the loaded index.
        """
        tree_index = DocumentTree.load_from_file(
            DocumentTree.get_save_path(config.save_path)
        )
        
        variant = get_graph_variant(config)
        graph_index = Graph.load_from_dir(config.save_path, variant=variant)
        GBC = cls(config=config, graph_index=graph_index, TreeIndex=tree_index)
        log.info(f"GBC index loaded from {config.save_path}")
        return GBC
