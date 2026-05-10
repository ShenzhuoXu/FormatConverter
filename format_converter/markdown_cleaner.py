from __future__ import annotations

import re
import shutil
from pathlib import Path


STRUCTURAL_PREFIXES = ("#", ">", "-", "*", "+", "|")
NUMBERED_LIST_PATTERN = re.compile(r"^\s*\d+[\.\)]")
LIST_PATTERN = re.compile(r"^\s*([\-\*\+\u2022]|\d+[\.\)]|\([a-zA-Z0-9]+\)|[a-zA-Z]\.)")
FENCED_CODE_PATTERN = re.compile(r"(```[\s\S]*?```)")


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def collapse_blank_lines(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def should_join_lines(previous_line: str, current_line: str) -> bool:
    """Decide whether two Markdown lines are probably one wrapped paragraph."""
    if not previous_line.strip() or not current_line.strip():
        return False
    if previous_line.lstrip().startswith(STRUCTURAL_PREFIXES):
        return False
    if current_line.lstrip().startswith(STRUCTURAL_PREFIXES):
        return False
    if NUMBERED_LIST_PATTERN.match(current_line):
        return False
    return True


def join_soft_wrapped_lines(text: str) -> str:
    """Join soft-wrapped lines while preserving fenced code blocks."""
    parts = FENCED_CODE_PATTERN.split(normalize_newlines(text))

    for index, part in enumerate(parts):
        if part.startswith("```"):
            continue

        joined_lines: list[str] = []
        for line in part.split("\n"):
            if joined_lines and should_join_lines(joined_lines[-1], line):
                joined_lines[-1] = joined_lines[-1].rstrip() + " " + line.lstrip()
            else:
                joined_lines.append(line)

        parts[index] = "\n".join(joined_lines)

    return collapse_blank_lines("".join(parts))


def clean_markdown_text(text: str, keep_lists: bool = True, dedupe: bool = True) -> str:
    """Clean converted Markdown text.

    The cleaner removes repeated paragraph blocks and joins soft-wrapped prose.
    List blocks keep their internal line breaks by default.
    """
    paragraphs = re.split(r"\n\s*\n", normalize_newlines(text))
    seen: set[str] = set()
    cleaned_blocks: list[str] = []

    for paragraph in paragraphs:
        if not paragraph.strip():
            continue

        lines = paragraph.split("\n")
        has_list_line = any(LIST_PATTERN.match(line) for line in lines)

        if keep_lists and has_list_line:
            cleaned = "\n".join(line.rstrip() for line in lines).strip()
        else:
            cleaned = join_soft_wrapped_lines(paragraph).strip()

        if not cleaned:
            continue
        if dedupe and cleaned in seen:
            continue

        seen.add(cleaned)
        cleaned_blocks.append(cleaned)

    return collapse_blank_lines("\n\n".join(cleaned_blocks))


def backup_path_for(path: Path) -> Path:
    if path.suffix.lower() == ".md":
        return path.with_suffix(".bak.md")
    return path.with_suffix(path.suffix + ".bak")


def clean_markdown_file(
    path: Path,
    keep_lists: bool = True,
    dedupe: bool = True,
    backup: bool = True,
) -> Path:
    """Clean one Markdown file in place."""
    path = path.resolve()

    if not path.is_file():
        raise FileNotFoundError(f"Markdown file not found: {path}")

    if backup:
        shutil.copy2(path, backup_path_for(path))

    cleaned = clean_markdown_text(
        path.read_text(encoding="utf-8"),
        keep_lists=keep_lists,
        dedupe=dedupe,
    )
    path.write_text(cleaned, encoding="utf-8", newline="\n")
    return path


def clean_markdown_directory(
    input_dir: Path,
    keep_lists: bool = True,
    dedupe: bool = True,
    backup: bool = True,
) -> list[Path]:
    """Clean every non-backup Markdown file in a directory."""
    input_dir = input_dir.resolve()

    if not input_dir.is_dir():
        raise NotADirectoryError(f"Markdown directory not found: {input_dir}")

    markdown_files = sorted(
        path for path in input_dir.glob("*.md") if not path.name.endswith(".bak.md")
    )
    return [
        clean_markdown_file(path, keep_lists=keep_lists, dedupe=dedupe, backup=backup)
        for path in markdown_files
    ]
