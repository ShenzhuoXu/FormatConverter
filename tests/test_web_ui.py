"""Offline tests for the packaged single-page web UI (Step 4).

Everything runs against a real localhost socket using only the standard
library (``http.client``): no browser, no browser automation, and no third
party dependencies. ``create_server()`` now defaults its ``static_dir`` to the
packaged ``format_converter/web/static`` directory, so the UI is served out of
the box.
"""

from __future__ import annotations

import base64
import http.client
import io
import json
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

import pytest

from format_converter.web_server import DEFAULT_STATIC_DIR, create_server

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "format_converter" / "web" / "static"
INDEX_HTML = STATIC_DIR / "index.html"
APP_JS = STATIC_DIR / "app.js"
STYLES_CSS = STATIC_DIR / "styles.css"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _request(
    port: int,
    method: str,
    path: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, dict(resp.getheaders()), data
    finally:
        conn.close()


@pytest.fixture
def server():
    """Start a real server with the default (packaged) static dir."""
    srv = create_server(port=0)
    try:
        yield srv
    finally:
        srv.shutdown()


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _post_clean(port: int, content: str) -> tuple[int, dict[str, str], bytes]:
    payload = {
        "job_type": "clean",
        "params": {},
        "upload": {"filename": "doc.md", "data_b64": _b64(content)},
    }
    body = json.dumps(payload).encode("utf-8")
    return _request(
        port, "POST", "/api/jobs", body=body, headers={"Content-Type": "application/json"}
    )


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


# ---------------------------------------------------------------------------
# packaged static content is served by default
# ---------------------------------------------------------------------------


class TestStaticServing:
    def test_default_static_dir_points_at_packaged_ui(self) -> None:
        assert (DEFAULT_STATIC_DIR / "index.html").is_file()
        assert (DEFAULT_STATIC_DIR / "app.js").is_file()
        assert (DEFAULT_STATIC_DIR / "styles.css").is_file()

    def test_index_served_with_default_static_dir(self, server) -> None:
        status, headers, data = _request(server.port, "GET", "/")
        assert status == 200
        assert headers["Content-Type"].startswith("text/html")
        body = data.decode("utf-8")
        for keyword in ("FormatConverter", "PDF", "Markdown", "AI", "清理", "流水线", "校对"):
            assert keyword in body, f"index.html missing keyword {keyword!r}"

    def test_app_js_served(self, server) -> None:
        status, headers, data = _request(server.port, "GET", "/static/app.js")
        assert status == 200
        assert "javascript" in headers["Content-Type"]
        assert data.decode("utf-8").strip()

    def test_styles_css_served(self, server) -> None:
        status, headers, data = _request(server.port, "GET", "/static/styles.css")
        assert status == 200
        assert "css" in headers["Content-Type"]
        assert data.decode("utf-8").strip()

    def test_health_still_works(self, server) -> None:
        status, _, data = _request(server.port, "GET", "/health")
        assert status == 200
        assert json.loads(data.decode("utf-8")) == {"status": "ok"}


# ---------------------------------------------------------------------------
# page-source compliance (no localStorage / external links / third parties)
# ---------------------------------------------------------------------------


class TestPageSourceCompliance:
    def test_index_has_no_forbidden_content(self) -> None:
        html = INDEX_HTML.read_text(encoding="utf-8")
        assert "localStorage" not in html
        assert "http://" not in html and "https://" not in html

        scripts = re.findall(r"<script\b[^>]*>", html)
        assert scripts, "index.html has no <script> tag at all"
        for tag in scripts:
            m = re.search(r'\bsrc\s*=\s*"([^"]*)"', tag)
            assert m is not None, f"script tag without src: {tag}"
            assert not m.group(1).startswith(("http://", "https://", "//", "/"))
            assert m.group(1) == "app.js"

        links = re.findall(r"<link\b[^>]*>", html)
        assert links, "index.html has no <link> tag at all"
        for tag in links:
            m = re.search(r'\bhref\s*=\s*"([^"]*)"', tag)
            assert m is not None, f"link tag without href: {tag}"
            assert not m.group(1).startswith(("http://", "https://", "//", "/"))
            assert m.group(1) == "styles.css"

    def test_app_js_has_no_forbidden_content(self) -> None:
        js = APP_JS.read_text(encoding="utf-8")
        assert "localStorage" not in js
        assert "cookie" not in js
        assert 'fetch("http' not in js
        assert 'fetch(`http' not in js

    def test_index_has_no_api_key_input(self) -> None:
        html = INDEX_HTML.read_text(encoding="utf-8")
        assert 'type="password"' not in html
        assert "apikey" not in html.lower()


# ---------------------------------------------------------------------------
# JS syntax check (Node available on this machine)
# ---------------------------------------------------------------------------


class TestJsSyntax:
    @pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
    def test_app_js_parses_with_node(self) -> None:
        result = subprocess.run(
            ["node", "--check", str(APP_JS)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"node --check failed:\n{result.stderr}"


# ---------------------------------------------------------------------------
# end-to-end flow mimicking app.js (upload -> poll -> download)
# ---------------------------------------------------------------------------


class TestEndToEndFlow:
    def test_clean_flow_upload_poll_download(self, server) -> None:
        content = "# Doc\n\nAlpha.\n\nAlpha.\n\nBeta.\n"
        status, _, data = _post_clean(server.port, content)
        assert status == 202
        payload = json.loads(data.decode("utf-8"))
        job_id = payload["job_id"]
        assert payload["status"] == "queued"

        final = _wait_terminal(server.port, job_id)
        assert final["status"] == "succeeded"
        assert final["job_id"] == job_id

        status, headers, data = _request(server.port, "GET", f"/api/jobs/{job_id}/download")
        assert status == 200
        assert headers["Content-Type"] == "application/zip"
        assert "attachment" in headers["Content-Disposition"]

        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            assert archive.namelist() == ["input/doc.md"]
            extracted = archive.read("input/doc.md").decode("utf-8")
        assert extracted == "# Doc\n\nAlpha.\n\nBeta.\n"
        assert extracted.count("Alpha.") == 1  # dedupe actually ran
