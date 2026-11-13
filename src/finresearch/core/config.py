"""Configuration files.

Every tunable number lives in a TOML file under ``config/`` (CLAUDE.md: no
magic numbers in code). TOML is read by the standard library, so configuration
adds no dependency.
"""

import tomllib
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"


def load_toml(name: str, config_dir: Path = CONFIG_DIR) -> dict[str, Any]:
    """Read ``<config_dir>/<name>.toml``."""
    with (config_dir / f"{name}.toml").open("rb") as fh:
        return tomllib.load(fh)
