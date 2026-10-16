from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Protocol

class Engine(Protocol):
    name: str
    extensions: tuple[str, ...]

    def supports(self, source: Path) -> bool: ...

    def convert(
        self,
        source: Path,
        options: "ConvertOptions",
    ) -> "ConvertResult": ...


@dataclass(frozen=True)
class ConvertOptions:
    assets_dir: Path | None
    image_ref_prefix: str | None
    language: str | None


@dataclass(frozen=True)
class ConvertResult:
    markdown: str
    image_files: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    metadata: Mapping[str, str] = field(default_factory=dict)