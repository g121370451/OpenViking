# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""Relation filtering utilities for the bot layer.

Moved from openviking.storage.viking_fs to decouple filtering logic from storage.
"""

from typing import Any, Dict, List

_ENGLISH_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "it", "as", "be", "was", "were",
    "been", "are", "am", "do", "did", "does", "has", "had", "have", "will",
    "would", "could", "should", "may", "might", "shall", "can", "not", "no",
    "nor", "so", "if", "then", "than", "that", "this", "these", "those",
    "what", "which", "who", "whom", "how", "when", "where", "why",
    "all", "each", "every", "both", "few", "more", "most", "other", "some",
    "such", "only", "own", "same", "too", "very", "just", "about", "above",
    "after", "again", "also", "any", "because", "before", "below", "between",
    "during", "into", "its", "out", "over", "through", "under", "until",
    "up", "down", "here", "there", "once", "further", "her", "his", "she",
    "he", "him", "his", "her", "hers", "its", "they", "them", "their",
    "theirs", "our", "ours", "your", "yours", "we", "you", "me", "my",
    "myself", "yourself", "himself", "herself", "itself", "themselves",
    "ourselves", "yourselves", "being", "having", "doing",
})


def extract_keywords(text: str) -> List[str]:
    """Extract keywords from text via tokenize + stopword filtering."""
    if not text:
        return []
    tokens = text.lower().split()
    seen = set()
    result = []
    for t in tokens:
        t = t.strip(".,;:!?\"'()[]{}—–-")
        if len(t) <= 2 or t in _ENGLISH_STOPWORDS or t in seen:
            continue
        seen.add(t)
        result.append(t)
    return sorted(result)


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """Compute cosine similarity between two vectors. Returns 0.0 on error."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def filter_relations_by_keyword(
    entries: List[Dict[str, Any]],
    query: str,
) -> List[Dict[str, Any]]:
    """Filter relation entries by keyword overlap with query.

    Each entry is expected to have a "keywords" field (list of strings).
    Entries are scored by the number of overlapping keywords.
    """
    query_kw = set(extract_keywords(query))
    if not query_kw:
        return entries  # no keywords to filter by, return all
    scored = []
    for entry in entries:
        entry_kw = set(entry.get("keywords", []))
        if not entry_kw:
            # No stored keywords, keep entry with score 0
            scored.append(entry)
            continue
        overlap = query_kw & entry_kw
        if overlap:
            scored.append({**entry, "score": len(overlap)})
    scored.sort(key=lambda x: x.get("score", 0), reverse=True)
    return scored


def filter_relations_by_vector(
    entries: List[Dict[str, Any]],
    query_embedding: List[float],
    threshold: float = 0.7,
) -> List[Dict[str, Any]]:
    """Filter relation entries by cosine similarity with query embedding.

    Each entry is expected to have a "query_embedding" field (list of floats).
    Only entries with similarity > threshold are returned.
    """
    if not query_embedding:
        return entries
    scored = []
    for entry in entries:
        emb = entry.get("query_embedding")
        if not emb:
            continue
        score = cosine_similarity(query_embedding, emb)
        if score > threshold:
            scored.append({**entry, "score": round(score, 4)})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored
