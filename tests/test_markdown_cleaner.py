"""Regression tests for the existing Markdown cleaner (no new behavior changed)."""

from __future__ import annotations

from pathlib import Path

from format_converter.markdown_cleaner import (
    backup_path_for,
    clean_markdown_file,
    clean_markdown_text,
    join_soft_wrapped_lines,
)


class TestSoftWrappedLines:
    def test_prose_lines_are_joined(self) -> None:
        text = "This is the first part of a sentence\nand this is the continuation."
        result = clean_markdown_text(text)
        assert "This is the first part of a sentence and this is the continuation." in result

    def test_join_soft_wrapped_lines_returns_trailing_newline(self) -> None:
        result = join_soft_wrapped_lines("line one\nline two")
        assert result == "line one line two\n"


class TestLists:
    def test_list_blocks_keep_line_breaks_by_default(self) -> None:
        text = "- apple\n- banana\n- cherry"
        result = clean_markdown_text(text)
        assert "- apple\n- banana\n- cherry" in result

    def test_numbered_list_lines_are_kept(self) -> None:
        text = "1. first\n2. second"
        result = clean_markdown_text(text)
        assert "1. first\n2. second" in result


class TestCodeFences:
    def test_fenced_code_block_is_preserved(self) -> None:
        text = "Before.\n\n```python\nline_one\nline_two\n```\n\nAfter."
        result = clean_markdown_text(text)
        assert "```python\nline_one\nline_two\n```" in result

    def test_fence_content_is_not_line_joined(self) -> None:
        text = "```\nkeep_a\nkeep_b\n```"
        result = clean_markdown_text(text)
        assert "keep_a\nkeep_b" in result
        assert "keep_a keep_b" not in result


class TestRepeatedParagraphs:
    def test_repeated_paragraphs_are_deduped(self) -> None:
        result = clean_markdown_text("Repeat me.\n\nRepeat me.")
        assert result.count("Repeat me.") == 1

    def test_dedupe_can_be_disabled(self) -> None:
        result = clean_markdown_text("Repeat me.\n\nRepeat me.", dedupe=False)
        assert result.count("Repeat me.") == 2


class TestBackupFile:
    def test_backup_path_for_md(self) -> None:
        assert backup_path_for(Path("a.md")) == Path("a.bak.md")

    def test_backup_path_for_other_suffix(self) -> None:
        assert backup_path_for(Path("a.txt")) == Path("a.txt.bak")

    def test_clean_file_creates_backup_with_original(self, tmp_path: Path) -> None:
        target = tmp_path / "doc.md"
        target.write_text("Dup.\n\nDup.", encoding="utf-8")
        result = clean_markdown_file(target)
        assert result == target
        backup = tmp_path / "doc.bak.md"
        assert backup.exists()
        assert backup.read_text(encoding="utf-8") == "Dup.\n\nDup."
        assert target.read_text(encoding="utf-8") == "Dup.\n"

    def test_clean_file_skips_backup_when_disabled(self, tmp_path: Path) -> None:
        target = tmp_path / "doc.md"
        target.write_text("Hello.", encoding="utf-8")
        clean_markdown_file(target, backup=False)
        assert not (tmp_path / "doc.bak.md").exists()
