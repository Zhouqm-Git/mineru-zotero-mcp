"""Shared config and singletons for tools.

Tools import `get_vault_root()` and `get_mineru_client()` from here so config
is read once and errors are surfaced consistently. This keeps tool functions
short and testable.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from .mineru_client import MineruClient, MineruError


class ConfigError(RuntimeError):
    """Raised when required env config is missing."""


def get_vault_root() -> Path:
    vault = os.environ.get("VAULT_ROOT", "").strip()
    if not vault:
        raise ConfigError(
            "VAULT_ROOT is not set. Point it at your Obsidian vault root "
            "(.raw/<doc_id>/ and attachments/papers/<doc_id>/ will be created there)."
        )
    p = Path(vault).expanduser()
    if not p.is_dir():
        raise ConfigError(f"VAULT_ROOT does not exist or is not a directory: {p}")
    return p


@lru_cache(maxsize=1)
def get_mineru_client() -> MineruClient:
    token = os.environ.get("MINERU_API_TOKEN", "").strip()
    if not token:
        raise ConfigError(
            "MINERU_API_TOKEN is not set. Create one at https://mineru.net "
            "(API Management page) and set it in your environment."
        )
    base_url = os.environ.get("MINERU_BASE_URL", "https://mineru.net")
    return MineruClient(token=token, base_url=base_url)
