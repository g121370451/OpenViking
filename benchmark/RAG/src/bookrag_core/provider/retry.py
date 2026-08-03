"""Retry helpers shared by online BookRAG providers."""

from __future__ import annotations

from collections.abc import Callable
from email.utils import parsedate_to_datetime
import logging
import random
import time
from typing import TypeVar


T = TypeVar("T")


def is_rate_limit_error(error: Exception) -> bool:
    """Return whether an SDK/provider exception represents HTTP 429."""
    status_code = getattr(error, "status_code", None)
    response = getattr(error, "response", None)
    if status_code is None and response is not None:
        status_code = getattr(response, "status_code", None)
    if status_code == 429 or str(status_code) == "429":
        return True

    error_name = type(error).__name__.lower()
    error_text = str(error).lower()
    return (
        "ratelimit" in error_name
        or "rate limit" in error_text
        or "account ratelimit exceeded" in error_text
        or "accountratelimitexceeded" in error_text
    )


def _retry_after_seconds(error: Exception) -> float | None:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        headers = getattr(error, "headers", None)
    if not headers:
        return None

    retry_after_ms = headers.get("retry-after-ms")
    if retry_after_ms is not None:
        try:
            return max(0.0, float(retry_after_ms) / 1000.0)
        except (TypeError, ValueError):
            pass

    retry_after = headers.get("retry-after")
    if retry_after is None:
        return None
    try:
        return max(0.0, float(retry_after))
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(str(retry_after))
            now = time.time()
            return max(0.0, retry_at.timestamp() - now)
        except (TypeError, ValueError, OverflowError):
            return None


def call_with_retry(
    operation: Callable[[], T],
    *,
    operation_name: str,
    logger: logging.Logger,
    max_attempts: int = 3,
    rate_limit_max_attempts: int = 8,
    rate_limit_base_delay: float = 2.0,
    rate_limit_max_delay: float = 60.0,
    retry_predicate: Callable[[Exception], bool] | None = None,
    exponential_backoff: bool = False,
    retry_base_delay: float = 1.0,
    retry_max_delay: float = 30.0,
) -> T:
    """Run an operation, using a longer exponential backoff for HTTP 429.

    SDK clients often retry only a couple of times.  That is too short for an
    account-level quota window during long BookRAG imports, so 429 responses get
    a separate application-level retry budget.  Other errors retain BookRAG's
    previous three-attempt behavior.
    """
    attempt = 1
    while True:
        try:
            return operation()
        except Exception as error:
            rate_limited = is_rate_limit_error(error)
            if retry_predicate is not None and not retry_predicate(error):
                raise
            allowed_attempts = (
                rate_limit_max_attempts if rate_limited else max_attempts
            )
            if attempt >= allowed_attempts:
                raise

            if rate_limited:
                backoff = min(
                    rate_limit_max_delay,
                    rate_limit_base_delay * (2 ** (attempt - 1)),
                )
                retry_after = _retry_after_seconds(error)
                delay = max(backoff, retry_after or 0.0)
                # Concurrent embedding workers should not all retry together.
                delay += random.uniform(0.0, min(1.0, delay * 0.25))
                reason = "429 rate limit"
            else:
                if exponential_backoff:
                    delay = min(
                        retry_max_delay,
                        retry_base_delay * (2 ** (attempt - 1)),
                    )
                    delay += random.uniform(0.0, min(1.0, delay * 0.25))
                else:
                    delay = retry_base_delay
                reason = type(error).__name__

            logger.warning(
                "%s failed with %s; retrying in %.1fs (attempt %d/%d).",
                operation_name,
                reason,
                delay,
                attempt + 1,
                allowed_attempts,
            )
            time.sleep(delay)
            attempt += 1
