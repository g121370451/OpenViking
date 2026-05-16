"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from vikingbot.agent.context import ContextBuilder
from vikingbot.agent.memory import MemoryStore
from vikingbot.agent.subagent import SubagentManager
from vikingbot.agent.tools import register_default_tools
from vikingbot.agent.tools.registry import ToolRegistry
from vikingbot.bus.events import InboundMessage, OutboundEventType, OutboundMessage
from vikingbot.bus.queue import MessageBus
from vikingbot.config import load_config
from vikingbot.config.schema import BotMode, Config, SessionKey
from vikingbot.hooks import HookContext
from vikingbot.hooks.manager import hook_manager
from vikingbot.agent.link_strategies import get_link_strategy
from vikingbot.providers.base import LLMProvider
from vikingbot.sandbox import SandboxManager
from vikingbot.session.manager import SessionManager
from vikingbot.utils.helpers import cal_str_tokens
from vikingbot.utils.tracing import trace

if TYPE_CHECKING:
    from vikingbot.config.schema import ExecToolConfig
    from vikingbot.cron.service import CronService


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 50,
        memory_window: int = 50,
        brave_api_key: str | None = None,
        exa_api_key: str | None = None,
        gen_image_model: str | None = None,
        exec_config: "ExecToolConfig | None" = None,
        cron_service: "CronService | None" = None,
        session_manager: SessionManager | None = None,
        sandbox_manager: SandboxManager | None = None,
        config: Config = None,
        eval: bool = False,
    ):
        """
        Initialize the AgentLoop with all required dependencies and configuration.

        Args:
            bus: MessageBus instance for publishing and subscribing to messages.
            provider: LLMProvider instance for making LLM calls.
            workspace: Path to the workspace directory for file operations.
            model: Optional model identifier. If not provided, uses the provider's default.
            max_iterations: Maximum number of tool execution iterations per message (default: 50).
            memory_window: Maximum number of messages to keep in session memory (default: 50).
            brave_api_key: Optional API key for Brave search integration.
            exa_api_key: Optional API key for Exa search integration.
            gen_image_model: Optional model identifier for image generation (default: openai/doubao-seedream-4-5-251128).
            exec_config: Optional configuration for the exec tool (command execution).
            cron_service: Optional CronService for scheduled task management.
            session_manager: Optional SessionManager for session persistence. If not provided, a new one is created.
            sandbox_manager: Optional SandboxManager for sandboxed operations.
            config: Optional Config object with full configuration. Used if other parameters are not provided.

        Note:
            The AgentLoop creates its own ContextBuilder, SessionManager (if not provided),
            ToolRegistry, and SubagentManager during initialization.

        Example:
            >>> loop = AgentLoop(
            ...     bus=message_bus,
            ...     provider=llm_provider,
            ...     workspace=Path("/path/to/workspace"),
            ...     model="gpt-4",
            ...     max_iterations=30,
            ... )
        """
        from vikingbot.config.schema import ExecToolConfig  # noqa: F811

        self.bus = bus
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.memory_window = memory_window
        self.brave_api_key = brave_api_key
        self.exa_api_key = exa_api_key
        self.gen_image_model = gen_image_model or "openai/doubao-seedream-4-5-251128"
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.sandbox_manager = sandbox_manager
        self.config = config

        self.context = ContextBuilder(workspace, sandbox_manager=sandbox_manager)

        self._register_builtin_hooks()
        self.sessions = session_manager or SessionManager(
            self.config.bot_data_path, sandbox_manager=sandbox_manager
        )
        self.tools = ToolRegistry()
        self._eval = eval
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            config=self.config,
            model=self.model,
            sandbox_manager=sandbox_manager,
        )

        self._running = False
        self._register_default_tools()

    async def _publish_thinking_event(
        self, session_key: SessionKey, event_type: OutboundEventType, content: str
    ) -> None:
        """
        Publish a thinking event to the message bus.

        Thinking events are used to communicate the agent's internal processing
        state to the user, such as when the agent is executing a tool or
        processing a complex request.

        Args:
            session_key: The session key identifying the conversation.
            event_type: The type of thinking event (e.g., THINKING, TOOL_START).
            content: The message content to display to the user.

        Note:
            This is an internal method used by the agent loop to communicate
            progress to users during long-running operations.

        Example:
            >>> await self._publish_thinking_event(
            ...     session_key=SessionKey(channel="telegram", chat_id="123"),
            ...     event_type=OutboundEventType.TOOL_START,
            ...     content="Executing web search..."
            ... )
        """
        await self.bus.publish_outbound(
            OutboundMessage(
                session_key=session_key,
                content=content,
                event_type=event_type,
            )
        )

    def _register_builtin_hooks(self):
        """Register built-in hooks."""
        hook_manager.register_path(self.config.hooks)

    def _register_default_tools(self) -> None:
        """Register default set of tools."""
        register_default_tools(
            registry=self.tools,
            config=self.config,
            send_callback=self.bus.publish_outbound,
            subagent_manager=self.subagents,
            cron_service=self.cron_service,
            include_web_tools=not self._eval,
            include_message_tool=not self._eval,
            include_spawn_tool=not self._eval,
            include_cron_tool=not self._eval,
            include_image_tool=not self._eval,
            include_filesystem_tools=not self._eval,
            include_exec_tool=not self._eval,
            eval_mode=self._eval,
        )

    async def run(self) -> None:
        """Run the agent loop, processing messages from the bus."""
        self._running = True
        logger.info("Agent loop started")

        while self._running:
            try:
                # Wait for next message
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)

                # Process it
                try:
                    response = await self._process_message(msg)
                    if response:
                        await self.bus.publish_outbound(response)
                except Exception as e:
                    logger.exception(f"Error processing message: {e}")
                    # Send error response
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            session_key=msg.session_key,
                            content=f"Sorry, I encountered an error: {str(e)}",
                            metadata=msg.metadata,
                        )
                    )
            except asyncio.TimeoutError:
                continue

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    async def _run_agent_loop(
        self,
        messages: list[dict],
        session_key: SessionKey,
        publish_events: bool = True,
        sender_id: str | None = None,
    ) -> tuple[str | None, list[dict], dict[str, int], int, str]:
        """
        Run the core agent loop: call LLM, execute tools, repeat until done.

        Args:
            messages: Initial message list
            session_key: Session key for tool execution context
            publish_events: Whether to publish ITERATION/REASONING/TOOL_CALL events to the bus

        Returns:
            tuple of (final_content, tools_used, token_usage, iteration, original_query)
        """
        iteration = 0
        final_content = None
        tools_used: list[dict] = []
        token_usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        tool_result_tokens: dict[str, int] = {}
        per_iteration_usage: list[dict] = []
        reasoning_tokens: int = 0

        # Extract original question from messages
        import re as _re_q
        original_query = ""
        for msg in reversed(messages):
            content = msg.get("content", "") if isinstance(msg, dict) else ""
            if not isinstance(content, str):
                continue
            for pattern in [r"Here'?s the question:\s*(.+)", r"Question:\s*(.+)"]:
                m = _re_q.search(pattern, content, _re_q.DOTALL)
                if m:
                    original_query = m.group(1).strip()
                    break
            if original_query:
                break
        if not original_query:
            for msg in reversed(messages):
                if isinstance(msg, dict) and msg.get("role") == "user":
                    content = msg.get("content", "")
                    if isinstance(content, str) and len(content) > 5:
                        original_query = content
                        break
        logger.error(f"[RelationsDebug] origin_query is {original_query}");

        # Reasoning switch: when disabled, force first search with original question
        enable_reasoning = os.environ.get("VIKINGBOT_ENABLE_REASONING", "1")
        if enable_reasoning == "0" and original_query:
            search_tool = self.tools.get("openviking_search")
            if search_tool:
                try:
                    search_result, _ = await self.tools.execute(
                        "openviking_search",
                        {"query": original_query, "target_uri": "viking://resources/"},
                        session_key=session_key,
                        sandbox_manager=self.sandbox_manager,
                        sender_id=sender_id,
                    )
                    messages.append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": "forced_search_0",
                            "type": "function",
                            "function": {
                                "name": "openviking_search",
                                "arguments": json.dumps({"query": original_query, "target_uri": "viking://resources/"})
                            }
                        }]
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": "forced_search_0",
                        "name": "openviking_search",
                        "content": search_result,
                    })
                    # Parse relations_found and searched terms from search result
                    import re as _re_fs
                    forced_relations = 0
                    forced_searched: list[str] = []
                    rf_match = _re_fs.search(r'<!-- relations_found:(\d+)\s+searched:(.*?)\s*-->', search_result or "")
                    if rf_match:
                        forced_relations = int(rf_match.group(1))
                        searched_str = rf_match.group(2).strip()
                        if searched_str:
                            forced_searched = [s.strip() for s in searched_str.split(";;") if s.strip()]
                    else:
                        rf_match = _re_fs.search(r'<!-- relations_found:(\d+)\s*-->', search_result or "")
                        if rf_match:
                            forced_relations = int(rf_match.group(1))

                    iteration = 1
                    tools_used.append({
                        "tool_name": "openviking_search",
                        "args": json.dumps({"query": original_query, "target_uri": "viking://resources/"}),
                        "reasoning": "(forced first search, reasoning disabled)",
                        "result": search_result,
                        "duration": 0,
                        "execute_success": True if search_result and "Error executing" not in search_result else False,
                        "input_token": 0,
                        "output_token": cal_str_tokens(search_result or "", text_type="mixed"),
                        "iteration": 1,
                        "relations_found": forced_relations,
                    })
                    tool_result_tokens["openviking_search"] = tool_result_tokens.get("openviking_search", 0) + cal_str_tokens(search_result or "", text_type="mixed")
                    if forced_relations > 0:
                        searched_lines = "\n".join(f'  ✗ "{s}"' for s in forced_searched)
                        messages.append({
                            "role": "user",
                            "content": (
                                "The following searches have ALREADY been executed for this question "
                                "by a previous session. Do NOT repeat any of them:\n"
                                f"{searched_lines}\n\n"
                                "Now read the PRIORITY documents using openviking_multi_read. "
                                "If they answer the question, respond immediately. "
                                "If not, read the SEARCH RESULTS."
                            )
                        })
                    else:
                        messages.append(
                            {"role": "system", "content": "Reflect on the results and decide next steps."}
                        )
                except Exception:
                    logger.exception("Forced first search failed, continuing with normal loop")

        while iteration < self.max_iterations:
            iteration += 1

            if publish_events:
                await self.bus.publish_outbound(
                    OutboundMessage(
                        session_key=session_key,
                        content=f"Iteration {iteration}/{self.max_iterations}",
                        event_type=OutboundEventType.ITERATION,
                    )
                )

            _extra_body = {}
            if enable_reasoning != "0":
                _extra_body["thinking"] = {"type": "enabled"}
            response = await self.provider.chat(
                messages=messages,
                tools=self.tools.get_definitions(),
                model=self.model,
                session_id=session_key.safe_name(),
                extra_body=_extra_body or None,
            )
            if response.usage:
                cur_token = response.usage
                token_usage["prompt_tokens"] += cur_token["prompt_tokens"]
                token_usage["completion_tokens"] += cur_token["completion_tokens"]
                token_usage["total_tokens"] += cur_token["total_tokens"]
                per_iteration_usage.append({
                    "iteration": iteration,
                    "prompt_tokens": cur_token["prompt_tokens"],
                    "completion_tokens": cur_token["completion_tokens"],
                })

            # 统计 reasoning tokens
            if response.reasoning_content:
                reasoning_tokens += cal_str_tokens(response.reasoning_content, text_type="mixed")

            if publish_events and response.reasoning_content:
                await self.bus.publish_outbound(
                    OutboundMessage(
                        session_key=session_key,
                        content=response.reasoning_content,
                        event_type=OutboundEventType.REASONING,
                    )
                )

            if response.has_tool_calls:
                args_list = [tc.arguments for tc in response.tool_calls]
                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(args),
                        },
                    }
                    for tc, args in zip(response.tool_calls, args_list)
                ]
                messages = self.context.add_assistant_message(
                    messages,
                    response.content,
                    tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                )

                # Stage 2: Execute all tools in parallel
                async def execute_single_tool(idx: int, tool_call):
                    """Execute a single tool and track execution time."""
                    tool_execute_start_time = time.time()
                    result, tool_context = await self.tools.execute(
                        tool_call.name,
                        tool_call.arguments,
                        session_key=session_key,
                        sandbox_manager=self.sandbox_manager,
                        sender_id=sender_id,
                    )
                    tool_execute_duration = (time.time() - tool_execute_start_time) * 1000
                    return idx, tool_call, result, tool_execute_duration, tool_context

                # Run all tool executions in parallel
                tool_tasks = [
                    execute_single_tool(idx, tool_call)
                    for idx, tool_call in enumerate(response.tool_calls)
                ]
                results = await asyncio.gather(*tool_tasks)

                total_relations_found = 0
                total_all_searched: set[str] = set()
                # Stage 3: Process results sequentially in original order
                for _idx, tool_call, result, tool_execute_duration, tool_context in results:
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info(f"[TOOL_CALL]: {tool_call.name}({args_str[:200]})")
                    logger.info(f"[RESULT]: {str(result)[:600]}")

                    if publish_events:
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                session_key=session_key,
                                content=f"{tool_call.name}({args_str})",
                                event_type=OutboundEventType.TOOL_CALL,
                            )
                        )
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                session_key=session_key,
                                content=str(result),
                                event_type=OutboundEventType.TOOL_RESULT,
                            )
                        )
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )

                    import re as _re
                    relations_found = 0
                    searched_from_comment: list[str] = []
                    if tool_call.name in ("openviking_search", "openviking_multi_read"):
                        rf_match = _re.search(r'<!-- relations_found:(\d+)\s+searched:(.*?)\s*-->', result or "")
                        if rf_match:
                            relations_found = int(rf_match.group(1))
                            searched_str = rf_match.group(2).strip()
                            if searched_str:
                                searched_from_comment = [s.strip() for s in searched_str.split(";;") if s.strip()]
                        else:
                            # Fallback: old format without searched terms
                            rf_match = _re.search(r'<!-- relations_found:(\d+)\s*-->', result or "")
                            if rf_match:
                                relations_found = int(rf_match.group(1))

                    total_relations_found += relations_found
                    total_all_searched.update(searched_from_comment)

                    # Parse args as object if it's a JSON string
                    try:
                        args_obj = json.loads(args_str) if isinstance(args_str, str) else args_str
                    except (json.JSONDecodeError, TypeError):
                        args_obj = args_str

                    # Use structured result if available, otherwise fall back to string result
                    display_result = getattr(tool_context, 'structured_result', None)
                    if display_result is None:
                        display_result = result

                    tool_used_dict = {
                        "tool_name": tool_call.name,
                        "args": args_obj,
                        "reasoning": (response.reasoning_content or response.content or "")[:1000],
                        "result": display_result,
                        "duration": tool_execute_duration,
                        "execute_success": True
                        if result and "Error executing" not in result
                        else False,
                        "input_token": tool_call.tokens,
                        "output_token": cal_str_tokens(result, text_type="mixed"),
                        "iteration": iteration,
                        "relations_found": relations_found,
                    }
                    tools_used.append(tool_used_dict)
                    tool_result_tokens[tool_call.name] = tool_result_tokens.get(tool_call.name, 0) + cal_str_tokens(result, text_type="mixed")

                if total_relations_found > 0:
                    searched_lines = "\n".join(f'  ✗ "{s}"' for s in sorted(total_all_searched))
                    messages.append({
                        "role": "user",
                        "content": (
                            "The following searches have ALREADY been executed for this question "
                            "by a previous session. Do NOT repeat any of them:\n"
                            f"{searched_lines}\n\n"
                            "Now read the PRIORITY documents using openviking_multi_read. "
                            "If they answer the question, respond immediately. "
                            "If not, read the SEARCH RESULTS."
                        )
                    })
                else:
                    messages.append(
                        {"role": "system", "content": "Reflect on the results and decide next steps."}
                    )
            else:
                final_content = response.content
                break
        if final_content is None:
            if iteration >= self.max_iterations:
                final_content = f"Reached {self.max_iterations} iterations without completion."
            else:
                final_content = "I've completed processing but have no response to give."

        # LLM Review step: precise relation building with minimal context
        _link_strategy = os.environ.get("VIKINGBOT_LINK_STRATEGY", "")
        _enable_linking = os.environ.get("VIKINGBOT_ENABLE_LINKING", "0")
        logger.info(
            f"[LLMReview] gate check: strategy={_link_strategy}, enable={_enable_linking}, "
            f"tools_used={len(tools_used) if tools_used else 0}, "
            f"original_query={bool(original_query)}"
        )
        if _link_strategy == "llm_review" and _enable_linking == "1" and tools_used and original_query:
            from vikingbot.agent.link_strategies import LinkStrategy

            read_uris: set[str] = set()
            uri_content: dict[str, str] = {}
            for tool in tools_used:
                if tool.get("tool_name") in ("openviking_read", "openviking_multi_read") and tool.get("execute_success"):
                    args = tool.get("args", "")
                    uris = LinkStrategy._extract_uris_from_args(args)
                    result = tool.get("result", "")
                    for u in uris:
                        read_uris.add(u)
                        if u not in uri_content:
                            uri_content[u] = (result or "")

            # 收集 search 结果中的 URI 及其所在轮次，同时保存 abstract/score
            search_uri_iterations: dict[str, int] = {}
            search_uri_meta: dict[str, dict] = {}  # uri -> {"abstract": ..., "score": ...}
            for tool in tools_used:
                if tool.get("tool_name") == "openviking_search" and tool.get("execute_success"):
                    iter_num = tool.get("iteration", 1)
                    result = tool.get("result", "")
                    if isinstance(result, list):
                        for item in result:
                            if not isinstance(item, dict):
                                continue
                            uri = item.get("uri", "")
                            if uri:
                                uri = LinkStrategy._normalize_uri(uri)
                                if uri not in search_uri_iterations:
                                    search_uri_iterations[uri] = iter_num
                                    search_uri_meta[uri] = {
                                        "abstract": item.get("abstract", ""),
                                        "score": item.get("score", 0),
                                    }
                    elif isinstance(result, str):
                        for u in LinkStrategy._extract_uris_from_args(result):
                            if u not in search_uri_iterations:
                                search_uri_iterations[u] = iter_num
            search_uris = set(search_uri_iterations.keys())

            logger.info(
                f"[LLMReview] collected: read_uris={len(read_uris)}, search_uris={len(search_uris)}, "
                f"search_uri_meta={len(search_uri_meta)}"
            )

            if read_uris or search_uris:
                # 确定候选池和 prompt
                import re as _re

                if read_uris:
                    # Case 1: 有 read_uris，用 read 内容作为候选池
                    candidate_uris = set(read_uris)
                    uri_list_lines = []
                    for idx, u in enumerate(sorted(read_uris), 1):
                        preview = uri_content.get(u, "")[:300].replace("\n", " ")
                        uri_list_lines.append(f"{idx}. {u}\n   Content: {preview}")
                    uri_list = "\n".join(uri_list_lines)
                    review_prompt = (
                        f"Question: {original_query}\n"
                        f"Answer: {final_content[:800]}\n\n"
                        f"Documents read during research:\n{uri_list}\n\n"
                        f"Task: Which of the above documents were USEFUL for answering the question?\n"
                        f"The answer was generated using these documents, so at least one MUST be relevant.\n\n"
                        f"Rules:\n"
                        f"- You MUST return at least one URI. An empty array [] is NEVER valid.\n"
                        f"- If unsure, pick the document most likely related to the answer.\n\n"
                        f"Output ONLY a JSON array of URI strings, no other text:\n"
                        f'["viking://...", "viking://...", ...]\n'
                    )
                else:
                    # Case 2: search-only，用 search 结果的 abstract 作为候选池
                    # 按 score 排序取 top 15
                    sorted_search = sorted(
                        search_uri_meta.items(),
                        key=lambda x: x[1].get("score", 0),
                        reverse=True,
                    )[:15]
                    candidate_uris = {uri for uri, _ in sorted_search}
                    uri_list_lines = []
                    for idx, (u, meta) in enumerate(sorted_search, 1):
                        abstract = (meta.get("abstract") or "")[:200].replace("\n", " ")
                        uri_list_lines.append(f"{idx}. {u}\n   Abstract: {abstract}")
                    uri_list = "\n".join(uri_list_lines)
                    review_prompt = (
                        f"Question: {original_query}\n"
                        f"Answer: {final_content[:800]}\n\n"
                        f"Documents found via search:\n{uri_list}\n\n"
                        f"Task: Based on the abstracts, which documents were USEFUL for answering the question?\n"
                        f"The answer was generated using these documents, so at least one MUST be relevant.\n\n"
                        f"Rules:\n"
                        f"- You MUST return at least one URI. An empty array [] is NEVER valid.\n"
                        f"- If unsure, pick the document most likely related to the answer.\n\n"
                        f"Output ONLY a JSON array of URI strings, no other text:\n"
                        f'["viking://...", "viking://...", ...]\n'
                    )

                logger.info(
                    f"[LLMReview] candidate_uris={len(candidate_uris)}, "
                    f"mode={'read' if read_uris else 'search-only'}, "
                    f"search_uris={len(search_uris)}"
                )
                logger.debug(f"[LLMReview] review_prompt:\n{review_prompt[:1500]}")

                try:
                    review_messages = [
                        {"role": "system", "content": "You are a document relation analyst. Output only valid JSON."},
                        {"role": "user", "content": review_prompt},
                    ]
                    review_response = await self.provider.chat(
                        messages=review_messages,
                        tools=[],
                        model=self.model,
                        session_id=session_key.safe_name(),
                    )

                    if review_response.usage:
                        token_usage["prompt_tokens"] += review_response.usage.get("prompt_tokens", 0)
                        token_usage["completion_tokens"] += review_response.usage.get("completion_tokens", 0)
                        token_usage["total_tokens"] += review_response.usage.get("total_tokens", 0)

                    # Parse LLM output as JSON
                    raw_output = review_response.content or ""
                    # 1. 清理 markdown 代码块包裹
                    cleaned = _re.sub(r'```(?:json)?\s*', '', raw_output).strip()
                    cleaned = _re.sub(r'```\s*$', '', cleaned).strip()

                    # 2. 尝试提取 JSON 数组
                    json_match = _re.search(r'\[.*\]', cleaned, _re.DOTALL)
                    useful_uris = []
                    parse_method = "none"

                    logger.info(f"[LLMReview] raw_output={raw_output[:300]}")

                    if json_match:
                        try:
                            parsed = json.loads(json_match.group())
                            for item in parsed:
                                if isinstance(item, str):
                                    u = LinkStrategy._normalize_uri(item)
                                    if u in candidate_uris:
                                        useful_uris.append(u)
                                elif isinstance(item, dict):
                                    u = LinkStrategy._normalize_uri(item.get("uri", ""))
                                    if u in candidate_uris:
                                        useful_uris.append(u)
                            parse_method = "json"
                        except json.JSONDecodeError:
                            logger.warning(
                                f"[LLMReview] JSON parse FAILED. Raw output (first 300 chars): {raw_output[:300]}"
                            )

                    # 3. fallback：正则提取 viking:// URI，与 candidate_uris 取交集
                    if not useful_uris:
                        found_uris = _re.findall(r'viking://[^\s"\'\\,\]\}]+', raw_output)
                        for u in found_uris:
                            u = LinkStrategy._normalize_uri(u.rstrip('.'))
                            if u in candidate_uris:
                                useful_uris.append(u)
                        if useful_uris:
                            parse_method = "regex-fallback"

                    # 去重
                    useful_uris = list(dict.fromkeys(useful_uris))

                    # Fallback: 如果 LLM 返回空数组，强制选 score 最高的候选文档
                    if not useful_uris:
                        if read_uris:
                            # read 模式：选第一个 read_uri 作为 fallback
                            useful_uris = [sorted(candidate_uris)[0]]
                        elif search_uri_meta:
                            # search-only 模式：选 score 最高的
                            best_uri = max(search_uri_meta.keys(), key=lambda u: search_uri_meta[u].get("score", 0))
                            if best_uri in candidate_uris:
                                useful_uris = [best_uri]
                            else:
                                useful_uris = [sorted(candidate_uris)[0]]
                        else:
                            useful_uris = [sorted(candidate_uris)[0]]
                        parse_method = "forced-fallback"
                        logger.warning(
                            f"[LLMReview] LLM returned empty, forced fallback: {useful_uris}"
                        )

                    # 日志
                    if useful_uris:
                        logger.info(f"[LLMReview] Parsed {len(useful_uris)} useful doc(s) via {parse_method}")
                    else:
                        logger.warning(
                            f"[LLMReview] 0 useful docs extracted. "
                            f"candidate_uris={len(candidate_uris)}, raw_output={raw_output[:300]}"
                        )

                    if useful_uris:
                        from vikingbot.openviking_mount.ov_server import VikingClient
                        workspace_id = self.sandbox_manager.to_workspace_id(session_key) if self.sandbox_manager else None
                        rv_client = await VikingClient.create(workspace_id)
                        try:
                            linked = 0
                            seen_pairs: set[tuple[str, str]] = set()

                            # 用原始问题搜索 top5 URI，并入 from_uris 以扩展链接覆盖
                            try:
                                search_result = await rv_client.search(original_query)
                                top5_resources = search_result.get("resources", [])[:5]
                                top5_uris = set()
                                for r in top5_resources:
                                    uri = r.get("uri", "")
                                    if uri:
                                        top5_uris.add(LinkStrategy._normalize_uri(uri))
                                all_search_uris = search_uris | top5_uris
                                logger.info(
                                    f"[LLMReview] top5 search added {len(top5_uris)} URIs to from_uris "
                                    f"(total search_uris: {len(search_uris)} -> {len(all_search_uris)})"
                                )
                            except Exception as e:
                                logger.warning(f"[LLMReview] top5 search failed, using original search_uris: {e}")
                                all_search_uris = search_uris

                            # from_uris = search_uris - useful_uris（避免自链接）
                            from_uris = all_search_uris - set(useful_uris)
                            if not from_uris:
                                # 如果所有 search URI 都被选为 useful，用全部 search_uris
                                from_uris = all_search_uris

                            # 收集所有 search 轮次的改写 query，构建富 reason
                            search_queries = []
                            for tool in tools_used:
                                if tool.get("tool_name") == "openviking_search" and tool.get("execute_success"):
                                    args = tool.get("args", {})
                                    if isinstance(args, str):
                                        try:
                                            args = json.loads(args)
                                        except (json.JSONDecodeError, TypeError):
                                            args = {}
                                    if isinstance(args, dict):
                                        q = args.get("query", "")
                                        if q and q not in search_queries:
                                            search_queries.append(q)
                            reason_parts = [f"Question: {original_query}"]
                            if search_queries:
                                reason_parts.append(f"Searched: {'; '.join(search_queries)}")
                            link_reason = " | ".join(reason_parts)

                            for tgt in useful_uris:
                                for src in from_uris:
                                    if src == tgt:
                                        continue
                                    pair = (min(src, tgt), max(src, tgt))
                                    if pair in seen_pairs:
                                        continue
                                    seen_pairs.add(pair)
                                    try:
                                        await rv_client.link(src, [tgt], reason=link_reason,
                                                            query=original_query, strategy="llm_review", weight=1.0)
                                        linked += 1
                                    except Exception as e:
                                        logger.warning(f"[LLMReview] Link failed: {e}")

                            iteration += 1
                            tools_used.append({
                                "tool_name": "openviking_link",
                                "args": json.dumps({
                                    "from_uris": sorted(from_uris),
                                    "to_uris": useful_uris,
                                }),
                                "reasoning": "(precise review step)",
                                "result": f"Created {linked} relation(s) from {len(from_uris)} from_uris to {len(useful_uris)} useful docs, parse={parse_method}",
                                "duration": 0,
                                "execute_success": linked > 0,
                                "input_token": 0,
                                "output_token": 0,
                                "iteration": iteration,
                                "relations_found": 0,
                            })
                            logger.info(
                                f"[LLMReview] post_link DONE: created {linked} edge(s), "
                                f"from {len(from_uris)} from_uris to {len(useful_uris)} useful_docs, "
                                f"parse={parse_method}"
                            )
                        finally:
                            await rv_client.close()
                    else:
                        # 解析失败，仍然写 tools_used 记录
                        logger.warning(
                            f"[LLMReview] post_link SKIPPED - no useful docs parsed. "
                            f"search_uris={len(search_uris)}, read_uris={len(read_uris)}, "
                            f"query={original_query[:100]}"
                        )
                        iteration += 1
                        tools_used.append({
                            "tool_name": "openviking_link",
                            "args": json.dumps({"from_uris": sorted(search_uris), "to_uris": []}),
                            "reasoning": "(precise review step - PARSE FAILED)",
                            "result": f"FAILED: parse_method={parse_method}",
                            "duration": 0,
                            "execute_success": False,
                            "input_token": 0,
                            "output_token": 0,
                            "iteration": iteration,
                            "relations_found": 0,
                            "parse_method": parse_method,
                        })
                except Exception:
                    logger.exception("[LLMReview] Review step failed, continuing")

        # 估算固定开销
        estimated_system_tokens = cal_str_tokens(messages[0].get("content", "") if messages and messages[0].get("role") == "system" else "", text_type="mixed")
        estimated_tool_schema_tokens = cal_str_tokens(json.dumps(self.tools.get_definitions()), text_type="mixed")

        token_usage["tool_result_tokens"] = tool_result_tokens
        token_usage["per_iteration"] = per_iteration_usage
        token_usage["reasoning_tokens"] = reasoning_tokens
        token_usage["estimated_system_prompt_tokens"] = estimated_system_tokens
        token_usage["estimated_tool_schema_tokens"] = estimated_tool_schema_tokens
        return final_content, tools_used, token_usage, iteration, original_query

    @trace(
        name="process_message",
        extract_session_id=lambda msg: msg.session_key.safe_name(),
        extract_user_id=lambda msg: msg.sender_id,
    )
    async def _process_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        Process a single inbound message.

        Args:
            msg: The inbound message to process.
            session_key: Override session key (used by process_direct).

        Returns:
            The response message, or None if no response needed.
        """
        # Handle system messages (subagent announces)
        # The chat_id contains the original "channel:chat_id" to route back to
        start_time = time.time()
        long_running_notified = False

        # 监控处理时长，每50秒发送处理中提示事件
        async def check_long_running():
            nonlocal long_running_notified
            tick_count = 0
            # 最多发送7次提示
            max_ticks = 7

            while not long_running_notified and tick_count < max_ticks:
                await asyncio.sleep(40)
                if long_running_notified:
                    break
                if msg.metadata:
                    message_id = msg.metadata.get("message_id")
                    if message_id:
                        try:
                            # 发送处理中tick事件，对应channel会自行处理展示逻辑
                            await self.bus.publish_outbound(
                                OutboundMessage(
                                    session_key=msg.session_key,
                                    content="",
                                    metadata={
                                        "action": "processing_tick",
                                        "tick_count": tick_count,
                                        "message_id": message_id,
                                    },
                                )
                            )
                            tick_count += 1
                        except Exception as e:
                            logger.debug(f"Failed to send processing tick: {e}")

        monitor_task = asyncio.create_task(check_long_running())

        try:
            if msg.session_key.type == "system":
                return await self._process_system_message(msg)

            preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
            logger.info(f"Processing message from {msg.session_key}:{msg.sender_id}: {preview}")

            session_key = msg.session_key
            # For CLI/direct sessions, skip heartbeat by default
            skip_heartbeat = session_key.type == "cli"
            session = self.sessions.get_or_create(session_key, skip_heartbeat=skip_heartbeat)

            # Handle slash commands
            is_group_chat = msg.metadata.get("chat_type") == "group" if msg.metadata else False
            if is_group_chat:
                cmd = msg.content.replace(f"@{msg.sender_id}", "").strip().lower()
            else:
                cmd = msg.content.strip().lower()
            if cmd == "/new":
                # Clone session for async consolidation, then immediately clear original
                if not self._check_cmd_auth(msg):
                    return OutboundMessage(
                        session_key=msg.session_key, content="🐈 Sorry, you are not authorized to use this command.",
                        metadata=msg.metadata
                    )
                session_clone = session.clone()
                session.clear()
                await self.sessions.save(session)
                # Run consolidation in background
                await self._safe_consolidate_memory(session_clone, archive_all=True)
                return OutboundMessage(
                    session_key=msg.session_key, content="🐈 New session started. Memory consolidated.", metadata=msg.metadata
                )
            if cmd == "/remember":
                if not self._check_cmd_auth(msg):
                    return OutboundMessage(
                        session_key=msg.session_key, content="🐈 Sorry, you are not authorized to use this command.",
                        metadata=msg.metadata
                    )
                session_clone = session.clone()
                await self._consolidate_viking_memory(session_clone)
                return OutboundMessage(
                    session_key=msg.session_key, content="This conversation has been submitted to memory storage.", metadata=msg.metadata
                )
            if cmd == "/help":
                return OutboundMessage(
                    session_key=msg.session_key,
                    content="🐈 vikingbot commands:\n/new — Start a new conversation\n/remember — Submit current session to memories and start new session\n/help — Show available commands",
                    metadata=msg.metadata
                )

            # Debug mode handling
            if self.config.mode == BotMode.DEBUG:
                # In debug mode, only record message to session, no processing or reply
                session.add_message("user", msg.content, sender_id=msg.sender_id)
                await self.sessions.save(session)
                return None

            # Consolidate memory before processing if session is too large
            if len(session.messages) > self.memory_window:
                # Clone session for async consolidation, then immediately trim original
                session_clone = session.clone()
                keep_count = min(10, max(2, self.memory_window // 2))
                session.messages = session.messages[-keep_count:] if keep_count else []
                await self.sessions.save(session)
                # Run consolidation in background
                await self._safe_consolidate_memory(session_clone, archive_all=False)

            if self.sandbox_manager:
                message_workspace = self.sandbox_manager.get_workspace_path(session_key)
            else:
                message_workspace = self.workspace

            from vikingbot.agent.context import ContextBuilder

            message_context = ContextBuilder(
                message_workspace,
                sandbox_manager=self.sandbox_manager,
                sender_id=msg.sender_id,
                is_group_chat=is_group_chat,
                eval=self._eval,
            )

            # Build initial messages (use get_history for LLM-formatted messages)
            messages = await message_context.build_messages(
                history=session.get_history(),
                current_message=msg.content,
                media=msg.media if msg.media else None,
                session_key=msg.session_key,
            )
            # logger.info(f"New messages: {messages}")

            # Run agent loop
            final_content, tools_used, token_usage, iteration, original_query = await self._run_agent_loop(
                messages=messages,
                session_key=session_key,
                publish_events=True,
                sender_id=msg.sender_id,
            )

            # Log response preview
            preview = final_content[:300] + "..." if len(final_content) > 300 else final_content
            logger.info(f"Response to {msg.session_key}: {preview}")

            # Post-answer linking: build relations between cross-iteration read URIs
            enable_linking_val = os.environ.get("VIKINGBOT_ENABLE_LINKING", "0")
            # logger.error(f"[PostAnswerLink] Check: enable_linking={enable_linking_val}, tools_used_count={len(tools_used) if tools_used else 0}")
            if enable_linking_val == "1" and tools_used:
                await self._post_answer_link(tools_used, original_query, session_key)

            # Save to session (include tool names so consolidation sees what happened)
            session.add_message("user", msg.content, sender_id=msg.sender_id)
            session.add_message(
                "assistant", final_content, tools_used=tools_used if tools_used else None, token_usage=token_usage,
                sender_id=msg.sender_id,
            )
            await self.sessions.save(session)

            time_cost = round(time.time() - start_time, 2)
            if tools_used is not None:
                tools_used_names = [tool["tool_name"] for tool in tools_used]
            else:
                tools_used_names = []
                tools_used = []
            return OutboundMessage(
                session_key=msg.session_key,
                content=final_content,
                metadata=msg.metadata,
                token_usage=token_usage,
                time_cost=time_cost,
                iteration=iteration,
                tools_used_names=tools_used_names,
                tools_used=tools_used
            )
        finally:
            long_running_notified = True
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass

    async def _post_answer_link(
        self,
        tools_used: list[dict],
        original_question: str,
        session_key: "SessionKey",
    ) -> None:
        """Build relations between documents read during this QA session."""
        strategy_name = os.environ.get("VIKINGBOT_LINK_STRATEGY", "blind")
        try:
            strategy = get_link_strategy(strategy_name)
            logger.info(f"[PostAnswerLink] Using strategy={strategy.name}, query={original_question[:100]}")

            from vikingbot.openviking_mount.ov_server import VikingClient
            workspace_id = self.sandbox_manager.to_workspace_id(session_key) if self.sandbox_manager else None
            client = await VikingClient.create(workspace_id)

            linked = await strategy.build_links(
                tools_used=tools_used,
                original_question=original_question,
                client=client,
                provider=self.provider,
                model=self.model,
            )
            if linked:
                logger.info(f"[PostAnswerLink] Strategy={strategy.name}, created {linked} relation(s)")
        except Exception as e:
            logger.warning(f"Post-answer linking failed: {e}")

    async def _process_system_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        Process a system message (e.g., subagent announce).

        The chat_id field contains "original_channel:original_chat_id" to route
        the response back to the correct destination.
        """
        logger.info(f"Processing system message from {msg.sender_id}")

        session = self.sessions.get_or_create(msg.session_key)

        # Build messages with the announce content
        messages = await self.context.build_messages(
            history=session.get_history(), current_message=msg.content, session_key=msg.session_key
        )

        # Run agent loop (no events published)
        final_content, tools_used, token_usage, iteration, _original_query = await self._run_agent_loop(
            messages=messages,
            session_key=msg.session_key,
            publish_events=False,
        )

        if final_content is None:
            final_content = "Background task completed."

        # Save to session (mark as system message in history)
        session.add_message("user", f"[System: {msg.sender_id}] {msg.content}")
        session.add_message(
            "assistant", final_content, tools_used=tools_used if tools_used else None
        )
        await self.sessions.save(session)

        return OutboundMessage(session_key=msg.session_key, content=final_content)

    async def _consolidate_memory(self, session, archive_all: bool = False) -> None:
        """Consolidate old messages into MEMORY.md + HISTORY.md. Works on a cloned session."""
        try:
            if not session.messages:
                return

            # use openviking tools to extract memory
            config = self.config
            if config.mode == BotMode.READONLY:
                if not config.channels_config or not config.channels_config.get_all_channels():
                    return
                allow_from = [config.ov_server.admin_user_id]
                for channel_config in config.channels_config.get_all_channels():
                    if channel_config and channel_config.type.value == session.key.type:
                        if hasattr(channel_config, "allow_from"):
                            allow_from.extend(channel_config.allow_from)
                messages = [msg for msg in session.messages if msg.get("sender_id") in allow_from]
                session.messages = messages
            await self._consolidate_viking_memory(session)

            if self.sandbox_manager:
                memory_workspace = self.sandbox_manager.get_workspace_path(session.key)
            else:
                memory_workspace = self.workspace

            memory = MemoryStore(memory_workspace)
            if archive_all:
                old_messages = session.messages
                keep_count = 0
            else:
                keep_count = min(10, max(2, self.memory_window // 2))
                old_messages = session.messages[:-keep_count]
            if not old_messages:
                return
            logger.info(
                f"Memory consolidation started: {len(session.messages)} messages, archiving {len(old_messages)}, keeping {keep_count}"
            )

            # Format messages for LLM (include tool names when available)
            lines = []
            for m in old_messages:
                if not m.get("content"):
                    continue
                tools_used = m.get("tools_used", [])
                if tools_used and isinstance(tools_used, list):
                    tool_names = [
                        tc.get("tool_name", "unknown") for tc in tools_used if isinstance(tc, dict)
                    ]
                    tools_str = f" [tools: {', '.join(tool_names)}]" if tool_names else ""
                else:
                    tools_str = ""
                lines.append(
                    f"[{m.get('timestamp', '?')[:16]}] {m['role'].upper()}{tools_str}: {m['content']}"
                )
            conversation = "\n".join(lines)
            current_memory = memory.read_long_term()

            prompt = f"""You are a memory consolidation agent. Process this conversation and return a JSON object with exactly two keys:

1. "history_entry": A paragraph (2-5 sentences) summarizing the key events/decisions/topics. Start with a timestamp like [YYYY-MM-DD HH:MM]. Include enough detail to be useful when found by grep search later.

2. "memory_update": The updated long-term memory content. Add any new facts: user location, preferences, personal info, habits, project context, technical decisions, tools/services used. If nothing new, return the existing content unchanged.

## Current Long-term Memory
{current_memory or "(empty)"}

## Conversation to Process
{conversation}

Respond with ONLY valid JSON, no markdown fences."""

            response = await self.provider.chat(
                messages=[
                    {
                        "role": "system",
                        "content": "You are a memory consolidation agent. Respond only with valid JSON.",
                    },
                    {"role": "user", "content": prompt},
                ],
                model=self.model,
                session_id=session.key.safe_name(),
            )
            text = (response.content or "").strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            result = json.loads(text)

            if entry := result.get("history_entry"):
                memory.append_history(entry)
            if update := result.get("memory_update"):
                if load_config().use_local_memory and update != current_memory:
                    memory.write_long_term(update)

            # Session trimming and saving is handled by the caller before calling _consolidate_memory
            # This method works on a cloned session, so no need to save it
            logger.info("Memory consolidation done")
        except Exception as e:
            logger.exception(f"Memory consolidation failed: {e}")

    async def _consolidate_viking_memory(self, session) -> None:
        """Consolidate old messages into MEMORY.md + HISTORY.md. Works on a cloned session."""
        try:
            if not session.messages:
                logger.info(f"No messages to commit openviking for session {session.key.safe_name()} (allow_from filter applied)")
                return

            # use openviking tools to extract memory
            await hook_manager.execute_hooks(
                context=HookContext(
                    event_type="message.compact",
                    session_id=session.key.safe_name(),
                    workspace_id=self.sandbox_manager.to_workspace_id(session.key),
                    session_key=session.key,
                ),
                session=session,
            )
        except Exception as e:
            logger.exception(f"Memory consolidation failed: {e}")

    async def _safe_consolidate_memory(self, session, archive_all: bool = False) -> None:
        """Safe wrapper for _consolidate_memory that ensures all exceptions are caught."""
        try:
            await self._consolidate_memory(session, archive_all)
        except Exception as e:
            logger.exception(f"Background memory consolidation task failed: {e}")

    def _check_cmd_auth(self, msg: InboundMessage) -> bool:
        """Check if the session key is authorized for command execution.

        Returns:
            True if authorized, False otherwise.
        Args:
            session_key: Session key to check.
        """
        if self.config.mode == BotMode.NORMAL:
            return True
        allow_from = []
        if self.config.ov_server and self.config.ov_server.admin_user_id:
            allow_from.append(self.config.ov_server.admin_user_id)
        for channel in self.config.channels_config.get_all_channels():
            if channel.channel_key() == msg.session_key.channel_key():
                allow_cmd = getattr(channel, 'allow_cmd_from', [])
                if allow_cmd:
                    allow_from.extend(allow_cmd)
                break

        # If channel not found or sender not in allow_from list, ignore message
        if msg.sender_id not in allow_from:
            logger.debug(f"Sender {msg.sender_id} not allowed in channel {msg.session_key.channel_key()}")
            return False
        return True

    async def process_direct(
        self,
        content: str,
        session_key: SessionKey = SessionKey(type="cli", channel_id="default", chat_id="direct"),
    ) -> str:
        """
        Process a message directly (for CLI or cron usage).

        Args:
            content: The message content.
            session_key: Session identifier (overrides channel:chat_id for session lookup).

        Returns:
            The agent's response.
        """
        msg = InboundMessage(session_key=session_key, sender_id="user", content=content)

        response = await self._process_message(msg)
        return response.content if response else ""
