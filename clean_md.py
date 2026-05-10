import sys

from format_converter.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["clean", "--flatten-lists", *sys.argv[1:]]))
