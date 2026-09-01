# FormatConverter

本项目把 `pdfs/` 目录里的 PDF 转成 Markdown，并对生成的 Markdown 做段落合并、列表保留和重复段落清理。它同时提供**命令行**（CLI）和**本机图形界面**（Web UI）两种用法；另提供一个**可选的、非默认的** AI 校对命令 `ai-clean`，可对单个 Markdown 文件调用你自带的第三方 AI 服务做校对。

## 目录结构

```text
FormatConverter/
  pdfs/                  # 原始 PDF
  mds/                   # 生成和清理后的 Markdown
  format_converter/      # 可维护的核心代码
    cli.py               # 命令行入口（含 ai-clean）
    config.py            # 默认路径
    jobs.py              # 任务服务层（后台任务模型，CLI 与 Web 共用）
    markdown_cleaner.py  # Markdown 清理逻辑
    pdf_converter.py     # PDF 转 Markdown 逻辑
    pipeline.py          # 转换 + 清理流水线
    providers.py         # AI Provider 预设（可选功能）
    llm_client.py        # OpenAI-compatible 客户端封装（可选功能）
    ai_cleaner.py        # Markdown 分块 + AI 校对编排（可选功能）
    env_store.py         # 项目根 .env 的严格本地解析（ORCAROUTER_API_KEY）
    web_server.py        # 本机 Web 服务（仅监听 127.0.0.1）
    web/static/          # 单页图形界面（index.html / app.js / styles.css）
  tests/                 # 自动化测试（离线运行）
  .env.example           # .env 参考模板（仅占位值）
  启动图形界面.bat       # Windows 一键启动脚本（双击运行）
  打开我.html            # 服务未启动时的说明页
  main.py                # 本地运行入口
```

## 图形界面三步启动

### ① 安装依赖（首次使用）

若尚未创建虚拟环境 / 未安装依赖，在项目根目录打开 PowerShell 执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

### ② 启动服务

在项目根目录**双击** `启动图形界面.bat`（推荐），或在 PowerShell 中执行：

```powershell
python -m format_converter.web_server
```

服务启动后，命令行窗口会打印「服务已就绪：http://127.0.0.1:8765/」「按 Ctrl+C 停止」，并自动打开默认浏览器。该服务**只监听本机回环地址 `127.0.0.1`**，不会暴露到局域网或公网。

### ③ 使用页面

浏览器打开 `http://127.0.0.1:8765/` 后，在「FormatConverter」页面上选择一种任务类型（顶部按钮切换），再点击或拖拽选择**一个或多个**文件：

- **① PDF 转 Markdown**：上传一个或多个 `.pdf` 文件，下载转换结果。单个输出直接下载该文件，多个输出打包为 ZIP。
- **② Markdown 清理**：上传一个或多个 `.md` 文件，下载清理结果。单个输出直接下载该文件，多个输出打包为 ZIP。
- **③ 转换后清理流水线**：上传一个或多个 `.pdf` 文件，一次完成「转换 + 清理」，下载结果。单个输出直接下载该文件，多个输出打包为 ZIP。
- **④ AI 校对**：上传一个或多个 `.md` 文件并填写模型名，下载校对结果。单个输出直接下载该文件，多个输出打包为 ZIP。页面还提供「OrcaRouter API 配置」区域，可查看 Key 状态/来源、把 Key 保存到本机 `.env`、清除本地 Key 或重新检测。

> 任务提交后在下方「最近任务」区域查看进度（排队 / 处理中 / 成功 / 失败），切换任务类型或刷新页面都不会丢失当前服务进程内的任务，成功任务可随时点「下载结果」。

> **停止方式**：在命令行服务窗口按 **Ctrl+C**，窗口会打印「收到 Ctrl+C，正在停止服务...」后退出，下次可重新双击启动。**保持该窗口前台运行**：它就是服务进程本体，关闭或 Ctrl+C 即停止服务。

## CLI 用法

在项目根目录用虚拟环境中的 Python 调用 `main.py`。以下命令与 `format_converter/cli.py` 的 argparse 定义一致。

### 常用命令

```powershell
.\.venv\Scripts\python.exe main.py convert
```

把 `pdfs/` 下所有 PDF 转成同名 `.md` 文件，输出到 `mds/`。

```powershell
.\.venv\Scripts\python.exe main.py clean
```

清理 `mds/` 下的 Markdown：默认保留列表换行、删除重复段落，并生成 `.bak.md` 备份。

```powershell
.\.venv\Scripts\python.exe main.py pipeline --overwrite
```

重新转换 PDF，并清理输出结果。

### 单文件用法

```powershell
.\.venv\Scripts\python.exe main.py convert --file .\pdfs\国防教育.pdf
.\.venv\Scripts\python.exe main.py clean --file .\mds\国防教育.md
```

### 完整命令参考

**`convert`** — 用 pymupdf4llm 把 PDF 转成 Markdown。

| 参数 | 说明 | 默认 |
| ---- | ---- | ---- |
| `--input` / `-i PATH` | PDF 目录 | `pdfs/` |
| `--output` / `-o PATH` | 输出 Markdown 目录 | `mds/` |
| `--file PATH` | 只转换这一个 PDF（代替目录） | — |
| `--overwrite` | 覆盖已存在的 Markdown 文件 | 不覆盖 |

输出：目录模式下每个 PDF 生成同名 `<名称>.md` 到 `--output` 目录；单文件模式输出到 `--output` 指定目录。

**`marker`** — 用 marker-pdf 转换**单个** PDF。

| 参数 | 说明 | 默认 |
| ---- | ---- | ---- |
| `file PATH`（位置参数） | 要转换的 PDF 文件 | 必填 |
| `--output` / `-o PATH` | marker 输出目录 | `output_markdown/` |
| `--name NAME` | 输出基名 | PDF 文件名 |

输出：marker-pdf 的完整转换结果保存到 `--output` 指定的目录。

**`clean`** — 原地清理 Markdown。

| 参数 | 说明 | 默认 |
| ---- | ---- | ---- |
| `--input` / `-i PATH` | Markdown 目录 | `mds/` |
| `--file PATH` | 只清理这一个 Markdown 文件 | — |
| `--no-backup` | 不生成 `.bak.md` 备份 | 生成备份 |
| `--no-dedupe` | 保留重复段落块 | 删除重复段落 |
| `--flatten-lists` | 把列表块当普通段落合并 | 保留列表换行 |

输出：原地覆盖清理后的文件；默认先生成同名 `.bak.md` 备份。

**`pipeline`** — 先转换 PDF，再清理 Markdown。

| 参数 | 说明 | 默认 |
| ---- | ---- | ---- |
| `--pdf-dir PATH` | PDF 目录 | `pdfs/` |
| `--md-dir PATH` | Markdown 目录 | `mds/` |
| `--overwrite` | 覆盖已存在的 Markdown | 不覆盖 |
| `--no-backup` | 不生成 `.bak.md` | 生成备份 |
| `--no-dedupe` | 保留重复段落 | 删除重复段落 |
| `--flatten-lists` | 列表块按普通段落合并 | 保留列表换行 |

输出：先转换到 `--md-dir`，再原地清理；每次清理默认生成 `.bak.md` 备份。

**`ai-clean`** — 用 AI 校对**单个** Markdown 文件（可选、非默认）。完整说明见下文「AI 校对」。

## AI 校对（可选，非默认）

`ai-clean` 用你自己提供的 AI 模型对**单个** Markdown 文件做校对：只修复明显的 OCR 错误、断行和 Markdown 格式问题，保留原文的语言、事实、链接、代码块、表格、列表和标题语义；不会总结、翻译、删减、扩写或加解释。

### 隐私、费用与 Key 配置

> ⚠️ **网络与费用**：`ai-clean` 会把文件内容分块发送到你选择的第三方 AI 服务（第一版仅支持 OrcaRouter）。这是真实的网络请求，**可能产生费用，费用由你自己的 OrcaRouter 账户承担**。请确认内容适合发送到第三方后再使用。`convert`、`clean`、`pipeline` 以及 Web UI 的其它三张卡片都不会触发 AI。

- **Key 来源优先级（固定）**：① 系统/进程环境变量 `ORCAROUTER_API_KEY`；② 项目根目录 `.env` 文件的 `ORCAROUTER_API_KEY`；③ 未配置。环境变量未设置时，每次执行都会重新读取 `.env`（无需重启服务）。API Key **不支持**作为命令行参数传入，也**不会**写入日志、异常信息或 git 跟踪文件。
- **配置方法一（环境变量，PowerShell）**：在启动服务 / 运行命令的**同一个终端**里先设置环境变量，再启动：

```powershell
$env:ORCAROUTER_API_KEY = "你的-key"
```

- **配置方法二（项目根目录 `.env`）**：在 Web UI 的「AI 校对」卡片「OrcaRouter API 配置」区域粘贴 Key 并点「保存到本地」，或手动在项目根目录创建 `.env`：

```text
ORCAROUTER_API_KEY=你的-key
```

  `.env` 是**明文配置文件**（不是加密保险箱），已被 `.gitignore` 忽略、不会被提交，只适用于本机个人使用场景。参考模板见 `.env.example`（仅占位值）。
- **模型名本地记忆与连接测试**：Web UI「AI 校对」里填写的模型名可点「保存模型」存入项目根目录 gitignored 的 `.formatconverter-models.json`（**只存模型名、绝不存 API Key**），提交 AI 任务时也会自动记住，下次打开可直接下拉选择。「测试连接」会用当前 Key 与模型向 OrcaRouter 发起一次极小真实请求（可能产生费用），用来提前确认 Key 与模型可用。
- **`.env` 与网页的隐私边界**：页面只把 Key 通过本机回环请求保存到项目根目录 `.env`，**不会**写入浏览器存储（无 Cookie / localStorage 等）；但**不宣称 Key 不会联网**——执行 AI 校对时，Python 后端会用该 Key 真实调用 OrcaRouter。写入/删除接口需验证服务启动时生成的仅内存会话令牌与回环 Host/Origin，防止其它网页对 localhost 发起未授权操作。
- **缺失时的表现**：环境变量与 `.env` 均未设置（或为空白）时，`ai-clean` 会在任何网络请求之前报错并退出。命令行错误消息为 `error: Missing API key for provider 'orcarouter'. Set the ORCAROUTER_API_KEY environment variable and try again.`（返回码 1）；Web UI 的「AI 校对」卡片会显示对应的失败提示。
- 在 [OrcaRouter](https://www.orcarouter.ai/) 注册并获取 API Key（维护者可把该链接替换为自己的注册链接）。

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
- API Key 按「环境变量 `ORCAROUTER_API_KEY` > 项目根目录 `.env`」的优先级读取，不支持作为命令行参数传入。
- 原文件绝不会被覆盖；即使 `--output` 指向原文件，也必须加 `--overwrite` 才会继续。
- 只处理单一 `.md` 文件，不做目录批量处理。
- 请确认你要用的模型名在 OrcaRouter 上可用。

### 分块与失败安全

- 只在空段落边界分块并保持顺序；默认单块最大 12,000 字符。
- 若某个不可再分的段落超过上限，命令会报错并建议你拆小文件，**不会静默截断**。
- 任一分块失败时，不会写入最终输出文件。

## 常见故障排查

### 端口被占用

- 若 `8765` 已被**本服务**的另一个实例占用：启动时提示「服务已在运行：http://127.0.0.1:8765/」并**复用**现有实例，不会重复启动一个无法访问的服务。
- 若 `8765` 被**其它程序**占用：自动依次尝试 `8766` 等备用端口（默认最多 5 个），并提示「端口 8765 被占用，改用端口 …」。
- 若首选及备用端口全部被占用：打印明确错误（含端口范围与建议），以非零码退出。关闭占用这些端口的程序后重试即可。

### 浏览器没有自动打开

手动在浏览器访问 `http://127.0.0.1:8765/`（若启用了备用端口，访问启动提示中给出的端口）。也可用 `python -m format_converter.web_server --no-browser` 禁用自动打开。

### 依赖缺失提示

- **找不到 Python**：启动脚本打印 `[ERROR] Python was not found.`，提示安装 Python 3.11 或更高版本（`https://www.python.org/downloads/`），并给出 `python -m venv .venv`、`python -m pip install -r requirements.txt`、`python -m pip install -r requirements-dev.txt` 三条命令后退出。
- **核心依赖缺失**（`import format_converter.web_server` 失败）：启动脚本打印 `[ERROR] Python 3 or the project dependencies are not ready.`，并给出同样的三条命令（`python -m venv .venv`、`python -m pip install -r requirements.txt`、`python -m pip install -r requirements-dev.txt`）后退出。
- **`pymupdf4llm` 缺失**：启动时打印 `[NOTE] pymupdf4llm is missing: PDF conversion will not work.` 并**继续启动**；Web UI 的「PDF 转 Markdown」「转换后清理流水线」两张卡片不可用（`clean` 与本地界面其余功能不受影响）。安装命令：`.\.venv\Scripts\python.exe -m pip install -r requirements.txt`。
- **`openai` 缺失**：启动时打印 `[NOTE] openai is missing: the AI proofreading card will not work.` 并继续启动；「AI 校对」卡片不可用。安装命令同上。
- `convert` / `pipeline` 需要 `pymupdf4llm`；`ai-clean` 需要 `openai`；`clean` 和 Web UI 不需要这两个可选包。

### AI 缺 Key 报错

环境变量 `ORCAROUTER_API_KEY` 与项目根目录 `.env` 均未设置时运行 `ai-clean`：命令行报错并返回码 1；Web UI 的「AI 校对」卡片显示失败提示。不会发起任何网络请求。设置方法见「AI 校对 → 隐私、费用与 Key 配置」。

### 非 UTF-8 文件报错

`ai-clean` 只接受 UTF-8 编码的 `.md` 文件；若文件是 GBK 等其它编码，命令会报错（消息形如 `Could not decode ... as UTF-8. ...`）并返回码 1，不会写输出。请把文件另存为 UTF-8 后重试。

### 后台残留进程 / 端口未释放

- 停止服务请尽量在服务窗口按 **Ctrl+C**，脚本会打印「收到 Ctrl+C，正在停止服务...」并清理临时目录。
- 若服务窗口被直接关闭或进程残留导致端口仍被占用：再次启动时，若 `8765` 上仍有本服务的健康实例，会被自动复用；若是其它程序占用，则按上文「端口被占用」处理。必要时可在任务管理器中结束残留的 `python.exe` / `py.exe` 进程后重试。

## Windows 支持范围

- **系统**：本机开发与测试在 **Windows 11** 上完成；`启动图形界面.bat` 面向 Windows，建议使用 **Windows 10 / 11**。
- **Python**：**Python 3.11 或更高版本**（本地在 Python 3.13 下测试通过；CI 覆盖 Python 3.12 与 3.13）。
- **使用范围**：图形界面服务**只监听本机回环地址 `127.0.0.1`**，仅限本机使用，不提供跨平台或多用户部署保证。
- **浏览器**：图形界面是单页应用，需使用支持 **ES6+（`fetch`、`Promise`、模板字符串等）** 的现代浏览器（如最新版 Edge / Chrome / Firefox）。
- **marker-pdf**：`marker` 命令依赖 `marker-pdf`（会拉取 torch / transformers / onnxruntime 等重依赖）；其在 **Windows + Python 3.13** 上的可安装性以 CI 首次运行为准（本地开发环境未安装验证）。若不需要 `marker` 命令，可仅安装 `openai` 与 `pytest` 运行测试。
- **跨平台**：BAT 启动脚本仅适用于 Windows；Python 核心代码未做跨平台专门验证，**无跨平台保证**。

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

当前入口统一为：图形界面（双击 `启动图形界面.bat`）、CLI（`python main.py ...`）、Web 服务（`python -m format_converter.web_server`）。以后需要改转换逻辑时，优先修改 `format_converter/pdf_converter.py`；需要改 Markdown 清理规则时，优先修改 `format_converter/markdown_cleaner.py`；需要调整 AI 校对（分块、提示词、Provider 预设、客户端封装）时，优先修改 `format_converter/ai_cleaner.py`、`format_converter/providers.py` 和 `format_converter/llm_client.py`，并同步更新 `tests/`。任务服务层在 `format_converter/jobs.py`，本机 Web 服务与图形界面分别在 `format_converter/web_server.py` 与 `format_converter/web/static/`。
