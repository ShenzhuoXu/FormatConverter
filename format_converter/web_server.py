"""Localhost-only web service for FormatConverter.

This module exposes a small JSON task API (plus optional, protected static
file serving) over plain HTTP bound exclusively to the loopback interface.
There is deliberately **no** browser UI beyond a static information page:

- No API-key input box, no ``localStorage``, and no scripts that touch user
  directories.
- The HTTP layer never reads an API key; AI tasks resolve their key deep
  inside :mod:`format_converter.jobs`, far away from this module.
- Responses never carry cross-origin (CORS) headers.
- Uploads are written only into a fresh per-job temporary directory created
  under the server's own temporary root, which is deleted on shutdown.

Only the Python standard library and this package's modules are imported.
"""

from __future__ import annotations

import base64
import io
import ipaddress
import json
import mimetypes
import os
import re
import shutil
import sys
import tempfile
import threading
import urllib.parse
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .jobs import JobManager, JobStatus, UnknownJobTypeError

__all__ = ["JobWebServer", "create_server"]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

# Only loopback addresses may ever be requested. Anything else is rejected at
# construction/serve time with ValueError and the socket is never opened.

# Valid job ids are 32-char lowercase hex uuid4 values (as produced by
# uuid.uuid4().hex). Anything else must never be treated as a path component.
_JOB_ID_RE = re.compile(r"[0-9a-f]{32}")

# Prefix under which protected static files are served (when configured).
STATIC_PREFIX = "/static/"

# Ceiling for a single request body, to avoid unbounded memory use.
MAX_BODY_BYTES = 256 * 1024 * 1024

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
        else:
            self._not_found(handler)

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
    static_dir: str | os.PathLike[str] | None = None,
    manager: JobManager | None = None,
) -> JobWebServer:
    """Construct and start a :class:`JobWebServer`, returning the running server."""
    server = JobWebServer(host=host, port=port, static_dir=static_dir, manager=manager)
    server.serve()
    return server
