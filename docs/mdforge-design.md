# mdforge 设计规格（供手搓实现对照）

> 这是 mdforge（FormatConverter 重写项目）的**设计规格，不含实现**。
> 代码由项目作者手写；本文只固定架构决策、模块契约、边界条件和已踩过的坑。
> 完成里程碑后请回填「状态」小节。

## 1. 目标与动机

1. 多格式支持（PDF 之外：docx/xlsx/pptx/html/epub/...）
2. PDF 质量优化（扫描件 OCR、结构保留、阅读顺序、图片提取）
3. 架构从零手搓，清晰分层

## 2. 分层职责红线（最重要的三条）

- **引擎保结构**：标题层级/列表/表格/阅读顺序由转换引擎负责
- **清洗保整洁**：清洗器只处理空白/断行/重复，**永远不猜结构**（旧项目"列表当普通段落合并"这类选项不再进入清洗器）
- **AI 修 OCR**：AI 校对只修 OCR 错误/断行/格式，不总结不翻译不增删

## 3. 核心不变量

- **核心零依赖**：`import mdforge` 不得拉起任何引擎 SDK（pymupdf/pymupdf4llm/markitdown/docling）。所有引擎**惰性导入**自己的 SDK，缺失时抛 `DependencyError`（消息含 `pip install 'mdforge[extra]'` 提示）
- **测试全离线**：引擎的 SDK 加载通过模块级 `_load_xxx()` 函数（可被 monkeypatch 替换为 fake）；测试永不联网、不需要真实依赖

## 4. 模块布局（建议）

```
mdforge/
  src/mdforge/
    engines/
      base.py          # Engine 协议 + ConvertOptions/ConvertResult
      registry.py      # 有序注册表
      pdf_fast.py      # pymupdf4llm 快速档
      pdf_quality.py   # docling 高质量档（实验性）
      office.py        # markitdown 门面（多格式合一）
    cli.py
    errors.py
  tests/
  pyproject.toml       # 核心零依赖；extras: fast / office / quality / dev
```

## 5. Engine 协议（签名级契约）

```python
class Engine(Protocol):
    name: str                       # 小写唯一标识，如 "fast"
    extensions: tuple[str, ...]     # 带点小写扩展名
    def supports(self, source: Path) -> bool: ...
    def convert(self, source: Path, options: ConvertOptions) -> ConvertResult: ...

@dataclass(frozen=True)
class ConvertOptions:
    assets_dir: Path | None      # 图片输出目录；None=禁用图片提取
    image_ref_prefix: str | None # markdown 里 ![]() 的相对引用前缀，如 "assets/report"
    language: str | None         # BCP-47 OCR 提示，如 "zh"

@dataclass(frozen=True)
class ConvertResult:
    markdown: str
    image_files: tuple[str, ...] = ()   # 引擎写入 assets_dir 的文件名（裸名）
    warnings: tuple[str, ...] = ()
    metadata: Mapping[str, str] = field(default_factory=dict)  # 只放 engine/source 等短字符串
```

**图片契约**：引擎自己把图片文件写进 `assets_dir`（**惰性创建**，只在真有图片时建目录），markdown 内引用用 `image_ref_prefix` 拼成相对路径；`image_files` 报告写入的文件名。调用方（CLI）据此不需要再列目录。

## 6. Registry 语义

- `register(engine)`：**注册顺序 = auto 优先级**；重名/空扩展名抛 `ValueError`
- `resolve(source, preference)`：
  - `"auto"`：按注册顺序返回**第一个 `supports()` 通过**的引擎；全部拒绝 → `NoEngineError`（消息必须含「扫描件需装 quality 引擎」的安装提示）
  - 其它值：按 name 找引擎，**绕过 supports()**（用户显式指定即强制），但引擎必须声明该扩展名，否则 `NoEngineError`；名字不存在 → `UnknownEngineError`
- 内置注册顺序：`fast` → `quality` → `markitdown`（fast 在前：文字层 PDF 不该碰重 OCR 引擎）

## 7. 三个引擎的行为要点

### fast（pymupdf4llm）
- `supports()`：**文字层探测**——只看前 3 页 `get_text()`，有任何非空文本 → True；全空（疑似扫描件）→ False，让位给 quality
- 探测自身失败的两种情况都要**宽松放行**（返回 True）：pymupdf 不可导入（DependencyError → 放行，convert 时再报真正的错）、文件损坏/异常（→ 放行，convert 时暴露真实错误）
- 图片：`write_images=True` + `image_path=assets_dir`；转换后把 markdown 里的**绝对路径引用重写为 prefix**（需处理 `/` 和 `\` 两种分隔符——pymupdf4llm 拼接风格不可假设）；`image_files` 用列目录收集

### quality（docling，实验性）
- `supports()`：接受**所有** PDF（它自己会 OCR）
- 惰性导入 docling；图片提取**未定**，先在请求 assets 时返回 warning 说明占位——M1 基准测试后再实现

### markitdown（office 门面）
- 覆盖：.docx .xlsx .xls .pptx .html .htm .csv .json .xml .epub .zip .txt
- **不接 PDF**（PDF 走专用引擎）

## 8. CLI 语义（convert 命令）

- 参数：`sources...`（文件或目录）、`-o/--output`、`--engine {auto,fast,quality,markitdown}`、`--language`、`--overwrite`、`--no-images`
- 目录展开：**只取顶层**（不递归）、按已知扩展名过滤、排序；空目录 → 报问题不中断
- 源去重：按 `os.path.normcase(resolve())`
- 输出命名：`<stem>.md`；**stem 冲突稳定重命名** `doc.md → doc-2.md`（大小写不敏感计数）
- 已存在输出：无 `--overwrite` → **跳过并提示**（不算失败）；有 → 覆盖
- 批量容错：单文件失败（引擎异常/无引擎）打印 error 并**继续处理后续**；有失败 → 退出码 1，否则 0；无输入源时 argparse 自己退 2
- markdown 写盘：UTF-8、`newline="\n"`

## 9. 错误层级

`MdforgeError` 基类，子类：`DependencyError`（含安装提示）、`NoEngineError`（auto 失败要解释"扫描件"场景 + 提示）、`UnknownEngineError`（列出已注册引擎名）。CLI 捕获 `MdforgeError` 打干净消息；未知异常打类型名+消息（批量不中断）。

## 10. 测试要求

- 全离线：fake 引擎 / fake SDK 注入，`b"pdf"` 这类假文件**永远不该**碰到真引擎
- 必测不变量：子进程 fresh interpreter `import mdforge` 后 `sys.modules` 无任何引擎 SDK
- 必测语义：auto 升级链（fast 拒绝 → quality 接手）、显式指定绕过 supports、稳定重命名、overwrite 跳过语义、目录非递归、错误退出码
- pytest 配置：`pythonpath = ["src", "."]` + `tests/__init__.py`（否则 `from tests.conftest import ...` 不可用）

## 11. 已踩过的坑（写码前先读，能省一轮调试）

1. **CLI 顶层 from-import 会废掉测试注入**：`from .engines import default_registry` 把引用固化进 cli 命名空间，测试 monkeypatch `mdforge.engines.default_registry` 无效。要用 `from . import engines` + `engines.default_registry()`（调用时解析模块属性）
2. **测试忘记注入 fake registry** 会打到真引擎（假 PDF 字节 → pymupdf FileDataError），报错信息看起来像引擎坏了，其实是测试装配问题
3. **子进程导入不变量测试**必须给 subprocess 设 `cwd=<src>` 或 PYTHONPATH，否则 `import mdforge` 直接 ModuleNotFoundError，测的是别的东西
4. **argparse 缺 required 位置参数抛 SystemExit(2)**，不是让 main 返回 2——测试要 `pytest.raises(SystemExit)`
5. **引用重写要同时处理两种分隔符**（pymupdf4llm 在 Windows 上可能用 `\` 拼路径），且替换完还要把 prefix 后残留的 `\` 归一成 `/`
6. **pytest 收集两个 conftest 身份**：加了 `tests/__init__.py` 后 `import tests.conftest` 与 pytest 自己加载的 conftest 才是同一模块
7. Windows 控制台打印中文文件名会乱码（cp936 vs 终端编码）——cosmetic，先不管
8. 写"目录不递归"这类负向断言的测试时，构造的文件要真的在子目录里（否则断言恒假或恒真，测试自欺）

## 12. 路线图与状态

| 里程碑 | 内容 | 状态 |
| ---- | ---- | ---- |
| M1 | 引擎层 + CLI（本文档 §4–§11） | 待手搓 |
| M1 收尾 | 装 docling 用真实扫描件做三引擎基准，补 quality 图片提取 | 未开始 |
| M2 | 清洗管线（算法可参考旧项目 markdown_cleaner.py，职责按 §2 收窄） | 未开始 |
| M3 | AI 校对（分块 round-trip 算法参考旧项目 ai_cleaner.py：separators 多一个元素、代码块原子、超限抛错不截断） | 未开始 |
| M4 | Web 服务（安全红线参考旧项目 web_server.py：回环硬绑定、会话令牌、Key 脱敏） | 未开始 |
| M5 | 发布 | 未开始 |

旧项目（main 分支）是参照物：**算法和测试设计可以平移，架构和接线全部重写**。
