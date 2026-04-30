"""Make project-root modules (db, models, app, seed) importable from tests/."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
