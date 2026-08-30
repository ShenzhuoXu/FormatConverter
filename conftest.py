"""Pytest configuration.

Ensures the project root is importable regardless of how pytest is invoked,
so tests can do ``from format_converter import ...``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
