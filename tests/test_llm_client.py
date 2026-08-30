"""Tests for the OpenAI-compatible LLM client (fully offline, no API calls)."""

from __future__ import annotations

from types import SimpleNamespace

import httpx2
import openai
import pytest

from format_converter.llm_client import (
    AuthenticationError,
    ConnectionFailedError,
    EmptyResponseError,
    LLMClient,
    OpenAICompatClient,
    PermissionDeniedError,
    RateLimitError,
    ServerError,
)
from format_converter.providers import get_provider

SECRET = "sk-leak-check-value"


def _http_response(status: int) -> httpx2.Response:
    request = httpx2.Request("POST", "https://api.orcarouter.ai/v1/chat/completions")
    return httpx2.Response(status, request=request)


def _status_error(cls: type, status: int) -> openai.APIStatusError:
    return cls(f"status {status}", response=_http_response(status), body={})


class _FakeCompletions:
    """Fake ``chat.completions`` object: records calls, raises or returns."""

    def __init__(self, *, raise_exc: Exception | None = None, content: str | None = "revised"):
        self.raise_exc = raise_exc
        self.content = content
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_exc is not None:
            raise self.raise_exc
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))]
        )


class _FakeSDK:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.chat = SimpleNamespace(completions=completions)


def make_client(fake: _FakeCompletions, api_key: str = SECRET) -> OpenAICompatClient:
    client = OpenAICompatClient(get_provider("orcarouter"), api_key, timeout=5.0)
    client._client = _FakeSDK(fake)  # swap the real SDK client for a fake
    return client


class TestOpenAICompatClient:
    def test_satisfies_llm_client_protocol(self) -> None:
        client = OpenAICompatClient(get_provider("orcarouter"), "k", timeout=5.0)
        assert isinstance(client, LLMClient)

    def test_sends_expected_messages_and_returns_content(self) -> None:
        fake = _FakeCompletions(content="  revised text  ")
        client = make_client(fake)
        result = client.complete(system="SYS", user="USR", model="model-1")
        assert result == "  revised text  "
        call = fake.calls[0]
        assert call["model"] == "model-1"
        assert call["messages"] == [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "USR"},
        ]

    def test_auth_error_mapped_and_key_not_leaked(self) -> None:
        fake = _FakeCompletions(
            raise_exc=_status_error(openai.AuthenticationError, 401)
        )
        client = make_client(fake)
        with pytest.raises(AuthenticationError) as excinfo:
            client.complete(system="s", user="u", model="m")
        assert SECRET not in str(excinfo.value)
        assert "ORCAROUTER_API_KEY" in str(excinfo.value)

    def test_permission_error_mapped_and_key_not_leaked(self) -> None:
        # 403 (PermissionDeniedError) is a sibling of AuthenticationError in
        # openai's SDK and must not be swallowed by the generic APIStatusError
        # branch into ServerError.
        fake = _FakeCompletions(
            raise_exc=_status_error(openai.PermissionDeniedError, 403)
        )
        client = make_client(fake)
        with pytest.raises(PermissionDeniedError) as excinfo:
            client.complete(system="s", user="u", model="m")
        assert SECRET not in str(excinfo.value)
        assert "403" in str(excinfo.value)
        assert "permission" in str(excinfo.value).lower()

    def test_rate_limit_mapped_and_key_not_leaked(self) -> None:
        fake = _FakeCompletions(
            raise_exc=_status_error(openai.RateLimitError, 429)
        )
        client = make_client(fake)
        with pytest.raises(RateLimitError) as excinfo:
            client.complete(system="s", user="u", model="m")
        assert SECRET not in str(excinfo.value)

    def test_connection_error_mapped_and_key_not_leaked(self) -> None:
        request = httpx2.Request("POST", "https://api.orcarouter.ai/v1/chat/completions")
        fake = _FakeCompletions(raise_exc=openai.APIConnectionError(request=request))
        client = make_client(fake)
        with pytest.raises(ConnectionFailedError) as excinfo:
            client.complete(system="s", user="u", model="m")
        assert SECRET not in str(excinfo.value)

    def test_timeout_mapped_to_connection_error(self) -> None:
        request = httpx2.Request("POST", "https://api.orcarouter.ai/v1/chat/completions")
        fake = _FakeCompletions(raise_exc=openai.APITimeoutError(request=request))
        client = make_client(fake)
        with pytest.raises(ConnectionFailedError):
            client.complete(system="s", user="u", model="m")

    def test_server_error_mapped_and_key_not_leaked(self) -> None:
        fake = _FakeCompletions(
            raise_exc=_status_error(openai.APIStatusError, 500)
        )
        client = make_client(fake)
        with pytest.raises(ServerError) as excinfo:
            client.complete(system="s", user="u", model="m")
        assert SECRET not in str(excinfo.value)

    @pytest.mark.parametrize("content", [None, "", "   "])
    def test_empty_response_raises(self, content: str | None) -> None:
        fake = _FakeCompletions(content=content)
        client = make_client(fake)
        with pytest.raises(EmptyResponseError) as excinfo:
            client.complete(system="s", user="u", model="m")
        assert SECRET not in str(excinfo.value)

    def test_unreadable_response_raises_empty_error(self) -> None:
        class _BareCompletions:
            def create(self, **kwargs):  # pragma: no cover - returns malformed object
                return object()  # no .choices

        client = make_client(_FakeCompletions())  # type: ignore[arg-type]
        client._client = _FakeSDK(_BareCompletions())  # type: ignore[arg-type]
        with pytest.raises(EmptyResponseError):
            client.complete(system="s", user="u", model="m")
