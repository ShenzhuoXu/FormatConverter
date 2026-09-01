"""Unified job/task service layer for FormatConverter.

Wraps the ``convert``, ``clean``, ``pipeline``, and ``ai-clean`` operations
behind a small, thread-safe task model (:class:`JobManager`) so the existing
CLI and a future Web UI can share the same submission, status, and result
plumbing.

Design notes:

- :meth:`JobManager.submit` runs each task in a background **daemon** thread,
  so the interpreter can still exit while a job is pending.
- Task exceptions never escape the worker thread: they are converted into a
  ``failed`` :class:`JobResult` instead of crashing anything.
- State flow is ``queued -> running -> succeeded|failed``.
- API keys never appear in results or messages.  As a last-resort safety net,
  messages are scrubbed of the configured ``ORCAROUTER_API_KEY`` value (from
  the environment or the local ``.env`` file) before being stored, so even an
  exception that accidentally embeds the key cannot leak it.

This module deliberately has **no** HTTP, HTML, or browser dependencies; it
only imports the Python standard library plus this package's worker modules.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping

from .cli import ai_clean
from .env_store import read_env_key
from .markdown_cleaner import clean_markdown_directory, clean_markdown_file
from .pdf_converter import convert_pdf_directory, convert_pdf_file
from .pipeline import run_pipeline

__all__ = ["JobStatus", "JobResult", "JobManager", "UnknownJobTypeError"]


class JobStatus(str, Enum):
    """Lifecycle state of a submitted job."""

    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


_TERMINAL_STATUSES = frozenset({JobStatus.succeeded, JobStatus.failed})


@dataclass(frozen=True)
class JobResult:
    """Immutable snapshot of a job's current state."""

    job_id: str
    status: JobStatus
    message: str
    output_paths: tuple[Path, ...]


class UnknownJobTypeError(Exception):
    """Raised by :meth:`JobManager.submit` for an unregistered ``job_type``."""

    def __init__(self, job_type: str, known: tuple[str, ...]) -> None:
        self.job_type = job_type
        self.known = known
        super().__init__(
            f"Unknown job type: {job_type!r}. "
            f"Supported job types: {', '.join(known) or '(none)'}."
        )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _as_path(value: object) -> Path:
    """Coerce a ``str``/``Path`` (or Path-like) value into a :class:`Path`."""
    if isinstance(value, Path):
        return value
    return Path(str(value))


def _normalize_paths(paths: object) -> tuple[Path, ...]:
    """Collect handler output (single Path, list, or tuple) into a Path tuple."""
    if paths is None:
        return ()
    if isinstance(paths, Path):
        return (paths,)
    if isinstance(paths, (list, tuple)):
        return tuple(_as_path(path) for path in paths)
    return (_as_path(paths),)


def _sanitize_message(message: str) -> str:
    """Mask the configured API key inside ``message`` as a fallback.

    Acts on both the ``ORCAROUTER_API_KEY`` environment variable and the
    project-root ``.env`` value (when set); key values themselves are never
    stored or logged, only used as the search text.

    This is a *defensive* backstop, not the primary guarantee: it does an exact
    match on the current configured values, so a transformed or truncated key
    would not be matched and a very short key could be over-masked. The primary
    guarantee is that handlers never place the key into a message at all.
    """
    key = os.environ.get("ORCAROUTER_API_KEY")
    if key:
        message = message.replace(key, "***")
    dotenv_key = read_env_key()
    if dotenv_key:
        message = message.replace(dotenv_key, "***")
    return message


# ---------------------------------------------------------------------------
# Task handlers (one per supported job type)
# ---------------------------------------------------------------------------
# Each handler takes the caller-supplied ``params`` dict and returns
# ``(output_paths, message)``. Handlers only call this package's existing
# worker functions -- they never spawn shells or touch the network.


def _handle_convert(params: Mapping[str, object]) -> tuple[tuple[Path, ...], str]:
    overwrite = bool(params.get("overwrite", False))
    output_dir = _as_path(params["output_dir"])

    file = params.get("file")
    if file is not None:
        output = convert_pdf_file(_as_path(file), output_dir, overwrite=overwrite)
        return (output,), f"Converted 1 PDF file: {output}"

    input_dir = _as_path(params["input_dir"])
    converted = convert_pdf_directory(input_dir, output_dir, overwrite=overwrite)
    return tuple(converted), f"Converted {len(converted)} PDF file(s)."


def _handle_clean(params: Mapping[str, object]) -> tuple[tuple[Path, ...], str]:
    keep_lists = bool(params.get("keep_lists", True))
    dedupe = bool(params.get("dedupe", True))
    backup = bool(params.get("backup", True))

    file = params.get("file")
    if file is not None:
        output = clean_markdown_file(
            _as_path(file), keep_lists=keep_lists, dedupe=dedupe, backup=backup
        )
        return (output,), f"Cleaned 1 Markdown file: {output}"

    input_dir = _as_path(params["input_dir"])
    cleaned = clean_markdown_directory(
        input_dir, keep_lists=keep_lists, dedupe=dedupe, backup=backup
    )
    return tuple(cleaned), f"Cleaned {len(cleaned)} Markdown file(s)."


def _handle_pipeline(params: Mapping[str, object]) -> tuple[tuple[Path, ...], str]:
    converted, cleaned = run_pipeline(
        _as_path(params["pdf_dir"]),
        _as_path(params["md_dir"]),
        overwrite=bool(params.get("overwrite", False)),
        keep_lists=bool(params.get("keep_lists", True)),
        dedupe=bool(params.get("dedupe", True)),
        backup=bool(params.get("backup", True)),
    )
    paths = tuple(converted) + tuple(cleaned)
    return paths, (
        f"Converted {len(converted)} PDF(s), "
        f"cleaned {len(cleaned)} Markdown file(s)."
    )


def _handle_ai_clean(params: Mapping[str, object]) -> tuple[tuple[Path, ...], str]:
    # ``client`` is optional: when absent, ``ai_clean`` reads the API key from
    # the ORCAROUTER_API_KEY environment variable internally. The raw key is
    # never passed through params/results/messages.
    client = params.get("client")
    provider = str(params["provider"])
    model = str(params["model"])
    overwrite = bool(params.get("overwrite", False))

    file = params.get("file")
    if file is not None:
        # Single-file mode (CLI contract): proofread exactly one file.
        output_param = params.get("output")
        output = ai_clean(
            _as_path(file),
            provider,
            model,
            output=_as_path(output_param) if output_param is not None else None,
            overwrite=overwrite,
            client=client,  # type: ignore[arg-type]
        )
        return (output,), f"AI proofread: {output}"

    # Web batch mode: proofread every uploaded .md in input_dir into
    # output_dir, one output per file (``<stem>.ai.md``). The CLI never
    # produces these params, so this does not change any CLI behavior.
    input_dir = _as_path(params["input_dir"])
    output_dir = _as_path(params["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    for md_path in sorted(input_dir.glob("*.md")):
        out = ai_clean(
            md_path,
            provider,
            model,
            output=output_dir / f"{md_path.stem}.ai.md",
            overwrite=overwrite,
            client=client,  # type: ignore[arg-type]
        )
        outputs.append(out)
    return tuple(outputs), f"AI proofread {len(outputs)} Markdown file(s)."


_JOB_HANDLERS: dict[str, Callable[[Mapping[str, object]], tuple[tuple[Path, ...], str]]] = {
    "convert": _handle_convert,
    "clean": _handle_clean,
    "pipeline": _handle_pipeline,
    "ai-clean": _handle_ai_clean,
}


# ---------------------------------------------------------------------------
# JobManager
# ---------------------------------------------------------------------------


class JobManager:
    """Submit and track conversion/cleanup jobs in background threads.

    Instances are thread-safe: :meth:`submit`, :meth:`get`, and :meth:`wait`
    may be called from any thread. Each submitted job runs in a daemon thread;
    handler exceptions never escape and never crash the thread -- they are
    captured as a ``failed`` :class:`JobResult`.

    Boolean parameters (``overwrite``, ``keep_lists``, ``dedupe``, ``backup``)
    must be real Python ``bool`` values (or omitted to use the default).
    String or integer lookalikes such as ``"false"`` or ``0`` are truthy in
    ways that can silently flip behavior, so callers such as a Web UI should
    pass genuine booleans, not stringified values.
    """

    def __init__(self) -> None:
        self._results: dict[str, JobResult] = {}
        self._cond = threading.Condition()
        # A per-instance copy so subclasses / callers can register extra
        # handlers without mutating a shared global registry.
        self.handlers: dict[
            str, Callable[[Mapping[str, object]], tuple[tuple[Path, ...], str]]
        ] = dict(_JOB_HANDLERS)

    def submit(self, job_type: str, params: dict) -> str:
        """Register a new job and return its id.

        Raises :class:`UnknownJobTypeError` synchronously (before any thread is
        started) when ``job_type`` has no registered handler.
        """
        handler = self.handlers.get(job_type)
        if handler is None:
            raise UnknownJobTypeError(job_type, tuple(self.handlers))
        if not isinstance(params, dict):
            raise TypeError(f"params must be a dict, got {type(params).__name__}")

        # Snapshot the params so the caller mutating the dict after submit()
        # cannot affect the running job.
        params = dict(params)

        job_id = uuid.uuid4().hex
        self._store(job_id, JobStatus.queued, "Job queued.", ())

        thread = threading.Thread(
            target=self._run_job,
            args=(job_id, job_type, params),
            name=f"format-converter-job-{job_id[:8]}",
            daemon=True,
        )
        thread.start()
        return job_id

    def get(self, job_id: str) -> JobResult | None:
        """Return the current result for ``job_id``, or None if unknown."""
        with self._cond:
            return self._results.get(job_id)

    def wait(self, job_id: str, timeout: float | None = None) -> JobResult | None:
        """Block until ``job_id`` reaches a terminal state.

        Returns the final :class:`JobResult`, or None if the job id is unknown.
        Raises :class:`TimeoutError` if the job does not finish within
        ``timeout`` seconds (``None`` waits indefinitely).
        """
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative")
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while True:
                result = self._results.get(job_id)
                if result is None:
                    return None
                if result.status in _TERMINAL_STATUSES:
                    return result
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError(
                        f"Job {job_id!r} did not finish within {timeout!r}s"
                    )
                self._cond.wait(remaining)

    # -- internals ---------------------------------------------------------

    def _run_job(self, job_id: str, job_type: str, params: dict) -> None:
        """Daemon-thread entry point: run the handler and record the outcome.

        Never raises -- even an unexpected failure is recorded as a ``failed``
        result so the background thread always exits cleanly.
        """
        try:
            self._execute(job_id, job_type, params)
        # The worker thread must turn *any* exception into a failed result
        # rather than let the thread vanish silently (daemon threads have no
        # unhandled-exception hook). Catching BaseException here is therefore
        # intentional: even a SystemExit or KeyboardInterrupt raised inside a
        # handler becomes a failed job instead of killing the thread.
        except BaseException:  # noqa: BLE001 - deliberate last-resort guard
            try:
                self._store(job_id, JobStatus.failed, "Task failed unexpectedly.", ())
            except BaseException:  # noqa: BLE001 - never raise from a daemon thread
                pass

    def _execute(self, job_id: str, job_type: str, params: dict) -> None:
        """Run a job's handler and store the terminal result."""
        self._store(job_id, JobStatus.running, "Job running.", ())
        status: JobStatus = JobStatus.failed
        message = "Task failed unexpectedly."
        output_paths: tuple[Path, ...] = ()
        try:
            handler = self.handlers[job_type]
            paths, message = handler(params)
            output_paths = _normalize_paths(paths)
            status = JobStatus.succeeded
            message = _sanitize_message(message)
        except Exception as exc:  # noqa: BLE001 - any handler error -> failed
            status = JobStatus.failed
            # Deliberately flatten the exception into a single message string
            # and do not preserve the original traceback/exception chain: the
            # chain could embed sensitive details (e.g. a provider response)
            # that we do not want stored in the result.
            try:
                message = _sanitize_message(str(exc) or repr(exc))
            except Exception:  # noqa: BLE001
                message = f"Task failed: {type(exc).__name__}"
            output_paths = ()
        self._store(job_id, status, message, output_paths)

    def _store(
        self,
        job_id: str,
        status: JobStatus,
        message: str,
        output_paths: tuple[Path, ...],
    ) -> None:
        """Write a result snapshot under the lock and wake any waiters."""
        result = JobResult(job_id, status, message, output_paths)
        with self._cond:
            self._results[job_id] = result
            self._cond.notify_all()
