# FormatConverter

本项目用于把 `pdfs/` 目录里的 PDF 转成 Markdown，并对生成的 Markdown 做段落合并、列表保留和重复段落清理。另提供一个**可选的、非默认的** AI 校对命令 `ai-clean`，可对单个 Markdown 文件调用你自带的第三方 AI 服务做校对。

## 目录结构

```text
FormatConverter/
  pdfs/                  # 原始 PDF
  mds/                   # 生成和清理后的 Markdown
  format_converter/      # 可维护的核心代码
    cli.py               # 命令行入口（含 ai-clean）
    config.py            # 默认路径
    markdown_cleaner.py  # Markdown 清理逻辑
    pdf_converter.py     # PDF 转 Markdown 逻辑
    pipeline.py          # 转换 + 清理流水线
    providers.py         # AI Provider 预设（可选功能）
    llm_client.py        # OpenAI-compatible 客户端封装（可选功能）
    ai_cleaner.py        # Markdown 分块 + AI 校对编排（可选功能）
  tests/                 # 自动化测试（离线运行）
  main.py                # 本地运行入口
  convert.py             # 兼容旧脚本入口
  convert2.py            # marker-pdf 单文件转换入口
  clean_md.py            # 兼容旧脚本入口
  clean_md_keep_lists.py # 兼容旧脚本入口
  join_paragraphs.py     # 兼容旧脚本入口
```

## 常用命令

从项目根目录运行：

```powershell
.\.venv\Scripts\python.exe main.py convert
```

这会把 `pdfs/` 下的所有 PDF 转成同名 `.md` 文件，输出到 `mds/`。

```powershell
.\.venv\Scripts\python.exe main.py clean
```

这会清理 `mds/` 下的 Markdown。默认会保留列表换行、删除重复段落，并生成 `.bak.md` 备份。

```powershell
.\.venv\Scripts\python.exe main.py pipeline --overwrite
```

这会重新转换 PDF，并清理输出结果。

## 单文件用法

```powershell
.\.venv\Scripts\python.exe main.py convert --file .\pdfs\国防教育.pdf
.\.venv\Scripts\python.exe main.py clean --file .\mds\国防教育.md
```

## AI 校对（可选，非默认）

`ai-clean` 用你自己提供的 AI 模型对**单个** Markdown 文件做校对：只修复明显的 OCR 错误、断行和 Markdown 格式问题，保留原文的语言、事实、链接、代码块、表格、列表和标题语义；不会总结、翻译、删减、扩写或加解释。

> ⚠️ **网络与费用**：`ai-clean` 会把文件内容分块发送到你选择的第三方 AI 服务（第一版仅支持 OrcaRouter）。这是真实的网络请求，**可能产生费用，费用由你自己的 OrcaRouter 账户承担**。请确认内容适合发送到第三方后再使用。`convert`、`clean`、`pipeline` 都不会自动触发 AI。

### 前置条件

1. 在 [OrcaRouter](https://www.orcarouter.ai/) 注册并获取 API Key（维护者可把该链接替换为自己的注册链接）。
2. 把 Key 写入环境变量。Key **不会**保存到任何项目文件、日志或异常信息里：

```powershell
$env:ORCAROUTER_API_KEY = "你的-key"
```

### 用法

```powershell
.\.venv\Scripts\python.exe main.py ai-clean `
  --file .\mds\example.md `
  --provider orcarouter `
  --model <模型名>
```

默认输出为同目录下的 `<原文件名>.ai.md`（例如 `example.md` → `example.ai.md`），**不会覆盖原文件**。

可选参数：

| 参数 | 说明 |
| ---- | ---- |
| `--output PATH` | 指定输出文件路径 |
| `--overwrite` | 允许覆盖原文件或已存在的输出文件 |

要点：

- `--file`、`--provider`、`--model` 必填；第一版 `--provider` 只接受 `orcarouter`。
- API Key 只从环境变量 `ORCAROUTER_API_KEY` 读取，不支持作为命令行参数传入。
- 原文件绝不会被覆盖；即使 `--output` 指向原文件，也必须加 `--overwrite` 才会继续。
- 只处理单一 `.md` 文件，不做目录批量处理。
- 请确认你要用的模型名在 OrcaRouter 上可用。

### 分块与失败安全

- 只在空段落边界分块并保持顺序；默认单块最大 12,000 字符。
- 若某个不可再分的段落超过上限，命令会报错并建议你拆小文件，**不会静默截断**。
- 任一分块失败时，不会写入最终输出文件。

### 测试

```powershell
.\.venv\Scripts\python.exe -m pytest
```

测试全部离线运行：使用 fake/injected 客户端，不联网、不依赖真实 API Key。

## Windows 一键启动与手工验证

### 双击启动（手工验收项）

1. 在项目根目录**双击** `启动图形界面.bat`。
2. 预期现象：
   - 弹出一个命令行**服务窗口**，先后显示「Using Python …」「Starting FormatConverter local service…」「服务已就绪：http://127.0.0.1:8765/」「按 Ctrl+C 停止」。
   - 默认浏览器自动打开 `http://127.0.0.1:8765/`，页面加载出「FormatConverter」四张功能卡片。
3. **保持该窗口前台运行**：它就是服务进程本体；关闭或 Ctrl+C 即停止服务。
4. **停止方式**：在服务窗口按 **Ctrl+C**，窗口会打印「收到 Ctrl+C，正在停止服务...」后退出，下次可重新双击启动。

### 端口占用行为

- 若 `8765` 已被**本服务**的另一个实例占用：BAT 会提示「服务已在运行：http://127.0.0.1:8765/」并直接复用，**不会**重复启动一个无法访问的实例。
- 若 `8765` 被**其它程序**占用：自动依次尝试 `8766` 等备用端口，并提示「端口 8765 被占用，改用端口 …」。
- 若首选及备用端口全被占用：BAT 会打印明确错误提示（含端口范围与建议），以非零码退出。

### 依赖缺失提示

- 找不到 Python：提示安装 Python 3.11+，并给出可复制的环境准备命令。
- 核心依赖缺失（`import format_converter.web_server` 失败）：提示「Python 3 or the project dependencies are not ready」，并给出 `python -m venv .venv`、`pip install -r requirements.txt`、`pip install -r requirements-dev.txt` 三条命令，随后退出。
- `openai` / `pymupdf4llm` 缺失：仅打印 [NOTE] 警告并**继续启动**（对应的 AI 校对 / PDF 转换卡片不可用，`clean` 与本地界面不受影响）。

> 这是**手工验收项**：自动化测试已用 `http.client` 复现同一 API 流程（见 `docs/verification-checklist.md`），真实双击与浏览器点击体验请按上述步骤确认。

## 开发与测试

### Python 版本建议

建议使用 Python 3.11 或更高版本；本地已在 Python 3.13 下测试通过，CI 会额外覆盖 Python 3.12。

### 环境准备

在项目根目录创建并激活虚拟环境，然后安装依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

- `requirements.txt`：运行依赖（PDF 转换与 AI 校对所需）。
- `requirements-dev.txt`：开发/测试依赖（目前仅 `pytest`）。

> 注意：`requirements.txt` 含 `marker-pdf`，会拉取 `torch`/`transformers`/`onnxruntime` 等重依赖，首次安装可能较慢、体积较大。若只需运行测试，装 `openai` + `pytest`（`requirements-dev.txt`）即可。

### 运行测试

```powershell
.\.venv\Scripts\python.exe -m pytest
```

测试全部离线运行：使用 fake/injected 客户端，不联网、不依赖真实 API Key。

### 语法检查

对全项目做一次字节码编译，确认所有模块可被 Python 正常解析：

```powershell
.\.venv\Scripts\python.exe -m compileall -q .
```

### 持续集成

GitHub Actions 工作流（`.github/workflows/tests.yml`）会在 `windows-latest` runner 上、对 Python 3.12 与 3.13 运行同样的命令：安装 `requirements.txt` 与 `requirements-dev.txt` 后执行 `python -m pytest` 和 `python -m compileall -q .`。

## 维护说明

原来的脚本已经改成兼容入口。以后需要改转换逻辑时，优先修改 `format_converter/pdf_converter.py`；需要改 Markdown 清理规则时，优先修改 `format_converter/markdown_cleaner.py`；需要调整 AI 校对（分块、提示词、Provider 预设、客户端封装）时，优先修改 `format_converter/ai_cleaner.py`、`format_converter/providers.py` 和 `format_converter/llm_client.py`，并同步更新 `tests/`。
