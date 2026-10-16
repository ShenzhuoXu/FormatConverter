from mdforge.errors import (
    DependencyError,
    MdforgeError,
    NoEngineError,
    UnknownEngineError,
)


def test_error_inheritance() -> None:
    assert issubclass(DependencyError, MdforgeError)
    assert issubclass(NoEngineError, MdforgeError)
    assert issubclass(UnknownEngineError, MdforgeError)


def test_dependency_error_message() -> None:
    error = DependencyError("docling", "quality")

    assert "pip install 'mdforge[quality]'" in str(error)


def test_no_engine_error_message() -> None:
    error = NoEngineError("scan.pdf")

    assert "扫描件需装 quality 引擎" in str(error)
    assert "scan.pdf" in str(error)


def test_unknown_engine_error_message() -> None:
    error = UnknownEngineError("abc", ["fast", "quality"])

    assert "abc" in str(error)
    assert "fast" in str(error)
    assert "quality" in str(error)


def test_dependency_error_without_extra_uses_sdk_install_command() -> None:
    error = DependencyError("pymupdf")

    assert "pip install 'pymupdf'" in str(error)


def test_unknown_engine_error_without_available_engines() -> None:
    error = UnknownEngineError("abc", [])

    assert "已注册引擎：无" in str(error)