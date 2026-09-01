"""Tests for the unified job/task service layer (format_converter.jobs).

All tests are offline: they use tmp_path fixtures, injected fake clients, and
monkeypatched conversion functions (pymupdf4llm is not installed in the venv,
so the PDF path is faked while still exercising the real handler logic).
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from format_converter.jobs import JobManager, JobStatus, UnknownJobTypeError


class EchoClient:
    """Injected fake LLM client mirroring tests/test_cli.py's EchoClient."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.calls: list[dict] = []

    def complete(self, *, system: str, user: str, model: str) -> str:
        self.calls.append({"system": system, "user": user, "model": model})
        return f"[revised] {user}"


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
