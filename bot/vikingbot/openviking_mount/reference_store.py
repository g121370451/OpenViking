"""
ReferenceStore: 管理 .reference_questions.jsonl

同一问题只存一份（通过 sha256 做 question_id），
relation 记录通过 question_id 引用而非存储完整文本。
"""

import os
import json
import hashlib
import logging

logger = logging.getLogger(__name__)


class ReferenceStore:
    """管理 .reference_questions.jsonl 的读写。

    每个文档目录下共享一份 .reference_questions.jsonl，
    存储 question_id → {question, embedding} 映射。
    """

    def __init__(self, parent_dir: str):
        self.path = os.path.join(parent_dir, ".reference_questions.jsonl")
        self._cache: dict[str, dict] | None = None  # id → {question, embedding}

    @staticmethod
    def compute_id(question: str) -> str:
        """通过 sha256 生成确定性的 question_id。"""
        return hashlib.sha256(question.lower().strip().encode()).hexdigest()[:16]

    def _load(self) -> dict[str, dict]:
        """加载全部记录到内存缓存。"""
        if self._cache is not None:
            return self._cache
        self._cache = {}
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        qid = rec.get("id", "")
                        if qid:
                            self._cache[qid] = {
                                "question": rec.get("question", ""),
                                "embedding": rec.get("embedding"),
                            }
                    except json.JSONDecodeError:
                        continue
        return self._cache

    def get_or_create(self, question: str, embedder=None) -> str:
        """返回 question_id。已存在则返回已有 ID，否则写入新记录。

        Args:
            question: 原始问题文本
            embedder: Optional[_Embedder]，用于生成 question embedding

        Returns:
            question_id (16 字符 hex)
        """
        qid = self.compute_id(question)
        cache = self._load()

        if qid in cache:
            return qid

        # Generate embedding
        embedding = None
        if embedder:
            try:
                embedding = embedder.embed(question)
            except Exception as e:
                logger.warning(f"[ReferenceStore] Embedding failed for question_id={qid}: {e}")

        record = {"id": qid, "question": question}
        if embedding:
            record["embedding"] = embedding

        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        self._cache[qid] = {"question": question, "embedding": embedding}
        logger.debug(f"[ReferenceStore] Created question_id={qid} for: {question[:80]}")
        return qid

    def get(self, question_id: str) -> dict | None:
        """通过 ID 查询 {question, embedding}。"""
        cache = self._load()
        return cache.get(question_id)

    def invalidate(self):
        """清除内存缓存（用于外部修改后重载）。"""
        self._cache = None
