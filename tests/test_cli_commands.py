"""End-to-end wiring tests for the CLI ``main()`` entry point.

``tests/test_cli.py`` covers argument parsing for all commands and the full
``ai-clean`` path. This file closes the remaining gap: it drives ``main()``
through the **convert**, **clean**, **pipeline**, and **marker** commands with
monkeypatched worker functions (so no real ``pymupdf4llm``/``marker`` call is
ever made) and asserts the return code and printed output. It also documents
the pre-existing error-handling contract for those commands: graceful output
for an empty directory, and exception propagation for a missing file/dir.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from format_converter.cli import main


# ---------------------------------------------------------------------------
# fake worker functions (monkeypatched into format_converter.cli)
# ---------------------------------------------------------------------------


def _fake_convert_file(pdf_path: Path, output_dir: Path, overwrite: bool = False) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out = output_dir / f"{Path(pdf_path).stem}.md"
    out.write_text("# fake", encoding="utf-8")
    return out


def _fake_convert_directory(input_dir: Path, output_dir: Path, overwrite: bool = False) -> list[Path]:
    input_dir = Path(input_dir)
    return [
        _fake_convert_file(p, output_dir, overwrite=overwrite)
        for p in sorted(input_dir.glob("*.pdf"))
    ]


def _fake_clean_file(path: Path, keep_lists: bool = True, dedupe: bool = True, backup: bool = True) -> Path:
    path = Path(path)
    path.write_text("cleaned", encoding="utf-8")
    return path


def _fake_clean_directory(
    input_dir: Path, keep_lists: bool = True, dedupe: bool = True, backup: bool = True
) -> list[Path]:
    return sorted(Path(input_dir).glob("*.md"))


def _fake_run_pipeline(
    pdf_dir: Path,
    md_dir: Path,
    overwrite: bool = False,
    keep_lists: bool = True,
    dedupe: bool = True,
    backup: bool = True,
) -> tuple[list[Path], list[Path]]:
    md_dir = Path(md_dir)
    md_dir.mkdir(parents=True, exist_ok=True)
    outputs = [md_dir / "a.md", md_dir / "b.md"]
    for out in outputs:
        out.write_text("# converted", encoding="utf-8")
    return outputs, outputs


def _fake_marker(pdf_path: Path, output_dir: Path, output_name: str | None = None) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def _write_pdf(path: Path) -> Path:
    path.write_bytes(b"%PDF-1.4 fake")
    return path


def _write_md(path: Path, content: str = "# Hello\n\nBody.") -> Path:
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# convert
# ---------------------------------------------------------------------------


class TestConvertMain:
    def test_convert_single_file_wiring(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        pdf = _write_pdf(tmp_path / "doc.pdf")
        out_dir = tmp_path / "out"
        monkeypatch.setattr("format_converter.cli.convert_pdf_file", _fake_convert_file)

        rc = main(["convert", "--file", str(pdf), "--output", str(out_dir)])

        assert rc == 0
        captured = capsys.readouterr().out
        assert f"Converted: {out_dir / 'doc.md'}" in captured
        assert (out_dir / "doc.md").read_text(encoding="utf-8") == "# fake"

    def test_convert_directory_wiring(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        in_dir = tmp_path / "pdfs"
        in_dir.mkdir()
        _write_pdf(in_dir / "a.pdf")
        _write_pdf(in_dir / "b.pdf")
        out_dir = tmp_path / "out"
        monkeypatch.setattr("format_converter.cli.convert_pdf_directory", _fake_convert_directory)

        rc = main(["convert", "--input", str(in_dir), "--output", str(out_dir)])

        assert rc == 0
        captured = capsys.readouterr().out
        assert "Converted 2 PDF file(s)." in captured
        assert f"- {out_dir / 'a.md'}" in captured
        assert f"- {out_dir / 'b.md'}" in captured
        assert (out_dir / "a.md").is_file()
        assert (out_dir / "b.md").is_file()

    def test_convert_empty_directory_returns_zero(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        # No PDFs in the directory: graceful "0 files" path, no real conversion.
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        out_dir = tmp_path / "out"

        rc = main(["convert", "--input", str(empty_dir), "--output", str(out_dir)])

        assert rc == 0
        assert "Converted 0 PDF file(s)." in capsys.readouterr().out

    def test_convert_directory_with_non_pdf_files_returns_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        in_dir = tmp_path / "mixed"
        in_dir.mkdir()
        (in_dir / "notes.txt").write_text("not a pdf", encoding="utf-8")
        rc = main(["convert", "--input", str(in_dir), "--output", str(tmp_path / "out")])
        assert rc == 0
        assert "Converted 0 PDF file(s)." in capsys.readouterr().out

    def test_convert_missing_file_propagates(self, tmp_path: Path) -> None:
        # Pre-existing CLI contract: a missing --file raises out of main().
        with pytest.raises(FileNotFoundError):
            main(["convert", "--file", str(tmp_path / "missing.pdf"), "--output", str(tmp_path / "out")])

    def test_convert_missing_directory_propagates(self, tmp_path: Path) -> None:
        with pytest.raises(NotADirectoryError):
            main(["convert", "--input", str(tmp_path / "nope"), "--output", str(tmp_path / "out")])


# ---------------------------------------------------------------------------
# clean
# ---------------------------------------------------------------------------


class TestCleanMain:
    def test_clean_single_file_wiring(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        md = _write_md(tmp_path / "doc.md")
        monkeypatch.setattr("format_converter.cli.clean_markdown_file", _fake_clean_file)

        rc = main(["clean", "--file", str(md)])

        assert rc == 0
        assert f"Cleaned: {md.resolve()}" in capsys.readouterr().out
        assert md.read_text(encoding="utf-8") == "cleaned"

    def test_clean_directory_wiring(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        in_dir = tmp_path / "mds"
        in_dir.mkdir()
        a = _write_md(in_dir / "a.md")
        b = _write_md(in_dir / "b.md")
        monkeypatch.setattr("format_converter.cli.clean_markdown_directory", _fake_clean_directory)

        rc = main(["clean", "--input", str(in_dir)])

        assert rc == 0
        captured = capsys.readouterr().out
        assert "Cleaned 2 Markdown file(s)." in captured
        assert f"- {a}" in captured
        assert f"- {b}" in captured

    def test_clean_empty_directory_returns_zero(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        rc = main(["clean", "--input", str(empty_dir)])
        assert rc == 0
        assert "Cleaned 0 Markdown file(s)." in capsys.readouterr().out

    def test_clean_missing_file_propagates(self, tmp_path: Path) -> None:
        # Pre-existing CLI contract: a missing --file raises out of main().
        with pytest.raises(FileNotFoundError):
            main(["clean", "--file", str(tmp_path / "missing.md")])

    def test_clean_missing_directory_propagates(self, tmp_path: Path) -> None:
        with pytest.raises(NotADirectoryError):
            main(["clean", "--input", str(tmp_path / "nope")])


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------


class TestPipelineMain:
    def test_pipeline_wiring(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        pdf_dir = tmp_path / "pdfs"
        pdf_dir.mkdir()
        md_dir = tmp_path / "mds"
        monkeypatch.setattr("format_converter.cli.run_pipeline", _fake_run_pipeline)

        rc = main(["pipeline", "--pdf-dir", str(pdf_dir), "--md-dir", str(md_dir)])

        assert rc == 0
        captured = capsys.readouterr().out
        assert "Converted 2 PDF file(s)." in captured
        assert "Cleaned 2 Markdown file(s)." in captured

    def test_pipeline_missing_pdf_dir_propagates(self, tmp_path: Path) -> None:
        # Pre-existing CLI contract: a missing pdf-dir raises out of main().
        with pytest.raises(NotADirectoryError):
            main(["pipeline", "--pdf-dir", str(tmp_path / "nope"), "--md-dir", str(tmp_path / "mds")])


# ---------------------------------------------------------------------------
# marker
# ---------------------------------------------------------------------------


class TestMarkerMain:
    def test_marker_wiring(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        pdf = _write_pdf(tmp_path / "doc.pdf")
        out_dir = tmp_path / "out"
        received: dict = {}

        def fake_marker(pdf_path: Path, output_dir: Path, output_name: str | None = None) -> Path:
            received["output_dir"] = Path(output_dir)
            received["output_dir"].mkdir(parents=True, exist_ok=True)
            return received["output_dir"]

        monkeypatch.setattr("format_converter.cli.convert_pdf_with_marker", fake_marker)

        rc = main(["marker", str(pdf), "--output", str(out_dir)])

        assert rc == 0
        # Assert on the exact Path the fake received (no .resolve(), which can
        # differ on Windows when 8.3 short names or symlinks are involved).
        assert received["output_dir"] == out_dir
        assert f"Marker output saved to: {received['output_dir']}" in capsys.readouterr().out

    def test_marker_output_name_passed_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pdf = _write_pdf(tmp_path / "doc.pdf")
        seen: dict = {}

        def fake_marker(pdf_path: Path, output_dir: Path, output_name: str | None = None) -> Path:
            seen["output_name"] = output_name
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            return Path(output_dir)

        monkeypatch.setattr("format_converter.cli.convert_pdf_with_marker", fake_marker)
        rc = main(["marker", str(pdf), "--output", str(tmp_path / "out"), "--name", "custom"])
        assert rc == 0
        assert seen["output_name"] == "custom"

    def test_marker_error_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pdf = _write_pdf(tmp_path / "doc.pdf")

        def boom(pdf_path: Path, output_dir: Path, output_name: str | None = None) -> Path:
            raise FileNotFoundError(f"PDF file not found: {pdf_path}")

        monkeypatch.setattr("format_converter.cli.convert_pdf_with_marker", boom)
        # Pre-existing CLI contract: marker errors propagate out of main().
        with pytest.raises(FileNotFoundError):
            main(["marker", str(pdf), "--output", str(tmp_path / "out")])
