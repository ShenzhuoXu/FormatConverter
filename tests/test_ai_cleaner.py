"""Tests for Markdown chunking and AI proofreading orchestration (offline)."""

from __future__ import annotations

import re
import time

import pytest

from format_converter.ai_cleaner import (
    DEFAULT_MAX_CHUNK_CHARS,
    SYSTEM_PROMPT,
    ChunkTooLargeError,
    clean_markdown_with_ai,
    split_into_chunks,
)
from format_converter.llm_client import (
    AuthenticationError,
    ConnectionFailedError,
    InvalidRequestError,
    ModelNotFoundError,
    RateLimitError,
    ServerError,
)

_FENCE_MARKER = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def _fence_marker_count(chunk: str) -> int:
    """Number of fenced-code-block opener/closer lines inside ``chunk``."""
    return sum(1 for line in chunk.split("\n") if _FENCE_MARKER.match(line))


class FakeClient:
    """Echoes each chunk back; records calls; can fail on a chosen chunk."""

    def __init__(self, *, fail_on: int | None = None, fail_exc: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.fail_on = fail_on
        self.fail_exc = fail_exc or RuntimeError("simulated provider failure")

    def complete(self, *, system: str, user: str, model: str) -> str:
        index = len(self.calls)
        self.calls.append({"system": system, "user": user, "model": model})
        if self.fail_on is not None and index == self.fail_on:
            raise self.fail_exc
        return f"[revised:{index}] {user}"


class EchoClient:
    """Pure-echo client: returns the chunk verbatim (whitespace included)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def complete(self, *, system: str, user: str, model: str) -> str:
        self.calls.append({"system": system, "user": user, "model": model})
        return user


class FlakyClient:
    """Client whose ``complete`` pops the next outcome (exception or text)."""

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def complete(self, *, system: str, user: str, model: str) -> str:
        self.calls.append({"system": system, "user": user, "model": model})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return str(outcome)


class TestSplitIntoChunks:
    def test_default_limit_is_12000(self) -> None:
        assert DEFAULT_MAX_CHUNK_CHARS == 12_000

    def test_blank_line_boundaries_split(self) -> None:
        text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
        chunks, separators = split_into_chunks(text, max_chars=30)
        assert chunks == ["First paragraph.\n", "Second paragraph.\n", "Third paragraph."]
        assert separators == ["", "\n", "\n", ""]

    def test_preserves_order(self) -> None:
        text = "\n\n".join(f"Paragraph {i} " + "x" * 10 for i in range(1, 6))
        chunks, _ = split_into_chunks(text, max_chars=25)
        # 5 paragraphs of length ~23 each with 25-char limit -> one per chunk.
        assert [c.startswith(f"Paragraph {i}") for i, c in enumerate(chunks, start=1)] == [
            True
        ] * 5
        assert len(chunks) == 5

    def test_does_not_split_mid_paragraph(self) -> None:
        soft_wrapped = "Line one of the same paragraph\nline two continues\nline three ends."
        chunks, _ = split_into_chunks(soft_wrapped, max_chars=10_000)
        assert len(chunks) == 1
        assert "line one" in chunks[0].lower()
        assert "line three" in chunks[0].lower()

    def test_paragraphs_are_packed_up_to_limit(self) -> None:
        text = ("a" * 30) + "\n\n" + ("b" * 30)
        chunks, _ = split_into_chunks(text, max_chars=50)
        # 30 + 2 + 30 = 62 > 50, so two chunks; each <= 50.
        assert len(chunks) == 2
        assert all(len(c) <= 50 for c in chunks)

    def test_oversized_unsplittable_block_raises(self) -> None:
        text = "x" * (DEFAULT_MAX_CHUNK_CHARS + 1)
        with pytest.raises(ChunkTooLargeError) as excinfo:
            split_into_chunks(text)
        message = str(excinfo.value)
        assert str(DEFAULT_MAX_CHUNK_CHARS) in message
        assert "smaller" in message.lower()

    def test_no_silent_truncation_of_oversized_block(self) -> None:
        """The oversized block must fail, not be silently cut."""
        text = "y" * (DEFAULT_MAX_CHUNK_CHARS + 500)
        with pytest.raises(ChunkTooLargeError):
            split_into_chunks(text)
        # Nothing may have been returned as a partial chunk.

    def test_crlf_line_endings_are_preserved(self) -> None:
        text = "One.\r\n\r\nTwo.\r\n\r\nThree."
        chunks, separators = split_into_chunks(text, max_chars=8)
        assert chunks == ["One.\r\n", "Two.\r\n", "Three."]
        assert separators == ["", "\r\n", "\r\n", ""]

    def test_lone_cr_line_ending_is_preserved(self) -> None:
        text = "One.\rTwo."
        chunks, _ = split_into_chunks(text, max_chars=100)
        assert chunks == ["One.\rTwo."]

    def test_lone_cr_blank_line_splits_paragraphs(self) -> None:
        # "one\r\rtwo" is two short paragraphs separated by a lone-CR blank
        # line, not one oversized line — it must not raise ChunkTooLargeError.
        chunks, separators = split_into_chunks("one\r\rtwo", max_chars=4)
        assert chunks == ["one\r", "two"]
        assert separators == ["", "\r", ""]

    def test_lone_cr_fenced_code_block_recognized(self) -> None:
        text = "```python\rcode\r\rmore\r```"
        chunks, _ = split_into_chunks(text, max_chars=1000)
        assert chunks == ["```python\rcode\r\rmore\r```"]

    def test_fence_interrupts_paragraph(self) -> None:
        # A fence directly after a paragraph (no blank line) must still be
        # recognized as its own block, not swallowed into the paragraph.
        text = "para\n```\ncode\n```\nend"
        chunks, _ = split_into_chunks(text, max_chars=13)
        assert chunks == ["para\n", "```\ncode\n```\n", "end"]

    def test_blank_only_input_yields_no_chunks_but_preserves_text(self) -> None:
        # No chunks, but the whole (whitespace-only) original survives as the
        # sole trailing separator.
        assert split_into_chunks("") == ([], [""])
        assert split_into_chunks("\n\n  \n\n") == ([], ["\n\n  \n\n"])
        assert split_into_chunks("\r\n  \r\n") == ([], ["\r\n  \r\n"])

    def test_multiple_blank_lines_preserved_between_blocks(self) -> None:
        chunks, separators = split_into_chunks("A.\n\n\n\nB.", max_chars=5)
        assert chunks == ["A.\n", "B."]
        assert separators == ["", "\n\n\n", ""]

    def test_leading_and_trailing_blank_lines_preserved_in_separators(self) -> None:
        chunks, separators = split_into_chunks("\n\nFirst.\n\n\n", max_chars=100)
        assert chunks == ["First.\n"]
        assert separators == ["\n\n", "\n\n"]

    def test_normal_paragraph_kept_verbatim_with_trailing_spaces(self) -> None:
        chunks, _ = split_into_chunks("line one  \nline two", max_chars=1000)
        assert chunks == ["line one  \nline two"]

    def test_fenced_code_block_with_blank_lines_is_atomic(self) -> None:
        fence = "```python\ncode1\n\ncode2\n```"
        chunks, _ = split_into_chunks("Before.\n\n" + fence + "\n\nAfter.", max_chars=40)
        assert any(fence in chunk for chunk in chunks)  # fence stays whole
        assert all(_fence_marker_count(c) % 2 == 0 for c in chunks)

    def test_tilde_fenced_code_block_with_blank_lines_is_atomic(self) -> None:
        fence = "~~~text\nline1\n\nline2\n~~~"
        chunks, _ = split_into_chunks(fence, max_chars=1000)
        assert chunks == [fence]

    def test_indented_code_block_with_blank_line_is_atomic(self) -> None:
        text = "    one\n    two\n\n    three"
        chunks, _ = split_into_chunks(text, max_chars=1000)
        assert chunks == ["    one\n    two\n\n    three"]

    def test_large_interior_blank_run_is_atomic_and_fast(self) -> None:
        # A long run of blank lines inside an indented code block must be
        # handled in O(run) time (a re-scan from every blank line would be
        # O(run^2) and take seconds), while staying byte-exact and atomic.
        blank_run = "\n" * 20_000
        text = "    one" + blank_run + "    two"
        start = time.perf_counter()
        chunks, _ = split_into_chunks(text, max_chars=1_000_000)
        elapsed = time.perf_counter() - start
        assert chunks == [text]  # whole block intact, including every blank line
        assert elapsed < 5.0  # O(run^2) regressions would blow far past this

    def test_indented_backticks_do_not_close_fence(self) -> None:
        # A 4-space-indented ``` line inside a fence is code, not the closer.
        text = "```python\n    ```\ncode\n```"
        chunks, _ = split_into_chunks(text, max_chars=1000)
        assert chunks == ["```python\n    ```\ncode\n```"]

    def test_oversized_fenced_code_block_raises(self) -> None:
        text = "```python\n" + "x" * (DEFAULT_MAX_CHUNK_CHARS + 1) + "\n```"
        with pytest.raises(ChunkTooLargeError):
            split_into_chunks(text)

    def test_no_chunk_contains_an_unclosed_fence(self) -> None:
        fence = "```python\ncode1\n\ncode2\n```"
        text = ("A" * 30) + "\n\n" + fence + "\n\n" + ("B" * 30)
        chunks, _ = split_into_chunks(text, max_chars=40)
        assert len(chunks) >= 2
        for chunk in chunks:
            assert _fence_marker_count(chunk) % 2 == 0, f"unclosed fence in chunk: {chunk!r}"


class TestCleanMarkdownWithAI:
    def test_returns_revised_text_in_order(self) -> None:
        text = "Alpha.\n\nBeta.\n\nGamma."
        client = FakeClient()
        result = clean_markdown_with_ai(text, client, model="m1", max_chars=10)
        assert result == "[revised:0] Alpha.\n\n[revised:1] Beta.\n\n[revised:2] Gamma."
        assert [c["user"] for c in client.calls] == ["Alpha.\n", "Beta.\n", "Gamma."]
        assert all(c["model"] == "m1" for c in client.calls)

    def test_system_prompt_is_sent(self) -> None:
        client = FakeClient()
        clean_markdown_with_ai("Hello.", client, model="m1")
        assert all(c["system"] == SYSTEM_PROMPT for c in client.calls)

    @pytest.mark.parametrize(
        "text",
        ["", "\n\n\n", "\r\n  \r\n", "   ", " \r\n\t"],
    )
    def test_blank_only_input_preserved_verbatim_and_not_sent_to_client(
        self, text: str
    ) -> None:
        client = FakeClient()
        assert clean_markdown_with_ai(text, client, model="m1") == text
        assert client.calls == []

    def test_any_chunk_failure_propagates(self) -> None:
        text = "First.\n\nSecond.\n\nThird."
        client = FakeClient(fail_on=1)
        with pytest.raises(RuntimeError):
            clean_markdown_with_ai(text, client, model="m1", max_chars=10)
        # No result object is returned on failure; caller must not write output.

    def test_oversized_block_fails_before_calling_client(self) -> None:
        text = "z" * (DEFAULT_MAX_CHUNK_CHARS + 1)
        client = FakeClient()
        with pytest.raises(ChunkTooLargeError):
            clean_markdown_with_ai(text, client, model="m1")
        assert client.calls == []

    def test_indented_code_block_preserved_verbatim_through_client(self) -> None:
        text = "    one\n    two\n\n    three"
        client = FakeClient()
        result = clean_markdown_with_ai(text, client, model="m1")
        # The chunk handed to the model keeps the four-space indentation.
        assert client.calls[0]["user"] == "    one\n    two\n\n    three"
        # The echoed output likewise keeps it; it must not collapse to prose.
        assert result == "[revised:0]     one\n    two\n\n    three"
        assert "\n    three" in result

    def test_echo_model_round_trip_preserves_whitespace(self) -> None:
        text = "\n\nAlpha.\n\n\nBeta.\r\n"
        client = EchoClient()
        result = clean_markdown_with_ai(text, client, model="m1", max_chars=8)
        assert client.calls[0]["user"] == "Alpha.\n"
        assert result == text  # leading, cross-chunk, and trailing whitespace kept

    def test_system_prompt_requires_preservation_and_forbids_changes(self) -> None:
        prompt = SYSTEM_PROMPT.lower()
        for required in ("language", "links", "code blocks", "tables", "lists", "headings"):
            assert required in prompt
        for forbidden in ("summarize", "translate", "delete", "expand", "explain"):
            assert forbidden in prompt
        assert "ocr" in prompt
        assert "markdown formatting" in prompt
        assert "only the revised markdown" in prompt

    def test_retries_retryable_chunk_failure_then_continues(self) -> None:
        client = FlakyClient([ConnectionFailedError("network"), "fixed"])
        sleeps: list[float] = []
        result = clean_markdown_with_ai(
            "Alpha.",
            client,
            model="m1",
            max_attempts=2,
            backoff_seconds=(0.25,),
            sleep=sleeps.append,
        )
        assert result == "fixed"
        assert sleeps == [0.25]
        assert len(client.calls) == 2

    def test_retries_exhausted_raises(self) -> None:
        client = FlakyClient([ConnectionFailedError("network"), ConnectionFailedError("network")])
        sleeps: list[float] = []
        with pytest.raises(ConnectionFailedError):
            clean_markdown_with_ai(
                "Alpha.",
                client,
                model="m1",
                max_attempts=2,
                backoff_seconds=(0.1, 0.2),
                sleep=sleeps.append,
            )
        assert len(client.calls) == 2  # exactly max_attempts, no extra attempt
        assert sleeps == [0.1]

    def test_permanent_error_does_not_retry(self) -> None:
        client = FlakyClient([AuthenticationError("401")])
        sleeps: list[float] = []
        with pytest.raises(AuthenticationError):
            clean_markdown_with_ai(
                "Alpha.",
                client,
                model="m1",
                max_attempts=4,
                backoff_seconds=(0.1, 0.2, 0.3),
                sleep=sleeps.append,
            )
        assert len(client.calls) == 1
        assert sleeps == []

    @pytest.mark.parametrize("exc", [ModelNotFoundError("404"), InvalidRequestError("400")])
    def test_http_4xx_errors_do_not_retry(self, exc: Exception) -> None:
        # 404/400-style provider errors are permanent and must not be retried,
        # even though they are provider-level LLMClientError subclasses.
        client = FlakyClient([exc])
        sleeps: list[float] = []
        with pytest.raises(type(exc)):
            clean_markdown_with_ai(
                "Alpha.",
                client,
                model="m1",
                max_attempts=4,
                backoff_seconds=(0.1, 0.2, 0.3),
                sleep=sleeps.append,
            )
        assert len(client.calls) == 1
        assert sleeps == []

    def test_server_error_is_retryable_and_rate_limit_is_retryable(self) -> None:
        # A retryable failure that is not ConnectionFailedError must also retry.
        client = FlakyClient([ServerError("500"), "ok"])
        sleeps: list[float] = []
        assert (
            clean_markdown_with_ai(
                "Alpha.",
                client,
                model="m1",
                max_attempts=3,
                backoff_seconds=(0.1, 0.2),
                sleep=sleeps.append,
            )
            == "ok"
        )
        assert len(client.calls) == 2

        client2 = FlakyClient([RateLimitError("429"), "ok"])
        sleeps2: list[float] = []
        assert (
            clean_markdown_with_ai(
                "Alpha.",
                client2,
                model="m1",
                max_attempts=3,
                backoff_seconds=(0.1, 0.2),
                sleep=sleeps2.append,
            )
            == "ok"
        )
        assert len(client2.calls) == 2

    def test_reports_progress_after_each_successful_chunk(self) -> None:
        progress: list[tuple[int, int]] = []
        client = EchoClient()
        clean_markdown_with_ai(
            "One.\n\nTwo.",
            client,
            model="m1",
            max_chars=6,
            progress=lambda current, total: progress.append((current, total)),
        )
        assert progress == [(1, 2), (2, 2)]

    def test_whitespace_only_input_does_not_call_progress(self) -> None:
        progress: list[tuple[int, int]] = []
        client = FakeClient()
        assert (
            clean_markdown_with_ai(
                "   ",
                client,
                model="m1",
                progress=lambda current, total: progress.append((current, total)),
            )
            == "   "
        )
        assert progress == []
        assert client.calls == []
