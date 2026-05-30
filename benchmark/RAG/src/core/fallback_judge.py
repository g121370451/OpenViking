from dataclasses import dataclass
from typing import List


REFUSAL_PATTERNS = [
    "not mentioned",
    "insufficient information",
    "i don't know",
    "i do not know",
    "cannot be determined",
    "not enough information",
    "no relevant information",
    "unable to find",
    "cannot answer",
    "no information available",
]


@dataclass
class JudgeVerdict:
    should_fallback: bool
    reasoning: str


def judge_answer(answer: str, refusal_patterns: List[str] = None) -> JudgeVerdict:
    patterns = refusal_patterns or REFUSAL_PATTERNS
    answer = answer.strip()

    answer_lower = answer.lower()
    for pattern in patterns:
        if pattern in answer_lower:
            return JudgeVerdict(
                should_fallback=True,
                reasoning=f"Refusal pattern detected: '{pattern}'",
            )

    return JudgeVerdict(
        should_fallback=False,
        reasoning="Answer looks valid",
    )
