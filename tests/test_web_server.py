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
import socket
import threading
import time
import zipfile
from pathlib import Path

import pytest

from format_converter.web_server import JobWebServer, create_server


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
        assert _request(port, "GET", "/api/jobs")[0] == 404
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

        # Download the ZIP and verify it is exactly the cleaned result.
        status, headers, data = _request(port, "GET", f"/api/jobs/{job_id}/download")
        assert status == 200
        assert headers["Content-Type"] == "application/zip"
        assert "attachment" in headers["Content-Disposition"]
        assert "Access-Control-Allow-Origin" not in headers

        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            assert names == ["input/doc.md"]  # job-relative, no absolute path
            extracted = archive.read("input/doc.md").decode("utf-8")

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

    def test_convert_empty_file_rejected_400(self, make_server) -> None:
        server, port = make_server()
        status, _, _ = _post_job(port, "convert", {}, "doc.pdf", "")
        assert status == 400


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
