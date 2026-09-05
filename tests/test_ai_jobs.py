"""Tests for the durable AI job store (format_converter.ai_jobs).

All tests are offline and use ``tmp_path`` fixtures.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from format_converter.ai_jobs import AIJobError, AIJobManifest, AIJobStore


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_md(path: Path, content: str = "# Hello\n\nBody.\n\nBody.") -> Path:
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# manifest creation
# ---------------------------------------------------------------------------


class TestCreateJob:
    def test_create_job_writes_manifest_input_chunks_and_separators(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(
            input_path=tmp_path / "doc.md",
            text="One.\n\nTwo.",
            provider="orcarouter",
            model="m1",
            max_chars=6,
        )
        job_dir = store.job_dir(manifest.job_id)
        assert (job_dir / "manifest.json").is_file()
        # open(newline="") rather than Path.read_text(newline=...), which only
        # supports newline on Python 3.13+.
        with (job_dir / "input.md").open(encoding="utf-8", newline="") as handle:
            assert handle.read() == "One.\n\nTwo."
        assert (job_dir / "chunks" / "0001.txt").is_file()
        assert (job_dir / "chunks" / "0002.txt").is_file()
        assert (job_dir / "chunks" / "0001.txt").read_text(encoding="utf-8") == "One.\n"
        assert (job_dir / "chunks" / "0002.txt").read_text(encoding="utf-8") == "Two."
        assert "api_key" not in (job_dir / "manifest.json").read_text(encoding="utf-8").lower()

    def test_manifest_does_not_contain_api_key_or_secret(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(
            input_path=tmp_path / "doc.md",
            text="One.\n\nTwo.",
            provider="orcarouter",
            model="m1",
            max_chars=6,
        )
        raw = (store.job_dir(manifest.job_id) / "manifest.json").read_text(encoding="utf-8")
        assert "sk-" not in raw
        assert "api_key" not in raw.lower()
        assert "ORCAROUTER_API_KEY" not in raw

    def test_chunks_numbered_stably(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(
            input_path=tmp_path / "doc.md",
            text="One.\n\nTwo.\n\nThree.",
            provider="orcarouter",
            model="m1",
            max_chars=6,
        )
        chunks_dir = store.job_dir(manifest.job_id) / "chunks"
        names = sorted(chunks_dir.iterdir())
        assert names == [
            chunks_dir / "0001.txt",
            chunks_dir / "0002.txt",
            chunks_dir / "0003.txt",
        ]

    def test_manifest_fields(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(
            input_path=tmp_path / "doc.md",
            text="One.\n\nTwo.",
            provider="orcarouter",
            model="m1",
            max_chars=6,
        )
        assert manifest.job_id
        assert len(manifest.job_id) == 32
        assert manifest.type == "ai-clean"
        assert manifest.status == "running"
        assert manifest.provider == "orcarouter"
        assert manifest.model == "m1"
        assert manifest.max_chars == 6
        assert manifest.total_chunks == 2
        assert manifest.current_chunk == 0
        assert len(manifest.chunks) == 2
        assert manifest.chunks[0].index == 1
        assert manifest.chunks[0].status == "pending"
        assert manifest.chunks[1].index == 2
        assert manifest.chunks[1].status == "pending"
        assert manifest.created_at > 0
        assert manifest.updated_at > 0

    def test_create_job_with_image_input_only_separator(self, tmp_path: Path) -> None:
        """Whitespace-only input produces zero chunks and a single separator."""
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(
            input_path=tmp_path / "doc.md",
            text="   \n\n  ",
            provider="orcarouter",
            model="m1",
            max_chars=6,
        )
        assert manifest.total_chunks == 0
        job_dir = store.job_dir(manifest.job_id)
        separators = json.loads((job_dir / "separators.json").read_text(encoding="utf-8"))
        assert len(separators) == 1
        assert separators[0] == "   \n\n  "

    def test_create_job_rejects_secret_shaped_model(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        with pytest.raises(AIJobError, match="model name must not look like an API key"):
            store.create_job(
                input_path=tmp_path / "doc.md",
                text="Hello.",
                provider="orcarouter",
                model="sk-short",
                max_chars=100,
            )
        # No manifest should have been written.
        roots = [d for d in tmp_path.iterdir() if d.is_dir()]
        assert len(roots) == 0

    def test_create_job_rejects_secret_shaped_model_no_echo(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        with pytest.raises(AIJobError) as excinfo:
            store.create_job(
                input_path=tmp_path / "doc.md",
                text="Hello.",
                provider="orcarouter",
                model="sk-secret",
                max_chars=100,
            )
        assert "sk-" not in str(excinfo.value)

    def test_legitimate_model_creates_job(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(
            input_path=tmp_path / "doc.md",
            text="Hello.",
            provider="orcarouter",
            model="gpt-4o",
            max_chars=100,
        )
        assert (store.job_dir(manifest.job_id) / "manifest.json").is_file()
        assert manifest.model == "gpt-4o"

    def test_web_job_id_stored_in_manifest(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(
            input_path=tmp_path / "doc.md",
            text="Hello.",
            provider="orcarouter",
            model="m1",
            max_chars=100,
            web_job_id="abc123def4567890abc123def4567890",
        )
        assert manifest.web_job_id == "abc123def4567890abc123def4567890"
        loaded = store.load(manifest.job_id)
        assert loaded.web_job_id == "abc123def4567890abc123def4567890"

    def test_create_job_rejects_whitespace_padded_secret_shaped_model(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        with pytest.raises(AIJobError, match="model name must not look like an API key"):
            store.create_job(
                input_path=tmp_path / "doc.md",
                text="Hello.",
                provider="orcarouter",
                model="  sk-short  ",
                max_chars=100,
            )
        roots = [d for d in tmp_path.iterdir() if d.is_dir()]
        assert len(roots) == 0

    def test_create_job_trims_model_name(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(
            input_path=tmp_path / "doc.md",
            text="Hello.",
            provider="orcarouter",
            model="  gpt-4o  ",
            max_chars=100,
        )
        assert manifest.model == "gpt-4o"
        loaded = store.load(manifest.job_id)
        assert loaded.model == "gpt-4o"

    def test_create_job_rejects_secret_shaped_web_job_id(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        with pytest.raises(AIJobError):
            store.create_job(
                input_path=tmp_path / "doc.md",
                text="Hello.",
                provider="orcarouter",
                model="m1",
                max_chars=100,
                web_job_id="sk-short",
            )
        roots = [d for d in tmp_path.iterdir() if d.is_dir()]
        assert len(roots) == 0

    def test_create_job_rejects_invalid_web_job_id_no_echo(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        with pytest.raises(AIJobError) as excinfo:
            store.create_job(
                input_path=tmp_path / "doc.md",
                text="Hello.",
                provider="orcarouter",
                model="m1",
                max_chars=100,
                web_job_id="sk-short",
            )
        assert "sk-short" not in str(excinfo.value)
        assert "sk-" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# save_result atomic write + manifest update
# ---------------------------------------------------------------------------


class TestSaveResult:
    def test_save_result_writes_file_and_updates_manifest(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(tmp_path / "doc.md", "One.\n\nTwo.", "orcarouter", "m1", 6)
        job_dir = store.job_dir(manifest.job_id)

        store.save_result(manifest.job_id, 1, "Revised one.")
        result_path = job_dir / "results" / "0001.md"
        assert result_path.is_file()
        assert result_path.read_text(encoding="utf-8") == "Revised one."

        updated = store.load(manifest.job_id)
        assert updated.current_chunk == 1
        assert updated.chunks[0].status == "completed"

    def test_save_result_atomic_write_survives_interrupt(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(tmp_path / "doc.md", "One.\n\nTwo.", "orcarouter", "m1", 6)
        job_dir = store.job_dir(manifest.job_id)
        results_dir = job_dir / "results"

        store.save_result(manifest.job_id, 1, "Final text.")
        tmp_file = results_dir / "0001.md.tmp"
        assert not tmp_file.exists()
        assert (results_dir / "0001.md").read_text(encoding="utf-8") == "Final text."

    def test_save_result_all_chunks(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(tmp_path / "doc.md", "One.\n\nTwo.\n\nThree.", "orcarouter", "m1", 6)
        for i in range(1, 4):
            store.save_result(manifest.job_id, i, f"Revised {i}.")
        updated = store.load(manifest.job_id)
        assert all(c.status == "completed" for c in updated.chunks)


# ---------------------------------------------------------------------------
# next_unfinished — must distrust stale manifest
# ---------------------------------------------------------------------------


class TestNextUnfinished:
    def test_next_unfinished_returns_first_chunk_initially(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(tmp_path / "doc.md", "One.\n\nTwo.", "orcarouter", "m1", 6)
        assert store.next_unfinished(manifest.job_id) == 1

    def test_next_unfinished_returns_none_when_all_done(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(tmp_path / "doc.md", "One.\n\nTwo.", "orcarouter", "m1", 6)
        store.save_result(manifest.job_id, 1, "R1")
        store.save_result(manifest.job_id, 2, "R2")
        assert store.next_unfinished(manifest.job_id) is None

    def test_next_unfinished_uses_result_files_not_only_manifest(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(tmp_path / "doc.md", "One.\n\nTwo.", "orcarouter", "m1", 6)
        store.save_result(manifest.job_id, 1, "R1")
        # Manually corrupt the manifest: mark chunk 2 as completed without a result file.
        manifest_path = store.job_dir(manifest.job_id) / "manifest.json"
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        for entry in raw["chunks"]:
            entry["status"] = "completed"
        manifest_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        # Chunk 2's result file is missing, so it should be returned as unfinished.
        assert store.next_unfinished(manifest.job_id) == 2

    def test_next_unfinished_skips_existing_result_files(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(tmp_path / "doc.md", "One.\n\nTwo.\n\nThree.", "orcarouter", "m1", 6)
        store.save_result(manifest.job_id, 1, "R1")
        store.save_result(manifest.job_id, 3, "R3")
        # Chunk 2 is missing -> return 2
        assert store.next_unfinished(manifest.job_id) == 2

    def test_next_unfinished_result_file_present_manifest_pending(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(tmp_path / "doc.md", "One.\n\nTwo.", "orcarouter", "m1", 6)
        job_dir = store.job_dir(manifest.job_id)
        # Manually write result files without updating manifest.
        (job_dir / "results").mkdir()
        (job_dir / "results" / "0001.md").write_text("R1", encoding="utf-8")
        (job_dir / "results" / "0002.md").write_text("R2", encoding="utf-8")
        # Manifest still says "pending" for both, but result files exist.
        assert store.next_unfinished(manifest.job_id) is None

    def test_next_unfinished_manifest_completed_but_file_missing(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(tmp_path / "doc.md", "One.\n\nTwo.", "orcarouter", "m1", 6)
        store.save_result(manifest.job_id, 1, "R1")
        # Save result updated manifest for chunk 1. Manually mark chunk 2 as completed.
        manifest_path = store.job_dir(manifest.job_id) / "manifest.json"
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        for entry in raw["chunks"]:
            entry["status"] = "completed"
        manifest_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        # Chunk 2 has no result file, so it must be returned as unfinished.
        assert store.next_unfinished(manifest.job_id) == 2


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------


class TestMerge:
    def test_merge_uses_separators_correctly(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(tmp_path / "doc.md", "One.\n\nTwo.", "orcarouter", "m1", 6)
        store.save_result(manifest.job_id, 1, "Revised one.")
        store.save_result(manifest.job_id, 2, "Revised two.")
        final_path = store.merge(manifest.job_id)
        assert final_path.is_file()
        merged = final_path.read_text(encoding="utf-8")
        # The separator between chunks is "\n" (trailing newline of "One.\n").
        assert merged == "Revised one.\nRevised two."

    def test_merge_sets_status_to_completed(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(tmp_path / "doc.md", "One.\n\nTwo.", "orcarouter", "m1", 6)
        store.save_result(manifest.job_id, 1, "R1")
        store.save_result(manifest.job_id, 2, "R2")
        store.merge(manifest.job_id)
        updated = store.load(manifest.job_id)
        assert updated.status == "completed"

    def test_merge_failure_does_not_re_request_ai(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(tmp_path / "doc.md", "One.\n\nTwo.", "orcarouter", "m1", 6)
        store.save_result(manifest.job_id, 1, "R1")
        # Chunk 2 result is missing — merge must raise.
        with pytest.raises(AIJobError, match="result file missing"):
            store.merge(manifest.job_id)
        # Status should remain as "merging" (set before the merge attempt).
        updated = store.load(manifest.job_id)
        assert updated.status == "merging"

    def test_merge_single_chunk(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(tmp_path / "doc.md", "Only one.", "orcarouter", "m1", 100)
        store.save_result(manifest.job_id, 1, "Revised.")
        final_path = store.merge(manifest.job_id)
        merged = final_path.read_text(encoding="utf-8")
        assert merged == "Revised."


# ---------------------------------------------------------------------------
# read_chunk
# ---------------------------------------------------------------------------


class TestReadChunk:
    def test_read_chunk_returns_correct_content(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(tmp_path / "doc.md", "One.\n\nTwo.", "orcarouter", "m1", 6)
        chunk1 = store.read_chunk(manifest.job_id, 1)
        assert chunk1 == "One.\n"
        chunk2 = store.read_chunk(manifest.job_id, 2)
        assert chunk2 == "Two."

    def test_read_chunk_raises_for_missing_chunk(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(tmp_path / "doc.md", "One.\n\nTwo.", "orcarouter", "m1", 6)
        with pytest.raises(AIJobError):
            store.read_chunk(manifest.job_id, 99)


# ---------------------------------------------------------------------------
# security: invalid / traversing job ids
# ---------------------------------------------------------------------------


class TestSecurity:
    def test_invalid_job_id_cannot_path_traverse(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        (tmp_path / ".formatconverter-jobs").mkdir(parents=True, exist_ok=True)
        (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
        for bad in ["..", "../", ".", "0" * 31, "g" * 32, "G" * 32, "nope", "../../secret"]:
            with pytest.raises(AIJobError, match="Invalid job id"):
                store.job_dir(bad)
            with pytest.raises(AIJobError):
                store.load(bad)
            with pytest.raises(AIJobError):
                store.next_unfinished(bad)

    def test_corrupted_manifest_raises_clear_exception(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        store._root.mkdir(parents=True, exist_ok=True)
        job_dir = store._root / "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        job_dir.mkdir()
        (job_dir / "manifest.json").write_text("{corrupted", encoding="utf-8")
        with pytest.raises(AIJobError, match="Could not read manifest"):
            store.load("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")

    def test_missing_manifest_raises_clear_exception(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        store._root.mkdir(parents=True, exist_ok=True)
        job_dir = store._root / "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        job_dir.mkdir()
        with pytest.raises(AIJobError, match="Manifest not found"):
            store.load("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")


# ---------------------------------------------------------------------------
# update_status
# ---------------------------------------------------------------------------


class TestUpdateStatus:
    def test_update_status_changes_manifest_status(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(tmp_path / "doc.md", "One.\n\nTwo.", "orcarouter", "m1", 6)
        store.update_status(manifest.job_id, "failed")
        updated = store.load(manifest.job_id)
        assert updated.status == "failed"

    def test_update_status_with_current_chunk(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(tmp_path / "doc.md", "One.\n\nTwo.", "orcarouter", "m1", 6)
        store.update_status(manifest.job_id, "running", current_chunk=1)
        updated = store.load(manifest.job_id)
        assert updated.current_chunk == 1
        assert updated.status == "running"


# ---------------------------------------------------------------------------
# load and roundtrip
# ---------------------------------------------------------------------------


class TestLoad:
    def test_load_returns_same_manifest(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(tmp_path / "doc.md", "One.\n\nTwo.", "orcarouter", "m1", 6)
        loaded = store.load(manifest.job_id)
        assert loaded.job_id == manifest.job_id
        assert loaded.total_chunks == manifest.total_chunks
        assert loaded.provider == manifest.provider
        assert loaded.model == manifest.model
        assert loaded.max_chars == manifest.max_chars

    def test_load_after_save_result(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(tmp_path / "doc.md", "One.\n\nTwo.", "orcarouter", "m1", 6)
        store.save_result(manifest.job_id, 1, "R1")
        loaded = store.load(manifest.job_id)
        assert loaded.current_chunk == 1
        assert loaded.chunks[0].status == "completed"


# ---------------------------------------------------------------------------
# mark_stale_running_interrupted, scan_recent, completed_count
# ---------------------------------------------------------------------------


class TestStaleScan:
    def test_mark_stale_running_interrupted(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(tmp_path / "doc.md", "One.\n\nTwo.", "orcarouter", "m1", 6)
        store.update_status(manifest.job_id, "running")
        store.mark_stale_running_interrupted()
        assert store.load(manifest.job_id).status == "interrupted"

    def test_mark_stale_merging_interrupted(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(tmp_path / "doc.md", "One.\n\nTwo.", "orcarouter", "m1", 6)
        store.update_status(manifest.job_id, "merging")
        store.mark_stale_running_interrupted()
        assert store.load(manifest.job_id).status == "interrupted"

    def test_mark_stale_skips_completed_and_failed(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        m1 = store.create_job(tmp_path / "a.md", "One.", "orcarouter", "m1", 100)
        store.update_status(m1.job_id, "completed")
        m2 = store.create_job(tmp_path / "b.md", "Two.", "orcarouter", "m1", 100)
        store.update_status(m2.job_id, "failed")
        store.mark_stale_running_interrupted()
        assert store.load(m1.job_id).status == "completed"
        assert store.load(m2.job_id).status == "failed"

    def test_scan_recent_sorts_by_updated_at(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        m1 = store.create_job(tmp_path / "a.md", "One.", "orcarouter", "m1", 100)
        import time
        time.sleep(0.01)
        m2 = store.create_job(tmp_path / "b.md", "Two.", "orcarouter", "m1", 100)
        recent = store.scan_recent(limit=10)
        assert recent[0].job_id == m2.job_id
        assert recent[1].job_id == m1.job_id

    def test_scan_recent_skips_corrupt_manifests(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        m1 = store.create_job(tmp_path / "a.md", "One.", "orcarouter", "m1", 100)
        # Create a corrupt dir that looks like a valid job id.
        corrupt_id = "cccccccccccccccccccccccccccccccc"
        corrupt_dir = store._root / corrupt_id
        corrupt_dir.mkdir()
        (corrupt_dir / "manifest.json").write_text("{corrupt", encoding="utf-8")
        recent = store.scan_recent(limit=10)
        ids = [m.job_id for m in recent]
        assert m1.job_id in ids
        assert corrupt_id not in ids

    def test_scan_recent_empty_root(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs" / "nonexistent")
        assert store.scan_recent() == []

    def test_completed_count_uses_result_files(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(tmp_path / "doc.md", "One.\n\nTwo.\n\nThree.", "orcarouter", "m1", 6)
        store.save_result(manifest.job_id, 1, "R1")
        store.save_result(manifest.job_id, 3, "R3")
        assert store.completed_count(manifest.job_id) == 2

    def test_completed_count_no_results_dir(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(tmp_path / "doc.md", "One.", "orcarouter", "m1", 100)
        assert store.completed_count(manifest.job_id) == 0

    def test_completed_count_ignores_non_chunk_md_files(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(tmp_path / "doc.md", "One.\n\nTwo.\n\nThree.", "orcarouter", "m1", 6)
        store.save_result(manifest.job_id, 1, "R1")
        job_dir = store.job_dir(manifest.job_id)
        results_dir = job_dir / "results"
        # Write a stray .md file that does not correspond to any chunk index.
        (results_dir / "9999.md").write_text("stray", encoding="utf-8")
        (results_dir / "readme.md").write_text("readme", encoding="utf-8")
        # Only chunk 1 is counted; stray files are ignored.
        assert store.completed_count(manifest.job_id) == 1

    def test_output_basename_stored_in_manifest(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(
            tmp_path / "doc.md",
            "Hello.",
            "orcarouter",
            "m1",
            100,
            web_job_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            output_basename="doc.ai.md",
        )
        assert manifest.output_basename == "doc.ai.md"
        loaded = store.load(manifest.job_id)
        assert loaded.output_basename == "doc.ai.md"

    def test_output_basename_defaults_to_empty(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(
            tmp_path / "doc.md", "Hello.", "orcarouter", "m1", 100,
        )
        assert manifest.output_basename == ""
        loaded = store.load(manifest.job_id)
        assert loaded.output_basename == ""


class TestOutputBasename:
    def test_accepts_legitimate_basename(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(
            tmp_path / "doc.md", "Hello.", "orcarouter", "m1", 100,
            output_basename="doc.ai.md",
        )
        assert manifest.output_basename == "doc.ai.md"

    def test_accepts_empty_basename(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(
            tmp_path / "doc.md", "Hello.", "orcarouter", "m1", 100,
            output_basename="",
        )
        assert manifest.output_basename == ""

    @pytest.mark.parametrize("bad", [
        "../escaped.md",
        "..\\..\\escaped.md",
        "/absolute/path.md",
        "C:\\bad.md",
        "sub/dir/file.md",
        ".",
        "..",
        "   ",
    ])
    def test_rejects_unsafe_basename(self, tmp_path: Path, bad: str) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        with pytest.raises(AIJobError, match="invalid output basename"):
            store.create_job(
                tmp_path / "doc.md", "Hello.", "orcarouter", "m1", 100,
                output_basename=bad,
            )
        # No directory should have been created.
        assert not any(tmp_path.iterdir())

    def test_rejects_unsafe_basename_no_echo(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        with pytest.raises(AIJobError) as excinfo:
            store.create_job(
                tmp_path / "doc.md", "Hello.", "orcarouter", "m1", 100,
                output_basename="../escaped.md",
            )
        assert "../escaped.md" not in str(excinfo.value)

    def test_scan_recent_skips_manifest_with_invalid_output_basename(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        good = store.create_job(
            tmp_path / "doc.md", "Hello.", "orcarouter", "m1", 100,
            output_basename="good.md",
        )
        # Manually create a manifest with an invalid output_basename.
        bad_id = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        bad_dir = store._root / bad_id
        bad_dir.mkdir()
        import json
        (bad_dir / "manifest.json").write_text(
            json.dumps({
                "job_id": bad_id, "type": "ai-clean", "status": "interrupted",
                "provider": "orcarouter", "model": "m1",
                "output_basename": "../escaped.md",
                "total_chunks": 1, "current_chunk": 0,
                "chunks": [{"index": 1, "chars": 5, "status": "pending"}],
                "created_at": 0.0, "updated_at": 0.0,
            }),
            encoding="utf-8",
        )
        recent = store.scan_recent(limit=10)
        ids = [m.job_id for m in recent]
        assert good.job_id in ids
        assert bad_id not in ids

    def test_hydration_skips_unsafe_output_basename(self, tmp_path: Path) -> None:
        import json
        from format_converter.jobs import JobManager
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        store._root.mkdir(parents=True, exist_ok=True)
        web_id = "cccccccccccccccccccccccccccccccc"
        bad_dir = store._root / web_id
        bad_dir.mkdir()
        (bad_dir / "manifest.json").write_text(
            json.dumps({
                "job_id": web_id, "type": "ai-clean", "status": "interrupted",
                "provider": "orcarouter", "model": "m1",
                "web_job_id": web_id, "output_basename": "../escaped.md",
                "total_chunks": 1, "current_chunk": 0,
                "chunks": [{"index": 1, "chars": 5, "status": "pending"}],
                "created_at": 0.0, "updated_at": 0.0,
            }),
            encoding="utf-8",
        )
        # Also create a manifest with an invalid web_job_id (secret-shaped / wrong length).
        web_bad = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        bad_dir2 = store._root / web_bad
        bad_dir2.mkdir()
        (bad_dir2 / "manifest.json").write_text(
            json.dumps({
                "job_id": web_bad, "type": "ai-clean", "status": "interrupted",
                "provider": "orcarouter", "model": "m1",
                "web_job_id": "sk-short", "output_basename": "ok.ai.md",
                "total_chunks": 1, "current_chunk": 0,
                "chunks": [{"index": 1, "chars": 5, "status": "pending"}],
                "created_at": 0.0, "updated_at": 0.0,
            }),
            encoding="utf-8",
        )
        manager = JobManager(ai_job_store=store)
        manager._hydrate_ai_job_snapshots(base_dir=tmp_path)
        # Neither the unsafe-basename manifest nor the bad-web_job_id manifest
        # may create output paths outside their job root.
        for bad in (web_id, web_bad):
            result = manager.get(bad)
            assert result is None or result.output_paths == ()
        # No file may have been written outside the job roots.
        escaped = tmp_path / "escaped.md"
        assert not escaped.exists()


# ---------------------------------------------------------------------------
# find_by_web_job_id (Task 4: resume/retry must find old jobs)
# ---------------------------------------------------------------------------


class TestFindByWebJobId:
    def test_finds_manifests_shared_by_web_job_id(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        web_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        m1 = store.create_job(tmp_path / "a.md", "One.", "orcarouter", "m1", 100,
                              web_job_id=web_id, output_basename="a.ai.md")
        m2 = store.create_job(tmp_path / "b.md", "Two.", "orcarouter", "m1", 100,
                              web_job_id=web_id, output_basename="b.ai.md")
        other = store.create_job(tmp_path / "c.md", "Three.", "orcarouter", "m1", 100,
                                 web_job_id="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                                 output_basename="c.ai.md")
        found = {m.job_id for m in store.find_by_web_job_id(web_id)}
        assert found == {m1.job_id, m2.job_id}
        assert other.job_id not in found

    def test_finds_old_job_beyond_scan_recent_limit(self, tmp_path: Path) -> None:
        # A job older than scan_recent(limit=100) would be dropped by that scan;
        # find_by_web_job_id must still locate it.
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        target_web = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        target = store.create_job(tmp_path / "old.md", "Old.", "orcarouter", "m1", 100,
                                  web_job_id=target_web, output_basename="old.ai.md")
        for i in range(101):
            store.create_job(
                tmp_path / f"n{i}.md", f"New {i}.", "orcarouter", "m1", 100,
                web_job_id="cccccccccccccccccccccccccccccccc",
                output_basename=f"n{i}.ai.md",
            )
        assert len(store.scan_recent(limit=100)) == 100
        assert target.job_id not in {m.job_id for m in store.scan_recent(limit=100)}
        assert target.job_id in {m.job_id for m in store.find_by_web_job_id(target_web)}

    def test_returns_empty_for_unknown_web_job_id(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        store.create_job(tmp_path / "a.md", "One.", "orcarouter", "m1", 100,
                         web_job_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                         output_basename="a.ai.md")
        assert store.find_by_web_job_id("ffffffffffffffffffffffffffffffff") == []

    def test_invalid_web_job_id_matches_nothing(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        for bad in ("", "..", "short", "sk-short", "0" * 31):
            assert store.find_by_web_job_id(bad) == []


# ---------------------------------------------------------------------------
# durable job deletion (Task 4)
# ---------------------------------------------------------------------------


class TestDeleteDurableJob:
    def test_delete_job_removes_directory(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        manifest = store.create_job(tmp_path / "doc.md", "One.", "orcarouter", "m1", 100)
        assert store.job_dir(manifest.job_id).is_dir()
        assert store.delete_job(manifest.job_id) is True
        assert not store.job_dir(manifest.job_id).exists()
        # Idempotent: deleting again reports nothing was removed.
        assert store.delete_job(manifest.job_id) is False

    def test_delete_job_rejects_invalid_ids(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        store._root.mkdir(parents=True, exist_ok=True)
        (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
        for bad in ["..", "../", ".", "0" * 31, "g" * 32, "../../secret"]:
            with pytest.raises(AIJobError):
                store.delete_job(bad)
        # Nothing outside the store was removed.
        assert (tmp_path / "secret.txt").is_file()

    def test_delete_web_job_removes_all_shared_manifests(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        web_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        m1 = store.create_job(tmp_path / "a.md", "One.", "orcarouter", "m1", 100,
                              web_job_id=web_id, output_basename="a.ai.md")
        m2 = store.create_job(tmp_path / "b.md", "Two.", "orcarouter", "m1", 100,
                              web_job_id=web_id, output_basename="b.ai.md")
        other = store.create_job(tmp_path / "c.md", "Three.", "orcarouter", "m1", 100,
                                 web_job_id="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                                 output_basename="c.ai.md")
        removed = store.delete_web_job(web_id)
        assert removed == 2
        assert not store.job_dir(m1.job_id).exists()
        assert not store.job_dir(m2.job_id).exists()
        # The unrelated durable job survives.
        assert store.job_dir(other.job_id).is_dir()
        assert store.find_by_web_job_id(web_id) == []

    def test_delete_web_job_invalid_id_raises(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        store._root.mkdir(parents=True, exist_ok=True)
        with pytest.raises(AIJobError, match="invalid web_job_id"):
            store.delete_web_job("..")
        with pytest.raises(AIJobError, match="invalid web_job_id"):
            store.delete_web_job("sk-short")
        with pytest.raises(AIJobError, match="invalid web_job_id"):
            store.delete_web_job("0" * 31)

    def test_delete_web_job_empty_is_zero(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        assert store.delete_web_job("ffffffffffffffffffffffffffffffff") == 0

    def test_delete_web_job_never_removes_outside_store(self, tmp_path: Path) -> None:
        store = AIJobStore(tmp_path / ".formatconverter-jobs")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret", encoding="utf-8")
        # A durable directory whose name matches a job-id shape but was never a
        # real manifest is left alone; delete_web_job only deletes directories
        # linked by a readable manifest's web_job_id.
        store._root.mkdir(parents=True, exist_ok=True)
        foreign = store._root / "dddddddddddddddddddddddddddddddd"
        foreign.mkdir()
        (foreign / "secret.txt").write_text("keep", encoding="utf-8")
        removed = store.delete_web_job("dddddddddddddddddddddddddddddddd")
        assert removed == 0
        assert (outside / "secret.txt").is_file()
        assert (foreign / "secret.txt").is_file()