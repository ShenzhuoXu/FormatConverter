from __future__ import annotations

import argparse
from pathlib import Path

from .config import MARKDOWN_DIR, MARKER_OUTPUT_DIR, PDF_DIR
from .markdown_cleaner import clean_markdown_directory, clean_markdown_file
from .pdf_converter import convert_pdf_directory, convert_pdf_file, convert_pdf_with_marker
from .pipeline import run_pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="format-converter",
        description="Convert PDFs to Markdown and clean Markdown files.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    convert = commands.add_parser("convert", help="Convert PDFs with pymupdf4llm.")
    convert.add_argument("--input", "-i", type=Path, default=PDF_DIR, help="PDF directory.")
    convert.add_argument("--output", "-o", type=Path, default=MARKDOWN_DIR, help="Markdown output directory.")
    convert.add_argument("--file", type=Path, help="Convert one PDF instead of a directory.")
    convert.add_argument("--overwrite", action="store_true", help="Overwrite existing Markdown files.")

    marker = commands.add_parser("marker", help="Convert one PDF with marker-pdf.")
    marker.add_argument("file", type=Path, help="PDF file to convert.")
    marker.add_argument("--output", "-o", type=Path, default=MARKER_OUTPUT_DIR)
    marker.add_argument("--name", help="Output base name.")

    clean = commands.add_parser("clean", help="Clean Markdown files in place.")
    clean.add_argument("--input", "-i", type=Path, default=MARKDOWN_DIR, help="Markdown directory.")
    clean.add_argument("--file", type=Path, help="Clean one Markdown file instead of a directory.")
    clean.add_argument("--no-backup", action="store_true", help="Do not create .bak.md files.")
    clean.add_argument("--no-dedupe", action="store_true", help="Keep repeated paragraph blocks.")
    clean.add_argument("--flatten-lists", action="store_true", help="Join list blocks like normal paragraphs.")

    pipeline = commands.add_parser("pipeline", help="Convert PDFs, then clean Markdown.")
    pipeline.add_argument("--pdf-dir", type=Path, default=PDF_DIR)
    pipeline.add_argument("--md-dir", type=Path, default=MARKDOWN_DIR)
    pipeline.add_argument("--overwrite", action="store_true")
    pipeline.add_argument("--no-backup", action="store_true")
    pipeline.add_argument("--no-dedupe", action="store_true")
    pipeline.add_argument("--flatten-lists", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "convert":
        if args.file:
            output = convert_pdf_file(args.file, args.output, overwrite=args.overwrite)
            print(f"Converted: {output}")
            return 0

        outputs = convert_pdf_directory(args.input, args.output, overwrite=args.overwrite)
        print(f"Converted {len(outputs)} PDF file(s).")
        for output in outputs:
            print(f"- {output}")
        return 0

    if args.command == "marker":
        output = convert_pdf_with_marker(args.file, args.output, output_name=args.name)
        print(f"Marker output saved to: {output}")
        return 0

    if args.command == "clean":
        keep_lists = not args.flatten_lists
        dedupe = not args.no_dedupe
        backup = not args.no_backup

        if args.file:
            output = clean_markdown_file(args.file, keep_lists=keep_lists, dedupe=dedupe, backup=backup)
            print(f"Cleaned: {output}")
            return 0

        outputs = clean_markdown_directory(args.input, keep_lists=keep_lists, dedupe=dedupe, backup=backup)
        print(f"Cleaned {len(outputs)} Markdown file(s).")
        for output in outputs:
            print(f"- {output}")
        return 0

    if args.command == "pipeline":
        converted, cleaned = run_pipeline(
            args.pdf_dir,
            args.md_dir,
            overwrite=args.overwrite,
            keep_lists=not args.flatten_lists,
            dedupe=not args.no_dedupe,
            backup=not args.no_backup,
        )
        print(f"Converted {len(converted)} PDF file(s).")
        print(f"Cleaned {len(cleaned)} Markdown file(s).")
        return 0

    return 2
