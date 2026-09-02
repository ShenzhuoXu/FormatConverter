"""Offline tests for format_converter.web_server (localhost-only web API).

No browser, no real API calls, and no third-party dependencies: every request
is made over a real localhost socket using the standard library's
``http.client``. A daemon server thread is started per test and shut down in
the fixture teardown so no threads or temporary directories leak.
"""

from __future__ import annotations

import base64
import http.client
import io
import json
import os
import re
import socket
import threading
import time
import zipfile
from pathlib import Path

import pytest

from format_converter import env_store
from format_converter.web_server import (
    DEFAULT_STATIC_DIR,
    MAX_BODY_BYTES,
    JobWebServer,
    create_server,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _request(port: int, method: str, path: str, body: bytes | None = None,
             headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, dict(resp.getheaders()), data
    finally:
        conn.close()


def _post_job(port: int, job_type: str, params: dict, filename: str, content: str
              ) -> tuple[int, dict[str, str], bytes]:
    payload = {
        "job_type": job_type,
        "params": params,
        "upload": {"filename": filename, "data_b64": _b64(content)},
    }
    body = json.dumps(payload).encode("utf-8")
    return _request(port, "POST", "/api/jobs", body=body,
                    headers={"Content-Type": "application/json"})


def _post_jobs(port: int, job_type: str, params: dict,
               files: list[tuple[str, str]]) -> tuple[int, dict[str, str], bytes]:
    """Submit a multi-file job via the ``uploads`` array field."""
    payload = {
        "job_type": job_type,
        "params": params,
        "uploads": [{"filename": name, "data_b64": _b64(content)}
                    for name, content in files],
    }
    body = json.dumps(payload).encode("utf-8")
    return _request(port, "POST", "/api/jobs", body=body,
                    headers={"Content-Type": "application/json"})


def _post_raw(port: int, payload: dict) -> int:
    body = json.dumps(payload).encode("utf-8")
    return _request(port, "POST", "/api/jobs", body=body,
                    headers={"Content-Type": "application/json"})[0]


def _wait_terminal(port: int, job_id: str, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        status, _, data = _request(port, "GET", f"/api/jobs/{job_id}")
        assert status == 200
        payload = json.loads(data.decode("utf-8"))
        if payload["status"] in ("succeeded", "failed"):
            return payload
        if time.monotonic() > deadline:
            raise AssertionError(f"job {job_id} did not finish in {timeout}s: {payload}")
        time.sleep(0.05)


def _fake_convert_file(pdf_path, output_dir, overwrite: bool = False) -> Path:
    """Stand-in for convert_pdf_file that writes a fake Markdown output."""
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out = output_dir / f"{pdf_path.stem}.md"
    out.write_text("# converted", encoding="utf-8")
    return out


def _fake_run_pipeline(pdf_dir, md_dir, overwrite: bool = False, keep_lists: bool = True,
                       dedupe: bool = True, backup: bool = True) -> tuple[list[Path], list[Path]]:
    """Stand-in for run_pipeline used by the web 'pipeline' job handler."""
    md_dir = Path(md_dir)
    md_dir.mkdir(parents=True, exist_ok=True)
    converted = md_dir / "doc.md"
    converted.write_text("# pipelined", encoding="utf-8")
    cleaned = md_dir / "doc.cleaned.md"
    cleaned.write_text("# cleaned", encoding="utf-8")
    return [converted], [cleaned]


def _fake_ai_clean(file, provider: str, model: str, *, output=None,
                   overwrite: bool = False, client=None, progress=None,
                   ai_job_store=None, **kwargs) -> Path:
    """Stand-in for cli.ai_clean used by the web 'ai-clean' job handler."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("# ai cleaned", encoding="utf-8")
    return output


def _fake_convert_directory(input_dir, output_dir, overwrite: bool = False) -> list[Path]:
    """Stand-in for convert_pdf_directory that fakes one .md per input .pdf."""
    input_dir = Path(input_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    converted: list[Path] = []
    for pdf in sorted(input_dir.glob("*.pdf")):
        out = output_dir / f"{pdf.stem}.md"
        out.write_text("# converted", encoding="utf-8")
        converted.append(out)
    return converted


def _fake_run_pipeline_batch(pdf_dir, md_dir, overwrite: bool = False,
                             keep_lists: bool = True, dedupe: bool = True,
                             backup: bool = True) -> tuple[list[Path], list[Path]]:
    """Stand-in for run_pipeline that fakes output for every input .pdf."""
    pdf_dir = Path(pdf_dir)
    md_dir = Path(md_dir)
    md_dir.mkdir(parents=True, exist_ok=True)
    converted: list[Path] = []
    cleaned: list[Path] = []
    for pdf in sorted(pdf_dir.glob("*.pdf")):
        out = md_dir / f"{pdf.stem}.md"
        out.write_text("# pipelined", encoding="utf-8")
        converted.append(out)
        cln = md_dir / f"{pdf.stem}.cleaned.md"
        cln.write_text("# cleaned", encoding="utf-8")
        cleaned.append(cln)
    return converted, cleaned


@pytest.fixture
def make_server():
    """Factory fixture: start a server on a random port, shut it down on exit."""
    servers = []

    def _make(*, static_dir=None, host="127.0.0.1", port=0, manager=None):
        if static_dir is not None:
            static_dir = Path(static_dir)
            static_dir.mkdir(parents=True, exist_ok=True)
        server = JobWebServer(host=host, port=port, static_dir=static_dir, manager=manager)
        bound = server.serve()
        servers.append(server)
        return server, bound

    yield _make

    for server in servers:
        try:
            server.shutdown()
        except Exception:  # noqa: BLE001 - teardown must never raise
            pass


# ---------------------------------------------------------------------------
# health / index / routing
# ---------------------------------------------------------------------------


class TestHealthAndIndex:
    def test_health(self, make_server) -> None:
        server, port = make_server()
        status, headers, data = _request(port, "GET", "/health")
        assert status == 200
        assert json.loads(data.decode("utf-8")) == {"status": "ok"}
        assert "Access-Control-Allow-Origin" not in headers

    def test_index_page(self, make_server) -> None:
        server, port = make_server()
        status, headers, data = _request(port, "GET", "/")
        assert status == 200
        assert headers["Content-Type"].startswith("text/html")
        body = data.decode("utf-8")
        assert "FormatConverter" in body
        assert "/api/jobs" in body
        assert "localStorage" not in body
        assert "apiKey" not in body.lower()

    def test_unknown_route_404(self, make_server) -> None:
        server, port = make_server()
        assert _request(port, "GET", "/nope")[0] == 404
        # ``GET /api/jobs`` is now a valid recent-jobs endpoint (Step 4.1).
        assert _request(port, "GET", "/api/jobs/abc/extra/parts")[0] == 404
        assert _request(port, "POST", "/health")[0] == 404
        assert _request(port, "POST", "/api/jobs/unknown-id")[0] == 404

    def test_no_cors_headers(self, make_server) -> None:
        server, port = make_server()
        for method, path, body in [("GET", "/health", None), ("GET", "/", None)]:
            _, headers, _ = _request(port, method, path, body=body)
            assert "Access-Control-Allow-Origin" not in headers


# ---------------------------------------------------------------------------
# end-to-end success (clean uses only stdlib and works offline)
# ---------------------------------------------------------------------------


class TestSubmitAndDownload:
    def test_submit_clean_success_e2e(self, make_server) -> None:
        server, port = make_server()
        content = "# Doc\n\nAlpha.\n\nAlpha.\n\nBeta.\n"
        status, _, data = _post_job(
            port, "clean", {"dedupe": True, "backup": True}, "doc.md", content
        )
        assert status == 202
        payload = json.loads(data.decode("utf-8"))
        job_id = payload["job_id"]
        assert payload["status"] == "queued"
        assert job_id

        final = _wait_terminal(port, job_id)
        assert final["status"] == "succeeded"
        assert final["job_id"] == job_id
        assert "output_paths" not in final
        # No absolute server path may leak in the status response.
        assert str(server.base_temp_dir) not in data.decode("utf-8")
        raw_status = json.dumps(final).encode("utf-8")
        assert str(server.base_temp_dir) not in raw_status.decode("utf-8")

        # Download: a single output file is returned directly (not a ZIP).
        status, headers, data = _request(port, "GET", f"/api/jobs/{job_id}/download")
        assert status == 200
        assert headers["Content-Type"] != "application/zip"
        assert 'filename="doc.md"' in headers["Content-Disposition"]
        assert "Access-Control-Allow-Origin" not in headers
        # A direct file response must not be a ZIP archive.
        assert not data.startswith(b"PK\x03\x04")

        extracted = data.decode("utf-8")
        assert extracted == "# Doc\n\nAlpha.\n\nBeta.\n"
        assert extracted.count("Alpha.") == 1  # dedupe ran
        job_dir = server.base_temp_dir / job_id
        assert (job_dir / "input" / "doc.md").read_text(encoding="utf-8") == extracted
        assert (job_dir / "input" / "doc.bak.md").is_file()  # backup ran

    def test_status_and_download_unknown_job_404(self, make_server) -> None:
        server, port = make_server()
        assert _request(port, "GET", "/api/jobs/does-not-exist")[0] == 404
        assert _request(port, "GET", "/api/jobs/does-not-exist/download")[0] == 404

    def test_download_while_running_409(self, make_server) -> None:
        server, port = make_server()
        manager = server._manager
        entered = threading.Event()
        release = threading.Event()

        def _blocking(_params: dict) -> tuple[tuple[Path, ...], str]:
            entered.set()
            assert release.wait(5)
            return (Path("x.md"),), "blocked done"

        manager.handlers["slow"] = _blocking
        job_id = manager.submit("slow", {})
        assert entered.wait(5), "handler never started"

        status, _, data = _request(port, "GET", f"/api/jobs/{job_id}/download")
        assert status == 409
        assert json.loads(data.decode("utf-8")) == {"error": "Job is not complete."}

        release.set()
        # allow the job to finish so no thread leaks
        final = manager.wait(job_id, timeout=5)
        assert final is not None and final.status.value == "succeeded"

    def test_create_server_function_roundtrip(self) -> None:
        server = create_server(port=0)
        try:
            assert server.port is not None
            status, _, data = _request(server.port, "GET", "/health")
            assert status == 200
            assert json.loads(data.decode("utf-8")) == {"status": "ok"}
        finally:
            server.shutdown()


# ---------------------------------------------------------------------------
# failures (offline, no real API)
# ---------------------------------------------------------------------------


class TestFailures:
    def test_ai_clean_failed_no_key_leak(self, make_server, monkeypatch) -> None:
        monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
        server, port = make_server()
        content = "# Alpha\n\nBeta.\n"
        status, _, data = _post_job(
            port, "ai-clean", {"provider": "orcarouter", "model": "m1"}, "doc.md", content
        )
        assert status == 202
        job_id = json.loads(data.decode("utf-8"))["job_id"]

        final = _wait_terminal(port, job_id)
        assert final["status"] == "failed"
        assert "ORCAROUTER_API_KEY" in final["message"]  # env var name is fine
        assert "sk-" not in final["message"]  # key value must never appear

        # download of a failed job is refused
        assert _request(port, "GET", f"/api/jobs/{job_id}/download")[0] == 409

    def test_ai_clean_rejects_secret_shaped_model(self, make_server) -> None:
        server, port = make_server()
        content = "# Alpha\n\nBeta.\n"
        status, _, data = _post_job(
            port, "ai-clean", {"provider": "orcarouter", "model": "sk-short"}, "doc.md", content
        )
        assert status == 400
        body = data.decode("utf-8")
        assert "sk-" not in body

    def test_ai_clean_rejects_whitespace_padded_secret_model(self, make_server) -> None:
        server, port = make_server()
        content = "# Alpha\n\nBeta.\n"
        status, _, data = _post_job(
            port, "ai-clean", {"provider": "orcarouter", "model": "  sk-short  "}, "doc.md", content
        )
        assert status == 400
        body = data.decode("utf-8")
        assert "sk-" not in body

    def test_ai_clean_model_not_found_does_not_leak_model_name(self, make_server, monkeypatch) -> None:
        from format_converter.llm_client import ModelNotFoundError

        def _raising_fake(file, provider: str, model: str, *, output=None,
                          overwrite: bool = False, client=None, progress=None,
                          ai_job_store=None, **kwargs) -> Path:
            raise ModelNotFoundError(
                "Provider 'orcarouter' could not find the requested model (HTTP 404)."
            )

        monkeypatch.setattr("format_converter.jobs.ai_clean", _raising_fake)
        server, port = make_server()
        content = "# Alpha\n\nBeta.\n"
        status, _, data = _post_job(
            port, "ai-clean", {"provider": "orcarouter", "model": "no-such-model"}, "doc.md", content
        )
        assert status == 202
        job_id = json.loads(data.decode("utf-8"))["job_id"]

        final = _wait_terminal(port, job_id)
        assert final["status"] == "failed"
        assert "no-such-model" not in final["message"]

    def test_convert_empty_file_rejected_400(self, make_server) -> None:
        server, port = make_server()
        status, _, _ = _post_job(port, "convert", {}, "doc.pdf", "")
        assert status == 400


# ---------------------------------------------------------------------------
# additional end-to-end paths (convert / pipeline / ai-clean / edge cases)
# ---------------------------------------------------------------------------


class TestAdditionalE2E:
    def test_convert_success_e2e(self, make_server, monkeypatch) -> None:
        # The real pymupdf4llm is not installed; the worker is faked while the
        # whole web path (upload -> job -> status -> download) is exercised.
        monkeypatch.setattr("format_converter.jobs.convert_pdf_file", _fake_convert_file)
        server, port = make_server()

        status, _, data = _post_job(port, "convert", {}, "doc.pdf", "%%PDF fake")
        assert status == 202
        job_id = json.loads(data.decode("utf-8"))["job_id"]

        final = _wait_terminal(port, job_id)
        assert final["status"] == "succeeded"

        # Single output file -> direct download, not a ZIP.
        status, headers, data = _request(port, "GET", f"/api/jobs/{job_id}/download")
        assert status == 200
        assert headers["Content-Type"] != "application/zip"
        assert 'filename="doc.md"' in headers["Content-Disposition"]
        assert data.decode("utf-8") == "# converted"
        assert not data.startswith(b"PK\x03\x04")

    def test_pipeline_success_e2e(self, make_server, monkeypatch) -> None:
        monkeypatch.setattr("format_converter.jobs.run_pipeline", _fake_run_pipeline)
        server, port = make_server()

        status, _, data = _post_job(port, "pipeline", {}, "doc.pdf", "%%PDF fake")
        assert status == 202
        job_id = json.loads(data.decode("utf-8"))["job_id"]

        final = _wait_terminal(port, job_id)
        assert final["status"] == "succeeded"

        # Two outputs -> ZIP, entries at the root (no input/output prefix).
        status, headers, data = _request(port, "GET", f"/api/jobs/{job_id}/download")
        assert status == 200
        assert headers["Content-Type"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            assert "doc.md" in names
            assert "doc.cleaned.md" in names
            assert not any(n.startswith(("input/", "output/")) for n in names)

    def test_ai_clean_success_e2e(self, make_server, monkeypatch) -> None:
        # The web layer resolves the API key inside jobs; a faked ai_clean keeps
        # the test fully offline while still exercising the full web path.
        monkeypatch.setattr("format_converter.jobs.ai_clean", _fake_ai_clean)
        server, port = make_server()

        status, _, data = _post_job(
            port, "ai-clean", {"provider": "orcarouter", "model": "m1"}, "doc.md", "# Alpha"
        )
        assert status == 202
        job_id = json.loads(data.decode("utf-8"))["job_id"]

        final = _wait_terminal(port, job_id)
        assert final["status"] == "succeeded"

        # Single output file -> direct download, not a ZIP.
        status, headers, data = _request(port, "GET", f"/api/jobs/{job_id}/download")
        assert status == 200
        assert headers["Content-Type"] != "application/zip"
        assert 'filename="doc.ai.md"' in headers["Content-Disposition"]
        assert data.decode("utf-8") == "# ai cleaned"
        assert not data.startswith(b"PK\x03\x04")

    def test_download_succeeded_but_no_output_files_404(self, make_server) -> None:
        server, port = make_server()
        manager = server._manager
        # A handler that succeeds with zero output paths.
        manager.handlers["clean"] = lambda _params: ((), "no outputs")

        status, _, data = _post_job(port, "clean", {}, "doc.md", "# x")
        assert status == 202
        job_id = json.loads(data.decode("utf-8"))["job_id"]

        final = _wait_terminal(port, job_id)
        assert final["status"] == "succeeded"
        # Succeeded but nothing to package -> 404 "No output files available."
        status, _, data = _request(port, "GET", f"/api/jobs/{job_id}/download")
        assert status == 404
        assert "No output files available" in data.decode("utf-8")

    def test_string_bool_lookalikes_do_not_flip_behavior(self, make_server) -> None:
        # "false"/"true" strings are not genuine booleans: the web layer must
        # keep the defaults (dedupe on, backup on) rather than silently flip.
        # Black-box note: this asserts observable behavior (dedupe + backup ran
        # despite a string "false"); it does not assert the exact internal
        # coercion path. The JobManager docstring is the authoritative statement
        # that booleans must be real Python bools.
        server, port = make_server()
        content = "# Doc\n\nAlpha.\n\nAlpha.\n\nBeta.\n"
        status, _, data = _post_job(
            port, "clean", {"dedupe": "false", "backup": "false"}, "doc.md", content
        )
        assert status == 202
        job_id = json.loads(data.decode("utf-8"))["job_id"]

        final = _wait_terminal(port, job_id)
        assert final["status"] == "succeeded"

        job_dir = server.base_temp_dir / job_id
        assert (job_dir / "input" / "doc.bak.md").is_file()  # backup ran (default True)

        status, headers, data = _request(port, "GET", f"/api/jobs/{job_id}/download")
        assert status == 200
        assert headers["Content-Type"] != "application/zip"
        extracted = data.decode("utf-8")
        assert extracted.count("Alpha.") == 1  # dedupe still ran (default True)


class TestJobProgressInAPI:
    def test_status_response_includes_current_and_total(self, make_server) -> None:
        server, port = make_server()
        content = "# Doc\n\nAlpha.\n\nAlpha.\n\nBeta.\n"
        status, _, data = _post_job(
            port, "clean", {"dedupe": True, "backup": True}, "doc.md", content
        )
        assert status == 202
        job_id = json.loads(data.decode("utf-8"))["job_id"]

        final = _wait_terminal(port, job_id)
        assert final["status"] == "succeeded"
        assert final["current"] == 0
        assert final["total"] == 0
        assert "output_paths" not in final
        assert str(server.base_temp_dir) not in json.dumps(final)

    def test_list_response_includes_current_and_total(self, make_server) -> None:
        server, port = make_server()
        content = "# Doc\n\nAlpha.\n\nAlpha.\n\nBeta.\n"
        status, _, data = _post_job(
            port, "clean", {"dedupe": True, "backup": True}, "doc.md", content
        )
        assert status == 202
        job_id = json.loads(data.decode("utf-8"))["job_id"]
        _wait_terminal(port, job_id)

        status, _, data = _request(port, "GET", "/api/jobs")
        assert status == 200
        payload = json.loads(data.decode("utf-8"))
        entry = next(j for j in payload["jobs"] if j["job_id"] == job_id)
        assert entry["current"] == 0
        assert entry["total"] == 0
        assert "output_paths" not in entry
        assert str(server.base_temp_dir) not in json.dumps(entry)

    def test_ai_clean_progress_reaches_status_api(self, make_server, monkeypatch) -> None:
        def _progressing_ai_clean(file, provider, model, *, output=None,
                                  overwrite=False, client=None, progress=None,
                                  ai_job_store=None, **kwargs):
            if progress is not None:
                progress(1, 2)
                progress(2, 2)
            output = Path(output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("# ai cleaned", encoding="utf-8")
            return output

        monkeypatch.setattr("format_converter.jobs.ai_clean", _progressing_ai_clean)
        server, port = make_server()
        status, _, data = _post_job(
            port, "ai-clean", {"provider": "orcarouter", "model": "m1"}, "doc.md", "# Alpha"
        )
        assert status == 202
        job_id = json.loads(data.decode("utf-8"))["job_id"]

        final = _wait_terminal(port, job_id)
        assert final["status"] == "succeeded"
        assert final["current"] == 2
        assert final["total"] == 2
        assert "output_paths" not in final
        assert str(server.base_temp_dir) not in json.dumps(final)


class TestMultiUpload:
    def test_batch_clean_success_e2e(self, make_server) -> None:
        server, port = make_server()
        files = [
            ("a.md", "# A\n\nAlpha.\n\nAlpha.\n\nBeta.\n"),
            ("b.md", "# B\n\nGamma.\n\nGamma.\n\nDelta.\n"),
        ]
        status, _, data = _post_jobs(port, "clean", {"dedupe": True, "backup": True}, files)
        assert status == 202
        job_id = json.loads(data.decode("utf-8"))["job_id"]

        final = _wait_terminal(port, job_id)
        assert final["status"] == "succeeded"

        status, headers, data = _request(port, "GET", f"/api/jobs/{job_id}/download")
        assert status == 200
        assert headers["Content-Type"] == "application/zip"
        assert "Access-Control-Allow-Origin" not in headers
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = sorted(archive.namelist())
            assert names == ["a.md", "b.md"]  # no input/ prefix
            a = archive.read("a.md").decode("utf-8")
            b = archive.read("b.md").decode("utf-8")
        assert a.count("Alpha.") == 1  # dedupe ran per file
        assert b.count("Gamma.") == 1
        # No absolute server path may leak in any response body.
        assert str(server.base_temp_dir).encode() not in data
        assert str(server.base_temp_dir) not in json.dumps(final)

    def test_batch_convert_success_e2e(self, make_server, monkeypatch) -> None:
        monkeypatch.setattr(
            "format_converter.jobs.convert_pdf_directory", _fake_convert_directory
        )
        server, port = make_server()
        files = [("a.pdf", "%%PDF-a"), ("b.pdf", "%%PDF-b")]
        status, _, data = _post_jobs(port, "convert", {}, files)
        assert status == 202
        job_id = json.loads(data.decode("utf-8"))["job_id"]

        final = _wait_terminal(port, job_id)
        assert final["status"] == "succeeded"

        # Directory mode: both PDFs landed in the job's input dir.
        job_dir = server.base_temp_dir / job_id
        assert sorted(p.name for p in (job_dir / "input").glob("*.pdf")) == ["a.pdf", "b.pdf"]

        status, headers, data = _request(port, "GET", f"/api/jobs/{job_id}/download")
        assert status == 200
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            assert sorted(archive.namelist()) == ["a.md", "b.md"]
        assert str(server.base_temp_dir).encode() not in data

    def test_batch_pipeline_success_e2e(self, make_server, monkeypatch) -> None:
        captured: dict[str, Path] = {}

        def _spy_pipeline(pdf_dir, md_dir, **kwargs):
            captured["pdf_dir"] = Path(pdf_dir)
            captured["md_dir"] = Path(md_dir)
            return _fake_run_pipeline_batch(pdf_dir, md_dir)

        monkeypatch.setattr("format_converter.jobs.run_pipeline", _spy_pipeline)
        server, port = make_server()
        files = [("a.pdf", "%%PDF-a"), ("b.pdf", "%%PDF-b")]
        status, _, data = _post_jobs(port, "pipeline", {}, files)
        assert status == 202
        job_id = json.loads(data.decode("utf-8"))["job_id"]

        final = _wait_terminal(port, job_id)
        assert final["status"] == "succeeded"
        # run_pipeline received an input dir holding both uploaded PDFs.
        assert sorted(p.name for p in captured["pdf_dir"].glob("*.pdf")) == ["a.pdf", "b.pdf"]

        status, headers, data = _request(port, "GET", f"/api/jobs/{job_id}/download")
        assert status == 200
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            assert "a.md" in names
            assert "b.md" in names
            assert "a.cleaned.md" in names
            assert "b.cleaned.md" in names
            assert not any(n.startswith(("input/", "output/")) for n in names)
        assert str(server.base_temp_dir).encode() not in data

    def test_batch_ai_clean_success_e2e(self, make_server, monkeypatch) -> None:
        calls: list[str] = []

        def _recording_ai_clean(file, provider: str, model: str, *, output=None,
                               overwrite: bool = False, client=None, progress=None,
                               ai_job_store=None, **kwargs) -> Path:
            output = Path(output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("# ai cleaned", encoding="utf-8")
            calls.append(Path(file).name)
            return output

        monkeypatch.setattr("format_converter.jobs.ai_clean", _recording_ai_clean)
        server, port = make_server()
        files = [("a.md", "# A"), ("b.md", "# B")]
        status, _, data = _post_jobs(
            port, "ai-clean", {"provider": "orcarouter", "model": "m1"}, files
        )
        assert status == 202
        job_id = json.loads(data.decode("utf-8"))["job_id"]

        final = _wait_terminal(port, job_id)
        assert final["status"] == "succeeded"
        assert sorted(calls) == ["a.md", "b.md"]  # called once per uploaded file

        status, headers, data = _request(port, "GET", f"/api/jobs/{job_id}/download")
        assert status == 200
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            assert sorted(archive.namelist()) == ["a.ai.md", "b.ai.md"]
        # No API key material or absolute path leaks anywhere.
        assert b"sk-" not in data
        assert "sk-" not in json.dumps(final)
        assert str(server.base_temp_dir).encode() not in data

    def test_single_upload_field_still_works(self, make_server, monkeypatch) -> None:
        # Legacy single 'upload' field must remain a valid way to submit a job.
        monkeypatch.setattr("format_converter.jobs.convert_pdf_file", _fake_convert_file)
        server, port = make_server()
        status, _, data = _post_job(port, "convert", {}, "doc.pdf", "%%PDF fake")
        assert status == 202
        job_id = json.loads(data.decode("utf-8"))["job_id"]
        assert _wait_terminal(port, job_id)["status"] == "succeeded"


class TestOversizedBody:
    def test_oversized_request_body_413(self, make_server) -> None:
        server, port = make_server()
        # Send only the headers with an oversized Content-Length. The handler
        # rejects with 413 before reading the (un-sent) body.
        header = (
            "POST /api/jobs HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {MAX_BODY_BYTES + 1}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("utf-8")
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        try:
            sock.sendall(header)
            data = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
        finally:
            sock.close()
        assert b"413" in data
        assert b"Request body too large" in data


# ---------------------------------------------------------------------------
# input validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_invalid_json_400(self, make_server) -> None:
        server, port = make_server()
        status, _, _ = _request(port, "POST", "/api/jobs", body=b"not json",
                                headers={"Content-Type": "application/json"})
        assert status == 400

    def test_missing_job_type_400(self, make_server) -> None:
        server, port = make_server()
        body = json.dumps({"params": {}, "upload": {"filename": "a.md", "data_b64": _b64("# x")}})
        assert _request(port, "POST", "/api/jobs", body=body.encode())[0] == 400

    def test_unknown_job_type_400(self, make_server) -> None:
        server, port = make_server()
        assert _post_job(port, "bogus", {}, "a.md", "# x")[0] == 400

    def test_missing_upload_400(self, make_server) -> None:
        server, port = make_server()
        body = json.dumps({"job_type": "clean", "params": {}})
        assert _request(port, "POST", "/api/jobs", body=body.encode())[0] == 400

    def test_empty_upload_400(self, make_server) -> None:
        server, port = make_server()
        payload = {"job_type": "clean", "params": {},
                   "upload": {"filename": "a.md", "data_b64": ""}}
        assert _request(port, "POST", "/api/jobs", body=json.dumps(payload).encode())[0] == 400

    def test_missing_filename_400(self, make_server) -> None:
        server, port = make_server()
        payload = {"job_type": "clean", "params": {}, "upload": {"data_b64": _b64("# x")}}
        assert _request(port, "POST", "/api/jobs", body=json.dumps(payload).encode())[0] == 400

    def test_unsafe_filename_400(self, make_server) -> None:
        server, port = make_server()
        for name in ["../evil.md", "a/b.md", "a\\b.md", "..", ".", ""]:
            payload = {"job_type": "clean", "params": {},
                       "upload": {"filename": name, "data_b64": _b64("# x")}}
            status, _, _ = _request(port, "POST", "/api/jobs", body=json.dumps(payload).encode())
            assert status == 400, f"filename {name!r} should be rejected"

    def test_unsupported_extension_400(self, make_server) -> None:
        server, port = make_server()
        # convert / pipeline accept only .pdf
        assert _post_job(port, "convert", {}, "doc.md", "%%PDF")[0] == 400
        assert _post_job(port, "convert", {}, "doc.txt", "%%PDF")[0] == 400
        assert _post_job(port, "pipeline", {}, "doc.md", "%%PDF")[0] == 400
        # clean / ai-clean accept only .md
        assert _post_job(port, "clean", {}, "doc.pdf", "# x")[0] == 400
        assert _post_job(
            port, "ai-clean", {"provider": "orcarouter", "model": "m"}, "doc.pdf", "# x"
        )[0] == 400

    def test_missing_ai_clean_params_400(self, make_server) -> None:
        server, port = make_server()
        assert _post_job(port, "ai-clean", {}, "doc.md", "# x")[0] == 400

    def test_params_non_object_400(self, make_server) -> None:
        server, port = make_server()
        payload = {"job_type": "clean", "params": ["not", "a", "dict"],
                   "upload": {"filename": "a.md", "data_b64": _b64("# x")}}
        assert _request(port, "POST", "/api/jobs", body=json.dumps(payload).encode())[0] == 400


class TestMultiUploadValidation:
    def test_empty_uploads_array_400(self, make_server) -> None:
        server, port = make_server()
        assert _post_raw(port, {"job_type": "clean", "params": {}, "uploads": []}) == 400

    def test_uploads_not_array_400(self, make_server) -> None:
        server, port = make_server()
        assert _post_raw(
            port, {"job_type": "clean", "params": {}, "uploads": "not-an-array"}
        ) == 400

    def test_both_upload_and_uploads_400(self, make_server) -> None:
        server, port = make_server()
        payload = {
            "job_type": "clean", "params": {},
            "upload": {"filename": "a.md", "data_b64": _b64("# x")},
            "uploads": [{"filename": "b.md", "data_b64": _b64("# y")}],
        }
        assert _post_raw(port, payload) == 400

    def test_duplicate_filenames_case_insensitive_400(self, make_server) -> None:
        server, port = make_server()
        files = [("A.md", "# x"), ("a.md", "# y")]
        assert _post_jobs(port, "clean", {}, files)[0] == 400

    def test_dangerous_filename_in_uploads_400(self, make_server) -> None:
        server, port = make_server()
        for bad in ["../evil.md", "a/b.md", "a\\b.md", "..", "."]:
            files = [("a.md", "# x"), (bad, "# y")]
            status, _, _ = _post_jobs(port, "clean", {}, files)
            assert status == 400, f"filename {bad!r} should be rejected"

    def test_mixed_wrong_extensions_400(self, make_server) -> None:
        server, port = make_server()
        # convert only accepts .pdf; a stray .md must reject the whole batch.
        files = [("a.pdf", "%%PDF"), ("b.md", "# x")]
        assert _post_jobs(port, "convert", {}, files)[0] == 400

    def test_invalid_base64_in_uploads_400(self, make_server) -> None:
        server, port = make_server()
        payload = {
            "job_type": "clean", "params": {},
            "uploads": [
                {"filename": "a.md", "data_b64": _b64("# ok")},
                {"filename": "b.md", "data_b64": "%%%not-base64%%%"},
            ],
        }
        assert _post_raw(port, payload) == 400

    def test_too_many_files_400(self, make_server) -> None:
        from format_converter.web_server import MAX_UPLOAD_FILES
        server, port = make_server()
        files = [(f"{i}.md", "# x") for i in range(MAX_UPLOAD_FILES + 1)]
        assert _post_jobs(port, "clean", {}, files)[0] == 400

    def test_invalid_file_creates_no_job_dir(self, make_server) -> None:
        server, port = make_server()
        base = server.base_temp_dir
        assert base is not None
        before = {p.name for p in base.iterdir()}
        # One valid + one unsafe filename: the whole request must fail cleanly
        # without leaving a partial job directory on disk.
        files = [("a.md", "# ok"), ("../evil.md", "# bad")]
        status, _, _ = _post_jobs(port, "clean", {}, files)
        assert status == 400
        after = {p.name for p in base.iterdir()}
        assert after == before

    def test_strict_base64_rejects_garbage_suffix_400(self, make_server) -> None:
        # "YQ==" decodes to b"a"; appending non-base64 characters must NOT be
        # silently discarded (the lax default would accept it as b"a").
        server, port = make_server()
        payload = {
            "job_type": "clean", "params": {},
            "uploads": [{"filename": "a.md", "data_b64": "YQ==!!!!"}],
        }
        assert _post_raw(port, payload) == 400

    def test_strict_base64_rejects_non_base64_chars_400(self, make_server) -> None:
        server, port = make_server()
        payload = {
            "job_type": "clean", "params": {},
            "uploads": [{"filename": "a.md", "data_b64": "%%%not-base64%%%"}],
        }
        assert _post_raw(port, payload) == 400

    def test_invalid_base64_creates_no_partial_job(self, make_server) -> None:
        server, port = make_server()
        base = server.base_temp_dir
        assert base is not None
        before = {p.name for p in base.iterdir()}
        # One valid file + one file whose base64 has a garbage suffix: the
        # whole request must fail and leave no job directory on disk.
        payload = {
            "job_type": "clean", "params": {},
            "uploads": [
                {"filename": "a.md", "data_b64": _b64("# ok")},
                {"filename": "b.md", "data_b64": "YQ==!!!!"},
            ],
        }
        assert _post_raw(port, payload) == 400
        after = {p.name for p in base.iterdir()}
        assert after == before


# ---------------------------------------------------------------------------
# download rules: single file direct / multi-file ZIP with root entries
# ---------------------------------------------------------------------------


class TestDownloadRules:
    def test_output_path_outside_job_root_skipped_404(self, make_server) -> None:
        server, port = make_server()
        manager = server._manager
        outside = server.base_temp_dir.parent / "outside-secret.txt"
        outside.write_text("secret outside", encoding="utf-8")
        # Handler claims a path outside this job's private directory.
        manager.handlers["clean"] = lambda _params: ((outside,), "done")

        status, _, data = _post_job(port, "clean", {}, "doc.md", "# x")
        assert status == 202
        job_id = json.loads(data.decode("utf-8"))["job_id"]
        assert _wait_terminal(port, job_id)["status"] == "succeeded"

        # The outside path must be skipped and (with nothing else) yield 404.
        status, _, data = _request(port, "GET", f"/api/jobs/{job_id}/download")
        assert status == 404
        assert "No output files available" in data.decode("utf-8")
        assert outside.name not in data.decode("utf-8")
        outside.unlink(missing_ok=True)

    def test_output_path_outside_job_root_skipped_keeps_inside(self, make_server) -> None:
        server, port = make_server()
        outside = server.base_temp_dir.parent / "outside-keep.txt"
        outside.write_text("secret outside", encoding="utf-8")

        def _mixed(_params: dict) -> tuple[tuple[Path, ...], str]:
            job_dir = server.base_temp_dir
            inside = max((d for d in job_dir.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime)
            legit = inside / "ok.md"
            legit.write_text("# ok", encoding="utf-8")
            return (outside, legit), "done"

        manager = server._manager
        manager.handlers["clean"] = _mixed
        status, _, data = _post_job(port, "clean", {}, "doc.md", "# x")
        assert status == 202
        job_id = json.loads(data.decode("utf-8"))["job_id"]
        assert _wait_terminal(port, job_id)["status"] == "succeeded"

        # Outside path is skipped; the single remaining file downloads directly.
        status, headers, data = _request(port, "GET", f"/api/jobs/{job_id}/download")
        assert status == 200
        assert headers["Content-Type"] != "application/zip"
        assert 'filename="ok.md"' in headers["Content-Disposition"]
        assert data.decode("utf-8") == "# ok"
        assert outside.name not in data.decode("utf-8", errors="replace")
        outside.unlink(missing_ok=True)

    def test_zip_same_name_conflict_renamed_stably(self, make_server) -> None:
        # Clean up any leftover AIJobStore entries so hydration does not create
        # extra directories under the server's temp root.
        import shutil
        from pathlib import Path
        leftover = Path.cwd() / ".formatconverter-jobs"
        if leftover.is_dir():
            shutil.rmtree(leftover)

        server, port = make_server()
        manager = server._manager

        def _dupe_names(_params: dict) -> tuple[tuple[Path, ...], str]:
            job_dir = server.base_temp_dir
            job_id = max((d.name for d in job_dir.iterdir() if d.is_dir()), key=lambda n: n)
            a = job_dir / job_id / "a" / "doc.md"
            b = job_dir / job_id / "b" / "doc.md"
            a.parent.mkdir(parents=True, exist_ok=True)
            b.parent.mkdir(parents=True, exist_ok=True)
            a.write_text("# first", encoding="utf-8")
            b.write_text("# second", encoding="utf-8")
            return (a, b), "done"

        manager.handlers["clean"] = _dupe_names
        status, _, data = _post_job(port, "clean", {}, "doc.md", "# x")
        assert status == 202
        job_id = json.loads(data.decode("utf-8"))["job_id"]
        assert _wait_terminal(port, job_id)["status"] == "succeeded"

        status, headers, data = _request(port, "GET", f"/api/jobs/{job_id}/download")
        assert status == 200
        assert headers["Content-Type"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            assert sorted(names) == ["doc-2.md", "doc.md"]
            assert archive.read("doc.md").decode("utf-8") == "# first"
            assert archive.read("doc-2.md").decode("utf-8") == "# second"

    def test_zip_double_suffix_conflict_renamed_stably(self, make_server) -> None:
        import shutil
        leftover = Path.cwd() / ".formatconverter-jobs"
        if leftover.is_dir():
            shutil.rmtree(leftover)

        server, port = make_server()
        manager = server._manager

        def _dupe_ai(_params: dict) -> tuple[tuple[Path, ...], str]:
            job_dir = server.base_temp_dir
            job_id = max((d.name for d in job_dir.iterdir() if d.is_dir()), key=lambda n: n)
            a = job_dir / job_id / "x" / "doc.ai.md"
            b = job_dir / job_id / "y" / "doc.ai.md"
            a.parent.mkdir(parents=True, exist_ok=True)
            b.parent.mkdir(parents=True, exist_ok=True)
            a.write_text("# first", encoding="utf-8")
            b.write_text("# second", encoding="utf-8")
            return (a, b), "done"

        manager.handlers["clean"] = _dupe_ai
        status, _, data = _post_job(port, "clean", {}, "doc.md", "# x")
        assert status == 202
        job_id = json.loads(data.decode("utf-8"))["job_id"]
        assert _wait_terminal(port, job_id)["status"] == "succeeded"

        status, headers, data = _request(port, "GET", f"/api/jobs/{job_id}/download")
        assert status == 200
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            assert sorted(names) == ["doc.ai-2.md", "doc.ai.md"]
            assert archive.read("doc.ai.md").decode("utf-8") == "# first"
            assert archive.read("doc.ai-2.md").decode("utf-8") == "# second"

    def test_directory_output_collected_recursively(self, make_server) -> None:
        import shutil
        leftover = Path.cwd() / ".formatconverter-jobs"
        if leftover.is_dir():
            shutil.rmtree(leftover)

        server, port = make_server()
        manager = server._manager

        def _dir_output(_params: dict) -> tuple[tuple[Path, ...], str]:
            job_dir = server.base_temp_dir
            job_id = max((d.name for d in job_dir.iterdir() if d.is_dir()), key=lambda n: n)
            out_dir = job_dir / job_id / "output"
            (out_dir / "sub").mkdir(parents=True, exist_ok=True)
            (out_dir / "one.md").write_text("# one", encoding="utf-8")
            (out_dir / "sub" / "two.md").write_text("# two", encoding="utf-8")
            return (out_dir,), "done"

        manager.handlers["clean"] = _dir_output
        status, _, data = _post_job(port, "clean", {}, "doc.md", "# x")
        assert status == 202
        job_id = json.loads(data.decode("utf-8"))["job_id"]
        assert _wait_terminal(port, job_id)["status"] == "succeeded"

        status, headers, data = _request(port, "GET", f"/api/jobs/{job_id}/download")
        assert status == 200
        assert headers["Content-Type"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            assert sorted(archive.namelist()) == ["one.md", "two.md"]
        assert str(server.base_temp_dir).encode() not in data


# ---------------------------------------------------------------------------
# security: non-loopback, static traversal, temp cleanup
# ---------------------------------------------------------------------------


class TestSecurity:
    def test_non_loopback_host_rejected(self) -> None:
        # clear non-loopback addresses / wildcard
        with pytest.raises(ValueError):
            JobWebServer(host="0.0.0.0")
        with pytest.raises(ValueError):
            JobWebServer(host="::")
        with pytest.raises(ValueError):
            create_server("0.0.0.0")
        # host names that merely *start with* a loopback prefix are not loopback
        with pytest.raises(ValueError):
            JobWebServer(host="127.0.0.1.evil")
        with pytest.raises(ValueError):
            JobWebServer(host="127.0.0.1.nip.io")

        server = JobWebServer()  # default 127.0.0.1
        try:
            with pytest.raises(ValueError):
                server.serve(host="0.0.0.0")
            with pytest.raises(ValueError):
                server.serve(host="127.0.0.1.evil")
        finally:
            server.shutdown()

    def test_static_file_served_and_traversal_blocked(self, make_server, tmp_path) -> None:
        static_dir = tmp_path / "static"
        static_dir.mkdir(parents=True, exist_ok=True)
        (static_dir / "hello.txt").write_text("hello static", encoding="utf-8")
        (static_dir / "secret.txt").write_text("secret", encoding="utf-8")
        server, port = make_server(static_dir=static_dir)

        status, headers, data = _request(port, "GET", "/static/hello.txt")
        assert status == 200
        assert data.decode("utf-8") == "hello static"

        # traversal attempts must not escape the static root
        assert _request(port, "GET", "/static/../secret.txt")[0] == 404
        assert _request(port, "GET", "/static/%2e%2e%2fsecret.txt")[0] == 404
        assert _request(port, "GET", "/static/missing.txt")[0] == 404

    def test_static_disabled_when_no_static_dir(self, make_server) -> None:
        server, port = make_server()
        assert _request(port, "GET", "/static/anything.txt")[0] == 404

    def test_shutdown_removes_temp_root(self, make_server) -> None:
        server, port = make_server()
        base = server.base_temp_dir
        assert base is not None and base.is_dir()
        server.shutdown()
        assert not base.exists()
        # shutdown is idempotent and safe to call again
        server.shutdown()

    def test_cleanup_job_valid_removes_job_dir(self, make_server) -> None:
        server, port = make_server()
        content = "# Doc\n\nAlpha.\n\nAlpha.\n\nBeta.\n"
        status, _, data = _post_job(port, "clean", {}, "doc.md", content)
        assert status == 202
        job_id = json.loads(data.decode("utf-8"))["job_id"]
        job_dir = server.base_temp_dir / job_id
        assert job_dir.is_dir()

        # wait for completion so the worker thread has released its file handles
        assert _wait_terminal(port, job_id)["status"] == "succeeded"
        server.cleanup_job(job_id)
        assert not job_dir.exists()
        # only the job's own directory was removed; the server root survives
        assert server.base_temp_dir.is_dir()

    def test_cleanup_job_unsafe_id_deletes_nothing(self, make_server) -> None:
        server, port = make_server()
        base = server.base_temp_dir
        marker = base / "marker.txt"
        marker.write_text("keep me", encoding="utf-8")

        # traversal / malformed / non-hex ids must never resolve to a path
        for bad in ["..", "../", ".", "0" * 31, "g" * 32, "G" * 32, None, "nope"]:
            server.cleanup_job(bad)  # must not raise and must not delete anything

        assert base.is_dir()
        assert marker.read_text(encoding="utf-8") == "keep me"

    def test_malformed_request_line_no_crash(self, make_server, capsys) -> None:
        server, port = make_server()
        for malformed in (b"GET\r\n\r\n", b"A B C D\r\n\r\n"):
            sock = socket.create_connection(("127.0.0.1", port), timeout=10)
            try:
                sock.sendall(malformed)
                data = sock.recv(4096)
                assert data, f"no response for malformed request {malformed!r}"
            finally:
                sock.close()

        # allow background handler threads to finish logging
        time.sleep(0.2)
        captured = capsys.readouterr()
        assert "Traceback" not in captured.err
        assert "Traceback" not in captured.out

        # the server must still be healthy
        assert _request(port, "GET", "/health")[0] == 200


# ---------------------------------------------------------------------------
# local API-key configuration endpoints (session-token protected)
# ---------------------------------------------------------------------------


def _get_session_token(port: int) -> str:
    """GET / and extract the injected session token from the served index."""
    status, _, data = _request(port, "GET", "/")
    assert status == 200
    match = re.search(rb'name="fc-session-token"\s+content="([^"]+)"', data)
    assert match is not None, "served index.html has no fc-session-token meta tag"
    token = match.group(1).decode("ascii")
    assert token != "__FC_SESSION_TOKEN__"
    assert len(token) > 20
    return token


def _save_key(port: int, token: str, api_key: str, *, origin: str | None = None,
              host: str | None = None) -> tuple[int, dict[str, str], bytes]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-FC-Session-Token"] = token
    if origin is None:
        origin = f"http://127.0.0.1:{port}"
    headers["Origin"] = origin
    if host is not None:
        headers["Host"] = host
    body = json.dumps({"api_key": api_key}).encode("utf-8")
    return _request(port, "POST", "/api/ai/key", body=body, headers=headers)


def _delete_key(port: int, token: str | None = None, *, origin: str | None = None,
                host: str | None = None) -> tuple[int, dict[str, str], bytes]:
    headers: dict[str, str] = {}
    if token is not None:
        headers["X-FC-Session-Token"] = token
    if origin is None:
        origin = f"http://127.0.0.1:{port}"
    headers["Origin"] = origin
    if host is not None:
        headers["Host"] = host
    return _request(port, "DELETE", "/api/ai/key", headers=headers)


def _save_model(port: int, token: str | None, model: str, *, origin: str | None = None,
                host: str | None = None) -> tuple[int, dict[str, str], bytes]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-FC-Session-Token"] = token
    if origin is None:
        origin = f"http://127.0.0.1:{port}"
    headers["Origin"] = origin
    if host is not None:
        headers["Host"] = host
    body = json.dumps({"model": model}).encode("utf-8")
    return _request(port, "POST", "/api/ai/models", body=body, headers=headers)


def _delete_model(port: int, token: str | None, model: str, *, origin: str | None = None,
                  host: str | None = None) -> tuple[int, dict[str, str], bytes]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-FC-Session-Token"] = token
    if origin is None:
        origin = f"http://127.0.0.1:{port}"
    headers["Origin"] = origin
    if host is not None:
        headers["Host"] = host
    body = json.dumps({"model": model}).encode("utf-8")
    return _request(port, "DELETE", "/api/ai/models", body=body, headers=headers)


def _post_connection_test(port: int, token: str | None, provider: str, model: str,
                          *, origin: str | None = None,
                          host: str | None = None) -> tuple[int, dict[str, str], bytes]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-FC-Session-Token"] = token
    if origin is None:
        origin = f"http://127.0.0.1:{port}"
    headers["Origin"] = origin
    if host is not None:
        headers["Host"] = host
    body = json.dumps({"provider": provider, "model": model}).encode("utf-8")
    return _request(port, "POST", "/api/ai/connection-test", body=body, headers=headers)


class TestKeyConfigEndpoints:
    def test_key_status_none(self, make_server, monkeypatch) -> None:
        monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
        server, port = make_server(static_dir=DEFAULT_STATIC_DIR)
        status, headers, data = _request(port, "GET", "/api/ai/key-status")
        assert status == 200
        assert json.loads(data.decode("utf-8")) == {"configured": False, "source": "none"}
        assert "Access-Control-Allow-Origin" not in headers

    def test_key_status_environment(self, make_server, monkeypatch) -> None:
        monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-env-secret-value")
        server, port = make_server(static_dir=DEFAULT_STATIC_DIR)
        status, _, data = _request(port, "GET", "/api/ai/key-status")
        assert status == 200
        assert json.loads(data.decode("utf-8")) == {
            "configured": True,
            "source": "environment",
        }
        body = data.decode("utf-8")
        assert "sk-" not in body
        assert "sk-env-secret-value" not in body

    def test_key_status_dotenv(self, make_server, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
        env_store.write_env_key("sk-dotenv-value", tmp_path / ".env")
        server, port = make_server(static_dir=DEFAULT_STATIC_DIR)
        status, _, data = _request(port, "GET", "/api/ai/key-status")
        assert status == 200
        assert json.loads(data.decode("utf-8")) == {
            "configured": True,
            "source": "dot_env",
        }

    def test_save_key_writes_env_and_preserves_lines(
        self, make_server, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
        (tmp_path / ".env").write_bytes(b"FOO=bar\n")
        server, port = make_server(static_dir=DEFAULT_STATIC_DIR)
        token = _get_session_token(port)
        status, _, data = _save_key(port, token, "  sk-test-saved-value  ")
        assert status == 200
        assert json.loads(data.decode("utf-8")) == {"saved": True}
        assert b"sk-test-saved-value" not in data
        content = (tmp_path / ".env").read_bytes()
        assert b'ORCAROUTER_API_KEY="sk-test-saved-value"\n' in content
        assert b"FOO=bar\n" in content

    def test_save_key_requires_auth(self, make_server, monkeypatch) -> None:
        monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
        server, port = make_server(static_dir=DEFAULT_STATIC_DIR)
        token = _get_session_token(port)
        body = json.dumps({"api_key": "sk-abc-12345"}).encode("utf-8")

        # missing token
        headers = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"}
        status, _, _ = _request(port, "POST", "/api/ai/key", body=body, headers=headers)
        assert status == 403
        # wrong token
        assert _save_key(port, "wrong-token", "sk-abc-12345")[0] == 403
        # missing Origin
        headers = {"Content-Type": "application/json", "X-FC-Session-Token": token}
        status, _, _ = _request(port, "POST", "/api/ai/key", body=body, headers=headers)
        assert status == 403
        # non-loopback Origin
        assert _save_key(port, token, "sk-abc-12345", origin="http://evil.example/")[0] == 403
        # DNS-rebinding Origin host
        assert _save_key(port, token, "sk-abc-12345", origin="http://127.0.0.1.evil.com/")[0] == 403
        # Origin with a path / userinfo / malformed port
        assert _save_key(port, token, "sk-abc-12345", origin=f"http://127.0.0.1:{port}/extra")[0] == 403
        assert _save_key(port, token, "sk-abc-12345", origin=f"http://user@127.0.0.1:{port}")[0] == 403
        assert _save_key(port, token, "sk-abc-12345", origin="http://127.0.0.1:abc")[0] == 403
        # non-loopback Host
        assert _save_key(port, token, "sk-abc-12345", host="evil.example")[0] == 403
        # Host that merely starts with a loopback prefix
        assert _save_key(port, token, "sk-abc-12345", host="127.0.0.1.evil:1234")[0] == 403
        # Host with userinfo / malformed port
        assert _save_key(port, token, "sk-abc-12345", host="evil@127.0.0.1")[0] == 403
        assert _save_key(port, token, "sk-abc-12345", host="127.0.0.1:abc")[0] == 403

    def test_save_key_validation(self, make_server, monkeypatch) -> None:
        monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
        server, port = make_server(static_dir=DEFAULT_STATIC_DIR)
        token = _get_session_token(port)
        headers = {
            "Content-Type": "application/json",
            "X-FC-Session-Token": token,
            "Origin": f"http://127.0.0.1:{port}",
        }

        def post(payload: object) -> int:
            body = json.dumps(payload).encode("utf-8")
            return _request(port, "POST", "/api/ai/key", body=body, headers=headers)[0]

        assert post({}) == 400  # missing api_key
        assert post({"api_key": 123}) == 400  # not a string
        assert post({"api_key": "   "}) == 400  # whitespace-only
        assert post({"api_key": "1234567"}) == 400  # too short
        assert post({"api_key": "x" * 1025}) == 400  # too long
        assert post({"api_key": "abc\ndefghi"}) == 400  # embedded LF (corrupts .env)
        assert post({"api_key": "abc\rdefghi"}) == 400  # embedded CR (corrupts .env)
        status, _, _ = _request(
            port, "POST", "/api/ai/key", body=b"not json", headers=headers
        )
        assert status == 400  # non-JSON body

    def test_delete_key_removes_only_key_line(
        self, make_server, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
        (tmp_path / ".env").write_bytes(b"FOO=bar\nORCAROUTER_API_KEY=sk-test-old\nBAZ=qux\n")
        server, port = make_server(static_dir=DEFAULT_STATIC_DIR)
        token = _get_session_token(port)
        status, _, data = _delete_key(port, token)
        assert status == 200
        assert json.loads(data.decode("utf-8")) == {"deleted": True}
        assert b"old" not in data
        assert (tmp_path / ".env").read_bytes() == b"FOO=bar\nBAZ=qux\n"

    def test_delete_key_requires_auth(self, make_server, monkeypatch) -> None:
        monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
        server, port = make_server(static_dir=DEFAULT_STATIC_DIR)
        token = _get_session_token(port)
        # missing token
        assert _delete_key(port)[0] == 403
        # wrong token
        assert _delete_key(port, "wrong-token")[0] == 403
        # missing Origin
        headers = {"X-FC-Session-Token": token}
        status, _, _ = _request(port, "DELETE", "/api/ai/key", headers=headers)
        assert status == 403
        # non-loopback Origin
        assert _delete_key(port, token, origin="http://evil.example/")[0] == 403
        # non-loopback Host
        assert _delete_key(port, token, host="evil.example")[0] == 403
        # DNS-rebinding Host
        assert _delete_key(port, token, host="127.0.0.1.evil:1234")[0] == 403

    def test_environment_source_not_removable_by_web(
        self, make_server, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-env-value")
        server, port = make_server(static_dir=DEFAULT_STATIC_DIR)
        token = _get_session_token(port)
        # POST saves a .env backup
        status, _, _ = _save_key(port, token, "sk-test-dotenv-backup")
        assert status == 200
        assert (tmp_path / ".env").read_bytes() == b'ORCAROUTER_API_KEY="sk-test-dotenv-backup"\n'
        # DELETE clears only the .env line
        status, _, _ = _delete_key(port, token)
        assert status == 200
        assert (tmp_path / ".env").read_bytes() == b""
        # the process environment variable is untouched
        assert os.environ["ORCAROUTER_API_KEY"] == "sk-env-value"
        # status stays "environment" throughout
        status, _, data = _request(port, "GET", "/api/ai/key-status")
        assert json.loads(data.decode("utf-8")) == {
            "configured": True,
            "source": "environment",
        }

    def test_two_servers_have_different_tokens(self, make_server, monkeypatch) -> None:
        monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
        server1, port1 = make_server(static_dir=DEFAULT_STATIC_DIR)
        server2, port2 = make_server(static_dir=DEFAULT_STATIC_DIR)
        token1 = _get_session_token(port1)
        token2 = _get_session_token(port2)
        assert token1 != token2
        # a token from server1 is rejected by server2 (stale/foreign token)
        assert _save_key(port2, token1, "sk-abc-12345")[0] == 403

    def test_no_key_material_in_any_response(
        self, make_server, monkeypatch
    ) -> None:
        secret = "sk-SUPER-SECRET-VALUE-987654321"
        monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
        server, port = make_server(static_dir=DEFAULT_STATIC_DIR)
        token = _get_session_token(port)
        responses: list[tuple[int, bytes]] = []

        # status endpoint
        r = _request(port, "GET", "/api/ai/key-status")
        responses.append((r[0], r[2]))
        # 403 (missing token, secret in the request body we send)
        headers = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"}
        body = json.dumps({"api_key": secret}).encode("utf-8")
        r = _request(port, "POST", "/api/ai/key", body=body, headers=headers)
        responses.append((r[0], r[2]))
        # 403 (missing Origin)
        headers = {"Content-Type": "application/json", "X-FC-Session-Token": token}
        r = _request(port, "POST", "/api/ai/key", body=body, headers=headers)
        responses.append((r[0], r[2]))
        # 400 (too short)
        headers = {
            "Content-Type": "application/json",
            "X-FC-Session-Token": token,
            "Origin": f"http://127.0.0.1:{port}",
        }
        r = _request(port, "POST", "/api/ai/key", body=json.dumps({"api_key": secret[:5]}).encode(), headers=headers)
        responses.append((r[0], r[2]))

        # 500 (write failure)
        def _boom(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr("format_converter.web_server.write_env_key", _boom)
        r = _save_key(port, token, secret)
        responses.append((r[0], r[2]))

        for status, data in responses:
            assert status in (200, 400, 403, 500)
            body = data.decode("utf-8", "replace")
            assert secret not in body, f"key leaked in HTTP {status}: {body!r}"
            assert "sk-" not in body, f"sk- prefix leaked in HTTP {status}: {body!r}"

    def test_no_cors_on_post_delete(self, make_server, monkeypatch) -> None:
        monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
        server, port = make_server(static_dir=DEFAULT_STATIC_DIR)
        token = _get_session_token(port)
        status, headers, _ = _save_key(port, token, "sk-abc-12345")
        assert status == 200
        assert "Access-Control-Allow-Origin" not in headers
        status, headers, _ = _delete_key(port, token)
        assert status == 200
        assert "Access-Control-Allow-Origin" not in headers

    def test_index_serves_token_and_no_store(self, make_server, monkeypatch) -> None:
        monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
        server, port = make_server(static_dir=DEFAULT_STATIC_DIR)
        status, headers, data = _request(port, "GET", "/")
        assert status == 200
        assert headers.get("Cache-Control") == "no-store"
        match = re.search(rb'name="fc-session-token"\s+content="([^"]+)"', data)
        assert match is not None
        assert match.group(1).decode("ascii") == server._session_token


# ---------------------------------------------------------------------------
# recent jobs listing (Step 4.1: recover progress after reload / mode switch)
# ---------------------------------------------------------------------------


class TestRecentJobs:
    def test_get_jobs_lists_recent_jobs_without_leaks(self, make_server) -> None:
        server, port = make_server()
        content = "# Doc\n\nAlpha.\n\nAlpha.\n\nBeta.\n"
        status, _, data = _post_job(port, "clean", {"dedupe": True}, "doc.md", content)
        assert status == 202
        job_id = json.loads(data.decode("utf-8"))["job_id"]
        assert _wait_terminal(port, job_id)["status"] == "succeeded"

        status, headers, data = _request(port, "GET", "/api/jobs")
        assert status == 200
        assert "Access-Control-Allow-Origin" not in headers
        payload = json.loads(data.decode("utf-8"))
        assert isinstance(payload.get("jobs"), list)
        entry = next(j for j in payload["jobs"] if j["job_id"] == job_id)
        assert entry["job_type"] == "clean"
        assert entry["status"] == "succeeded"
        assert isinstance(entry["created_at"], float) and entry["created_at"] > 0
        assert isinstance(entry["updated_at"], float) and entry["updated_at"] > 0
        assert "output_paths" not in entry
        body = data.decode("utf-8")
        assert "output_paths" not in body
        assert str(server.base_temp_dir) not in body  # no absolute temp path
        assert "sk-" not in body

    def test_get_jobs_includes_running_job(self, make_server) -> None:
        server, port = make_server()
        manager = server._manager
        entered = threading.Event()
        release = threading.Event()

        def _blocking(_params: dict) -> tuple[tuple[Path, ...], str]:
            entered.set()
            assert release.wait(5), "release event not set"
            return (Path("x.md"),), "blocked done"

        manager.handlers["clean"] = _blocking
        job_id = manager.submit("clean", {"file": "doc.md"})
        assert entered.wait(5), "handler never started"

        status, _, data = _request(port, "GET", "/api/jobs")
        assert status == 200
        jobs = json.loads(data.decode("utf-8"))["jobs"]
        running = next((j for j in jobs if j["job_id"] == job_id), None)
        assert running is not None
        assert running["status"] == "running"
        assert running["job_type"] == "clean"

        release.set()
        manager.wait(job_id, timeout=5)

    def test_job_status_response_has_metadata(self, make_server) -> None:
        server, port = make_server()
        content = "# Doc\n\nAlpha.\n\nAlpha.\n\nBeta.\n"
        status, _, data = _post_job(port, "clean", {"dedupe": True}, "doc.md", content)
        assert status == 202
        job_id = json.loads(data.decode("utf-8"))["job_id"]
        final = _wait_terminal(port, job_id)
        assert final["status"] == "succeeded"
        assert final["job_type"] == "clean"
        assert isinstance(final["created_at"], float)
        assert isinstance(final["updated_at"], float)
        assert "output_paths" not in final


# ---------------------------------------------------------------------------
# local model-name memory (Step 4.1)
# ---------------------------------------------------------------------------


class TestModelStore:
    def test_get_models_empty_by_default(self, make_server) -> None:
        server, port = make_server(static_dir=DEFAULT_STATIC_DIR)
        status, _, data = _request(port, "GET", "/api/ai/models")
        assert status == 200
        assert json.loads(data.decode("utf-8")) == {"models": []}

    def test_save_requires_auth(self, make_server, monkeypatch) -> None:
        monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
        server, port = make_server(static_dir=DEFAULT_STATIC_DIR)
        model = "deepseek/deepseek-v4-flash-free"
        # missing token
        status, _, _ = _save_model(port, None, model)
        assert status == 403
        # wrong token
        assert _save_model(port, "wrong-token", model)[0] == 403
        # missing Origin
        headers = {"Content-Type": "application/json", "X-FC-Session-Token": "x" * 32}
        body = json.dumps({"model": model}).encode("utf-8")
        status, _, _ = _request(port, "POST", "/api/ai/models", body=body, headers=headers)
        assert status == 403
        # non-loopback Origin
        token = _get_session_token(port)
        assert _save_model(port, token, model, origin="http://evil.example/")[0] == 403
        # non-loopback Host
        assert _save_model(port, token, model, host="evil.example")[0] == 403

    def test_save_list_delete_roundtrip(self, make_server, monkeypatch) -> None:
        monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
        server, port = make_server(static_dir=DEFAULT_STATIC_DIR)
        token = _get_session_token(port)
        model = "deepseek/deepseek-v4-flash-free"

        status, _, data = _save_model(port, token, model)
        assert status == 200
        saved = json.loads(data.decode("utf-8"))
        assert saved["models"] == [model]
        assert b"sk-" not in data

        status, _, data = _request(port, "GET", "/api/ai/models")
        assert status == 200
        assert json.loads(data.decode("utf-8")) == {"models": [model]}

        status, _, data = _delete_model(port, token, model)
        assert status == 200
        deleted = json.loads(data.decode("utf-8"))
        assert deleted["models"] == []
        assert b"sk-" not in data

    def test_save_is_idempotent_and_case_sensitive(self, make_server, monkeypatch) -> None:
        monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
        server, port = make_server(static_dir=DEFAULT_STATIC_DIR)
        token = _get_session_token(port)
        model = "deepseek/deepseek-v4-flash-free"
        _save_model(port, token, model)
        _save_model(port, token, model)  # identical -> no duplicate
        status, _, data = _request(port, "GET", "/api/ai/models")
        assert json.loads(data.decode("utf-8")) == {"models": [model]}
        # case differs -> a separate entry
        _save_model(port, token, model.upper())
        status, _, data = _request(port, "GET", "/api/ai/models")
        assert json.loads(data.decode("utf-8")) == {"models": [model, model.upper()]}

    def test_save_model_validation_400(self, make_server, monkeypatch) -> None:
        monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
        server, port = make_server(static_dir=DEFAULT_STATIC_DIR)
        token = _get_session_token(port)
        assert _save_model(port, token, "   ")[0] == 400  # whitespace-only
        assert _save_model(port, token, "x" * 201)[0] == 400  # too long
        assert _save_model(port, token, "a\nb")[0] == 400  # embedded LF
        assert _save_model(port, token, "a\rb")[0] == 400  # embedded CR

    def test_save_key_shaped_model_400_and_never_returned(
        self, make_server, monkeypatch
    ) -> None:
        monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
        server, port = make_server(static_dir=DEFAULT_STATIC_DIR)
        token = _get_session_token(port)
        # Built at runtime so the joined secret never appears literally in the
        # tracked source (the repo-wide key scan must stay clean).
        secret = "sk-" + "THISISAREALKEY123456"

        status, _, data = _save_model(port, token, secret)
        assert status == 400
        body = data.decode("utf-8", "replace")
        assert secret not in body
        assert "sk-" not in body

        # the rejected value is never remembered
        status, _, data = _request(port, "GET", "/api/ai/models")
        assert status == 200
        assert json.loads(data.decode("utf-8")) == {"models": []}

        # a legal model name still saves and lists after the rejection
        assert _save_model(port, token, "deepseek/deepseek-v4-flash-free")[0] == 200
        status, _, data = _request(port, "GET", "/api/ai/models")
        assert json.loads(data.decode("utf-8")) == {
            "models": ["deepseek/deepseek-v4-flash-free"]
        }

    def test_get_models_filters_preexisting_secret(
        self, make_server, monkeypatch
    ) -> None:
        monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
        from format_converter import model_store

        secret = "sk-" + "THISISAREALKEY123456"
        # The autouse model-store fixture points at this test's temp file;
        # seed it with dirty history the way an old file could contain.
        model_store.models_path().write_text(
            json.dumps({"models": ["deepseek/deepseek-v4-flash-free", secret]}),
            encoding="utf-8",
        )
        server, port = make_server(static_dir=DEFAULT_STATIC_DIR)
        status, _, data = _request(port, "GET", "/api/ai/models")
        assert status == 200
        assert json.loads(data.decode("utf-8")) == {
            "models": ["deepseek/deepseek-v4-flash-free"]
        }
        assert secret not in data.decode("utf-8", "replace")

    def test_delete_requires_auth(self, make_server, monkeypatch) -> None:
        monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
        server, port = make_server(static_dir=DEFAULT_STATIC_DIR)
        model = "deepseek/deepseek-v4-flash-free"
        assert _delete_model(port, None, model)[0] == 403
        assert _delete_model(port, "wrong-token", model)[0] == 403
        assert _delete_model(port, "wrong-token", model, origin="http://evil.example/")[0] == 403


# ---------------------------------------------------------------------------
# AI connection test (Step 4.1)
# ---------------------------------------------------------------------------


class TestConnectionTest:
    def test_connection_test_requires_auth(self, make_server, monkeypatch) -> None:
        monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
        server, port = make_server(static_dir=DEFAULT_STATIC_DIR)
        assert _post_connection_test(port, None, "orcarouter", "m1")[0] == 403
        assert _post_connection_test(port, "wrong-token", "orcarouter", "m1")[0] == 403
        assert _post_connection_test(
            port, "wrong-token", "orcarouter", "m1", origin="http://evil.example/"
        )[0] == 403

    def test_connection_test_unsupported_provider_400(self, make_server, monkeypatch) -> None:
        monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
        server, port = make_server(static_dir=DEFAULT_STATIC_DIR)
        token = _get_session_token(port)
        assert _post_connection_test(port, token, "bogus", "m1")[0] == 400

    def test_connection_test_missing_key_ok_false_no_leak(self, make_server, monkeypatch) -> None:
        monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
        server, port = make_server(static_dir=DEFAULT_STATIC_DIR)
        token = _get_session_token(port)
        status, _, data = _post_connection_test(port, token, "orcarouter", "m1")
        assert status == 200
        payload = json.loads(data.decode("utf-8"))
        assert payload == {"ok": False, "error": "未配置 API Key。"}
        assert b"sk-" not in data

    def test_connection_test_success_with_fake_client(self, make_server, monkeypatch) -> None:
        monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
        calls: list[dict] = []

        class FakeClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def complete(self, *, system: str, user: str, model: str) -> str:
                calls.append({"system": system, "user": user, "model": model})
                return "OK"

        monkeypatch.setattr("format_converter.web_server.get_api_key", lambda _cfg: "sk-test-fake-key")
        monkeypatch.setattr("format_converter.web_server.OpenAICompatClient", FakeClient)
        server, port = make_server(static_dir=DEFAULT_STATIC_DIR)
        token = _get_session_token(port)

        status, _, data = _post_connection_test(port, token, "orcarouter", "my-model")
        assert status == 200
        assert json.loads(data.decode("utf-8")) == {"ok": True}
        assert calls and calls[0]["model"] == "my-model"
        assert calls[0]["user"] == "OK"
        assert b"sk-" not in data

    def test_connection_test_maps_client_errors(self, make_server, monkeypatch) -> None:
        from format_converter.llm_client import (
            AuthenticationError,
            ConnectionFailedError,
            EmptyResponseError,
            PermissionDeniedError,
            RateLimitError,
            ServerError,
        )

        cases = [
            (AuthenticationError, "认证失败"),
            (PermissionDeniedError, "无权限"),
            (ConnectionFailedError, "无法连接"),
            (RateLimitError, "请求过于频繁"),
            (ServerError, "服务器错误"),
            (EmptyResponseError, "空响应"),
        ]
        monkeypatch.setattr("format_converter.web_server.get_api_key", lambda _cfg: "sk-test-fake-key")
        server, port = make_server(static_dir=DEFAULT_STATIC_DIR)
        token = _get_session_token(port)

        for exc_type, expected in cases:
            class BoomClient:
                def __init__(self, *args: object, **kwargs: object) -> None:
                    pass

                def complete(self, **kwargs: object) -> str:
                    raise exc_type("boom")

            monkeypatch.setattr("format_converter.web_server.OpenAICompatClient", BoomClient)
            status, _, data = _post_connection_test(port, token, "orcarouter", "m1")
            assert status == 200
            payload = json.loads(data.decode("utf-8"))
            assert payload["ok"] is False
            assert expected in payload["error"]
            assert b"sk-" not in data


# ---------------------------------------------------------------------------
# durable AI job checkpoint (Task 2: Step 4.3)
# ---------------------------------------------------------------------------


class TestDurableAIJob:
    def test_ai_clean_durable_job_directory_created(self, make_server, monkeypatch) -> None:
        monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-test-fake-key-for-durable")

        class FakeOpenAIClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def complete(self, *, system: str, user: str, model: str) -> str:
                return f"[revised] {user}"

        monkeypatch.setattr("format_converter.cli.OpenAICompatClient", FakeOpenAIClient)
        server, port = make_server()

        content = "# Alpha\n\nBeta.\n"
        status, _, data = _post_job(
            port, "ai-clean", {"provider": "orcarouter", "model": "m1"}, "doc.md", content
        )
        assert status == 202
        job_id = json.loads(data.decode("utf-8"))["job_id"]

        final = _wait_terminal(port, job_id)
        assert final["status"] == "succeeded"

        # Durable job directory should exist under CWD.
        durable_root = Path.cwd() / ".formatconverter-jobs"
        assert durable_root.is_dir()
        dirs = [d for d in durable_root.iterdir() if d.is_dir()]
        assert len(dirs) >= 1
        # No absolute path or API key leaks in responses.
        body = json.dumps(final)
        assert "sk-" not in body
        assert str(durable_root) not in body
        assert "output_paths" not in final

        # Download still works through the normal path.
        status, headers, data = _request(port, "GET", f"/api/jobs/{job_id}/download")
        assert status == 200
        assert 'filename="doc.ai.md"' in headers["Content-Disposition"]

    def test_durable_job_list_does_not_leak_paths(self, make_server, monkeypatch) -> None:
        monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-test-fake-key-for-durable")

        class FakeOpenAIClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def complete(self, *, system: str, user: str, model: str) -> str:
                return f"[revised] {user}"

        monkeypatch.setattr("format_converter.cli.OpenAICompatClient", FakeOpenAIClient)
        server, port = make_server()

        content = "# Alpha\n\nBeta.\n"
        status, _, data = _post_job(
            port, "ai-clean", {"provider": "orcarouter", "model": "m1"}, "doc.md", content
        )
        assert status == 202
        job_id = json.loads(data.decode("utf-8"))["job_id"]
        _wait_terminal(port, job_id)

        # List endpoint must not leak durable-job paths.
        status, _, data = _request(port, "GET", "/api/jobs")
        assert status == 200
        body = data.decode("utf-8")
        assert ".formatconverter-jobs" not in body
        assert "sk-" not in body
        assert "output_paths" not in body

    def test_durable_manifest_contains_web_job_id(self, make_server, monkeypatch) -> None:
        import json
        monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-test-fake-key-for-durable")

        class FakeOpenAIClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def complete(self, *, system: str, user: str, model: str) -> str:
                return f"[revised] {user}"

        monkeypatch.setattr("format_converter.cli.OpenAICompatClient", FakeOpenAIClient)
        server, port = make_server()

        content = "# Alpha\n\nBeta.\n"
        status, _, data = _post_job(
            port, "ai-clean", {"provider": "orcarouter", "model": "m1"}, "doc.md", content
        )
        assert status == 202
        job_id = json.loads(data.decode("utf-8"))["job_id"]

        final = _wait_terminal(port, job_id)
        assert final["status"] == "succeeded"

        # Find the durable manifest and check web_job_id.
        durable_root = Path.cwd() / ".formatconverter-jobs"
        for d in durable_root.iterdir():
            if not d.is_dir():
                continue
            manifest_path = d / "manifest.json"
            if manifest_path.is_file():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest.get("web_job_id") == job_id:
                    return
        pytest.fail(f"no durable manifest with web_job_id={job_id!r} found")

    def test_durable_manifest_trims_model_name(self, make_server, monkeypatch) -> None:
        import json
        monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-test-fake-key-for-durable")

        class FakeOpenAIClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def complete(self, *, system: str, user: str, model: str) -> str:
                return f"[revised] {user}"

        monkeypatch.setattr("format_converter.cli.OpenAICompatClient", FakeOpenAIClient)
        server, port = make_server()

        content = "# Alpha\n\nBeta.\n"
        status, _, data = _post_job(
            port, "ai-clean", {"provider": "orcarouter", "model": "  gpt-4o  "}, "doc.md", content
        )
        assert status == 202
        job_id = json.loads(data.decode("utf-8"))["job_id"]

        final = _wait_terminal(port, job_id)
        assert final["status"] == "succeeded"

        # Find the durable manifest and check model is trimmed.
        durable_root = Path.cwd() / ".formatconverter-jobs"
        for d in durable_root.iterdir():
            if not d.is_dir():
                continue
            manifest_path = d / "manifest.json"
            if manifest_path.is_file():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest.get("web_job_id") == job_id:
                    assert manifest["model"] == "gpt-4o"
                    return
        pytest.fail(f"no durable manifest with web_job_id={job_id!r} found")


# ---------------------------------------------------------------------------
# service restart recovery (Task 3: Step 4.3 resume)
# ---------------------------------------------------------------------------


class TestResumeDurableJob:
    def test_resume_interrupted_durable_job(self, make_server, monkeypatch, tmp_path) -> None:
        import json as _json
        monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-test-fake-key-for-resume")

        class FakeOpenAIClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def complete(self, *, system: str, user: str, model: str) -> str:
                return f"[revised] {user}"

        monkeypatch.setattr("format_converter.cli.OpenAICompatClient", FakeOpenAIClient)
        from format_converter.ai_jobs import AIJobStore
        from format_converter.web_server import _IdAwareJobManager

        server, port = make_server()
        content = "# Alpha\n\nBeta.\n"
        status, _, data = _post_job(
            port, "ai-clean", {"provider": "orcarouter", "model": "m1"}, "doc.md", content
        )
        assert status == 202
        job_id = _json.loads(data.decode("utf-8"))["job_id"]
        _wait_terminal(port, job_id)

        store = AIJobStore()
        store.mark_stale_running_interrupted()
        for m in store.scan_recent():
            if m.web_job_id == job_id:
                store.update_status(m.job_id, "interrupted")
                break

        manager = _IdAwareJobManager(ai_job_store=store)
        server2, port2 = make_server(manager=manager)

        status, _, data = _request(port2, "GET", f"/api/jobs/{job_id}")
        assert status == 200
        payload = _json.loads(data.decode("utf-8"))
        assert payload["status"] == "interrupted"

        status, _, data = _request(port2, "POST", f"/api/jobs/{job_id}/resume")
        assert status == 202
        payload = _json.loads(data.decode("utf-8"))
        assert payload["status"] == "running"

        final = _wait_terminal(port2, job_id)
        assert final["status"] == "succeeded"

    def test_resume_unknown_job_404(self, make_server) -> None:
        server, port = make_server()
        status, _, _ = _request(port, "POST", "/api/jobs/00000000000000000000000000000000/resume")
        assert status == 404

    def test_default_server_creates_ai_job_store(self, make_server) -> None:
        server, port = make_server()
        assert hasattr(server._manager, '_ai_job_store')
        assert server._manager._ai_job_store is not None

    def test_resume_succeeded_download_returns_200(self, make_server, monkeypatch, tmp_path) -> None:
        import json as _json
        monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-test-fake-key-for-resume-dl")

        class FakeOpenAIClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def complete(self, *, system: str, user: str, model: str) -> str:
                return f"[revised] {user}"

        monkeypatch.setattr("format_converter.cli.OpenAICompatClient", FakeOpenAIClient)
        from format_converter.ai_jobs import AIJobStore

        server, port = make_server()
        content = "# Alpha\n\nBeta.\n"
        status, _, data = _post_job(
            port, "ai-clean", {"provider": "orcarouter", "model": "m1"}, "doc.md", content
        )
        assert status == 202
        job_id = _json.loads(data.decode("utf-8"))["job_id"]
        _wait_terminal(port, job_id)

        store = AIJobStore()
        store.mark_stale_running_interrupted()
        for m in store.scan_recent():
            if m.web_job_id == job_id:
                store.update_status(m.job_id, "interrupted")
                break

        from format_converter.web_server import _IdAwareJobManager
        manager = _IdAwareJobManager(ai_job_store=store)
        server2, port2 = make_server(manager=manager)

        status, _, data = _request(port2, "POST", f"/api/jobs/{job_id}/resume")
        assert status == 202
        final = _wait_terminal(port2, job_id)
        assert final["status"] == "succeeded"

        status, _, data = _request(port2, "GET", f"/api/jobs/{job_id}/download")
        assert status == 200

    def test_resume_failure_message_sanitized(self, make_server, monkeypatch, tmp_path) -> None:
        import json as _json
        monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-secret-value")

        class BoomClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def complete(self, *, system: str, user: str, model: str) -> str:
                raise RuntimeError("sk-secret-value leaked in resume error")

        monkeypatch.setattr("format_converter.cli.OpenAICompatClient", BoomClient)
        # Resume runs a different code path (jobs._resume_single_chunk_loop) that
        # constructs the client from llm_client, so patch that too to keep the
        # test fully offline and deterministic.
        monkeypatch.setattr("format_converter.llm_client.OpenAICompatClient", BoomClient)
        from format_converter.ai_jobs import AIJobStore

        server, port = make_server()
        content = "# Alpha\n\nBeta.\n"
        status, _, data = _post_job(
            port, "ai-clean", {"provider": "orcarouter", "model": "m1"}, "doc.md", content
        )
        assert status == 202
        job_id = _json.loads(data.decode("utf-8"))["job_id"]
        _wait_terminal(port, job_id)

        store = AIJobStore()
        store.mark_stale_running_interrupted()
        for m in store.scan_recent():
            if m.web_job_id == job_id:
                store.update_status(m.job_id, "interrupted")
                break

        from format_converter.web_server import _IdAwareJobManager
        manager = _IdAwareJobManager(ai_job_store=store)
        server2, port2 = make_server(manager=manager)

        status, _, data = _request(port2, "POST", f"/api/jobs/{job_id}/resume")
        assert status == 202
        final = _wait_terminal(port2, job_id)
        assert final["status"] == "failed"
        body = _json.dumps(final)
        assert "sk-" not in body

    def test_resume_non_interrupted_409(self, make_server) -> None:
        server, port = make_server()
        content = "# Doc\n\nAlpha.\n\nAlpha.\n\nBeta.\n"
        status, _, data = _post_job(port, "clean", {"dedupe": True, "backup": True}, "doc.md", content)
        assert status == 202
        import json as _json
        job_id = _json.loads(data.decode("utf-8"))["job_id"]
        _wait_terminal(port, job_id)

        status, _, data = _request(port, "POST", f"/api/jobs/{job_id}/resume")
        assert status == 409
        payload = _json.loads(data.decode("utf-8"))
        assert "not in interrupted state" in payload["error"]


# ---------------------------------------------------------------------------
# Task 4: retry + delete job management (API level)
# ---------------------------------------------------------------------------


class TestRetryJob:
    def test_retry_failed_ai_clean_skips_completed_chunk(
        self, make_server, monkeypatch, tmp_path
    ) -> None:
        import json as _json
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-test-fake-key-for-retry")

        class EchoClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def complete(self, *, system: str, user: str, model: str) -> str:
                return f"[revised] {user}"

        monkeypatch.setattr("format_converter.cli.OpenAICompatClient", EchoClient)
        server, port = make_server()

        # Two-chunk document: chunk 1 (A-run) then chunk 2 (B-run + C).
        content = ("A" * 6000) + "\n\n" + ("B" * 6000) + "\n\n" + "C"
        status, _, data = _post_job(
            port, "ai-clean", {"provider": "orcarouter", "model": "m1"},
            "doc.md", content,
        )
        assert status == 202
        job_id = _json.loads(data.decode("utf-8"))["job_id"]
        final = _wait_terminal(port, job_id)
        assert final["status"] == "succeeded"

        from format_converter.ai_jobs import AIJobStore
        store = AIJobStore()
        manifest = next(m for m in store.find_by_web_job_id(job_id))
        assert manifest.total_chunks == 2
        results_dir = store.job_dir(manifest.job_id) / "results"
        assert (results_dir / "0001.md").is_file()
        # Simulate a durable failure after chunk 1: chunk 2's result vanished
        # and the manifest was marked failed.
        (results_dir / "0002.md").unlink()
        store.update_status(manifest.job_id, "failed")

        class RecordingClient:
            calls: list[str] = []

            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def complete(self, *, system: str, user: str, model: str) -> str:
                RecordingClient.calls.append(user)
                return f"[revised] {user}"

        RecordingClient.calls = []
        monkeypatch.setattr("format_converter.llm_client.OpenAICompatClient", RecordingClient)

        from format_converter.web_server import _IdAwareJobManager
        manager = _IdAwareJobManager(ai_job_store=store)
        server2, port2 = make_server(manager=manager)

        status, _, data = _request(port2, "GET", f"/api/jobs/{job_id}")
        assert status == 200
        assert _json.loads(data.decode("utf-8"))["status"] == "failed"

        status, _, data = _request(port2, "POST", f"/api/jobs/{job_id}/retry")
        assert status == 202
        payload = _json.loads(data.decode("utf-8"))
        assert payload["status"] == "running"

        final = _wait_terminal(port2, job_id)
        assert final["status"] == "succeeded"
        # Only the missing chunk (chunk 2) was re-requested — once.
        assert len(RecordingClient.calls) == 1
        assert ("B" * 6000) in RecordingClient.calls[0]
        assert store.load(manifest.job_id).status == "completed"
        assert (results_dir / "0002.md").is_file()
        # The previously completed chunk-1 result was never re-requested and
        # is untouched.
        assert (results_dir / "0001.md").is_file()

    def test_retry_rejects_succeeded(self, make_server) -> None:
        server, port = make_server()
        content = "# Doc\n\nAlpha.\n\nAlpha.\n\nBeta.\n"
        status, _, data = _post_job(
            port, "clean", {"dedupe": True, "backup": True}, "doc.md", content
        )
        assert status == 202
        import json as _json
        job_id = _json.loads(data.decode("utf-8"))["job_id"]
        _wait_terminal(port, job_id)
        status, _, data = _request(port, "POST", f"/api/jobs/{job_id}/retry")
        assert status == 409
        payload = _json.loads(data.decode("utf-8"))
        assert "retryable" in payload["error"]

    def test_retry_unknown_job_404(self, make_server) -> None:
        server, port = make_server()
        status, _, _ = _request(
            port, "POST", "/api/jobs/00000000000000000000000000000000/retry"
        )
        assert status == 404

    def test_retry_while_running_409(self, make_server) -> None:
        server, port = make_server()
        entered = threading.Event()
        release = threading.Event()

        def _blocking_ai(_params: dict) -> tuple[tuple[Path, ...], str]:
            entered.set()
            assert release.wait(10), "release event not set"
            return (Path("out.md"),), "blocked done"

        server._manager.handlers["ai-clean"] = _blocking_ai
        status, _, data = _post_job(
            port, "ai-clean", {"provider": "orcarouter", "model": "m1"},
            "doc.md", "# Alpha\n\nBeta.\n",
        )
        assert status == 202
        import json as _json
        job_id = _json.loads(data.decode("utf-8"))["job_id"]
        assert entered.wait(5), "ai-clean handler never started"

        status, _, data = _request(port, "POST", f"/api/jobs/{job_id}/retry")
        assert status == 409
        payload = _json.loads(data.decode("utf-8"))
        assert "retryable" in payload["error"]

        release.set()
        final = _wait_terminal(port, job_id)
        assert final["status"] == "succeeded"

    def test_retry_rejects_failed_non_ai_job(self, make_server) -> None:
        server, port = make_server()

        def _boom(_params: dict) -> tuple[tuple[Path, ...], str]:
            raise RuntimeError("boom")

        server._manager.handlers["clean"] = _boom
        status, _, data = _post_job(port, "clean", {}, "doc.md", "# hi")
        assert status == 202
        import json as _json
        job_id = _json.loads(data.decode("utf-8"))["job_id"]
        final = _wait_terminal(port, job_id)
        assert final["status"] == "failed"

        status, _, data = _request(port, "POST", f"/api/jobs/{job_id}/retry")
        assert status == 409
        payload = _json.loads(data.decode("utf-8"))
        assert "Only AI jobs can be retried" in payload["error"]


class TestDeleteJob:
    def test_delete_ai_clean_job_removes_temp_and_durable(
        self, make_server, monkeypatch, tmp_path
    ) -> None:
        import json as _json
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-test-fake-key-for-delete")

        class EchoClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def complete(self, *, system: str, user: str, model: str) -> str:
                return f"[revised] {user}"

        monkeypatch.setattr("format_converter.cli.OpenAICompatClient", EchoClient)
        server, port = make_server()

        content = "# Alpha\n\nBeta.\n"
        status, _, data = _post_job(
            port, "ai-clean", {"provider": "orcarouter", "model": "m1"},
            "doc.md", content,
        )
        assert status == 202
        job_id = _json.loads(data.decode("utf-8"))["job_id"]
        final = _wait_terminal(port, job_id)
        assert final["status"] == "succeeded"

        from format_converter.ai_jobs import AIJobStore
        store = AIJobStore()
        assert store.find_by_web_job_id(job_id) != []
        base = server.base_temp_dir
        assert base is not None
        assert (base / job_id).is_dir()

        status, _, data = _request(port, "DELETE", f"/api/jobs/{job_id}")
        assert status == 200
        payload = _json.loads(data.decode("utf-8"))
        assert payload == {"deleted": True, "job_id": job_id}
        body = data.decode("utf-8")
        assert "sk-" not in body
        assert str(base) not in body

        # Snapshot gone from status and list, temp dir gone, durable gone.
        assert _request(port, "GET", f"/api/jobs/{job_id}")[0] == 404
        assert not (base / job_id).exists()
        assert store.find_by_web_job_id(job_id) == []

    def test_delete_clean_terminal_job(self, make_server) -> None:
        import json as _json
        server, port = make_server()
        content = "# Doc\n\nAlpha.\n\nAlpha.\n\nBeta.\n"
        status, _, data = _post_job(
            port, "clean", {"dedupe": True, "backup": True}, "doc.md", content
        )
        assert status == 202
        job_id = _json.loads(data.decode("utf-8"))["job_id"]
        _wait_terminal(port, job_id)

        base = server.base_temp_dir
        assert base is not None
        assert (base / job_id).is_dir()
        status, _, data = _request(port, "DELETE", f"/api/jobs/{job_id}")
        assert status == 200
        assert not (base / job_id).exists()
        assert _request(port, "GET", f"/api/jobs/{job_id}")[0] == 404

    def test_delete_unknown_job_404(self, make_server) -> None:
        server, port = make_server()
        status, _, _ = _request(
            port, "DELETE", "/api/jobs/00000000000000000000000000000000"
        )
        assert status == 404

    def test_delete_running_job_409(self, make_server) -> None:
        server, port = make_server()
        entered = threading.Event()
        release = threading.Event()

        def _blocking_ai(_params: dict) -> tuple[tuple[Path, ...], str]:
            entered.set()
            assert release.wait(10), "release event not set"
            return (Path("out.md"),), "blocked done"

        server._manager.handlers["ai-clean"] = _blocking_ai
        status, _, data = _post_job(
            port, "ai-clean", {"provider": "orcarouter", "model": "m1"},
            "doc.md", "# Alpha\n\nBeta.\n",
        )
        assert status == 202
        import json as _json
        job_id = _json.loads(data.decode("utf-8"))["job_id"]
        assert entered.wait(5), "ai-clean handler never started"

        status, _, data = _request(port, "DELETE", f"/api/jobs/{job_id}")
        assert status == 409
        release.set()
        _wait_terminal(port, job_id)

    def test_delete_does_not_touch_unrelated_durable(
        self, make_server, monkeypatch, tmp_path
    ) -> None:
        import json as _json
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-test-fake-key-for-delete-multi")

        class EchoClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def complete(self, *, system: str, user: str, model: str) -> str:
                return f"[revised] {user}"

        monkeypatch.setattr("format_converter.cli.OpenAICompatClient", EchoClient)
        server, port = make_server()

        content = "# Alpha\n\nBeta.\n"
        status, _, data = _post_job(
            port, "ai-clean", {"provider": "orcarouter", "model": "m1"},
            "doc.md", content,
        )
        assert status == 202
        job_id = _json.loads(data.decode("utf-8"))["job_id"]
        _wait_terminal(port, job_id)

        from format_converter.ai_jobs import AIJobStore
        store = AIJobStore()
        # A second, unrelated durable job in the same store must survive.
        other = store.create_job(
            tmp_path / "other.md", "Keep me.", "orcarouter", "m1", 100,
            web_job_id="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
            output_basename="keep.ai.md",
        )
        assert store.find_by_web_job_id(job_id) != []
        assert store.load(other.job_id).status == "running"

        status, _, data = _request(port, "DELETE", f"/api/jobs/{job_id}")
        assert status == 200
        assert store.find_by_web_job_id(job_id) == []
        assert store.load(other.job_id).status == "running"
        assert store.job_dir(other.job_id).is_dir()


# ---------------------------------------------------------------------------
# Task 4 (fix): durable jobs older than the startup hydration window
# ---------------------------------------------------------------------------


class TestOldDurableJobs:
    """Handlers must not 404 on durable jobs that startup hydration skipped.

    Startup only snapshots ``scan_recent()`` (20 newest) into JobManager
    memory. These tests seed a target that is deliberately older than 25 newer
    durable jobs, so it has no in-memory snapshot — yet resume/retry/delete must
    still act on its checkpoints via the durable store.
    """

    def _seed_old_target(self, store, tmp_path, *, target_status="failed",
                         completed=False) -> tuple[str, object]:
        import json as _json
        import uuid as _uuid
        web_id = _uuid.uuid4().hex
        manifest = store.create_job(
            tmp_path / "target.md", "One.\n\nTwo.", "orcarouter", "m1", 6,
            web_job_id=web_id, output_basename="target.ai.md",
        )
        assert manifest.total_chunks == 2
        if completed:
            store.save_result(manifest.job_id, 1, "R1")
            store.save_result(manifest.job_id, 2, "R2")
            store.merge(manifest.job_id)
        else:
            # Only chunk 1's result is on disk; chunk 2 stays missing so a
            # later continuation must re-request exactly one chunk.
            store.save_result(manifest.job_id, 1, "R1")
            store.update_status(manifest.job_id, target_status)
        # Push the target far below the newest-20 window.
        manifest_path = store.job_dir(manifest.job_id) / "manifest.json"
        raw = _json.loads(manifest_path.read_text(encoding="utf-8"))
        raw["created_at"] = 0.0
        raw["updated_at"] = 0.0
        manifest_path.write_text(_json.dumps(raw), encoding="utf-8")
        # 26 newer durable jobs guarantee scan_recent(20) never sees the target.
        for i in range(26):
            fill_id = _uuid.uuid4().hex
            fill = store.create_job(
                tmp_path / f"fill{i}.md", "Keep.", "orcarouter", "m1", 100,
                web_job_id=fill_id, output_basename=f"fill{i}.ai.md",
            )
            store.update_status(fill.job_id, "completed")
        return web_id, manifest

    def _server_for(self, make_server, store):
        from format_converter.ai_jobs import AIJobStore
        from format_converter.web_server import _IdAwareJobManager
        assert isinstance(store, AIJobStore)
        manager = _IdAwareJobManager(ai_job_store=store)
        return make_server(manager=manager)

    def test_retry_old_failed_job_beyond_hydration_returns_202(
        self, make_server, monkeypatch, tmp_path
    ) -> None:
        import json as _json
        monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-test-fake-key-for-old-retry")
        from format_converter.ai_jobs import AIJobStore
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        web_id, manifest = self._seed_old_target(store, tmp_path, target_status="failed")

        class RecordingClient:
            calls: list[str] = []

            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def complete(self, *, system: str, user: str, model: str) -> str:
                RecordingClient.calls.append(user)
                return f"[revised] {user}"

        RecordingClient.calls = []
        monkeypatch.setattr("format_converter.llm_client.OpenAICompatClient", RecordingClient)
        server, port = self._server_for(make_server, store)

        # No in-memory snapshot (older than the hydration window).
        status, _, _ = _request(port, "GET", f"/api/jobs/{web_id}")
        assert status == 404

        status, _, data = _request(port, "POST", f"/api/jobs/{web_id}/retry")
        assert status == 202
        payload = _json.loads(data.decode("utf-8"))
        assert payload["status"] == "running"
        body = data.decode("utf-8")
        assert "sk-" not in body and "output_paths" not in body
        assert str(server.base_temp_dir) not in body

        final = _wait_terminal(port, web_id)
        assert final["status"] == "succeeded"
        # Only the missing chunk was re-requested.
        assert len(RecordingClient.calls) == 1
        assert RecordingClient.calls[0] == "Two."
        assert store.load(manifest.job_id).status == "completed"

    def test_resume_old_interrupted_job_beyond_hydration_returns_202(
        self, make_server, monkeypatch, tmp_path
    ) -> None:
        import json as _json
        monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-test-fake-key-for-old-resume")
        from format_converter.ai_jobs import AIJobStore
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        web_id, manifest = self._seed_old_target(
            store, tmp_path, target_status="interrupted"
        )

        class RecordingClient:
            calls: list[str] = []

            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def complete(self, *, system: str, user: str, model: str) -> str:
                RecordingClient.calls.append(user)
                return f"[revised] {user}"

        RecordingClient.calls = []
        monkeypatch.setattr("format_converter.llm_client.OpenAICompatClient", RecordingClient)
        server, port = self._server_for(make_server, store)

        status, _, _ = _request(port, "GET", f"/api/jobs/{web_id}")
        assert status == 404

        status, _, data = _request(port, "POST", f"/api/jobs/{web_id}/resume")
        assert status == 202
        body = data.decode("utf-8")
        assert "sk-" not in body and "output_paths" not in body
        assert str(server.base_temp_dir) not in body

        final = _wait_terminal(port, web_id)
        assert final["status"] == "succeeded"
        assert len(RecordingClient.calls) == 1
        assert RecordingClient.calls[0] == "Two."
        assert store.load(manifest.job_id).status == "completed"

    def test_delete_old_durable_without_snapshot(self, make_server, tmp_path) -> None:
        import json as _json
        from format_converter.ai_jobs import AIJobStore
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        web_id, _ = self._seed_old_target(store, tmp_path, target_status="failed")
        assert store.find_by_web_job_id(web_id) != []

        server, port = self._server_for(make_server, store)
        status, _, _ = _request(port, "GET", f"/api/jobs/{web_id}")
        assert status == 404

        status, _, data = _request(port, "DELETE", f"/api/jobs/{web_id}")
        assert status == 200
        payload = _json.loads(data.decode("utf-8"))
        assert payload == {"deleted": True, "job_id": web_id}
        body = data.decode("utf-8")
        assert "sk-" not in body and "output_paths" not in body
        assert str(server.base_temp_dir) not in body
        # The durable checkpoint is gone even though no snapshot ever existed.
        assert store.find_by_web_job_id(web_id) == []

    def test_completed_old_durable_retry_and_resume_409(self, make_server, tmp_path) -> None:
        from format_converter.ai_jobs import AIJobStore
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        web_id, _ = self._seed_old_target(store, tmp_path, completed=True)
        server, port = self._server_for(make_server, store)

        status, _, _ = _request(port, "GET", f"/api/jobs/{web_id}")
        assert status == 404

        status, _, data = _request(port, "POST", f"/api/jobs/{web_id}/retry")
        assert status == 409
        import json as _json
        assert "retryable" in _json.loads(data.decode("utf-8"))["error"]

        status, _, data = _request(port, "POST", f"/api/jobs/{web_id}/resume")
        assert status == 409
        assert "not in interrupted state" in _json.loads(data.decode("utf-8"))["error"]
        # The completed checkpoint is untouched.
        assert store.find_by_web_job_id(web_id) != []

    def test_unknown_old_id_still_404(self, make_server, tmp_path) -> None:
        from format_converter.ai_jobs import AIJobStore
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        unknown = "a" * 32
        server, port = self._server_for(make_server, store)
        assert _request(port, "POST", f"/api/jobs/{unknown}/retry")[0] == 404
        assert _request(port, "POST", f"/api/jobs/{unknown}/resume")[0] == 404
        assert _request(port, "DELETE", f"/api/jobs/{unknown}")[0] == 404
