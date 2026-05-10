# FormatConverter

本项目用于把 `pdfs/` 目录里的 PDF 转成 Markdown，并对生成的 Markdown 做段落合并、列表保留和重复段落清理。

## 目录结构

```text
FormatConverter/
  pdfs/                  # 原始 PDF
  mds/                   # 生成和清理后的 Markdown
  format_converter/      # 可维护的核心代码
    cli.py               # 命令行入口
    config.py            # 默认路径
    markdown_cleaner.py  # Markdown 清理逻辑
    pdf_converter.py     # PDF 转 Markdown 逻辑
    pipeline.py          # 转换 + 清理流水线
  main.py                # 本地运行入口
  convert.py             # 兼容旧脚本入口
  convert2.py            # marker-pdf 单文件转换入口
  clean_md.py            # 兼容旧脚本入口
  clean_md_keep_lists.py # 兼容旧脚本入口
  join_paragraphs.py     # 兼容旧脚本入口
```

## 常用命令

从项目根目录运行：

```powershell
.\.venv\Scripts\python.exe main.py convert
```

这会把 `pdfs/` 下的所有 PDF 转成同名 `.md` 文件，输出到 `mds/`。

```powershell
.\.venv\Scripts\python.exe main.py clean
```

这会清理 `mds/` 下的 Markdown。默认会保留列表换行、删除重复段落，并生成 `.bak.md` 备份。

```powershell
.\.venv\Scripts\python.exe main.py pipeline --overwrite
```

这会重新转换 PDF，并清理输出结果。

## 单文件用法

```powershell
.\.venv\Scripts\python.exe main.py convert --file .\pdfs\国防教育.pdf
.\.venv\Scripts\python.exe main.py clean --file .\mds\国防教育.md
```

## 维护说明

原来的脚本已经改成兼容入口。以后需要改转换逻辑时，优先修改 `format_converter/pdf_converter.py`；需要改 Markdown 清理规则时，优先修改 `format_converter/markdown_cleaner.py`。
