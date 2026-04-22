import time
from typing import List, Optional
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, BaseMessage


class LLMClientWrapper:
    def __init__(self, config: dict, api_key: str):
        self.llm = ChatOpenAI(
            model=config['model'],
            temperature=config['temperature'],
            api_key=api_key,
            base_url=config['base_url']
        )
        self.retry_count = 3

    def generate(self, prompt: str) -> str:
        """Call LLM to generate answer with simple exponential backoff retry"""
        last_err = None
        for attempt in range(self.retry_count):
            try:
                resp = self.llm.invoke([HumanMessage(content=prompt)])
                return resp.content
            except Exception as e:
                last_err = e
                if attempt < self.retry_count - 1:
                    time.sleep(1.5 * (attempt + 1))

        raise RuntimeError(
            f"LLM generate failed after {self.retry_count} retries: {type(last_err).__name__}: {last_err}"
        ) from last_err

    def generate_with_tools(self, messages: List[BaseMessage], tools: List[dict], tool_choice: Optional[str] = "auto"):
        """Call LLM with tool definitions, returning AIMessage with possible tool_calls.

        Args:
            messages: LangChain message list (SystemMessage, HumanMessage, AIMessage, ToolMessage)
            tools: OpenAI-format tool definitions (list of dicts)
            tool_choice: Tool choice strategy ("auto", "none", "required", or specific tool name)

        Returns:
            AIMessage with .content and .tool_calls attributes
        """
        last_err = None
        bound = self.llm.bind_tools(tools, tool_choice=tool_choice)
        for attempt in range(self.retry_count):
            try:
                return bound.invoke(messages)
            except Exception as e:
                last_err = e
                if attempt < self.retry_count - 1:
                    time.sleep(1.5 * (attempt + 1))

        raise RuntimeError(
            f"LLM generate_with_tools failed after {self.retry_count} retries: {type(last_err).__name__}: {last_err}"
        ) from last_err
