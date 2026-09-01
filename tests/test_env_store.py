"""Tests for format_converter.env_store (byte-level .env parser).

Fully offline: all reads/writes go to a ``tmp_path`` file, and the autouse
conftest fixture keeps the real project-root ``.env`` out of the picture.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from format_converter import env_store


def _write(tmp_path: Path, content: bytes) -> Path:
    p = tmp_path / ".env"
    p.write_bytes(content)
    return p


class TestReadEnvKey:
    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert env_store.read_env_key(tmp_path / ".env") is None

    def test_empty_file_returns_none(self, tmp_path: Path) -> None:
        p = _write(tmp_path, b"")
        assert env_store.read_env_key(p) is None

    def test_comments_only_returns_none(self, tmp_path: Path) -> None:
        p = _write(tmp_path, b"# ORCAROUTER_API_KEY=sk-test-abc\n# another comment\n")
        assert env_store.read_env_key(p) is None

    def test_unrelated_keys_ignored(self, tmp_path: Path) -> None:
        p = _write(tmp_path, b"FOO=bar\nBAZ=qux\n")
        assert env_store.read_env_key(p) is None

    def test_simple_unquoted_value(self, tmp_path: Path) -> None:
        p = _write(tmp_path, b"ORCAROUTER_API_KEY=sk-test-abc\n")
        assert env_store.read_env_key(p) == "sk-test-abc"

    def test_spaces_around_equals(self, tmp_path: Path) -> None:
        p = _write(tmp_path, b"ORCAROUTER_API_KEY   =   sk-test-abc\n")
        assert env_store.read_env_key(p) == "sk-test-abc"

    def test_quoted_double_value(self, tmp_path: Path) -> None:
        p = _write(tmp_path, b'ORCAROUTER_API_KEY="sk-test-abc"\n')
        assert env_store.read_env_key(p) == "sk-test-abc"

    def test_quoted_single_value(self, tmp_path: Path) -> None:
        p = _write(tmp_path, b"ORCAROUTER_API_KEY='sk-test-abc'\n")
        assert env_store.read_env_key(p) == "sk-test-abc"

    def test_first_non_empty_wins(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path,
            b"ORCAROUTER_API_KEY=" + b"\n" + b'ORCAROUTER_API_KEY="sk-test-real"\n',
        )
        assert env_store.read_env_key(p) == "sk-test-real"

    def test_empty_blank_or_quoted_empty_value_is_none(self, tmp_path: Path) -> None:
        for content in (
            b"ORCAROUTER_API_KEY=" + b"\n",
            b"ORCAROUTER_API_KEY=   " + b"\n",
            b'ORCAROUTER_API_KEY=""' + b"\n",
            b"ORCAROUTER_API_KEY=''" + b"\n",
        ):
            assert env_store.read_env_key(_write(tmp_path, content)) is None

    def test_inline_hash_in_value_is_literal(self, tmp_path: Path) -> None:
        # Documented boundary: inline `#` is literal, not a comment.
        p = _write(tmp_path, b"ORCAROUTER_API_KEY=sk-test-abc#part\n")
        assert env_store.read_env_key(p) == "sk-test-abc#part"

    def test_crlf_value_reads_clean(self, tmp_path: Path) -> None:
        p = _write(tmp_path, b'ORCAROUTER_API_KEY="sk-test-abc"\r\n')
        assert env_store.read_env_key(p) == "sk-test-abc"

    def test_bom_and_leading_spaces_tolerated(self, tmp_path: Path) -> None:
        p = _write(tmp_path, b"\xef\xbb\xbf  ORCAROUTER_API_KEY=sk-test-abc\n")
        assert env_store.read_env_key(p) == "sk-test-abc"


class TestWriteEnvKey:
    def test_write_creates_new_file(self, tmp_path: Path) -> None:
        p = tmp_path / ".env"
        env_store.write_env_key("sk-test-abc", p)
        assert p.read_bytes() == b'ORCAROUTER_API_KEY="sk-test-abc"\n'

    def test_write_raises_on_empty_value(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            env_store.write_env_key("", tmp_path / ".env")

    def test_write_rejects_embedded_line_break(self, tmp_path: Path) -> None:
        # A \n/\r would corrupt the .env layout with a permanent stray line.
        p = tmp_path / ".env"
        for bad in ("abc\ndefghi", "abc\rdefghi"):
            with pytest.raises(ValueError):
                env_store.write_env_key(bad, p)
            assert not p.exists()

    def test_write_replaces_value_preserving_other_lines(self, tmp_path: Path) -> None:
        p = _write(tmp_path, b"# top\nFOO=bar\nORCAROUTER_API_KEY=sk-test-old\nBAZ=qux\n")
        env_store.write_env_key("sk-test-new", p)
        assert p.read_bytes() == b'# top\nFOO=bar\nORCAROUTER_API_KEY="sk-test-new"\nBAZ=qux\n'

    def test_write_dedupes_duplicate_target_lines(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path,
            b"FOO=bar\nORCAROUTER_API_KEY=sk-test-old1\nBAZ=qux\nORCAROUTER_API_KEY=sk-test-old2\n",
        )
        env_store.write_env_key("sk-test-new", p)
        assert p.read_bytes() == b'FOO=bar\nORCAROUTER_API_KEY="sk-test-new"\nBAZ=qux\n'

    def test_write_appends_when_absent(self, tmp_path: Path) -> None:
        p = _write(tmp_path, b"FOO=bar\nBAZ=qux\n")
        env_store.write_env_key("sk-test-abc", p)
        assert p.read_bytes() == b'FOO=bar\nBAZ=qux\nORCAROUTER_API_KEY="sk-test-abc"\n'

    def test_write_appends_to_no_trailing_newline_file(self, tmp_path: Path) -> None:
        p = _write(tmp_path, b"FOO=bar")
        env_store.write_env_key("sk-test-abc", p)
        assert p.read_bytes() == b'FOO=bar\nORCAROUTER_API_KEY="sk-test-abc"\n'

    def test_write_preserves_crlf_for_untouched_lines(self, tmp_path: Path) -> None:
        p = _write(tmp_path, b"FOO=bar\r\nORCAROUTER_API_KEY=sk-test-old\r\nBAZ=qux\r\n")
        env_store.write_env_key("sk-test-new", p)
        assert (
            p.read_bytes()
            == b'FOO=bar\r\nORCAROUTER_API_KEY="sk-test-new"\r\nBAZ=qux\r\n'
        )

    def test_write_appends_crlf_when_file_is_crlf(self, tmp_path: Path) -> None:
        p = _write(tmp_path, b"FOO=bar\r\n")
        env_store.write_env_key("sk-test-abc", p)
        assert p.read_bytes() == b'FOO=bar\r\nORCAROUTER_API_KEY="sk-test-abc"\r\n'

    def test_write_preserves_non_utf8_bytes(self, tmp_path: Path) -> None:
        p = _write(tmp_path, b"FOO=\xff\xfe bar\nORCAROUTER_API_KEY=sk-test-old\n")
        env_store.write_env_key("sk-test-new", p)
        assert p.read_bytes() == b'FOO=\xff\xfe bar\nORCAROUTER_API_KEY="sk-test-new"\n'

    def test_write_failure_leaves_original_and_removes_temp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        p = _write(tmp_path, b"FOO=bar\n")

        def _boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(env_store.os, "replace", _boom)
        with pytest.raises(OSError):
            env_store.write_env_key("sk-test-abc", p)
        assert p.read_bytes() == b"FOO=bar\n"
        leftovers = list(tmp_path.glob(".env.*.tmp"))
        assert leftovers == []

    def test_atomic_write_retries_transient_permission_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Windows antivirus/indexer can briefly lock a just-written file,
        # surfacing as a one-shot PermissionError on os.replace; the write
        # must ride that out and still succeed.
        p = tmp_path / ".env"
        real_replace = env_store.os.replace
        calls = {"n": 0}

        def _flaky(src, dst):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError(32, "file in use by another process")
            return real_replace(src, dst)

        monkeypatch.setattr(env_store.os, "replace", _flaky)
        env_store.write_env_key("sk-test-abc", p)
        assert calls["n"] >= 2
        assert p.read_bytes() == b'ORCAROUTER_API_KEY="sk-test-abc"\n'
        assert list(tmp_path.glob(".env.*.tmp")) == []

    def test_write_retries_transient_read_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A one-shot read lock (another writer's replace) must be retried, not
        # mistaken for a missing file that would clobber existing lines.
        p = _write(tmp_path, b"FOO=bar\nORCAROUTER_API_KEY=sk-test-old\n")
        real_read = Path.read_bytes
        calls = {"n": 0}

        def _flaky(self_):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError(13, "sharing violation")
            return real_read(self_)

        monkeypatch.setattr(Path, "read_bytes", _flaky)
        env_store.write_env_key("sk-test-abc", p)
        assert calls["n"] >= 2
        assert p.read_bytes() == b'FOO=bar\nORCAROUTER_API_KEY="sk-test-abc"\n'

    def test_write_persistent_read_error_propagates_without_clobber(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A persistently unreadable .env must propagate, never be treated as
        # "missing" and replaced with a key-only file.
        p = _write(tmp_path, b"FOO=bar\n")

        def _read_boom(*_args):
            raise PermissionError(13, "sharing violation")

        monkeypatch.setattr(Path, "read_bytes", _read_boom)
        with pytest.raises(OSError):
            env_store.write_env_key("sk-test-abc", p)
        assert p.open("rb").read() == b"FOO=bar\n"


class TestDeleteEnvKey:
    def test_delete_removes_only_target_lines(self, tmp_path: Path) -> None:
        p = _write(tmp_path, b"FOO=bar\nORCAROUTER_API_KEY=sk-test-old\nBAZ=qux\n")
        env_store.delete_env_key(p)
        assert p.read_bytes() == b"FOO=bar\nBAZ=qux\n"

    def test_delete_idempotent_on_missing_file(self, tmp_path: Path) -> None:
        p = tmp_path / ".env"
        env_store.delete_env_key(p)  # must not raise
        assert not p.exists()

    def test_delete_noop_when_no_target(self, tmp_path: Path) -> None:
        p = _write(tmp_path, b"FOO=bar\n")
        env_store.delete_env_key(p)
        assert p.read_bytes() == b"FOO=bar\n"

    def test_delete_only_line_leaves_empty_file(self, tmp_path: Path) -> None:
        p = _write(tmp_path, b'ORCAROUTER_API_KEY="sk-test-x"\n')
        env_store.delete_env_key(p)
        assert p.exists()
        assert p.read_bytes() == b""

    def test_delete_preserves_crlf_and_non_utf8_bytes(self, tmp_path: Path) -> None:
        p = _write(tmp_path, b"FOO=\xff bar\r\nORCAROUTER_API_KEY=sk-test-old\r\nBAZ=qux\r\n")
        env_store.delete_env_key(p)
        assert p.read_bytes() == b"FOO=\xff bar\r\nBAZ=qux\r\n"

    def test_delete_removes_multiple_targets(self, tmp_path: Path) -> None:
        p = _write(tmp_path, b"ORCAROUTER_API_KEY=sk-test-a\nFOO=bar\nORCAROUTER_API_KEY=sk-test-b\n")
        env_store.delete_env_key(p)
        assert p.read_bytes() == b"FOO=bar\n"


class TestKeyStatus:
    def test_env_set_wins_over_dotenv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-test-env")
        env_store.write_env_key("sk-test-dotenv", tmp_path / ".env")
        assert env_store.key_status(path=tmp_path / ".env") == (True, "environment")

    def test_dotenv_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
        env_store.write_env_key("sk-test-dotenv", tmp_path / ".env")
        assert env_store.key_status(path=tmp_path / ".env") == (True, "dot_env")

    def test_neither(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
        assert env_store.key_status(path=tmp_path / ".env") == (False, "none")

    def test_blank_env_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ORCAROUTER_API_KEY", "   ")
        env_store.write_env_key("sk-test-dotenv", tmp_path / ".env")
        assert env_store.key_status(path=tmp_path / ".env") == (True, "dot_env")
