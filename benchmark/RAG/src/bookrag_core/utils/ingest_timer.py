"""Thread-safe ingestion work-time accounting.

The benchmark's insertion ``time`` metric represents total work rather than
elapsed wall-clock time.  Every top-level parallel ingestion task contributes
its own elapsed duration.  The final value replaces the union of concurrent
task intervals in wall time with the sum of those intervals::

    work_time = wall_time + sum(task_intervals) - union(task_intervals)

Nested executors are deliberately not counted twice.  A task submitted while
another measured ingestion task is active inherits the timer for token and
progress purposes, but its interval is already covered by the outer task.
"""

from __future__ import annotations

from concurrent.futures import Executor, Future
from contextvars import ContextVar, Token
from threading import Lock
from time import perf_counter
from types import TracebackType
from typing import Any, Callable, TypeVar


ResultT = TypeVar("ResultT")

_CURRENT_TIMER: ContextVar["DocumentWorkTimer | None"] = ContextVar(
    "bookrag_ingest_work_timer",
    default=None,
)
_CURRENT_TASK_DEPTH: ContextVar[int] = ContextVar(
    "bookrag_ingest_work_task_depth",
    default=0,
)


class DocumentWorkTimer:
    """Collect top-level parallel task intervals for one ingestion run.

    The historical class name remains public because the runner and existing
    callers already import it.  It now accounts for every ingestion executor,
    not only the document worker pool.
    """

    def __init__(self) -> None:
        self._intervals: list[tuple[float, float]] = []
        self._lock = Lock()
        self._context_token: Token[DocumentWorkTimer | None] | None = None

    def __enter__(self) -> "DocumentWorkTimer":
        if self._context_token is not None:
            raise RuntimeError("DocumentWorkTimer cannot be entered twice")
        self._context_token = _CURRENT_TIMER.set(self)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        if self._context_token is not None:
            _CURRENT_TIMER.reset(self._context_token)
            self._context_token = None

    def _record(self, started: float, finished: float) -> None:
        with self._lock:
            self._intervals.append((started, finished))

    def _run_task(
        self,
        function: Callable[..., ResultT],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        record_interval: bool,
    ) -> ResultT:
        timer_token = _CURRENT_TIMER.set(self)
        depth_token = _CURRENT_TASK_DEPTH.set(_CURRENT_TASK_DEPTH.get() + 1)
        started = perf_counter() if record_interval else None
        try:
            return function(*args, **kwargs)
        finally:
            if started is not None:
                self._record(started, perf_counter())
            _CURRENT_TASK_DEPTH.reset(depth_token)
            _CURRENT_TIMER.reset(timer_token)

    def statistics(self) -> dict[str, float | int]:
        """Return task time, occupied wall time, and overlap correction."""
        with self._lock:
            intervals = list(self._intervals)
        if not intervals:
            empty = {
                "task_count": 0,
                "task_time": 0.0,
                "task_wall_time": 0.0,
                "task_overlap_time": 0.0,
                "document_task_count": 0,
                "document_time": 0.0,
                "document_wall_time": 0.0,
                "document_overlap_time": 0.0,
            }
            return empty

        worker_time = sum(finished - started for started, finished in intervals)
        ordered = sorted(intervals)
        union_time = 0.0
        union_start, union_end = ordered[0]
        for started, finished in ordered[1:]:
            if started <= union_end:
                union_end = max(union_end, finished)
            else:
                union_time += union_end - union_start
                union_start, union_end = started, finished
        union_time += union_end - union_start
        overlap_time = max(0.0, worker_time - union_time)
        statistics = {
            "task_count": len(intervals),
            "task_time": worker_time,
            "task_wall_time": union_time,
            "task_overlap_time": overlap_time,
            # Backwards-compatible aliases.  They now describe all measured
            # ingestion tasks rather than documents alone.
            "document_task_count": len(intervals),
            "document_time": worker_time,
            "document_wall_time": union_time,
            "document_overlap_time": overlap_time,
        }
        return statistics

    def total_work_time(self, wall_time: float) -> float:
        """Replace concurrent task wall time with the sum of task durations."""
        return float(wall_time) + float(
            self.statistics()["task_overlap_time"]
        )


def submit_ingest_task(
    executor: Executor,
    function: Callable[..., ResultT],
    /,
    *args: Any,
    **kwargs: Any,
) -> Future[ResultT]:
    """Submit work and measure it when it is a top-level ingestion task."""
    timer = _CURRENT_TIMER.get()
    if timer is None:
        return executor.submit(function, *args, **kwargs)

    # The decision is made in the submitting thread.  Context variables are
    # not automatically copied into ThreadPoolExecutor workers, so checking in
    # the new worker would incorrectly classify nested work as top level.
    record_interval = _CURRENT_TASK_DEPTH.get() == 0
    return executor.submit(
        timer._run_task,
        function,
        args,
        kwargs,
        record_interval=record_interval,
    )


def submit_document_task(
    executor: Executor,
    function: Callable[..., ResultT],
    /,
    *args: Any,
    **kwargs: Any,
) -> Future[ResultT]:
    """Compatibility wrapper for document-pipeline submissions."""
    return submit_ingest_task(executor, function, *args, **kwargs)
