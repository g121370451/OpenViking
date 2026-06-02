import time
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage


class LLMClientWrapper:
    def __init__(self, config: dict, api_key: str):
        self.llm = ChatOpenAI(
            model=config['model'],
            temperature=config['temperature'],
            api_key=api_key,
            base_url=config['base_url']
        )
        self.retry_count = 10

    def generate(self, prompt: str) -> str:
        last_err = None
        for attempt in range(self.retry_count):
            try:
                resp = self.llm.invoke([HumanMessage(content=prompt)])
                return resp.content
            except Exception as e:
                last_err = e
                if "429" in str(e) or "RateLimit" in str(e) or "TooManyRequests" in str(e) or "TPM" in str(e):
                    delay = 5.0 * (2 ** min(attempt, 6))
                    print(f"[LLM] Rate limited, retry {attempt + 1}/{self.retry_count} after {delay:.1f}s")
                    time.sleep(delay)
                else:
                    if attempt < self.retry_count - 1:
                        time.sleep(1.5 * (attempt + 1))

        raise RuntimeError(
            f"LLM generate failed after {self.retry_count} retries: {type(last_err).__name__}: {last_err}"
        ) from last_err
