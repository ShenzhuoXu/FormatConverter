# Changelog

本项目的所有重要更改都记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

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
