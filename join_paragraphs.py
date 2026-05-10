from pathlib import Path

from format_converter.config import MARKDOWN_DIR
from format_converter.markdown_cleaner import join_soft_wrapped_lines


def join_file(path: Path) -> Path:
    text = path.read_text(encoding="utf-8")
    path.write_text(join_soft_wrapped_lines(text), encoding="utf-8", newline="\n")
    return path


if __name__ == "__main__":
    for markdown_file in sorted(MARKDOWN_DIR.glob("*.md")):
        if not markdown_file.name.endswith(".bak.md"):
            print(f"Processed: {join_file(markdown_file)}")
