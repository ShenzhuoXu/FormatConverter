# FormatConverter 验收清单（Step 6：补测试与修复）

> 本清单记录 Step 6 的**自动化验收结果**与**手工验收项**。自动化部分全部在本机（Windows 11，Python 3.13，venv）离线运行通过；手工部分标注「待用户执行」。

---

## 一、自动化验收结果

最终门禁：`pytest` → **198 passed**（基线 170 + 新增 28）；`compileall -q .` → 0；`git diff --check` → 通过。
另在 `ORCAROUTER_API_KEY` **未设置**环境下重跑全量 → **198 passed**（证明测试套件不依赖真实 Key、无真实网络调用）。

### 1. CLI（convert / marker / clean / pipeline / ai-clean）

| 验收项 | 覆盖文件 / 用例 | 结果 |
| ---- | ---- | ---- |
| 五条命令参数解析 | `tests/test_cli.py::TestParser` | ✅ 既有 |
| `ai-clean` 的 `main()` 端到端（成功 / 坏 Provider / 缺 Key / 覆盖保护 / 非 .md / 非 UTF-8） | `tests/test_cli.py::TestMainAIClean` | ✅ 既有 |
| **`convert` / `clean` / `pipeline` / `marker` 的 `main()` 端到端 wiring**（monkeypatch 假转换/清理函数，断言返回码与输出） | `tests/test_cli_commands.py`（16 条，新增） | ✅ 新增通过 |
| `convert` 空目录 / 无 PDF | `tests/test_cli_commands.py::TestConvertMain::test_convert_empty_directory_returns_zero` / `test_convert_directory_with_non_pdf_files_returns_zero` | ✅ 新增通过（rc=0，打印 0 个文件） |
| `convert` / `clean` 缺失文件、`pipeline` 缺失目录、`marker` 错误的错误处理路径 | `tests/test_cli_commands.py`（`pytest.raises` 文档化既有契约） | ✅ 新增通过（保持既有行为：异常向上传播，**未改动 CLI 行为**） |

### 2. Web（localhost 任务 API）

| 验收项 | 覆盖文件 / 用例 | 结果 |
| ---- | ---- | ---- |
| health（正面 + 无 CORS） | `tests/test_web_server.py::TestHealthAndIndex` | ✅ 既有 |
| 上传（clean 正面 e2e + 各类 400 负面） | `tests/test_web_server.py::TestSubmitAndDownload` / `TestValidation` / `TestFailures` | ✅ 既有 |
| **413 超大请求体** | `tests/test_web_server.py::TestOversizedBody::test_oversized_request_body_413` | ✅ 新增通过 |
| **convert / pipeline / ai-clean 三条全流程 e2e（上传→状态→下载 ZIP）** | `tests/test_web_server.py::TestAdditionalE2E` | ✅ 新增通过 |
| 任务状态（正面 + 未知 job 404） | `tests/test_web_server.py` | ✅ 既有 |
| 下载（正面 ZIP + 未知 404 + 未完成 409 + 失败 409） | `tests/test_web_server.py::TestSubmitAndDownload` / `TestFailures` | ✅ 既有 |
| **下载：succeeded 但无输出文件 → 404** | `tests/test_web_server.py::TestAdditionalE2E::test_download_succeeded_but_no_output_files_404` | ✅ 新增通过 |
| **上传安全（非法文件名 / 扩展名 / 空上传 / 非对象 params）** | `tests/test_web_server.py::TestValidation` | ✅ 既有 |
| **布尔字符串形近值（`"false"`）不翻转行为** | `tests/test_web_server.py::TestAdditionalE2E::test_string_bool_lookalikes_do_not_flip_behavior` | ✅ 新增通过 |
| `cleanup_job`（合法删除 / 非法 id 不删）与 `shutdown` 临时根清理 | `tests/test_web_server.py::TestSecurity` | ✅ 既有 |

### 3. AI（可选校对）

| 验收项 | 覆盖文件 / 用例 | 结果 |
| ---- | ---- | ---- |
| 无 Key → `MissingApiKeyError`（CLI / jobs / Web 三层） | `tests/test_providers.py`、`tests/test_cli.py`、`tests/test_jobs.py`、`tests/test_web_server.py` | ✅ 既有 |
| 坏 Provider（非 orcarouter） | `tests/test_providers.py::TestProviderLookup`、`tests/test_cli.py` | ✅ 既有 |
| 分块失败传播、不写输出 | `tests/test_ai_cleaner.py::TestCleanMarkdownWithAI::test_any_chunk_failure_propagates`、`tests/test_cli.py::TestAIClean::test_failure_does_not_write_output` | ✅ 既有 |
| 非 `.md` 输入拒绝（大小写后缀） | `tests/test_cli.py::TestAIClean` | ✅ 既有 |
| LF / CRLF / lone-CR 换行保留（分块层 + `ai_clean` 集成层） | `tests/test_ai_cleaner.py::TestSplitIntoChunks`、`tests/test_cli.py::TestAIClean::test_crlf_input_preserves_crlf_in_output` | ✅ 既有 |
| **纯空白输入：原样写出、不调客户端** | `tests/test_cli.py::TestAIClean::test_whitespace_only_input_written_verbatim_without_client` | ✅ 新增通过 |
| 超大块（不可分块段落）→ `ChunkTooLargeError` | `tests/test_ai_cleaner.py::TestSplitIntoChunks`、`tests/test_cli.py::TestAIClean::test_oversized_block_fails_without_writing` | ✅ 既有 |

### 4. 安全

| 验收项 | 覆盖文件 / 用例 | 结果 |
| ---- | ---- | ---- |
| 127.0.0.1 绑定 / 非 loopback 拒绝 | `tests/test_web_server.py::TestSecurity`、`tests/test_launcher.py` | ✅ 既有 |
| 无 CORS 头（成功 / 400 / 404 / 409 / 下载） | `tests/test_web_server.py::TestHealthAndIndex`、`test_web_ui.py` | ✅ 既有 |
| **无真实 Key 落盘（全仓扫描：`sk-<12+ alnum>` 与 `ORCAROUTER_API_KEY = "<非占位符>"`）** | `tests/test_security_invariants.py::TestNoKeyOnDisk`（新增） | ✅ 新增通过 |
| **`.idea/`、`.pytest-tmp`、`__pycache__`、`.pytest_cache` 不在 git 跟踪** | `tests/test_security_invariants.py::TestNoKeyOnDisk::test_no_ide_or_cache_artifacts_tracked`（新增） | ✅ 新增通过 |
| 无 Key 前端存储（localStorage / Cookie / Key 输入框） | `tests/test_web_ui.py::TestPageSourceCompliance` | ✅ 既有 |
| **模块依赖守卫：核心模块顶层无 requests/httpx/openai/pymupdf4llm/marker/socket/http.client** | `tests/test_security_invariants.py::TestNoNetworkImports`（新增，AST 扫描） | ✅ 新增通过 |
| **全新解释器导入 web_server/jobs 不加载任何网络客户端** | `tests/test_security_invariants.py::TestNoNetworkImports::test_fresh_import_loads_no_network_clients`（新增，子进程验证） | ✅ 新增通过 |
| 测试套件无需真实 Key（`ORCAROUTER_API_KEY` 未设置全量仍绿） | 门禁重跑（见本文件顶部） | ✅ 通过 |

---

## 二、手工验收项（待用户执行）

> 以下为**真实浏览器 / 双击体验**验收，自动化已用 `http.client` 复现同一 API 流程，但真实点击与双击体验需用户确认。

1. **双击 `启动图形界面.bat` 一键启动**
   - 预期：弹出命令行服务窗口，显示「Using Python …」「Starting FormatConverter local service …」「服务已就绪：http://127.0.0.1:8765/」「按 Ctrl+C 停止」，并自动打开默认浏览器访问 `http://127.0.0.1:8765/`。
   - 验证：服务窗口保持前台运行；浏览器打开后出现「FormatConverter」四张功能卡片页面。
2. **四张功能卡片流程**（在打开的页面中）
   - ① PDF 转 Markdown：选一个 `.pdf`，提交后轮询到成功，点「下载 ZIP」解压得到 `.md`。
   - ② Markdown 清理：选一个 `.md`（含重复段落），提交后下载，确认重复段落被去重且页面有 `.bak.md` 备份。
   - ③ 转换后清理（流水线）：选一个 `.pdf`，提交后下载，确认转换+清理一次完成。
   - ④ AI 校对：选一个 `.md`、填模型名；未设置 `ORCAROUTER_API_KEY` 时应看到缺 Key 的失败提示；设置后（`$env:ORCAROUTER_API_KEY="你的-key"`）成功并下载 `.ai.md`。
3. **停止方式**：在服务窗口按 **Ctrl+C**，确认窗口打印「收到 Ctrl+C，正在停止服务...」后退出；再次启动不报端口占用。
4. **端口占用行为**：先用任意程序占用 `8765` 再双击 BAT，确认服务改用 `8766` 等备用端口并提示「端口 8765 被占用，改用端口 …」；若 `8765` 已被本服务实例占用，确认「服务已在运行」复用现有实例、不重复启动。
5. **依赖缺失提示**：临时把 `.venv` 改名后双击 BAT，确认打印 Python/依赖缺失提示与可复制的安装命令（`python -m venv .venv`、`pip install -r requirements.txt` 等）后退出。

---

## 三、安全清单证据与结论

| 项 | 证据 | 结论 |
| ---- | ---- | ---- |
| 仅监听回环 | `serve()` 硬绑定 `("127.0.0.1", port)`；`_is_loopback` 用 `ipaddress.is_loopback` 严格解析；`0.0.0.0`/`::`/`127.0.0.1.evil` 均被拒（测试覆盖） | ✅ |
| 无 CORS | 所有响应路径不写 `Access-Control-Allow-Origin`（测试对成功/400/404/409/下载实测） | ✅ |
| 无 Key 落盘 | 全仓 git 跟踪文件扫描：无 `sk-[A-Za-z0-9]{12,}`，无 `ORCAROUTER_API_KEY = "<非占位符>"`；README 仅出现占位符「你的-key」；测试用 `sk-test*` 短假值不被误报 | ✅ |
| 无 Key 前端存储 | `index.html` / `app.js` 无 localStorage、Cookie、Key 输入框、外部 CDN/链接（页面合规测试 + `node --check`） | ✅ |
| 无真实网络调用 | 核心模块顶层仅 import 标准库 + 本包；`openai`/`pymupdf4llm`/`marker` 全部函数内惰性导入；全新解释器导入 `web_server`/`jobs` 后 `sys.modules` 无任何网络客户端；测试全离线只连 `127.0.0.1` | ✅ |
| 测试不依赖真实 Key | 环境无 `ORCAROUTER_API_KEY` 时全量 198 测试全绿 | ✅ |
