"""Offline tests for the packaged single-page web UI (Step 4 redesign).

Everything runs against a real localhost socket using only the standard
library (``http.client``): no browser, no browser automation, and no third
party dependencies. ``create_server()`` now defaults its ``static_dir`` to the
packaged ``format_converter/web/static`` directory, so the UI is served out of
the box.

The UI contract covered here:

- One shared file picker with ``multiple``, a drop zone, a per-file list,
  a count/size summary, and a clear button.
- The frontend always submits through the ``uploads`` array (even for a
  single file) and never relies on ``files[0]`` as the only upload.
- The download button text is always ``下载结果`` (the format is decided by
  the backend, never predicted by the frontend).
- No persistent browser storage, no third-party resources, no console.log.
"""

from __future__ import annotations

import base64
import http.client
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from format_converter.web_server import DEFAULT_STATIC_DIR, create_server

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "format_converter" / "web" / "static"
INDEX_HTML = STATIC_DIR / "index.html"
APP_JS = STATIC_DIR / "app.js"
STYLES_CSS = STATIC_DIR / "styles.css"

# Anything that would violate the "zero persistence / zero telemetry" rule.
FORBIDDEN_TOKENS = (
    "localStorage",
    "sessionStorage",
    "indexedDB",
    "document.cookie",
    "console.log",
)


def _is_same_origin_static(url: str) -> bool:
    """True only for a same-origin ``/static/`` path (never external/CDN)."""
    return url.startswith("/static/") and not url.startswith("//")


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

    def test_referenced_assets_are_servable(self, server) -> None:
        # Regression: the page must load in a real browser, so every
        # /static/ asset the served index.html references must return 200.
        status, _, data = _request(server.port, "GET", "/")
        assert status == 200
        html = data.decode("utf-8")
        refs = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
        assert refs, "page references no /static/ assets"
        for ref in refs:
            s, _, _ = _request(server.port, "GET", ref)
            assert s == 200, f"page-referenced asset is not servable: {ref}"


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
            assert _is_same_origin_static(m.group(1)), (
                f"script src must be a same-origin /static/ path: {m.group(1)!r}"
            )

        links = re.findall(r"<link\b[^>]*>", html)
        assert links, "index.html has no <link> tag at all"
        for tag in links:
            m = re.search(r'\bhref\s*=\s*"([^"]*)"', tag)
            assert m is not None, f"link tag without href: {tag}"
            assert _is_same_origin_static(m.group(1)), (
                f"link href must be a same-origin /static/ path: {m.group(1)!r}"
            )

    def test_app_js_has_no_forbidden_content(self) -> None:
        js = APP_JS.read_text(encoding="utf-8")
        for token in FORBIDDEN_TOKENS:
            assert token not in js, f"app.js contains forbidden token {token!r}"
        assert 'fetch("http' not in js
        assert 'fetch(`http' not in js
        assert "http://" not in js and "https://" not in js
        # The key request flow sends the session token header and clears the
        # password input after each request.
        assert "X-FC-Session-Token" in js
        assert 'value = ""' in js

    def test_styles_css_has_no_forbidden_content(self) -> None:
        css = STYLES_CSS.read_text(encoding="utf-8")
        for token in FORBIDDEN_TOKENS:
            assert token not in css, f"styles.css contains forbidden token {token!r}"
        assert "http://" not in css and "https://" not in css

    def test_no_external_urls_in_any_static_file(self) -> None:
        # A static file that is not itself an external resource must never
        # reference one: no absolute http(s) URLs anywhere in the packaged UI.
        for path in (INDEX_HTML, APP_JS, STYLES_CSS):
            text = path.read_text(encoding="utf-8")
            assert "http://" not in text, f"external URL in {path.name}"
            assert "https://" not in text, f"external URL in {path.name}"

    def test_index_contains_key_config_region(self) -> None:
        html = INDEX_HTML.read_text(encoding="utf-8")
        # The OrcaRouter API config region: password input + copy + buttons.
        assert 'type="password"' in html
        assert 'id="ai-api-key"' in html
        assert 'name="api_key"' in html
        assert 'autocomplete="off"' in html
        assert 'autocorrect="off"' in html
        assert 'autocapitalize="off"' in html
        assert 'spellcheck="false"' in html
        assert "API Key 仅保存于本机项目目录的 .env 文件，不保存到浏览器。" in html
        assert "执行 AI 校对时，Python 后端会使用该 Key 调用 OrcaRouter（真实网络请求）。" in html
        assert 'data-key-save' in html
        assert 'data-key-clear' in html
        assert 'data-key-detect' in html
        assert 'data-key-config-status' in html
        assert 'data-key-section' in html
        assert "保存到本地" in html
        assert "清除本地 Key" in html
        assert "重新检测" in html

    def test_ai_key_section_collapsed_by_default(self) -> None:
        # The AI key region only expands in ai-clean mode; the default mode
        # (convert) must start with it collapsed. app.js toggles it on switch.
        js = APP_JS.read_text(encoding="utf-8")
        assert "hidden = !isAi" in js
        # The section is discoverable and has a stable container hook.
        html = INDEX_HTML.read_text(encoding="utf-8")
        assert 'id="ai-key-section"' in html

    def test_index_contains_session_token_placeholder(self) -> None:
        html = INDEX_HTML.read_text(encoding="utf-8")
        # The tracked static file must contain only the placeholder; the server
        # replaces it with the live token when serving.
        assert 'name="fc-session-token"' in html
        assert "__FC_SESSION_TOKEN__" in html
        assert "sessionStorage" not in html
        assert "indexedDB" not in html

    def test_all_file_inputs_have_multiple(self) -> None:
        # Every file picker on the page must allow selecting more than one file.
        html = INDEX_HTML.read_text(encoding="utf-8")
        inputs = re.findall(r'<input\b[^>]*\btype="file"[^>]*>', html)
        assert inputs, "no file input at all"
        for tag in inputs:
            assert re.search(r"\bmultiple\b", tag), f"file input lacks multiple: {tag}"

    def test_page_has_file_list_summary_and_clear(self) -> None:
        html = INDEX_HTML.read_text(encoding="utf-8")
        assert 'id="file-list"' in html  # per-file list
        assert 'id="file-summary"' in html  # count / size summary
        assert 'id="clear-files-btn"' in html  # clear button
        assert "清空" in html

    def test_download_button_text_is_download_result(self) -> None:
        # The download control is always labelled "下载结果"; the format is
        # decided by the backend, never predicted by the frontend.
        html = INDEX_HTML.read_text(encoding="utf-8")
        js = APP_JS.read_text(encoding="utf-8")
        css = STYLES_CSS.read_text(encoding="utf-8")
        assert "下载结果" in js or "下载结果" in html
        for text in (html, js, css):
            assert "下载 ZIP" not in text

    def test_app_js_uses_uploads_field(self) -> None:
        js = APP_JS.read_text(encoding="utf-8")
        assert "uploads" in js
        assert "uploads:" in js  # the submit payload always carries the array
        assert "job_type" in js

    def test_app_js_reads_all_selected_files(self) -> None:
        # The UI must never upload only the first selected file, and it must
        # define the multi-file helpers (per the Step 4 design spec).
        js = APP_JS.read_text(encoding="utf-8")
        assert "files[0]" not in js
        for helper in (
            "formatBytes",
            "getSelectedFiles",
            "validateFiles",
            "readFileAsDataUrl",
            "readUploads",
            "renderFileList",
            "clearFiles",
        ):
            assert helper in js, f"app.js missing helper {helper}"


# ---------------------------------------------------------------------------
# Step 4.1: recent-jobs recovery, model memory, connection test (static)
# ---------------------------------------------------------------------------


class TestStep41Frontend:
    def test_app_js_loads_recent_jobs_on_start(self) -> None:
        # On page load the UI must call GET /api/jobs and read the returned
        # "jobs" array to recover jobs still in the current server process.
        js = APP_JS.read_text(encoding="utf-8")
        assert "/api/jobs" in js
        assert "data.jobs" in js

    def test_page_has_recent_jobs_area(self) -> None:
        html = INDEX_HTML.read_text(encoding="utf-8")
        assert 'id="job-list"' in html
        assert "最近任务" in html

    def test_app_js_tracks_jobs_without_a_single_current_id(self) -> None:
        # A single "currentJobId" that setMode() nulls out is exactly what
        # cancelled polling on mode switch. The redesigned controller must
        # track jobs independently of the active mode.
        js = APP_JS.read_text(encoding="utf-8")
        assert "currentJobId" not in js
        assert "jobs" in js
        assert "pollJob" in js

    def test_app_js_has_model_memory_and_connection_test_hooks(self) -> None:
        js = APP_JS.read_text(encoding="utf-8")
        html = INDEX_HTML.read_text(encoding="utf-8")
        assert "/api/ai/models" in js
        assert "/api/ai/connection-test" in js
        assert 'data-model-save' in html
        assert 'data-model-delete' in html
        assert 'data-connection-test' in html
        assert "保存模型" in html
        assert "删除模型" in html
        assert "测试连接" in html

    def test_connection_test_warns_about_real_request(self) -> None:
        # The UI must say plainly that the test fires a real network request
        # which may incur cost, before the user clicks it.
        html = INDEX_HTML.read_text(encoding="utf-8")
        assert "真实网络请求" in html
        assert "费用" in html


# ---------------------------------------------------------------------------
# Step 4.3: service restart recovery (static)
# ---------------------------------------------------------------------------


class TestStep43Frontend:
    def test_app_js_has_interrupted_status(self) -> None:
        js = APP_JS.read_text(encoding="utf-8")
        assert "interrupted" in js
        assert "已中断" in js

    def test_app_js_has_resume_job_function(self) -> None:
        js = APP_JS.read_text(encoding="utf-8")
        assert "resumeJob" in js
        assert "/resume" in js

    def test_app_js_has_continue_button_text(self) -> None:
        js = APP_JS.read_text(encoding="utf-8")
        assert "继续处理" in js

    def test_app_js_renders_resume_button_for_interrupted(self) -> None:
        js = APP_JS.read_text(encoding="utf-8")
        assert 'job.status === "interrupted"' in js


# ---------------------------------------------------------------------------
# Step 4.2: AI chunk progress rendering (static)
# ---------------------------------------------------------------------------


class TestStep42Frontend:
    def test_app_js_renders_ai_chunk_progress(self) -> None:
        js = APP_JS.read_text(encoding="utf-8")
        assert "AI 校对中" in js
        assert "data.total" in js
        assert "data.current" in js
        # Only ai-clean jobs (total > 0 while running) should render chunk
        # progress; other job types must not be touched.
        assert "job_type" in js
        assert "ai-clean" in js


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
        # Legacy single 'upload' field must keep working through the server.
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
        assert headers["Content-Type"] != "application/zip"
        assert 'filename="doc.md"' in headers["Content-Disposition"]

        extracted = data.decode("utf-8")
        assert extracted == "# Doc\n\nAlpha.\n\nBeta.\n"
        assert extracted.count("Alpha.") == 1  # dedupe actually ran

    def test_clean_flow_uploads_single_file_poll_download(self, server) -> None:
        # The redesigned UI always submits through the 'uploads' array, even
        # for a single file; the server must accept it and stream one .md.
        content = "# Doc\n\nAlpha.\n\nAlpha.\n\nBeta.\n"
        payload = {
            "job_type": "clean",
            "params": {},
            "uploads": [{"filename": "doc.md", "data_b64": _b64(content)}],
        }
        body = json.dumps(payload).encode("utf-8")
        status, _, data = _request(
            server.port,
            "POST",
            "/api/jobs",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        assert status == 202
        job_id = json.loads(data.decode("utf-8"))["job_id"]

        final = _wait_terminal(server.port, job_id)
        assert final["status"] == "succeeded"

        status, headers, data = _request(server.port, "GET", f"/api/jobs/{job_id}/download")
        assert status == 200
        assert headers["Content-Type"] != "application/zip"
        assert 'filename="doc.md"' in headers["Content-Disposition"]

        extracted = data.decode("utf-8")
        assert extracted.count("Alpha.") == 1  # dedupe actually ran


# ---------------------------------------------------------------------------
# Step 4.4: retry / delete controls (static)
# ---------------------------------------------------------------------------


class TestStep44Frontend:
    def test_app_js_has_retry_function_and_endpoint(self) -> None:
        js = APP_JS.read_text(encoding="utf-8")
        assert "retryJob" in js
        assert "/retry" in js
        assert 'method: "POST"' in js

    def test_app_js_has_delete_function_and_endpoint(self) -> None:
        js = APP_JS.read_text(encoding="utf-8")
        assert "deleteJob" in js
        assert 'method: "DELETE"' in js

    def test_app_js_retry_copy_distinguishes_failed_state(self) -> None:
        js = APP_JS.read_text(encoding="utf-8")
        assert "重试" in js
        # failed ai-clean shows the retry control; interrupted shows continue.
        assert 'job.status === "failed"' in js
        assert 'job.status === "interrupted"' in js

    def test_app_js_renders_delete_for_terminal_jobs(self) -> None:
        js = APP_JS.read_text(encoding="utf-8")
        assert "删除" in js
        # Terminal jobs (succeeded/failed/interrupted) all get a delete button.
        assert 'job.status === "succeeded"' in js
        assert "terminal" in js
        assert "deleteJob" in js

    def test_app_js_disables_actions_while_pending(self) -> None:
        js = APP_JS.read_text(encoding="utf-8")
        # A row action guard prevents duplicate submits.
        assert "pendingActions" in js
        assert "btn.disabled = true" in js
        assert "处理中…" in js

    def test_app_js_never_leaks_durable_internals(self) -> None:
        js = APP_JS.read_text(encoding="utf-8")
        for token in (".formatconverter-jobs", "manifest", "checkpoint", "output_paths"):
            assert token not in js, f"app.js leaks internal token {token!r}"
