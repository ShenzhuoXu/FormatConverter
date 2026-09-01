"""Localhost-only web service for FormatConverter.

This module exposes a small JSON task API (plus optional, protected static
file serving) over plain HTTP bound exclusively to the loopback interface.
There is deliberately no external network exposure:

- The UI may save the user's OrcaRouter API key into a git-ignored
  project-root ``.env`` file; keys are never stored in the browser or in
  any git-tracked file.
- The HTTP layer never logs an API key; AI tasks resolve their key deep
  inside :mod:`format_converter.jobs`, far away from this module.
- Responses never carry cross-origin (CORS) headers.
- Uploads are written only into a fresh per-job temporary directory created
  under the server's own temporary root, which is deleted on shutdown.
- Key write/delete endpoints require a memory-only session token plus
  loopback ``Host``/``Origin`` headers (see :meth:`JobWebServer._auth_ok`).

Only the Python standard library and this package's modules are imported.
"""

from __future__ import annotations

import argparse
import base64
import io
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .env_store import delete_env_key, key_status, write_env_key
from .jobs import JobManager, JobStatus, UnknownJobTypeError

__all__ = [
    "JobWebServer",
    "create_server",
    "DEFAULT_STATIC_DIR",
    "run_server",
    "main",
    "ServerStartError",
]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

# Packaged single-page UI, served by default when present.
DEFAULT_STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"

# Only loopback addresses may ever be requested. Anything else is rejected at
# construction/serve time with ValueError and the socket is never opened.

# Valid job ids are 32-char lowercase hex uuid4 values (as produced by
# uuid.uuid4().hex). Anything else must never be treated as a path component.
_JOB_ID_RE = re.compile(r"[0-9a-f]{32}")

# Prefix under which protected static files are served (when configured).
STATIC_PREFIX = "/static/"

# Ceiling for a single request body, to avoid unbounded memory use.
MAX_BODY_BYTES = 256 * 1024 * 1024

# Ceiling for the small JSON body of the API-key save endpoint.
MAX_KEY_BODY_BYTES = 16 * 1024

# File extensions the web layer accepts per job type (matched lowercased).
_ALLOWED_EXTENSIONS: dict[str, frozenset[str]] = {
    "convert": frozenset({".pdf"}),
    "pipeline": frozenset({".pdf"}),
    "clean": frozenset({".md"}),
    "ai-clean": frozenset({".md"}),
}

_INDEX_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>FormatConverter — local web service</title>
</head>
<body>
<h1>FormatConverter Web Service</h1>
<p>This service runs on <code>localhost</code> only and exposes a small JSON API.</p>
<h2>Endpoints</h2>
<ul>
  <li><code>GET /health</code> — health check</li>
  <li><code>POST /api/jobs</code> — submit a job (<code>convert</code>, <code>clean</code>, <code>pipeline</code>, <code>ai-clean</code>)</li>
  <li><code>GET /api/jobs/{id}</code> — job status</li>
  <li><code>GET /api/jobs/{id}/download</code> — download job output as a ZIP archive</li>
</ul>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _is_loopback(host: str) -> bool:
    """Return True only for genuine loopback addresses.

    ``localhost`` is accepted by name; everything else must parse as an IP
    address that reports itself as loopback (``127.0.0.0/8`` and ``::1``).
    Non-IP host names such as ``127.0.0.1.evil`` are therefore rejected.
    """
    host = host.strip().lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_loopback_host(host: str) -> bool:
    """True when a ``Host`` header value names a loopback address.

    Accepts ``127.0.0.1``, ``127.0.0.1:8765``, ``localhost``, and
    ``[::1]:8765``; the optional port is stripped before the loopback check.
    Anything else (including DNS-rebinding names such as
    ``127.0.0.1.evil``) is rejected.
    """
    if not host:
        return False
    host = host.strip()
    try:
        hostname = urllib.parse.urlsplit("//" + host).hostname or ""
    except ValueError:
        return False
    return _is_loopback(hostname)


def _is_loopback_origin(origin: str) -> bool:
    """True when an ``Origin`` header value is a loopback http(s) origin.

    The scheme must be ``http`` or ``https`` and the hostname must be a
    genuine loopback address. ``null``, a missing value, or a non-loopback
    host (e.g. ``http://127.0.0.1.evil.com``) are all rejected.
    """
    if not origin:
        return False
    try:
        parts = urllib.parse.urlsplit(origin)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    hostname = parts.hostname or ""
    return _is_loopback(hostname)


def _bool_param(params: dict, name: str, default: bool) -> bool:
    """Read a genuine JSON boolean, ignoring string/integer lookalikes.

    Mirrors the JobManager contract that booleans must be real ``bool`` values;
    ``"false"``/``0`` must not silently become truthy.
    """
    value = params.get(name)
    if isinstance(value, bool):
        return value
    return default


def _safe_upload_filename(name: object) -> str | None:
    """Return the sanitized upload filename, or None when it is unsafe.

    Only a bare basename is acceptable: no path separators (``/`` or ``\\``),
    no ``.``/``..``, and a non-empty string.
    """
    if not isinstance(name, str):
        return None
    name = name.strip()
    if not name:
        return None
    if "/" in name or "\\" in name:
        return None
    if name in (".", ".."):
        return None
    return name


# ---------------------------------------------------------------------------
# JobManager with caller-suppliable job ids
# ---------------------------------------------------------------------------


class _IdAwareJobManager(JobManager):
    """JobManager whose :meth:`submit` accepts a pre-assigned ``job_id``.

    The web layer needs to create ``<base>/<job_id>`` and write the uploaded
    file *before* the worker thread starts, otherwise the handler could race
    ahead of the file being written. Pre-assigning the id closes that race.
    """

    def submit(self, job_type: str, params: dict, *, job_id: str | None = None) -> str:
        handler = self.handlers.get(job_type)
        if handler is None:
            raise UnknownJobTypeError(job_type, tuple(self.handlers))
        if not isinstance(params, dict):
            raise TypeError(f"params must be a dict, got {type(params).__name__}")
        params = dict(params)
        if job_id is None:
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


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------


class _JobHTTPServer(ThreadingHTTPServer):
    """Threaded HTTP server carrying a back-reference to its JobWebServer."""

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        web_server: "JobWebServer",
    ) -> None:
        super().__init__(server_address, handler_class)
        self.web_server = web_server


class _Handler(BaseHTTPRequestHandler):
    """Request handler: routes every request to the owning JobWebServer.

    ``log_message`` is overridden so the server only ever records the HTTP
    method, the request path (without the query string), and the status code —
    never the request body, headers, or parameters.
    """

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        try:
            self.server.web_server._handle_get(self)
        except Exception:  # noqa: BLE001 - never let a handler crash the thread
            self.server.web_server._send_json(self, 500, {"error": "Internal server error."})

    def do_POST(self) -> None:
        try:
            self.server.web_server._handle_post(self)
        except Exception:  # noqa: BLE001
            self.server.web_server._send_json(self, 500, {"error": "Internal server error."})

    def do_DELETE(self) -> None:
        try:
            self.server.web_server._handle_delete(self)
        except Exception:  # noqa: BLE001
            self.server.web_server._send_json(self, 500, {"error": "Internal server error."})

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A002
        # fmt/args are deliberately ignored: we emit a fixed, sanitized shape.
        # ``command``/``path`` may be missing or None when parse_request()
        # rejects a malformed request line, so never assume they are set.
        method = getattr(self, "command", None) or "?"
        raw_path = getattr(self, "path", None)
        self.server.web_server._log_sanitized(method, raw_path, args)


# ---------------------------------------------------------------------------
# Web server
# ---------------------------------------------------------------------------


class JobWebServer:
    """Localhost-only HTTP server for FormatConverter task submission.

    Parameters
    ----------
    host:
        Loopback host name accepted for validation only (the socket is always
        bound to ``127.0.0.1``). Non-loopback values raise :class:`ValueError`.
    port:
        TCP port to bind; ``0`` selects a random free port.
    static_dir:
        Optional directory served under ``/static/`` with path-traversal
        protection. ``None`` disables static serving.
    manager:
        Optional :class:`~format_converter.jobs.JobManager`. Defaults to a
        private instance whose job ids the web layer can pre-assign.
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        *,
        static_dir: str | os.PathLike[str] | None = None,
        manager: JobManager | None = None,
    ) -> None:
        if not _is_loopback(host):
            raise ValueError(
                f"Refusing to bind to non-loopback host {host!r}. "
                "This service may only run on localhost."
            )
        self._host = host
        self._port = port
        self._static_dir = Path(static_dir).resolve() if static_dir is not None else None

        if manager is None:
            manager = _IdAwareJobManager()
        self._manager = manager
        self._id_aware = isinstance(manager, _IdAwareJobManager)

        # Per-server temporary root; each job gets a private sub-directory.
        self._base: Path | None = Path(tempfile.mkdtemp(prefix="format-converter-web-"))
        # Only used when a caller-supplied (non id-aware) manager is in use:
        # job_id -> staging directory.
        self._job_dirs: dict[str, Path] = {}

        self._lock = threading.RLock()
        self._server: _JobHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._bound_port: int | None = None

        # Random, memory-only session token: never written to disk and only
        # valid for the lifetime of this server process. It is injected into
        # the served index.html so the UI can authenticate key write/delete.
        self._session_token = secrets.token_urlsafe(32)

    # -- public API ---------------------------------------------------------

    @property
    def base_temp_dir(self) -> Path | None:
        """The server's temporary root (None after :meth:`shutdown`)."""
        return self._base

    @property
    def port(self) -> int | None:
        """The port the server is bound to (None until :meth:`serve`)."""
        return self._bound_port

    def serve(self, host: str | None = None, port: int | None = None) -> int:
        """Bind the loopback socket and start serving in a daemon thread.

        Returns the actual bound port (useful when ``port=0``). Non-loopback
        ``host`` raises :class:`ValueError` before any socket is opened.
        """
        effective_host = self._host if host is None else host
        effective_port = self._port if port is None else port
        if not _is_loopback(effective_host):
            raise ValueError(
                f"Refusing to bind to non-loopback host {effective_host!r}. "
                "This service may only run on localhost."
            )
        with self._lock:
            if self._server is not None:
                raise RuntimeError("server is already running")
            server = _JobHTTPServer(("127.0.0.1", effective_port), _Handler, self)
            self._server = server
            self._bound_port = server.server_address[1]
            thread = threading.Thread(
                target=server.serve_forever,
                name="format-converter-web",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            return self._bound_port

    def shutdown(self) -> None:
        """Stop the HTTP server and delete the server's temporary root.

        Safe to call more than once and safe to call when :meth:`serve` was
        never reached (or raised).
        """
        with self._lock:
            server, thread = self._server, self._thread
            self._server = None
            self._thread = None
            base = self._base
            self._base = None
            self._bound_port = None
        if server is not None:
            try:
                server.shutdown()
            except Exception:  # noqa: BLE001 - best effort
                pass
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)
        if base is not None:
            shutil.rmtree(base, ignore_errors=True)

    def cleanup_job(self, job_id: str) -> None:
        """Delete one job's temporary directory (best effort).

        Only ever removes a directory that is strictly inside the server's own
        temporary root. Job ids that are not 32-char hex uuids (or that resolve
        outside the root) are ignored, so this can never delete a parent or
        arbitrary directory.
        """
        with self._lock:
            job_dir = self._job_dir_for(job_id)
            self._job_dirs.pop(job_id, None)
        base = self._base
        if job_dir is not None and base is not None:
            try:
                inside = job_dir.resolve().is_relative_to(base.resolve())
            except (OSError, ValueError):
                inside = False
            if not inside:
                job_dir = None
        if job_dir is not None:
            shutil.rmtree(job_dir, ignore_errors=True)

    # -- request dispatch ---------------------------------------------------

    def _handle_get(self, handler: BaseHTTPRequestHandler) -> None:
        path = urllib.parse.urlsplit(handler.path).path
        if path == "/":
            self._send_index(handler)
        elif path == "/health":
            self._send_json(handler, 200, {"status": "ok"})
        elif path == "/api/ai/key-status":
            self._send_key_status(handler)
        elif path.startswith(STATIC_PREFIX):
            self._serve_static(handler, path[len(STATIC_PREFIX):])
        elif path.startswith("/api/jobs/"):
            rest = path[len("/api/jobs/"):].rstrip("/")
            parts = rest.split("/")
            if len(parts) == 1 and parts[0]:
                self._send_job_status(handler, parts[0])
            elif len(parts) == 2 and parts[0] and parts[1] == "download":
                self._send_job_download(handler, parts[0])
            else:
                self._not_found(handler)
        else:
            self._not_found(handler)

    def _handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        path = urllib.parse.urlsplit(handler.path).path
        if path == "/api/jobs":
            self._handle_submit(handler)
        elif path == "/api/ai/key":
            self._handle_save_key(handler)
        else:
            self._not_found(handler)

    def _handle_delete(self, handler: BaseHTTPRequestHandler) -> None:
        path = urllib.parse.urlsplit(handler.path).path
        if path == "/api/ai/key":
            self._handle_delete_key(handler)
        else:
            self._not_found(handler)

    # -- local API-key configuration ---------------------------------------

    def _auth_ok(self, handler: BaseHTTPRequestHandler) -> bool:
        """True only for a loopback, session-token-authenticated key request.

        Requires a loopback ``Host`` header, a loopback http(s) ``Origin``
        header, and an ``X-FC-Session-Token`` header equal (constant-time) to
        this server's memory-only session token. Any failure returns False.
        """
        host = handler.headers.get("Host", "")
        origin = handler.headers.get("Origin", "")
        if not _is_loopback_host(host) or not _is_loopback_origin(origin):
            return False
        provided = handler.headers.get("X-FC-Session-Token", "")
        return secrets.compare_digest(provided, self._session_token)

    def _handle_save_key(self, handler: BaseHTTPRequestHandler) -> None:
        """Validate and save the user's API key into the project ``.env``."""
        if not self._auth_ok(handler):
            return self._send_json(handler, 403, {"error": "Forbidden."})
        try:
            length = int(handler.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0:
            return self._send_json(handler, 400, {"error": "Missing or empty request body."})
        if length > MAX_KEY_BODY_BYTES:
            return self._send_json(handler, 413, {"error": "Request body too large."})
        body = handler.rfile.read(length)

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._send_json(handler, 400, {"error": "Invalid JSON body."})
        if not isinstance(payload, dict):
            return self._send_json(handler, 400, {"error": "Request body must be a JSON object."})

        api_key = payload.get("api_key")
        if not isinstance(api_key, str):
            return self._send_json(handler, 400, {"error": "Invalid api_key."})
        stripped = api_key.strip()
        if not (8 <= len(stripped) <= 1024):
            return self._send_json(handler, 400, {"error": "Invalid api_key."})

        try:
            write_env_key(stripped)
        except Exception:  # noqa: BLE001 - never leak the key or the path
            return self._send_json(handler, 500, {"error": "Could not save the API key."})
        return self._send_json(handler, 200, {"saved": True})

    def _handle_delete_key(self, handler: BaseHTTPRequestHandler) -> None:
        """Remove the ``ORCAROUTER_API_KEY`` entry from the project ``.env``.

        Idempotent. Never touches Windows/system/process environment
        variables and never echoes the key.
        """
        if not self._auth_ok(handler):
            return self._send_json(handler, 403, {"error": "Forbidden."})
        try:
            delete_env_key()
        except Exception:  # noqa: BLE001 - never leak the key or the path
            return self._send_json(handler, 500, {"error": "Could not delete the API key."})
        return self._send_json(handler, 200, {"deleted": True})

    def _send_key_status(self, handler: BaseHTTPRequestHandler) -> None:
        """Report whether an API key is configured and from which source."""
        configured, source = key_status()
        self._send_json(handler, 200, {"configured": configured, "source": source})

    # -- job submission -----------------------------------------------------

    def _handle_submit(self, handler: BaseHTTPRequestHandler) -> None:
        try:
            length = int(handler.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0:
            return self._send_json(handler, 400, {"error": "Missing or empty request body."})
        if length > MAX_BODY_BYTES:
            return self._send_json(handler, 413, {"error": "Request body too large."})
        body = handler.rfile.read(length)

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._send_json(handler, 400, {"error": "Invalid JSON body."})
        if not isinstance(payload, dict):
            return self._send_json(handler, 400, {"error": "Request body must be a JSON object."})

        job_type = payload.get("job_type")
        if not isinstance(job_type, str) or job_type not in self._manager.handlers:
            return self._send_json(handler, 400, {"error": "Unknown or unsupported job type."})

        params = payload.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return self._send_json(handler, 400, {"error": "params must be a JSON object."})

        upload = payload.get("upload")
        if not isinstance(upload, dict):
            return self._send_json(handler, 400, {"error": "Missing upload."})

        filename = _safe_upload_filename(upload.get("filename"))
        if filename is None:
            return self._send_json(handler, 400, {"error": "Invalid or unsafe upload filename."})

        data_b64 = upload.get("data_b64")
        if not isinstance(data_b64, str) or not data_b64.strip():
            return self._send_json(handler, 400, {"error": "Missing upload data."})
        try:
            data = base64.b64decode(data_b64)
        except Exception:  # noqa: BLE001 - malformed base64
            return self._send_json(handler, 400, {"error": "Invalid base64 upload data."})
        if not data:
            return self._send_json(handler, 400, {"error": "Empty upload."})

        if Path(filename).suffix.lower() not in _ALLOWED_EXTENSIONS.get(job_type, frozenset()):
            return self._send_json(
                handler, 400, {"error": "Unsupported file extension for this job type."}
            )

        if job_type == "ai-clean":
            for required in ("provider", "model"):
                if required not in params:
                    return self._send_json(
                        handler, 400, {"error": f"Missing required param: {required}."}
                    )

        try:
            job_id = self._prepare_job(job_type, params, filename, data)
        except Exception:  # noqa: BLE001 - never leak internals
            return self._send_json(handler, 500, {"error": "Could not prepare job."})

        self._send_json(handler, 202, {"job_id": job_id, "status": "queued"})

    def _prepare_job(self, job_type: str, params: dict, filename: str, data: bytes) -> str:
        """Create the job's private temp dir, write the upload, and submit."""
        base = self._base
        if base is None:
            raise RuntimeError("server has been shut down")
        with self._lock:
            if self._id_aware:
                job_id = uuid.uuid4().hex
                job_dir = base / job_id
                input_dir = job_dir / "input"
                output_dir = job_dir / "output"
                input_dir.mkdir(parents=True, exist_ok=True)
                output_dir.mkdir(parents=True, exist_ok=True)
                (input_dir / filename).write_bytes(data)
                submit_params = self._build_params(job_type, params, job_dir, filename)
                self._manager.submit(job_type, submit_params, job_id=job_id)
                return job_id

            # Caller-supplied manager: submit first, then record the mapping.
            job_dir = Path(tempfile.mkdtemp(dir=str(base), prefix="job-"))
            input_dir = job_dir / "input"
            output_dir = job_dir / "output"
            input_dir.mkdir(parents=True, exist_ok=True)
            output_dir.mkdir(parents=True, exist_ok=True)
            (input_dir / filename).write_bytes(data)
            submit_params = self._build_params(job_type, params, job_dir, filename)
            job_id = self._manager.submit(job_type, submit_params)
            self._job_dirs[job_id] = job_dir
            return job_id

    def _build_params(self, job_type: str, params: dict, job_dir: Path, filename: str) -> dict:
        """Rewrite caller params to point at this job's private directories."""
        input_dir = job_dir / "input"
        output_dir = job_dir / "output"
        if job_type == "convert":
            return {
                "file": str(input_dir / filename),
                "output_dir": str(output_dir),
                "overwrite": _bool_param(params, "overwrite", False),
            }
        if job_type == "clean":
            return {
                "file": str(input_dir / filename),
                "keep_lists": _bool_param(params, "keep_lists", True),
                "dedupe": _bool_param(params, "dedupe", True),
                "backup": _bool_param(params, "backup", True),
            }
        if job_type == "ai-clean":
            return {
                "file": str(input_dir / filename),
                "provider": str(params["provider"]),
                "model": str(params["model"]),
                "output": str(output_dir / f"{Path(filename).stem}.ai.md"),
                "overwrite": _bool_param(params, "overwrite", False),
            }
        if job_type == "pipeline":
            return {
                "pdf_dir": str(input_dir),
                "md_dir": str(output_dir),
                "overwrite": _bool_param(params, "overwrite", False),
                "keep_lists": _bool_param(params, "keep_lists", True),
                "dedupe": _bool_param(params, "dedupe", True),
                "backup": _bool_param(params, "backup", True),
            }
        raise UnknownJobTypeError(job_type, tuple(self._manager.handlers))

    # -- job status / download ----------------------------------------------

    def _send_job_status(self, handler: BaseHTTPRequestHandler, job_id: str) -> None:
        result = self._manager.get(job_id)
        if result is None:
            return self._not_found(handler)
        # output_paths are intentionally omitted; the message is sanitized so
        # no absolute server path reaches the client.
        message = self._sanitize_message(result.message, job_id)
        self._send_json(
            handler,
            200,
            {"job_id": result.job_id, "status": result.status.value, "message": message},
        )

    def _send_job_download(self, handler: BaseHTTPRequestHandler, job_id: str) -> None:
        result = self._manager.get(job_id)
        if result is None:
            return self._not_found(handler)
        if result.status is not JobStatus.succeeded:
            return self._send_json(handler, 409, {"error": "Job is not complete."})

        job_dir = self._job_dir_for(job_id)
        if job_dir is None or not job_dir.is_dir():
            return self._send_json(handler, 404, {"error": "No output available."})

        zip_data = self._build_zip(result, job_dir.resolve())
        if zip_data is None:
            return self._send_json(handler, 404, {"error": "No output files available."})

        self._send_bytes(
            handler,
            200,
            zip_data,
            "application/zip",
            {"Content-Disposition": f'attachment; filename="{job_id}.zip"'},
        )

    def _build_zip(self, result: object, job_root: Path) -> bytes | None:
        """Package this job's own output files into an in-memory ZIP.

        Every path is taken from ``JobResult.output_paths`` and re-validated
        against ``job_root``; anything outside the job's private directory is
        silently skipped, so a client can never request arbitrary paths.
        """
        buffer = io.BytesIO()
        added = False
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for out in getattr(result, "output_paths", ()):
                src = Path(out)
                try:
                    rel = src.resolve().relative_to(job_root)
                except ValueError:
                    # Not inside this job's own directory: never package it.
                    continue
                if src.is_dir():
                    for file_path in sorted(src.rglob("*")):
                        if file_path.is_file():
                            archive.write(str(file_path), file_path.relative_to(job_root).as_posix())
                            added = True
                elif src.is_file():
                    archive.write(str(src), rel.as_posix())
                    added = True
        if not added:
            return None
        return buffer.getvalue()

    # -- static files -------------------------------------------------------

    def _serve_static(self, handler: BaseHTTPRequestHandler, rel: str) -> None:
        if self._static_dir is None:
            return self._not_found(handler)
        base = self._static_dir
        target = (base / urllib.parse.unquote(rel)).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            return self._not_found(handler)
        try:
            data = target.read_bytes()
        except OSError:
            return self._not_found(handler)
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send_bytes(handler, 200, data, content_type)

    # -- misc ---------------------------------------------------------------

    def _send_index(self, handler: BaseHTTPRequestHandler) -> None:
        # Prefer the packaged UI's index.html when a static dir is configured
        # and actually contains one; otherwise fall back to the built-in page.
        if self._static_dir is not None:
            index = self._static_dir / "index.html"
            if index.is_file():
                try:
                    data = index.read_bytes()
                except OSError:
                    pass
                else:
                    # Inject the live, memory-only session token by replacing
                    # the placeholder; the served copy is marked no-store so a
                    # stale token is never cached.
                    data = data.replace(
                        b"__FC_SESSION_TOKEN__", self._session_token.encode("ascii")
                    )
                    self._send_bytes(
                        handler, 200, data, "text/html; charset=utf-8",
                        {"Cache-Control": "no-store"},
                    )
                    return
        body = _INDEX_HTML.encode("utf-8")
        self._send_bytes(handler, 200, body, "text/html; charset=utf-8")

    def _not_found(self, handler: BaseHTTPRequestHandler) -> None:
        self._send_json(handler, 404, {"error": "Not found."})

    def _job_dir_for(self, job_id: str) -> Path | None:
        """Return the job's private temp dir, or None for unknown/unsafe ids.

        Only 32-char lowercase hex uuid values are ever treated as a path
        component; anything else (e.g. ``".."``) yields None, which prevents
        ``cleanup_job`` from ever resolving to a directory outside the server's
        temporary root.
        """
        if not isinstance(job_id, str) or _JOB_ID_RE.fullmatch(job_id) is None:
            return None
        base = self._base
        if self._id_aware:
            return base / job_id if base is not None else None
        return self._job_dirs.get(job_id)

    def _sanitize_message(self, message: str, job_id: str) -> str:
        """Strip absolute temp paths out of a handler-produced message."""
        if not message:
            return message
        prefixes: list[str] = []
        job_dir = self._job_dir_for(job_id)
        if job_dir is not None:
            root = str(job_dir.resolve())
            prefixes.append(root + os.sep)
            prefixes.append(root)
        base = self._base
        if base is not None:
            base_root = str(base.resolve())
            prefixes.append(base_root + os.sep)
            prefixes.append(base_root)
        out = message
        for prefix in sorted({p for p in prefixes if p}, key=len, reverse=True):
            out = out.replace(prefix, "")
        return out

    def _log_sanitized(
        self, method: str, raw_path: object, args: tuple[object, ...]
    ) -> None:
        """Emit a log line containing only method, path, and status.

        Called from request-handling contexts where a malformed request line
        may leave ``command``/``path`` missing, so it must never raise.
        """
        try:
            status: str | None = None
            if len(args) >= 2:
                candidate = args[1]
                if isinstance(candidate, int):
                    status = str(candidate)
                elif isinstance(candidate, str) and candidate.isdigit():
                    status = candidate
            method = method or "?"
            path = "?"
            if isinstance(raw_path, str):
                path = urllib.parse.urlsplit(raw_path).path or "?"
            parts = [method, path]
            if status is not None:
                parts.append(status)
            sys.stderr.write(" ".join(parts) + "\n")
        except Exception:  # noqa: BLE001 - logging must never raise
            try:
                sys.stderr.write("? ?\n")
            except Exception:  # noqa: BLE001 - never raise from logging
                pass

    def _send_json(self, handler: BaseHTTPRequestHandler, status: int, obj: object) -> None:
        body = json.dumps(obj).encode("utf-8")
        self._send_bytes(handler, status, body, "application/json; charset=utf-8")

    def _send_bytes(
        self,
        handler: BaseHTTPRequestHandler,
        status: int,
        body: bytes,
        content_type: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        if headers:
            for key, value in headers.items():
                handler.send_header(key, value)
        handler.end_headers()
        handler.wfile.write(body)


def create_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    static_dir: str | os.PathLike[str] | None = DEFAULT_STATIC_DIR,
    manager: JobManager | None = None,
) -> JobWebServer:
    """Construct and start a :class:`JobWebServer`, returning the running server.

    ``static_dir`` defaults to the packaged ``format_converter/web/static``
    directory so the single-page UI is served out of the box. Pass ``None`` to
    disable static serving, or another directory to serve different assets.
    """
    server = JobWebServer(host=host, port=port, static_dir=static_dir, manager=manager)
    server.serve()
    return server


# ---------------------------------------------------------------------------
# Launcher layer (Step 5): find/start a server, reuse an existing one, block
# until Ctrl+C, etc. All loopback-only; nothing here ever touches a public
# address.
# ---------------------------------------------------------------------------

# Per-probe timeout for the /health check, in seconds.
_HEALTH_TIMEOUT = 0.5
# How long to keep polling /health after binding before giving up.
_READY_TIMEOUT = 30.0


class ServerStartError(RuntimeError):
    """Raised when the launcher can neither start nor reuse a web service."""


def _health_ok(port: int, timeout: float = _HEALTH_TIMEOUT) -> bool:
    """Return True only when ``127.0.0.1:port/health`` serves our contract.

    The exact contract is HTTP 200 with a JSON body of ``{"status": "ok"}``.
    Any error (connection refused, timeout, non-200, malformed body) means
    "not one of our instances" and yields ``False``.
    """
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return False
            body = resp.read().decode("utf-8")
            return json.loads(body) == {"status": "ok"}
    except Exception:  # noqa: BLE001 - refused / timeout / non-JSON body
        return False


def _wait_healthy(port: int, timeout: float = _READY_TIMEOUT) -> None:
    """Block until ``/health`` answers, or raise after ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        if _health_ok(port):
            return
        if time.monotonic() > deadline:
            raise ServerStartError(
                f"服务在 {timeout:.0f} 秒内未就绪（http://127.0.0.1:{port}/health）。"
            )
        time.sleep(0.1)


def _open_browser(port: int) -> None:
    """Open the default browser at the local (loopback-only) service URL."""
    webbrowser.open(f"http://127.0.0.1:{port}/")


def run_server(
    preferred_port: int = DEFAULT_PORT,
    *,
    open_browser: bool = True,
    max_backup_ports: int = 5,
) -> JobWebServer | None:
    """Start the web service, reusing an already-running instance when possible.

    Port strategy
    -------------
    - ``preferred_port == 0`` selects a random free port (the OS picks it).
    - Otherwise try ``preferred_port`` first. If a *live* instance of this
      service already answers ``/health`` there, reuse it (no new server).
    - If the port is occupied by something else, try ``preferred_port + 1``
      up to ``preferred_port + max_backup_ports`` in order.
    - A bind that races with another process follows the same
      health-check-reuse / next-port path.

    Returns
    -------
    The running :class:`JobWebServer` when a **new** instance was started, or
    ``None`` when an existing instance was reused (the caller must then *not*
    call :meth:`JobWebServer.shutdown` -- the existing server keeps running).
    """
    if isinstance(preferred_port, bool) or not isinstance(preferred_port, int):
        raise ValueError("preferred_port must be an int")
    if max_backup_ports < 0:
        raise ValueError("max_backup_ports must be non-negative")

    if preferred_port == 0:
        candidates: list[int] = [0]
    else:
        candidates = list(range(preferred_port, preferred_port + max_backup_ports + 1))

    last_error: OSError | None = None
    for port in candidates:
        if port != 0 and _health_ok(port):
            _announce_reuse(port, open_browser)
            return None

        server = JobWebServer(host=DEFAULT_HOST, port=port, static_dir=DEFAULT_STATIC_DIR)
        try:
            bound = server.serve()
        except OSError as exc:
            last_error = exc
            server.shutdown()
            # Race: an instance may have started between the probe and the
            # bind; treat it exactly like the pre-bind reuse case.
            if port != 0 and _health_ok(port):
                _announce_reuse(port, open_browser)
                return None
            continue

        try:
            if port != 0 and bound != preferred_port:
                _print(f"端口 {preferred_port} 被占用，改用端口 {bound}")
            _wait_healthy(bound)
        except Exception:
            server.shutdown()
            raise
        _print(f"服务已就绪：http://127.0.0.1:{bound}/")
        _print("按 Ctrl+C 停止")
        if open_browser:
            _open_browser(bound)
        return server

    # Every candidate was occupied by something that is not our service.
    detail = f"端口 {preferred_port}"
    if preferred_port != 0 and max_backup_ports > 0:
        detail += (
            f" 及备用端口 {preferred_port + 1}..{preferred_port + max_backup_ports}"
        )
    detail += " 均被占用"
    if last_error is not None:
        detail += f"（最后一次绑定错误：{last_error}）"
    detail += "。请关闭占用这些端口的程序后重试，或改用其它端口。"
    raise ServerStartError(detail)


def _announce_reuse(port: int, open_browser: bool) -> None:
    _print(f"服务已在运行：http://127.0.0.1:{port}/")
    _print("无需重复启动，直接使用现有实例。")
    if open_browser:
        _open_browser(port)


def _print(*parts: object) -> None:
    print(" ".join(str(part) for part in parts))


def _wait_until_interrupted() -> None:
    """Block until Ctrl+C; return once KeyboardInterrupt is delivered."""
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: parse args, run the server, block until Ctrl+C.

    Returns 0 on a clean shutdown (including the reuse case) and a non-zero
    code when the server could not be started.
    """
    parser = argparse.ArgumentParser(
        prog="format_converter.web_server",
        description="启动 FormatConverter 本地图形界面服务（仅绑定 127.0.0.1）。",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"首选端口（默认 {DEFAULT_PORT}；0 = 随机空闲端口）",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="启动后不自动打开默认浏览器",
    )
    parser.add_argument(
        "--max-backup-ports",
        type=int,
        default=5,
        dest="max_backup_ports",
        help="首选端口被占用时依次尝试的备用端口数量（默认 5）",
    )
    args = parser.parse_args(argv)

    try:
        server = run_server(
            preferred_port=args.port,
            open_browser=not args.no_browser,
            max_backup_ports=args.max_backup_ports,
        )
    except Exception as exc:  # noqa: BLE001 - report any launcher error clearly
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    if server is None:
        return 0

    try:
        _wait_until_interrupted()
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在停止服务...")
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
