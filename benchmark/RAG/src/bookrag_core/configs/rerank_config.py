# Copyright (C) 2025-2026 Shu Wang
# SPDX-License-Identifier: Apache-2.0 OR AGPL-3.0-only

from dataclasses import dataclass


@dataclass
class RerankerConfig:
    model_name: str = "Qwen/Qwen3-Reranker-0.6B"
    max_length: int = 8192
    device: str = "cuda:2"
    backend: str = "local"  # Options: 'local', 'vllm', 'vikingdb'
    api_base: str = "http://localhost:8011/v1"
    ak: str = ""
    sk: str = ""
    host: str = "api-vikingdb.vikingdb.cn-beijing.volces.com"
    model_version: str = "251028"
    threshold: float = 0.1
    batch_size: int = 100

    def __post_init__(self):
        if self.backend not in ["local", "vllm", "vikingdb"]:
            raise ValueError(f"Unsupported reranker backend: {self.backend}")
        if not 0.0 <= self.threshold <= 1.0:
            raise ValueError("Reranker threshold must be between 0 and 1")
        max_batch_size = 100 if self.backend == "vikingdb" else 200
        if self.batch_size < 1 or self.batch_size > max_batch_size:
            raise ValueError(
                f"Reranker batch_size must be between 1 and {max_batch_size} "
                f"for backend '{self.backend}'"
            )
