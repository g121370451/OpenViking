import json
import re
from dataclasses import dataclass


@dataclass
class LLMResponse:
    sufficient: bool
    answer: str
    reasoning: str
    raw: str


def parse_llm_response(raw: str) -> LLMResponse:
    text = raw.strip()

    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()

    def _try_parse(s: str):
        try:
            obj = json.loads(s)
            sufficient = obj.get("sufficient", True)
            if isinstance(sufficient, str):
                sufficient = sufficient.lower() in ("true", "1", "yes")
            answer = str(obj.get("answer", "")).strip()
            reasoning = str(obj.get("reasoning", "")).strip()
            return LLMResponse(sufficient=sufficient, answer=answer, reasoning=reasoning, raw=raw)
        except (json.JSONDecodeError, ValueError):
            return None

    result = _try_parse(text)
    if result:
        return result

    match = re.search(r'\{[^{}]*"sufficient"\s*:', text)
    if match:
        brace_start = match.start()
        depth = 0
        for i in range(brace_start, len(text)):
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
                if depth == 0:
                    result = _try_parse(text[brace_start:i + 1])
                    if result:
                        return result
                    break

    answ = raw.strip()
    if answ:
        return LLMResponse(sufficient=True, answer=answ, reasoning="", raw=raw)
    return LLMResponse(sufficient=False, answer="", reasoning="parse failed", raw=raw)
