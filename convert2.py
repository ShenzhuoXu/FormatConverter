import sys

from format_converter.config import PDF_DIR
from format_converter.cli import main


if __name__ == "__main__":
    default_pdf = PDF_DIR / "南洋学辅2022年军理资料.pdf"
    marker_args = sys.argv[1:] or [str(default_pdf)]
    raise SystemExit(main(["marker", *marker_args]))
