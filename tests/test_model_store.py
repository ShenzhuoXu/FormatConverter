"""Direct unit tests for ``format_converter.model_store`` (fully offline).

Step 4.1 P1 regression coverage: a model name that looks like an API key
(``sk-...``) must be rejected on save, never written to the models file, and
filtered out of any pre-existing history file so it can never be returned.
"""

from __future__ import annotations

import json

import pytest

from format_converter.model_store import add_model, delete_model, list_models, validate_model

# Built at runtime from two literals so the joined secret never appears as a
# contiguous token in the tracked source (the repo-wide key scan must stay
# clean). Behaviorally it is a realistic secret-shaped model name.
_SECRET_SHAPED = "sk-" + "THISISAREALKEY123456"
_LEGIT = "deepseek/deepseek-v4-flash-free"


class TestValidateModelRejectsSecrets:
    def test_rejects_secret_shaped_model(self) -> None:
        with pytest.raises(ValueError):
            validate_model(_SECRET_SHAPED)

    def test_rejects_secret_shaped_model_with_surrounding_whitespace(self) -> None:
        with pytest.raises(ValueError):
            validate_model("  " + _SECRET_SHAPED + "  ")

    def test_legit_model_name_still_accepted(self) -> None:
        assert validate_model(_LEGIT) == _LEGIT


class TestAddModelRejectsSecrets:
    def test_add_secret_raises_and_creates_no_file(self, tmp_path) -> None:
        target = tmp_path / ".formatconverter-models.json"
        with pytest.raises(ValueError):
            add_model(_SECRET_SHAPED, path=target)
        assert not target.exists()  # nothing was written

    def test_add_secret_does_not_pollute_existing_file(self, tmp_path) -> None:
        target = tmp_path / ".formatconverter-models.json"
        target.write_text(
            json.dumps({"models": [_LEGIT]}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        before = target.read_text(encoding="utf-8")
        with pytest.raises(ValueError):
            add_model(_SECRET_SHAPED, path=target)
        assert target.read_text(encoding="utf-8") == before  # byte-for-byte intact


class TestDeleteModelReusesValidation:
    def test_delete_secret_raises(self, tmp_path) -> None:
        with pytest.raises(ValueError):
            delete_model(_SECRET_SHAPED, path=tmp_path / "models.json")


class TestListModelsFiltersDirtyHistory:
    def test_filters_dirty_secret_keeps_legit(self, tmp_path) -> None:
        target = tmp_path / ".formatconverter-models.json"
        target.write_text(
            json.dumps({"models": [_LEGIT, _SECRET_SHAPED]}),
            encoding="utf-8",
        )
        assert list_models(path=target) == [_LEGIT]

    def test_secret_only_history_reads_as_empty(self, tmp_path) -> None:
        target = tmp_path / ".formatconverter-models.json"
        target.write_text(
            json.dumps({"models": [_SECRET_SHAPED]}),
            encoding="utf-8",
        )
        assert list_models(path=target) == []

    def test_filtering_never_breaks_malformed_mixed_list(self, tmp_path) -> None:
        target = tmp_path / ".formatconverter-models.json"
        target.write_text(
            json.dumps({"models": [_LEGIT, 123, None, _SECRET_SHAPED]}),
            encoding="utf-8",
        )
        assert list_models(path=target) == [_LEGIT]
