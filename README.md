# FormatConverter

把 `pdfs/` 里的 PDF 转成 Markdown，并对生成的 Markdown 做段落合并、列表保留与重复段落清理；同时提供一个**可选的 AI 校对**能力，可调用你自己提供的第三方 AI 服务校对单个 Markdown 文件。

本项目提供两种使用方式：**本机图形界面**（Web UI，默认仅监听 `127.0.0.1`）与**命令行**（CLI，通过 `python main.py`）。默认目录约定：`pdfs/`（输入 PDF）、`mds/`（转换与清理后的 Markdown）。

## 功能概览

- **PDF → Markdown**：用 pymupdf4llm 把 PDF 转成同名 `.md`（也可用 marker-pdf 转换单个 PDF）。
- **Markdown 清理**：合并被硬断行的段落、保留列表结构、删除重复段落，默认生成 `.bak.md` 备份。
- **流水线**：一次完成「转换 + 清理」。
- **AI 校对（可选，非默认）**：对单个 Markdown 文件分块调用你自己的 AI 模型（第一版为 OrcaRouter），只修正明显的 OCR / 断行 / Markdown 格式问题。
- **本机图形界面**：双击即启动的本地 Web 页面，支持一次选择多个文件、查看任务进度与下载结果。
- **本地优先**：转换与清理完全在本机离线完成；除 AI 校对外，不向任何第三方发送文件。

## 快速开始

> 以下命令均在**项目根目录**、使用虚拟环境解释器 `.venv\Scripts\python.exe` 执行；BAT 已内置 Python 探测，普通用户双击即可。

### 首次准备：安装依赖

若尚未创建虚拟环境或未安装依赖，在项目根目录打开 PowerShell 执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

### 方式一：双击 BAT 启动（推荐）

在项目根目录**双击** `启动图形界面.bat`。脚本会自动探测 Python（`.venv` → `py -3` → `python`），检查依赖后启动服务并打开默认浏览器。

> **保持弹出的黑色窗口前台运行**：它就是服务进程本体，关闭窗口或按 **Ctrl+C** 即停止服务。停止时窗口会打印「收到 Ctrl+C，正在停止服务...」。

### 方式二：PowerShell 命令启动

```powershell
.\.venv\Scripts\python.exe -m format_converter.web_server
```

可用参数（与 `启动图形界面.bat` 内部同一条命令）：

| 参数 | 说明 | 默认 |
| ---- | ---- | ---- |
| `--port N` | 首选端口 | `8765` |
| `--no-browser` | 启动后不自动打开浏览器 | 打开 |
| `--max-backup-ports N` | 首选端口被占用时依次尝试的备用端口数 | `5` |

### 访问地址

浏览器打开 **http://127.0.0.1:8765/**。若 `8765` 被其它程序占用，脚本会提示「端口 8765 被占用，改用端口 …」并自动改用 `8766` 等备用端口（此时访问启动提示中给出的地址）；若 `8765` 上已运行本服务，会提示「服务已在运行：http://127.0.0.1:8765/」并直接复用现有实例。

> 误双击了 `打开我.html`？那只是一个**说明页**（不是应用入口，也不会启动服务），按页内提示先启动服务即可。

## Web UI 使用说明

页面顶部用分段控制器切换**四种任务类型**，工作面板随类型切换：

| 任务类型 | 接受的文件 | 做什么 |
| ---- | ---- | ---- |
| ① PDF 转 Markdown | `.pdf` | 转换出 `.md` |
| ② Markdown 清理 | `.md` | 合并断行、去重、保留列表，生成 `.bak.md` |
| ③ 转换后清理流水线 | `.pdf` | 先转换再清理 |
| ④ AI 校对 | `.md` | 分块发送到 OrcaRouter 校对（真实网络请求） |

- **多文件选择**：点击或拖拽到「点击或拖拽文件到此处」，一次可选择**多个**文件。选择/拖拽时页面立即校验扩展名（PDF 类任务只收 `.pdf`，Markdown 类只收 `.md`）、文件名重复（不区分大小写）与空文件，任一不符会提示并阻止「开始处理」。已选文件列表可逐条移除（×），也可「清空列表」。
- **提交与进度**：点「开始处理」提交；任务状态显示为 **排队 / 处理中 / 已中断 / 成功 / 失败**。AI 校对任务在处理中会显示进度（如 `AI 校对中 · 2 / 8`，即“已完成块数 / 总块数”）。
- **最近任务**：页面下方「最近任务」列出当前服务进程内的任务（类型 / 状态 / 更新时间）。**切换任务类型或刷新页面不会丢失这些任务**，运行中的任务会自动继续轮询到完成；成功任务可随时点「下载结果」。服务进程重启后旧任务的临时输出会消失（见「AI 校对说明 · 检查点与继续处理」）。
- **下载结果**：成功任务点「下载结果」（按钮固定叫这个，不叫“下载 ZIP”）：
  - **单个**最终输出文件 → 直接下载该文件本身（不打包）。
  - **多个**最终输出文件 → 下载一个 ZIP，ZIP **根目录**直接是最终产物文件名（如 `a.md`、`a.ai.md`），**不包含** `input/`、`output/` 这类目录；同名条目会稳定重命名（`doc.md` → `doc-2.md`），绝不静默覆盖。
- 仅「AI 校对」模式会展开 **OrcaRouter API 配置**与**模型名**输入区，详见下文「AI 校对说明」。

## CLI 使用说明

所有命令通过虚拟环境解释器运行，命令与参数和 `format_converter/cli.py` 的 argparse 定义一致：

```powershell
.\.venv\Scripts\python.exe main.py <命令> [参数]
```

### convert — 转换 PDF

| 参数 | 说明 | 默认 |
| ---- | ---- | ---- |
| `--input` / `-i PATH` | PDF 目录 | `pdfs/` |
| `--output` / `-o PATH` | 输出 Markdown 目录 | `mds/` |
| `--file PATH` | 只转换这一个 PDF（代替目录） | — |
| `--overwrite` | 覆盖已存在的 `.md` | 不覆盖 |

目录模式把每个 PDF 转成同名 `.md` 到输出目录；单文件模式只处理该 PDF。

```powershell
.\.venv\Scripts\python.exe main.py convert
.\.venv\Scripts\python.exe main.py convert --file .\pdfs\示例.pdf --output .\mds
```

### marker — 用 marker-pdf 转换单个 PDF

| 参数 | 说明 | 默认 |
| ---- | ---- | ---- |
| `file PATH`（位置参数） | 要转换的 PDF | 必填 |
| `--output` / `-o PATH` | 输出目录 | `output_markdown/` |
| `--name NAME` | 输出基名 | PDF 文件名 |

依赖 `marker-pdf`（会拉取 torch / transformers / onnxruntime 等重依赖）；其在 Windows + Python 3.13 上的可安装性以 CI 首次运行为准，本机开发环境未安装验证。若不需要此命令，可只装 `openai` 与 `pytest` 运行测试。

### clean — 清理 Markdown（原地）

| 参数 | 说明 | 默认 |
| ---- | ---- | ---- |
| `--input` / `-i PATH` | Markdown 目录 | `mds/` |
| `--file PATH` | 只清理这一个 Markdown | — |
| `--no-backup` | 不生成 `.bak.md` 备份 | 生成备份 |
| `--no-dedupe` | 保留重复段落块 | 删除重复段落 |
| `--flatten-lists` | 把列表块当普通段落合并 | 保留列表换行 |

原地覆盖清理后的文件；默认先生成同名 `.bak.md` 备份。Web UI 的「Markdown 清理」即该能力。

### pipeline — 转换 + 清理

| 参数 | 说明 | 默认 |
| ---- | ---- | ---- |
| `--pdf-dir PATH` | PDF 目录 | `pdfs/` |
| `--md-dir PATH` | Markdown 目录 | `mds/` |
| `--overwrite` | 覆盖已存在的 Markdown | 不覆盖 |
| `--no-backup` | 不生成 `.bak.md` | 生成备份 |
| `--no-dedupe` | 保留重复段落 | 删除重复段落 |
| `--flatten-lists` | 列表块按普通段落合并 | 保留列表换行 |

先转换到 `--md-dir`，再原地清理。

### ai-clean — AI 校对单个 Markdown（可选、非默认）

```powershell
.\.venv\Scripts\python.exe main.py ai-clean `
  --file .\mds\示例.md `
  --provider orcarouter `
  --model <模型名>
```

详见下文「AI 校对说明」。**CLI 的 `ai-clean` 只处理单个 `.md` 文件**；需要多文件 / 断点续跑时请用 Web UI 的「AI 校对」。

## AI 校对说明（可选，非默认）

`ai-clean` 用你自己提供的 AI 模型校对 Markdown：**只**修正明显的 OCR 错误、断行与 Markdown 格式问题，保留原文的语言、事实、链接、代码块、表格、列表和标题语义；**不**总结、翻译、删减、扩写或加解释。文件内容会分块发送到你选择的第三方 AI 服务（第一版仅 OrcaRouter）——这是**真实的网络请求，可能产生费用**，费用由你的 OrcaRouter 账户承担。`convert` / `clean` / `pipeline` 以及 Web UI 的其它三种任务都不会触发 AI。

### Key 来源优先级

固定为：**① 系统/进程环境变量 `ORCAROUTER_API_KEY` → ② 项目根目录 `.env` 文件 → ③ 未配置**。环境变量未设置时，每次执行都会重新读取 `.env`（无需重启服务）。API Key **不支持**作为命令行参数传入，也**不会**写入日志、异常信息、浏览器存储、任务检查点或任何 git 跟踪文件。

**配置方法一（环境变量，PowerShell）**：在启动服务 / 运行命令的**同一个终端**里先设置，再启动：

```powershell
$env:ORCAROUTER_API_KEY = "你的-key"
```

**配置方法二（项目根目录 `.env`）**：在 Web UI「AI 校对」的配置区粘贴 Key 并点「保存到本地」，或手动在项目根目录创建 `.env`：

```text
ORCAROUTER_API_KEY=你的-key
```

`.env` 是**明文本地配置文件，不是加密保险箱**：它只适合本机个人使用，已被 `.gitignore` 忽略、不会被 Git 提交，也只应放在项目根目录。参考模板见 `.env.example`（仅占位值）。要点：

- Web 配置区会显示 Key 状态与来源（系统环境变量 / 本地 `.env` / 未配置）；「保存到本地」只写 `.env`，「清除本地 Key」只删 `.env` 里的该行（即使生效来源是环境变量，也绝不动环境变量本身）。
- Key 只通过本机回环请求保存到 `.env`，**不进入浏览器持久化存储**（无 Cookie / localStorage）；但 **Key 确实会联网**——执行 AI 校对时，Python 后端用该 Key 真实调用 OrcaRouter。
- 写入 / 清除 `.env` 的接口要求服务启动时生成的**仅内存会话令牌**（服务停止即失效、不落盘）与回环 `Host`/`Origin`，防止其它网页对 localhost 发起未授权操作。

### 模型名记忆与删除

Web UI「AI 校对」里填写的模型名可点「保存模型」存入项目根目录 gitignored 的 `.formatconverter-models.json`（**只存模型名，绝不存 API Key**；会拒绝保存形如 `sk-…` 的值），提交 AI 任务时也会自动记住，下次可直接从下拉历史选择。「删除模型」会把该模型从列表移除。重启服务后记忆仍在。

### 连接测试

「AI 校对」配置区的「测试连接」会用当前 Key 与当前模型向 OrcaRouter 发起一次**极小真实请求**（`Reply with OK.`），用来提前确认 Key 与模型可用。页面已明确提示该操作会发起真实网络请求、**可能产生费用**。成功显示「连接正常」；失败显示脱敏原因（如「未配置 API Key。」「认证失败」「无权限使用该模型或端点」「请求过于频繁」「无法连接 provider」等），不会回显 Key、请求体或响应原文。

### CLI 单文件 vs Web 多文件

- **CLI**：`main.py ai-clean` 每次只处理**一个** `.md`（`--file` 必填、`--provider` 与 `--model` 必填；`--provider` 目前只接受 `orcarouter`）。默认输出为同目录 `<原文件名>.ai.md`，不覆盖原文件；若输出已存在或指向原文件，须加 `--overwrite` 才会继续。CLI 没有检查点 / 继续 / 重试等任务管理能力。
- **Web**：「AI 校对」支持一次上传**多个** `.md`，逐个输出 `<文件名>.ai.md`，并且（见下）具备检查点与续跑能力。

### 检查点、继续处理与重试（仅 Web AI 任务）

Web UI 的 AI 校对任务（无论单个还是多个文件）会按“块”调用 AI，并把每块的输入、分隔符与结果**原子写入**项目根目录 gitignored 的 `.formatconverter-jobs/<任务>/` 检查点目录，同时页面显示 `AI 校对中 · N / M` 进度。据此：

- **任务失败（「失败」）**：行上提供「重试」。重试复用已写入的检查点，**只重新请求缺失的块**，已完成块的 AI 结果不会重复付费调用。
- **任务被中断（「已中断」）**：行上提供「继续处理」。例如校对中途按 Ctrl+C 停止服务、或服务进程异常退出——重新启动服务后，上次仍在处理中的检查点会被标记为「已中断」并重新出现在「最近任务」，点「继续处理」即从断点续跑。
- **删除**：终态任务（成功 / 失败 / 已中断）行上提供「删除」（会弹窗确认）。删除同时清掉该任务的临时输出目录与 `.formatconverter-jobs/` 下的检查点，不可恢复。
- 单个分块在遇到瞬时错误（网络中断 / 限流 / 服务端 5xx）时会自动重试（最多 4 次，间隔约 1 / 2 / 4 秒）；认证、无权限、请求非法、模型不存在等**非瞬时**错误不重试、立即失败。
- 这些能力只存在于 Web UI 的「AI 校对」；CLI 的 `ai-clean` 为单文件直调，不写检查点，也就没有「继续 / 重试 / 删除检查点」。

### 分块与失败安全

- 只在**空段落边界**分块并保持顺序；代码块（围栏 / 缩进）作为整体绝不跨块拆分。默认单块最大 **12,000 字符**。
- 若某个不可再分的段落 / 代码块超过上限，命令会报错并建议拆小文件，**绝不静默截断**。
- 任一分块失败时不写最终输出文件；原文件永远不被覆盖（见上）。

## 隐私、安全与限制

- **只监听本机**：服务（`format_converter/web_server.py`，BAT 与 `python -m format_converter.web_server` 均调用它）把 socket 硬绑定到 `127.0.0.1`，非回环地址在构造与启动两处都被拒绝，**不暴露到局域网或公网**；页面不加载任何第三方资源，也不做遥测。
- **除 AI 校对外，文件不出本机**：转换与清理在本地离线完成。AI 校对本机进程会把内容发送到 OrcaRouter——**不宣称第三方 AI 请求不会离开本机**，请确认内容适合外发、并知晓可能产生费用后再使用。
- **Key 不落浏览器、不入日志与产物**：页面不把 Key 写入浏览器存储（无 Cookie / localStorage 等）；Key 不进入服务日志、任务状态消息、API 响应、下载的 ZIP 或 AI 检查点。
- **本地数据都是 gitignored 的明文数据**：`.env`、`.formatconverter-models.json`、`.formatconverter-jobs/` 三个本地数据文件/目录均不在 Git 跟踪内，也都不应被手动提交。它们只适用于本机个人使用。
- **无 CORS**：所有响应不携带跨域头，浏览器里的其它网站无法读取本服务数据。
- **平台与版本**：图形界面与 BAT 面向 **Windows 10 / 11**；建议 **Python 3.11+**（本地在 3.13 测试，CI 覆盖 3.12 与 3.13）；页面为单页应用，需现代浏览器（Edge / Chrome / Firefox，支持 ES6+）。Python 核心代码未做跨平台专门验证，**无跨平台保证**。

## 常见问题（FAQ）

**找不到 Python 或依赖缺失**
双击 BAT 时若打印 `[ERROR] Python was not found.`，说明未安装 Python：请安装 Python 3.11 或更高版本（<https://www.python.org/downloads/>）；若打印 `[ERROR] Python 3 or the project dependencies are not ready.`，说明 Python 3 或核心依赖未就绪。两种情况脚本都会给出可复制的三条命令（`python -m venv .venv`、`pip install -r requirements.txt`、`pip install -r requirements-dev.txt`）。可选包缺失只提示不退出：缺 `pymupdf4llm` 时「PDF 转 Markdown」与「流水线」不可用，缺 `openai` 时「AI 校对」不可用，`clean` 与页面其余功能不受影响。

**端口被占用**
`8765` 已被**本服务**占用 → 提示「服务已在运行：http://127.0.0.1:8765/」并复用现有实例；被**其它程序**占用 → 自动改用 `8766` 等备用端口并提示；全部占用 → 打印含端口范围与建议的明确错误并以非零码退出，关闭占用程序后重试即可。若服务窗口被直接关闭导致端口未释放，可在任务管理器结束残留的 `python.exe` / `py.exe` 进程后重试。

**浏览器没有自动打开**
手动访问 http://127.0.0.1:8765/（若用了备用端口，访问启动提示中的端口）；或加 `--no-browser` 启动。

**AI 校对连接失败**
命令行报 `Could not connect to provider 'orcarouter'. Check your network connection.` 之类错误，通常是网络无法访问 OrcaRouter：请检查网络 / 代理后重试（该类瞬时错误本身会自动重试最多 4 次）。Web 端可先用「测试连接」确认连通。

**Key 无效或无权限**
命令行为 `Provider 'orcarouter' rejected the API key. Check the value of ORCAROUTER_API_KEY.` 说明 Key 认证失败（401）；`denied access (HTTP 403)` 说明 Key 有效但无权使用该模型/端点。请核对 `ORCAROUTER_API_KEY` 环境变量或 `.env` 中保存的 Key、以及账户权限。

**模型不存在**
命令行为 `Provider 'orcarouter' could not find the requested model (HTTP 404).`，说明该模型名在 OrcaRouter 上不存在。请确认模型名拼写与可用性后改填再试。

**非 UTF-8 Markdown**
`ai-clean` 只接受 UTF-8 编码的 `.md`；GBK 等其它编码会报错（消息形如 `Could not decode ... as UTF-8. ...`）并返回码 1、不写输出。请把文件另存为 UTF-8 后重试。

**AI 任务失败 / 想重试 / 想继续**
Web UI 中 AI 校对任务失败时在「最近任务」行上点「重试」（复用检查点，只重新请求缺失块）；若服务曾中断、重启后任务显示「已中断」，点「继续处理」从断点续跑。想彻底清除任务与检查点可点「删除」。CLI 没有这些能力（单文件直调，一次运行内自动重试瞬时错误）。两者都**不会**暴露或存储你的 API Key。

## 开发与测试

建议 Python 3.11+；本地在 Python 3.13 下测试，CI 额外覆盖 3.12。在项目根目录准备虚拟环境并安装依赖（命令同「快速开始」）：

- `requirements.txt`：运行依赖（PDF 转换与 AI 校对所需）。
- `requirements-dev.txt`：开发/测试依赖（目前仅 `pytest`）。

```powershell
.\.venv\Scripts\python.exe -m pytest          # 运行全量测试
.\.venv\Scripts\python.exe -m compileall -q . # 字节码编译检查
node --check format_converter\web\static\app.js  # 前端 JS 语法检查
```

测试全部**离线**运行：使用 fake / 注入客户端，不联网、不依赖真实 API Key；`.env`、模型记忆与 AI 检查点均被 conftest 隔离到每个测试的临时路径。GitHub Actions 工作流 `.github/workflows/tests.yml` 会在 `windows-latest` 上对 Python 3.12 / 3.13 运行同样的 `pytest` 与 `compileall`。

## 项目结构

```text
FormatConverter/
  pdfs/                     # 原始 PDF（输入；目录本身被 gitignore）
  mds/                      # 转换/清理后的 Markdown（输出；首次转换时自动创建）
  LICENSE                   # MIT License
  main.py                   # CLI 入口（转发到 format_converter.cli）
  启动图形界面.bat           # Windows 一键启动（双击运行）
  打开我.html               # 说明页（非应用入口，服务未启动时可查看启动指引）
  .env.example              # .env 参考模板（仅占位值）
  format_converter/
    cli.py                  # 命令行定义（convert/marker/clean/pipeline/ai-clean）
    config.py               # 默认路径（pdfs/、mds/、output_markdown/）
    jobs.py                 # 任务服务层（后台任务模型，CLI 与 Web 共用）
    ai_jobs.py              # 持久化 AI 任务检查点存储（.formatconverter-jobs/）
    model_store.py          # 模型名本地记忆（.formatconverter-models.json）
    env_store.py            # 项目根 .env 的严格本地解析（仅 ORCAROUTER_API_KEY）
    pdf_converter.py        # PDF → Markdown（pymupdf4llm / marker-pdf）
    markdown_cleaner.py     # Markdown 清理逻辑
    pipeline.py             # 转换 + 清理流水线
    ai_cleaner.py           # Markdown 分块 + AI 校对编排（可选）
    llm_client.py           # OpenAI-compatible 客户端封装（可选）
    providers.py            # AI Provider 预设（orcarouter）与 Key 解析
    web_server.py           # 本机 Web 服务（仅监听 127.0.0.1）
    web/static/             # 单页图形界面（index.html / styles.css / app.js）
  tests/                    # 自动化测试（离线运行）
```

## License

本项目以 **MIT License** 开源，详见根目录 [`LICENSE`](LICENSE)。
