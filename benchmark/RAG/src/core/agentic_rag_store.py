import os
import time
from typing import List, Dict
from collections import OrderedDict

from langchain_core.messages import SystemMessage, HumanMessage, ToolMessage

from core.vector_store import VikingStoreWrapper
from core.llm_client import LLMClientWrapper
from core.logger import get_logger

logger = get_logger()

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOL_SEARCH = {
    "type": "function",
    "function": {
        "name": "search",
        "description": "Semantic search across the OpenViking knowledge base. Returns ranked URIs with abstracts.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
                "target_uri": {"type": "string", "description": "URI prefix scope (default: viking://resources)", "default": "viking://resources"},
                "limit": {"type": "integer", "description": "Max results (default: 5)", "default": 5},
            },
            "required": ["query"],
        },
    },
}

TOOL_READ = {
    "type": "function",
    "function": {
        "name": "read",
        "description": "Read full content of one or more OpenViking resources by URI.",
        "parameters": {
            "type": "object",
            "properties": {
                "uris": {"type": "array", "items": {"type": "string"}, "description": "List of viking:// URIs to read"},
            },
            "required": ["uris"],
        },
    },
}

TOOL_LIST = {
    "type": "function",
    "function": {
        "name": "list_resources",
        "description": "List resources under a given URI path.",
        "parameters": {
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "The URI path to list"},
                "recursive": {"type": "boolean", "description": "List recursively (default: false)", "default": False},
            },
            "required": ["uri"],
        },
    },
}

TOOL_GREP = {
    "type": "function",
    "function": {
        "name": "grep",
        "description": "Regex search across Viking resources. Useful for exact matching of names, dates, IDs.",
        "parameters": {
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "Viking URI to search within"},
                "pattern": {"type": "string", "description": "Regex pattern"},
                "case_insensitive": {"type": "boolean", "description": "Case-insensitive (default: false)", "default": False},
            },
            "required": ["uri", "pattern"],
        },
    },
}

TOOL_GLOB = {
    "type": "function",
    "function": {
        "name": "glob",
        "description": "Find Viking resources matching a glob pattern (e.g. **/*.md).",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern to match"},
                "uri": {"type": "string", "description": "URI to search within (default: viking://resources)", "default": "viking://resources"},
            },
            "required": ["pattern"],
        },
    },
}

TOOL_SUBMIT = {
    "type": "function",
    "function": {
        "name": "submit",
        "description": "Submit URIs as final relevant retrieval results. Only submit documents you have read and judged relevant.",
        "parameters": {
            "type": "object",
            "properties": {
                "uris": {"type": "array", "items": {"type": "string"}, "description": "URIs to include in final results"},
            },
            "required": ["uris"],
        },
    },
}

TOOL_LINK = {
    "type": "function",
    "function": {
        "name": "link",
        "description": "Create a reference link between Viking resources. Use when you discover documents that are related (same topic, person, event, or continuation). Links persist and build a knowledge graph for future queries.",
        "parameters": {
            "type": "object",
            "properties": {
                "from_uri": {"type": "string", "description": "Source URI to link FROM"},
                "uris": {"type": "array", "items": {"type": "string"}, "description": "Target URIs to link TO"},
                "reason": {"type": "string", "description": "Brief explanation of why these documents are related"},
            },
            "required": ["from_uri", "uris", "reason"],
        },
    },
}

TOOL_RELATIONS = {
    "type": "function",
    "function": {
        "name": "relations",
        "description": "Query existing reference links for a resource. Returns linked URIs and reasons. Use to discover related documents and follow graph edges during search.",
        "parameters": {
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "The URI to query relations for"},
            },
            "required": ["uri"],
        },
    },
}

ALL_TOOLS = [TOOL_SEARCH, TOOL_READ, TOOL_LIST, TOOL_GREP, TOOL_GLOB, TOOL_SUBMIT, TOOL_LINK, TOOL_RELATIONS]

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

RETRIEVAL_AGENT_SYSTEM_PROMPT = """You are a retrieval assistant. Your ONLY job is to find relevant document chunks for a given question. You must NOT generate an answer.

Workflow:
1. `search` with a semantic query derived from the question.
2. Review returned URIs and abstracts for relevance.
3. `read` promising resources to see full content.
4. If relevant, `submit` the URI. Only submitted documents count as results.
5. If results are insufficient, refine keywords, use `grep` for exact terms, or `glob` for file discovery.
6. Use `relations` on promising URIs to discover already-linked documents and follow graph edges as search leads.
7. When you find documents that are clearly related (same topic, same person, same event, continuation), use `link` to connect them with a brief reason. This builds a knowledge graph for future queries.
8. Stop when you have submitted ~{topk} relevant chunks or further search yields diminishing returns.

Rules:
- ALWAYS search in viking://resources/ path.
- Do NOT answer the question. Only the documents you `submit` matter.
- You MUST `submit` every document you want in the final results. Reading alone does NOT submit.
- Only submit documents you have read and judged relevant.
- Use `link` to connect documents that share meaningful relationships. Always provide a reason.
- Use `relations` to check existing links before creating duplicates.
- When done, say "Retrieval complete."
"""

# ---------------------------------------------------------------------------
# AgenticVikingStore
# ---------------------------------------------------------------------------

class AgenticVikingStore:
    """VikingStoreWrapper-compatible store whose retrieve() runs an LLM-driven
    ReAct agent loop. The agent searches, reads, evaluates, and explicitly
    submits relevant URIs. retrieve() returns the same dict shape as
    VikingStoreWrapper so the pipeline is store-agnostic.
    """

    def __init__(self, store_path: str, agentic_config: dict, llm_config: dict = None):
        self._inner_store = VikingStoreWrapper(store_path=store_path)
        self._read_cache: OrderedDict[str, str] = OrderedDict()
        self._submitted: List[Dict] = []  # [{uri, overlap, matched_tokens}, ...]
        self._links_created: List[Dict] = []  # [{from_uri, to_uris, reason}, ...]
        self._current_query: str = ""

        agent_llm_cfg = llm_config or agentic_config
        api_key = os.environ.get(
            agent_llm_cfg.get('api_key_env_var', ''),
            agent_llm_cfg.get('api_key', '')
        )
        self._agent_llm = LLMClientWrapper(config=agent_llm_cfg, api_key=api_key)
        self._max_iterations = int(agentic_config.get('max_iterations', 10))

    # -- Delegated methods -----------------------------------------------------

    def ingest(self, samples, max_workers=10, monitor=None, ingest_mode="per_file") -> dict:
        return self._inner_store.ingest(samples, max_workers, monitor, ingest_mode)

    def read_resource(self, uri: str) -> str:
        if uri in self._read_cache:
            return self._read_cache[uri]
        content = self._inner_store.read_resource(uri)
        self._read_cache[uri] = content
        return content

    def clear(self):
        self._read_cache.clear()
        self._submitted.clear()
        self._inner_store.clear()

    def count_tokens(self, text: str) -> int:
        return self._inner_store.count_tokens(text)

    # -- Tool execution --------------------------------------------------------

    def _exec_search(self, query: str, target_uri: str = "viking://resources", limit: int = 5) -> str:
        try:
            search_res = self._inner_store.client.find(query=query, limit=limit, target_uri=target_uri, telemetry=False)
            resources = getattr(search_res, 'resources', []) or []
            if not resources:
                return "No results found."
            lines = []
            for r in resources:
                level_tag = f"L{getattr(r, 'level', '?')}"
                score = f"{getattr(r, 'score', 0):.4f}"
                abstract = (getattr(r, 'abstract', '') or '')[:200]
                lines.append(f"- [{level_tag} score={score}] {r.uri}\n  {abstract}")
            return "\n".join(lines)
        except Exception as e:
            return f"Search error: {e}"

    def _exec_read(self, uris: List[str]) -> str:
        results = []
        for uri in uris:
            try:
                content = self.read_resource(uri)
                results.append(f"=== {uri} ===\n{content}")
            except Exception as e:
                results.append(f"=== {uri} ===\nRead error: {e}")
        return "\n\n".join(results)

    def _exec_list(self, uri: str, recursive: bool = False) -> str:
        try:
            items = self._inner_store.client.ls(uri, recursive=recursive)
            if not items:
                return f"No resources found under {uri}"
            lines = []
            for item in items:
                name = item.get('name', item.get('uri', str(item))) if isinstance(item, dict) else str(item)
                lines.append(f"- {name}")
            return "\n".join(lines[:50])
        except Exception as e:
            return f"List error: {e}"
    def _exec_grep(self, uri: str, pattern: str, case_insensitive: bool = False) -> str:
        try:
            result = self._inner_store.client.grep(uri, pattern, case_insensitive=case_insensitive)
            matches = result.get('matches', []) if isinstance(result, dict) else getattr(result, 'matches', [])
            if not matches:
                return f"No matches found for pattern '{pattern}' in {uri}"
            lines = []
            for m in matches[:30]:
                if isinstance(m, dict):
                    lines.append(f"- {m.get('uri', m.get('file', ''))}:{m.get('line_number', m.get('line', ''))}  {m.get('content', m.get('text', ''))[:200]}")
                else:
                    lines.append(f"- {str(m)[:200]}")
            return f"Found {len(matches)} match(es):\n" + "\n".join(lines)
        except Exception as e:
            return f"Grep error: {e}"

    def _exec_glob(self, pattern: str, uri: str = "viking://resources") -> str:
        try:
            result = self._inner_store.client.glob(pattern, uri=uri)
            matches = result.get('matches', []) if isinstance(result, dict) else getattr(result, 'matches', [])
            count = result.get('count', len(matches)) if isinstance(result, dict) else getattr(result, 'count', len(matches))
            if not matches:
                return f"No files matching '{pattern}'"
            lines = []
            for m in matches[:50]:
                if isinstance(m, dict):
                    lines.append(f"- {m.get('uri', m.get('path', str(m)))}")
                elif isinstance(m, str):
                    lines.append(f"- {m}")
                else:
                    lines.append(f"- {getattr(m, 'uri', str(m))}")
            return f"Found {count} file(s):\n" + "\n".join(lines)
        except Exception as e:
            return f"Glob error: {e}"

    def _exec_submit(self, uris: List[str]) -> str:
        submitted_uris = {s["uri"] for s in self._submitted}
        added, skipped_dup, skipped_irrelevant = [], [], []
        for uri in uris:
            if uri in submitted_uris:
                skipped_dup.append(uri)
                continue
            # Auto-read if not yet read
            if uri not in self._read_cache:
                try:
                    self.read_resource(uri)
                except Exception as e:
                    skipped_irrelevant.append((uri, f"read failed: {e}", {}))
                    continue
            # Keyword overlap validation
            content = self._read_cache.get(uri, "")
            passed, overlap_info = self._check_relevance(content)
            if not passed:
                skipped_irrelevant.append((uri, "low keyword overlap", overlap_info))
                continue
            self._submitted.append({
                "uri": uri,
                "overlap": overlap_info["overlap"],
                "matched_tokens": overlap_info["matched_tokens"],
            })
            submitted_uris.add(uri)
            added.append((uri, overlap_info))
        # Build response with full submitted list
        parts = []
        if added:
            details = [f"{u} (overlap={info['overlap']:.0%}, hits={info['matched_tokens']})" for u, info in added]
            parts.append(f"Submitted {len(added)}: {'; '.join(details)}")
        if skipped_irrelevant:
            details = [f"{u} ({r}, overlap={info.get('overlap', 0):.0%})" for u, r, info in skipped_irrelevant]
            parts.append(f"Rejected {len(skipped_irrelevant)}: {'; '.join(details)}")
        if skipped_dup:
            parts.append(f"Skipped {len(skipped_dup)} already submitted.")
        # Show all submitted with scores
        all_submitted = sorted(self._submitted, key=lambda s: s["overlap"], reverse=True)
        ranking = [f"{s['uri']} ({s['overlap']:.0%})" for s in all_submitted]
        parts.append(f"Total submitted ({len(self._submitted)}): {'; '.join(ranking)}")
        return " | ".join(parts)

    def _check_relevance(self, content: str, threshold: float = 0.15) -> tuple:
        """Check if content is relevant to the current query via keyword token overlap.

        Returns:
            (passed: bool, info: dict) where info contains:
              - query_tokens: list of tokens checked
              - matched_tokens: list of tokens found in content
              - missed_tokens: list of tokens not found
              - overlap: float ratio (0.0 ~ 1.0)
              - threshold: the threshold used
        """
        if not self._current_query or not content:
            return True, {"query_tokens": [], "matched_tokens": [], "missed_tokens": [], "overlap": 1.0, "threshold": threshold}
        query_tokens = set(self._current_query.lower().split())
        query_tokens = {t for t in query_tokens if len(t) > 2}
        if not query_tokens:
            return True, {"query_tokens": [], "matched_tokens": [], "missed_tokens": [], "overlap": 1.0, "threshold": threshold}
        content_lower = content.lower()
        matched = [t for t in query_tokens if t in content_lower]
        missed = [t for t in query_tokens if t not in content_lower]
        overlap = len(matched) / len(query_tokens)
        info = {
            "query_tokens": sorted(query_tokens),
            "matched_tokens": sorted(matched),
            "missed_tokens": sorted(missed),
            "overlap": overlap,
            "threshold": threshold,
        }
        return overlap >= threshold, info

    def _dispatch_tool(self, name: str, args: dict) -> str:
        dispatch = {
            "search": lambda: self._exec_search(args.get("query", ""), args.get("target_uri", "viking://resources"), int(args.get("limit", 5))),
            "read": lambda: self._exec_read(args.get("uris", [])),
            "list_resources": lambda: self._exec_list(args.get("uri", "viking://resources"), args.get("recursive", False)),
            "grep": lambda: self._exec_grep(args.get("uri", "viking://resources"), args.get("pattern", ""), args.get("case_insensitive", False)),
            "glob": lambda: self._exec_glob(args.get("pattern", ""), args.get("uri", "viking://resources")),
            "submit": lambda: self._exec_submit(args.get("uris", [])),
            "link": lambda: self._exec_link(args.get("from_uri", ""), args.get("uris", []), args.get("reason", "")),
            "relations": lambda: self._exec_relations(args.get("uri", "")),
        }
        fn = dispatch.get(name)
        return fn() if fn else f"Unknown tool: {name}"

    def _exec_link(self, from_uri: str, uris: List[str], reason: str) -> str:
        if not from_uri or not uris:
            return "Error: from_uri and uris are required."
        if not reason:
            return "Error: reason is required for link creation."
        try:
            self._inner_store.client.link(from_uri, uris, reason=reason)
            self._links_created.append({
                "from_uri": from_uri,
                "to_uris": uris,
                "reason": reason,
            })
            targets = ", ".join(uris)
            return f"Linked: {from_uri} -> [{targets}] (reason: {reason})"
        except Exception as e:
            return f"Link error: {e}"

    def _exec_relations(self, uri: str) -> str:
        try:
            rels = self._inner_store.client.relations(uri)
            if not rels:
                return f"No relations found for {uri}"
            lines = []
            for r in rels:
                rel_uri = r.get("uri", "")
                reason = r.get("reason", "")
                lines.append(f"- {rel_uri} (reason: {reason})" if reason else f"- {rel_uri}")
            return f"Relations for {uri} ({len(rels)}):\n" + "\n".join(lines)
        except Exception as e:
            return f"Relations error: {e}"

    # -- Core: retrieve --------------------------------------------------------

    def retrieve(self, query: str, topk: int, target_uri: str = "viking://resources") -> Dict:
        """Run agentic ReAct loop, return same dict shape as VikingStoreWrapper.retrieve().

        Returns:
            Dict with keys:
              - recall_texts: {uri: full_content} for submitted (relevant) docs
              - context_blocks: [truncated_content, ...] for prompt building
              - retrieved_uris: [uri, ...]
              - retrieval_tokens: 0 (no embedding cost in agent mode)
              - agentic_metadata: {tool_calls, iterations_used}
        """
        self._read_cache.clear()
        self._submitted.clear()
        self._links_created.clear()
        self._current_query = query
        tool_call_trace: List[dict] = []

        system_prompt = RETRIEVAL_AGENT_SYSTEM_PROMPT.format(topk=topk)
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=f"Find relevant documents for this question:\n\n{query}"),
        ]

        iterations_used = 0
        for iteration in range(1, self._max_iterations + 1):
            iterations_used = iteration
            try:
                response = self._agent_llm.generate_with_tools(messages, ALL_TOOLS)
            except Exception as e:
                logger.error(f"[AgenticRAG] LLM call failed at iteration {iteration}: {e}")
                break

            tool_calls = getattr(response, 'tool_calls', None) or []
            if not tool_calls:
                logger.info(f"[AgenticRAG] Agent finished after {iteration} iteration(s)")
                break

            messages.append(response)
            for tc in tool_calls:
                tool_name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
                tool_args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
                tool_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")

                logger.debug(f"[AgenticRAG] iter={iteration} tool={tool_name} args={tool_args}")
                result_text = self._dispatch_tool(tool_name, tool_args)
                messages.append(ToolMessage(content=result_text, tool_call_id=tool_id))

                tool_call_trace.append({
                    "iteration": iteration,
                    "tool": tool_name,
                    "args": tool_args,
                    "result_preview": result_text,
                })
        else:
            logger.warning(f"[AgenticRAG] Reached max iterations ({self._max_iterations})")

        # Build result: sort by overlap score, take topk
        ranked = sorted(self._submitted, key=lambda s: s["overlap"], reverse=True)
        top = ranked[:topk]

        logger.info(f"[AgenticRAG] Total submitted: {len(self._submitted)}, topk={topk}, selected={len(top)}")
        for s in ranked:
            marker = ">>>" if s in top else "   "
            logger.info(f"[AgenticRAG] {marker} {s['uri']} overlap={s['overlap']:.0%} hits={s['matched_tokens']}")

        recall_texts = {}
        context_blocks = []
        retrieved_uris = []

        for s in top:
            uri = s["uri"]
            if uri.endswith(('.abstract.md', '.overview.md')):
                continue
            content = self.read_resource(uri)
            retrieved_uris.append(uri)
            recall_texts[uri] = content
            context_blocks.append(content[:8000])

        return {
            "recall_texts": recall_texts,
            "context_blocks": context_blocks,
            "retrieved_uris": retrieved_uris,
            "retrieval_tokens": 0,
            "agentic_metadata": {
                "tool_calls": tool_call_trace,
                "iterations_used": iterations_used,
                "links_created": self._links_created.copy(),
                "submitted_ranking": [
                    {"uri": s["uri"], "overlap": s["overlap"], "matched_tokens": s["matched_tokens"], "selected": s in top}
                    for s in ranked
                ],
            },
        }
