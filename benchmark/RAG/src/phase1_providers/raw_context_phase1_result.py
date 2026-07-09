import json
import re
from typing import Any

from core.response_parser import LLMResponse, parse_llm_response

from .base import Phase1Provider, Phase1ProviderResult, default_supplemental, timed


EVIDENCE_SUFFICIENCY_PROMPT = """You are judging whether the retrieved context contains enough direct evidence to answer the question. Do not answer the question yet.

Task:
1. Identify every required part of the question: answer type, entity, scope, date, version, count, comparison target, yes/no condition, and any other constraint.
2. Select only evidence that directly supports those required parts.
3. Decide whether the selected evidence is sufficient to answer the question without guessing.

Rules:
- Context relevance is not enough. The evidence must directly support every required part.
- Similar-but-different entities, versions, dates, counts, names, or scopes are not sufficient.
- If the answer requires a number, date, version, name, location, yes/no conclusion, or comparison result, that exact value or conclusion must be directly supported by selected evidence.
- If any required part is missing, weakly supported, inferred, ambiguous, or conflicting, set "sufficient" to false.
- Do not use external knowledge.
- Do not answer the question in this stage.

Question-type checklist:
- Definition questions: selected evidence must include the definition text itself. If exclusions, aliases, variants, or scope limits are needed to make the definition complete, they must also be selected.
- Yes/no questions: selected evidence must directly support the yes or no conclusion. Related context alone is not enough. A negative answer is valid only when the selected evidence covers the requested scope and supports absence, non-existence, non-mention, or no-change.
- Location, source, or section questions: selected evidence must identify both the requested location/source/section and enough nearby content to verify it is the right one.
- Comparison questions: selected evidence must cover every compared item and the comparison criterion.
- Change, version, or temporal questions: selected evidence must cover the relevant version/time scope and either the before/after change or an explicit no-change/absence conclusion.
- Multi-hop questions: selected evidence must cover every required hop and the link between hops.
- List/count questions: selected evidence must support the complete set or count, not just examples.
- Information-about or open-ended questions: selected evidence must cover the main facts requested by the question and any important conditions, limitations, exceptions, or triggers required to avoid a misleading partial answer.

Return JSON only:
{
  "sufficient": true/false,
  "requirements": ["<required part 1>", "<required part 2>"],
    "selected_evidence": [
    {
      "quote": "<exact quote from context>",
      "source_hint": "<URI, section, or context block identifier when available>",
      "why_relevant": "<short explanation of why this quote is selected>",
      "supports": "<which required part this quote supports>"
    }
  ],
  "missing_info": ["<missing or unsupported required part>"],
  "reasoning": "<one short sentence>"
}"""


SELECTED_EVIDENCE_ANSWER_PROMPT = """Answer the question using only the selected evidence below.

Rules:
- Do not use the original retrieved context.
- Do not use external knowledge.
- Do not add facts that are not directly supported by the selected evidence.
- If the selected evidence does not support the answer, set "answer" to "Not mentioned".

Return JSON only:
{
  "answer": "<final answer or Not mentioned>",
  "reasoning": "<one short sentence explaining the support>"
}"""


class RawContextPhase1ResultProvider(Phase1Provider):
    name = "raw_context_phase1_result"

    def run(self, qa, search_res: dict, **kwargs) -> Phase1ProviderResult:
        start = timed()
        provider_cfg = self.provider_config()

        stage1_prompt = self._build_evidence_sufficiency_prompt(qa, search_res)
        stage1_start = timed()
        stage1_raw = self.llm.generate(stage1_prompt)
        stage1_latency = timed() - stage1_start
        stage1_obj, stage1_parse_error = self.extract_json_object(stage1_raw)
        stage1_json_parse_failed = stage1_obj is None
        stage1_regex_recovered = False
        stage1_sufficient_regex_found = False
        stage1_used_raw_context_for_answer = False
        if stage1_obj is None:
            (
                stage1_obj,
                stage1_parse_error,
                stage1_regex_recovered,
                stage1_sufficient_regex_found,
                stage1_used_raw_context_for_answer,
            ) = self._recover_stage1_from_sufficient_regex(stage1_raw, search_res, stage1_parse_error)
        stage1 = self._normalize_stage1(stage1_obj)

        selected_evidence = stage1["selected_evidence"]
        requirements = stage1["requirements"]
        missing_info = stage1["missing_info"]
        stage1_sufficient = bool(stage1["sufficient"])
        parse_trigger = stage1_obj is None
        missing_info_trigger = bool(missing_info) and self.config_bool(
            provider_cfg.get("fallback_on_missing_info"),
            True,
        )
        no_evidence_trigger = stage1_sufficient and not selected_evidence
        insufficient_trigger = (
            self.config_bool(provider_cfg.get("fallback_on_insufficient"), True)
            and not stage1_sufficient
        )
        stage1_triggered = (
            parse_trigger
            or missing_info_trigger
            or no_evidence_trigger
            or insufficient_trigger
        )

        stage2_prompt = ""
        stage2_raw = ""
        stage2_latency = 0.0
        stage2_parsed = parse_llm_response("")
        answer = "Not mentioned"
        action_trigger = False
        stage2_refusal_trigger = False
        stage2_insufficient_trigger = False

        if not stage1_triggered:
            stage2_prompt, stage2_meta = self._build_selected_evidence_answer_prompt(qa, selected_evidence)
            stage2_start = timed()
            stage2_raw = self.llm.generate(stage2_prompt)
            stage2_latency = timed() - stage2_start
            stage2_parsed = parse_llm_response(stage2_raw)
            answer = self.adapter.post_process_answer(qa, stage2_parsed.answer, stage2_meta)
            action_text = str(stage2_parsed.action or "answer").strip().lower()
            action_trigger = (
                self.config_bool(provider_cfg.get("fallback_on_action_fallback"), True)
                and action_text == "fallback"
            )
            stage2_insufficient_trigger = (
                self.config_bool(provider_cfg.get("fallback_on_stage2_insufficient"), True)
                and not bool(stage2_parsed.sufficient)
            )
            stage2_refusal_trigger = self.is_refusal_like(answer)

        triggered = (
            stage1_triggered
            or action_trigger
            or stage2_insufficient_trigger
            or stage2_refusal_trigger
        )
        if parse_trigger:
            reasoning = f"Evidence sufficiency output could not be parsed: {stage1_parse_error}"
        elif insufficient_trigger:
            reasoning = stage1["reasoning"] or "Selected evidence is insufficient."
        elif missing_info_trigger:
            reasoning = "Evidence sufficiency stage reported missing information."
        elif no_evidence_trigger:
            reasoning = "Evidence sufficiency stage selected no direct evidence."
        elif action_trigger:
            reasoning = "Selected-evidence answer stage routed to fallback."
        elif stage2_insufficient_trigger:
            reasoning = "Selected-evidence answer stage marked the answer insufficient."
        elif stage2_refusal_trigger:
            reasoning = "Selected-evidence answer stage produced a refusal-like answer."
        else:
            reasoning = stage2_parsed.reasoning or stage1["reasoning"] or "Selected evidence supports the answer."

        final_answer = "Not mentioned" if triggered else answer
        combined_raw = self._combined_raw(stage1_raw, stage2_raw)
        parsed = LLMResponse(
            action="fallback" if triggered else "answer",
            sufficient=not triggered,
            answer=final_answer,
            reasoning=reasoning,
            evidence_analysis=self._evidence_analysis(requirements, selected_evidence, missing_info),
            missing_info=missing_info if triggered else [],
            raw=combined_raw,
        )

        full_prompt = self._combined_prompt(stage1_prompt, stage2_prompt)
        raw = combined_raw
        stage1_input_tokens = self.count_tokens(stage1_prompt)
        stage1_output_tokens = self.count_tokens(stage1_raw)
        stage2_input_tokens = self.count_tokens(stage2_prompt)
        stage2_output_tokens = self.count_tokens(stage2_raw)

        return Phase1ProviderResult(
            name=self.name,
            answer=final_answer,
            parsed=parsed,
            should_fallback=triggered,
            reasoning=reasoning,
            search_res=search_res,
            prompt=full_prompt,
            raw=raw,
            meta={
                "phase1_provider_mode": "two_stage_evidence_sufficiency",
                "stage1_sufficient": stage1_sufficient,
                "stage2_ran": bool(stage2_prompt),
            },
            supplemental=default_supplemental("Two-stage evidence sufficiency provider"),
            input_tokens=stage1_input_tokens + stage2_input_tokens,
            output_tokens=stage1_output_tokens + stage2_output_tokens,
            latency_sec=timed() - start,
            details={
                "rule": "two_stage_evidence_sufficiency_then_answer",
                "fallback_on_insufficient": self.config_bool(provider_cfg.get("fallback_on_insufficient"), True),
                "fallback_on_missing_info": self.config_bool(provider_cfg.get("fallback_on_missing_info"), True),
                "fallback_on_action_fallback": self.config_bool(provider_cfg.get("fallback_on_action_fallback"), True),
                "fallback_on_stage2_insufficient": self.config_bool(
                    provider_cfg.get("fallback_on_stage2_insufficient"),
                    True,
                ),
                "stage1_parse_error": stage1_parse_error,
                "stage1_json_parse_failed": stage1_json_parse_failed,
                "stage1_regex_recovered": stage1_regex_recovered,
                "stage1_sufficient_regex_found": stage1_sufficient_regex_found,
                "stage1_used_raw_context_for_answer": stage1_used_raw_context_for_answer,
                "stage1_sufficient": stage1_sufficient,
                "stage1_triggered": stage1_triggered,
                "stage1_latency_sec": stage1_latency,
                "stage1_input_tokens": stage1_input_tokens,
                "stage1_output_tokens": stage1_output_tokens,
                "stage2_ran": bool(stage2_prompt),
                "stage2_latency_sec": stage2_latency,
                "stage2_input_tokens": stage2_input_tokens,
                "stage2_output_tokens": stage2_output_tokens,
                "selected_evidence_count": len(selected_evidence),
                "requirements": requirements,
                "selected_evidence": selected_evidence,
                "missing_info": missing_info,
                "parse_trigger": parse_trigger,
                "insufficient_trigger": insufficient_trigger,
                "missing_info_trigger": missing_info_trigger,
                "no_evidence_trigger": no_evidence_trigger,
                "action_trigger": action_trigger,
                "stage2_insufficient_trigger": stage2_insufficient_trigger,
                "stage2_refusal_trigger": stage2_refusal_trigger,
            },
        )

    def _build_evidence_sufficiency_prompt(self, qa, search_res: dict) -> str:
        context_text = self._format_search_context(search_res)
        # Adapter-specific evidence guidance is intentionally disabled here so
        # the provider can be evaluated as a dataset-agnostic reviewer.
        selection_block = ""
        sufficiency_block = ""
        return (
            f"Retrieved context:\n{context_text}\n\n"
            f"{selection_block}"
            f"{sufficiency_block}"
            f"{EVIDENCE_SUFFICIENCY_PROMPT}\n\n"
            f"Question: {qa.question}"
        )

    def _build_selected_evidence_answer_prompt(self, qa, selected_evidence: list[str]) -> tuple[str, dict]:
        evidence_blocks = [
            f"[Selected evidence {idx}]\n{evidence}"
            for idx, evidence in enumerate(selected_evidence, start=1)
        ]
        build_prompt = getattr(self.adapter, "build_prompt", None)
        if callable(build_prompt):
            return build_prompt(qa, evidence_blocks)

        evidence_text = "\n\n".join(evidence_blocks)
        return (
            f"Selected evidence:\n{evidence_text}\n\n"
            f"{SELECTED_EVIDENCE_ANSWER_PROMPT}\n\n"
            f"Question: {qa.question}",
            {},
        )

    def _adapter_instruction(self, hook_name: str, qa) -> str:
        hook = getattr(self.adapter, hook_name, None)
        if not callable(hook):
            return ""
        return str(hook(qa) or "").strip()

    def _format_search_context(self, search_res: dict) -> str:
        recall_texts = search_res.get("recall_texts", {}) or {}
        retrieved_uris = list(search_res.get("retrieved_uris", []) or [])
        if retrieved_uris and recall_texts:
            blocks = []
            seen = set()
            for uri in retrieved_uris:
                if uri in seen:
                    continue
                seen.add(uri)
                content = str(recall_texts.get(uri, "") or "").strip()
                if content:
                    blocks.append(f"[Context block {len(blocks) + 1}]\nURI: {uri}\n{content[:8000]}")
            if blocks:
                return "\n\n".join(blocks)

        context_blocks = self.context_blocks(search_res)
        if not context_blocks:
            return "No retrieved context."
        return "\n\n".join(
            f"[Context block {idx}]\n{block}"
            for idx, block in enumerate(context_blocks, start=1)
        )

    def _recover_stage1_from_sufficient_regex(
        self,
        raw: str,
        search_res: dict,
        parse_error: str,
    ) -> tuple[dict, str, bool, bool, bool]:
        found, sufficient = self._regex_sufficient(raw)
        selected_evidence = self._raw_context_evidence(search_res) if found and sufficient else []
        used_raw_context = bool(selected_evidence)
        missing_info = [] if sufficient else ["Evidence sufficiency output could not be parsed as JSON."]
        obj = {
            "sufficient": bool(sufficient) if found else False,
            "requirements": [],
            "selected_evidence": selected_evidence,
            "missing_info": missing_info,
            "reasoning": (
                "Recovered sufficient=true from malformed stage1 JSON; using original context blocks as answer evidence."
                if used_raw_context
                else "Stage1 JSON could not be parsed and sufficient=true was not recoverable."
            ),
        }
        recovery_note = (
            f"regex sufficient fallback after {parse_error}; "
            f"sufficient_found={found}; sufficient={bool(sufficient) if found else False}; "
            f"used_raw_context={used_raw_context}"
        )
        return obj, recovery_note, True, found, used_raw_context

    def _regex_sufficient(self, raw: str) -> tuple[bool, bool]:
        match = re.search(
            r"""["']?sufficient["']?\s*:\s*(true|false)""",
            raw or "",
            re.IGNORECASE,
        )
        if not match:
            return False, False
        return True, match.group(1).lower() == "true"

    def _raw_context_evidence(self, search_res: dict) -> list[str]:
        recall_texts = search_res.get("recall_texts", {}) or {}
        retrieved_uris = list(search_res.get("retrieved_uris", []) or [])
        if retrieved_uris and recall_texts:
            evidence = []
            seen = set()
            for uri in retrieved_uris:
                if uri in seen:
                    continue
                seen.add(uri)
                content = str(recall_texts.get(uri, "") or "").strip()
                if content:
                    evidence.append(f"URI: {uri}\n{content[:8000]}")
            if evidence:
                return evidence

        return [
            f"Context block {idx}\n{block}"
            for idx, block in enumerate(self.context_blocks(search_res), start=1)
        ]

    def _normalize_stage1(self, obj: dict | None) -> dict:
        if not isinstance(obj, dict):
            return {
                "sufficient": False,
                "requirements": [],
                "selected_evidence": [],
                "missing_info": ["Evidence sufficiency output was not valid JSON."],
                "reasoning": "",
            }

        return {
            "sufficient": self._json_bool(obj.get("sufficient"), False),
            "requirements": self._string_list(obj.get("requirements")),
            "selected_evidence": self._selected_evidence_list(obj.get("selected_evidence")),
            "missing_info": self._string_list(obj.get("missing_info")),
            "reasoning": str(obj.get("reasoning", "") or "").strip(),
        }

    def _json_bool(self, value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        if value is None:
            return default
        return bool(value)

    def _string_list(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            items = value
        else:
            items = [value]
        result = []
        for item in items:
            text = str(item or "").strip()
            if text:
                result.append(text)
        return result

    def _selected_evidence_list(self, value: Any) -> list[str]:
        if value is None:
            return []
        items = value if isinstance(value, list) else [value]
        evidence = []
        for item in items:
            if isinstance(item, dict):
                quote = str(
                    item.get("quote")
                    or item.get("evidence")
                    or item.get("text")
                    or item.get("content")
                    or ""
                ).strip()
                source_hint = str(
                    item.get("source_hint")
                    or item.get("source")
                    or item.get("uri")
                    or item.get("section")
                    or ""
                ).strip()
                why_relevant = str(item.get("why_relevant") or item.get("why") or "").strip()
                supports = str(item.get("supports") or item.get("reason") or "").strip()
                if not quote:
                    continue
                parts = []
                if source_hint:
                    parts.append(f"Source hint: {source_hint}")
                parts.append(f"Quote: {quote}")
                if why_relevant:
                    parts.append(f"Why relevant: {why_relevant}")
                if supports:
                    parts.append(f"Supports: {supports}")
                evidence.append("\n".join(parts))
            else:
                text = str(item or "").strip()
                if text:
                    evidence.append(text)
        return evidence

    def _evidence_analysis(
        self,
        requirements: list[str],
        selected_evidence: list[str],
        missing_info: list[str],
    ) -> list[str]:
        requirement_text = "; ".join(requirements) if requirements else "Not specified by the model."
        evidence_text = " | ".join(selected_evidence) if selected_evidence else "No direct evidence selected."
        missing_text = "; ".join(missing_info) if missing_info else "None."
        return [
            f"Question requirements: {requirement_text}",
            f"Direct support: {evidence_text}",
            f"Unsupported or inferred parts: {missing_text}",
        ]

    def _combined_prompt(self, stage1_prompt: str, stage2_prompt: str) -> str:
        if not stage2_prompt:
            return f"[Stage 1: evidence sufficiency]\n{stage1_prompt}"
        return (
            f"[Stage 1: evidence sufficiency]\n{stage1_prompt}\n\n"
            f"[Stage 2: selected evidence answer]\n{stage2_prompt}"
        )

    def _combined_raw(self, stage1_raw: str, stage2_raw: str) -> str:
        return json.dumps(
            {
                "stage1_evidence_sufficiency_raw": stage1_raw,
                "stage2_answer_raw": stage2_raw,
            },
            ensure_ascii=False,
        )
