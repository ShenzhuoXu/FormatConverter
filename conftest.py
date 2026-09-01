"""Pytest configuration.

Ensures the project root is importable regardless of how pytest is invoked,
so tests can do ``from format_converter import ...``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(autouse=True)
def _isolate_dotenv(tmp_path, monkeypatch):
    """Point every ``.env`` read/write at a per-test temp file.

    This keeps the real project-root ``.env`` out of the test run and gives
    each test a clean, isolated ``.env`` path.
    """
    from format_converter import env_store

    monkeypatch.setattr(env_store, "dotenv_path", lambda: tmp_path / ".env")


@pytest.fixture(autouse=True)
def _isolate_model_store(tmp_path, monkeypatch):
    """Point every model-name read/write at a per-test temp file.

    This keeps the real project-root ``.formatconverter-models.json`` out of
    the test run and gives each test a clean, isolated models file.
    """
    from format_converter import model_store

    monkeypatch.setattr(
        model_store,
        "models_path",
        lambda: tmp_path / ".formatconverter-models.json",
    )
