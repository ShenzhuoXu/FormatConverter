"""Markdown chunking and orchestration for optional AI proofreading.

The actual LLM call happens through the :class:`format_converter.llm_client.LLMClient`
protocol. This module never touches the OpenAI SDK directly and never makes
real network calls, so it is fully testable offline with a fake client.

Chunking rules:

- Fenced (`` ``` `` / `` ~~~ ``) and indented code blocks are atomic units:
  they are never split across chunks, even when they contain blank lines.
  A code block that alone exceeds ``DEFAULT_MAX_CHUNK_CHARS`` raises
  :class:`ChunkTooLargeError`; it is never truncated.
- Normal Markdown text is split at blank-line boundaries and packed greedily.
- Whitespace is preserved exactly: newline styles (LF / CRLF / lone CR) are
  never normalized, and the leading / trailing / cross-chunk separators are
  kept and used to reassemble the model's revised chunks. Nothing is stripped.

``split_into_chunks`` returns ``(chunks, separators)`` where
``separators[i]`` is the exact separator that precedes ``chunks[i]`` and the
final element is the trailing separator, so reassembling the original text is
``separators[0] + chunks[0] + separators[1] + ... + separators[-1]``.
"""

from __future__ import annotations

import re

from .llm_client import LLMClient

DEFAULT_MAX_CHUNK_CHARS = 12_000

# Fixed instruction sent with every chunk. Models must preserve the original
# content and only fix obvious OCR, line-break, and Markdown formatting issues.
SYSTEM_PROMPT = (
    "You are a Markdown proofreader. Revise the Markdown text below to fix ONLY "
    "obvious OCR errors, broken line breaks, and Markdown formatting problems.\n"
    "\n"
    "You MUST preserve the original language, facts, links, code blocks, tables, "
    "lists, and the semantics of headings exactly as given.\n"
    "You MUST fix only clear OCR, line-break, and Markdown formatting issues.\n"
    "You MUST return ONLY the revised Markdown text, with nothing else.\n"
    "\n"
    "You MUST NOT summarize, translate, delete, expand, explain, or add any extra text."
)

# A fence opener/closer may be indented by at most three spaces (CommonMark);
# deeper indentation means indented code, not a fence.
_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*\r?$")


class AICleanError(Exception):
    """Base error for AI proofreading."""


class ChunkTooLargeError(AICleanError):
    """A single block cannot be split further but exceeds the chunk limit."""

    def __init__(self, length: int, max_chars: int) -> None:
        self.length = length
        self.max_chars = max_chars
        super().__init__(
            f"A single paragraph or code block in this Markdown file is {length} "
            f"characters, which exceeds the {max_chars}-character chunk limit and "
            "cannot be split further. Split the file into smaller sections and try again."
        )


_LINE_BREAK = re.compile(r"\r\n|\r|\n")


def _split_lines(text: str) -> list[str]:
    """Split ``text`` into logical lines, keeping each original terminator.

    Handles LF, CRLF, and lone CR losslessly: ``\r\n`` is a single terminator,
    while a lone ``\r`` or ``\n`` terminates its own line. Concatenating the
    returned lines reproduces ``text`` exactly, so newline styles are never
    normalized.
    """
    lines: list[str] = []
    start = 0
    for match in _LINE_BREAK.finditer(text):
        lines.append(text[start : match.end()])
        start = match.end()
    lines.append(text[start:])
    return lines


def _is_indented(line: str) -> bool:
    """True for a line that starts an indented code block (tab or 4+ spaces)."""
    return line.startswith("\t") or line.startswith("    ")


def _scan_blocks(text: str) -> tuple[list[tuple[str, str]], str]:
    """Scan ``text`` into ``([(separator, block), ...], trailing_separator)``.

    Each ``block`` is an atomic unit — a fenced code block, an indented code
    block, or a normal paragraph — with its original content preserved
    verbatim (original newlines, no stripping). ``separator`` is the exact
    blank-line whitespace preceding the block. The returned trailing
    separator is the exact whitespace after the last block. Together they
    reproduce the input exactly:
    ``separator[0] + block[0] + separator[1] + block[1] + ... + trailing``.

    Fenced and indented code blocks are never split, including their interior
    blank lines. Normal paragraphs are maximal runs of non-blank lines.
    """
    lines = _split_lines(text)
    blocks: list[tuple[str, str]] = []
    pending = ""
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        if not line.strip():
            pending += line
            i += 1
            continue

        separator = pending
        pending = ""

        opening = _FENCE_OPEN.match(line)
        if opening:
            fence_char = opening.group(1)[0]
            fence_len = len(opening.group(1))
            content = [line]
            i += 1
            while i < n:
                content.append(lines[i])
                closing = _FENCE_CLOSE.match(lines[i])
                if closing and closing.group(1)[0] == fence_char and len(closing.group(1)) >= fence_len:
                    i += 1
                    break
                i += 1
            blocks.append((separator, "".join(content)))
            continue

        if _is_indented(line):
            content = []
            while i < n:
                current = lines[i]
                if _is_indented(current):
                    content.append(current)
                    i += 1
                elif not current.strip():
                    # A blank line belongs to the code block only if the next
                    # non-blank line is still indented (an interior blank).
                    j = i
                    while j < n and not lines[j].strip():
                        j += 1
                    if j < n and _is_indented(lines[j]):
                        content.append(current)
                        i += 1
                    else:
                        break
                else:
                    break
            blocks.append((separator, "".join(content)))
            continue

        # Normal paragraph: all consecutive non-blank lines, kept verbatim.
        # A fenced code block interrupts a paragraph (CommonMark), so it is
        # never swallowed into the paragraph — it must stay its own block.
        content = []
        while i < n and lines[i].strip():
            if _FENCE_OPEN.match(lines[i]):
                break
            content.append(lines[i])
            i += 1
        blocks.append((separator, "".join(content)))

    return blocks, pending


def _join_blocks(blocks: list[tuple[str, str]]) -> str:
    """Join a chunk's blocks using their internal separators.

    The separator before the chunk's first block is not included; it is the
    chunk-boundary separator returned separately by :func:`split_into_chunks`.
    """
    if not blocks:
        return ""
    return blocks[0][1] + "".join(separator + block for separator, block in blocks[1:])


def split_into_chunks(
    text: str, max_chars: int = DEFAULT_MAX_CHUNK_CHARS
) -> tuple[list[str], list[str]]:
    """Split Markdown into ``(chunks, separators)``, never breaking code blocks.

    Fenced and indented code blocks (including interior blank lines) are
    atomic. Any block that alone exceeds ``max_chars`` raises
    :class:`ChunkTooLargeError` instead of being truncated or split. Normal
    paragraphs are packed greedily at blank-line boundaries, preserving order.

    ``separators`` has one more element than ``chunks``: ``separators[i]`` is
    the exact original whitespace before ``chunks[i]`` and ``separators[-1]``
    is the trailing whitespace, so reassembly preserves the original exactly.
    For whitespace-only input there are no chunks and the single separator is
    the entire original text.
    """
    blocks, trailing = _scan_blocks(text)
    if not blocks:
        # Whitespace-only input: nothing to proofread, but the complete
        # original text is preserved as the sole trailing separator so
        # reassembly (and clean_markdown_with_ai) is byte-exact.
        return [], [trailing]

    for _, block in blocks:
        if len(block) > max_chars:
            raise ChunkTooLargeError(len(block), max_chars)

    chunks: list[str] = []
    separators: list[str] = []
    current: list[tuple[str, str]] = []
    current_len = 0

    for separator, block in blocks:
        block_len = len(separator) + len(block)
        if current and current_len + block_len > max_chars:
            chunks.append(_join_blocks(current))
            current = [(separator, block)]
            current_len = block_len
            separators.append(separator)
        elif current:
            current.append((separator, block))
            current_len += block_len
        else:
            current = [(separator, block)]
            current_len = block_len
            separators.append(separator)

    if current:
        chunks.append(_join_blocks(current))
    separators.append(trailing)
    return chunks, separators


def clean_markdown_with_ai(
    text: str,
    client: LLMClient,
    *,
    model: str,
    system_prompt: str = SYSTEM_PROMPT,
    max_chars: int = DEFAULT_MAX_CHUNK_CHARS,
) -> str:
    """Proofread ``text`` chunk by chunk and return the revised Markdown.

    Each chunk is sent to ``client.complete`` in order. If any chunk fails
    (or chunking itself raises :class:`ChunkTooLargeError`), the exception
    propagates and no result is returned — callers must write the output file
    only after this function returns successfully.

    Revised chunks are reassembled with the *original* separators (leading,
    cross-chunk, and trailing), so an echo model reproduces the input's
    whitespace byte-for-byte and code blocks are never merged or broken.
    Whitespace-only input is returned verbatim without calling the client.
    """
    chunks, separators = split_into_chunks(text, max_chars=max_chars)
    if not chunks:
        # Whitespace-only input has no proofreadable content; return it
        # verbatim rather than clearing it.
        return text

    revised = [
        client.complete(system=system_prompt, user=chunk, model=model)
        for chunk in chunks
    ]
    parts = []
    for i, revised_chunk in enumerate(revised):
        parts.append(separators[i])
        parts.append(revised_chunk)
    parts.append(separators[len(revised)])
    return "".join(parts)
