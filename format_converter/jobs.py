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

from .ai_cleaner import SYSTEM_PROMPT
from .cli import ai_clean
from .ai_jobs import (
    AIJobStore,
    aggregate_web_job_status,
    map_manifest_status,
)
from .env_store import read_env_key
from .markdown_cleaner import clean_markdown_directory, clean_markdown_file
from .pdf_converter import convert_pdf_directory, convert_pdf_file
from .pipeline import run_pipeline

__all__ = ["JobStatus", "JobResult", "JobManager", "UnknownJobTypeError"]


class JobStatus(str, Enum):
    """Lifecycle state of a submitted job."""

    queued = "queued"
    running = "running"
    interrupted = "interrupted"
    succeeded = "succeeded"
    failed = "failed"


_TERMINAL_STATUSES = frozenset({JobStatus.succeeded, JobStatus.failed, JobStatus.interrupted})

# Durable (AIJobManifest) internal statuses a web AI job may be continued
# from. ``running`` is deliberately excluded: that would mean another worker
# thread is already processing the checkpoint, so a resume/retry must not
# double-process it. ``completed`` is a terminal durable state and is skipped
# (its result files are left untouched).
_CONTINUABLE_DURABLE_STATUSES = frozenset({"failed", "interrupted", "merging"})


@dataclass(frozen=True)
class JobResult:
    """Immutable snapshot of a job's current state.

    ``job_type``, ``created_at`` and ``updated_at`` let a caller (such as the
    web layer) surface recent jobs and their progress without leaking output
    paths. ``created_at`` is the wall-clock ``time.time()`` of the first
    (queued) snapshot; ``updated_at`` is rewritten on every state change.
    ``current``/``total`` report AI proofreading chunk progress (1-based
    chunk index and total chunk count); non-AI jobs keep them at ``0``.
    """

    job_id: str
    status: JobStatus
    message: str
    output_paths: tuple[Path, ...]
    job_type: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    current: int = 0
    total: int = 0


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


def _is_valid_ai_web_id(web_job_id: str) -> bool:
    """True only for a 32-char lowercase hex web/durable job id."""
    from .ai_jobs import _JOB_ID_RE as _AI_JOB_ID_RE
    return bool(
        isinstance(web_job_id, str)
        and web_job_id
        and _AI_JOB_ID_RE.fullmatch(web_job_id)
    )


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
    progress = params.get("_progress")
    web_job_id = params.get("_web_job_id", "")

    # Durable AI job store for chunk checkpointing (Web path only).
    ai_job_store: AIJobStore | None = None
    if params.get("_durable", False):
        ai_job_root = params.get("_ai_job_root")
        if ai_job_root is not None:
            ai_job_store = AIJobStore(root=_as_path(ai_job_root))
        else:
            ai_job_store = AIJobStore()

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
            progress=progress,  # type: ignore[arg-type]
            ai_job_store=ai_job_store,
            web_job_id=web_job_id,
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
            progress=progress,  # type: ignore[arg-type]
            ai_job_store=ai_job_store,
            web_job_id=web_job_id,
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

    def __init__(self, ai_job_store: AIJobStore | None = None) -> None:
        self._results: dict[str, JobResult] = {}
        self._cond = threading.Condition()
        # A per-instance copy so subclasses / callers can register extra
        # handlers without mutating a shared global registry.
        self.handlers: dict[
            str, Callable[[Mapping[str, object]], tuple[tuple[Path, ...], str]]
        ] = dict(_JOB_HANDLERS)
        self._ai_job_store = ai_job_store
        # Output root recorded by _hydrate_ai_job_snapshots(base_dir=...). It
        # is used to re-derive each durable manifest's final output path when a
        # continuation (resume/retry) must write results that were never stored
        # in a JobResult (e.g. a web job that failed live, before hydration).
        self._ai_output_base: Path | None = None
        if self._ai_job_store is not None:
            self._ai_job_store.mark_stale_running_interrupted()
            self._hydrate_ai_job_snapshots()

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
        self._store(job_id, JobStatus.queued, "Job queued.", (), job_type)

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

    def list_recent(self, limit: int = 20) -> list[dict]:
        """Return the most recently updated jobs as lightweight dicts.

        Entries are ordered newest-updated first and contain ``job_id``,
        ``job_type``, ``status``, ``message``, ``created_at``, ``updated_at``,
        ``current`` and ``total``. ``output_paths`` are deliberately omitted so
        absolute server paths can never reach a caller. Unknown/cleaned-up jobs
        simply no longer appear; this only reflects what is in the current
        process.
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError("limit must be a non-negative int")
        with self._cond:
            results = sorted(
                self._results.values(),
                key=lambda r: (r.updated_at, r.job_id),
                reverse=True,
            )
        return [
            {
                "job_id": r.job_id,
                "job_type": r.job_type,
                "status": r.status.value,
                "message": r.message,
                "created_at": r.created_at,
                "updated_at": r.updated_at,
                "current": r.current,
                "total": r.total,
            }
            for r in results[:limit]
        ]

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
                self._store(
                    job_id, JobStatus.failed, "Task failed unexpectedly.", (), job_type
                )
            except BaseException:  # noqa: BLE001 - never raise from a daemon thread
                pass

    def _execute(self, job_id: str, job_type: str, params: dict) -> None:
        """Run a job's handler and store the terminal result."""
        self._store(job_id, JobStatus.running, "Job running.", (), job_type)
        if job_type == "ai-clean":
            params["_progress"] = self._make_progress_callback(job_id, job_type)
            params["_web_job_id"] = job_id
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
        self._store(job_id, status, message, output_paths, job_type)

    def _make_progress_callback(self, job_id: str, job_type: str) -> Callable[[int, int], None]:
        """Return a callback that records AI chunk progress on the running job."""

        def progress(current: int, total: int) -> None:
            self._store(
                job_id,
                JobStatus.running,
                f"AI 校对中 · {current} / {total}",
                (),
                job_type,
                current=current,
                total=total,
            )

        return progress

    def _hydrate_ai_job_snapshots(self, base_dir: Path | None = None) -> None:
        """Load durable AI job snapshots into _results at startup.

        When ``base_dir`` is provided, output paths are set to
        ``<base_dir>/<web_job_id>/output/<output_basename>`` for each manifest
        that carries an ``output_basename``, and ``base_dir`` is remembered as
        ``self._ai_output_base`` so a later resume/retry can re-derive output
        locations for jobs that never reached a hydrated snapshot.
        """
        if base_dir is not None:
            self._ai_output_base = Path(base_dir)
        if self._ai_job_store is None:
            return
        from collections import defaultdict
        by_web_job: dict[str, list[AIJobManifest]] = defaultdict(list)
        for manifest in self._ai_job_store.scan_recent():
            web_id = manifest.web_job_id or manifest.job_id
            by_web_job[web_id].append(manifest)
        for web_id, manifests in by_web_job.items():
            total_current = 0
            total_total = 0
            output_paths: list[Path] = []
            for m in manifests:
                completed = self._ai_job_store.completed_count(m.job_id)
                total_current += completed
                total_total += m.total_chunks
                if base_dir is not None and m.output_basename:
                    job_dir = base_dir / web_id
                    out_dir = job_dir / "output"
                    out_dir.mkdir(parents=True, exist_ok=True)
                    candidate = out_dir / m.output_basename
                    try:
                        candidate.resolve().relative_to(job_dir.resolve())
                        output_paths.append(candidate)
                    except (ValueError, OSError):
                        pass
            agg_status = aggregate_web_job_status(manifests)
            try:
                status = JobStatus(map_manifest_status(agg_status))
            except ValueError:
                status = JobStatus.failed
            message = self._make_hydrated_message(
                agg_status, total_current, total_total
            )
            self._store(
                web_id,
                status,
                message,
                tuple(output_paths),
                "ai-clean",
                current=total_current,
                total=total_total,
            )

    @staticmethod
    def _make_hydrated_message(manifest_status: str, completed: int, total: int) -> str:
        if manifest_status == "interrupted":
            if total > 0:
                return f"AI 校对中 · {completed} / {total}"
            return "任务已中断"
        if manifest_status == "completed":
            return "任务已完成"
        if manifest_status == "failed":
            return "任务失败"
        return "任务已中断"

    def resume_ai_job(self, web_job_id: str) -> str | None:
        """Start a background thread to continue interrupted durable AI jobs.

        When multiple durable manifests share the same ``web_job_id`` (multi-file
        ``ai-clean``), every manifest that is still resumable (failed,
        interrupted, or merging) is continued; already completed manifests and
        their result files are left untouched.

        Returns the first continued durable job_id, or None when nothing can be
        continued. The caller should verify the web job exists and is in an
        allowed state first.
        """
        return self._continue_ai_job(web_job_id)

    def retry_ai_job(self, web_job_id: str) -> str | None:
        """Start a background thread to continue failed/interrupted durable AI jobs.

        Like :meth:`resume_ai_job`, a retry reuses durable checkpoints: a chunk
        whose ``results/NNNN.md`` already exists and is readable is skipped, and
        only missing chunks are re-requested. Any durable manifest in a
        ``failed``, ``interrupted``, or ``merging`` state is flipped back to
        ``running`` and continued until it is completed, so a failed job is
        never re-run from scratch.

        Returns the first continued durable job_id, or None when nothing can be
        continued. The caller should verify the web job is ``failed`` or
        ``interrupted`` first.
        """
        return self._continue_ai_job(web_job_id)

    def delete_ai_web_job(self, web_job_id: str) -> int:
        """Delete every durable checkpoint directory under ``web_job_id``.

        Validates ``web_job_id`` against the 32-char hex pattern up front (and
        the store re-validates each durable directory), so an arbitrary value
        can never remove paths outside the durable store root. Returns the
        number of directories removed (0 when the store is unconfigured or no
        checkpoints exist).
        """
        if not _is_valid_ai_web_id(web_job_id):
            return 0
        store = self._ai_job_store
        if store is None:
            return 0
        return store.delete_web_job(web_job_id)

    def forget(self, job_id: str) -> None:
        """Remove a job's in-memory snapshot (used after durable cleanup).

        A subsequent :meth:`get` / :meth:`list_recent` no longer reports the
        job. Safe to call for unknown ids.
        """
        with self._cond:
            self._results.pop(job_id, None)
            self._cond.notify_all()

    def ai_web_job_exists(self, web_job_id: str) -> bool:
        """True when durable checkpoints exist for ``web_job_id``.

        Unlike :meth:`get`, this reflects the durable store (unbounded) rather
        than the in-memory snapshot list, so an old job that startup hydration
        never snapshotted is still discoverable. An invalid id returns False.
        """
        if not _is_valid_ai_web_id(web_job_id):
            return False
        store = self._ai_job_store
        if store is None:
            return False
        return bool(store.find_by_web_job_id(web_job_id))

    def ai_web_job_status(self, web_job_id: str) -> JobStatus | None:
        """Return the aggregated durable status for ``web_job_id``.

        Aggregates every durable manifest sharing ``web_job_id`` into one of
        ``interrupted`` / ``succeeded`` / ``failed`` (see
        :func:`format_converter.ai_jobs.aggregate_web_job_status`), so the Web
        layer can decide resume/retry/delete for a job that has no in-memory
        snapshot (e.g. older than the startup hydration window). Returns None
        when the id is invalid or no durable manifest exists.
        """
        if not _is_valid_ai_web_id(web_job_id):
            return None
        store = self._ai_job_store
        if store is None:
            return None
        manifests = store.find_by_web_job_id(web_job_id)
        if not manifests:
            return None
        agg_status = aggregate_web_job_status(manifests)
        try:
            return JobStatus(map_manifest_status(agg_status))
        except ValueError:
            return JobStatus.failed

    def _continue_ai_job(self, web_job_id: str) -> str | None:
        """Shared core of resume/retry: launch continuation of durable jobs."""
        if not _is_valid_ai_web_id(web_job_id):
            return None
        store = self._ai_job_store
        if store is None:
            return None
        # Refuse to double-launch while an earlier continuation is active.
        current = self.get(web_job_id)
        if current is not None and current.status in (JobStatus.queued, JobStatus.running):
            return None

        manifests = store.find_by_web_job_id(web_job_id)
        targets = [
            m for m in manifests if m.status in _CONTINUABLE_DURABLE_STATUSES
        ]
        if not targets:
            return None
        # Deterministic order (oldest first) so progress and outputs are stable.
        targets.sort(key=lambda m: (m.created_at, m.job_id))
        plan = [
            (m.job_id, self._durable_output_path(web_job_id, m))
            for m in targets
        ]

        total_current = sum(store.completed_count(m.job_id) for m in targets)
        total_total = sum(m.total_chunks for m in targets)
        self._store(web_job_id, JobStatus.running, "正在继续…", (), "ai-clean",
                    current=total_current, total=total_total)

        thread = threading.Thread(
            target=self._run_continue_multi,
            args=(web_job_id, plan),
            name=f"format-converter-resume-{web_job_id[:8]}",
            daemon=True,
        )
        thread.start()
        return targets[0].job_id

    def _run_continue_multi(
        self, web_job_id: str, plan: list[tuple[str, Path]]
    ) -> None:
        """Background thread: continue one or more durable AI jobs.

        ``plan`` is an ordered list of ``(durable_job_id, output_path)`` pairs.
        All targets must succeed for the web job to be ``succeeded``; any single
        failure causes the entire web job to be ``failed``. Chunk results that
        are already on disk are never re-requested.

        Progress is aggregated across every target manifest, so the web job's
        ``current``/``total`` reflect all files being continued.
        """
        store = self._ai_job_store
        if store is None:
            return
        total_all = sum(store.load(durable_job_id).total_chunks for durable_job_id, _ in plan)

        def aggregate_progress() -> None:
            current = sum(store.completed_count(durable_job_id) for durable_job_id, _ in plan)
            self._store(web_job_id, JobStatus.running, f"AI 校对中 · {current} / {total_all}", (),
                        "ai-clean", current=current, total=total_all)

        failure_msg: str | None = None
        for durable_job_id, output_path in plan:
            try:
                self._resume_single_chunk_loop(
                    web_job_id, durable_job_id, output_path, progress_cb=aggregate_progress
                )
            except BaseException as exc:
                try:
                    store.update_status(durable_job_id, "failed")
                except Exception:
                    pass
                failure_msg = _sanitize_message(str(exc) or repr(exc)) or "继续处理失败。"
                break

        if failure_msg is not None:
            self._store(web_job_id, JobStatus.failed, failure_msg, (), "ai-clean")
            return
        final_paths = self._derived_web_outputs(web_job_id)
        final_current = sum(store.completed_count(durable_job_id) for durable_job_id, _ in plan)
        self._store(web_job_id, JobStatus.succeeded, "AI proofread completed.",
                    tuple(final_paths), "ai-clean",
                    current=final_current, total=total_all)

    def _resume_single_chunk_loop(
        self,
        web_job_id: str,
        durable_job_id: str,
        output_path: Path | None = None,
        *,
        progress_cb: Callable[[], None] | None = None,
    ) -> Path:
        """Process missing chunks for one durable job, write output, return output_path.

        ``output_path`` is where the reassembled ``final.md`` is copied; when
        None it is derived from the manifest via :meth:`_durable_output_path`.
        ``progress_cb`` is invoked (with no arguments) after each chunk result
        is saved, so the caller decides how to report progress (per-file or
        aggregate across a multi-file web job).

        Completed chunk result files are skipped (via ``store.next_unfinished``)
        and never re-requested. Never calls ``self._store()`` with a terminal
        status — the caller (``_run_continue_multi``) is responsible for that.
        """
        store = self._ai_job_store
        if store is None:
            raise RuntimeError("no AIJobStore configured")

        manifest = store.load(durable_job_id)
        provider = manifest.provider
        model = manifest.model

        from .providers import get_api_key, get_provider
        from .llm_client import OpenAICompatClient
        provider_config = get_provider(provider)
        api_key = get_api_key(provider_config)
        client = OpenAICompatClient(provider_config, api_key)

        store.update_status(durable_job_id, "running")

        index = store.next_unfinished(durable_job_id)
        while index is not None:
            chunk = store.read_chunk(durable_job_id, index)
            max_attempts = 4
            backoff = (1.0, 2.0, 4.0)
            import time as _time
            attempts = 0
            while True:
                attempts += 1
                try:
                    result = client.complete(system=SYSTEM_PROMPT, user=chunk, model=model)
                    break
                except Exception as exc:
                    from .llm_client import is_retryable_llm_error
                    if attempts >= max_attempts or not is_retryable_llm_error(exc):
                        raise
                    _time.sleep(backoff[min(attempts - 1, len(backoff) - 1)])

            store.save_result(durable_job_id, index, result)
            if progress_cb is not None:
                progress_cb()
            index = store.next_unfinished(durable_job_id)

        store.merge(durable_job_id)
        final_path = store.job_dir(durable_job_id) / "final.md"
        merged = final_path.read_text(encoding="utf-8")

        if output_path is None:
            output_path = self._durable_output_path(web_job_id, manifest)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(merged, encoding="utf-8", newline="\n")
        return output_path

    def _durable_output_path(self, web_job_id: str, manifest: object) -> Path:
        """Resolve where a durable manifest's final output should be written.

        Uses the private output root captured during hydration
        (``<base>/<web_job_id>/output/<output_basename>``), matching the paths
        :meth:`_hydrate_ai_job_snapshots` reports. ``output_basename`` is
        validated safe by the store (no separators), so the result always stays
        inside the web job's own directory. Falls back to a matching path on an
        existing snapshot when no base root was ever recorded.
        """
        basename = str(getattr(manifest, "output_basename", "") or "")
        base = self._ai_output_base
        if base is not None and basename:
            return base / web_job_id / "output" / basename
        current = self.get(web_job_id)
        if current is not None and basename:
            for out in current.output_paths:
                if out.name == basename:
                    return out
        raise RuntimeError("No output path available to continue the AI job.")

    def _derived_web_outputs(self, web_job_id: str) -> list[Path]:
        """Return one final output path per durable manifest of ``web_job_id``.

        Only manifests that carry an ``output_basename`` produce a path; paths
        are deduplicated and returned sorted by name for stable ordering.
        """
        store = self._ai_job_store
        base = self._ai_output_base
        if store is None or base is None:
            return []
        paths: list[Path] = []
        seen: set[Path] = set()
        for manifest in store.find_by_web_job_id(web_job_id):
            basename = str(manifest.output_basename or "")
            if not basename:
                continue
            candidate = base / web_job_id / "output" / basename
            if candidate in seen:
                continue
            seen.add(candidate)
            paths.append(candidate)
        paths.sort(key=lambda p: p.name)
        return paths

    def _store(
        self,
        job_id: str,
        status: JobStatus,
        message: str,
        output_paths: tuple[Path, ...],
        job_type: str = "",
        *,
        current: int | None = None,
        total: int | None = None,
    ) -> None:
        """Write a result snapshot under the lock and wake any waiters.

        ``created_at`` is captured on the first (queued) snapshot and kept
        stable on every later transition; ``updated_at`` is rewritten to the
        current wall-clock time on every snapshot so callers can see progress.
        ``current``/``total`` default to preserving the previous snapshot's
        values, so a progress update is not reset by a later terminal store.
        ``output_paths`` defaults to the previous snapshot's value when empty,
        so a resume thread does not lose the paths set during hydration.
        """
        with self._cond:
            prev = self._results.get(job_id)
            created_at = prev.created_at if prev is not None else 0.0
            now = time.time()
            if not created_at:
                created_at = now
            if current is None:
                current = prev.current if prev is not None else 0
            if total is None:
                total = prev.total if prev is not None else 0
            if not output_paths and prev is not None and prev.output_paths:
                output_paths = prev.output_paths
            result = JobResult(
                job_id, status, message, output_paths, job_type, created_at, now, current, total
            )
            self._results[job_id] = result
            self._cond.notify_all()
