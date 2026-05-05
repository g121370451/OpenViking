"""
Linking strategies for post-answer document relation building.

三种策略:
- BlindLinkStrategy: O(n²) 全盲链接 (现有行为)
- CrossIterationLinkStrategy: 只在不同迭代轮次间盲链接
- LLMReviewLinkStrategy: LLM 判定有用后才链接
"""

import json
import logging
import math
import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vikingbot.openviking_mount.ov_server import VikingClient
    from vikingbot.providers.base import LLMProvider

logger = logging.getLogger(__name__)


class LinkStrategy(ABC):
    """Linking strategy interface."""

    @property
    @abstractmethod
    def name(self) -> str:
        """策略名，决定存储文件名 .relations_{name}.jsonl"""

    @abstractmethod
    async def build_links(
        self,
        tools_used: list[dict],
        original_question: str,
        client: "VikingClient",
        provider: "LLMProvider | None" = None,
        model: str = "",
    ) -> int:
        """执行链接构建，返回实际创建的链接数。"""

    @staticmethod
    def _extract_uris_from_args(args_raw) -> list[str]:
        """Extract viking:// URIs from tool call args."""
        if isinstance(args_raw, dict):
            args_str = json.dumps(args_raw)
        elif isinstance(args_raw, str):
            args_str = args_raw
        else:
            return []
        return re.findall(r'viking://[^\s"\\,\]\}]+', args_str)

    def _collect_read_uris(self, tools_used: list[dict]) -> list[str]:
        """收集所有成功 read 的文档 URI（去重）。"""
        read_tools = {"openviking_multi_read", "openviking_read"}
        all_uris = []
        for tool in tools_used:
            if tool.get("tool_name") in read_tools and tool.get("execute_success"):
                uris = self._extract_uris_from_args(tool.get("args", ""))
                if uris:
                    all_uris.extend(uris)

        seen = set()
        unique = []
        for u in all_uris:
            if u not in seen:
                seen.add(u)
                unique.append(u)
        return unique

    @staticmethod
    def _compute_weight(iter_a: int, iter_b: int) -> float:
        """Gaussian weight peaking at iteration distance 1 (cross-iteration evidence pairs)."""
        d = abs(iter_a - iter_b)
        return math.exp(-((d - 1) ** 2) / 4)

    @staticmethod
    def _compute_jaccard(text1: str, text2: str) -> float:
        """Compute Jaccard similarity between two texts based on word tokens."""
        if not text1 or not text2:
            return 0.0
        tokens1 = set(text1.lower().split())
        tokens2 = set(text2.lower().split())
        if not tokens1 or not tokens2:
            return 0.0
        intersection = len(tokens1 & tokens2)
        union = len(tokens1 | tokens2)
        return intersection / union if union > 0 else 0.0

    @staticmethod
    def _extract_uri_content(tools_used: list[dict]) -> dict[str, str]:
        """Extract uri → content mapping from tool results (first 500 chars)."""
        read_tools = {"openviking_multi_read", "openviking_read"}
        uri_content: dict[str, str] = {}
        for tool in tools_used:
            if not (tool.get("tool_name") in read_tools and tool.get("execute_success")):
                continue
            result = tool.get("result", "")
            if not result:
                continue
            args = tool.get("args", "")
            uris = LinkStrategy._extract_uris_from_args(args)

            if tool.get("tool_name") == "openviking_multi_read" and uris:
                for uri in uris:
                    escaped = re.escape(uri)
                    m = re.search(
                        rf'--- START OF {escaped} ---(.*?)--- END OF {escaped} ---', result, re.DOTALL
                    )
                    if m:
                        uri_content[uri] = m.group(1).strip()[:500]
                    elif uri not in uri_content:
                        uri_content[uri] = ""
            elif tool.get("tool_name") == "openviking_read" and uris:
                uri = uris[0]
                if uri not in uri_content:
                    uri_content[uri] = result.strip()[:500]
        return uri_content

    @staticmethod
    def _extract_iteration_for_uri(tools_used: list[dict]) -> dict[str, int]:
        """Build uri → first_iteration mapping from tools_used."""
        read_tools = {"openviking_multi_read", "openviking_read"}
        uri_iter: dict[str, int] = {}
        for tool in tools_used:
            if not (tool.get("tool_name") in read_tools and tool.get("execute_success")):
                continue
            uris = LinkStrategy._extract_uris_from_args(tool.get("args", ""))
            iteration = tool.get("iteration", 0)
            for u in uris:
                if u not in uri_iter:
                    uri_iter[u] = iteration
        return uri_iter


class BlindLinkStrategy(LinkStrategy):
    """现有行为: 所有 read 过的 URI 去重后两两盲链接。"""

    @property
    def name(self) -> str:
        return "blind"

    async def build_links(
        self,
        tools_used: list[dict],
        original_question: str,
        client: "VikingClient",
        provider: "LLMProvider | None" = None,
        model: str = "",
    ) -> int:
        unique_uris = self._collect_read_uris(tools_used)
        logger.info(f"[BlindLink] Total unique URIs: {len(unique_uris)}")

        if len(unique_uris) < 2:
            return 0

        uri_iter = self._extract_iteration_for_uri(tools_used)

        linked = set()
        for i in range(len(unique_uris)):
            for j in range(i + 1, len(unique_uris)):
                u1, u2 = unique_uris[i], unique_uris[j]
                pair = (min(u1, u2), max(u1, u2))
                if pair in linked:
                    continue
                linked.add(pair)
                weight = self._compute_weight(uri_iter.get(u1, 0), uri_iter.get(u2, 0))
                try:
                    await client.link(u1, [u2], reason="co-referenced", query=original_question, strategy=self.name, weight=weight)
                except Exception as e:
                    logger.warning(f"[BlindLink] Link failed: {e}")

        logger.info(f"[BlindLink] Created {len(linked)} relation(s)")
        return len(linked)


class CrossIterationLinkStrategy(LinkStrategy):
    """跨轮次盲链接 + 同轮次内低重叠链接。"""

    @property
    def name(self) -> str:
        return "cross_iteration"

    async def build_links(
        self,
        tools_used: list[dict],
        original_question: str,
        client: "VikingClient",
        provider: "LLMProvider | None" = None,
        model: str = "",
    ) -> int:
        read_tools = {"openviking_multi_read", "openviking_read"}

        # 按迭代轮次分组 URI
        iteration_uris: dict[int, list[str]] = {}
        for tool in tools_used:
            if not (tool.get("tool_name") in read_tools and tool.get("execute_success")):
                continue
            uris = self._extract_uris_from_args(tool.get("args", ""))
            if not uris:
                continue
            iteration = tool.get("iteration", 0)
            if iteration not in iteration_uris:
                iteration_uris[iteration] = []
            iteration_uris[iteration].extend(uris)

        # 每轮内去重
        for it in iteration_uris:
            seen = set()
            unique = []
            for u in iteration_uris[it]:
                if u not in seen:
                    seen.add(u)
                    unique.append(u)
            iteration_uris[it] = unique

        iterations = sorted(iteration_uris.keys())
        logger.info(f"[CrossIterLink] Iterations: {iterations}, URIs per iter: {[len(iteration_uris[i]) for i in iterations]}")

        linked = set()
        cross_linked = 0
        same_linked = 0

        # 跨轮次链接
        for i in range(len(iterations)):
            for j in range(i + 1, len(iterations)):
                it_a, it_b = iterations[i], iterations[j]
                weight = self._compute_weight(it_a, it_b)
                for u1 in iteration_uris[it_a]:
                    for u2 in iteration_uris[it_b]:
                        pair = (min(u1, u2), max(u1, u2))
                        if pair in linked:
                            continue
                        linked.add(pair)
                        try:
                            await client.link(u1, [u2], reason="cross-iteration", query=original_question, strategy=self.name, weight=weight)
                            cross_linked += 1
                        except Exception as e:
                            logger.warning(f"[CrossIterLink] Link failed: {e}")

        # 同轮次低重叠链接
        uri_content = self._extract_uri_content(tools_used)
        for it in iterations:
            uris = iteration_uris[it]
            if len(uris) < 2:
                continue
            for i in range(len(uris)):
                for j in range(i + 1, len(uris)):
                    u1, u2 = uris[i], uris[j]
                    pair = (min(u1, u2), max(u1, u2))
                    if pair in linked:
                        continue
                    jaccard = self._compute_jaccard(uri_content.get(u1, ""), uri_content.get(u2, ""))
                    if jaccard < 0.3:
                        linked.add(pair)
                        try:
                            await client.link(u1, [u2], reason="same-iteration-low-overlap", query=original_question, strategy=self.name, weight=0.3)
                            same_linked += 1
                        except Exception as e:
                            logger.warning(f"[CrossIterLink] Same-iter link failed: {e}")

        logger.info(f"[CrossIterLink] Created {cross_linked} cross-iter + {same_linked} same-iter = {cross_linked + same_linked} relation(s)")
        return cross_linked + same_linked


class LLMReviewLinkStrategy(LinkStrategy):
    """LLM Review: 建边已在 _run_agent_loop() 的 review 步骤中完成，此处返回 0。"""

    @property
    def name(self) -> str:
        return "llm_review"

    async def build_links(
        self,
        tools_used: list[dict],
        original_question: str,
        client: "VikingClient",
        provider: "LLMProvider | None" = None,
        model: str = "",
    ) -> int:
        logger.info("[LLMReviewLink] Links already created during bot review step, nothing to do")
        return 0


def get_link_strategy(strategy_name: str) -> LinkStrategy:
    """工厂函数：根据策略名返回对应的 LinkStrategy 实例。"""
    strategies = {
        "blind": BlindLinkStrategy(),
        "read_blind": BlindLinkStrategy(),
        "cross_iteration": CrossIterationLinkStrategy(),
        "llm_review": LLMReviewLinkStrategy(),
    }
    s = strategies.get(strategy_name)
    if s is None:
        logger.warning(f"Unknown link_strategy '{strategy_name}', falling back to 'blind'")
        s = strategies["blind"]
    return s
