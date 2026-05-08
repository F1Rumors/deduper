#!/usr/bin/env python3
"""Backwards-compatible entry point.  Delegates to deduper.cli.main."""

import sys
import os

# Allow running as ./deduper.py from anywhere.
# __file__ is inside the deduper package directory; we need its *parent*
# (i.e. the claude/ directory) on sys.path so that "from deduper.cli import main"
# resolves to deduper/cli.py correctly.
_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _parent)

# Remove the CWD from sys.path so that local files in the working directory
# (e.g. exiftool.py at the project root) cannot shadow installed packages.
_cwd = os.getcwd()
sys.path = [p for p in sys.path if p not in ('', '.', _cwd)]
sys.path.insert(0, _parent)  # keep it first after filtering

from deduper.cli import main

if __name__ == "__main__":
    sys.exit(main())
