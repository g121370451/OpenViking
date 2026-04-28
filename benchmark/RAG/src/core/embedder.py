"""Lightweight Volcengine embedding wrapper for benchmark."""

from typing import List, Optional


class VolcengineEmbedder:
    """Volcengine embedding client using volcenginesdkarkruntime."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://ark.cn-beijing.volces.com/api/v3",
        model: str = "doubao-embedding-vision-250615",
    ):
        from volcenginesdkarkruntime import Ark

        self.client = Ark(api_key=api_key, base_url=base_url)
        self.model = model

    def embed(self, text: str) -> List[float]:
        """Generate embedding vector for a single text string."""
        resp = self.client.multimodal_embeddings.create(
            input=[{"type": "text", "text": text}], model=self.model
        )
        return resp.data.embedding
