# Copyright (C) 2025-2026 Shu Wang
# SPDX-License-Identifier: Apache-2.0 OR AGPL-3.0-only

from dataclasses import dataclass, field

from bookrag_core.configs.embedding_config import EmbeddingConfig
from bookrag_core.configs.rerank_config import RerankerConfig


@dataclass
class GraphConfig:
    # KG extraction
    extractor_type: str = "llm"  # Options: "llm", "local"
    local_model_name: str = "en_core_web_sm"
    image_description_force: bool = False
    max_gleaning: int = 0

    # KG refinement
    refine_type: str = "advanced"  # Options: "basic", "advanced"
    g: float = 0.6 # For advanced refinement, the Gradient-based similar threshold
    checkpoint_every: int = 250

    embedding_config: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    reranker_config: RerankerConfig = field(default_factory=RerankerConfig)
