import time
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage


class LLMClientWrapper:
    def __init__(self, config: dict, api_key: str):
        extra_body = self._build_extra_body(config)
        self.llm = ChatOpenAI(
            model=config['model'],
            temperature=config['temperature'],
            api_key=api_key,
            base_url=config['base_url'],
            **({"extra_body": extra_body} if extra_body else {}),
        )
        self.retry_count = 10

    def _build_extra_body(self, config: dict) -> dict:
        model = str(config.get("model", "") or "").lower()
        base_url = str(config.get("base_url", "") or "").lower()
        thinking = config.get("thinking", False)

        extra_body = dict(config.get("extra_body", {}) or {})
        is_volcengine = (
            "doubao" in model
            or "volcengine/" in model
            or "volces/" in model
            or model.startswith("ark/")
            or "volces.com" in base_url
            or "volcengine" in base_url
        )
        is_dashscope = "dashscope/" in model or "qwen" in model or "dashscope" in base_url

        if is_volcengine:
            extra_body["thinking"] = {"type": "enabled" if self._config_bool(thinking, False) else "disabled"}
        elif is_dashscope:
            extra_body["enable_thinking"] = self._config_bool(thinking, False)

        return extra_body

    def _config_bool(self, value, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "enabled")
        return bool(value)

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
