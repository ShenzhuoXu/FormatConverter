# FormatConverter 发布检查清单

> 本清单用于发布前逐项核对。所有验证命令在**项目根目录**用 **Windows PowerShell** 执行（不以 Unix-only 命令为准）。`./.venv/Scripts/python.exe` 为虚拟环境解释器，等价写法是 `.\.venv\Scripts\python.exe`。
>
> 红线：**不得 push、不得打 tag、不得创建 GitHub Release、不得使用真实 API Key**。测试一律离线（fake/injected 客户端，不联网、不依赖真实 Key）。

## 1. 测试全量通过（0 failed / 0 error）

- [ ] 全量 pytest（串行执行；`.formatconverter-jobs/` 是共享持久化任务目录，不要并行跑 pytest）：
  ```powershell
  .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp .pytest-tmp-step5
  ```
  - 预期：**0 failed / 0 error**（Step 5 验收实测 2026-09-03 为 **472 passed**）。
  - 测后清理：`Remove-Item -Recurse -Force .pytest-tmp-step5`。
- [ ] 无 `ORCAROUTER_API_KEY` 环境重跑全量（证明测试不依赖真实 Key、无真实网络）。PowerShell（本会话临时移除变量，不影响系统/进程级持久设置）：
  ```powershell
  Remove-Item Env:ORCAROUTER_API_KEY -ErrorAction SilentlyContinue
  .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp .pytest-tmp-step5
  ```
- [ ] 字节码编译：`.\.venv\Scripts\python.exe -m compileall -q .` → 退出码 0。
- [ ] 前端 JS 语法：`node --check format_converter\web\static\app.js` → 通过。
- [ ] diff 空白检查：`git diff --check` → 通过（Windows autocrlf 的 LF→CRLF 提示性 warning 不算错误）。
- [ ] 依赖解析可复现：`.\.venv\Scripts\python.exe -m pip install -r requirements.txt --dry-run` → 退出码 0（无 `ResolutionImpossible`）。
  - 已知约束：`openai` 与 `marker-pdf` 统一在 `openai==1.106.0`（marker-pdf 要求 `openai<2.0.0`），勿再固定 openai 3.x。

## 2. 无真实 Key

- [ ] 全仓（git 跟踪文件）扫描无真实 API Key：
  ```powershell
  git grep -nE "(?!sk-test)sk-[A-Za-z0-9]{12,}"   # 测试中的 sk-test* 短假值除外
  git grep -nE 'ORCAROUTER_API_KEY\s*=\s*["'']?([^"''\s#]+)["'']?'  # 仅 .env.example 的占位符与文档「你的-key」等占位值可命中
  ```
- [ ] `ORCAROUTER_API_KEY` 仅以**变量名**形式出现在文档 / 错误消息中；项目文件中无真实 Key 值（`tests/test_security_invariants.py` 的扫描自动覆盖）。
- [ ] Web API 任何响应（status / save / delete / 400 / 403 / 500 / AI 任务失败消息 / 下载内容）均不含 Key、掩码、长度或可推断信息。
- [ ] AI 检查点目录 `.formatconverter-jobs/` 内的 manifest / 分块 / 结果文件不含 API Key 或 Provider 原始响应（`tests/test_ai_jobs.py` 覆盖）。

## 3. `.env` 允许存在但必须被忽略且不跟踪；无临时文件入库

- [ ] `.env` **允许存在于项目根目录**（Web「保存到本地」/ 手工写入是正式功能）：它是明文本地配置，只适用于本机个人使用；必须满足：未被 Git 跟踪、被 `.gitignore` 忽略、不进入任何 ZIP / 日志 / 异常 / API 响应 / 检查点 / 文档 / 浏览器存储。
  ```powershell
  git ls-files .env            # 期望：无输出（未跟踪）
  git check-ignore .env        # 期望：命中（被忽略）
  ```
- [ ] `.env.example` 已跟踪且仅占位值；`.formatconverter-models.json`（模型名记忆，不含 Key）与 `.formatconverter-jobs/`（AI 检查点）同样被忽略、未被跟踪：
  ```powershell
  git check-ignore .env .formatconverter-models.json .formatconverter-jobs
  git ls-files | Select-String -Pattern '\.env$|\.env\..*\.tmp|\.formatconverter-models|\.formatconverter-jobs|\.pytest-tmp|__pycache__|\.pytest_cache|\.idea'
  ```
- [ ] 测试临时目录与缓存未入库：测试后删除 `.pytest-tmp-step5`；`__pycache__` / `.pytest_cache` 未跟踪。

## 4. 无绝对用户路径

- [ ] 全仓（README / docs / CHANGELOG / 代码）扫描无本机用户绝对路径（如 `C:\Users\<用户名>\`）：
  ```powershell
  git grep -nE 'C:\\Users\\|C:/Users/|/Users/'
  ```

## 5. LICENSE 存在且为标准 MIT 文本

- [ ] 根目录存在文件 `LICENSE`（文件名必须是 `LICENSE`，不是 `LICENSE.txt`）：
  ```powershell
  Test-Path .\LICENSE
  git ls-files LICENSE
  ```
- [ ] 内容为标准 MIT License 文本：含 `MIT License`、`Copyright (c) 2026 FormatConverter contributors`、完整的许可/免责（THE SOFTWARE IS PROVIDED "AS IS"...）三段；不含真实姓名、邮箱、API Key 或本机路径。
- [ ] README 含 License 章节并链接到 `LICENSE`。

## 6. README 与实际一致

- [ ] README 引用的文件名均实际存在且被跟踪：`LICENSE`、`main.py`、`启动图形界面.bat`、`format_converter/web_server.py`、`format_converter/cli.py`、`format_converter/web/static/app.js`、`.env.example`、`.github/workflows/tests.yml` 等（用 `git ls-files` 核对）。
- [ ] README 的 CLI 命令 / 参数与 `format_converter/cli.py` 的 argparse 定义一致（convert / marker / clean / pipeline / ai-clean；`ai-clean` 明确为**单文件**）。
- [ ] README 的 Web UI 下载行为与实际一致：单文件**直接下载该文件**、多文件才打包 ZIP、ZIP **根目录不含 `input/` / `output/`**、页面按钮固定为「下载结果」（无“下载 ZIP”作为单文件说明的过时表述）。
- [ ] README 的 BAT / PowerShell 启动、端口占用（复用/回退）、Key 来源优先级（环境变量 > `.env` > 未配置）、`.env` 明文本地配置等表述与代码一致。
- [ ] README 指向 OrcaRouter 的链接仅为实际存在的官方域名，无虚构邀请码 / 推广链接 / 新域名 / badge / 截图 / 在线 Demo。

## 7. AI 检查点 / 续跑 / 重试 / 删除行为（当前实现，如实核对）

- [ ] Web 端 AI 校对任务（单个或多个 `.md`）走持久化检查点：逐块原子写入项目根目录 `.formatconverter-jobs/<任务>/`，完成后合并 `final.md`（`format_converter/ai_jobs.py` + `format_converter/jobs.py`；`tests/test_ai_jobs.py` 覆盖）。
- [ ] 任务状态含 `interrupted`（已中断）与分块进度 `current`/`total`；「最近任务」对已中断 AI 任务显示「继续处理」、失败显示「重试」、终态显示「删除」（前端文案逐字核对 `format_converter/web/static/app.js`）。
- [ ] `POST /api/jobs/{id}/resume` 与 `POST /api/jobs/{id}/retry` 都**复用磁盘检查点**：已存在且可读的 `results/NNNN.md` 不重复请求（只重跑缺失块）；`DELETE /api/jobs/{id}` 同时清理临时输出目录与检查点，运行中（queued/running）任务拒绝删除。
- [ ] 服务重启恢复：启动时把上次 running / merging 的检查点标记为 `interrupted` 并重新水合进「最近任务」，可继续处理。
- [ ] CLI `ai-clean` 仍为单文件直调、无检查点 / 无 resume / retry / delete 能力（README 已明确区分 CLI 单文件与 Web 多文件）。
- [ ] 检查点与响应不含 API Key / 绝对路径（消息经 `_sanitize_message` 脱敏；`output_paths` 不下发）。

## 8. 不 push / 不创建 Release / 不调用真实 API

- [ ] 本步骤仅修改工作区文件并本地提交，**未** `git push`、**未**打 tag、**未**创建 GitHub Release、**未**调用真实 AI API、**未**使用真实 API Key。
- [ ] 版本号 `format_converter/__init__.py` 的 `__version__` 与文档描述一致（当前 `0.2.1`），全仓无互相矛盾的版本引用。
