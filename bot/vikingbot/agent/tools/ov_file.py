"""OpenViking file system tools: read, write, list, search resources."""

import asyncio
import os
from abc import ABC
from pathlib import Path
from typing import Any, Optional, Union

import httpx
from loguru import logger

from vikingbot.agent.tools.base import Tool, ToolContext
from vikingbot.openviking_mount.ov_server import VikingClient


class OVFileTool(Tool, ABC):
    def __init__(self):
        super().__init__()
        self._client = None

    async def _get_client(self, tool_context: ToolContext):
        if self._client is None:
            self._client = await VikingClient.create(tool_context.workspace_id)
        return self._client

class VikingListTool(OVFileTool):
    """Tool to list Viking resources."""

    @property
    def name(self) -> str:
        return "openviking_list"

    @property
    def description(self) -> str:
        return "List resources in a OpenViking folder path."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "uri": {
                    "type": "string",
                    "description": "The parent Viking uri to list (e.g., viking://resources/)",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "Whether to list recursively",
                    "default": False,
                },
            },
            "required": ["uri"],
        }

    async def execute(
        self, tool_context: "ToolContext", uri: str, recursive: bool = False, **kwargs: Any
    ) -> str:
        try:
            client = await self._get_client(tool_context)
            entries = await client.list_resources(path=uri, recursive=recursive)

            if not entries:
                return f"No resources found at {uri}"

            result = []
            for entry in entries:
                item = {
                    "name": entry["name"],
                    "size": entry["size"],
                    "uri": entry["uri"],
                    "isDir": entry["isDir"],
                }
                result.append(str(item))
            return "\n".join(result)
        except Exception as e:
            logger.exception(f"Error processing message: {e}")
            return f"Error listing Viking resources: {str(e)}"


class VikingSearchTool(OVFileTool):
    """Tool to search Viking resources."""

    @property
    def name(self) -> str:
        return "openviking_search"

    @property
    def description(self) -> str:
        return ("Using query to search for resources (knowledge, code, files, workflow, etc.) in OpenViking. "
                "This operation performs semantic retrieval, not full character matching. Please avoid repeated calls with similar queries as much as possible."
                "bad-case: after searching with ‘Nate Joanna dog playdate 3:00 pm', another search was performed using 'Nate Joanna dog playdate'.")

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
                "target_uri": {
                    "type": "string",
                    "description": "Optional target URI to limit search scope, if is None, then search the entire range.(e.g., viking://resources/)",
                },
            },
            "required": ["query"],
        }

    async def execute(
        self,
        tool_context: "ToolContext",
        query: str,
        target_uri: Optional[str] = "",
        **kwargs: Any,
    ) -> str:
        try:
            client = await self._get_client(tool_context)
            search_client = getattr(client, 'admin_user_client', client)
            results = await search_client.search(query, target_uri=target_uri)

            if not results:
                return f"No results found for query: {query}"

            if isinstance(results, dict):
                resources_list = results.get('resources', []) or []
            elif isinstance(results, list):
                resources_list = results
            else:
                resources_list = []

            if not resources_list:
                return str(results)

            if not resources_list:
                return f"No results found for query: {query}"

            use_relations = os.environ.get("VIKINGBOT_USE_RELATIONS", "0") == "1"
            relations_found = 0
            if use_relations:
                link_strategy = os.environ.get("VIKINGBOT_LINK_STRATEGY", "llm_review")
                seen_uris = {r.get("uri", "") for r in resources_list}
                initial_count = len(resources_list)

                frontier = [r.get("uri", "") for r in resources_list if r.get("uri", "")]
                logger.error(
                    f"[Search] Relations expansion starting: "
                    f"frontier={len(frontier)} URIs, strategy={link_strategy}"
                )

                while frontier:
                    rel_tasks = [client.relations(uri, query=query, strategy=link_strategy) for uri in frontier]
                    rel_results = await asyncio.gather(*rel_tasks, return_exceptions=True)

                    next_frontier = []
                    for from_uri, rels in zip(frontier, rel_results):
                        if isinstance(rels, Exception):
                            continue
                        for rel in rels:
                            rel_uri = rel.get("uri", "")
                            if not rel_uri:
                                continue
                            if rel_uri in seen_uris:
                                for existing in resources_list:
                                    if existing.get("uri") == rel_uri and not existing.get("match_reason"):
                                        existing["match_reason"] = f"relation_from: {from_uri}"
                                        existing["relation_reason"] = rel.get("reason", "")
                                        relations_found += 1
                                        break
                                continue
                            seen_uris.add(rel_uri)
                            try:
                                abstract = await client.read_content(rel_uri, level="abstract")
                            except Exception:
                                abstract = ""
                            resources_list.append({
                                "uri": rel_uri,
                                "context_type": "ContextType.RESOURCE",
                                "is_leaf": False,
                                "abstract": abstract or "",
                                "overview": None,
                                "category": "",
                                "score": 0,
                                "match_reason": f"relation_from: {from_uri}",
                                "relation_reason": rel.get("reason", ""),
                                "relations": [],
                            })
                            relations_found += 1
                            next_frontier.append(rel_uri)

                    frontier = next_frontier

                logger.error(
                    f"[Search] Relations expansion done: "
                    f"{relations_found} related docs found, total results={len(resources_list)} (was {initial_count})"
                )

            if tool_context:
                tool_context.structured_result = resources_list

            if use_relations and relations_found > 0:
                logger.info(f"[Search] Relations mode: {relations_found} related docs found, using priority format")
                relation_results = []
                search_results = []
                for r in resources_list:
                    if r.get("match_reason", "").startswith("relation_from:"):
                        relation_results.append(r)
                    else:
                        search_results.append(r)

                result_strs = []
                idx = 1

                reason_groups: dict[str, list[str]] = {}
                for r in relation_results:
                    reason = r.get("relation_reason", "")
                    if reason and reason not in reason_groups:
                        reason_groups[reason] = []
                    if reason:
                        reason_groups[reason].append(r.get("uri", ""))

                result_strs.append("=== PRIORITY (pre-explored results) ===")
                result_strs.append("")
                result_strs.append("[RECORD OF PREVIOUS SESSION]")
                result_strs.append("A previous bot session already searched and answered THIS EXACT SAME QUESTION:")

                for reason_text in reason_groups:
                    result_strs.append("")
                    result_strs.append(reason_text)
                    result_strs.append("")

                result_strs.append("Documents selected as useful by that session:")
                for r in relation_results:
                    rel_uri = r.get("uri", "")
                    rel_abstract = r.get("abstract", "")[:200]
                    result_strs.append(f"  {idx}. [{rel_uri}] {rel_abstract}")
                    idx += 1
                result_strs.append("")
                result_strs.append("[YOUR STRATEGY]")
                result_strs.append("(1) Batch-read ONLY the PRIORITY documents in a SINGLE openviking_multi_read call.")
                result_strs.append("(2) Answer immediately from the content. Only read SEARCH RESULTS if PRIORITY documents are insufficient.")
                result_strs.append("(3) The Path above shows exactly how the previous session found the answer — skip those steps.")
                result_strs.append("")
                result_strs.append("=== SEARCH RESULTS ===")
                for r in search_results:
                    uri = r.get("uri", "")
                    abstract = r.get("abstract", "")[:200]
                    score = r.get("score", 0)
                    result_strs.append(f"{idx}. [{uri}] (score: {score:.2f}) {abstract}")
                    idx += 1

                output = "\n".join(result_strs)
                output += f"\n<!-- relations_found:{relations_found} searched:{len(resources_list)} -->"
            else:
                logger.info(f"[Search] Standard mode: {len(resources_list)} results")
                result_strs = [f"Search results for: {query}"]
                result_strs.append(f"\n{'=' * 40}")
                result_strs.append(f"SEARCH RESULTS ({len(resources_list)} items):")
                result_strs.append(f"{'=' * 40}")
                for i, item in enumerate(resources_list, 1):
                    uri = item.get("uri", "")
                    abstract = item.get("abstract", "")
                    score = item.get("score", 0.0)
                    result_strs.append(f"\n  [{i}] {uri} (score: {score:.3f})")
                    if abstract:
                        result_strs.append(f"      {abstract[:200]}")
                output = "\n".join(result_strs)

            return output
        except Exception as e:
            return f"Error searching Viking: {str(e)}"


class VikingAddResourceTool(OVFileTool):
    """Tool to add a resource to Viking."""

    @property
    def name(self) -> str:
        return "openviking_add_resource"

    @property
    def description(self) -> str:
        return "Add a resource (url like pic, git code or local file path) to OpenViking.This is a asynchronous operation."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Url or local file path"},
                "description": {"type": "string", "description": "Description of the resource"},
            },
            "required": ["path", "description"],
        }

    async def execute(
        self,
        tool_context: "ToolContext",
        path: str,
        description: str,
        **kwargs: Any,
    ) -> str:
        client = None
        try:
            if path and not path.startswith("http"):
                local_path = Path(path).expanduser().resolve()
                if not local_path.exists():
                    return f"Error: File not found: {path}"
                if not local_path.is_file():
                    return f"Error: Not a file: {path}"

            client = await VikingClient.create(tool_context.workspace_id)
            result = await client.add_resource(path, description)

            if result:
                root_uri = result.get("root_uri", "")
                return f"Successfully added resource: {root_uri}"
            else:
                return "Failed to add resource"
        except httpx.ReadTimeout:
            return f"Request timed out. The resource addition task may still be processing on the server side."
        except Exception as e:
            logger.warning(f"Error adding resource: {e}")
            return f"Error adding resource to Viking: {str(e)}"
        finally:
            if client:
                await client.close()


class VikingGrepTool(OVFileTool):
    """Tool to search Viking resources using regex patterns."""

    @property
    def name(self) -> str:
        return "openviking_grep"

    @property
    def description(self) -> str:
        return ("Search Viking resources using regex patterns (like grep). Supports multiple patterns to search concurrently."
                "Please avoid repeated calls with similar queries as much as possible.")

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "uri": {
                    "type": "string",
                    "description": "The whole Viking URI to search within (e.g., viking://resources/)",
                },
                "pattern": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Regex pattern or array of regex patterns to search for",
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "Case-insensitive search",
                    "default": False,
                },
            },
            "required": ["uri", "pattern"],
        }

    async def execute(
        self,
        tool_context: "ToolContext",
        uri: str,
        pattern: Union[str, list[str]],
        case_insensitive: bool = False,
        **kwargs: Any,
    ) -> str:
        try:
            client = await self._get_client(tool_context)
            patterns = [pattern] if isinstance(pattern, str) else pattern

            # Limit concurrent requests to avoid overwhelming the server and memory
            max_concurrent = 10
            semaphore = asyncio.Semaphore(max_concurrent)

            async def run_grep(p: str) -> tuple[str, list[Any]]:
                async with semaphore:
                    try:
                        result = await client.grep(uri, p, case_insensitive=case_insensitive)
                        if isinstance(result, dict):
                            matches = result.get("matches", [])
                        else:
                            matches = getattr(result, "matches", [])
                        return (p, matches)
                    except Exception as e:
                        logger.warning(f"Error searching for pattern '{p}': {e}")
                        return (p, [])

            tasks = [run_grep(p) for p in patterns]
            results = await asyncio.gather(*tasks)

            # Merge results by URI
            merged_results: dict[str, list[tuple[int, str, str]]] = {}
            total_matches = 0

            for p, matches in results:
                if not matches:
                    continue
                total_matches += len(matches)
                for match in matches:
                    if isinstance(match, dict):
                        match_uri = match.get("uri", "unknown")
                        line = match.get("line", "?")
                        content = match.get("content", "")
                    else:
                        match_uri = getattr(match, "uri", "unknown")
                        line = getattr(match, "line", "?")
                        content = getattr(match, "content", "")

                    if match_uri not in merged_results:
                        merged_results[match_uri] = []
                    merged_results[match_uri].append((line, content, p))

            if not merged_results:
                pattern_str = ", ".join(f"'{p}'" for p in patterns)
                return f"No matches found for patterns: {pattern_str}"

            result_lines = [f"Found {total_matches} match{'es' if total_matches != 1 else ''} across {len(patterns)} pattern{'s' if len(patterns) != 1 else ''}:"]

            struct_result: list[dict[str, Any]] = []

            for match_uri, matches in merged_results.items():
                matches.sort(key=lambda x: int(x[0]) if str(x[0]).isdigit() else 0)
                result_lines.append(f"\n[{match_uri}]")
                uri_struct = {"uri": match_uri, "lines": []}
                for line, content, pattern_name in matches:
                    result_lines.append(f"  L{line}: {content}")
                    uri_struct["lines"].append({"line": line, "content": content, "pattern": pattern_name})
                struct_result.append(uri_struct)


            return "\n".join(result_lines)
        except Exception as e:
            return f"Error searching Viking with grep: {str(e)}"


class VikingGlobTool(OVFileTool):
    """Tool to find Viking resources using glob patterns."""

    @property
    def name(self) -> str:
        return "openviking_glob"

    @property
    def description(self) -> str:
        return "Find Viking resources using glob patterns (like **/*.md, *.py)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern to match (e.g., **/*.md, *.py, src/**/*.js)",
                },
                "uri": {
                    "type": "string",
                    "description": "The whole Viking URI to search within (e.g., viking://resources/path/)",
                    "default": "",
                },
            },
            "required": ["pattern"],
        }

    async def execute(
        self, tool_context: "ToolContext", pattern: str, uri: str = "", **kwargs: Any
    ) -> str:
        try:
            client = await self._get_client(tool_context)
            result = await client.glob(pattern, uri=uri or None)

            if isinstance(result, dict):
                matches = result.get("matches", [])
                count = result.get("count", 0)
            else:
                matches = getattr(result, "matches", [])
                count = getattr(result, "count", 0)

            if not matches:
                return f"No files found for pattern: {pattern}"

            result_lines = [f"Found {count} file{'s' if count != 1 else ''}:"]
            for match_uri in matches:
                if isinstance(match_uri, dict):
                    match_uri = match_uri.get("uri", str(match_uri))
                result_lines.append(f"📄 {match_uri}")

            return "\n".join(result_lines)
        except Exception as e:
            return f"Error searching Viking with glob: {str(e)}"

class VikingMemoryCommitTool(OVFileTool):
    """Tool to commit messages to OpenViking session."""

    @property
    def name(self) -> str:
        return "openviking_memory_commit"

    @property
    def description(self) -> str:
        return "When user has personal information needs to be remembered, Commit messages to OpenViking."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "messages": {
                    "type": "array",
                    "description": "List of messages to commit, each with role, content",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string", "enum": ["user", "assistant"]},
                            "content": {"type": "string"},
                        },
                        "required": ["role", "content"],
                    },
                },
            },
            "required": ["messages"],
        }

    async def execute(
        self,
        tool_context: ToolContext,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> str:
        try:
            if not tool_context.sender_id:
                return "Error committed, sender_id is required."
            client = await self._get_client(tool_context)
            session_id = tool_context.session_key.safe_name()
            await client.commit(session_id, messages, tool_context.sender_id)
            return f"Successfully committed to session {session_id}"
        except Exception as e:
            logger.exception(f"Error processing message: {e}")
            return f"Error committing to Viking: {str(e)}"

class VikingMultiReadTool(OVFileTool):
    """Tool to read content from multiple Viking resources concurrently."""

    @property
    def name(self) -> str:
        return "openviking_multi_read"

    @property
    def description(self) -> str:
        return "Read full content from multiple OpenViking resources concurrently. Returns complete content for all URIs with no truncation."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "uris": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of Viking file URIs to read from (e.g., [\"viking://resources/path/123.md\", \"viking://resources/path/456.md\"])",
                },
            },
            "required": ["uris"],
        }

    async def execute(
        self,
        tool_context: ToolContext,
        uris: list[str],
        **kwargs: Any,
    ) -> str:
        level = "read"  # 默认获取完整内容
        try:
            if not uris:
                return "Error: No URIs provided."

            client = await self._get_client(tool_context)
            max_concurrent = 10
            semaphore = asyncio.Semaphore(max_concurrent)

            async def read_single_uri(uri: str) -> dict:
                async with semaphore:
                    try:
                        content = await client.read_content(uri, level=level)
                        return {
                            "uri": uri,
                            "content": content,
                            "success": True,
                        }
                    except Exception as e:
                        logger.warning(f"Error reading from {uri}: {e}")
                        return {
                            "uri": uri,
                            "content": f"Error reading from Viking: {str(e)}",
                            "success": False,
                        }

            # 并发读取所有URI
            read_tasks = [read_single_uri(uri) for uri in uris]
            results = await asyncio.gather(*read_tasks)

            # 构建结果
            result_lines = [f"Multi-read results for {len(uris)} resources (level: {level}):"]

            for i, result in enumerate(results, 1):
                uri = result["uri"]
                content = result["content"]
                success = result["success"]

                result_lines.append(f"\n--- START OF {uri} ---")
                if success:
                    result_lines.append(content)
                else:
                    result_lines.append(f"ERROR: {content}")
                result_lines.append(f"--- END OF {uri} ---")

            return "\n".join(result_lines)

        except Exception as e:
            logger.exception(f"Error in VikingMultiReadTool: {e}")
            return f"Error multi-reading Viking resources: {str(e)}"