# Copyright (C) 2025-2026 Shu Wang
# SPDX-License-Identifier: Apache-2.0 OR AGPL-3.0-only

from concurrent.futures import ThreadPoolExecutor
import inspect
from typing import Optional, Set

from bookrag_core.Index.Tree import TreeNode, NodeType, DocumentTree
from bookrag_core.prompts.summary_prompt import NODE_SUMMARY_PROMPT, SEC_SUMMARY_PROMPT
from bookrag_core.pipelines.summary_journal import (
    SummaryJournal,
    is_valid_summary,
    summary_prompt_fingerprint,
)
from bookrag_core.provider.llm import LLM
from bookrag_core.provider.vlm import VLM
from bookrag_core.utils.utils import num_tokens, TextProcessor
import os
import logging
from tqdm import tqdm

log = logging.getLogger(__name__)


def _get_nodes_by_level(node, current_level=0, level_dict=None):
    """Get root-reachable nodes organized by depth."""
    if level_dict is None:
        level_dict = {}
    level_dict.setdefault(current_level, []).append(node)
    for child in node.children:
        _get_nodes_by_level(child, current_level + 1, level_dict)
    return level_dict


def get_node_summary_prompt(tree_node: TreeNode, max_token: int) -> str:
    node_type = tree_node.type
    if node_type not in [
        NodeType.TEXT,
        NodeType.IMAGE,
        NodeType.TABLE,
        NodeType.EQUATION,
    ]:
        log.warning(f"Node type {node_type} is not supported for summary generation.")
        return ""

    if node_type in [NodeType.IMAGE, NodeType.TABLE]:
        content = (
            "This is an image." if node_type == NodeType.IMAGE else "This is a table."
        )
        content += "Here is the caption: "
        content += tree_node.meta_info.content or ""
        content += (
            f"\n{tree_node.meta_info.table_body}" if node_type == NodeType.TABLE else ""
        )
    else:
        content = tree_node.meta_info.content or ""

    # 2. 【新增】检查并截断内容
    base_prompt_tokens = num_tokens(NODE_SUMMARY_PROMPT.format(node_text=""))
    available_tokens = max_token - base_prompt_tokens
    if num_tokens(content) > available_tokens:
        log.warning(
            f"Content length ({num_tokens(content)} tokens) exceeds max_token ({max_token}). Truncating."
        )
        # 调用静态方法进行切分
        chunks = TextProcessor.split_text_into_chunks(text=content, max_length=max_token)
        # 只取第一个分片
        content = chunks[0] if chunks else ""

    prompt = NODE_SUMMARY_PROMPT.format(node_text=content)
    return prompt


def generate_node_summary(
    tree_node: TreeNode, llm: LLM, use_VLM: bool = False, vlm: Optional[VLM] = None
) -> str:
    """Generate a summary for a single tree node.
    This function uses the LLM to generate a summary based on the node's content.
    If the node is an image or table, it will use the VLM if provided.
    """
    node_type = tree_node.type
    prompt = get_node_summary_prompt(tree_node, max_token=llm.config.max_tokens)

    if use_VLM and vlm is not None and node_type in [NodeType.IMAGE, NodeType.TABLE]:
        # Use VLM for image or table nodes
        image_path = tree_node.meta_info.img_path
        if not os.path.exists(image_path):
            log.warning(
                f"Image path {image_path} does not exist for node {tree_node.index_id}."
            )
            return ""
        summary = vlm.generate(prompt_or_memory=prompt, images=[image_path])
    else:
        summary = llm.get_completion(prompt=prompt, json_response=False)

    if not summary:
        log.warning(f"Failed to generate summary for node {tree_node.index_id}.")
        return ""
    log.info(f"Generated summary for node {tree_node.index_id}: {summary}")
    return summary.strip()


def get_sec_summary_prompt(sec_node: TreeNode, max_token: int) -> str:
    """Get the prompt for generating a section summary.
    This function formats the section text and its immediate children's summaries into a prompt.
    """
    base_prompt_tokens = num_tokens(
        SEC_SUMMARY_PROMPT.format(section_text="", content_summary="")
    )
    available_tokens = max_token - base_prompt_tokens

    if available_tokens <= 0:
        log.warning(
            f"max_token ({max_token}) is too small to even fit the prompt template."
        )
        return ""

    # Get initial content
    section_text = sec_node.meta_info.content or ""

    def get_children_text(node: TreeNode) -> str:
        """Recursively get text from all immediate children nodes."""
        if not node.children:
            return ""
        children_text = []
        child_prefix = {
            NodeType.TEXT: "Text: ",
            NodeType.IMAGE: "Image: ",
            NodeType.TABLE: "Table: ",
            NodeType.EQUATION: "Equation: ",
        }
        for child in node.children:
            child_text = child.summary or child.meta_info.content or ""
            child_type = child.type
            if child_text:
                children_text.append(f"{child_prefix.get(child_type, '')}{child_text}")
        return "\n".join(children_text)

    children_text = get_children_text(sec_node)

    # Truncation Logic
    section_tokens = num_tokens(section_text)

    if section_tokens >= available_tokens:
        # If section_text alone is too long, truncate it and use no children_text
        log.warning(
            f"Section text ({section_tokens} tokens) exceeds available tokens ({available_tokens}). Truncating section text and omitting children summaries."
        )
        chunks = TextProcessor.split_text_into_chunks(
            text=section_text, max_length=available_tokens
        )
        section_text = chunks[0] if chunks else ""
        children_text = ""
    else:
        # If section_text fits, see how much of children_text can be included
        remaining_tokens = available_tokens - section_tokens
        children_tokens = num_tokens(children_text)
        if children_tokens > remaining_tokens:
            log.warning(
                f"Children summaries ({children_tokens} tokens) exceed remaining tokens ({remaining_tokens}). Truncating children summaries."
            )
            chunks = TextProcessor.split_text_into_chunks(
                text=children_text, max_length=remaining_tokens
            )
            children_text = chunks[0] if chunks else ""

    return SEC_SUMMARY_PROMPT.format(
        section_text=section_text, content_summary=children_text
    )


def generate_section_summary(sec_node: TreeNode, llm: LLM) -> str:
    """Generate a summary for a section node.
    This function uses the LLM to generate a summary based on the section's content and its children.
    It includes text from the section itself and summaries of its immediate children.
    """
    prompt = get_sec_summary_prompt(sec_node, llm.config.max_tokens)
    summary = llm.get_completion(prompt=prompt, json_response=False)
    return summary


def _summary_prompt_for_node(node: TreeNode, max_token: int) -> str:
    if node.children:
        return get_sec_summary_prompt(node, max_token)
    return get_node_summary_prompt(node, max_token=max_token)


def replay_tree_node_summary_journal(
    tree_index: DocumentTree,
    *,
    max_token: int,
    journal: SummaryJournal,
) -> dict[str, int]:
    """Replay matching per-node records from children to parents."""
    records_by_node = journal.records_by_node()
    if not records_by_node:
        return {"applied": 0, "changed": 0, "invalidated_ancestors": 0}

    level_dict = _get_nodes_by_level(tree_index.root_node)
    changed_nodes: set[int] = set()
    applied = 0
    changed = 0
    invalidated_ancestors = 0

    for level in sorted(level_dict.keys(), reverse=True):
        for node in level_dict[level]:
            if node.type == NodeType.ROOT:
                continue
            child_changed = any(
                child.index_id in changed_nodes for child in node.children
            )
            prompt = _summary_prompt_for_node(node, max_token)
            prompt_fingerprint = summary_prompt_fingerprint(prompt)
            matching_record = next(
                (
                    record
                    for record in reversed(records_by_node.get(node.index_id, []))
                    if record["prompt_fingerprint"] == prompt_fingerprint
                ),
                None,
            )
            if matching_record is not None:
                summary = str(matching_record["summary"]).strip()
                applied += 1
                if node.summary != summary:
                    node.summary = summary
                    changed += 1
                    changed_nodes.add(node.index_id)
                continue

            # A parent summary persisted before a child changed is stale unless
            # the journal also contains a result for the parent's new prompt.
            if child_changed and node.summary:
                node.summary = ""
                invalidated_ancestors += 1
                changed_nodes.add(node.index_id)

    return {
        "applied": applied,
        "changed": changed,
        "invalidated_ancestors": invalidated_ancestors,
    }


def generate_tree_node_summary(
    tree_index: DocumentTree,
    llm: LLM,
    use_VLM: bool = False,
    vlm: Optional[VLM] = None,
    target_node_ids: Optional[Set[int]] = None,
    summary_journal: Optional[SummaryJournal] = None,
    request_batch_size: int = 250,
) -> DocumentTree:
    """Generate summaries for each node in the tree index.
    The generating order is from the leaf nodes to the root node.
    """
    # log.info("Generating summaries for tree nodes...")

    # Get all nodes organized by level from bottom to top
    level_dict = _get_nodes_by_level(tree_index.root_node)
    # log.info(f"Processing tree with {len(level_dict)} levels")
    work_levels = [
        (
            level,
            [
                node
                for node in level_dict[level]
                if node.type != NodeType.ROOT
                and (
                    target_node_ids is None
                    or node.index_id in target_node_ids
                )
            ],
        )
        for level in sorted(level_dict.keys(), reverse=True)
    ]
    work_levels = [(level, nodes) for level, nodes in work_levels if nodes]
    total_nodes = sum(len(nodes) for _, nodes in work_levels)
    summary_workers = max(1, int(getattr(llm, "max_workers", 1)))
    llm_batch_parameters = inspect.signature(
        llm.batch_get_completion
    ).parameters
    llm_accepts_executor = "executor" in llm_batch_parameters
    llm_accepts_result_callback = "result_callback" in llm_batch_parameters
    vlm_accepts_executor = bool(
        use_VLM
        and vlm is not None
        and "executor" in inspect.signature(vlm.batch_generate).parameters
    )
    vlm_accepts_result_callback = bool(
        use_VLM
        and vlm is not None
        and "result_callback" in inspect.signature(vlm.batch_generate).parameters
    )
    if summary_journal is None and tree_index.save_dir:
        summary_journal = SummaryJournal(tree_index.save_dir, tree_index)

    log.info(
        "Summary plan: total_tree_nodes=%d, structural_roots=%d, "
        "summary_targets=%d, levels=%d, workers=%d.",
        len(tree_index.nodes),
        sum(node.type == NodeType.ROOT for node in tree_index.nodes),
        total_nodes,
        len(work_levels),
        summary_workers,
    )

    # Process nodes from the bottom level to the top
    with ThreadPoolExecutor(
        max_workers=summary_workers,
        thread_name_prefix="bookrag-summary",
    ) as summary_executor, tqdm(
        total=total_nodes,
        desc="BookRAG summaries",
        unit="node",
        dynamic_ncols=True,
    ) as progress:
        for level_number, (level, level_nodes) in enumerate(work_levels, start=1):
            progress.set_postfix_str(
                f"level {level_number}/{len(work_levels)}, depth={level}, "
                f"workers={summary_workers}",
                refresh=True,
            )
            log.info(f"Processing level {level} with {len(level_nodes)} summary nodes.")
            # Initialize lists for LLM and VLM prompts
            llm_prompt_list = []
            llm_node_idx_list = []

            vlm_prompt_list = []
            vlm_images_list = []
            vlm_node_idx_list = []

            for node in level_nodes:
                children_len = len(node.children)
                if children_len == 0:
                    # Leaf node, generate summary directly
                    summary_prompt = get_node_summary_prompt(
                        node, max_token=llm.config.max_tokens
                    )
                    if use_VLM and node.type in [NodeType.IMAGE, NodeType.TABLE]:
                        # Use VLM for image or table nodes
                        image_path = node.meta_info.img_path
                        if not os.path.exists(image_path):
                            log.warning(
                                f"Image path {image_path} does not exist for node {node.index_id}."
                            )
                            continue
                        vlm_prompt_list.append(summary_prompt)
                        vlm_images_list.append(image_path)
                        vlm_node_idx_list.append(node.index_id)
                    else:
                        # Use LLM for text nodes or if VLM is not used
                        llm_prompt_list.append(summary_prompt)
                        llm_node_idx_list.append(node.index_id)
                else:
                    # Non-leaf node, prepare for section summary
                    summary_prompt = get_sec_summary_prompt(node, llm.config.max_tokens)
                    llm_prompt_list.append(summary_prompt)
                    llm_node_idx_list.append(node.index_id)

            # Keep request submission bounded. Durability is per result through
            # SummaryJournal and is independent of this request batch size.
            if request_batch_size < 1:
                raise ValueError("request_batch_size must be positive")
            if llm_prompt_list:
                log.info(
                    f"Generating summaries for {len(llm_prompt_list)} nodes using LLM."
                )
                for batch_start in range(
                    0, len(llm_prompt_list), request_batch_size
                ):
                    batch_end = batch_start + request_batch_size
                    batch_prompts = llm_prompt_list[batch_start:batch_end]
                    batch_node_ids = llm_node_idx_list[batch_start:batch_end]
                    def persist_llm_result(position: int, value: str) -> None:
                        node_id = batch_node_ids[position]
                        node = tree_index.get_node_by_index_id(node_id)
                        if node is None:
                            raise RuntimeError(
                                f"Node with ID {node_id} not found in the tree index"
                            )
                        if not is_valid_summary(value):
                            log.warning(
                                "Summary result for node %d is invalid and was not persisted: %s",
                                node_id,
                                value,
                            )
                            return
                        summary = str(value).strip()
                        if summary_journal is not None:
                            summary = summary_journal.append(
                                node_id=node_id,
                                prompt=batch_prompts[position],
                                summary=summary,
                            )
                        node.summary = summary
                        progress.update(1)

                    batch_kwargs = {
                        "prompts": batch_prompts,
                        "json_response": False,
                    }
                    if llm_accepts_executor:
                        batch_kwargs["executor"] = summary_executor
                    if llm_accepts_result_callback:
                        batch_kwargs["result_callback"] = persist_llm_result
                    llm_summaries = llm.batch_get_completion(**batch_kwargs)
                    if not llm_accepts_result_callback:
                        for position, summary in enumerate(llm_summaries):
                            persist_llm_result(position, summary)

            # Generate summaries using VLM if applicable. The VLM batch API does
            # not expose per-future callbacks, so this advances after the batch.
            if use_VLM and vlm_prompt_list:
                log.info(
                    f"Generating summaries for {len(vlm_prompt_list)} nodes using VLM."
                )
                for batch_start in range(
                    0, len(vlm_prompt_list), request_batch_size
                ):
                    batch_end = batch_start + request_batch_size
                    batch_prompts = vlm_prompt_list[batch_start:batch_end]
                    batch_images = vlm_images_list[batch_start:batch_end]
                    batch_node_ids = vlm_node_idx_list[batch_start:batch_end]
                    def persist_vlm_result(position: int, value: str) -> None:
                        node_id = batch_node_ids[position]
                        node = tree_index.get_node_by_index_id(node_id)
                        if node is None:
                            raise RuntimeError(
                                f"Node with ID {node_id} not found in the tree index"
                            )
                        if not is_valid_summary(value):
                            log.warning(
                                "VLM summary result for node %d is invalid and was not persisted: %s",
                                node_id,
                                value,
                            )
                            return
                        summary = str(value).strip()
                        if summary_journal is not None:
                            summary = summary_journal.append(
                                node_id=node_id,
                                prompt=batch_prompts[position],
                                summary=summary,
                            )
                        node.summary = summary
                        progress.update(1)

                    vlm_kwargs = {
                        "queries": batch_prompts,
                        "images_list": batch_images,
                        "max_workers": summary_workers,
                    }
                    if vlm_accepts_executor:
                        vlm_kwargs["executor"] = summary_executor
                    if vlm_accepts_result_callback:
                        vlm_kwargs["result_callback"] = persist_vlm_result
                    vlm_summaries = vlm.batch_generate(**vlm_kwargs)
                    if not vlm_accepts_result_callback:
                        for position, summary in enumerate(vlm_summaries):
                            persist_vlm_result(position, summary)

    log.info("All node summaries generated successfully.")
    # Return the updated tree index with summaries

    return tree_index


if __name__ == "__main__":
    DEBUG = False
    if DEBUG:
        logging.basicConfig(
            level=logging.INFO,  # 或 logging.DEBUG
            format="%(asctime)s %(levelname)s %(message)s",
        )
    tmp_path = "/home/wangshu/multimodal/GBC-RAG/test/tree_index"
    tree_index = DocumentTree.load_from_file(DocumentTree.get_save_path(tmp_path))
    from bookrag_core.configs.system_config import load_system_config

    cfg = load_system_config("/home/wangshu/multimodal/GBC-RAG/config/default.yaml")

    llm = LLM(llm_config=cfg.llm)

    tree_index = generate_tree_node_summary(tree_index=tree_index, llm=llm)
    one_step_index_1 = tree_index.get_one_depth_summary(1)
    print(f"Node ID: 1, Summary: \n")
    print(one_step_index_1)
