# FormatConverter 发布检查清单（Step 7 / v0.2.1 发布准备）

> 本清单用于发布前逐项核对。所有验证命令在项目根目录（Windows）执行；`./.venv/Scripts/python.exe` 为项目虚拟环境解释器。

## 1. 测试全量通过

- [ ] 全量 pytest：
  `./.venv/Scripts/python.exe -m pytest -p no:cacheprovider --basetemp .pytest-tmp`
  - 预期：**254 passed**，0 failed / 0 error。
  - 测后清理：`rm -rf .pytest-tmp`。
- [ ] 无 `ORCAROUTER_API_KEY` 环境重跑全量（Git Bash）：`env -u ORCAROUTER_API_KEY ./.venv/Scripts/python.exe -m pytest -p no:cacheprovider --basetemp .pytest-tmp -q` → 254 passed（证明测试不依赖真实 Key、无真实网络）。
- [ ] 字节码编译：`./.venv/Scripts/python.exe -m compileall -q .` → 退出码 0。
- [ ] diff 空白检查：`git diff --check` → 成功（Windows autocrlf 的 LF→CRLF 提示性 warning 不算错误）。
- [ ] 依赖解析可复现（全新安装不冲突）：
  `./.venv/Scripts/python.exe -m pip install -r requirements.txt --dry-run` → 成功（退出码 0，无 `ResolutionImpossible`）。
  - 已知约束：`openai` 与 `marker-pdf` 统一在 `openai==1.106.0`（marker-pdf 要求 `openai<2.0.0`），勿再固定 openai 3.x。

## 2. 无真实 Key

- [ ] 全仓扫描无真实 API Key：
  - `git grep -nE 'sk-[A-Za-z0-9]{12,}'` → 无命中（测试中的 `sk-test*` 占位值除外）。
  - `git grep -nE 'ORCAROUTER_API_KEY[[:space:]]*=[[:space:]]*[^[:space:]#]+'` → 除 `.env.example` 的 `your_api_key_here` 与文档占位符「你的-key」外无命中。
- [ ] `ORCAROUTER_API_KEY` 仅以**变量名**形式出现在文档 / 错误消息中，项目文件中无真实 Key 值。
- [ ] Web API 任何响应（status / save / delete / 400 / 403 / 500）均不包含 Key、掩码、长度或可推断信息（`tests/test_web_server.py::TestKeyConfigEndpoints::test_no_key_material_in_any_response` 覆盖）。

## 3. 无临时文件 / 无 .idea / 无 .env

- [ ] `git ls-files | grep -iE '\.env$|\.env\.[0-9a-zA-Z]*\.tmp|\.idea|\.pytest-tmp|__pycache__|\.pytest_cache'` → 无命中（`.env.example` 例外，应被跟踪且仅占位值）。
- [ ] `git status --short` 仅包含预期发布文件：
  - README.md、CHANGELOG.md、docs/implementation-status.md、docs/release-checklist.md（修改）；
  - `.env.example`（新增，仅占位值）、`.gitignore`（新增 `.env.*.tmp`）、`format_converter/__init__.py`（版本号 → 0.2.1）；
  - `format_converter/env_store.py`（新增）、`web_server.py`、`providers.py`、`jobs.py`、`web/static/`（index.html / app.js / styles.css）、`conftest.py`；
  - `tests/`（test_env_store.py 新增；test_providers / test_web_server / test_web_ui / test_security_invariants 修改）。
- [ ] `git check-ignore .env` → 命中（`.env` 文件已被忽略，即使出现也不会被跟踪）。
- [ ] 磁盘上不存在 `.env` 文件：`ls -la .env` → 不存在。
- [ ] `.pytest-tmp` 已在测试后删除；`__pycache__` / `.pytest_cache` 未被 git 跟踪。

## 4. 无绝对用户路径

- [ ] 全仓扫描无本机用户绝对路径（例如 README / docs 中出现 `C:\Users\<用户名>\` 形式的字符串）：
  `git grep -nE 'C:\\\\Users\\\\<用户名>|C:/Users/<用户名>|/Users/<用户名>'` → 无命中。

## 5. README 与实际一致

- [ ] README 引用的文件名均实际存在：`启动图形界面.bat`、`format_converter/web_server.py`、`main.py`、`requirements.txt`、`requirements-dev.txt` 等。
- [ ] README 的 CLI 命令 / 参数与 `format_converter/cli.py` 的 argparse 定义一致（convert / marker / clean / pipeline / ai-clean）。
- [ ] README 的图形界面启动步骤与 `启动图形界面.bat` 的实际行为一致（Python 探测顺序、依赖提示、仅监听 127.0.0.1、Ctrl+C 停止）。
- [ ] README 指向 OrcaRouter 的链接仅为已存在的 `https://www.orcarouter.ai/`，无虚构邀请码 / 推广链接 / 新域名。

## 6. 不 push / 不创建 Release

- [ ] 本步骤仅修改工作区文件，**未** `git push`、**未**创建 GitHub Release、**未**调用真实 API。
- [ ] 版本号：`format_converter/__init__.py` 的 `__version__` 已从 `0.2.0` 更新为 `0.2.1`，全仓无其它版本引用。

## 7. v0.2.1 新增项核验

- [ ] `.env` 仅保存 `ORCAROUTER_API_KEY`，其它内容（注释/空行/无关行）逐字保留（`tests/test_env_store.py` 覆盖，含 CRLF / 非 UTF-8 字节）。
- [ ] `POST`/`DELETE /api/ai/key` 需回环 `Host` + 回环 `Origin` + 仅内存会话令牌；缺失或非法 → 403；令牌服务停止即失效、不落盘（`tests/test_web_server.py::TestKeyConfigEndpoints` 覆盖）。
- [ ] AI 卡片「OrcaRouter API 配置」：Key 仅保存到本机 `.env`、不写入浏览器存储、不 `console.log`、请求后清空输入；文案说明「不宣称 Key 永不联网」（`tests/test_web_ui.py` 覆盖）。
