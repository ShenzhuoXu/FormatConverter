"""Durable AI job store with atomic chunk/result/merge checkpointing.

Every file written to the ``.formatconverter-jobs/<job_id>/`` directory is
created atomically via a ``.tmp`` rename.  The store is designed so that a
crash or power loss mid-write leaves either the old file or the new file
complete, never a partial file.

Directory layout::

    .formatconverter-jobs/
        <job_id>/
            manifest.json
            input.md
            separators.json
            chunks/
                0001.txt
                0002.txt
                ...
            results/
                0001.md
                0002.md
                ...
            final.md

Key invariants
--------------
- ``next_unfinished()`` does **not** trust the manifest's ``chunks[*].status``
  alone: it also checks whether the corresponding ``results/NNNN.md`` file
  exists and is readable.  This is the core checkpoint recovery constraint.
- ``merge()`` is a separate stage that reassembles results using the original
  separators.  It never calls the AI client.
- No API key, raw provider response, or secret-shaped string is ever written
  to the manifest or any other file.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Sequence

from .ai_cleaner import split_into_chunks
from .model_store import validate_model

# A valid job id is a 32-character lowercase hex string (uuid4 hex).
_JOB_ID_RE = re.compile(r"[0-9a-f]{32}")

# Sub-directories inside a job's private directory.
_CHUNKS_DIR = "chunks"
_RESULTS_DIR = "results"


# ---------------------------------------------------------------------------
# Manifest data model
# ---------------------------------------------------------------------------


@dataclass
class ChunkEntry:
    """One entry in the manifest's ``chunks`` list."""

    index: int
    chars: int
    status: str = "pending"


@dataclass
class AIJobManifest:
    """In-memory representation of a durable AI job's manifest.json.

    ``web_job_id`` links back to the Web-level :class:`JobResult` that
    submitted this durable job.  When a single Web job submits multiple
    durable jobs (e.g. multi-file ``ai-clean``), each durable manifest
    carries the same ``web_job_id``.
    """

    job_id: str
    type: str = "ai-clean"
    status: str = "running"
    provider: str = ""
    model: str = ""
    max_chars: int = 12000
    total_chunks: int = 0
    current_chunk: int = 0
    chunks: list[ChunkEntry] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
    web_job_id: str = ""
    output_basename: str = ""


def _manifest_to_dict(m: AIJobManifest) -> dict:
    d = asdict(m)
    return d


def _manifest_from_dict(d: dict) -> AIJobManifest:
    chunks = [ChunkEntry(**c) for c in d.get("chunks", [])]
    return AIJobManifest(
        job_id=d["job_id"],
        type=d.get("type", "ai-clean"),
        status=d.get("status", "running"),
        provider=d.get("provider", ""),
        model=d.get("model", ""),
        max_chars=d.get("max_chars", 12000),
        total_chunks=d.get("total_chunks", 0),
        current_chunk=d.get("current_chunk", 0),
        chunks=chunks,
        created_at=d.get("created_at", 0.0),
        updated_at=d.get("updated_at", 0.0),
        web_job_id=d.get("web_job_id", ""),
        output_basename=d.get("output_basename", ""),
    )


# ---------------------------------------------------------------------------
# Atomic write helpers
# ---------------------------------------------------------------------------


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _atomic_write_json(path: Path, obj: object) -> None:
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    _atomic_write_text(path, text)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _is_safe_output_basename(name: str) -> bool:
    """True when ``name`` is a safe basename with no path separators."""
    if not name:
        return True
    if os.sep in name or (os.altsep is not None and os.altsep in name):
        return False
    if name in (".", ".."):
        return False
    if not name.strip():
        return False
    if os.path.isabs(name):
        return False
    return True


# ---------------------------------------------------------------------------
# AIJobStore
# ---------------------------------------------------------------------------


class AIJobStore:
    """Disk-backed durable AI job checkpoint store.

    Parameters
    ----------
    root:
        The root directory for all durable job data (default
        ``.formatconverter-jobs`` under the current working directory).
    """

    def __init__(self, root: Path | None = None) -> None:
        if root is None:
            root = Path.cwd() / ".formatconverter-jobs"
        self._root = root.resolve()

    # -- public API ---------------------------------------------------------

    def create_job(
        self,
        input_path: Path,
        text: str,
        provider: str,
        model: str,
        max_chars: int,
        web_job_id: str = "",
        output_basename: str = "",
    ) -> AIJobManifest:
        """Split ``text`` into chunks, write manifest / input / chunks / separators.

        Raises :class:`AIJobError` if ``model`` looks like an API key
        (``sk-`` prefix) or if ``web_job_id`` is non-empty but not a valid
        32-character lowercase hex string — no files are written in that case.

        Returns the newly created :class:`AIJobManifest`.
        """
        try:
            model = validate_model(model)
        except ValueError as exc:
            raise AIJobError("model name must not look like an API key") from exc

        if web_job_id and not _JOB_ID_RE.fullmatch(web_job_id):
            raise AIJobError("invalid web_job_id")

        if output_basename and not _is_safe_output_basename(output_basename):
            raise AIJobError("invalid output basename")

        chunks, separators = split_into_chunks(text, max_chars=max_chars)

        job_id = uuid.uuid4().hex
        now = time.time()

        chunk_entries: list[ChunkEntry] = []
        for i, chunk_text in enumerate(chunks, start=1):
            chunk_entries.append(ChunkEntry(index=i, chars=len(chunk_text), status="pending"))

        manifest = AIJobManifest(
            job_id=job_id,
            provider=provider,
            model=model,
            max_chars=max_chars,
            total_chunks=len(chunks),
            current_chunk=0,
            chunks=chunk_entries,
            created_at=now,
            updated_at=now,
            web_job_id=web_job_id,
            output_basename=output_basename,
        )

        job_dir = self.job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)

        _atomic_write_json(job_dir / "manifest.json", _manifest_to_dict(manifest))
        _atomic_write_text(job_dir / "input.md", text)
        _atomic_write_json(job_dir / "separators.json", separators)

        if chunks:
            chunks_dir = job_dir / _CHUNKS_DIR
            chunks_dir.mkdir(parents=True, exist_ok=True)
            for i, chunk_text in enumerate(chunks, start=1):
                chunk_path = chunks_dir / f"{i:04d}.txt"
                _atomic_write_text(chunk_path, chunk_text)

        return manifest

    def load(self, job_id: str) -> AIJobManifest:
        """Load a manifest from disk.

        Raises :class:`AIJobError` if the job directory does not exist or the
        manifest is corrupted or missing.
        """
        path = self.job_dir(job_id) / "manifest.json"
        if not path.is_file():
            raise AIJobError(f"Manifest not found for job {job_id!r}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise AIJobError(
                f"Could not read manifest for job {job_id!r}: {exc}"
            ) from exc
        if not isinstance(raw, dict) or "job_id" not in raw:
            raise AIJobError(f"Corrupted manifest for job {job_id!r}")
        return _manifest_from_dict(raw)

    def job_dir(self, job_id: str) -> Path:
        """Return the absolute path to the job's private directory.

        Raises :class:`AIJobError` if ``job_id`` is not a valid 32-char hex
        string, preventing path traversal.
        """
        if not isinstance(job_id, str) or _JOB_ID_RE.fullmatch(job_id) is None:
            raise AIJobError(f"Invalid job id: {job_id!r}")
        return self._root / job_id

    def read_chunk(self, job_id: str, index: int) -> str:
        """Read a previously saved chunk text by 1-based index."""
        path = self.job_dir(job_id) / _CHUNKS_DIR / f"{index:04d}.txt"
        if not path.is_file():
            raise AIJobError(f"Chunk {index} not found for job {job_id!r}")
        try:
            return _read_text(path)
        except OSError as exc:
            raise AIJobError(
                f"Could not read chunk {index} for job {job_id!r}: {exc}"
            ) from exc

    def save_result(self, job_id: str, index: int, text: str) -> None:
        """Atomically write a chunk result to ``results/NNNN.md`` and update the manifest.

        The manifest's ``current_chunk`` is updated to ``index`` and the
        corresponding chunk entry's status is set to ``"completed"``.
        """
        job_dir = self.job_dir(job_id)
        results_dir = job_dir / _RESULTS_DIR
        results_dir.mkdir(parents=True, exist_ok=True)

        result_path = results_dir / f"{index:04d}.md"
        _atomic_write_text(result_path, text)

        manifest = self.load(job_id)
        manifest.current_chunk = index
        manifest.updated_at = time.time()
        for entry in manifest.chunks:
            if entry.index == index:
                entry.status = "completed"
                break
        if manifest.status == "running":
            manifest.status = "running"
        _atomic_write_json(job_dir / "manifest.json", _manifest_to_dict(manifest))

    def update_status(self, job_id: str, status: str, current_chunk: int | None = None) -> None:
        """Update the manifest's status (and optionally current_chunk) in place."""
        job_dir = self.job_dir(job_id)
        manifest = self.load(job_id)
        manifest.status = status
        manifest.updated_at = time.time()
        if current_chunk is not None:
            manifest.current_chunk = current_chunk
        _atomic_write_json(job_dir / "manifest.json", _manifest_to_dict(manifest))

    def next_unfinished(self, job_id: str) -> int | None:
        """Return the 1-based index of the first unfinished chunk, or ``None``.

        This method checks the **actual** ``results/NNNN.md`` file on disk,
        *not* the manifest's ``chunks[*].status``.  If the result file exists
        and is readable, the chunk is considered finished regardless of what
        the manifest says.  If the result file is missing or unreadable, the
        chunk is unfinished — even if the manifest claims it is completed.
        """
        manifest = self.load(job_id)
        job_dir = self.job_dir(job_id)
        results_dir = job_dir / _RESULTS_DIR

        for entry in manifest.chunks:
            result_path = results_dir / f"{entry.index:04d}.md"
            if result_path.is_file():
                try:
                    _read_text(result_path)
                    continue
                except OSError:
                    pass
            return entry.index
        return None

    def merge(self, job_id: str) -> Path:
        """Reassemble all completed chunk results using the original separators.

        Returns the path to the newly written ``final.md``.

        Status is set to ``"merging"`` before the merge and ``"completed"``
        after the atomic write of ``final.md``.  If the merge fails (e.g. a
        result file is missing), the status remains ``"merging"`` and the
        exception propagates — the AI client is never called again.
        """
        job_dir = self.job_dir(job_id)
        manifest = self.load(job_id)

        if manifest.status != "completed":
            self.update_status(job_id, "merging")

        manifest = self.load(job_id)

        separators: list[str] = json.loads(
            _read_text(job_dir / "separators.json")
        )
        results_dir = job_dir / _RESULTS_DIR

        result_texts: list[str] = []
        for entry in manifest.chunks:
            result_path = results_dir / f"{entry.index:04d}.md"
            if not result_path.is_file():
                raise AIJobError(
                    f"Cannot merge job {job_id!r}: chunk {entry.index} result file missing"
                )
            result_texts.append(_read_text(result_path))

        parts: list[str] = []
        for i, result_text in enumerate(result_texts):
            parts.append(separators[i])
            parts.append(result_text)
        parts.append(separators[len(result_texts)])
        merged = "".join(parts)

        final_path = job_dir / "final.md"
        _atomic_write_text(final_path, merged)
        self.update_status(job_id, "completed")
        return final_path

    def mark_stale_running_interrupted(self) -> None:
        """Scan all job directories, set running/merging manifests to interrupted."""
        if not self._root.is_dir():
            return
        for entry in list(self._root.iterdir()):
            if not entry.is_dir():
                continue
            job_id = entry.name
            if not _JOB_ID_RE.fullmatch(job_id):
                continue
            try:
                manifest = self.load(job_id)
            except AIJobError:
                continue
            if manifest.status in ("running", "merging"):
                self.update_status(job_id, "interrupted")

    def scan_recent(self, limit: int = 20) -> list[AIJobManifest]:
        """Return most recent manifests, skipping corrupt/invalid dirs."""
        if not self._root.is_dir():
            return []
        results: list[AIJobManifest] = []
        for entry in self._root.iterdir():
            if not entry.is_dir():
                continue
            job_id = entry.name
            if not _JOB_ID_RE.fullmatch(job_id):
                continue
            try:
                m = self.load(job_id)
                if m.web_job_id and not _JOB_ID_RE.fullmatch(m.web_job_id):
                    continue
                if not _is_safe_output_basename(m.output_basename):
                    continue
                results.append(m)
            except AIJobError:
                continue
        results.sort(key=lambda m: m.updated_at, reverse=True)
        return results[:limit]

    def completed_count(self, job_id: str) -> int:
        """Count result files on disk, only for chunk indices in the manifest."""
        manifest = self.load(job_id)
        results_dir = self.job_dir(job_id) / _RESULTS_DIR
        if not results_dir.is_dir():
            return 0
        count = 0
        for entry in manifest.chunks:
            result_path = results_dir / f"{entry.index:04d}.md"
            if result_path.is_file():
                try:
                    _read_text(result_path)
                    count += 1
                except OSError:
                    pass
        return count

    def find_by_web_job_id(self, web_job_id: str) -> list[AIJobManifest]:
        """Return every manifest whose ``web_job_id`` equals ``web_job_id``.

        Unlike :meth:`scan_recent`, this method is **unbounded** and never
        drops old jobs, so resume/retry can always locate a job's durable
        checkpoints regardless of how many newer jobs exist.

        ``web_job_id`` is never used as a path component (job directories are
        keyed by the durable ``job_id``, and every one of those is validated
        against the 32-hex pattern), so this lookup cannot traverse the store
        root. Corrupt manifests and unsafe ``output_basename`` values are
        skipped, mirroring :meth:`scan_recent`. Results are ordered newest
        first. A syntactically invalid ``web_job_id`` simply matches nothing.
        """
        if not isinstance(web_job_id, str) or not _JOB_ID_RE.fullmatch(web_job_id):
            return []
        if not self._root.is_dir():
            return []
        results: list[AIJobManifest] = []
        for entry in self._root.iterdir():
            if not entry.is_dir():
                continue
            job_id = entry.name
            if not _JOB_ID_RE.fullmatch(job_id):
                continue
            try:
                manifest = self.load(job_id)
            except AIJobError:
                continue
            if manifest.web_job_id != web_job_id:
                continue
            if not _is_safe_output_basename(manifest.output_basename):
                continue
            results.append(manifest)
        results.sort(key=lambda m: m.updated_at, reverse=True)
        return results

    def delete_job(self, job_id: str) -> bool:
        """Delete one durable job directory; return True if it existed.

        ``job_id`` must be a valid 32-character lowercase hex string —
        :meth:`job_dir` rejects anything else with :class:`AIJobError`, so an
        arbitrary value (``..``, ``../x``, ``C:\\...``) can never resolve to a
        path outside the store root. As a second line of defense the resolved
        directory is re-checked to be strictly inside ``self._root``.
        """
        path = self.job_dir(job_id)
        root = self._root
        try:
            inside = path != root and path.is_relative_to(root)
        except (OSError, ValueError):
            inside = False
        if not inside:
            raise AIJobError("Refusing to delete a path outside the job store")
        if not path.exists():
            return False
        shutil.rmtree(path)
        return True

    def delete_web_job(self, web_job_id: str) -> int:
        """Delete every durable job directory linked to ``web_job_id``.

        Validates ``web_job_id`` up front (32-hex pattern) and raises
        :class:`AIJobError` for anything else, so a caller can never use this
        method to target arbitrary paths. Returns the number of directories
        actually removed (0 when none exist).
        """
        if not isinstance(web_job_id, str) or not _JOB_ID_RE.fullmatch(web_job_id):
            raise AIJobError("invalid web_job_id")
        removed = 0
        for manifest in self.find_by_web_job_id(web_job_id):
            try:
                if self.delete_job(manifest.job_id):
                    removed += 1
            except AIJobError:
                # A corrupt manifest whose directory vanished mid-scan is not
                # an error the caller needs to see; keep removing the rest.
                continue
        return removed


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AIJobError(Exception):
    """Base error for durable AI job store operations."""


# ---------------------------------------------------------------------------
# Status mapping helpers
# ---------------------------------------------------------------------------


_JOB_STATUS_MAP: dict[str, str] = {
    "running": "running",
    "merging": "running",
    "interrupted": "interrupted",
    "completed": "succeeded",
    "failed": "failed",
}


def map_manifest_status(status: str) -> str:
    """Map a manifest internal status string to the ``JobStatus`` value string."""
    return _JOB_STATUS_MAP.get(status, "running")


def aggregate_web_job_status(manifests: Sequence[AIJobManifest]) -> str:
    """Aggregate one web job's durable manifests into a single internal status.

    Mirrors the classification the Web layer shows for a multi-file job:
    ``interrupted`` when any manifest is interrupted (or non-terminal, e.g.
    ``merging``), ``completed`` only when every manifest is completed, and
    ``failed`` only when every manifest failed. A mix of completed and failed
    manifests (a partially finished multi-file job) reports ``interrupted`` so
    it can still be continued.

    Raises :class:`AIJobError` for an empty list.
    """
    if not manifests:
        raise AIJobError("cannot aggregate an empty manifest list")
    has_interrupted = False
    all_completed = True
    all_failed = True
    for m in manifests:
        if m.status == "interrupted":
            has_interrupted = True
            all_completed = False
            all_failed = False
        elif m.status == "completed":
            all_failed = False
        elif m.status == "failed":
            all_completed = False
        else:
            # running / merging / unknown: not terminal in either direction.
            all_completed = False
            all_failed = False
    if has_interrupted:
        return "interrupted"
    if all_completed:
        return "completed"
    if all_failed:
        return "failed"
    return "interrupted"