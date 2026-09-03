# FormatConverter 验收清单（Step 5 发布前）

> 本清单把**自动化验收**与**手工验收**分开标注：
>
> - **一、自动化验收结果**：本机（Windows 11，Python 3.13，venv）离线实测通过（2026-09-03，全量 **472 passed / 0 failed / 0 error**；另在无 `ORCAROUTER_API_KEY` 环境下重跑同样全绿）。每项由对应测试模块自动覆盖。
> - **二、手工验收项（待用户执行）**：真实浏览器点击 / 双击 BAT 的体验验收。**尚未在浏览器中人工执行**，自动化仅用 `http.client` / 静态断言复现了同一条 API 流程，故不标注为“已通过”。

---

## 一、自动化验收结果（本机实测通过）

最终门禁：`pytest -q -p no:cacheprovider --basetemp .pytest-tmp-step5` → **472 passed**；`compileall -q .` → 0；`node --check format_converter\web\static\app.js` → 通过；`git diff --check` → 通过。

### 1. CLI（convert / marker / clean / pipeline / ai-clean）

| 验收项 | 覆盖文件 / 用例 | 结果 |
| ---- | ---- | ---- |
| 五条命令参数解析与默认路径 | `tests/test_cli.py` | ✅ |
| `convert` / `clean` / `pipeline` / `marker` / `ai-clean` 的 `main()` 端到端 wiring | `tests/test_cli_commands.py`、`tests/test_cli.py` | ✅ |
| `ai-clean`：坏 Provider / 缺 Key / 覆盖保护 / 非 `.md` / 非 UTF-8 / 纯空白输入 / CRLF 保留 | `tests/test_cli.py`、`tests/test_ai_cleaner.py` | ✅ |
| `ai-clean` 只处理**单文件**（无目录批量、无检查点能力） | `tests/test_cli.py` | ✅ |
| 分块：代码块整体不跨块、不可再分超大块报错不截断、失败不写输出 | `tests/test_ai_cleaner.py` | ✅ |
| 瞬时错误自动重试 / 非瞬时错误不重试 | `tests/test_llm_client.py`、`tests/test_ai_cleaner.py` | ✅ |

### 2. Web 任务 API（localhost，批量 / 下载 / 恢复）

| 验收项 | 覆盖文件 / 用例 | 结果 |
| ---- | ---- | ---- |
| health、静态资源、首页 | `tests/test_web_server.py`、`tests/test_web_ui.py` | ✅ |
| `uploads` 多文件上传（convert/clean/pipeline/ai-clean）与旧 `upload` 兼容 | `tests/test_web_server.py` | ✅ |
| 非法文件名 / 扩展名 / 重复名 / 空上传 / 无效 base64 / 超大请求体 → 400/413 且不写部分输出 | `tests/test_web_server.py` | ✅ |
| **下载规则**：单文件成功 → 直接返回该文件（非 ZIP）；多文件 → ZIP 且**根目录不含 `input/`/`output/`**、同名稳定重命名、绝不静默覆盖 | `tests/test_web_server.py`（`TestDownloadRules`） | ✅ |
| 未完成 409 / 未知 404 / 成功但无输出 404 | `tests/test_web_server.py` | ✅ |
| **最近任务**：`GET /api/jobs` 恢复列表（不含输出路径）、刷新/切换不丢任务 | `tests/test_web_server.py`、`tests/test_web_ui.py` | ✅ |
| **AI 检查点 / 续跑 / 重试 / 删除**：`resume`/`retry`/`DELETE /api/jobs/{id}` 路由与状态机（interrupted→继续、failed→重试、终态→删除；queued/running 拒绝删除；复用已完成块不重复请求） | `tests/test_web_server.py`、`tests/test_jobs.py`、`tests/test_ai_jobs.py` | ✅ |
| 服务重启恢复：running/merging 检查点 → `interrupted` 并重新水合 | `tests/test_ai_jobs.py`、`tests/test_jobs.py` | ✅ |
| `cleanup_job` / `shutdown` 临时目录清理；job_id 路径穿越防护 | `tests/test_web_server.py` | ✅ |

### 3. Web UI 前端（静态与行为）

| 验收项 | 覆盖文件 / 用例 | 结果 |
| ---- | ---- | ---- |
| 四种任务类型分段控制器 + 单一工作面板 + 多文件选择（`multiple`、文件列表/摘要/清空/逐条移除） | `tests/test_web_ui.py` | ✅ |
| 扩展名 / 重复名 / 空文件即时校验 | `tests/test_web_ui.py` | ✅ |
| 下载按钮文案固定为「下载结果」，页面无“下载 ZIP”按钮 | `tests/test_web_ui.py` | ✅ |
| 无浏览器持久化存储 / 无第三方资源 / 无 `console.log` / 仅同源 `/static/` 与 `/api/` | `tests/test_web_ui.py` | ✅ |
| `app.js` 语法：`node --check` | `tests/test_web_ui.py` 与门禁 | ✅ |
| 提交 → 轮询 → 下载端到端（clean 全流程；uploads 单文件） | `tests/test_web_ui.py`、`tests/test_web_server.py` | ✅ |

### 4. AI（Key 优先级 / 模型记忆 / 连接测试 / 检查点安全）

| 验收项 | 覆盖文件 / 用例 | 结果 |
| ---- | ---- | ---- |
| Key 优先级：环境变量 `ORCAROUTER_API_KEY` > 项目根 `.env` > 未配置 | `tests/test_env_store.py`、`tests/test_providers.py` | ✅ |
| `.env` 读写：只改/删 `ORCAROUTER_API_KEY` 行、逐字保留其它行、CRLF/非 UTF-8 字节保留、原子写 + 瞬时锁重试 | `tests/test_env_store.py` | ✅ |
| Key 端点会话令牌 + 回环 Host/Origin 校验；任何响应不含 Key | `tests/test_web_server.py` | ✅ |
| 模型名记忆 `.formatconverter-models.json`：保存/删除/列表/去重/上限/拒绝 `sk-…` 形状 | `tests/test_model_store.py`、`tests/test_web_server.py` | ✅ |
| 连接测试：极小真实请求（`Reply with OK.`）、成功 `ok:true`、失败脱敏映射、需令牌 | `tests/test_web_server.py` | ✅ |
| 缺 Key：CLI / jobs / Web 三层在任何网络请求前失败；消息只含变量名 | `tests/test_cli.py`、`tests/test_jobs.py`、`tests/test_web_server.py` | ✅ |
| 检查点不落 Key / 原始响应；manifest 不含敏感内容 | `tests/test_ai_jobs.py` | ✅ |

### 5. 安全与仓库不变量

| 验收项 | 覆盖文件 / 用例 | 结果 |
| ---- | ---- | ---- |
| 仅监听 `127.0.0.1`；非回环（`0.0.0.0` / `::` / `127.0.0.1.evil`）拒绝 | `tests/test_web_server.py`、`tests/test_launcher.py` | ✅ |
| 无 CORS 头（成功 / 400 / 404 / 409 / 下载 / 静态） | `tests/test_web_server.py`、`tests/test_web_ui.py` | ✅ |
| git 跟踪文件扫描无真实 Key / 无 `.idea` / 无缓存与测试临时目录 | `tests/test_security_invariants.py` | ✅ |
| `.env`、`.formatconverter-models.json`、`.formatconverter-jobs` 未被跟踪、被忽略 | `tests/test_security_invariants.py`、`tests/test_env_store.py` | ✅ |
| 核心模块顶层无网络库 / 全新解释器导入不加载网络客户端 | `tests/test_security_invariants.py` | ✅ |
| 测试不依赖真实 Key：无 `ORCAROUTER_API_KEY` 环境全量仍绿 | 门禁重跑 | ✅ |

---

## 二、手工验收项（待用户执行）

> 在真实浏览器 / 双击环境下逐项人工核对（自动化未覆盖“真人点击”与“真实双击”体验）。

1. **双击 `启动图形界面.bat` 一键启动**
   - 预期：弹出服务窗口，打印 Python 探测与「服务已就绪：http://127.0.0.1:8765/」「按 Ctrl+C 停止」，自动打开浏览器；保持窗口前台运行。
2. **四种任务 + 多文件选择**（页面 http://127.0.0.1:8765/）
   - ① PDF 转 Markdown：一次选 2 个以上 `.pdf` → 文件列表显示数量/大小/可逐条移除 → 提交后处理成功。
   - ② Markdown 清理：选含重复段落的 `.md` → 确认去重、断行合并、列表保留。
   - ③ 转换后清理流水线：选 `.pdf` → 一次完成转换 + 清理。
   - ④ AI 校对：选 `.md`、填模型名；未配置 Key 时应有缺 Key 失败提示；配置后成功。
   - 校验体验：选错扩展名 / 同名不同大小写 / 0 字节文件时页面即时给中文错误并阻止提交。
3. **下载结果**
   - 只选 1 个文件成功后点「下载结果」→ 直接得到该文件（`.md`，**不是 ZIP**）。
   - 选多个文件成功后点「下载结果」→ 得到 ZIP，**根目录直接是 `a.md` 等文件名，不含 `input/` / `output/`**。
   - 按钮文字始终为「下载结果」；页面任何位置不出现「下载 ZIP」按钮文案。
4. **最近任务 / 刷新恢复**
   - 任务「处理中」时切换到其它任务类型再切回 → 该任务仍在并继续更新。
   - 任务完成后按 F5 刷新页面（服务不重启）→ 最近任务仍在；成功任务可再点「下载结果」；运行中任务刷新后继续轮询。
5. **AI 任务中断 → 继续处理（断点续跑）**
   - 提交一个较长的 AI 校对任务，处理中在服务窗口按 Ctrl+C 停止；重新双击启动服务 → 「最近任务」出现「已中断」任务，点「继续处理」从断点续跑（已完成的块不重复请求）。
6. **AI 任务失败 → 重试**
   - 让 AI 任务失败（如断网或模型不可用），确认任务行显示「重试」；修复后点「重试」恢复。
7. **删除（含确认弹窗）**
   - 对成功/失败/已中断任务点「删除」→ 有确认弹窗；确认后任务行消失、再次刷新不出现（输出与检查点已清除）。
8. **模型名记忆与连接测试**
   - 「AI 校对」填模型名 → 点「保存模型」提示已保存；重启服务后仍可从下拉历史选择；「删除模型」后消失。
   - 有效 Key + 模型点「测试连接」→ 显示「连接正常」；未配置 Key / 无效模型 → 显示脱敏错误；页面文字须说明测试会发起真实网络请求、可能产生费用。
9. **停止与端口占用**
   - 服务窗口按 Ctrl+C → 打印「收到 Ctrl+C，正在停止服务...」后退出；再次启动不报端口占用。
   - 先用其它程序占用 `8765` 再启动 → 改用备用端口并提示；`8765` 已被本服务占用 → 提示「服务已在运行」并复用现有实例。
10. **依赖缺失提示**：临时把 `.venv` 改名后双击 BAT → 打印 Python/依赖缺失提示与可复制的安装命令后退出。

---

## 三、文档一致性核对结果（Step 5，本机核对）

| 核对项 | 结果 |
| ---- | ---- |
| 根目录存在标准 MIT `LICENSE`（含 `Copyright (c) 2026 FormatConverter contributors`）且 README 链接它 | ✅ |
| README 引用文件名均真实存在、CLI 命令与 argparse 一致 | ✅ |
| README / CHANGELOG / docs 无“下载 ZIP”作为单文件行为的过时表述；按钮统一「下载结果」 | ✅ |
| README / docs 无旧固定测试数量（如 254 passed）；无真实 Key、无绝对用户路径、无虚假链接 | ✅ |
| `.env` / `.formatconverter-models.json` / `.formatconverter-jobs` 未被 Git 跟踪 | ✅ |
| 全量 pytest / compileall / node --check / git diff --check 通过 | ✅ |
