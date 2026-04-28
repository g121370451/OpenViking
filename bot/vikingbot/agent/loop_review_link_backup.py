"""
Backup: LLM 审阅驱动的链接机制 (Review-Driven Link Building)

状态: 已暂存 (2026-04-25)
原因: 在 locomo 数据集上效果不好，需对比盲链接效果。

核心思路:
  收集 URI + 内容摘要 → self.provider.chat(审阅提示词) → 只对 LLM 选中的 pairs 调用 client.link()

如需恢复，将下方 _post_answer_link_review 方法复制回 loop.py 的 VikingAgent 类中。
"""


async def _post_answer_link_review(
    self,
    tools_used: list[dict],
    original_question: str,
    session_key: "SessionKey",
) -> None:
    """LLM-driven link building: review read docs and only link genuinely related pairs."""
    try:
        read_tools = {"openviking_multi_read", "openviking_read"}
        logger.info(f"[PostAnswerLink] origin query is {original_question}")

        # Build uri -> content summary mapping from read tool results
        uri_content: dict[str, str] = {}
        for tool in tools_used:
            if not (tool.get("tool_name") in read_tools and tool.get("execute_success")):
                continue
            result = tool.get("result", "")
            if not result:
                continue
            args = tool.get("args", "")
            uris = self._extract_uris_from_args(args)

            if tool.get("tool_name") == "openviking_multi_read" and uris:
                import re as _re_mr
                for uri in uris:
                    escaped = _re_mr.escape(uri)
                    m = _re_mr.search(
                        rf'--- START OF {escaped} ---(.*?)--- END OF {escaped} ---', result, _re_mr.DOTALL
                    )
                    if m:
                        uri_content[uri] = m.group(1).strip()[:500]
                    elif uri not in uri_content:
                        uri_content[uri] = ""
            elif tool.get("tool_name") == "openviking_read" and uris:
                uri = uris[0]
                if uri not in uri_content:
                    uri_content[uri] = result.strip()[:500]

        unique_uris = list(uri_content.keys())
        logger.info(f"[PostAnswerLink] Total unique URIs read: {len(unique_uris)}")

        if len(unique_uris) < 2:
            logger.info(f"[PostAnswerLink] Skipped: only {len(unique_uris)} unique URI(s), need >= 2")
            return

        # Ask LLM to review and select related pairs
        doc_list = "\n".join(
            f"- {uri}: {summary[:300]}" for uri, summary in uri_content.items()
        )
        review_prompt = f"""You are building a knowledge graph. Given a question and a list of documents that were read to answer it, identify which document pairs are genuinely related and should be linked.

Question: {original_question}

Documents read:
{doc_list}

Return ONLY a JSON array. Each element must have:
  "from_uri": source document URI (must be one of the URIs listed above)
  "to_uri": target document URI (must be a different URI from the list above)
  "reason": specific reason describing the relationship (e.g. "both discuss the Battle of Waterloo in 1815")

If no documents are genuinely related, return an empty array [].
Do not link documents that merely share a single keyword or appear in the same search results."""

        messages = [{"role": "user", "content": review_prompt}]
        try:
            response = await self.provider.chat(
                messages=messages,
                tools=None,
                model=self.model,
                temperature=0.0,
            )
            content = response.content or ""
        except Exception as e:
            logger.warning(f"[PostAnswerLink] LLM review call failed: {e}")
            return

        # Parse LLM response
        try:
            pairs = json.loads(content)
            if not isinstance(pairs, list):
                logger.warning(f"[PostAnswerLink] LLM returned non-array: {content[:200]}")
                return
        except json.JSONDecodeError:
            import re as _re_json
            m = _re_json.search(r'\[.*\]', content, re.DOTALL)
            if m:
                try:
                    pairs = json.loads(m.group())
                except json.JSONDecodeError:
                    logger.warning(f"[PostAnswerLink] Failed to parse LLM response: {content[:200]}")
                    return
            else:
                logger.warning(f"[PostAnswerLink] No JSON array found in: {content[:200]}")
                return

        if not pairs:
            logger.info("[PostAnswerLink] LLM returned no pairs to link")
            return

        # Validate and link
        valid_uris = set(unique_uris)
        from vikingbot.openviking_mount.ov_server import VikingClient
        workspace_id = self.sandbox_manager.to_workspace_id(session_key) if self.sandbox_manager else None
        client = await VikingClient.create(workspace_id)

        linked = 0
        for pair in pairs:
            from_uri = pair.get("from_uri", "")
            to_uri = pair.get("to_uri", "")
            reason = pair.get("reason", "related")

            if from_uri not in valid_uris or to_uri not in valid_uris:
                logger.warning(f"[PostAnswerLink] Skipping invalid pair: {from_uri} <-> {to_uri}")
                continue
            if from_uri == to_uri:
                continue

            try:
                logger.info(f"[PostAnswerLink] Linking: {from_uri} <-> {to_uri} (reason: {reason})")
                await client.link(from_uri, [to_uri], reason=reason, query=original_question)
                linked += 1
            except Exception as e:
                logger.warning(f"[PostAnswerLink] Link failed: {e}")

        if linked:
            logger.info(f"[PostAnswerLink] Created {linked} relation(s) from {len(pairs)} LLM-suggested pairs")
    except Exception as e:
        logger.warning(f"Post-answer linking failed: {e}")
