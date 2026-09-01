"""Byte-level strict parser for the project-root ``.env`` file.

This module reads and writes exactly one local setting:
``ORCAROUTER_API_KEY``. It is a deliberate, minimal stand-in for a full
``dotenv`` library: no shell expansion, no interpolation, no ``export``
handling, and no third-party dependency.

Key precedence (fixed, global)::

    system/process environment variable
        > project-root ``.env`` file
        > not configured

The ``.env`` file is plaintext, git-ignored, and intended only for the
user's own local single-user use. The web/CLI read it on **every** key
resolution (nothing is cached), so a change to the file takes effect
without restarting anything.

All parsing operates on **bytes** so untouched lines are preserved
verbatim (including non-UTF-8 bytes and CRLF endings). The only value that
may ever be written is ``ORCAROUTER_API_KEY``; every other line is left
byte-for-byte identical.

Accepted boundary: an inline ``#`` inside a value is treated as literal
text and is **not** stripped as a comment (this is a strict parser, not a
shell). The ``.env`` file is never created by this module's read path and
is only ever created/updated by :func:`write_env_key`.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .config import PROJECT_ROOT

__all__ = [
    "dotenv_path",
    "read_env_key",
    "write_env_key",
    "delete_env_key",
    "key_status",
]

_ENV_KEY = b"ORCAROUTER_API_KEY"


def dotenv_path() -> Path:
    """Return the project-root ``.env`` path (imported from :mod:`.config`)."""
    return PROJECT_ROOT / ".env"


def _is_target_line(line: bytes) -> bool:
    """True when ``line`` assigns a value to ``ORCAROUTER_API_KEY``.

    Leading spaces, a UTF-8 BOM, and trailing spaces around the key are
    tolerated; comment lines (first non-whitespace char ``#``) are not
    assignments. A missing ``=`` is not an assignment.
    """
    stripped = line.lstrip(b" \t\xef\xbb\xbf")
    if not stripped or stripped.startswith(b"#"):
        return False
    key, sep, _value = stripped.partition(b"=")
    if not sep:
        return False
    return key.rstrip(b" \t") == _ENV_KEY


def _first_target_index(lines: list[bytes]) -> int | None:
    """Return the index of the first ``ORCAROUTER_API_KEY`` line, or None."""
    for i, line in enumerate(lines):
        if _is_target_line(line):
            return i
    return None


def read_env_key(path: Path | None = None) -> str | None:
    """Return the first non-empty ``ORCAROUTER_API_KEY`` value from ``.env``.

    ``path`` defaults to :func:`dotenv_path`. A missing or unreadable file
    yields ``None`` (any read ``OSError`` is treated as "no key"). Comment
    lines, blank lines, and unrelated keys are skipped. An empty/blank value
    (including ``""`` / ``''``) counts as unset. The returned value is
    decoded as UTF-8 with ``errors="replace"``.
    """
    p = Path(path) if path is not None else dotenv_path()
    try:
        raw = p.read_bytes()
    except OSError:
        return None
    for line in raw.split(b"\n"):
        stripped = line.lstrip(b" \t\xef\xbb\xbf")
        if not stripped or stripped.startswith(b"#"):
            continue
        key, sep, value = stripped.partition(b"=")
        if not sep:
            continue
        if key.rstrip(b" \t") != _ENV_KEY:
            continue
        val = value.strip(b" \t\r")
        if len(val) >= 2 and val[:1] in (b'"', b"'") and val[-1:] == val[:1]:
            val = val[1:-1]
        if not val:
            continue  # empty/blank value counts as unset; keep scanning
        return val.decode("utf-8", errors="replace")
    return None


def write_env_key(value: str, path: Path | None = None) -> None:
    """Write ``value`` as the canonical ``ORCAROUTER_API_KEY`` line in ``.env``.

    ``value`` must be a non-empty ``str`` (:class:`ValueError` otherwise).
    The canonical line is written with the value wrapped in double quotes.

    - If the file exists, the **first** target line is replaced in place
      (preserving that line's CRLF ``\\r`` suffix when it is CRLF) and any
      additional duplicate target lines are deleted.
    - If no target line exists, the new line is appended: before a trailing
      empty element when the file ends with ``\\n``, otherwise the line is
      appended followed by a trailing ``\\n`` (adding a newline before it
      when the file lacked one). CRLF is used when any existing line is CRLF.
    - All other lines are preserved byte-for-byte.
    - If the file does not exist it is created with the new line + ``\\n``.

    The write is atomic: a temp file is written in the same directory and
    ``os.replace``-d over the target. On any exception the temp file is
    removed and the original ``.env`` is never corrupted.
    """
    if not isinstance(value, str) or not value:
        raise ValueError("value must be a non-empty string")

    p = Path(path) if path is not None else dotenv_path()
    line_body = b'ORCAROUTER_API_KEY="' + value.encode("utf-8") + b'"'

    try:
        raw = p.read_bytes()
    except OSError:
        raw = None

    if raw is None or raw == b"":
        content = line_body + b"\n"
    else:
        lines = raw.split(b"\n")
        trailing_empty = bool(lines and lines[-1] == b"")
        target_idx = _first_target_index(lines)
        if target_idx is not None:
            newline_suffix = b"\r" if lines[target_idx].endswith(b"\r") else b""
            new_lines = list(lines[:target_idx])
            new_lines.append(line_body + newline_suffix)
            for line in lines[target_idx + 1:]:
                if not _is_target_line(line):
                    new_lines.append(line)
        else:
            newline_suffix = b"\r" if any(line.endswith(b"\r") for line in lines) else b""
            new_lines = list(lines)
            if trailing_empty:
                new_lines.insert(-1, line_body + newline_suffix)
            else:
                new_lines.append(line_body + newline_suffix)
                new_lines.append(b"")
        content = b"\n".join(new_lines)

    _atomic_write(p, content)


def delete_env_key(path: Path | None = None) -> None:
    """Remove every ``ORCAROUTER_API_KEY`` line from ``.env``.

    All other lines are preserved byte-for-byte. Idempotent: a missing file
    or a file with no target line is a no-op. If nothing remains, an empty
    file is left in place (the file itself is never deleted).
    """
    p = Path(path) if path is not None else dotenv_path()
    try:
        raw = p.read_bytes()
    except OSError:
        return
    lines = raw.split(b"\n")
    new_lines = [line for line in lines if not _is_target_line(line)]
    if len(new_lines) == len(lines):
        return
    content = b"\n".join(new_lines)
    _atomic_write(p, content)


def key_status(
    env_var: str = "ORCAROUTER_API_KEY", path: Path | None = None
) -> tuple[bool, str]:
    """Report configured/source according to the global key precedence.

    Returns ``(True, "environment")`` when the process environment variable
    is set and non-blank, else ``(True, "dot_env")`` when ``.env`` has a
    non-empty value, else ``(False, "none")``.
    """
    env_value = os.environ.get(env_var)
    if env_value and env_value.strip():
        return (True, "environment")
    dotenv_value = read_env_key(path)
    if dotenv_value:
        return (True, "dot_env")
    return (False, "none")


def _atomic_write(path: Path, content: bytes) -> None:
    """Write ``content`` to ``path`` atomically; never corrupt the target."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=str(parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
