"""Tests for provider presets and API-key resolution (fully offline)."""

from __future__ import annotations

import pytest

from format_converter.providers import (
    MissingApiKeyError,
    ProviderConfig,
    UnknownProviderError,
    get_api_key,
    get_provider,
)


class TestProviderLookup:
    def test_known_provider_orcarouter(self) -> None:
        provider = get_provider("orcarouter")
        assert provider == ProviderConfig(
            name="orcarouter",
            base_url="https://api.orcarouter.ai/v1",
            api_key_env_var="ORCAROUTER_API_KEY",
        )

    def test_lookup_is_case_insensitive(self) -> None:
        assert get_provider("OrcaRouter").name == "orcarouter"
        assert get_provider("  ORCAROUTER  ").name == "orcarouter"

    def test_provider_config_is_immutable(self) -> None:
        provider = get_provider("orcarouter")
        with pytest.raises(Exception):
            provider.name = "other"  # type: ignore[misc]

    def test_unknown_provider_raises(self) -> None:
        with pytest.raises(UnknownProviderError) as excinfo:
            get_provider("not-a-provider")
        assert "not-a-provider" in str(excinfo.value)
        # Message should help the user: list known providers.
        assert "orcarouter" in str(excinfo.value)

    def test_first_release_only_accepts_orcarouter(self) -> None:
        with pytest.raises(UnknownProviderError):
            get_provider("openai")
        with pytest.raises(UnknownProviderError):
            get_provider("anthropic")


class TestApiKeyResolution:
    def test_missing_key_raises_actionable_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
        provider = get_provider("orcarouter")
        with pytest.raises(MissingApiKeyError) as excinfo:
            get_api_key(provider)
        message = str(excinfo.value)
        assert "ORCAROUTER_API_KEY" in message
        assert "set" in message.lower()

    def test_empty_and_whitespace_key_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = get_provider("orcarouter")
        for empty_value in ("", "   ", "\t\n"):
            monkeypatch.setenv("ORCAROUTER_API_KEY", empty_value)
            with pytest.raises(MissingApiKeyError):
                get_api_key(provider)

    def test_key_read_from_env_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-test-12345")
        assert get_api_key(get_provider("orcarouter")) == "sk-test-12345"

    def test_key_not_leaked_in_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Exceptions raised around key handling must never contain the key."""
        secret = "sk-SUPER-SECRET-VALUE-987654321"
        monkeypatch.setenv("ORCAROUTER_API_KEY", secret)
        provider = get_provider("orcarouter")
        assert get_api_key(provider) == secret
        # A missing-key error (no key in env) must not reference any key.
        monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
        try:
            get_api_key(provider)
        except MissingApiKeyError as exc:
            assert secret not in str(exc)
            assert "sk-" not in str(exc)
        # Unknown-provider error must not leak anything either.
        try:
            get_provider("nope")
        except UnknownProviderError as exc:
            assert secret not in str(exc)


class TestDotEnvFallback:
    def test_env_unset_dotenv_set_returns_dotenv_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from format_converter import env_store

        monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
        env_store.write_env_key("sk-dotenv-value", tmp_path / ".env")
        assert get_api_key(get_provider("orcarouter")) == "sk-dotenv-value"

    def test_env_set_dotenv_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from format_converter import env_store

        monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-env-value")
        env_store.write_env_key("sk-dotenv-value", tmp_path / ".env")
        assert get_api_key(get_provider("orcarouter")) == "sk-env-value"

    def test_env_unset_dotenv_empty_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
        # No .env written: the fallback finds nothing.
        with pytest.raises(MissingApiKeyError):
            get_api_key(get_provider("orcarouter"))
