"""CLI tests: new ``ai-clean`` command and old commands still parsing (offline)."""

from __future__ import annotations

from pathlib import Path

import pytest

from format_converter.ai_cleaner import ChunkTooLargeError
from format_converter.cli import (
    EncodingError,
    NotMarkdownError,
    OverwriteError,
    ai_clean,
    build_parser,
    default_output_path,
    main,
)
from format_converter.llm_client import ServerError


class EchoClient:
    """Injected fake LLM client: echoes each chunk, can fail on a chosen chunk.

    Accepts positional args so it can stand in for OpenAICompatClient(provider, key).
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.calls: list[dict] = []
        self.fail_on: int | None = kwargs.get("fail_on")  # type: ignore[assignment]
        self.fail_exc: Exception = kwargs.get("fail_exc") or ServerError(  # type: ignore[assignment]
            "simulated provider failure"
        )

    def complete(self, *, system: str, user: str, model: str) -> str:
        index = len(self.calls)
        self.calls.append({"system": system, "user": user, "model": model})
        if self.fail_on is not None and index == self.fail_on:
            raise self.fail_exc
        return f"[revised] {user}"


def make_md(
    tmp_path: Path,
    name: str = "example.md",
    content: str = "# Hello\n\nBody.",
) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


class TestParser:
    def test_ai_clean_subcommand_listed_in_help(self) -> None:
        assert "ai-clean" in build_parser().format_help()

    @pytest.mark.parametrize(
        "argv",
        [
            ["ai-clean"],
            ["ai-clean", "--file", "x.md"],
            ["ai-clean", "--file", "x.md", "--provider", "orcarouter"],
            ["ai-clean", "--file", "x.md", "--model", "m"],
            ["ai-clean", "--provider", "orcarouter", "--model", "m"],
        ],
    )
    def test_required_arguments_enforced(self, argv: list[str]) -> None:
        with pytest.raises(SystemExit) as excinfo:
            build_parser().parse_args(argv)
        assert excinfo.value.code == 2

    def test_valid_ai_clean_args_parse(self, tmp_path: Path) -> None:
        file = make_md(tmp_path)
        args = build_parser().parse_args(
            [
                "ai-clean",
                "--file",
                str(file),
                "--provider",
                "orcarouter",
                "--model",
                "m1",
                "--output",
                str(tmp_path / "out.md"),
                "--overwrite",
            ]
        )
        assert args.command == "ai-clean"
        assert args.file == file
        assert args.provider == "orcarouter"
        assert args.model == "m1"
        assert args.output == tmp_path / "out.md"
        assert args.overwrite is True

    @pytest.mark.parametrize(
        "argv",
        [
            ["convert"],
            ["convert", "--file", "a.pdf", "--overwrite"],
            ["clean", "--file", "a.md", "--no-backup", "--no-dedupe", "--flatten-lists"],
            ["pipeline", "--overwrite", "--no-backup"],
            ["marker", "a.pdf"],
        ],
    )
    def test_old_commands_still_parse(self, argv: list[str]) -> None:
        args = build_parser().parse_args(argv)
        assert args.command == argv[0]


class TestDefaultOutputPath:
    def test_md_becomes_ai_md(self) -> None:
        assert default_output_path(Path("example.md")) == Path("example.ai.md")

    def test_other_suffix_gets_ai_md_appended(self) -> None:
        assert default_output_path(Path("notes.txt")) == Path("notes.txt.ai.md")

    def test_no_suffix(self) -> None:
        assert default_output_path(Path("example")) == Path("example.ai.md")


class TestAIClean:
    def test_writes_default_output_and_leaves_original(self, tmp_path: Path) -> None:
        src = make_md(tmp_path, content="Alpha.\n\nBeta.")
        out = ai_clean(src, "orcarouter", "m1", client=EchoClient())
        assert out == src.with_suffix(".ai.md")
        assert src.read_text(encoding="utf-8") == "Alpha.\n\nBeta."  # unchanged
        # Small inputs are a single chunk, so the fake echoes the whole text once.
        assert out.read_text(encoding="utf-8") == "[revised] Alpha.\n\nBeta."

    def test_custom_output(self, tmp_path: Path) -> None:
        src = make_md(tmp_path)
        custom = tmp_path / "sub" / "custom.md"
        out = ai_clean(src, "orcarouter", "m1", output=custom, client=EchoClient())
        assert out == custom.resolve()
        assert custom.is_file()

    def test_overwrite_protection_for_original_file(self, tmp_path: Path) -> None:
        src = make_md(tmp_path)
        with pytest.raises(OverwriteError):
            ai_clean(src, "orcarouter", "m1", output=src, client=EchoClient())
        # With --overwrite the write to the original file is allowed.
        out = ai_clean(src, "orcarouter", "m1", output=src, overwrite=True, client=EchoClient())
        assert out == src.resolve()
        assert "revised" in src.read_text(encoding="utf-8")

    def test_existing_output_requires_overwrite(self, tmp_path: Path) -> None:
        src = make_md(tmp_path)
        out = tmp_path / "existing.ai.md"
        out.write_text("old", encoding="utf-8")
        with pytest.raises(OverwriteError):
            ai_clean(src, "orcarouter", "m1", output=out, client=EchoClient())
        assert out.read_text(encoding="utf-8") == "old"  # untouched
        ai_clean(src, "orcarouter", "m1", output=out, overwrite=True, client=EchoClient())
        assert "revised" in out.read_text(encoding="utf-8")

    def test_directory_input_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(IsADirectoryError):
            ai_clean(tmp_path, "orcarouter", "m1", client=EchoClient())

    def test_missing_input_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            ai_clean(tmp_path / "nope.md", "orcarouter", "m1", client=EchoClient())

    def test_unknown_provider_raises(self, tmp_path: Path) -> None:
        src = make_md(tmp_path)
        with pytest.raises(Exception) as excinfo:
            ai_clean(src, "openai", "m1", client=EchoClient())
        assert "openai" in str(excinfo.value)

    def test_missing_key_raises_without_injected_client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
        src = make_md(tmp_path)
        with pytest.raises(Exception) as excinfo:
            ai_clean(src, "orcarouter", "m1")
        assert "ORCAROUTER_API_KEY" in str(excinfo.value)

    def test_failure_does_not_write_output(self, tmp_path: Path) -> None:
        src = make_md(tmp_path, content="One.\n\nTwo.\n\nThree.")
        with pytest.raises(ServerError):
            ai_clean(src, "orcarouter", "m1", client=EchoClient(fail_on=0))
        assert not (tmp_path / "example.ai.md").exists()
        assert src.read_text(encoding="utf-8") == "One.\n\nTwo.\n\nThree."

    def test_oversized_block_fails_without_writing(self, tmp_path: Path) -> None:
        src = make_md(tmp_path, content="x" * 20_000)
        with pytest.raises(ChunkTooLargeError):
            ai_clean(src, "orcarouter", "m1", client=EchoClient())
        assert not (tmp_path / "example.ai.md").exists()

    def test_non_markdown_input_rejected(self, tmp_path: Path) -> None:
        txt = tmp_path / "notes.txt"
        txt.write_text("hi", encoding="utf-8")
        with pytest.raises(NotMarkdownError) as excinfo:
            ai_clean(txt, "orcarouter", "m1", client=EchoClient())
        assert ".md" in str(excinfo.value)
        assert not (tmp_path / "notes.ai.md").exists()

    def test_uppercase_md_extension_accepted(self, tmp_path: Path) -> None:
        md = tmp_path / "notes.MD"
        md.write_text("# Hi\n\nBody.", encoding="utf-8")
        out = ai_clean(md, "orcarouter", "m1", client=EchoClient())
        assert out == (tmp_path / "notes.ai.md").resolve()
        assert out.is_file()

    def test_crlf_input_preserves_crlf_in_output(self, tmp_path: Path) -> None:
        src = tmp_path / "crlf.md"
        src.write_bytes(b"Title.\r\n\r\nBody.\r\n")
        out = ai_clean(src, "orcarouter", "m1", client=EchoClient())
        assert out.read_bytes() == b"[revised] Title.\r\n\r\nBody.\r\n"
        assert src.read_bytes() == b"Title.\r\n\r\nBody.\r\n"  # original untouched

    def test_non_utf8_input_raises_encoding_error(self, tmp_path: Path) -> None:
        src = tmp_path / "gbk.md"
        src.write_bytes("中文内容".encode("gbk"))
        with pytest.raises(EncodingError) as excinfo:
            ai_clean(src, "orcarouter", "m1", client=EchoClient())
        assert "UTF-8" in str(excinfo.value)
        assert not (tmp_path / "gbk.ai.md").exists()

    def test_whitespace_only_input_written_verbatim_without_client(self, tmp_path: Path) -> None:
        # Blank-only Markdown has nothing to proofread: the original bytes must
        # be written out exactly (newline style included) and the LLM client
        # must never be called.
        src = tmp_path / "blank.md"
        src.write_bytes(b" \r\n\t \r\n")
        calls: list[dict] = []

        class NoopClient:
            def complete(self, *, system: str, user: str, model: str) -> str:  # pragma: no cover
                calls.append({"system": system, "user": user, "model": model})
                return "MUST NOT BE USED"

        out = ai_clean(src, "orcarouter", "m1", client=NoopClient())
        assert out == src.with_suffix(".ai.md")
        assert out.read_bytes() == b" \r\n\t \r\n"
        assert src.read_bytes() == b" \r\n\t \r\n"  # original untouched
        assert calls == []


class TestMainAIClean:
    def test_success_returns_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        src = make_md(tmp_path)
        monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-test")
        monkeypatch.setattr("format_converter.cli.OpenAICompatClient", EchoClient)
        rc = main(
            ["ai-clean", "--file", str(src), "--provider", "orcarouter", "--model", "m1"]
        )
        assert rc == 0
        captured = capsys.readouterr()
        assert "AI proofread" in captured.out
        assert (tmp_path / "example.ai.md").exists()

    def test_unknown_provider_returns_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        src = make_md(tmp_path)
        rc = main(
            ["ai-clean", "--file", str(src), "--provider", "openai", "--model", "m1"]
        )
        assert rc == 1
        assert "openai" in capsys.readouterr().err

    def test_missing_key_returns_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
        src = make_md(tmp_path)
        rc = main(
            ["ai-clean", "--file", str(src), "--provider", "orcarouter", "--model", "m1"]
        )
        assert rc == 1
        assert "ORCAROUTER_API_KEY" in capsys.readouterr().err

    def test_overwrite_protection_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        src = make_md(tmp_path)
        monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-test")
        monkeypatch.setattr("format_converter.cli.OpenAICompatClient", EchoClient)
        rc = main(
            [
                "ai-clean",
                "--file",
                str(src),
                "--provider",
                "orcarouter",
                "--model",
                "m1",
                "--output",
                str(src),
            ]
        )
        assert rc == 1
        assert "--overwrite" in capsys.readouterr().err

    def test_non_markdown_input_reports_error_without_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        txt = tmp_path / "notes.txt"
        txt.write_text("hi", encoding="utf-8")
        monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-test")
        monkeypatch.setattr("format_converter.cli.OpenAICompatClient", EchoClient)
        rc = main(
            ["ai-clean", "--file", str(txt), "--provider", "orcarouter", "--model", "m1"]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert ".md" in err
        assert "Traceback" not in err
        assert not (tmp_path / "notes.ai.md").exists()

    def test_non_utf8_input_reports_encoding_error_without_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        src = tmp_path / "gbk.md"
        src.write_bytes("中文内容".encode("gbk"))
        monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-test")
        monkeypatch.setattr("format_converter.cli.OpenAICompatClient", EchoClient)
        rc = main(
            ["ai-clean", "--file", str(src), "--provider", "orcarouter", "--model", "m1"]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "UTF-8" in err
        assert "Traceback" not in err
        assert not (tmp_path / "gbk.ai.md").exists()
