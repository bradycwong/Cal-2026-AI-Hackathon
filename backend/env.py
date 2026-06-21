"""Project .env loading for server-only configuration."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_project_env(env_path: str | Path | None = None) -> bool:
    """Load non-empty .env values without overriding the real process env."""
    path = Path(env_path) if env_path is not None else PROJECT_ROOT / ".env"
    if not path.exists():
        return False

    loaded = False
    for key, value in dotenv_values(path).items():
        if value in (None, "") or key in os.environ:
            continue
        os.environ[key] = value
        loaded = True
    return loaded
