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
