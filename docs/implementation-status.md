# FormatConverter 实现状态

> 本文件由主控工程 agent 维护，记录各 Step 的进度、提交 SHA、测试证据与审查结论。
> 目标：在保留现有 CLI 和 AI 校对能力的前提下，完成本机 Web UI、Windows BAT 启动、测试、文档和发布准备。

## 总览

| Step | 描述 | 状态 | 提交 | 验收 |
| ---- | ---- | ---- | ---- | ---- |
| Step 0 | 固化现有 CLI 与 AI 校对功能（ai-clean / OrcaRouter） | ✅ 通过 | `4834b2d`（功能）+ `973ff35`（硬化修复） | 103 tests 全绿，独立审查通过 |
| Step 1 | 工程化与持续集成（pytest 配置 / 依赖划分 / GitHub Actions） | ✅ 通过 | `e72eaa4` | 103 tests 全绿，独立审查通过 |
| Step 2 | 任务服务层（jobs.py：CLI 与未来 Web UI 复用） | ✅ 通过 | `735cc7b` | 118 tests 全绿，独立审查通过 |
| Step 3 | 本机 Web 服务（web_server.py：仅回环访问的任务 API） | ✅ 通过 | `fba444a` | 145 tests 全绿，独立审查通过 |
| Step 4 | 中文单页 Web UI（四张功能卡片） | ✅ 通过 | `4dd840e` | 155 tests 全绿，独立审查通过 |
| Step 5 | Windows 一键启动（启动图形界面.bat + 启动层） | ✅ 通过 | `f0f6fdf`（+`51c7419` 修复） | 170 tests 全绿，独立审查通过 |
| Step 6 | 补测试与修复（端到端回归 + 安全覆盖） | ✅ 通过 | `9f10a48` | 198 tests 全绿（含无 Key 环境），独立审查通过 |
| Step 7 | 发布准备（README/CHANGELOG/版本号/发布清单） | ✅ 通过 | `e7ccb15` | 198 tests 全绿，最终全分支审查通过 |
| Step 8 | v0.2.1 本地 OrcaRouter API Key 配置（env > .env > 未配置） | ✅ 通过 | `88bfb23`（实现）+ `f5204c3`（安全加固） | 254 tests 全绿（含无 Key 环境），独立安全审查 + 复审通过 |

---

## Step 0 — 固化现有 CLI 与 AI 校对功能

### 状态：✅ 验收通过（2026-08-30）

### 范围
- 核对并固化现有 `ai-clean`（可选 AI 校对）实现：`providers.py`（OrcaRouter Provider 预设 + Key 解析）、`llm_client.py`（OpenAI-compatible 客户端）、`ai_cleaner.py`（Markdown 分块 + 编排）、`cli.py`（ai-clean 子命令）、`tests/`、`README.md`。
- 不新增 Web UI。

### 提交
- `4834b2d` `feat: add optional ai-clean AI proofreading command (OrcaRouter)` — 功能实现（13 文件，+1526/−9）
- `973ff35` `fix: harden ai-clean error mapping, encoding, and chunk scanning` — 独立审查发现的 4 个 P2 修复（7 文件，+82/−6）

### 测试证据
- 初始核对：`pytest` → `99 passed`；`compileall -q .` → 退出码 0；`git diff --check` → 通过；`main.py --help` 含 `ai-clean`。
- P2 修复后：`pytest` → `103 passed in 0.90s`（新增 4 条测试：403 映射、GBK 非 UTF-8 两级、空行扫描计时）；`compileall -q .` → 退出码 0；`git diff --check` → 通过（仅有 Windows autocrlf 的 LF→CRLF 提示性 warning，非错误）。
- 测试全部离线：fake/injected 客户端，无真实网络、无真实 API Key。

### 审查结论
- 独立审查 agent（对抗式）：**P0 无、P1 无、P2 4 项**，结论「验收通过」。
  - P2-1 `llm_client.py`：403（`openai.PermissionDeniedError`）被通用 `APIStatusError` 分支误映射为 `ServerError`，且 `AuthenticationError` docstring 误称覆盖 401/403。→ 已修复：新增 `PermissionDeniedError`，403 单独映射，docstring 修正为 401。
  - P2-2 `cli.py`：非 UTF-8（如 GBK）`.md` 触发未捕获 `UnicodeDecodeError` 逃逸 traceback。→ 已修复：新增 `EncodingError`，捕获解码错误转友好消息，`main()` 返回 1、无 traceback。
  - P2-3 `ai_cleaner.py`：缩进代码块内连续空行逐行 lookahead 为 O(K²)。→ 已修复：一次扫描整段空行，O(run)；20,000 空行实测约 3ms。
  - P2-4 `tests/test_providers.py`：恒真无意义断言。→ 已删除。
- 修复后复审：**验收通过，无新 P0/P1/P2**。复审独立 fuzz 6000 例分块字节精确、0 失败；独立验证 403→`PermissionDeniedError`（非 `ServerError`）、GBK 端到端无 traceback、空行扫描线性。
- 行为保真：`git show 4834b2d -- pdf_converter.py markdown_cleaner.py pipeline.py config.py __init__.py` 为空 diff；`git diff 4834b2d --`（修复后）核心 5 文件仍为空。`convert`/`marker`/`clean`/`pipeline` 参数与行为未变。

### 安全核对（Step 0）
- `ORCAROUTER_API_KEY` 仅从环境变量读取（`providers.py:get_api_key`）；无 CLI Key 参数。
- 全仓（含 git 历史 blob、异常消息、README、测试）无真实 API Key；错误消息只引用环境变量**名**。
- 无 Web UI、无监听服务（本 Step 不涉及 Web）。
- `.idea/` 已从跟踪移除（`bb72f96`）且在 `.gitignore`；`.pytest-tmp` 测试后即删除，未入库。

### 注意事项
- 现有功能提交 `4834b2d` 的 message 与工作单指定文案（`feat: add optional OrcaRouter AI Markdown proofreading`）不完全一致；用户已确认**保持现状，不 amend / rebase / 改写历史**。

---

## Step 1 — 工程化与持续集成

### 状态：✅ 验收通过（2026-08-30）

### 范围
- 统一 pytest 配置；运行/测试依赖划分；Windows GitHub Actions 测试工作流；.gitignore 覆盖测试/Web 临时目录与缓存；README 新增「开发与测试」章节。未改动核心功能与 tests/。

### 提交
- `e72eaa4` `chore: add project test and CI foundation`（6 文件，+93/−1）

### 改动内容
- `pytest.ini`（新增）：`testpaths = tests`、`pythonpath = .`，无绝对路径；不全局禁用 cacheprovider（开发者 `--lf`/`--ff` 可用）。
- `requirements.txt`（修改）：移除 `pytest==9.1.1`，仅保留运行依赖（pymupdf4llm / marker-pdf / openai，版本号未变）。
- `requirements-dev.txt`（新增）：`pytest==9.1.1`。
- `.github/workflows/tests.yml`（新增）：`windows-latest`，Python matrix 3.12/3.13，`actions/checkout@v6` + `actions/setup-python@v6`（Node 24 运行时），步骤为升级 pip → 装运行依赖 → 装开发依赖 → `python -m pytest` → `python -m compileall -q .`。注释说明 marker-pdf 重依赖取舍与 Windows+3.13 残余风险。
- `.gitignore`（修改）：新增 `.pytest-tmp/` 与 Web 临时目录占位 `web_uploads/`、`web_outputs/`（注释独立成行，规避行尾注释坑）。
- `README.md`（修改）：新增「开发与测试」章节（Python 版本建议、venv 与两份依赖安装、离线测试、compileall、CI Windows runner）。

### 测试证据
- 最终门禁：`pytest` → `103 passed in 0.93s`（`configfile: pytest.ini`）；`compileall -q .` → 0；`git diff --check` → 通过（仅 autocrlf LF→CRLF 提示）。
- YAML 校验：系统 python + PyYAML 6.0.3 `yaml.safe_load` → `YAML OK`。
- `git check-ignore`：`.pytest-tmp/`、`.pytest_cache/`、`web_uploads/`、`.idea/` 等全部命中。
- 新增/修改文件 grep `C:\|C:/|/Users/` 0 命中（无用户绝对路径）。
- 依赖闭环核验：`tests/test_llm_client.py` 顶层 `import httpx`，而 `httpx` 是 `openai`（1.x，与 marker-pdf 兼容）的传递依赖，CI 安装 requirements.txt 后自动可用，无缺口。（注：早期版本用 `openai==3.6.0` 时此处为 `httpx2`；后因 marker-pdf 与 openai 3.x 版本冲突，已按发布审核统一降级到 openai 1.x。）

### 审查结论
- 独立审查 agent（对抗式）第一轮：**P0 无、P1 1 项、P2 多项**。
  - **P1（阻断）**：`actions/checkout@v4`/`actions/setup-python@v5` 基于 Node 20 运行时，GitHub 将于 2026-09-16 移除 Node 20。已主控独立核验 `@v6` 存在（Node 24）→ 实现 agent 升级为 `checkout@v6`、`setup-python@v6`。
  - **P2**：pytest.ini 全局 `-p no:cacheprovider` 使开发者 `--lf`/`--ff` 退化（已移除 addopts）；marker-pdf 重依赖与 Windows+3.13 可安装性未验证（已以 YAML 注释 + README 提示文档化，非阻塞）。
  - **P3**：README 未提及 marker-pdf 体积（已补提示）。
- 修复后复审：**验收通过，无 P0/P1/P2**。复核确认 action v6、pytest.ini 精简、注释/README 如实、回归全绿；残余风险 marker-pdf Windows+3.13 需首次真实 CI 运行确认（本阶段不得 push，无法在本地触发 CI）。

### 注意事项
- CI 无法在本机真实运行（不得 push / 不得创建 PR）；YAML 有效性已静态校验，真实 runner 首跑应确认 marker-pdf 在 Windows + Python 3.13 的安装可行性。

---

## Step 2 — 任务服务层（jobs.py）

### 状态：✅ 验收通过（2026-08-31）

### 范围
- 新增 `format_converter/jobs.py`：把 convert、clean、pipeline、ai-clean 包装为统一任务模型，供 CLI 与未来 Web UI 复用；不实现 HTTP/HTML/页面。未改动任何现有文件与 CLI 表现。

### 提交
- `735cc7b` `feat: add reusable background job service`（2 文件，+623）

### 改动内容
- `format_converter/jobs.py`（新增，342 行）：
  - `JobStatus(str, Enum)`：`queued` / `running` / `succeeded` / `failed`。
  - `JobResult`（frozen dataclass）：`job_id` / `status` / `message` / `output_paths: tuple[Path, ...]`。
  - `JobManager`：`submit(job_type, params) -> job_id`、`get(job_id) -> JobResult | None`、`wait(job_id, timeout) -> JobResult | None`。后台 **daemon** 线程执行；`threading.Condition` 守卫；异常一律转 `failed`（线程绝不崩溃）；handler 实例级注册表便于未来扩展。
  - handler：`convert` / `clean`（单文件+目录）、`pipeline`、`ai-clean`（可选注入 `client`，未注入时由 `cli.ai_clean` 内部只读 `ORCAROUTER_API_KEY`）；只调用既有 Python 函数，无 shell/子进程/网络。
  - `_sanitize_message`：失败/成功消息存储前，若 `ORCAROUTER_API_KEY` 已设置，把文本中出现的 Key 值替换为 `***`（防御性兜底；主保证是 handler 不写入 Key）。
  - 仅 import 标准库 + 本包模块，无 HTTP/HTML/浏览器依赖。
- `tests/test_jobs.py`（新增，281 行，15 测试）：成功（convert/clean/pipeline）、失败（FileNotFoundError → failed）、未知 job_type（同步抛错）、未知 job_id（get/wait 返回 None）、AI Key 缺失（failed、message 只含变量名不含值）、AI 注入 fake client 成功、脱敏（异常文本含 Key → `***`）、状态流转、wait 超时/负超时、非 dict params。

### 测试证据
- 最终门禁：`pytest` → `118 passed in 0.86s`（103 + 15）；`compileall -q .` → 0；`git diff --check` → 通过。
- 独立核验：`import format_converter.jobs` 后扫描 `sys.modules` 无任何 network 模块（openai/pymupdf4llm/marker 均惰性导入）；缺 Key 时 AI 任务在任何网络调用前经 `MissingApiKeyError` 失败；daemon=True 实测确认。

### 审查结论
- 独立审查 agent（对抗式）第一轮：**P0 无、P1 无、P2 4 项**，结论「验收通过」。
  - P2-1 `except BaseException` 兜底：审查认可行为（CPython 中 KeyboardInterrupt 只投递主线程；SystemExit → failed 可接受）→ 加设计意图注释，行为不变。
  - P2-2 handler `bool(params.get(...))` 强转：未来 Web UI 若传字符串形近值（"false"/"0"）会静默翻转 → JobManager docstring 注明布尔参数须为真正 Python bool。
  - P2-3 `_sanitize_message` 仅精确匹配 Key 值：属防御性兜底，主保证在 handler → docstring 注明边界。
  - P2-4 异常链被压平：刻意设计（链可能含敏感信息）→ 加注释。
- 修复后复审：**验收通过，无 P0/P1/P2 新增**。复审用 AST 剥离 docstring 后对比，确认可执行代码**逐字节等价**（纯注释/docstring 改动）。

### 注意事项
- `jobs.py` 依赖 `cli.ai_clean`（位于 cli.py）；未来若将 AI 编排迁出 CLI 可再解耦（非本次范围）。

---

## Step 3 — 本机 Web 服务（web_server.py）

### 状态：✅ 验收通过（2026-08-31）

### 范围
- 新增 `format_converter/web_server.py`：仅本机访问的 Web 服务（静态资源 + 任务 API），暂不制作正式 UI。复用 Step 2 的 `JobManager`。未改动任何现有文件。

### 提交
- `fba444a` `feat: add local-only web job API`（2 文件，+1135）

### 改动内容
- `format_converter/web_server.py`（新增，714 行）：
  - 默认 `127.0.0.1:8765`，host/port 可传参；**非 loopback 配置（0.0.0.0 / 局域网 / `127.0.0.1.evil` 类主机名）在构造与 serve 两处都抛 `ValueError`**，socket 始终硬绑定 `("127.0.0.1", port)`。
  - 路由：`GET /health`、`GET /`（极简信息页，无 Key 输入/无 localStorage/无访问用户目录的 JS）、`GET /static/<rel>`（可选 static_dir，防穿越）、`POST /api/jobs`、`GET /api/jobs/{id}`（不含 output_paths 绝对路径）、`GET /api/jobs/{id}/download`（ZIP，路径 relative_to 校验，只允许该 job 结果）；未知路由 404。
  - 上传：JSON + base64；文件名安全（拒 `..`/`/`/`\`/空）；按 job_type 校验扩展名（convert/pipeline→.pdf，clean/ai-clean→.md）；空上传/空文件名/无效 JSON/未知 job_type/缺参数 → 400；超大请求体 → 413。
  - 每任务独立临时目录 `<base>/<job_id>/input|output`；`_IdAwareJobManager` 预分配 job_id 避免「建目录写文件」与「worker 启动」竞态。
  - 下载 ZIP 只打包 `JobResult.output_paths` 且每个路径 `resolve().is_relative_to(job_root)` 强制校验；未完成 409、未知/无输出 404。
  - 无任何 `Access-Control-Allow-Origin`；web 层不读 Key；日志仅 method/path/status（畸形请求行也不崩溃）。
  - `shutdown()` 删除临时根（幂等）；`cleanup_job` 对 job_id 做 32 位 hex 强校验 + `is_relative_to(base)` 兜底，绝不删除根外目录。
- `tests/test_web_server.py`（新增，27 测试）：health、提交、状态、失败、下载（ZIP 解压校验）、非法输入、非 loopback 拒绝、路径穿越（上传文件名/下载/静态）、`cleanup_job` 合法/非法、畸形请求行不崩溃、无 CORS、无 Key 泄漏、临时目录清理。全部离线（http.client 打 127.0.0.1）。

### 测试证据
- 最终门禁：`pytest` → `145 passed in 14.83s`（118 + 27）；`compileall -q .` → 0；`git diff --check` → 通过。
- 独立核验：仅 stdlib + `format_converter.jobs` import；`serve()` 硬绑定 127.0.0.1；失败 job 响应/日志/ZIP 无 `sk-`；无 CORS 头（成功/400/404/409/下载均实测）。

### 审查结论
- 独立审查 agent（对抗式）第一轮：**P0 无、P1 1 项、P2 3 项**，结论「需修复后再审」。
  - **P1**：`cleanup_job("..")` 可递归删除父目录（`_job_dir_for("..")` → `base/".."`）。当前无 HTTP 路由可达，但属安全敏感模块纵深缺陷 → 修复：job_id 强校验（`[0-9a-f]{32}`）+ `resolve().is_relative_to(base)` 兜底；实测 12 种非法 id 均不删除任何目录。
  - **P2-1**：畸形请求行使日志崩溃（客户端收不到 400、刷 traceback）→ 修复：`log_message`/`_log_sanitized` 判空 + 整体 try/except，日志路径永不抛异常。
  - **P2-2**：`_is_loopback` 用 `startswith("127.")` 误放行 `127.0.0.1.evil` → 修复：`ipaddress.ip_address(host).is_loopback` 严格解析（`localhost` 按名接受）。
  - **P2-3**：测试未覆盖 `cleanup_job` 与畸形请求行 → 补 3 条测试。
- 修复后复审：**验收通过，无新 P0/P1/P2**。独立实测 P1 修复（12 种非法 id 安全）、日志不崩溃、`127.0.0.1.evil` 被拒；重跑测试无残留目录/线程。

### 注意事项
- `static_dir` 为可选能力（供未来 UI 使用），本步未提供具体静态内容；`GET /` 内置信息页。
- 下载 ZIP 文件名以 job_id 命名；上传内容（客户端自身数据）会原样出现在 ZIP 中（属预期）。

---

## Step 4 — 中文单页 Web UI

### 状态：✅ 验收通过（2026-08-31）

### 范围
- 新增 `format_converter/web/static/` 下三件套（index.html / styles.css / app.js），四张功能卡片；`web_server.py` 最小改动以默认服务打包 UI；不改变 API 契约与 CLI 行为。

### 提交
- `4dd840e` `feat: add local web interface for file conversion`（5 文件，+865/−3）

### 改动内容
- `format_converter/web/static/index.html`：中文单页，四张卡片（①PDF转Markdown ②Markdown清理 ③转换后清理流水线 ④AI校对），顶部网络/隐私/费用提示；所有控件有 `<label for>`、原生 `<button>`、`role="status"/"alert"` + `aria-live`；仅本地 `styles.css`/`app.js`，无 localStorage/CDN/外链/Key 输入。
- `format_converter/web/static/app.js`：FileReader → base64 → `POST /api/jobs`（同源相对路径）→ ~1s 递归轮询（无重叠、`currentJobId` 防串扰）→ succeeded 显示「下载 ZIP」链接 / failed 显示脱敏 message；按钮提交后禁用、终态恢复；错误可见。**AI 卡片**：仅单 .md、Provider 固定 OrcaRouter、模型名必填（关联 label）、Key 检测状态由任务结果推断（初始说明性文案 → 缺 Key 失败→警示 → 成功→✓），未改后端。
- `format_converter/web/static/styles.css`：本地样式、响应式网格、高对比错误色（对比度均 ≥4.5:1 AA）。
- `format_converter/web_server.py`（最小改动）：新增 `DEFAULT_STATIC_DIR`（打包 UI 目录）；`create_server()` 默认 static_dir 指向它（可显式传 None 禁用）；`_send_index` 有 index.html 优先服务否则回退内置页。路由/状态码/响应结构零改动；`JobWebServer(static_dir=None)` 直接构造行为不变。
- `tests/test_web_ui.py`（新增，10 测试）：静态加载、页面源码合规（无 localStorage/外链/Key 输入）、`node --check`、clean 全流程（提交→轮询→下载→解压去重校验）。

### 测试证据
- 最终门禁：`pytest` → `155 passed in 17.42s`（145 + 10）；`compileall -q .` → 0；`git diff --check` → 通过；`node --check app.js` → 通过。
- 独立核验：`create_server(port=0)` 起服务后 `/`、`/static/app.js`、`/static/styles.css` 均 200 且 Content-Type 正确；四卡片流程经 http.client 实测走通；页面合规 grep 0 命中；无障碍对比度核算均 ≥4.5:1。

### 审查结论
- 独立审查 agent（对抗式）：**无 P0/P1/P2**，结论「验收通过」。四卡片数据属性、轮询守卫、AI 卡片 Key 推断、页面合规扫描、无障碍、web_server 最小改动、测试质量均逐项独立核对通过。
- 仅 2 条 P3 设计备注（非阻塞，已接受）：app.js 的 "unknown status" 分支为防御性死代码（无害）；AI 卡片 key-status 在上次成功、本次非 Key 类失败时保持 ✓（检测对象是 Key 本身，行为合理）。

### 注意事项
- 验收项「用浏览器手工验证四张卡片的提交、状态与下载流程」属**用户侧手工验证**：自动化已用 http.client 复现同 API 流程并全部通过，但真实浏览器点击验证待用户执行（可运行 `main.py` 或 Step 5 的启动脚本后打开 `http://127.0.0.1:8765/`）。

---

## Step 5 — Windows 一键启动图形界面

### 状态：✅ 验收通过（2026-08-31）

### 范围
- 新增 `启动图形界面.bat`（一键启动）+ `打开我.html`（服务未启动时的说明页）；`web_server.py` 新增可测试的「启动层」；未改变 API 契约与既有行为。

### 提交
- `f0f6fdf` `feat: add Windows launcher for local web UI`（4 文件，+720/−1）

### 改动内容
- `启动图形界面.bat`：Python 探测顺序 ①`.venv\Scripts\python.exe` ②`py -3` ③`python`；**绝不执行 pip install**（只 echo 可复制安装命令）；找不到 Python/核心依赖缺失 → 打印 venv 创建 + 两份 requirements 安装命令并 pause 退出非 0；openai/pymupdf4llm 缺失仅警告并继续；前台运行 `python -m format_converter.web_server`（同窗口=服务窗口，显示「Press Ctrl+C to stop」）；`FC_TEST_PYTHON`/`FC_TEST_NO_PAUSE` 测试钩子。编码选纯 ASCII（独立实验证明 UTF-8+chcp 65001 会破坏 cmd 对 goto 标签后文件的重读解析）。
- `format_converter/web_server.py`（新增启动层，既有类/方法零改动）：`run_server(preferred_port=8765, open_browser=True, max_backup_ports=5)`——优先端口 → 已被本服务健康实例占用则**复用不新建**（重复启动不会产生多个不可访问实例）→ 绑定 OSError 竞态走同一路径 → 其它程序占用则依次备用端口并明确提示 → 全部占用抛清晰 `ServerStartError`；绑定后轮询 `/health`（30s 超时）→ 打印「服务已就绪：http://127.0.0.1:<端口>/」与「按 Ctrl+C 停止」→ `webbrowser.open`（**URL 恒为 127.0.0.1**，绝不公网）→ Ctrl+C 干净 `shutdown()` 退出 0；`main()`/`__main__`（argparse：--port/--no-browser/--max-backup-ports）。
- `打开我.html`：纯静态说明页（非应用入口），无表单/脚本/localStorage/CDN；说明启动方式、前置条件、仅监听 127.0.0.1。
- `tests/test_launcher.py`（新增，14 测试）：正常启动/浏览器 URL 回环、--no-browser、端口复用（不新建）、占用回退、全占用报错、负数参数、main 退出码、Ctrl+C 干净退出（临时根删除、无残留线程）、`_health_ok` 正反例、BAT 依赖缺失分支冒烟（复制到 ASCII 临时名调用 cmd /c call）。

### 测试证据
- 最终门禁：`pytest` → `169 passed in 27.82s`（155 + 14）；`compileall -q .` → 0；`git diff --check` → 通过；`python -m format_converter.web_server --help` 可用。
- 独立核验：BAT 逐行确认零 pip install；冒烟验证依赖缺失分支输出安装命令 + 退出码 1；端口复用/回退/竞态路径实测；URL 恒为 127.0.0.1；Ctrl+C 后临时根删除、无残留线程。

### 审查结论
- 独立审查 agent（对抗式）第一轮：**P0 无、P1 无、P2 3 项**，结论「验收通过」。
  - **P2-1**：BAT 冒烟测试依赖 8.3 短文件名，`NtfsDisable8dot3NameCreation=1` 时可能误失败 → 修复：改为复制到 ASCII 临时名再调用，完全脱离 8.3 依赖。
  - **P2-2**：`py` 存在但无 Python 3 时误报「Core dependency missing」措辞 → 修复：改为「Python 3 or the project dependencies are not ready」（同时覆盖两种情形），安装命令不变。
  - **P2-3**：编码选型——审查独立实验确认 ASCII 英文是正确且健壮的选型，无需改动。
- 修复后复审：**验收通过，无剩余 P0/P1/P2**。

### 注意事项
- 「Windows 双击 BAT 可启动服务并打开浏览器」属**用户侧手工验证**：自动化已覆盖 BAT 依赖缺失分支冒烟与启动层全部逻辑，真实双击体验待用户确认（双击 `启动图形界面.bat` 即可）。
- BAT 提示为英文（ASCII 稳健性优先）；页面与运行消息仍为中文。

### 修复记录（用户侧实测发现，2026-08-31，提交 `51c7419`）
用户报告双击 BAT 未启动浏览器，复现发现两个独立 bug，均已修复并端到端验证：
- **BAT 解析崩溃**：可选依赖 `if errorlevel 1 ( ... )` 括号块内的 `echo` 文本含裸括号（`(ai-clean)`/`(convert/pipeline)`），`cmd.exe` 把 `)` 当作块结束符，导致 `will was unexpected at this time.` 解析错误、服务未启动。修复：去掉两行 echo 中的括号；复现确认 BAT 走到服务启动并打开浏览器。
- **UI 资源 404**：`index.html` 的 `styles.css`/`app.js` 引用是相对路径，浏览器解析为 `/styles.css`、`/app.js`，而服务端只在 `/static/` 前缀下服务静态文件 → 404，页面无样式且无 JS 功能。修复：改用同源绝对路径 `/static/styles.css`、`/static/app.js`；并把 `test_web_ui.py` 合规断言从「禁止一切 `/` 开头路径」放宽为「仅允许同源 `/static/`、仍禁外链/CDN」，新增回归测试「首页引用的每个 /static/ 资源必须 200」。
- 修复后全量 `170 passed`（+1 回归测试）；端到端复现：BAT 启动 → `/`、`/static/app.js`、`/static/styles.css`、`/health` 全部 200。

---

## Step 6 — 补测试与修复

### 状态：✅ 验收通过（2026-08-31）

### 范围
- **只补测试与修复发现的问题，不添加新功能**。核心源码零改动；新增 28 个测试（170→198）+ 两份文档。

### 提交
- `9f10a48` `test: add end-to-end regression and security coverage`（6 文件，+787/−1）

### 改动内容
- `tests/test_cli_commands.py`（新增，16 测试）：`convert`/`clean`/`pipeline`/`marker` 的 `main()` 端到端 wiring（monkeypatch 假函数，断言返回码与输出）；空目录/无 PDF 优雅路径（rc=0 打印 0 个文件，走真实 glob 不触发惰性导入）；缺失文件/目录的错误传播路径（`pytest.raises` 文档化既有契约，未改 CLI 行为）。
- `tests/test_security_invariants.py`（新增，5 测试）：全仓 `git ls-files` 扫描无真实 Key（`sk-<12+ alnum>` 负向前瞻豁免 `sk-test*`；`ORCAROUTER_API_KEY = "<非占位符>"`）；`.idea/`/`.pytest-tmp`/`__pycache__`/`.pytest_cache` 不在跟踪；核心模块顶层 import 无网络库（AST 扫描，惰性导入不误报）；全新解释器导入 `web_server`/`jobs` 后 `sys.modules` 无网络客户端（子进程验证）。
- `tests/test_web_server.py`（+6）：413 超大请求体；convert/pipeline/ai-clean 三条全流程 e2e（上传→状态→下载 ZIP）；succeeded 但无输出文件 → 404；布尔字符串形近值（`"false"`）不翻转默认行为。
- `tests/test_cli.py`（+1）：纯空白输入 → 原样写出、LLM 客户端零调用。
- `docs/verification-checklist.md`（新增）：自动化验收矩阵（CLI/Web/AI/安全逐项 → 测试用例 → 结果）+ 手工验收项（待用户执行）+ 安全清单证据与结论。
- `README.md`（+25）：新增「Windows 一键启动与手工验证」小节（双击步骤、预期现象、停止方式、端口占用/复用行为、依赖缺失提示）。

### 测试证据
- 最终门禁：`pytest` → **198 passed**（正常）；**`unset ORCAROUTER_API_KEY` 下重跑 → 198 passed**（证明无需真实 Key、无真实网络）；`compileall -q .` → 0；`git diff --check` → 通过。
- 独立核验：`git diff --name-only HEAD | grep -c '^format_converter/'` = 0（**核心源码零改动**）；`git status --short` 恰为 6 个预期文件；无 `.pytest-tmp` 残留。

### 审查结论
- 独立审查 agent（对抗式，独立查看完整 diff 与测试结果）：第一轮**无 P0/P1**，P2×4（均轻微、非缺陷、建议性）：
  - 安全扫描 `_SK_REAL_KEY_RE` 靠连字符切分间接豁免 `sk-test*` → 改为负向前瞻 `(?!sk-test)` 显式豁免（真实 key 仍检出）。
  - `_KEY_ASSIGN_RE` 只匹配 `VAR = "..."` 形状 → 保持现状 + 注释声明边界（`sk-` 扫描是兜底网）。
  - `test_marker_wiring` 用 `.resolve()` 断言脆弱 → 改为捕获 fake 实参断言。
  - 布尔形近值测试为黑盒断言 → 加注释说明语义与局限。
- 修复后复审：**验收通过，无残留 P0/P1/P2**；核心源码零改动确认；两次 pytest 198 passed。

### 注意事项
- 手工验收项（BAT 双击、浏览器四卡片流程、停止方式、端口占用、依赖缺失提示）已写入 `docs/verification-checklist.md` 第二节与 README，待用户执行。

---

## Step 7 — 发布准备

### 状态：✅ 发布就绪（2026-08-31；未 push、未创建 GitHub Release）

### 范围
- README 增强（图形界面三步启动 / CLI 用法 / AI 隐私·费用·Key 配置 / 常见故障排查 / Windows 支持范围）；CHANGELOG.md；版本号 0.2.0；发布检查清单；`.env` 忽略。除版本号与一处测试 docstring 措辞外无代码改动。

### 提交
- `e7ccb15` `docs: prepare local web UI release`（6 文件，+211/−39）

### 改动内容
- `README.md`：新增「图形界面三步启动」（安装依赖 → 双击 `启动图形界面.bat` → 浏览器选卡片上传下载，写明仅监听 127.0.0.1、Ctrl+C 停止）、完整 CLI 命令参考（与 `cli.py` argparse 逐字一致）、AI 隐私/费用/Key 配置（Key 仅环境变量、真实网络请求、费用归用户、PowerShell 配置、缺 Key 逐字错误消息）、常见故障排查（端口占用复用/回退、浏览器未自动打开、依赖缺失提示、AI 缺 Key、非 UTF-8、残留进程）、Windows 支持范围（Win10/11、Python 3.11+、仅本机回环、ES6+ 浏览器、marker-pdf 以 CI 为准、无跨平台保证）。
- `CHANGELOG.md`（新增）：`[0.2.0] - 2026-08-31`（CLI ai-clean / 本机 Web UI / BAT / 任务服务层 / CI）+ `[0.1.0]` 初始记录，Keep a Changelog 风格。
- `format_converter/__init__.py`：`__version__` 0.1.0 → 0.2.0。
- `docs/release-checklist.md`（新增）：测试全量 / 无真实 Key / 无临时文件 / 无 .idea / 无 .env / 无绝对用户路径 / 不 push 不建 Release，每项含验证命令。
- `.gitignore`：补 `.env`（文件）忽略行（原仅 `.env/` 目录）。
- `tests/test_security_invariants.py`（docstring 仅措辞）：修复潜伏自引用 bug——模块 docstring 原含一个「环境变量名 + 等号 + 带引号占位值」形状的字面示例，而安全扫描会扫所有 git 跟踪文件；该形状会被扫描器判为非占位 Key 而误报自身。Step 6 提交（9f10a48）前该文件未跟踪故未被扫，提交后被跟踪即暴露。已改为纯文字描述（不再出现该形状），扫描逻辑零改动。

### 测试证据
- 最终门禁：`pytest` → **198 passed**；`compileall -q .` → 0；`git diff --check` → 通过；`python -c "from format_converter import __version__"` → 0.2.0；`git check-ignore .env` → 命中。
- 独立核验：README 引用的 24 个文件全部存在且被跟踪；五个命令 argparse、默认路径、AI Key 错误消息、端口占用行为、依赖缺失消息均与代码/BAT 逐字一致；OrcaRouter 链接仅 `https://www.orcarouter.ai/`（无虚构链接）；全仓无真实 Key / 绝对用户路径 / 临时文件 / `.env` / `.idea`。

### 审查结论
- 最终全分支独立审查：**无 P0/P1**，P2×1（README 把 BAT「找不到 Python」与「核心依赖缺失」两分支消息归属混并）→ 修复为逐字区分两条消息后**复审发布就绪，无 P0/P1/P2**。
- 潜伏 docstring bug 经独立核实：确为 docstring-only 修复、扫描逻辑零改动、测试 5/5 通过；成因（Step 6 提交前未跟踪不被扫）成立。
- 全项目梳理：Step 0–7 提交链条完整，跟踪文件 42 个，无 TODO/FIXME 遗留。

### 注意事项
- **未 push、未创建 GitHub Release**（按工作单红线）。用户后续可自行决定何时 push / 打 tag / 建 Release；发布前按 `docs/release-checklist.md` 逐项核对（含手工验收项）。

### 发布审核修复记录（2026-08-31，提交 `2d2cb82`）
发布审核发现并已修复：
- **P1**：本文件 Step 7 章节描述 docstring 修复时含一个「环境变量名 + 等号 + 带引号非占位值」形状的字面示例，安全扫描测试会扫所有已跟踪文件并将其判为非占位 Key → HEAD 全量测试实际失败（复现：`test_no_real_key_patterns_in_tracked_files` 失败）。已改为不含该形状的纯文字说明；扫描器规则零改动。
- **P2**：总览表残留一行重复的「Step 7（待工作单）」占位行，已删除。
- 修复后：全量 **198 passed**（含无 `ORCAROUTER_API_KEY` 环境重跑）、`compileall` 0、`git diff --check` 通过、工作树干净。

### 依赖版本冲突修复记录（2026-08-31，提交 `a4b9c01`）
用户按 README 执行 `pip install -r requirements.txt` 时发现依赖无法解析：
- **根因**：`requirements.txt` 同时固定 `openai==3.6.0`（3.x）与 `marker-pdf==1.10.2`，而 marker-pdf 要求 `openai>=1.65.2,<2.0.0` → pip `ResolutionImpossible`。此前本地 venv 只装过 openai 3.6.0、从未装 marker-pdf（转换惰性导入），故 198 个测试从未暴露该冲突。
- **修复**：统一降级 `openai==3.6.0` → `openai==1.106.0`（满足 marker-pdf `<2.0.0`）；`tests/test_llm_client.py` 的 `httpx2` → `httpx`（openai 1.x 的 HTTP 客户端）；`llm_client.py` 无需改动（`OpenAI(base_url=...)`/`chat.completions`/`AuthenticationError`/`PermissionDeniedError`/`APIStatusError` 等错误类在 1.106.0 全部存在，已实测）。
- **验证**：`pip install -r requirements.txt --dry-run` → 退出码 0（解析成功，含 marker-pdf/torch 整树）；venv 降到 openai 1.106.0 后全量 **198 passed**（含无 Key 环境），`compileall` 0、`git diff --check` 通过。
- CHANGELOG 与 release-checklist 已同步（新增依赖解析验证项）。

---

## Step 8 — v0.2.1：本地 OrcaRouter API Key 配置

### 状态：✅ 验收通过（2026-09-01）

### 范围
- 在既有本机 Web UI 与 CLI 基础上，增加安全的本地 OrcaRouter API Key 配置。Key 来源优先级固定为：系统环境变量 `ORCAROUTER_API_KEY` > 项目根目录 `.env` > 未配置。新增 Web API（key-status / save / delete）、AI 卡片「OrcaRouter API 配置」区域、严格的 `.env` 解析与写入、会话令牌防护。

### 提交
- `88bfb23` `feat: add local .env API key config with session-token protection`（16 文件，+1247/−64）—— 实现。
- `f5204c3` `fix: harden .env key config after security review`（5 文件，+202/−52）—— main 代理审阅 + 独立安全复审后的加固（见「修复记录」）。

### 改动内容
- `format_converter/env_store.py`（新增）：字节级严格 `.env` 解析；`dotenv_path` / `read_env_key`（首个非空值；空/`""`/`''` 视为未设置）/ `write_env_key`（首处替换、去重、逐字保留其它行、CRLF 保持、临时文件 + 原子 `os.replace`）/ `delete_env_key`（仅删目标行、幂等）/ `key_status`（环境 > `.env` > 无）。模块级 `RLock` 串行化并发写/删；读用 `_read_raw`（仅 `FileNotFoundError` 视为缺失，瞬时 `OSError` 重试，持久错误传播——绝不把「读不到」当「不存在」覆盖）。Key 值拒绝嵌入 `\n`/`\r`。
- `format_converter/providers.py`：`get_api_key` 现在先读环境变量、缺失时回退 `.env`（每次调用重新读取，无缓存）；`MissingApiKeyError` 消息逐字不变。
- `format_converter/jobs.py`：`_sanitize_message` 同时屏蔽环境变量与 `.env` 的 Key 值。
- `format_converter/web_server.py`：启动时生成仅内存会话令牌（`secrets.token_urlsafe(32)`）；`GET /api/ai/key-status`、`POST /api/ai/key`、`DELETE /api/ai/key`；`_auth_ok` 校验回环 `Host` + 回环 `Origin` + 令牌（`compare_digest`），缺失/非法 403；`_is_loopback_host`/`_is_loopback_origin` 拒绝 userinfo/路径/畸形端口；服务 `index.html` 时注入令牌并带 `Cache-Control: no-store`；`do_DELETE` 路由。响应一律不泄 Key。
- `format_converter/web/static/index.html` / `app.js` / `styles.css`：AI 卡片新增「OrcaRouter API 配置」（密码输入 + 保存/清除/重新检测 + 状态/来源 + 指定文案）；前端不持久化、不 `console.log` Key、请求后立即清空输入；清除按钮按来源显隐（none 隐藏；environment 下仍可清除 `.env` 备用 Key，仅影响 `.env`）。
- `.env.example`（新增，仅占位 `your_api_key_here`）；`.gitignore` 增加 `.env.*.tmp`；`__version__` → `0.2.1`；`conftest.py` 增加 autouse `_isolate_dotenv` fixture（把 `.env` 指到每测试临时路径，杜绝真实 `.env` 干扰）。
- 测试：`tests/test_env_store.py`（新增）、`tests/test_providers.py`、`tests/test_web_server.py`（`TestKeyConfigEndpoints`）、`tests/test_web_ui.py`、`tests/test_security_invariants.py`（`env_store.py` 入 CORE_MODULES、`_KEY_ASSIGN_RE` 加未引号匹配、新增 `.env`/`.env.example` 跟踪断言）。

### 测试证据
- 最终门禁：`pytest` → **254 passed**（198 + 56）；无 `ORCAROUTER_API_KEY` 环境重跑 → **254 passed**；`compileall -q .` → 0；`git diff --check` → 通过（仅 autocrlf 提示）；`node --check app.js` → 通过；`git grep 'sk-<12+ alnum>'` 无命中；`.env` 被 git 忽略且未跟踪，`.env.example` 仅占位值。

### 审查结论
- **main 代理审阅**（逐文件 diff + 亲测）：发现并修复 3 项——
  - **P1** `DELETE /api/ai/key` 偶发 500：Windows 上 `os.replace` 被杀软/索引瞬时锁（`PermissionError`）→ `_atomic_write` 对 `PermissionError` 有界重试（5 次 × 20ms）；复测全量两次 254 通过、不再复现。
  - **P2** `dotenv_path()` 结果假设为 `Path`（str 会崩）→ 三处 `Path(dotenv_path())` 强转。
  - **P2** 「清除本地 Key」在 environment 来源下被隐藏，与规格权限矩阵不符 → 改为显示（environment 下仍只清 `.env`、绝不动环境变量）。
- **独立安全审查**（对抗式，独立运行复现）：**无 P0/P1**，P2×2 + P3×2 → 结论 CONDITIONAL PASS。
  - **P2-1** 并发写/删可能把「读 OSError」误当「文件缺失」而丢其它行 → 修复：`_read_raw` 仅 `FileNotFoundError` 视为缺失 + 瞬时读锁重试 + `_ENV_LOCK` 串行化读写改。
  - **P2-2** POST 接受含换行的 Key 会破坏 `.env` 布局（残留半个行）→ 修复：Web 层 400 + `write_env_key` `ValueError`。
  - **P3-1** `Host`/`Origin` 接受 userinfo/路径/畸形端口 → 修复：两项解析函数拒绝之（`parts.port` 校验、userinfo/path/query 检查）。
  - **P3-2** 「清除本地 Key」不清空密码输入框 → 修复：`clearKey` 请求后清空。
- **独立复审**（复审代理独立复现全部 4 项修复 + 回归）：**PASS**，四项均确认修复、无新缺陷。
- 修复后全量 **254 passed**（含无 Key 环境重跑）、`compileall` 0、`git diff --check` 通过、工作树干净。

### 修复记录（2026-09-01，提交 `f5204c3`）
main 代理审阅 + 独立安全审查 + 独立复审确认的修复，全部包含在该提交：
- **P1** `DELETE /api/ai/key` 偶发 500：`os.replace` 被 Windows 杀软/索引瞬时锁（`PermissionError`）→ `_atomic_write` 对 `PermissionError` 有界重试（5 次 × 20ms），持久错误仍传播、原文件不损坏。
- **P2** 并发写/删丢行：`write_env_key` 原把任意读 `OSError` 当「文件缺失」→ `_read_raw` 仅 `FileNotFoundError` 返回缺失、瞬时读锁重试、持久错误传播；`_ENV_LOCK`（模块级 `RLock`）串行化 `write_env_key`/`delete_env_key` 的读-改-写。
- **P2** 换行注入破坏 `.env`：Web 层拒绝含 `\n`/`\r` 的 Key（400）；`write_env_key` 同样 `ValueError`。
- **P3** `Host`/`Origin` 形近绕过：`_is_loopback_host`/`_is_loopback_origin` 拒绝 userinfo、路径、query/fragment、畸形端口。
- **P2** UI 权限矩阵：environment 来源下「清除本地 Key」由隐藏改为显示（仍只清 `.env`、绝不动环境变量）；`clearKey` 请求后清空密码输入框。
- 补回归测试：换行拒绝、瞬时读重试、持久读不覆盖、`Host`/`Origin` 收紧用例（`tests/test_env_store.py`、`tests/test_web_server.py`）。

### 注意事项
- 未 push、未打 tag、未建 Release、未改写历史。
- 「会话令牌不落盘、服务停止即失效」；令牌不是 API Key，仅用于阻止其它网页对 localhost 的未授权写/删。
- `.env` 为明文本地配置，仅适用于本机个人使用；`.env.example` 是唯一入库的模板（占位值）。

## Step 2 — Web API 多文件上传

### 状态：✅ 验收通过（2026-09-01）

### 范围
- 让本机 Web API 支持一次上传多个文件并按同一任务类型处理。本步只做 Web API 批量上传协议与后端处理能力，不改 UI、不改最终下载规则（单文件直下载 `.md` / 多文件根目录 ZIP 留到 Step 3）、不改 CLI 契约、不削弱 Key/.env/会话令牌安全逻辑、不引入新依赖。

### 协议
- 新增 `uploads` 数组字段（每项 `{filename, data_b64}`）；旧单文件 `upload` 字段保持兼容（内部归一化为单元素 `uploads`）。
- 同时传 `upload` 与 `uploads` → 400；`uploads` 非非空数组 → 400；超过 `MAX_UPLOAD_FILES`（50）→ 400；单个请求总大小仍受 `MAX_BODY_BYTES` 限制。
- 文件名沿用既有 `_safe_upload_filename`（非空、无 `/`/`\`、非 `.`/`..`）；同请求内文件名大小写不敏感去重（`A.md`/`a.md` → 400）。
- 扩展名沿用 `_ALLOWED_EXTENSIONS`（convert/pipeline→`.pdf`，clean/ai-clean→`.md`）；任一文件非法或 base64 无效 → 整请求 400、不创建 job、不写临时文件。
- 仍用 base64 JSON，未切换 multipart/form-data。

### 改动内容
- `format_converter/web_server.py`：新增 `MAX_UPLOAD_FILES = 50` 常量与 `_parse_uploads(payload)` 归一化函数；`_handle_submit` 改为「归一化 → 逐文件校验名/重复/扩展名 → 解码全部 base64（写盘前完成）→ `_prepare_job`」；`_prepare_job` 与 `_build_params` 改收 `uploads: list[tuple[str, bytes]]`——单文件保持原 `file` 参数（旧测试兼容），多文件切目录模式（convert/clean 传 `input_dir`/`output_dir`，pipeline 仍 `pdf_dir`/`md_dir`，ai-clean 传 `input_dir`/`output_dir`）。下载 ZIP 规则未改。
- `format_converter/jobs.py`：`_handle_ai_clean` 新增目录批量分支——params 含 `input_dir`/`output_dir`（无 `file`）时遍历 `input_dir/*.md` 逐个调用 `ai_clean(..., output=output_dir/<stem>.ai.md)`，返回全部输出路径；单文件 `file` 分支逐字不变（CLI 契约不变）。`_handle_convert`/`_handle_clean`/`_handle_pipeline` 未改（已支持目录模式）。

### 测试
- `tests/test_web_server.py`（+15）：`TestMultiUpload`（批量 clean/convert/pipeline/ai-clean e2e + 单文件 `upload` 兼容）、`TestMultiUploadValidation`（空数组/非数组/同时传/重复名/危险名/混合扩展名/无效 base64/超限/不创建部分输出）。新增辅助 `_post_jobs`/`_post_raw` 与 `_fake_convert_directory`/`_fake_run_pipeline_batch`。
- `tests/test_jobs.py`（+1）：`TestAIClean::test_directory_batch_success_offline`（注入 `EchoClient`，离线、无真实 Key，直测 `_handle_ai_clean` 目录模式）。

### 测试证据
- `pytest tests/test_web_server.py` → **60 passed**；`pytest tests/test_jobs.py` → **16 passed**；全量 `pytest` → **269 passed**（254 + 15）；`compileall -q .` → 0；`git diff --check` → 通过（仅 autocrlf 提示）；所有临时目录已删除；`git status --short` 仅含本步 4 文件。

### 注意事项
- 未 push、未 tag、未建 Release、未使用真实 API Key、未改 Step 3 下载规则、未做 UI 美化。
- Web 仍只绑定 `127.0.0.1`；Key/.env/会话令牌安全逻辑未削弱（无 CORS、无 Key 落浏览器、写/删仍需令牌 + 回环校验）。
- 旧 `upload` 单文件协议仍可用；`python main.py --help` 仍正常。
