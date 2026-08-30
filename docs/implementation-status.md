# FormatConverter 实现状态

> 本文件由主控工程 agent 维护，记录各 Step 的进度、提交 SHA、测试证据与审查结论。
> 目标：在保留现有 CLI 和 AI 校对能力的前提下，完成本机 Web UI、Windows BAT 启动、测试、文档和发布准备。

## 总览

| Step | 描述 | 状态 | 提交 | 验收 |
| ---- | ---- | ---- | ---- | ---- |
| Step 0 | 固化现有 CLI 与 AI 校对功能（ai-clean / OrcaRouter） | ✅ 通过 | `4834b2d`（功能）+ `973ff35`（硬化修复） | 103 tests 全绿，独立审查通过 |
| Step 1 | （待工作单） | ⬜ 未开始 | — | — |
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
- 现有功能提交 `4834b2d` 的 message 与工作单指定文案（`feat: add optional OrcaRouter AI Markdown proofreading`）不完全一致；是否 amend 改名待用户确认（见交付汇报）。
