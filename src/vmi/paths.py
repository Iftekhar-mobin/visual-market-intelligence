"""Where things live on disk.

One module owns the filesystem layout so that a run started from the CLI, the
API or the Streamlit console writes to exactly the same places.
"""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
"""The repository root — `src/vmi/paths.py` is two directories deep."""


def project_path(value: str | os.PathLike[str]) -> Path:
    """Resolve *value* against the repository root unless it is absolute.

    Config files carry short relative paths (`data/runs`) because they read
    better and survive being moved between machines; everything downstream wants
    an absolute path.
    """
    path = Path(value)
    return path if path.is_absolute() else (PROJECT_ROOT / path)


CONFIG_DIR = PROJECT_ROOT / "configs"
DEFAULT_CONFIG = CONFIG_DIR / "default.yaml"
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
