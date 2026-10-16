from pathlib import Path

from mdforge.errors import NoEngineError, UnknownEngineError
from mdforge.engines.base import Engine


class EngineRegistry:
    def __init__(self) -> None:
        self._engines: list[Engine] = []

    def register(self, engine: Engine) -> None:
        if any(existing.name == engine.name for existing in self._engines):
            raise ValueError(f"引擎名称已注册：{engine.name}")

        if not engine.extensions:
            raise ValueError(f"引擎必须声明至少一个文件扩展名：{engine.name}")

        self._engines.append(engine)

    def resolve(self, source: Path, preference: str) -> Engine:
        if preference == "auto":
            for engine in self._engines:
                if engine.supports(source):
                    return engine

            raise NoEngineError(str(source))

        for engine in self._engines:
            if engine.name == preference:
                if source.suffix.lower() not in engine.extensions:
                    raise NoEngineError(str(source))

                return engine

        engine_names = (engine.name for engine in self._engines)
        raise UnknownEngineError(preference, engine_names)
