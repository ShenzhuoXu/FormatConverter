from __future__ import annotations

from pathlib import Path

from .markdown_cleaner import clean_markdown_directory
from .pdf_converter import convert_pdf_directory


def run_pipeline(
    pdf_dir: Path,
    markdown_dir: Path,
    overwrite: bool = False,
    keep_lists: bool = True,
    dedupe: bool = True,
    backup: bool = True,
) -> tuple[list[Path], list[Path]]:
    """Convert PDFs to Markdown, then clean the Markdown output."""
    converted = convert_pdf_directory(pdf_dir, markdown_dir, overwrite=overwrite)
    cleaned = clean_markdown_directory(
        markdown_dir,
        keep_lists=keep_lists,
        dedupe=dedupe,
        backup=backup,
    )
    return converted, cleaned
