# Copyright (C) 2025-2026 Shu Wang
# SPDX-License-Identifier: Apache-2.0 OR AGPL-3.0-only

from typing import Dict, Optional, Any, List, Union, Callable, Type
from abc import ABC, abstractmethod
from openai import OpenAI
import json
import logging
import re
from pydantic import BaseModel
from concurrent.futures import ThreadPoolExecutor, as_completed
from bookrag_core.Common.Memory import Memory
from bookrag_core.configs.llm_config import LLMConfig
from bookrag_core.provider.retry import call_with_retry
from bookrag_core.utils.utils import get_max_output_tokens
from bookrag_core.provider.TokenTracker import TokenTracker

log = logging.getLogger(__name__)

_JSON_CODE_FENCE = re.compile(
    r"^\s*```(?:json)?\s*(?P<content>.*?)\s*```\s*$",
    flags=re.IGNORECASE | re.DOTALL,
)


def parse_json_completion(content: str, schema: Type[BaseModel]) -> BaseModel:
    """Parse an OpenAI-compatible JSON response and validate its schema.

    Some compatible providers ignore structured-output formatting and wrap an
    otherwise valid object in a Markdown JSON code fence.  The OpenAI SDK's
    beta ``parse`` helper validates before returning the raw content, so callers
    cannot recover from that response.  Parse the raw content here instead and
    use ``json-repair`` only when strict JSON decoding fails.
    """
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Empty JSON response from LLM")

    cleaned = content.strip().lstrip("\ufeff")
    fence_match = _JSON_CODE_FENCE.match(cleaned)
    if fence_match:
        cleaned = fence_match.group("content").strip()

    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as strict_error:
        from json_repair import repair_json

        payload = repair_json(cleaned, return_objects=True)
        if payload in (None, ""):
            raise strict_error
        log.debug("Repaired a non-strict JSON completion before schema validation.")

    return schema.model_validate(payload)


class BaseLLMController(ABC):
    @abstractmethod
    def _prepare_messages(
        self, prompt_or_memory: Union[str, Memory], images: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """
        Prepares the message list in the format required by the specific API.
        This must be implemented by each subclass.
        """
        pass

    @abstractmethod
    def get_completion(self, prompt_or_memory: Union[str, Memory]) -> str:
        """Get completion from LLM."""
        pass

    @abstractmethod
    def get_json_completion(
        self,
        prompt_or_memory: Union[str, Memory],
        schema: BaseModel,
        images: Optional[List[str]] = None,
    ) -> dict:
        """Get structured JSON response from LLM using Pydantic schema."""
        pass


class OpenAIController(BaseLLMController):
    def __init__(
        self,
        llm_config: Optional[LLMConfig] = None,
    ):
        self.model = (
            llm_config.model_name
            if llm_config and hasattr(llm_config, "model_name")
            else "gpt-3.5-turbo"
        )
        self.max_tokens = (
            llm_config.max_tokens
            if llm_config and hasattr(llm_config, "max_tokens")
            else 4000
        )
        self.temperature = (
            llm_config.temperature
            if llm_config and hasattr(llm_config, "temperature")
            else 0.7
        )
        base_url = (
            llm_config.api_base
            if llm_config and hasattr(llm_config, "api_base")
            else None
        )
        self.frequency_penalty = (
            llm_config.frequency_penalty
            if llm_config and hasattr(llm_config, "frequency_penalty")
            else 0.0
        )
        self.presence_penalty = (
            llm_config.presence_penalty
            if llm_config and hasattr(llm_config, "presence_penalty")
            else 0.0
        )

        if base_url is not None:
            self.client = OpenAI(api_key=llm_config.api_key, base_url=base_url)
        else:
            self.client = OpenAI(api_key=llm_config.api_key)

    def _prepare_messages(
        self, prompt_or_memory: Union[str, Memory], add_system_prompt: bool = True
    ) -> List[Dict[str, Any]]:
        """Prepares messages for the OpenAI API format."""
        if isinstance(prompt_or_memory, Memory):
            res_dict = [msg.to_dict() for msg in prompt_or_memory.storage]
            return res_dict

        messages = []
        if add_system_prompt:
            messages.append(
                {"role": "system", "content": "You are a helpful assistant."}
            )
        messages.append({"role": "user", "content": prompt_or_memory})
        return messages

    def get_completion(
        self, prompt_or_memory: Union[str, Memory], json_response: bool = False
    ) -> str:
        messages = self._prepare_messages(prompt_or_memory)
        parameters = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": get_max_output_tokens(messages, self.max_tokens),
            "frequency_penalty": self.frequency_penalty,
            "presence_penalty": self.presence_penalty,
            "extra_body": {
                # Volcengine Ark's OpenAI-compatible API uses this field.
                "thinking": {"type": "disabled"},
                # Keep the vLLM-compatible switch for other deployments.
                "chat_template_kwargs": {"enable_thinking": False},
            },
        }
        if json_response:
            parameters["response_format"] = {"type": "json_object"}
        response = self.client.chat.completions.create(**parameters)

        if response.usage:
            tracker = TokenTracker.get_instance()
            tracker.add_usage(
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
            )

        return response.choices[0].message.content

    def get_json_completion(
        self,
        prompt_or_memory: Union[str, Memory],
        schema: BaseModel,
        images: Optional[list] = None,
        think_mode: bool = False,
    ) -> dict:
        """
        Get structured JSON response from OpenAI using Pydantic schema.
        :param prompt: str
        :param schema: Pydantic BaseModel
        :param images: Optional[list] = None
        :return: dict
        """
        if isinstance(prompt_or_memory, Memory):
            # If it's a memory object, use its history.
            # We will attach images to the content of the *last* message.
            messages = [msg.to_dict() for msg in prompt_or_memory.storage]
            last_content = messages[-1]["content"]

            content_list = [{"type": "text", "text": last_content}]
            if images:
                for img_url in images:
                    content_list.append(
                        {"type": "image_url", "image_url": {"url": img_url}}
                    )
            messages[-1]["content"] = content_list

        else:  # It's a simple string prompt
            content_list = [{"type": "text", "text": prompt_or_memory}]
            if images:
                for img_url in images:
                    content_list.append(
                        {"type": "image_url", "image_url": {"url": img_url}}
                    )
            messages = [{"role": "user", "content": content_list}]

        completion = self.client.chat.completions.create(
            temperature=self.temperature,
            model=self.model,
            messages=messages,
            response_format={"type": "json_object"},
            extra_body={
                "thinking": {
                    "type": "enabled" if think_mode else "disabled",
                },
                "chat_template_kwargs": {"enable_thinking": think_mode},
            },
        )

        if completion.usage:
            tracker = TokenTracker.get_instance()
            tracker.add_usage(
                prompt_tokens=completion.usage.prompt_tokens,
                completion_tokens=completion.usage.completion_tokens,
            )

        message = completion.choices[0].message
        refusal = getattr(message, "refusal", None)
        if refusal:
            raise RuntimeError(f"LLM refused the JSON request: {refusal}")
        return parse_json_completion(message.content, schema)


class OllamaController(BaseLLMController):
    def __init__(self, llm_config: Optional[LLMConfig] = None):
        import ollama

        self.model = (
            llm_config.model_name
            if llm_config and hasattr(llm_config, "model_name")
            else "llama3.1"
        )
        self.max_tokens = (
            llm_config.max_tokens
            if llm_config and hasattr(llm_config, "max_tokens")
            else 4000
        )
        self.temperature = (
            llm_config.temperature
            if llm_config and hasattr(llm_config, "temperature")
            else 0.7
        )
        self.frequency_penalty = (
            llm_config.frequency_penalty
            if llm_config and hasattr(llm_config, "frequency_penalty")
            else 0.0
        )
        self.presence_penalty = (
            llm_config.presence_penalty
            if llm_config and hasattr(llm_config, "presence_penalty")
            else 0.0
        )
        self.api_base = (
            llm_config.api_base
            if llm_config and hasattr(llm_config, "api_base")
            else None
        )
        self.api_key = (
            llm_config.api_key
            if llm_config and hasattr(llm_config, "api_key")
            else None
        )
        self.client = ollama.Client(host=self.api_base)

    def _prepare_messages(
        self, prompt_or_memory: Union[str, Memory], add_system_prompt: bool = True
    ) -> List[Dict[str, Any]]:
        """Prepares messages for the Ollama API format."""
        if isinstance(prompt_or_memory, Memory):
            return prompt_or_memory.get()

        messages = []
        if add_system_prompt:
            messages.append(
                {"role": "system", "content": "You are a helpful assistant."}
            )
        messages.append({"role": "user", "content": prompt_or_memory})
        return messages

    def get_completion(
        self, prompt_or_memory: Union[str, Memory], json_response: bool = False
    ) -> str:
        messages = self._prepare_messages(prompt_or_memory)
        response = self.client.chat(
            model=self.model,
            messages=messages,
            options={
                "temperature": self.temperature,
            },
            think=False,
        )

        if response:
            tracker = TokenTracker.get_instance()
            tracker.add_usage(
                prompt_tokens=response.get("prompt_eval_count", 0),
                completion_tokens=response.get("eval_count", 0),
            )

        return response["message"]["content"]

    def get_json_completion(
        self,
        prompt_or_memory: Union[str, Memory],
        schema: BaseModel,
        images: Optional[list] = None,
        think_mode: bool = False,
    ) -> dict:
        """
        Get structured JSON response from Ollama using Pydantic schema.
        :param prompt: str
        :param schema: Pydantic BaseModel
        :param images: Optional[list] = None
        :return: dict
        """
        messages = self._prepare_messages(prompt_or_memory)

        if images:
            # Ollama expects images at the top level of the message dictionary
            messages[-1]["images"] = images
        response = self.client.chat(
            model=self.model,
            messages=messages,
            format=schema.model_json_schema(),  # 关键：传递schema
            options={
                "temperature": self.temperature,
            },
        )

        if response:
            tracker = TokenTracker.get_instance()
            prompt_tokens = response.get("prompt_eval_count", 0)
            completion_tokens = response.get("eval_count", 0)
            try:
                prompt_tokens = int(prompt_tokens)
            except Exception as e:
                logging.error(f"Error converting prompt_tokens: {e}")
                prompt_tokens = 0

            try:
                completion_tokens = int(completion_tokens)
            except Exception as e:
                logging.error(f"Error converting completion_tokens: {e}")
                completion_tokens = 0
            tracker.add_usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

        return schema.model_validate_json(response["message"]["content"])


class LLM:
    """LLM-based controller for memory metadata generation"""

    def __init__(
        self,
        llm_config: Optional[LLMConfig] = None,
    ):
        if llm_config is None:
            raise ValueError("Config must be provided")
        if not isinstance(llm_config, LLMConfig):
            config = LLMConfig(**llm_config)
        else:
            config = llm_config
        self.config: LLMConfig = config
        self.max_workers = config.max_workers
        backend = config.backend
        if backend == "openai":
            self.llm = OpenAIController(llm_config=config)
        elif backend == "ollama":
            self.llm = OllamaController(llm_config=config)
        else:
            raise ValueError("Backend must be one of: 'openai', 'ollama'")

    def get_completion(
        self, prompt: Union[str, Memory], json_response: bool = False
    ) -> str:
        def request() -> str:
            res = self.llm.get_completion(prompt, json_response)
            if len(res.strip()) == 0:
                raise ValueError("Empty response from LLM")
            return res

        try:
            return call_with_retry(
                request,
                operation_name="LLM completion",
                logger=log,
            )
        except Exception as error:
            raise RuntimeError(
                "Failed to get completion after multiple retries"
            ) from error

    def batch_get_completion(
        self,
        prompts: List[Union[str, Memory]],
        json_response: bool = False,
        progress_callback: Optional[Callable[[], None]] = None,
    ) -> list:
        """Generate completions concurrently while preserving input order.

        ``progress_callback`` runs in the caller thread once for every finished
        request, including requests that finish with an error.  Keeping the
        callback here lets callers report real progress without duplicating the
        thread-pool implementation.
        """
        results = [None] * len(prompts)
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_idx = {
                executor.submit(self.get_completion, prompt, json_response): idx
                for idx, prompt in enumerate(prompts)
            }
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    results[idx] = future.result()
                except Exception as e:
                    results[idx] = f"Error: {e}"
                finally:
                    if progress_callback is not None:
                        try:
                            progress_callback()
                        except Exception:
                            log.debug(
                                "Ignoring an error raised by the batch progress callback.",
                                exc_info=True,
                            )
        return results

    def get_json_completion(
        self,
        prompt: Union[str, Memory],
        schema: BaseModel,
        images: Optional[list] = None,
        think_mode: bool = False,
    ) -> dict:
        def request() -> dict:
            res = self.llm.get_json_completion(
                prompt, schema, images, think_mode=think_mode
            )
            if not res:
                raise ValueError("Empty response from LLM")
            return res

        try:
            return call_with_retry(
                request,
                operation_name="LLM JSON completion",
                logger=log,
            )
        except Exception as error:
            raise RuntimeError(
                "Failed to get JSON completion after multiple retries"
            ) from error


if __name__ == "__main__":
    # Example usage
    print("Testing LLM controller...")

    # from bookrag_core.configs.system_config import load_system_config

    # cfg = load_system_config("/home/wangshu/multimodal/GBC-RAG/config/default.yaml")
    # cfg.llm.max_workers = 4
    # controller = LLM(llm_config=cfg.llm)
    # prompt = "respond in 20 words. who are you?"

    # token_tracker = TokenTracker.get_instance()
    # token_tracker.reset()

    # start_time = time.time()
    # response = controller.batch_get_completion(prompts=[prompt] * 8)
    # end_time = time.time()
    # print(response)
    # print(f"Response time: {end_time - start_time:.2f} seconds")

    # batch_cost = token_tracker.record_stage("batch_get_completion")
    # print(f"Batch cost: {batch_cost}")

    # tmp_memory = Memory()
    # tmp_memory.add_batch(
    #     [
    #         {"role": "system", "content": "You are a helpful assistant."},
    #         {"role": "user", "content": "What is the capital of France?"},
    #     ]
    # )
    # response = controller.get_completion(tmp_memory)
    # print(response)
    # memory_cost = token_tracker.record_stage("get_completion")
    # print(f"Memory cost: {memory_cost}")
    # token_tracker.print_all_stages()
