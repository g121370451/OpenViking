import json
import os
import re
from typing import Dict, List

from src.core.vector_store import VikingStoreWrapper


class VikingStoreWithRelations(VikingStoreWrapper):
    """Standard RAG vector store enhanced with .relations.jsonl edges.

    Inherits VikingStoreWrapper and overrides retrieve():
    1. Vector search (via parent class)
    2. For each result, query .relations.jsonl for related URIs
    3. Read and append related documents to context
    """

    def __init__(self, store_path: str, relations_topk: int = 0,
                 use_query_expansion: bool = False, llm=None):
        super().__init__(store_path)
        # 0 = unlimited, >0 = max additional docs from relations
        self.relations_topk = relations_topk
        # AGFS localfs maps viking:// to {store_path}/viking/
        self._vikingfs_path = os.path.join(store_path, "viking")
        # Query expansion: LLM reformulates original question into multiple queries
        self.use_query_expansion = use_query_expansion
        self._llm = llm  # LLMClientWrapper instance, required if use_query_expansion

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

    def _query_relations(self, uri: str, query: str) -> List[str]:
        """Read .relations.jsonl from uri's parent dir, bidirectional match."""
        parent_dir = self._uri_to_parent_path(uri)
        jsonl_path = os.path.join(parent_dir, ".relations.jsonl")
        if not os.path.exists(jsonl_path):
            return []

        results = []
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

                rec_query = rec.get("query_question", "")
                if query and rec_query != query:
                    continue

                uri1 = rec.get("uri1", "")
                uri2 = rec.get("uri2", "")
                if uri1 == uri2:
                    continue

                if uri1 == uri:
                    target = uri2
                elif uri2 == uri:
                    target = uri1
                else:
                    continue

                if target not in seen:
                    seen.add(target)
                    results.append(target)

        return results

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

        # Step 2: query relations for each vector result (always uses original query)
        related_uris = []
        for uri in vector_uris:
            try:
                rels = self._query_relations(uri, query)
                for r in rels:
                    if r not in vector_uris_set and r not in {x[0] for x in related_uris}:
                        related_uris.append((r, uri))
            except Exception:
                continue

        # Step 3: apply relations_topk limit
        if self.relations_topk > 0:
            related_uris = related_uris[:self.relations_topk]

        # Step 4: read and append related docs
        relations_uris = []
        relations_added = 0
        for rel_uri, source_uri in related_uris:
            try:
                content = self.read_resource(rel_uri)
                if not content:
                    continue
                ret["recall_texts"][rel_uri] = content
                ret["context_blocks"].append(content[:8000])
                relations_uris.append(rel_uri)
                relations_added += 1
            except Exception as e:
                continue

        # Keep retrieved_uris as vector-only
        ret["relations_uris"] = relations_uris
        ret["relations_found"] = len(related_uris)
        ret["relations_added"] = relations_added

        if expanded_queries is not None:
            ret["expanded_queries"] = expanded_queries

        return ret
