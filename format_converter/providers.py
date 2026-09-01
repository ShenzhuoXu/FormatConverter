"""Provider presets and API-key resolution for optional AI proofreading.

First version ships a single OpenAI-compatible preset: ``orcarouter``.
The feature is opt-in: the user must pass ``--provider orcarouter`` and
provide their own API key either through the ``ORCAROUTER_API_KEY``
environment variable or through a git-ignored project-root ``.env`` file.
Keys are never accepted as CLI arguments and never written to git-tracked
files, logs, or exception messages.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .env_store import read_env_key


class ProviderError(Exception):
    """Base error for provider presets and API-key resolution."""


class UnknownProviderError(ProviderError):
    """Raised when ``--provider`` names a preset that does not exist."""

    def __init__(self, name: str, known: tuple[str, ...]) -> None:
        self.name = name
        self.known = known
        super().__init__(
            f"Unknown provider: {name!r}. Supported providers: {', '.join(known) or '(none)'}."
        )


class MissingApiKeyError(ProviderError):
    """Raised when a provider's API-key environment variable is unset or empty."""

    def __init__(self, provider: "ProviderConfig") -> None:
        self.provider = provider
        super().__init__(
            f"Missing API key for provider {provider.name!r}. "
            f"Set the {provider.api_key_env_var} environment variable and try again."
        )


@dataclass(frozen=True)
class ProviderConfig:
    """Immutable description of an OpenAI-compatible provider preset."""

    name: str
    base_url: str
    api_key_env_var: str


_PROVIDERS: dict[str, ProviderConfig] = {
    "orcarouter": ProviderConfig(
        name="orcarouter",
        base_url="https://api.orcarouter.ai/v1",
        api_key_env_var="ORCAROUTER_API_KEY",
    ),
}


def get_provider(name: str) -> ProviderConfig:
    """Return the preset for ``name`` or raise :class:`UnknownProviderError`.

    Lookup is case-insensitive; the canonical name is the preset key.
    """
    key = name.strip().lower()
    try:
        return _PROVIDERS[key]
    except KeyError:
        raise UnknownProviderError(name, tuple(sorted(_PROVIDERS))) from None


def get_api_key(provider: ProviderConfig) -> str:
    """Read a provider's API key from the environment or the local ``.env``.

    Precedence: the ``ORCAROUTER_API_KEY`` environment variable first, then
    the project-root ``.env`` file (read on every call; nothing is cached).
    Raises :class:`MissingApiKeyError` when neither source is set. The
    resolved key is returned to the caller and is never written to files,
    logs, or exception messages.
    """
    value = os.environ.get(provider.api_key_env_var)
    if value and value.strip():
        return value
    dotenv_value = read_env_key()
    if dotenv_value:
        return dotenv_value
    raise MissingApiKeyError(provider)
