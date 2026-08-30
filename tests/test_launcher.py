"""Offline tests for the Step 5 launcher layer in format_converter.web_server.

Everything runs against real localhost sockets using only the standard
library. The default browser is never opened: ``webbrowser.open`` is mocked in
every test that would trigger it, and the loopback-URL assertion below is the
real guarantee that we never hand the browser a public/non-loopback address.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import threading
from pathlib import Path

import pytest

import format_converter.web_server as ws

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _request(port: int, method: str, path: str) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        conn.request(method, path)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def _health_json(port: int) -> dict:
    status, data = _request(port, "GET", "/health")
    assert status == 200
    return json.loads(data.decode("utf-8"))


def _mock_browser(monkeypatch) -> list[str]:
    opened: list[str] = []
    monkeypatch.setattr(ws.webbrowser, "open", opened.append)
    return opened


def _blocking_socket(port: int) -> socket.socket:
    """Bind+listen on ``port`` without accepting, simulating another program."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    sock.bind(("127.0.0.1", port))
    sock.listen(1)
    return sock


# ---------------------------------------------------------------------------
# normal startup + browser
# ---------------------------------------------------------------------------


class TestNormalStartup:
    def test_run_server_serves_and_opens_browser(self, monkeypatch) -> None:
        opened = _mock_browser(monkeypatch)
        server = ws.run_server(preferred_port=0)  # 0 = random free port
        assert server is not None
        try:
            port = server.port
            assert port is not None and port > 0
            assert _health_json(port) == {"status": "ok"}
        finally:
            server.shutdown()

        assert len(opened) == 1
        url = opened[0]
        assert url.startswith("http://127.0.0.1:")
        # Never a public address, never a wildcard, never non-loopback.
        assert "0.0.0.0" not in url
        assert ":0/" not in url
        assert not url.startswith(("http://0.0.0.0", "http://::", "http://localhost"))

    def test_run_server_no_browser(self, monkeypatch) -> None:
        opened = _mock_browser(monkeypatch)
        server = ws.run_server(preferred_port=0, open_browser=False)
        assert server is not None
        try:
            assert server.port is not None
            assert _health_json(server.port) == {"status": "ok"}
        finally:
            server.shutdown()
        assert opened == []

    def test_preferred_port_free_uses_it(self, monkeypatch) -> None:
        _mock_browser(monkeypatch)
        # Find a free port, then ask for it explicitly.
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
        probe.close()
        server = ws.run_server(preferred_port=free_port)
        try:
            assert server is not None
            assert server.port == free_port
        finally:
            server.shutdown()


# ---------------------------------------------------------------------------
# port reuse / fallback
# ---------------------------------------------------------------------------


class TestPortStrategy:
    def test_reuse_existing_instance(self, monkeypatch) -> None:
        existing = ws.create_server(port=0)
        try:
            port = existing.port
            assert port is not None

            # run_server must NOT construct a second server.
            init_calls: list[int] = []
            original_init = ws.JobWebServer.__init__

            def counting_init(self, *args, **kwargs) -> None:
                init_calls.append(1)
                original_init(self, *args, **kwargs)

            monkeypatch.setattr(ws.JobWebServer, "__init__", counting_init)
            opened = _mock_browser(monkeypatch)

            result = ws.run_server(preferred_port=port, open_browser=True)
            assert result is None  # reuse marker
            assert init_calls == []  # no new server was created
            assert opened == [f"http://127.0.0.1:{port}/"]  # browser → existing
            assert _health_json(port) == {"status": "ok"}  # still served once
        finally:
            existing.shutdown()

    def test_reuse_without_browser_does_not_open(self, monkeypatch) -> None:
        existing = ws.create_server(port=0)
        try:
            port = existing.port
            assert port is not None
            opened = _mock_browser(monkeypatch)
            result = ws.run_server(preferred_port=port, open_browser=False)
            assert result is None
            assert opened == []
        finally:
            existing.shutdown()

    def test_other_program_occupies_port_uses_backup(self, monkeypatch) -> None:
        blocker = _blocking_socket(0)
        try:
            preferred = blocker.getsockname()[1]
            opened = _mock_browser(monkeypatch)
            server = ws.run_server(preferred_port=preferred, max_backup_ports=2)
            assert server is not None
            try:
                actual = server.port
                assert actual is not None
                # It must NOT have stolen the occupied port; it moved to a backup.
                assert actual != preferred
                assert preferred < actual <= preferred + 2
                assert _health_json(actual) == {"status": "ok"}
                assert opened == [f"http://127.0.0.1:{actual}/"]
            finally:
                server.shutdown()
        finally:
            blocker.close()

    def test_all_ports_occupied_raises_clear_error(self) -> None:
        # Occupy preferred..preferred+2 with non-accepting listeners.
        blockers: list[socket.socket] = []
        try:
            first = _blocking_socket(0)
            blockers.append(first)
            preferred = first.getsockname()[1]
            for p in (preferred + 1, preferred + 2):
                try:
                    blockers.append(_blocking_socket(p))
                except OSError:
                    pass  # already occupied by an unrelated process: equally fine
            with pytest.raises(ws.ServerStartError) as excinfo:
                ws.run_server(preferred_port=preferred, max_backup_ports=2)
            message = str(excinfo.value)
            assert "被占用" in message
            assert str(preferred) in message
        finally:
            for sock in blockers:
                sock.close()

    def test_max_backup_ports_negative_rejected(self) -> None:
        with pytest.raises(ValueError):
            ws.run_server(preferred_port=8765, max_backup_ports=-1)


# ---------------------------------------------------------------------------
# Ctrl+C clean exit + main() exit codes
# ---------------------------------------------------------------------------


class TestMain:
    def test_main_success_returns_0(self, monkeypatch) -> None:
        monkeypatch.setattr(ws, "_wait_until_interrupted", lambda: None)
        assert ws.main(["--port", "0", "--no-browser"]) == 0

    def test_main_error_returns_nonzero(self, monkeypatch, capsys) -> None:
        def _boom(*args, **kwargs):
            raise ws.ServerStartError("端口全被占用")

        monkeypatch.setattr(ws, "run_server", _boom)
        assert ws.main(["--port", "1", "--max-backup-ports", "0"]) == 1
        captured = capsys.readouterr()
        assert "错误" in captured.err

    def test_main_ctrl_c_shuts_down_cleanly(self, monkeypatch) -> None:
        real = ws.create_server(port=0)
        try:
            shutdown_calls: list[int] = []
            original_shutdown = real.shutdown

            def record_shutdown() -> None:
                original_shutdown()
                shutdown_calls.append(1)

            real.shutdown = record_shutdown  # type: ignore[method-assign]

            monkeypatch.setattr(ws, "run_server", lambda *a, **k: real)

            def raise_interrupt() -> None:
                raise KeyboardInterrupt()

            monkeypatch.setattr(ws, "_wait_until_interrupted", raise_interrupt)

            code = ws.main(["--port", str(real.port), "--no-browser"])
            assert code == 0
            assert shutdown_calls == [1]
            assert real.base_temp_dir is None  # temporary root was deleted
            # The serve thread was joined; nothing leaks.
            assert not any(
                t.name == "format-converter-web" and t.is_alive()
                for t in threading.enumerate()
            )
        finally:
            real.shutdown()  # idempotent, safe even after the wrapped call


# ---------------------------------------------------------------------------
# helper sanity
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_health_ok_true_for_own_server(self) -> None:
        server = ws.create_server(port=0)
        try:
            assert ws._health_ok(server.port) is True  # type: ignore[attr-defined]
        finally:
            server.shutdown()

    def test_health_ok_false_for_closed_port(self) -> None:
        # A port with no listener at all must not look like our service.
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        assert ws._health_ok(port) is False  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# BAT smoke test (Windows only)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="BAT smoke test requires Windows cmd.exe")
class TestBatSmoke:
    def test_deps_missing_branch_prints_install_commands(self, tmp_path) -> None:
        bat = ROOT / "启动图形界面.bat"
        if not bat.is_file():
            pytest.skip("BAT file is not present")

        # cmd.exe cannot reliably decode the non-ASCII batch filename from an
        # argv when spawned programmatically, and 8.3 short names may be
        # disabled (NtfsDisable8dot3NameCreation). Copying the exact BAT bytes
        # to an ASCII-named temp file is robust on every Windows setup.
        copied = tmp_path / "fc_launcher_smoke.bat"
        shutil.copy2(bat, copied)

        env = dict(os.environ)
        # Test hook: force a Python that will never import the package, so the
        # BAT must take its "core dependency missing" branch and exit non-zero.
        env["FC_TEST_PYTHON"] = r"C:\nonexistent_fc_test\python.exe"
        env["FC_TEST_NO_PAUSE"] = "1"  # never block on `pause` during the test

        proc = subprocess.run(
            ["cmd.exe", "/c", "call", str(copied)],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            timeout=60,
        )
        assert proc.returncode != 0
        text = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
        # The copyable install commands must be printed.
        assert "python -m venv .venv" in text
        assert "pip install -r requirements.txt" in text
        assert "Python 3 or the project dependencies are not ready" in text
