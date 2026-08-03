# Copyright (C) 2025-2026 Shu Wang
# SPDX-License-Identifier: Apache-2.0 OR AGPL-3.0-only

from typing import Any, Union
from bookrag_core.configs.llm_config import LLMConfig
from bookrag_core.configs.vlm_config import VLMConfig
from bookrag_core.configs.rag_config import RAGConfig
from bookrag_core.rag.base_rag import BaseRAG

# Import the specific strategy config classes and the Union type
from bookrag_core.configs.rag import ALL_STRATEGY_CONFIGS
from bookrag_core.configs.rag.traverse_config import TraverseRAGConfig
from bookrag_core.configs.rag.mm_config import MMConfig 
from bookrag_core.configs.rag.gbc_config import GBCRAGConfig
from bookrag_core.configs.rag.graph_config import GraphRAGConfig
from bookrag_core.configs.rag.vanilla_config import VanillaConfig
from bookrag_core.configs.rag.gbc_vanilla_config import GBCVanillaConfig

from bookrag_core.provider.llm import LLM
from bookrag_core.provider.vlm import VLM

# Define the type for the strategy_config parameter
StrategyConfig = Union[ALL_STRATEGY_CONFIGS]


def create_rag_agent(
    # The first parameter is now the specific strategy config object
    strategy_config: StrategyConfig,
    llm_config: LLMConfig,
    vlm_config: VLMConfig,
    **dependencies: Any,
) -> BaseRAG:
    """
    Factory function to create a RAG agent based on the provided strategy configuration.
    """
    # Get the strategy name directly from the config object for logging
    strategy_name = strategy_config.strategy
    print(f"INFO: Creating RAG agent with strategy: '{strategy_name}'")

    # 1. Initialize common dependencies
    llm_client = LLM(llm_config)
    vlm_client = VLM(vlm_config)

    # 2. Use isinstance for type-safe dispatching
    if isinstance(strategy_config, TraverseRAGConfig):
        from bookrag_core.rag.traverse_agent import TraverseAgent

        tree_index = dependencies.get("tree_index")
        if not tree_index:
            raise ValueError("TraverseAgent requires a 'tree_index' in dependencies.")

        # 3. Pass the specific config object to the agent's constructor
        return TraverseAgent(
            config=strategy_config,
            llm=llm_client,
            vlm=vlm_client,
            tree_index=tree_index,
        )
    elif isinstance(strategy_config, GBCRAGConfig):
        from bookrag_core.rag.gbc_rag import GBCRAG

        # 4. For GBCRAG, we assume no additional dependencies are required
        gbc_index = dependencies.get("gbc_index")
        return GBCRAG(
            llm=llm_client,
            vlm=vlm_client,
            config=strategy_config,
            gbc_index=gbc_index,
        )
    elif isinstance(strategy_config, GraphRAGConfig):
        from bookrag_core.rag.graph_rag import GraphRAG

        gbc_index = dependencies.get("gbc_index")
        return GraphRAG(
            llm=llm_client,
            vlm=vlm_client,
            config=strategy_config,
            gbc_index=gbc_index,
        )
    elif isinstance(strategy_config, VanillaConfig):
        from bookrag_core.rag.vanilla_rag import VanillaRAG

        if strategy_config.retrieval_method == "bm25":
            bm25 = dependencies.get("bm25")
            return VanillaRAG(
                config=strategy_config,
                llm=llm_client,
                bm25=bm25,
                vector_store=None,
            )
        else:
            vector_store = dependencies.get("vector_store")
            return VanillaRAG(
                config=strategy_config,
                llm=llm_client,
                vector_store=vector_store,
                bm25=None,
            )
    elif isinstance(strategy_config, GBCVanillaConfig):
        from bookrag_core.rag.gbc_vanilla_rag import GBCVanillaRAG

        tree_vdb = dependencies.get("tree_vdb")
        graph_vdb = dependencies.get("graph_vdb")
        return GBCVanillaRAG(
            llm=llm_client,
            vlm=vlm_client,
            config=strategy_config,
            tree_vdb=tree_vdb,
            graph_vdb=graph_vdb,
        )
    elif isinstance(strategy_config, MMConfig):
        from bookrag_core.rag.mm_rag import MMRAG

        vector_store = dependencies.get("vector_store")
        if not vector_store:
            raise ValueError("MMRAG requires a 'vector_store' in dependencies.")

        return MMRAG(
            config=strategy_config,
            llm=llm_client,
            vlm=vlm_client,
            vector_store=vector_store,
            topk=strategy_config.topk if hasattr(strategy_config, 'topk') else 3,
        )

    else:
        raise NotImplementedError(
            f"RAG agent for strategy '{strategy_name}' is not implemented."
        )
