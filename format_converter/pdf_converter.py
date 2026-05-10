from __future__ import annotations

from pathlib import Path


def convert_pdf_to_markdown(pdf_path: Path) -> str:
    """Return Markdown text converted from one PDF using pymupdf4llm."""
    import pymupdf4llm

    return pymupdf4llm.to_markdown(str(pdf_path))


def convert_pdf_file(pdf_path: Path, output_dir: Path, overwrite: bool = False) -> Path:
    """Convert one PDF to a same-name Markdown file in output_dir."""
    pdf_path = pdf_path.resolve()
    output_dir = output_dir.resolve()

    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{pdf_path.stem}.md"

    if output_path.exists() and not overwrite:
        return output_path

    markdown = convert_pdf_to_markdown(pdf_path)
    output_path.write_text(markdown, encoding="utf-8", newline="\n")
    return output_path


def convert_pdf_directory(input_dir: Path, output_dir: Path, overwrite: bool = False) -> list[Path]:
    """Convert every PDF under input_dir to Markdown files under output_dir."""
    input_dir = input_dir.resolve()

    if not input_dir.is_dir():
        raise NotADirectoryError(f"PDF directory not found: {input_dir}")

    return [
        convert_pdf_file(pdf_path, output_dir, overwrite=overwrite)
        for pdf_path in sorted(input_dir.glob("*.pdf"))
    ]


def convert_pdf_with_marker(pdf_path: Path, output_dir: Path, output_name: str | None = None) -> Path:
    """Convert one PDF using marker-pdf and save marker's output bundle."""
    from marker.converters.pdf import PdfConverter
    from marker.models import create_model_dict
    from marker.output import save_output

    pdf_path = pdf_path.resolve()
    output_dir = output_dir.resolve()

    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    rendered = PdfConverter(artifact_dict=create_model_dict())(str(pdf_path))
    save_output(rendered, str(output_dir), output_name or pdf_path.stem)
    return output_dir
