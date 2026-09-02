"""Tests for the unified job/task service layer (format_converter.jobs).

All tests are offline: they use tmp_path fixtures, injected fake clients, and
monkeypatched conversion functions (pymupdf4llm is not installed in the venv,
so the PDF path is faked while still exercising the real handler logic).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from format_converter.ai_jobs import AIJobStore
from format_converter.jobs import JobManager, JobStatus, UnknownJobTypeError


class EchoClient:
    """Injected fake LLM client mirroring tests/test_cli.py's EchoClient."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.calls: list[dict] = []

    def complete(self, *, system: str, user: str, model: str) -> str:
        self.calls.append({"system": system, "user": user, "model": model})
        return f"[revised] {user}"


class _BlockingChunkClient:
    """Fake client that blocks on the second chunk to expose running progress."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.blocked = threading.Event()
        self.release = threading.Event()

    def complete(self, *, system: str, user: str, model: str) -> str:
        index = len(self.calls)
        self.calls.append(user)
        if index == 1:
            self.blocked.set()
            assert self.release.wait(5), "release event not set"
        return f"[revised:{index}] {user}"


def _fake_convert_file(pdf_path: Path, output_dir: Path, overwrite: bool = False) -> Path:
    """Stand-in for convert_pdf_file that writes a fake Markdown output."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out = output_dir / f"{Path(pdf_path).stem}.md"
    out.write_text(f"# {Path(pdf_path).stem}\n\nFake conversion output.", encoding="utf-8")
    return out


def _fake_convert_directory(
    input_dir: Path, output_dir: Path, overwrite: bool = False
) -> list[Path]:
    """Stand-in for convert_pdf_directory."""
    input_dir = Path(input_dir)
    return [
        _fake_convert_file(p, output_dir, overwrite=overwrite)
        for p in sorted(input_dir.glob("*.pdf"))
    ]


def _write_pdf(path: Path, text: str = "fake pdf") -> Path:
    path.write_bytes(f"%PDF-1.4 {text}".encode("utf-8"))
    return path


def _write_md(path: Path, content: str = "# Hello\n\nBody.\n\nBody.") -> Path:
    path.write_text(content, encoding="utf-8")
    return path


class TestSuccess:
    def test_convert_directory_success(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("format_converter.jobs.convert_pdf_directory", _fake_convert_directory)
        pdf_dir = tmp_path / "pdfs"
        pdf_dir.mkdir()
        _write_pdf(pdf_dir / "a.pdf")
        _write_pdf(pdf_dir / "b.pdf")
        out_dir = tmp_path / "out"

        manager = JobManager()
        job_id = manager.submit("convert", {"input_dir": pdf_dir, "output_dir": out_dir})
        result = manager.wait(job_id, timeout=10)

        assert result is not None
        assert result.status is JobStatus.succeeded
        assert result.message == "Converted 2 PDF file(s)."
        assert len(result.output_paths) == 2
        assert (out_dir / "a.md").is_file()
        assert (out_dir / "b.md").is_file()
        assert result.output_paths == (out_dir / "a.md", out_dir / "b.md")

    def test_convert_single_file_success(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("format_converter.jobs.convert_pdf_file", _fake_convert_file)
        pdf = _write_pdf(tmp_path / "doc.pdf")
        out_dir = tmp_path / "out"

        manager = JobManager()
        job_id = manager.submit("convert", {"file": pdf, "output_dir": out_dir})
        result = manager.wait(job_id, timeout=10)

        assert result is not None
        assert result.status is JobStatus.succeeded
        assert "Converted 1 PDF file" in result.message
        assert result.output_paths == (out_dir / "doc.md",)
        assert (out_dir / "doc.md").is_file()

    def test_clean_single_file_success(self, tmp_path: Path) -> None:
        md = _write_md(tmp_path / "doc.md")  # duplicate "Body." paragraph
        manager = JobManager()
        job_id = manager.submit("clean", {"file": md})
        result = manager.wait(job_id, timeout=10)

        assert result is not None
        assert result.status is JobStatus.succeeded
        assert "Cleaned 1 Markdown file" in result.message
        assert result.output_paths == (md.resolve(),)
        assert md.read_text(encoding="utf-8").count("Body.") == 1  # dedupe ran
        assert (tmp_path / "doc.bak.md").exists()  # backup ran

    def test_clean_directory_success(self, tmp_path: Path) -> None:
        md_dir = tmp_path / "mds"
        md_dir.mkdir()
        _write_md(md_dir / "a.md")
        _write_md(md_dir / "b.md")

        manager = JobManager()
        job_id = manager.submit("clean", {"input_dir": md_dir})
        result = manager.wait(job_id, timeout=10)

        assert result is not None
        assert result.status is JobStatus.succeeded
        assert result.message == "Cleaned 2 Markdown file(s)."
        assert len(result.output_paths) == 2
        assert (md_dir / "a.md").is_file()
        assert (md_dir / "b.md").is_file()

    def test_pipeline_success(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # run_pipeline looks up convert_pdf_directory inside format_converter.pipeline.
        monkeypatch.setattr("format_converter.pipeline.convert_pdf_directory", _fake_convert_directory)
        pdf_dir = tmp_path / "pdfs"
        pdf_dir.mkdir()
        _write_pdf(pdf_dir / "doc.pdf")
        md_dir = tmp_path / "mds"

        manager = JobManager()
        job_id = manager.submit("pipeline", {"pdf_dir": pdf_dir, "md_dir": md_dir})
        result = manager.wait(job_id, timeout=10)

        assert result is not None
        assert result.status is JobStatus.succeeded
        assert result.message == "Converted 1 PDF(s), cleaned 1 Markdown file(s)."
        assert (md_dir / "doc.md").is_file()
        # converted + cleaned paths are both collected (the same file appears twice)
        assert len(result.output_paths) == 2


class TestFailure:
    def test_failed_job_records_error_and_thread_survives(self, tmp_path: Path) -> None:
        manager = JobManager()
        job_id = manager.submit("convert", {"file": tmp_path / "missing.pdf", "output_dir": tmp_path})
        result = manager.wait(job_id, timeout=10)

        assert result is not None
        assert result.status is JobStatus.failed
        assert result.message  # non-empty
        assert "PDF file not found" in result.message
        assert result.output_paths == ()
        # get() returns the same JobResult stored for this job.
        assert manager.get(job_id) == result


class TestValidation:
    def test_unknown_job_type_raises(self) -> None:
        manager = JobManager()
        with pytest.raises(UnknownJobTypeError) as excinfo:
            manager.submit("nope", {})
        assert "nope" in str(excinfo.value)
        assert "convert" in str(excinfo.value)  # lists known types

    def test_unknown_job_id_returns_none(self) -> None:
        manager = JobManager()
        assert manager.get("does-not-exist") is None
        assert manager.wait("does-not-exist", timeout=0.05) is None

    def test_submit_rejects_non_dict_params(self) -> None:
        manager = JobManager()
        with pytest.raises(TypeError):
            manager.submit("convert", ["not", "a", "dict"])


class TestAIClean:
    def test_missing_key_fails_without_leaking_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
        src = _write_md(tmp_path / "doc.md", content="Alpha.\n\nBeta.")

        manager = JobManager()
        job_id = manager.submit(
            "ai-clean", {"file": src, "provider": "orcarouter", "model": "m1"}
        )
        result = manager.wait(job_id, timeout=10)

        assert result is not None
        assert result.status is JobStatus.failed
        # The variable name is fine to report; the key value is never present.
        assert "ORCAROUTER_API_KEY" in result.message
        assert "sk-" not in result.message

    def test_success_offline_with_injected_fake_client(self, tmp_path: Path) -> None:
        src = _write_md(tmp_path / "doc.md", content="Alpha.\n\nBeta.")

        manager = JobManager()
        job_id = manager.submit(
            "ai-clean",
            {"file": src, "provider": "orcarouter", "model": "m1", "client": EchoClient()},
        )
        result = manager.wait(job_id, timeout=10)

        assert result is not None
        assert result.status is JobStatus.succeeded
        out = src.with_suffix(".ai.md")
        assert out.is_file()
        assert out.read_text(encoding="utf-8") == "[revised] Alpha.\n\nBeta."
        assert result.output_paths == (out.resolve(),)
        assert "AI proofread" in result.message

    def test_directory_batch_success_offline(self, tmp_path: Path) -> None:
        # Web batch mode: input_dir + output_dir proofreads every .md, one
        # output per file. Uses an injected client so no real key is needed.
        md_dir = tmp_path / "mds"
        md_dir.mkdir()
        _write_md(md_dir / "a.md", content="Alpha.\n\nBeta.")
        _write_md(md_dir / "b.md", content="Gamma.\n\nDelta.")
        out_dir = tmp_path / "out"

        manager = JobManager()
        job_id = manager.submit(
            "ai-clean",
            {"input_dir": md_dir, "output_dir": out_dir,
             "provider": "orcarouter", "model": "m1", "client": EchoClient()},
        )
        result = manager.wait(job_id, timeout=10)

        assert result is not None
        assert result.status is JobStatus.succeeded
        assert result.message == "AI proofread 2 Markdown file(s)."
        assert len(result.output_paths) == 2
        assert (out_dir / "a.ai.md").is_file()
        assert (out_dir / "b.ai.md").is_file()
        assert (out_dir / "a.ai.md").read_text(encoding="utf-8") == "[revised] Alpha.\n\nBeta."
        assert (out_dir / "b.ai.md").read_text(encoding="utf-8") == "[revised] Gamma.\n\nDelta."


class TestProgress:
    def test_ai_job_updates_progress_while_running(self, tmp_path: Path) -> None:
        # Three paragraphs that always split into exactly two chunks (6000-char
        # paragraphs are far below the 12000 limit, so CRLF/LF translation on
        # Windows cannot push a block over the limit).
        content = ("A" * 6000) + "\n\n" + ("B" * 6000) + "\n\n" + "C"
        src = _write_md(tmp_path / "doc.md", content=content)
        client = _BlockingChunkClient()

        manager = JobManager()
        job_id = manager.submit(
            "ai-clean",
            {"file": src, "provider": "orcarouter", "model": "m1", "client": client},
        )
        assert client.blocked.wait(5), "second chunk never started"

        running = manager.get(job_id)
        assert running is not None
        assert running.status is JobStatus.running
        assert running.current == 1
        assert running.total == 2
        assert running.message == "AI 校对中 · 1 / 2"

        client.release.set()
        final = manager.wait(job_id, timeout=10)
        assert final is not None
        assert final.status is JobStatus.succeeded
        assert final.current == 2
        assert final.total == 2

    def test_success_preserves_final_progress(self, tmp_path: Path) -> None:
        src = _write_md(tmp_path / "doc.md", content="Alpha.\n\nBeta.")
        manager = JobManager()
        job_id = manager.submit(
            "ai-clean",
            {"file": src, "provider": "orcarouter", "model": "m1", "client": EchoClient()},
        )
        result = manager.wait(job_id, timeout=10)

        assert result is not None
        assert result.status is JobStatus.succeeded
        assert result.current == 1
        assert result.total == 1

    def test_non_ai_job_has_no_progress(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("format_converter.jobs.convert_pdf_file", _fake_convert_file)
        pdf = _write_pdf(tmp_path / "doc.pdf")
        out_dir = tmp_path / "out"

        manager = JobManager()
        job_id = manager.submit("convert", {"file": pdf, "output_dir": out_dir})
        result = manager.wait(job_id, timeout=10)

        assert result is not None
        assert result.status is JobStatus.succeeded
        assert result.current == 0
        assert result.total == 0
        assert "AI 校对中" not in result.message


class TestSanitization:
    def test_failed_message_masks_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-secret-xyz")
        manager = JobManager()

        def _leaky(_params: dict) -> tuple[tuple[Path, ...], str]:
            raise RuntimeError("boom sk-secret-xyz leaked")

        manager.handlers["leaky"] = _leaky
        job_id = manager.submit("leaky", {})
        result = manager.wait(job_id, timeout=10)

        assert result is not None
        assert result.status is JobStatus.failed
        assert "sk-secret-xyz" not in result.message
        assert "***" in result.message
        assert "boom" in result.message


class TestDurableAIJob:
    def test_ai_clean_with_durable_creates_job_directory(self, tmp_path: Path) -> None:
        src = _write_md(tmp_path / "doc.md", content="Alpha.\n\nBeta.")
        manager = JobManager()
        job_id = manager.submit(
            "ai-clean",
            {
                "file": src,
                "provider": "orcarouter",
                "model": "m1",
                "client": EchoClient(),
                "_durable": True,
                "_ai_job_root": tmp_path,
            },
        )
        result = manager.wait(job_id, timeout=10)
        assert result is not None
        assert result.status is JobStatus.succeeded
        # Durable job directory should exist under the root.
        dirs = [d for d in tmp_path.iterdir() if d.is_dir() and len(d.name) == 32]
        assert len(dirs) == 1
        # The output should still be at the expected path
        out = src.with_suffix(".ai.md")
        assert out.is_file()
        assert result.output_paths == (out.resolve(),)
        assert "AI proofread" in result.message

    def test_ai_clean_durable_result_files_on_disk_after_chunk_success(self, tmp_path: Path) -> None:
        src = _write_md(tmp_path / "doc.md", content="Alpha.\n\nBeta.")
        manager = JobManager()
        job_id = manager.submit(
            "ai-clean",
            {
                "file": src,
                "provider": "orcarouter",
                "model": "m1",
                "client": EchoClient(),
                "_durable": True,
                "_ai_job_root": tmp_path,
            },
        )
        result = manager.wait(job_id, timeout=10)
        assert result is not None
        assert result.status is JobStatus.succeeded
        # Find the durable job directory (there should be exactly one).
        dirs = [d for d in tmp_path.iterdir() if d.is_dir() and len(d.name) == 32]
        assert len(dirs) == 1
        job_dir = dirs[0]
        results_dir = job_dir / "results"
        assert (results_dir / "0001.md").is_file()
        assert (results_dir / "0001.md").read_text(encoding="utf-8") == "[revised] Alpha.\n\nBeta."
        assert (job_dir / "final.md").is_file()
        assert (job_dir / "manifest.json").is_file()

    def test_ai_clean_durable_mid_failure_keeps_completed_chunks(self, tmp_path: Path) -> None:
        class _HalfFailClient:
            def __init__(self) -> None:
                self.calls = 0

            def complete(self, *, system: str, user: str, model: str) -> str:
                self.calls += 1
                if self.calls >= 2:
                    raise RuntimeError("mid-way failure")
                return f"[revised] {user}"

        # Each paragraph is ~7000 chars, so with 3 paragraphs at 12000 limit,
        # chunks 1 and 2 fit together, chunk 3 is alone -> 2 chunks.
        para_a = "A." + "x" * 7000
        para_b = "B." + "y" * 7000
        para_c = "C." + "z" * 7000
        content = para_a + "\n\n" + para_b + "\n\n" + para_c
        src = _write_md(tmp_path / "doc.md", content=content)
        manager = JobManager()
        job_id = manager.submit(
            "ai-clean",
            {
                "file": src,
                "provider": "orcarouter",
                "model": "m1",
                "client": _HalfFailClient(),
                "_durable": True,
                "_ai_job_root": tmp_path,
            },
        )
        result = manager.wait(job_id, timeout=10)
        assert result is not None
        assert result.status is JobStatus.failed
        # Completed chunk 1 result should remain on disk.
        dirs = [d for d in tmp_path.iterdir() if d.is_dir() and len(d.name) == 32]
        assert len(dirs) == 1
        job_dir = dirs[0]
        results_dir = job_dir / "results"
        assert (results_dir / "0001.md").is_file()
        expected = "[revised] " + para_a + "\n"
        actual = (results_dir / "0001.md").read_text(encoding="utf-8")
        assert actual == expected
        # Chunk 2 result should NOT exist (it failed before save_result).
        assert not (results_dir / "0002.md").is_file()
        assert not (results_dir / "0003.md").is_file()
        assert not (job_dir / "final.md").is_file()
        # Failed job should not return output_paths.
        assert result.output_paths == ()
        # The durable job directory should still exist.
        assert job_dir.is_dir()
        # Durable manifest status should be "failed".
        import json
        manifest_path = job_dir / "manifest.json"
        assert manifest_path.is_file()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "failed"
        # Chunk 1 result file should still be on disk.
        assert (results_dir / "0001.md").is_file()
        # Chunk 2 result should NOT exist (it failed before save_result).
        assert not (results_dir / "0002.md").is_file()
        assert not (results_dir / "0003.md").is_file()
        assert not (job_dir / "final.md").is_file()
        # Failed job should not return output_paths.
        assert result.output_paths == ()
        # Status/list should not leak API key or secret.
        assert "sk-" not in result.message
        assert "secret" not in result.message.lower()

    def test_durable_manifest_web_job_id_matches(self, tmp_path: Path) -> None:
        src = _write_md(tmp_path / "doc.md", content="Alpha.\n\nBeta.")
        manager = JobManager()
        job_id = manager.submit(
            "ai-clean",
            {
                "file": src,
                "provider": "orcarouter",
                "model": "m1",
                "client": EchoClient(),
                "_durable": True,
                "_ai_job_root": tmp_path,
            },
        )
        result = manager.wait(job_id, timeout=10)
        assert result is not None
        assert result.status is JobStatus.succeeded
        dirs = [d for d in tmp_path.iterdir() if d.is_dir() and len(d.name) == 32]
        assert len(dirs) == 1
        job_dir = dirs[0]
        import json
        manifest = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["web_job_id"] == job_id


class TestListRecent:
    def test_list_recent_includes_metadata(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("format_converter.jobs.convert_pdf_file", _fake_convert_file)
        pdf = _write_pdf(tmp_path / "doc.pdf")
        out_dir = tmp_path / "out"

        manager = JobManager()
        job_id = manager.submit("convert", {"file": pdf, "output_dir": out_dir})
        manager.wait(job_id, timeout=10)

        recent = manager.list_recent()
        assert isinstance(recent, list)
        assert any(j["job_id"] == job_id for j in recent)
        entry = next(j for j in recent if j["job_id"] == job_id)
        assert entry["job_type"] == "convert"
        assert entry["status"] == "succeeded"
        assert isinstance(entry["created_at"], float) and entry["created_at"] > 0
        assert isinstance(entry["updated_at"], float) and entry["updated_at"] > 0
        assert entry["updated_at"] >= entry["created_at"]
        assert isinstance(entry["message"], str) and entry["message"]
        # list_recent must never leak output paths to the caller.
        assert "output_paths" not in entry
        assert "output_paths" not in str(entry)

    def test_list_recent_updated_at_changes_on_transition(self) -> None:
        manager = JobManager()
        entered = threading.Event()
        release = threading.Event()

        def _blocking(_params: dict) -> tuple[tuple[Path, ...], str]:
            entered.set()
            assert release.wait(5), "release event not set"
            return (Path("out.md"),), "blocked done"

        manager.handlers["blocking"] = _blocking
        job_id = manager.submit("blocking", {})
        assert entered.wait(5), "handler never started"
        assert manager.get(job_id).status is JobStatus.running

        running_updated = next(
            j for j in manager.list_recent() if j["job_id"] == job_id
        )["updated_at"]

        time.sleep(0.02)
        release.set()
        final = manager.wait(job_id, timeout=5)
        assert final is not None and final.status is JobStatus.succeeded

        final_updated = next(
            j for j in manager.list_recent() if j["job_id"] == job_id
        )["updated_at"]
        # A terminal transition rewrites the snapshot, so updated_at moves on.
        assert final_updated > running_updated

    def test_list_recent_honors_limit_and_order(self) -> None:
        manager = JobManager()
        for i in range(3):
            manager.handlers[f"h{i}"] = lambda _params: ((Path("x.md"),), "done")
            manager.submit(f"h{i}", {})
            manager.wait(f"h{i}", timeout=5)

        recent = manager.list_recent(limit=2)
        assert len(recent) == 2
        # Newest-updated first.
        assert recent[0]["updated_at"] >= recent[1]["updated_at"]

    def test_list_recent_negative_limit_rejected(self) -> None:
        manager = JobManager()
        with pytest.raises(ValueError):
            manager.list_recent(limit=-1)


class TestThreading:
    def test_state_transition_queued_running_succeeded(self) -> None:
        manager = JobManager()
        entered = threading.Event()
        release = threading.Event()

        def _blocking(_params: dict) -> tuple[tuple[Path, ...], str]:
            entered.set()
            assert release.wait(5), "release event not set"
            return (Path("out.md"),), "blocked handler done"

        manager.handlers["blocking"] = _blocking
        job_id = manager.submit("blocking", {})

        assert entered.wait(5), "handler never started"
        assert manager.get(job_id).status is JobStatus.running
        release.set()

        final = manager.wait(job_id, timeout=5)
        assert final is not None
        assert final.status is JobStatus.succeeded
        assert final.output_paths == (Path("out.md"),)

    def test_wait_times_out_then_completes(self) -> None:
        manager = JobManager()
        release = threading.Event()

        def _slow(_params: dict) -> tuple[tuple[Path, ...], str]:
            assert release.wait(5), "release event not set"
            return (Path("x.md"),), "slow done"

        manager.handlers["slow"] = _slow
        job_id = manager.submit("slow", {})

        with pytest.raises(TimeoutError):
            manager.wait(job_id, timeout=0.05)

        release.set()
        result = manager.wait(job_id, timeout=5)
        assert result is not None
        assert result.status is JobStatus.succeeded

    def test_negative_timeout_rejected(self) -> None:
        manager = JobManager()
        with pytest.raises(ValueError):
            manager.wait("some-job", timeout=-1)


class TestStartupHydration:
    def test_startup_hydrates_interrupted_durable_job(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        plain_store = store
        manifest = plain_store.create_job(
            tmp_path / "doc.md", "One.\n\nTwo.", "orcarouter", "m1", 6,
            web_job_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        plain_store.update_status(manifest.job_id, "running")
        # A new JobManager with the same AIJobStore should hydrate the interrupted job.
        manager = JobManager(ai_job_store=store)
        result = manager.get("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        assert result is not None
        assert result.status is JobStatus.interrupted
        assert result.job_type == "ai-clean"
        assert result.current == 0
        assert result.total == 2

    def test_startup_hydrates_completed_durable_job(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(
            tmp_path / "doc.md", "One.\n\nTwo.", "orcarouter", "m1", 6,
            web_job_id="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        )
        store.update_status(manifest.job_id, "completed")
        manager = JobManager(ai_job_store=store)
        result = manager.get("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
        assert result is not None
        assert result.status is JobStatus.succeeded
        assert result.message == "任务已完成"

    def test_startup_turns_stale_running_into_interrupted(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(
            tmp_path / "doc.md", "One.\n\nTwo.", "orcarouter", "m1", 6,
            web_job_id="cccccccccccccccccccccccccccccccc",
        )
        store.update_status(manifest.job_id, "running")
        # A new manager should mark running -> interrupted.
        manager = JobManager(ai_job_store=store)
        result = manager.get("cccccccccccccccccccccccccccccccc")
        assert result is not None
        assert result.status is JobStatus.interrupted


class TestResume:
    def test_resume_skips_completed_chunks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")

        class _FakeClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                self.calls: list[str] = []

            def complete(self, *, system: str, user: str, model: str) -> str:
                self.calls.append(user)
                return f"[revised] {user}"

        monkeypatch.setattr("format_converter.providers.get_api_key", lambda _cfg: "sk-fake")
        monkeypatch.setattr("format_converter.providers.get_provider", lambda _prov: None)
        monkeypatch.setattr("format_converter.llm_client.OpenAICompatClient", _FakeClient)

        manifest = store.create_job(
            tmp_path / "doc.md", "One.\n\nTwo.\n\nThree.", "orcarouter", "m1", 6,
            web_job_id="dddddddddddddddddddddddddddddddd",
            output_basename="doc.ai.md",
        )
        store.update_status(manifest.job_id, "running")
        store.save_result(manifest.job_id, 1, "R1")
        store.update_status(manifest.job_id, "interrupted")

        manager = JobManager(ai_job_store=store)
        manager._hydrate_ai_job_snapshots(base_dir=tmp_path)
        result = manager.get("dddddddddddddddddddddddddddddddd")
        assert result is not None
        assert result.status is JobStatus.interrupted

        durable_id = manager.resume_ai_job("dddddddddddddddddddddddddddddddd")
        assert durable_id is not None
        result = manager.wait("dddddddddddddddddddddddddddddddd", timeout=10)
        assert result is not None
        assert result.status is JobStatus.succeeded

    def test_resume_unknown_job_returns_none(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manager = JobManager(ai_job_store=store)
        assert manager.resume_ai_job("eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee") is None

    def test_resume_without_store_returns_none(self) -> None:
        manager = JobManager()
        assert manager.resume_ai_job("ffffffffffffffffffffffffffffffff") is None

    def test_resume_invalid_web_job_id_returns_none(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manager = JobManager(ai_job_store=store)
        assert manager.resume_ai_job("") is None
        assert manager.resume_ai_job("short") is None
        assert manager.resume_ai_job("gggggggggggggggggggggggggggggggg") is None

    def test_hydration_aggregates_multi_file_web_job_id(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        web_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        m1 = store.create_job(tmp_path / "a.md", "One.", "orcarouter", "m1", 100,
                              web_job_id=web_id, output_basename="a.ai.md")
        store.update_status(m1.job_id, "completed")
        m2 = store.create_job(tmp_path / "b.md", "Two.", "orcarouter", "m1", 100,
                              web_job_id=web_id, output_basename="b.ai.md")
        store.update_status(m2.job_id, "completed")
        manager = JobManager(ai_job_store=store)
        result = manager.get(web_id)
        assert result is not None
        assert result.job_type == "ai-clean"
        assert result.status is JobStatus.succeeded
        assert result.total == 2

    def test_resume_multi_file_aggregates_all_manifests(self, tmp_path: Path, monkeypatch) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")

        class _FakeClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                self.calls: list[str] = []

            def complete(self, *, system: str, user: str, model: str) -> str:
                self.calls.append(user)
                return f"[revised] {user}"

        monkeypatch.setattr("format_converter.providers.get_api_key", lambda _cfg: "sk-fake")
        monkeypatch.setattr("format_converter.providers.get_provider", lambda _prov: None)
        monkeypatch.setattr("format_converter.llm_client.OpenAICompatClient", _FakeClient)

        web_id = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        m1 = store.create_job(tmp_path / "a.md", "One.", "orcarouter", "m1", 100,
                              web_job_id=web_id, output_basename="a.ai.md")
        store.update_status(m1.job_id, "running")
        m2 = store.create_job(tmp_path / "b.md", "Two.", "orcarouter", "m1", 100,
                              web_job_id=web_id, output_basename="b.ai.md")
        store.update_status(m2.job_id, "running")
        store.mark_stale_running_interrupted()

        manager = JobManager(ai_job_store=store)
        manager._hydrate_ai_job_snapshots(base_dir=tmp_path)
        result = manager.get(web_id)
        assert result is not None
        assert result.status is JobStatus.interrupted
        assert len(result.output_paths) == 2

        durable_id = manager.resume_ai_job(web_id)
        assert durable_id is not None
        final = manager.wait(web_id, timeout=10)
        assert final is not None
        assert final.status is JobStatus.succeeded
        # Both output files must exist when wait() returns.
        out_a = tmp_path / web_id / "output" / "a.ai.md"
        out_b = tmp_path / web_id / "output" / "b.ai.md"
        assert out_a.is_file()
        assert out_b.is_file()
        # output_paths must preserve both paths.
        assert len(final.output_paths) == 2
        names = sorted(p.name for p in final.output_paths)
        assert names == ["a.ai.md", "b.ai.md"]
        # After a short settle, the job must still be succeeded (not flipped to failed).
        import time as _time
        _time.sleep(0.2)
        settled = manager.get(web_id)
        assert settled is not None
        assert settled.status is JobStatus.succeeded

    def test_resume_multi_file_progress_is_aggregate(self, tmp_path: Path, monkeypatch) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")

        class _FakeClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                self.calls: list[str] = []

            def complete(self, *, system: str, user: str, model: str) -> str:
                self.calls.append(user)
                return f"[revised] {user}"

        monkeypatch.setattr("format_converter.providers.get_api_key", lambda _cfg: "sk-fake")
        monkeypatch.setattr("format_converter.providers.get_provider", lambda _prov: None)
        monkeypatch.setattr("format_converter.llm_client.OpenAICompatClient", _FakeClient)

        web_id = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        # Each file splits into two chunks (max_chars=6 over "One.\n\nTwo.").
        m1 = store.create_job(tmp_path / "a.md", "One.\n\nTwo.", "orcarouter", "m1", 6,
                              web_job_id=web_id, output_basename="a.ai.md")
        assert m1.total_chunks == 2
        store.update_status(m1.job_id, "running")
        m2 = store.create_job(tmp_path / "b.md", "Cat.\n\nDog.", "orcarouter", "m1", 6,
                              web_job_id=web_id, output_basename="b.ai.md")
        assert m2.total_chunks == 2
        store.update_status(m2.job_id, "running")
        store.mark_stale_running_interrupted()

        manager = JobManager(ai_job_store=store)
        manager._hydrate_ai_job_snapshots(base_dir=tmp_path)
        result = manager.get(web_id)
        assert result is not None
        assert result.status is JobStatus.interrupted
        assert result.total == 4  # hydrated aggregate total

        durable_id = manager.resume_ai_job(web_id)
        assert durable_id is not None
        final = manager.wait(web_id, timeout=10)
        assert final is not None
        assert final.status is JobStatus.succeeded
        # Final progress must be the aggregate over both files: 4 / 4.
        assert final.current == 4
        assert final.total == 4

    def test_resume_single_file_progress_is_per_file(self, tmp_path: Path, monkeypatch) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")

        class _FakeClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                self.calls: list[str] = []

            def complete(self, *, system: str, user: str, model: str) -> str:
                self.calls.append(user)
                return f"[revised] {user}"

        monkeypatch.setattr("format_converter.providers.get_api_key", lambda _cfg: "sk-fake")
        monkeypatch.setattr("format_converter.providers.get_provider", lambda _prov: None)
        monkeypatch.setattr("format_converter.llm_client.OpenAICompatClient", _FakeClient)

        web_id = "ffffffffffffffffffffffffffffffff"
        manifest = store.create_job(tmp_path / "doc.md", "One.\n\nTwo.", "orcarouter", "m1", 6,
                                    web_job_id=web_id, output_basename="doc.ai.md")
        assert manifest.total_chunks == 2
        store.update_status(manifest.job_id, "running")
        store.mark_stale_running_interrupted()

        manager = JobManager(ai_job_store=store)
        manager._hydrate_ai_job_snapshots(base_dir=tmp_path)
        result = manager.get(web_id)
        assert result is not None
        assert result.status is JobStatus.interrupted

        durable_id = manager.resume_ai_job(web_id)
        assert durable_id is not None
        final = manager.wait(web_id, timeout=10)
        assert final is not None
        assert final.status is JobStatus.succeeded
        # Single-file resume reports that file's own chunk count: 2 / 2.
        assert final.current == 2
        assert final.total == 2

    def test_resume_multi_file_one_fails_job_fails(self, tmp_path: Path, monkeypatch) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")

        class _FailOnBClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                self.calls: list[str] = []

            def complete(self, *, system: str, user: str, model: str) -> str:
                self.calls.append(user)
                if "Two." in user:
                    raise RuntimeError("boom on second file")
                return f"[revised] {user}"

        monkeypatch.setattr("format_converter.providers.get_api_key", lambda _cfg: "sk-fake")
        monkeypatch.setattr("format_converter.providers.get_provider", lambda _prov: None)
        monkeypatch.setattr("format_converter.llm_client.OpenAICompatClient", _FailOnBClient)

        web_id = "dddddddddddddddddddddddddddddddd"
        m1 = store.create_job(tmp_path / "a.md", "One.", "orcarouter", "m1", 100,
                              web_job_id=web_id, output_basename="a.ai.md")
        store.update_status(m1.job_id, "running")
        m2 = store.create_job(tmp_path / "b.md", "Two.", "orcarouter", "m1", 100,
                              web_job_id=web_id, output_basename="b.ai.md")
        store.update_status(m2.job_id, "running")
        store.mark_stale_running_interrupted()

        manager = JobManager(ai_job_store=store)
        manager._hydrate_ai_job_snapshots(base_dir=tmp_path)
        result = manager.get(web_id)
        assert result is not None
        assert result.status is JobStatus.interrupted

        durable_id = manager.resume_ai_job(web_id)
        assert durable_id is not None
        final = manager.wait(web_id, timeout=10)
        assert final is not None
        assert final.status is JobStatus.failed
        # The failed durable job's manifest must be marked failed.
        assert store.load(m2.job_id).status == "failed"
        # wait() must not have first observed succeeded, then flipped to failed.
        # A short settle ensures no late success overwrite arrives.
        import time as _time
        _time.sleep(0.2)
        settled = manager.get(web_id)
        assert settled is not None
        assert settled.status is JobStatus.failed

    def test_resume_failure_message_sanitized(self, tmp_path: Path, monkeypatch) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")

        class _BoomClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def complete(self, *, system: str, user: str, model: str) -> str:
                raise RuntimeError("sk-secret-key leaked in resume error")

        monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-secret-key")
        monkeypatch.setattr("format_converter.providers.get_api_key", lambda _cfg: "sk-fake")
        monkeypatch.setattr("format_converter.providers.get_provider", lambda _prov: None)
        monkeypatch.setattr("format_converter.llm_client.OpenAICompatClient", _BoomClient)

        web_id = "cccccccccccccccccccccccccccccccc"
        manifest = store.create_job(tmp_path / "doc.md", "One.\n\nTwo.", "orcarouter", "m1", 6,
                                    web_job_id=web_id, output_basename="doc.ai.md")
        store.update_status(manifest.job_id, "running")
        store.mark_stale_running_interrupted()

        manager = JobManager(ai_job_store=store)
        manager._hydrate_ai_job_snapshots(base_dir=tmp_path)
        result = manager.get(web_id)
        assert result is not None
        assert result.status is JobStatus.interrupted

        durable_id = manager.resume_ai_job(web_id)
        assert durable_id is not None
        final = manager.wait(web_id, timeout=10)
        assert final is not None
        assert final.status is JobStatus.failed
        assert "sk-" not in final.message


class TestRetryAndDeleteManager:
    """Task 4: manager-level retry (from checkpoints) and durable delete."""

    def _patch_resume_client(self, monkeypatch, calls: list[str]) -> None:
        class _FakeClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def complete(self, *, system: str, user: str, model: str) -> str:
                calls.append(user)
                return f"[revised] {user}"

        monkeypatch.setattr("format_converter.providers.get_api_key", lambda _cfg: "sk-fake")
        monkeypatch.setattr("format_converter.providers.get_provider", lambda _prov: None)
        monkeypatch.setattr("format_converter.llm_client.OpenAICompatClient", _FakeClient)

    def test_retry_failed_job_skips_existing_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        web_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        manifest = store.create_job(
            tmp_path / "doc.md", "One.\n\nTwo.", "orcarouter", "m1", 6,
            web_job_id=web_id, output_basename="doc.ai.md",
        )
        assert manifest.total_chunks == 2
        store.save_result(manifest.job_id, 1, "R1")
        store.update_status(manifest.job_id, "failed")

        calls: list[str] = []
        self._patch_resume_client(monkeypatch, calls)
        manager = JobManager(ai_job_store=store)
        manager._hydrate_ai_job_snapshots(base_dir=tmp_path)
        result = manager.get(web_id)
        assert result is not None
        assert result.status is JobStatus.failed

        durable_id = manager.retry_ai_job(web_id)
        assert durable_id is not None
        final = manager.wait(web_id, timeout=10)
        assert final is not None
        assert final.status is JobStatus.succeeded
        # Only the missing chunk was requested — chunk 1 was skipped.
        assert len(calls) == 1
        assert calls[0] == "Two."  # chunk 2 body
        assert store.load(manifest.job_id).status == "completed"
        out = tmp_path / web_id / "output" / "doc.ai.md"
        assert out.is_file()

    def test_retry_multi_file_acts_on_all_shared_manifests(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        web_id = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        m1 = store.create_job(
            tmp_path / "a.md", "One.\n\nTwo.", "orcarouter", "m1", 6,
            web_job_id=web_id, output_basename="a.ai.md",
        )
        store.save_result(m1.job_id, 1, "R1a")
        store.update_status(m1.job_id, "failed")
        m2 = store.create_job(
            tmp_path / "b.md", "Cat.\n\nDog.", "orcarouter", "m1", 6,
            web_job_id=web_id, output_basename="b.ai.md",
        )
        store.save_result(m2.job_id, 1, "R1b")
        store.update_status(m2.job_id, "interrupted")
        # An unrelated completed durable job must survive.
        other = store.create_job(
            tmp_path / "c.md", "Keep.", "orcarouter", "m1", 100,
            web_job_id="ffffffffffffffffffffffffffffffff",
            output_basename="c.ai.md",
        )
        store.update_status(other.job_id, "completed")

        calls: list[str] = []
        self._patch_resume_client(monkeypatch, calls)
        manager = JobManager(ai_job_store=store)
        manager._hydrate_ai_job_snapshots(base_dir=tmp_path)

        durable_id = manager.retry_ai_job(web_id)
        assert durable_id is not None
        final = manager.wait(web_id, timeout=10)
        assert final is not None
        assert final.status is JobStatus.succeeded
        # Both shared manifests were continued; each requested only chunk 2.
        assert sorted(calls) == ["Dog.", "Two."]
        assert store.load(m1.job_id).status == "completed"
        assert store.load(m2.job_id).status == "completed"
        assert store.load(other.job_id).status == "completed"
        assert (tmp_path / web_id / "output" / "a.ai.md").is_file()
        assert (tmp_path / web_id / "output" / "b.ai.md").is_file()
        # Completed files from other web jobs are untouched.
        assert store.job_dir(other.job_id).is_dir()

    def test_retry_returns_none_for_completed_job(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        web_id = "cccccccccccccccccccccccccccccccc"
        manifest = store.create_job(
            tmp_path / "doc.md", "One.", "orcarouter", "m1", 100,
            web_job_id=web_id, output_basename="doc.ai.md",
        )
        store.save_result(manifest.job_id, 1, "R1")
        store.merge(manifest.job_id)
        calls: list[str] = []
        self._patch_resume_client(monkeypatch, calls)
        manager = JobManager(ai_job_store=store)
        manager._hydrate_ai_job_snapshots(base_dir=tmp_path)
        assert manager.retry_ai_job(web_id) is None
        assert manager.resume_ai_job(web_id) is None
        assert calls == []

    def test_retry_returns_none_for_unknown_web_job(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manager = JobManager(ai_job_store=store)
        assert manager.retry_ai_job("") is None
        assert manager.retry_ai_job("short") is None
        assert manager.retry_ai_job("dddddddddddddddddddddddddddddddd") is None

    def test_delete_ai_web_job_removes_all_shared_manifests(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        web_id = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        m1 = store.create_job(
            tmp_path / "a.md", "One.", "orcarouter", "m1", 100,
            web_job_id=web_id, output_basename="a.ai.md",
        )
        m2 = store.create_job(
            tmp_path / "b.md", "Two.", "orcarouter", "m1", 100,
            web_job_id=web_id, output_basename="b.ai.md",
        )
        other = store.create_job(
            tmp_path / "c.md", "Three.", "orcarouter", "m1", 100,
            web_job_id="ffffffffffffffffffffffffffffffff",
            output_basename="c.ai.md",
        )
        manager = JobManager(ai_job_store=store)
        assert manager.delete_ai_web_job(web_id) == 2
        assert not store.job_dir(m1.job_id).exists()
        assert not store.job_dir(m2.job_id).exists()
        assert store.job_dir(other.job_id).is_dir()
        # An invalid web_job_id never deletes anything.
        assert manager.delete_ai_web_job("..") == 0
        assert manager.delete_ai_web_job("") == 0

    def test_forget_removes_in_memory_snapshot(self, tmp_path: Path) -> None:
        md = _write_md(tmp_path / "doc.md", content="# Hi\n\nBody.\n")
        manager = JobManager()
        job_id = manager.submit("clean", {"file": md})
        assert manager.wait(job_id, timeout=10).status is JobStatus.succeeded
        assert manager.get(job_id) is not None
        manager.forget(job_id)
        assert manager.get(job_id) is None
        assert all(j["job_id"] != job_id for j in manager.list_recent())
