from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .ai_cleaner import AICleanError, clean_markdown_with_ai
from .config import MARKDOWN_DIR, MARKER_OUTPUT_DIR, PDF_DIR
from .llm_client import LLMClient, LLMClientError, OpenAICompatClient
from .markdown_cleaner import clean_markdown_directory, clean_markdown_file
from .pdf_converter import convert_pdf_directory, convert_pdf_file, convert_pdf_with_marker
from .pipeline import run_pipeline
from .providers import ProviderError, get_api_key, get_provider


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="format-converter",
        description=(
            "Convert PDFs to Markdown, clean Markdown files, and optionally "
            "proofread a single Markdown file with an AI provider."
        ),
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

    ai_clean = commands.add_parser(
        "ai-clean",
        help="Proofread ONE Markdown file with an AI provider (opt-in; you must provide your own API key).",
    )
    ai_clean.add_argument(
        "--file",
        type=Path,
        required=True,
        help="Single Markdown file to proofread.",
    )
    ai_clean.add_argument(
        "--provider",
        required=True,
        help="Provider preset. First release only accepts 'orcarouter'.",
    )
    ai_clean.add_argument(
        "--model",
        required=True,
        help="Model name on the provider, e.g. a chat model offered by OrcaRouter.",
    )
    ai_clean.add_argument(
        "--output",
        type=Path,
        help="Output path (default: <name>.ai.md next to the input file).",
    )
    ai_clean.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow overwriting the original file or an existing output file.",
    )

    return parser


class OverwriteError(Exception):
    """Raised when a write would clobber a file without the --overwrite flag."""


class NotMarkdownError(Exception):
    """Raised when ``ai-clean`` receives an input file that is not a ``.md`` file."""


class EncodingError(Exception):
    """Raised when ``ai-clean`` cannot decode an input file as UTF-8."""


def default_output_path(path: Path) -> Path:
    """Default AI proofreading output: ``<name>.md`` -> ``<name>.ai.md``."""
    if path.suffix.lower() == ".md":
        return path.with_suffix(".ai.md")
    return path.with_suffix(path.suffix + ".ai.md")


def ai_clean(
    file: Path,
    provider: str,
    model: str,
    *,
    output: Path | None = None,
    overwrite: bool = False,
    client: LLMClient | None = None,
) -> Path:
    """Proofread one Markdown file with an AI provider and write the result.

    Returns the output path. The original file is never overwritten unless
    ``--overwrite`` is set, even when ``--output`` points at it. An existing
    output file also requires ``--overwrite``. Nothing is written if any chunk
    fails or the input is rejected.
    """
    provider_config = get_provider(provider)

    input_path = file.resolve()
    if input_path.is_dir():
        raise IsADirectoryError(
            f"Expected a single Markdown file, got a directory: {input_path}"
        )
    if input_path.suffix.lower() != ".md":
        raise NotMarkdownError(f"Expected a .md file, got: {input_path}")
    if not input_path.is_file():
        raise FileNotFoundError(f"Markdown file not found: {input_path}")

    if client is None:
        api_key = get_api_key(provider_config)
        client = OpenAICompatClient(provider_config, api_key)

    output_path = (output or default_output_path(input_path)).resolve()
    if output_path == input_path and not overwrite:
        raise OverwriteError(
            "--output points at the original file, which would overwrite it. "
            "Pass --overwrite to allow this."
        )
    if output_path.exists() and not overwrite:
        raise OverwriteError(
            f"Output file already exists: {output_path}. Pass --overwrite to replace it."
        )

    # newline="" keeps CRLF/CR line endings untranslated so the AI proofreader
    # (and therefore the output) preserves the input's newline style exactly.
    try:
        markdown = input_path.read_text(encoding="utf-8", newline="")
    except UnicodeDecodeError as exc:
        raise EncodingError(
            f"Could not decode {input_path} as UTF-8. "
            "The file uses a different text encoding; re-save it as UTF-8 and try again."
        ) from exc
    revised = clean_markdown_with_ai(markdown, client, model=model)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(revised, encoding="utf-8", newline="\n")
    return output_path


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

    if args.command == "ai-clean":
        try:
            output = ai_clean(
                args.file,
                args.provider,
                args.model,
                output=args.output,
                overwrite=args.overwrite,
            )
        except (ProviderError, LLMClientError, AICleanError, EncodingError, OverwriteError, NotMarkdownError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except (FileNotFoundError, IsADirectoryError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"AI proofread: {output}")
        return 0

    return 2
