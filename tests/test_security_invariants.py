"""Repo-wide security invariants for FormatConverter (fully offline).

These tests are not about any single feature; they guard the project as a
whole against regressions that would break the stated security posture:

- **No real API key on disk.** Every git-tracked file is scanned for the
  two realistic key shapes the project could ever commit: an
  ``ORCAROUTER_API_KEY`` environment-variable assignment whose value is
  not a recognized placeholder, and an OpenAI-style ``sk-<12+ alnum>``
  token. Test doubles that use ``sk-test...`` short/dashed values are
  intentionally not flagged.
- **No IDE/cache artifacts tracked.** ``.idea/``, ``__pycache__`` and the
  pytest temp/cache dirs must never be committed.
- **No third-party / raw-network imports at module top level.** The core
  modules only import the standard library (``urllib.request`` is allowed
  for the localhost health probe) and this package. ``openai``,
  ``pymupdf4llm`` and ``marker`` are lazy imports inside functions and must
  stay that way.
- **A fresh import loads no network client.** Importing ``web_server`` and
  ``jobs`` in a clean interpreter must not pull in ``requests``/``httpx``/
  ``openai``/``pymupdf4llm``/``marker`` etc.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Modules that form the runtime/service layer; the AI/PDF libraries are only
# ever imported lazily inside functions, so they must not appear at top level.
CORE_MODULES = [
    "web_server.py",
    "jobs.py",
    "cli.py",
    "pdf_converter.py",
    "pipeline.py",
    "markdown_cleaner.py",
    "providers.py",
    "llm_client.py",
    "ai_cleaner.py",
    "config.py",
]

FORBIDDEN_THIRD_PARTY_ROOTS = {
    "requests",
    "httpx",
    "httpx2",
    "urllib3",
    "openai",
    "pymupdf4llm",
    "marker",
    "aiohttp",
    "selenium",
    "websockets",
    "socks",
    "anthropic",
}

# Raw network stdlib that must never be imported at module top level.
FORBIDDEN_RAW_NETWORK_ROOTS = {"socket"}
FORBIDDEN_IMPORT_FROM_MODULES = {"http.client"}

# ``_KEY_ASSIGN_RE`` deliberately matches only the ``VAR = "..."`` assignment
# shape (the only way the project would ever write a key literally); it does
# not cover JSON/YAML shapes. That is an accepted boundary: the generic
# ``sk-`` scan below is the real backstop and would catch a real key value
# regardless of the surrounding syntax. Test doubles (``sk-test...``) are
# exempted explicitly with a negative lookahead so this does not depend on the
# accidental hyphen-splitting of the existing fake values.
_KEY_ASSIGN_RE = re.compile(r'ORCAROUTER_API_KEY\s*=\s*["\']([^"\']*)["\']')
_SK_REAL_KEY_RE = re.compile(r"(?!sk-test)sk-[A-Za-z0-9]{12,}")
_CJK_RE = re.compile(r"[一-鿿]")


def _git_tracked_files() -> list[Path]:
    """Return tracked file paths (raw UTF-8, no quotepath escaping)."""
    proc = subprocess.run(
        ["git", "-c", "core.quotepath=false", "ls-files", "-z"],
        cwd=str(ROOT),
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    return [ROOT / p for p in proc.stdout.decode("utf-8").split("\0") if p]


def _top_level_imports(path: Path) -> tuple[set[str], set[str]]:
    """Return (roots, absolute-ImportFrom-modules) for module-level imports only."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    full_modules: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            if node.module:
                roots.add(node.module.split(".")[0])
                full_modules.add(node.module)
    return roots, full_modules


def _is_placeholder(value: str) -> bool:
    """True when an assigned value is clearly a placeholder/fake, not a secret."""
    v = value.strip()
    if not v:
        return True
    if _CJK_RE.search(v):  # e.g. README's "你的-key"
        return True
    if v.startswith("sk-test"):  # test doubles (sk-test, sk-test-12345, ...)
        return True
    if v.lower() in {"your-key", "your_key", "your key"}:
        return True
    return False


class TestNoKeyOnDisk:
    def test_no_real_key_patterns_in_tracked_files(self) -> None:
        bad: list[str] = []
        for path in _git_tracked_files():
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = path.relative_to(ROOT).as_posix()
            for match in _SK_REAL_KEY_RE.finditer(content):
                bad.append(f"{rel}: realistic sk- token {match.group(0)!r}")
            for match in _KEY_ASSIGN_RE.finditer(content):
                value = match.group(1)
                if not _is_placeholder(value):
                    bad.append(f"{rel}: non-placeholder ORCAROUTER_API_KEY assignment {value!r}")
        assert not bad, "Possible real API key in tracked files:\n" + "\n".join(bad)

    def test_no_ide_or_cache_artifacts_tracked(self) -> None:
        for path in _git_tracked_files():
            rel = path.relative_to(ROOT).as_posix()
            assert not rel.startswith(".idea"), f".idea tracked: {rel}"
            assert ".pytest-tmp" not in rel, f"pytest temp tracked: {rel}"
            assert ".pytest_cache" not in rel, f"pytest cache tracked: {rel}"
            assert "__pycache__" not in rel, f"__pycache__ tracked: {rel}"


class TestNoNetworkImports:
    def test_core_modules_have_no_third_party_or_raw_network_imports(self) -> None:
        for name in CORE_MODULES:
            path = ROOT / "format_converter" / name
            roots, full_modules = _top_level_imports(path)
            assert roots.isdisjoint(FORBIDDEN_THIRD_PARTY_ROOTS), (
                f"{name} imports a forbidden third-party module: "
                f"{sorted(roots & FORBIDDEN_THIRD_PARTY_ROOTS)}"
            )
            assert roots.isdisjoint(FORBIDDEN_RAW_NETWORK_ROOTS), (
                f"{name} imports raw socket at module top level"
            )
            assert full_modules.isdisjoint(FORBIDDEN_IMPORT_FROM_MODULES), (
                f"{name} imports {sorted(full_modules & FORBIDDEN_IMPORT_FROM_MODULES)}"
            )

    def test_web_server_only_uses_urllib_request_for_outbound(self) -> None:
        roots, _ = _top_level_imports(ROOT / "format_converter" / "web_server.py")
        assert "urllib" in roots  # urllib.request/urllib.parse: stdlib, allowed
        assert "requests" not in roots
        assert "httpx" not in roots

    def test_fresh_import_loads_no_network_clients(self) -> None:
        code = (
            "import sys, json;"
            f"sys.path.insert(0, {str(ROOT)!r});"
            "import format_converter.jobs;"
            "import format_converter.web_server;"
            "roots = {m.split('.')[0] for m in sys.modules};"
            "bad = sorted(roots & {"
            "'requests','httpx','httpx2','urllib3','openai','pymupdf4llm',"
            "'marker','aiohttp','selenium','websockets','socks','anthropic'});"
            "print(json.dumps(bad))"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        loaded = set(json.loads(proc.stdout.strip()))
        assert loaded == set(), f"fresh import loaded network clients: {sorted(loaded)}"
