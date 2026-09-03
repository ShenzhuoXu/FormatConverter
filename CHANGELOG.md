# Changelog

本项目的所有重要更改都记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 变更

- Web UI 重设计：改为「顶部任务类型分段控制器 + 单一工作面板」布局，去掉四张重复功能卡片。页面第一屏即为工具工作台，顶部显示「本地服务就绪」「数据不出站」徽标，底部保留隐私免责栏；仅「AI 校对」模式下才展开 API Key 配置区与模型名输入。图标使用原生 emoji，无外部资源、无外链字体/图片/样式/脚本。
- Web 下载行为改进：单个最终输出文件时，`GET /api/jobs/{id}/download` 直接返回该文件（`Content-Disposition` 带文件名，`Content-Type` 按 `mimetypes` 判断），不再打包 ZIP；多个最终输出文件时仍返回 ZIP，但 ZIP 内不再包含 `input/` / `output/` 文件夹，条目使用最终产物文件名（如 `a.md`、`a.ai.md`）。同名条目稳定重命名（`doc.md` → `doc-2.md`、`doc.ai.md` → `doc.ai-2.md`），不静默覆盖。

### 修复

- 修复任务进度丢失：切换任务模式或刷新页面后，前端通过 `GET /api/jobs` 从当前服务进程恢复最近任务列表（类型 / 状态 / 更新时间 / 失败 message 均脱敏），运行中任务继续轮询、成功任务仍可「下载结果」。前端改为按 job 独立追踪轮询，不再有一个会被模式切换清空的全局 job id；服务进程重启后旧任务可消失（输出在临时目录中，符合预期）。
- Web 上传 base64 解码改为严格校验：`base64.b64decode(data_b64, validate=True)`。此前默认模式会丢弃非法字符，导致 `YQ==!!!!` 这类含非法后缀的 payload 被剥离后解出非空 bytes 并接受（返回 202）。现此类 payload 返回 400 且不创建 job / 不写临时文件。

### 新增

- AI 模型名本地记忆：模型输入改为「可输入 + 下拉选择历史」的组合输入（`<datalist>`），可保存 / 删除模型名；模型名仅写入项目根目录 gitignored 的 `.formatconverter-models.json`（去重、大小写敏感、上限 50、不含 API Key），提交 AI 任务时自动记住当前模型，重启服务后仍可下拉选择。
- AI 连接测试：`POST /api/ai/connection-test` 用当前 Key 与当前模型做一次极小真实请求（`Reply with OK.`），成功返回 `{"ok": true}`，失败返回脱敏错误（未配置 Key / 认证失败 / 无权限 / 无法连接 / 限流 / 服务器错误 / 空响应），不把 Key、请求体、响应原文回显；写 / 测接口与 Key 端点一样要求会话令牌与回环 Host/Origin（缺失 403）。页面明确提示「测试会向 OrcaRouter 发起真实网络请求，可能产生费用」。
- Web 前端支持多文件选择：文件选择框加 `multiple`，可一次选择多个文件（点击或拖拽）并统一通过 `uploads` 数组提交（单文件同样走 `uploads`，前端不再只读取第一个文件）。页面展示已选文件列表（文件名 / 大小 / 状态 / 移除按钮）、数量与总大小摘要、「清空列表」按钮；选择或拖拽时即校验扩展名（convert/pipeline→`.pdf`，clean/ai-clean→`.md`）、重复文件名（大小写不敏感）与空文件，任一不符即阻止提交并明确提示。
- Web API 支持多文件上传：`POST /api/jobs` 现接受 `uploads` 数组字段（每项含 `filename` / `data_b64`），一次提交多个文件并按同一任务类型处理。`convert`/`clean`/`pipeline` 在多文件时切换为目录模式复用既有 worker；`ai-clean` 新增目录批量模式（遍历 `input_dir` 下 `.md`，逐个输出 `<stem>.ai.md`），单文件 CLI 契约不变。单次上传上限 50 个文件；同请求内文件名大小写不敏感去重；任一文件名/扩展名/base64 非法则整请求 400、不创建部分输出。旧单文件 `upload` 字段保持兼容（同时传 `upload` 与 `uploads` 返回 400）。
- AI 任务可靠性（持久化检查点）：Web 端的 AI 校对任务（单个或多个文件）改为逐“块”调用 AI，并把清单、输入、分隔符、分块与每块结果**原子写入**项目根目录 gitignored 的 `.formatconverter-jobs/<任务>/` 检查点目录，完成后合并出 `final.md`；任务状态新增 `interrupted`（已中断），任务响应携带分块进度 `current`/`total`（页面显示 `AI 校对中 · N / M`）。
- AI 任务继续处理 / 重试 / 删除：新增 `POST /api/jobs/{id}/resume`、`POST /api/jobs/{id}/retry`、`DELETE /api/jobs/{id}`；「最近任务」对已中断 AI 任务提供「继续处理」、对失败 AI 任务提供「重试」，两者都复用磁盘检查点——**已完成的块结果不重复请求、不重复计费**；删除同时清理临时输出目录与检查点（前端弹窗确认）。服务重启时把上次处于 running / merging 的检查点标记为 `interrupted` 并重新水合进「最近任务」，可继续处理。
- AI 校对逐块瞬时错误自动重试：网络中断 / 限流 / 服务端 5xx 等瞬时错误最多重试 4 次（间隔约 1 / 2 / 4 秒）；认证失败、无权限、请求非法、模型不存在等非瞬时错误立即失败、不重试。`is_retryable_llm_error` 只把 `ConnectionFailedError` / `RateLimitError` / `ServerError` 判为可重试。
- 模型名安全边界：模型名拒绝 `sk-…` 形状（避免把 API Key 误填成模型名），检查点 manifest 与任何写入文件都不含 API Key、Provider 原始响应等敏感内容（`tests/test_ai_jobs.py` 覆盖）。

### 文档与发布整理（Step 5）

- 新增根目录 MIT License（`LICENSE`），README 增加 License 章节并链接。
- 重构 README：项目简介 / 功能概览 / 快速开始（BAT 与 PowerShell、访问地址）/ Web UI 使用说明（四种任务、多文件选择、单文件直接下载、多文件 ZIP 且根目录不含 `input/`、`output/`）/ CLI 用法 / AI 校对（Key 优先级、`.env`、模型记忆与删除、连接测试、CLI 单文件 vs Web 多文件、检查点与续跑）/ 隐私、安全与限制 / 常见问题 / 开发与测试 / 项目结构 / License。
- 发布检查清单、验收清单与实现状态文档同步当前行为（当前全量测试数、批量下载规则、AI 检查点 / 续跑 / 重试 / 删除能力、LICENSE 核验）。

### 移除

- 删除 5 个历史兼容脚本：`convert.py`、`convert2.py`、`clean_md.py`、`clean_md_keep_lists.py`、`join_paragraphs.py`。它们只是转发到 `format_converter.cli.main` 的薄包装（`join_paragraphs.py` 的能力是 `clean` 命令的子集），功能已完全由 `python main.py convert / marker / clean / pipeline` 与图形界面覆盖，且无任何代码、测试或启动脚本引用。当前入口统一为：图形界面（双击 `启动图形界面.bat`）、CLI（`python main.py ...`）、Web 服务（`python -m format_converter.web_server`）。

## [0.2.1] - 2026-09-01

### 新增

- Web UI「AI 校对」卡片新增「OrcaRouter API 配置」区域：显示 Key 状态（已配置 / 未配置）与来源（系统环境变量 / 本地 `.env` / 未配置），可把 API Key 保存到本机项目根目录 `.env`、清除本地 Key、重新检测；页面明确说明 Key 仅保存在本机 `.env`、不保存到浏览器，且执行 AI 校对时后端会真实调用 OrcaRouter。
- API Key 来源优先级固定为：系统环境变量 `ORCAROUTER_API_KEY` > 项目根目录 `.env` > 未配置。环境变量缺失时，AI 任务每次执行都会重新读取 `.env`（CLI `ai-clean` 与 Web AI 任务均适用）。
- 新的本地 Web API：`GET /api/ai/key-status`、`POST /api/ai/key`、`DELETE /api/ai/key`。写入/删除接口校验服务启动时生成的**仅内存会话令牌**（`secrets.token_urlsafe(32)`，服务停止即失效、绝不落盘）与回环 `Host`/`Origin`；任何缺失或不合法即拒绝。令牌经服务端注入到所服务的 `index.html`（静态文件仅含占位符），响应带 `Cache-Control: no-store`。
- 新增 `format_converter/env_store.py`：严格的本地 `.env` 解析（不使用 python-dotenv），只更新/删除 `ORCAROUTER_API_KEY`，其它内容（含注释、空行、非 UTF-8 字节）逐字保留；采用「临时文件 + 原子 replace」写入，失败不损坏原文件；并发写入/删除经锁串行化。
- 新增 `.env.example`（仅占位值 `your_api_key_here`）；`.env` 继续被 `.gitignore` 忽略。

### 安全强化

- 写入/删除接口的 `Host`/`Origin` 校验拒绝 userinfo、路径、畸形端口等形近绕过。
- API Key 值拒绝嵌入换行（`\n`/`\r`），避免破坏 `.env` 布局。
- `.env` 读写在 Windows 上对瞬时文件锁（杀软/索引扫描）自动重试，不再把「读不到」误判为「文件不存在」而覆盖其它配置。
- `jobs._sanitize_message` 同时屏蔽环境变量与 `.env` 中的 Key 值（防御性兜底）。

## [0.2.0] - 2026-08-31

### 新增

- 可选 CLI AI 校对命令 `ai-clean`（OrcaRouter）：对单个 Markdown 文件做 OCR 纠错与排版修正；API Key 仅从环境变量 `ORCAROUTER_API_KEY` 读取，不落盘、不入日志、不作为命令行参数。
- 本机 Web UI：仅监听 `127.0.0.1` 的单页图形界面，四张功能卡片（PDF 转 Markdown / Markdown 清理 / 转换后清理流水线 / AI 校对）+ JSON 任务 API（`GET /health`、`POST /api/jobs`、`GET /api/jobs/{id}`、`GET /api/jobs/{id}/download`）。
- Windows 一键启动脚本 `启动图形界面.bat`：双击即可启动本地服务并打开默认浏览器。
- 任务服务层 `format_converter/jobs.py`：统一的后台任务模型，供 CLI 与 Web UI 复用。
- GitHub Actions 测试工作流 `.github/workflows/tests.yml`：在 `windows-latest` 上对 Python 3.12 / 3.13 运行全量测试与编译检查。

### 修复

- 修复 `requirements.txt` 中 `openai==3.6.0` 与 `marker-pdf==1.10.2` 的依赖版本冲突（marker-pdf 要求 `openai>=1.65.2,<2.0.0`）：统一降级到 `openai==1.106.0`，AI 校对客户端保持兼容（错误映射、OpenAI 兼容端点不变），相关测试适配（`httpx2` → `httpx`，openai 1.x 的 HTTP 客户端）。`pip install -r requirements.txt` 现在可正常解析安装。

## [0.1.0] - 2026-08-30

- 初始版本：`convert` / `marker` / `clean` / `pipeline` CLI 与 Markdown 清理能力。
