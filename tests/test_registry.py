from pathlib import Path
import pytest

from mdforge.engines.base import ConvertOptions, ConvertResult
from mdforge.engines.registry import EngineRegistry
from mdforge.errors import NoEngineError, UnknownEngineError


class FakeEngine:
    def __init__(
        self,
        name: str,
        accepts: bool,
        extensions: tuple[str, ...] = (".pdf",),
    ) -> None:
        self.name = name
        self.extensions = extensions
        self.accepts = accepts

    def supports(self, source: Path) -> bool:
        return self.accepts

    def convert(
        self,
        source: Path,
        options: ConvertOptions,
    ) -> ConvertResult:
        return ConvertResult(markdown=f"converted by {self.name}")


def test_auto_uses_first_supporting_engine_in_registration_order() -> None:
    registry = EngineRegistry()
    fast = FakeEngine("fast", accepts=False)
    quality = FakeEngine("quality", accepts=True)
    later = FakeEngine("later", accepts=True)

    registry.register(fast)
    registry.register(quality)
    registry.register(later)

    result = registry.resolve(Path("scan.pdf"), "auto")

    assert result is quality


def test_auto_uses_first_engine_when_all_engines_support_source() -> None:
    registry = EngineRegistry()
    fast = FakeEngine("fast", accepts=True)
    quality = FakeEngine("quality", accepts=True)
    markitdown = FakeEngine("markitdown", accepts=True)

    registry.register(fast)
    registry.register(quality)
    registry.register(markitdown)

    result = registry.resolve(Path("report.pdf"), "auto")

    assert result is fast


def test_register_rejects_duplicate_engine_name() -> None:
    registry = EngineRegistry()

    registry.register(FakeEngine("fast", accepts=True))

    with pytest.raises(ValueError):
        registry.register(FakeEngine("fast", accepts=True))


def test_register_rejects_engine_without_extensions() -> None:
    registry = EngineRegistry()
    engine = FakeEngine("empty", accepts=True, extensions=())

    with pytest.raises(ValueError):
        registry.register(engine)


def test_auto_raises_no_engine_error_when_none_supports() -> None:
    registry = EngineRegistry()
    registry.register(FakeEngine("fast", accepts=False))

    with pytest.raises(NoEngineError) as exc_info:
        registry.resolve(Path("scan.pdf"), "auto")

    assert "扫描件需装 quality 引擎" in str(exc_info.value)


def test_auto_raises_no_engine_error_for_empty_registry() -> None:
    registry = EngineRegistry()

    with pytest.raises(NoEngineError) as exc_info:
        registry.resolve(Path("report.pdf"), "auto")

    assert "扫描件需装 quality 引擎" in str(exc_info.value)


def test_explicit_engine_selection_bypasses_supports() -> None:
    registry = EngineRegistry()
    fast = FakeEngine("fast", accepts=False)
    registry.register(fast)

    result = registry.resolve(Path("scan.pdf"), "fast")

    assert result is fast


def test_unknown_engine_error_lists_registered_engines() -> None:
    registry = EngineRegistry()
    registry.register(FakeEngine("fast", accepts=True))
    registry.register(FakeEngine("quality", accepts=True))

    with pytest.raises(UnknownEngineError) as exc_info:
        registry.resolve(Path("scan.pdf"), "missing")

    message = str(exc_info.value)
    assert "fast" in message
    assert "quality" in message


def test_explicit_engine_rejects_unsupported_extension() -> None:
    registry = EngineRegistry()
    registry.register(FakeEngine("fast", accepts=True))

    with pytest.raises(NoEngineError):
        registry.resolve(Path("report.docx"), "fast")
