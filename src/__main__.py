"""SAGE package entry point for `python -m src`."""

import sys

from src.cli import main

if __name__ == "__main__":
    sys.exit(main())
