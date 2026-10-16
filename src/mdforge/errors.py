from collections.abc import Iterable

class MdforgeError(Exception):
    """缺少运行所需依赖时抛出。"""
    
    pass

class DependencyError(MdforgeError):
    """缺少运行所需依赖时抛出。"""

    def __init__(
        self,
        sdk_name: str,
        extra: str | None = None,
    ) -> None:
        self.sdk_name = sdk_name
        self.extra = extra

        if extra is None:
            install_command = f"pip install '{sdk_name}'"
        else:
            install_command = f"pip install 'mdforge[{extra}]'"

        message = (
            f"缺少依赖 {sdk_name}。"
            f"请安装：{install_command}"
        )
        super().__init__(message)


class NoEngineError(MdforgeError):
    """没有可用转换引擎时抛出。"""

    def __init__(self, source: str | None = None) -> None:
        self.source = source

        source_text = f"：{source}" if source else ""
        message = (
            f"没有找到可用的转换引擎{source_text}。"
            "扫描件需装 quality 引擎："
            "pip install 'mdforge[quality]'"
        )
        super().__init__(message)


class UnknownEngineError(MdforgeError):
    """用户指定了不存在的引擎时抛出。"""

    def __init__(
        self,
        name: str,
        available: Iterable[str],
    ) -> None:
        self.name = name
        self.available = tuple(available)

        registered = ", ".join(self.available) or "无"
        message = (
            f"未知引擎 {name!r}。"
            f"已注册引擎：{registered}"
        )
        super().__init__(message)