"""Make the repo root importable so `from pc_app import ...` works under pytest."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
