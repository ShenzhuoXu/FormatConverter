"""Local model-name memory for the web UI's AI proofreading card.

Stores a small list of model names in a git-ignored JSON file at the project
root (``.formatconverter-models.json``). **Only model names are stored --
never an API key.** Names that look like API keys (``sk-...``) are rejected on
save and filtered out on read, so a key can never be stored or returned.
Reads/writes are atomic and serialized with a module lock so the
multi-threaded web server can save/delete concurrently without losing entries.

File format::

    {"models": ["deepseek/deepseek-v4-flash-free", ...]}

A missing or malformed file reads as an empty list (the read path never
creates the file); the file is only created/updated by :func:`add_model` and
:func:`delete_model`.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

from .config import PROJECT_ROOT

__all__ = ["models_path", "list_models", "add_model", "delete_model", "validate_model"]

_MODELS_FILE = ".formatconverter-models.json"
_MAX_MODELS = 50
_MAX_MODEL_LENGTH = 200

# Serializes read-modify-write mutations so concurrent save/delete requests
# can never interleave and drop entries.
_MODELS_LOCK = threading.RLock()


def _is_secret_shaped(value: str) -> bool:
    """True when a trimmed model name looks like an API key, not a model.

    OrcaRouter/OpenRouter model names never start with ``sk-``, so this is a
    safe, explicit boundary against a user pasting an API key into the model
    name field.
    """
    return value.startswith("sk-")


def models_path() -> Path:
    """Return the project-root model-memory file path."""
    return PROJECT_ROOT / _MODELS_FILE


def validate_model(model: str) -> str:
    """Trim and validate a model name; raise :class:`ValueError` when invalid."""
    if not isinstance(model, str):
        raise ValueError("model must be a string")
    value = model.strip()
    if not value:
        raise ValueError("model name must not be empty")
    if _is_secret_shaped(value):
        # Deliberately generic: never echo the rejected value back.
        raise ValueError("model name must not look like an API key")
    if len(value) > _MAX_MODEL_LENGTH:
        raise ValueError(f"model name too long (max {_MAX_MODEL_LENGTH} chars)")
    if "\n" in value or "\r" in value:
        raise ValueError("model name must not contain line breaks")
    return value


def _read_raw(path: Path) -> bytes | None:
    """Read the file's raw bytes; ``None`` when it does not exist.

    A transient Windows sharing violation (e.g. another writer's atomic
    replace, or an antivirus scan) is retried briefly. A persistent error
    propagates so a caller never mistakes an unreadable file for a missing
    one and clobbers it.
    """
    for attempt in range(5):
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            if attempt == 4:
                raise
            time.sleep(0.02)
    return None  # pragma: no cover - the loop always returns or raises


def _read_models(path: Path) -> list[str]:
    """Return the stored model list (empty for a missing/malformed file).

    Entries are deduped (exact, case-sensitive) and trimmed on read so a
    hand-edited file cannot produce duplicates or blank entries. Secret-shaped
    entries (``sk-...``) are dropped so a key saved before this rule existed
    can never be returned. Filtering never raises: a dirty file still reads
    as its remaining legal names rather than failing the whole list.
    """
    try:
        raw = _read_raw(path)
    except OSError:
        return []
    if raw is None:
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return []
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return []
    seen: set[str] = set()
    result: list[str] = []
    for item in models:
        if isinstance(item, str):
            value = item.strip()
            if value and not _is_secret_shaped(value) and value not in seen:
                seen.add(value)
                result.append(value)
    return result


def list_models(path: Path | None = None) -> list[str]:
    """Return the stored model names in file order."""
    p = Path(path) if path is not None else Path(models_path())
    with _MODELS_LOCK:
        return _read_models(p)


def add_model(model: str, path: Path | None = None) -> list[str]:
    """Add ``model`` (deduped, case-sensitive) and return the new list.

    Raises :class:`ValueError` for an invalid name, or when the list already
    holds ``_MAX_MODELS`` entries and ``model`` is not already present.
    """
    value = validate_model(model)
    p = Path(path) if path is not None else Path(models_path())
    with _MODELS_LOCK:
        models = _read_models(p)
        if value not in models:
            if len(models) >= _MAX_MODELS:
                raise ValueError(f"model list is full (max {_MAX_MODELS})")
            models.append(value)
        _atomic_write(p, _encode(models))
        return list(models)


def delete_model(model: str, path: Path | None = None) -> list[str]:
    """Remove ``model`` (exact match after trimming) and return the new list.

    Idempotent: deleting an absent model is a no-op (the list is returned
    unchanged and the file is left untouched).
    """
    value = validate_model(model)
    p = Path(path) if path is not None else Path(models_path())
    with _MODELS_LOCK:
        models = _read_models(p)
        new_models = [m for m in models if m != value]
        if new_models != models:
            _atomic_write(p, _encode(new_models))
        return new_models


def _encode(models: list[str]) -> bytes:
    return (
        json.dumps({"models": models}, ensure_ascii=False, indent=2).encode("utf-8")
        + b"\n"
    )


def _atomic_write(path: Path, content: bytes) -> None:
    """Write ``content`` to ``path`` atomically; never corrupt the target.

    A same-directory temp file is written first and swapped in with
    :func:`os.replace`; a transient Windows lock (antivirus/indexer) is
    retried a few times. Any persistent error propagates and the original file
    is never corrupted.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".formatconverter-models.", suffix=".tmp", dir=str(parent)
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        for attempt in range(5):
            try:
                os.replace(tmp_path, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.02)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
