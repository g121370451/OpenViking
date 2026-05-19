import json
import os
import re
from typing import Dict, List

from src.core.vector_store import VikingStoreWrapper

# --- Relation matching utilities (inlined) ---

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


def _extract_keywords(text: str) -> set:
    """Extract keywords from text via tokenize + stopword filtering."""
    if not text:
        return set()
    tokens = text.lower().split()
    result = set()
    for t in tokens:
        t = t.strip(".,;:!?\"'()[]{}—–-")
        if len(t) <= 2 or t in _ENGLISH_STOPWORDS:
            continue
        result.add(t)
    return result


def _cosine_similarity(a: list, b: list) -> float:
    """Compute cosine similarity between two vectors. Returns 0.0 on error."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class VikingStoreWithRelations(VikingStoreWrapper):
    """Standard RAG vector store enhanced with .relations_{strategy}.jsonl edges.

    Inherits VikingStoreWrapper and overrides retrieve():
    1. Vector search (via parent class)
    2. For each result, query .relations_{strategy}.jsonl for related URIs (keyword + vector)
    3. Read and append related documents to context
    """

    def __init__(self, store_path: str, relations_topk: int = 0,
                 use_query_expansion: bool = False, llm=None, embedder=None,
                 strategy: str = "blind"):
        super().__init__(store_path)
        self.relations_topk = relations_topk
        self._vikingfs_path = os.path.join(store_path, "viking")
        self.use_query_expansion = use_query_expansion
        self._llm = llm
        self._embedder = embedder
        self._strategy = strategy
        self._relations_filename = ".relations.jsonl" if strategy == "blind" else f".relations_{strategy}.jsonl"
        self._embed_cache: dict[str, list] = {}
        self._ref_caches: dict[str, dict[str, dict]] = {}

    def _uri_to_parent_path(self, uri: str) -> str:
        """viking://resources/dataset/file.md -> {vikingfs_path}/resources/dataset"""
        if uri.startswith("viking://"):
            rel = uri[len("viking://"):]
        else:
            rel = uri
        local_path = os.path.join(self._vikingfs_path, rel)
        if os.path.isdir(local_path):
            return local_path
        return os.path.dirname(local_path)

    def _embed_text(self, text: str):
        """Generate embedding via embedder. Returns list of floats or None."""
        if not self._embedder or not text:
            return None
        if text in self._embed_cache:
            return self._embed_cache[text]
        try:
            result = self._embedder.embed(text)
            self._embed_cache[text] = result
            return result
        except Exception as e:
            print(f"[Warning] Embedding failed: {e}")
            return None

    def _load_ref_cache(self, parent_dir: str) -> dict[str, dict]:
        """Load .reference_questions.jsonl once per parent_dir, cache in memory."""
        if parent_dir in self._ref_caches:
            return self._ref_caches[parent_dir]
        cache: dict[str, dict] = {}
        ref_path = os.path.join(parent_dir, ".reference_questions.jsonl")
        if os.path.exists(ref_path):
            try:
                with open(ref_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                            qid = rec.get("id", "")
                            if qid:
                                cache[qid] = {"question": rec.get("question", ""), "embedding": rec.get("embedding")}
                        except json.JSONDecodeError:
                            continue
            except Exception:
                pass
        self._ref_caches[parent_dir] = cache
        return cache

    def _resolve_ref_question(self, parent_dir: str, question_id: str) -> dict | None:
        """Look up question_id from cached .reference_questions.jsonl."""
        cache = self._load_ref_cache(parent_dir)
        return cache.get(question_id)

    def _query_relations(self, uri: str, query: str) -> List[str]:
        """Read .relations_{strategy}.jsonl from uri's parent dir, keyword + vector dual matching."""
        parent_dir = self._uri_to_parent_path(uri)
        jsonl_path = os.path.join(parent_dir, self._relations_filename)
        if not os.path.exists(jsonl_path):
            return []

        # Pre-compute query embedding and keywords
        query_embedding = None
        query_keywords = set()
        if query:
            query_keywords = _extract_keywords(query)
            query_embedding = self._embed_text(query)

        # Cache for ReferenceStore lookups
        ref_cache: dict[str, dict | None] = {}

        results = []  # list of (uri, weight)
        seen = set()
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue

                uri1 = rec.get("uri1", "")
                uri2 = rec.get("uri2", "")
                if uri1 == uri2:
                    continue

                if uri1 != uri and uri2 != uri:
                    continue

                target = uri2 if uri1 == uri else uri1
                if target in seen:
                    continue

                rec_weight = rec.get("weight", 1.0)

                # Resolve question text and embedding from record
                rec_query = ""
                rec_embedding = None
                if "question_id" in rec:
                    qid = rec["question_id"]
                    if qid not in ref_cache:
                        ref_cache[qid] = self._resolve_ref_question(parent_dir, qid)
                    ref = ref_cache[qid]
                    if ref:
                        rec_query = ref.get("question", "")
                        rec_embedding = ref.get("embedding")
                else:
                    rec_query = rec.get("query_question", "")
                    rec_embedding = rec.get("query_embedding")

                # Bot self-link (empty question_id) — always accepted
                qid = rec.get("question_id", "")
                if qid == "" and "question_id" in rec:
                    seen.add(target)
                    results.append((target, rec_weight))
                    continue

                # No query context → accept all edges
                if not query:
                    seen.add(target)
                    results.append((target, rec_weight))
                    continue

                # Path 1: Keyword matching (coverage check)
                kw_matched = False
                if query_keywords and rec_query:
                    rec_keywords = _extract_keywords(rec_query)
                    if rec_keywords:
                        overlap = len(query_keywords & rec_keywords)
                        if overlap / len(query_keywords) > 0.7:
                            kw_matched = True

                # Path 2: Vector matching (always executes independently)
                vec_matched = False
                if query_embedding and rec_embedding:
                    sim = _cosine_similarity(query_embedding, rec_embedding)
                    if sim > 0.7:
                        vec_matched = True

                if kw_matched or vec_matched:
                    seen.add(target)
                    results.append((target, rec_weight))

        # Sort by weight descending
        results.sort(key=lambda x: x[1], reverse=True)
        return [uri for uri, _ in results]

    def _generate_search_queries(self, original_query: str) -> List[str]:
        """Use LLM to reformulate the question into multiple search queries."""
        system_prompt = (
            "You are an expert in generating search queries. Based on the user's original query, "
            "split it into 3–5 different query phrases to retrieve relevant documents. "
            "The generated queries should cover the intent of the original query from different perspectives, "
            "including synonyms, related concepts, various expressions, etc., and be as concise as possible. "
            "Please return only a JSON array with no extra content. Example:\n"
            '["Query 1", "Query 2", "Query 3"]'
        )
        messages = f"{system_prompt}\n\nquery: {original_query}\n"
        try:
            response = self._llm.generate(messages)
            content = response.strip()
            try:
                queries = json.loads(content)
            except json.JSONDecodeError:
                match = re.search(r'\[.*\]', content, re.DOTALL)
                queries = json.loads(match.group()) if match else None

            if isinstance(queries, list) and all(isinstance(q, str) for q in queries):
                queries = list(dict.fromkeys([original_query] + queries))  # dedup, keep order
                return queries[:5]
        except Exception as e:
            print(f"[Warning] Failed to generate search queries, using original: {e}")
        return [original_query]

    def retrieve(self, query: str, topk: int, target_uri: str = "viking://resources") -> Dict:
        """Retrieve with relations enhancement.

        Args:
            query: The original question. Used for .relations.jsonl exact matching.
                If use_query_expansion is enabled, LLM-generated variants are used for vector search.
            topk: Number of vector search results.
            target_uri: Search scope URI.

        Returns same dict as VikingStoreWrapper.retrieve(), plus:
          - relations_uris: [uri, ...] URIs added via relations (not from vector search)
          - relations_found: int total relations discovered
          - relations_added: int relations successfully read and added to context
          - expanded_queries: [str, ...] (only when use_query_expansion=True)

        retrieved_uris stays as vector-search-only URIs.
        recall_texts and context_blocks include both vector + relations results.
        """
        # Step 0: query expansion (if enabled)
        expanded_queries = None
        if self.use_query_expansion and self._llm:
            expanded_queries = self._generate_search_queries(query)
            vector_query = " ".join(expanded_queries)
        else:
            vector_query = query

        # Step 1: vector search (uses expanded query if available)
        ret = super().retrieve(vector_query, topk, target_uri)

        # Save vector-only URIs
        vector_uris = list(ret["retrieved_uris"])
        vector_uris_set = set(vector_uris)

        # Step 2: query relations for each vector result with BFS multi-hop
        related_uris = []
        related_uris_set = set()
        frontier = list(vector_uris)
        seen_uris = set(vector_uris)

        while frontier:
            next_frontier = []
            for uri in frontier:
                try:
                    rels = self._query_relations(uri, query)
                    for r in rels:
                        if r not in seen_uris and r not in related_uris_set:
                            related_uris.append((r, uri))
                            related_uris_set.add(r)
                            next_frontier.append(r)
                except Exception:
                    continue
            seen_uris.update(next_frontier)
            frontier = next_frontier

        # Step 3: read related docs and prepend to context (priority position)
        relations_uris = []
        relations_blocks = []
        for rel_uri, source_uri in related_uris:
            try:
                content = self.read_resource(rel_uri)
                if not content:
                    continue
                ret["recall_texts"][rel_uri] = content
                relations_blocks.append(content[:8000])
                relations_uris.append(rel_uri)
            except Exception:
                continue
        ret["context_blocks"] = relations_blocks + ret["context_blocks"]

        # Keep retrieved_uris as vector-only
        ret["relations_uris"] = relations_uris
        ret["relations_found"] = len(related_uris)
        ret["relations_added"] = len(relations_uris)

        if expanded_queries is not None:
            ret["expanded_queries"] = expanded_queries

        return ret
