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
| Step 2 | （待工作单） | ⬜ 未开始 | — | — |
| Step 3 | （待工作单） | ⬜ 未开始 | — | — |
| Step 4 | （待工作单） | ⬜ 未开始 | — | — |
| Step 5 | （待工作单） | ⬜ 未开始 | — | — |
| Step 6 | （待工作单） | ⬜ 未开始 | — | — |
| Step 7 | （待工作单） | ⬜ 未开始 | — | — |

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
- 依赖闭环核验：`tests/test_llm_client.py` 顶层 `import httpx2`，而 `httpx2` 是 `openai==3.6.0` 的传递依赖（`httpx2<3,>=2.7.0`），CI 安装 requirements.txt 后自动可用，无缺口。

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
