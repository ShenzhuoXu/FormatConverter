"""OpenAI-compatible LLM client for the optional AI proofreading command.

The OpenAI SDK is only touched inside this module. The CLI and the Markdown
chunking/orchestration modules depend on the small :class:`LLMClient`
protocol instead, so they can be tested with a fake client and never make
real API calls. All errors raised here are project-level exceptions that
never include the API key.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .providers import ProviderConfig


class LLMClientError(Exception):
    """Base class for LLM client failures.

    Messages never include the API key or request details that could leak it.
    """


class AuthenticationError(LLMClientError):
    """The provider rejected the API key (HTTP 401)."""


class PermissionDeniedError(LLMClientError):
    """The provider accepted the key but it lacks access (HTTP 403)."""


class RateLimitError(LLMClientError):
    """The provider throttled the request (HTTP 429)."""


class ConnectionFailedError(LLMClientError):
    """The request could not reach the provider, or timed out."""


class ServerError(LLMClientError):
    """The provider returned an unexpected server error."""


class EmptyResponseError(LLMClientError):
    """The provider returned a response with no message content."""


@runtime_checkable
class LLMClient(Protocol):
    """Minimal client interface the rest of the app depends on."""

    def complete(self, *, system: str, user: str, model: str) -> str:
        """Return the model's completion for ``user`` given ``system``.

        Raises :class:`LLMClientError` subclasses on failure.
        """
        ...


class OpenAICompatClient:
    """OpenAI-compatible chat-completions client for a provider preset."""

    def __init__(
        self,
        provider: ProviderConfig,
        api_key: str,
        *,
        timeout: float = 60.0,
    ) -> None:
        import openai  # Lazy import: module stays importable without the SDK.

        self._provider = provider
        self._api_key = api_key
        self._client = openai.OpenAI(
            base_url=provider.base_url,
            api_key=api_key,
            timeout=timeout,
        )

    def complete(self, *, system: str, user: str, model: str) -> str:
        import openai

        provider_name = self._provider.name
        try:
            response = self._client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
        except openai.AuthenticationError as exc:
            raise AuthenticationError(
                f"Provider {provider_name!r} rejected the API key. "
                f"Check the value of {self._provider.api_key_env_var}."
            ) from exc
        except openai.RateLimitError as exc:
            raise RateLimitError(
                f"Provider {provider_name!r} rate-limited the request. Wait a moment and retry."
            ) from exc
        except openai.APIConnectionError as exc:
            raise ConnectionFailedError(
                f"Could not connect to provider {provider_name!r}. Check your network connection."
            ) from exc
        except openai.PermissionDeniedError as exc:
            raise PermissionDeniedError(
                f"Provider {provider_name!r} denied access (HTTP 403): the API key is "
                f"valid but lacks permission to use this model or endpoint. Check the "
                f"account permissions linked to {self._provider.api_key_env_var}."
            ) from exc
        except openai.APIStatusError as exc:
            raise ServerError(
                f"Provider {provider_name!r} returned a server error (HTTP {exc.status_code})."
            ) from exc

        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError) as exc:
            raise EmptyResponseError(
                f"Provider {provider_name!r} returned an unreadable response."
            ) from exc

        if content is None or not content.strip():
            raise EmptyResponseError(
                f"Provider {provider_name!r} returned an empty response for a chunk."
            )
        return content
