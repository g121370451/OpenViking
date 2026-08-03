# Copyright (C) 2025-2026 Shu Wang
# SPDX-License-Identifier: Apache-2.0 OR AGPL-3.0-only

import json
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Dict, Generator


class QueryTokenUsage:
    """Token counters owned by one retrieval query."""

    def __init__(self):
        self._lock = threading.Lock()
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0

    def add_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        with self._lock:
            self.prompt_tokens += prompt_tokens
            self.completion_tokens += completion_tokens
            self.total_tokens += prompt_tokens + completion_tokens

    def get_usage(self) -> dict:
        with self._lock:
            return {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens,
            }


class TokenTracker:
    """
    A thread-safe singleton class to track LLM token usage across the application.
    """

    _instance = None
    _lock = threading.RLock() # Use RLock instead of Lock
    _query_usage: ContextVar[QueryTokenUsage | None] = ContextVar(
        "bookrag_query_token_usage",
        default=None,
    )
    _prepared_ingest: ContextVar[bool] = ContextVar(
        "bookrag_prepared_ingest_token_state",
        default=False,
    )

    def __new__(cls):
        # The __new__ method is called before __init__ when an object is created.
        # This is where we ensure only one instance is ever created.
        if cls._instance is None:
            with cls._lock:
                # Double-check locking to prevent race conditions
                if cls._instance is None:
                    cls._instance = super(TokenTracker, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        # The __init__ will be called every time TokenTracker() is invoked,
        # but we only want to initialize the state once.
        if not hasattr(self, "initialized"):
            self._persistence_path = None
            self.reset()
            self.initialized = True  # Mark as initialized

    @classmethod
    def get_instance(cls):
        """Public method to get the singleton instance."""
        return cls()

    def add_usage(self, prompt_tokens: int, completion_tokens: int):
        """
        Adds token usage to the global counters in a thread-safe manner.
        """
        prompt_tokens = int(prompt_tokens)
        completion_tokens = int(completion_tokens)
        query_usage = self._query_usage.get()
        if query_usage is not None:
            query_usage.add_usage(prompt_tokens, completion_tokens)

        with self._lock:
            self.prompt_tokens += prompt_tokens
            self.completion_tokens += completion_tokens
            self.total_tokens += prompt_tokens + completion_tokens
            self._persist_locked()

    @contextmanager
    def query_scope(self) -> Generator[QueryTokenUsage, None, None]:
        """Track usage for the current retrieval query without resetting globals."""
        usage = QueryTokenUsage()
        context_token = self._query_usage.set(usage)
        try:
            yield usage
        finally:
            self._query_usage.reset(context_token)

    @contextmanager
    def prepared_ingest_scope(self) -> Generator[None, None, None]:
        """Keep an outer orchestrator's token baseline in nested builders."""
        context_token = self._prepared_ingest.set(True)
        try:
            yield
        finally:
            self._prepared_ingest.reset(context_token)

    def ingest_state_is_prepared(self) -> bool:
        return bool(self._prepared_ingest.get())

    def set_persistence_path(self, path=None):
        """Enable or disable durable cumulative usage accounting."""
        with self._lock:
            self._persistence_path = Path(path) if path is not None else None

    def read_persisted_usage(self) -> dict | None:
        with self._lock:
            path = self._persistence_path
            if path is None or not path.is_file():
                return None
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                prompt_tokens = int(value.get("prompt_tokens", 0))
                completion_tokens = int(value.get("completion_tokens", 0))
            except (OSError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
                return None
            if prompt_tokens < 0 or completion_tokens < 0:
                return None
            return {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            }

    def _persist_locked(self):
        if self._persistence_path is None:
            return
        from bookrag_core.checkpoint import atomic_write_json

        atomic_write_json(
            self._persistence_path,
            {
                "format_version": 1,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens,
            },
        )

    def get_usage(self) -> dict:
        """Returns the current token usage."""
        with self._lock:
            return {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens,
            }

    def restore_usage(self, prompt_tokens: int, completion_tokens: int):
        """Restore persisted usage and make it the baseline for later stages."""
        with self._lock:
            self.prompt_tokens = int(prompt_tokens)
            self.completion_tokens = int(completion_tokens)
            self.total_tokens = self.prompt_tokens + self.completion_tokens
            self.last_stage_prompt_tokens = self.prompt_tokens
            self.last_stage_completion_tokens = self.completion_tokens
            self._persist_locked()

    def reset(self):
        """Resets the token counters."""
        with self._lock:
            self.prompt_tokens = 0
            self.completion_tokens = 0
            self.total_tokens = 0
            
            self.stage_history: Dict[str, Dict[str, int]] = {}
            # Keep track of the totals at the last recorded stage
            self.last_stage_prompt_tokens = 0
            self.last_stage_completion_tokens = 0

    def record_stage(self, stage_name: str) -> Dict[str, int]:
        """
        Records the token usage since the last stage and returns the delta.

        Args:
            stage_name (str): The name of the stage to record.

        Returns:
            dict: A dictionary containing the prompt, completion, and total tokens
                  used exclusively within this stage.
        """
        with self._lock:
            # Calculate the difference (delta) from the last stage
            stage_prompt_tokens = self.prompt_tokens - self.last_stage_prompt_tokens
            stage_completion_tokens = self.completion_tokens - self.last_stage_completion_tokens
            stage_total_tokens = stage_prompt_tokens + stage_completion_tokens

            stage_usage = {
                "prompt_tokens": stage_prompt_tokens,
                "completion_tokens": stage_completion_tokens,
                "total_tokens": stage_total_tokens,
            }
            
            # Store this stage's usage in the history
            self.stage_history[stage_name] = stage_usage
            
            # CRITICAL: Update the 'last stage' counters to the current totals
            # to set the baseline for the *next* stage.
            self.last_stage_prompt_tokens = self.prompt_tokens
            self.last_stage_completion_tokens = self.completion_tokens
            
            return stage_usage

    def record_stage_since(
        self,
        stage_name: str,
        baseline: Dict[str, int],
    ) -> Dict[str, int]:
        """Record a stage from an explicit main-thread token snapshot.

        Unlike ``record_stage``, this method does not depend on which worker
        happened to finish a previous stage first.  It should be called only
        after every future belonging to the stage has joined.
        """
        with self._lock:
            stage_prompt_tokens = self.prompt_tokens - int(
                baseline.get("prompt_tokens", 0)
            )
            stage_completion_tokens = self.completion_tokens - int(
                baseline.get("completion_tokens", 0)
            )
            if stage_prompt_tokens < 0 or stage_completion_tokens < 0:
                raise RuntimeError(
                    "Token stage baseline is newer than the current counters"
                )
            stage_usage = {
                "prompt_tokens": stage_prompt_tokens,
                "completion_tokens": stage_completion_tokens,
                "total_tokens": stage_prompt_tokens + stage_completion_tokens,
            }
            self.stage_history[stage_name] = stage_usage
            self.last_stage_prompt_tokens = self.prompt_tokens
            self.last_stage_completion_tokens = self.completion_tokens
            return stage_usage

    def print_all_stages(self):
        """
        Prints a formatted report of token usage for all recorded stages
        and the final total usage.
        """
        print("\n" + "="*50)
        print("📊 TOKEN USAGE REPORT 📊")
        print("="*50)
        
        with self._lock:
            if not self.stage_history:
                print("No stages have been recorded yet.")
            else:
                print("\n--- Stage-by-Stage Breakdown ---")
                for stage, usage in self.stage_history.items():
                    print(
                        f"  - Stage '{stage}':\n"
                        f"    Prompt: {usage['prompt_tokens']:>6} | "
                        f"Completion: {usage['completion_tokens']:>6} | "
                        f"Total: {usage['total_tokens']:>7}"
                    )
            
            print("\n--- Cumulative Total ---")
            print(
                f"  Overall Usage | "
                f"Prompt: {self.prompt_tokens} | "
                f"Completion: {self.completion_tokens} | "
                f"Total: {self.total_tokens}"
            )

        print("="*50 + "\n")


    def __str__(self):
        usage = self.get_usage()
        return (
            f"📊 Token Usage | "
            f"Prompt: {usage['prompt_tokens']} | "
            f"Completion: {usage['completion_tokens']} | "
            f"Total: {usage['total_tokens']}"
        )
