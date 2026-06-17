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
from .recommendations_bridge import RecommendationsConfig, _DEFAULT_URL
from .webdav_bridge import WebDAVConfig, ZoteroAPIConfig


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


@lru_cache(maxsize=1)
def get_zotero_api_config() -> ZoteroAPIConfig:
    """Zotero Web API credentials (needed for attachment creation + PATCH).

    Required for arxiv_ingest_paper / zotero_webdav_upload.
    Reads ZOTERO_USER_ID (preferred), falling back to ZOTERO_LIBRARY_ID,
    then to ZOTERO_ID (the variable name zotero-arxiv-daily uses) so a
    single shared .env works across both projects. Same fallback chain
    for the API key (ZOTERO_API_KEY -> ZOTERO_KEY).
    """
    user_id = os.environ.get("ZOTERO_USER_ID", "").strip()
    if not user_id:
        user_id = os.environ.get("ZOTERO_LIBRARY_ID", "").strip()
    if not user_id:
        user_id = os.environ.get("ZOTERO_ID", "").strip()
    api_key = os.environ.get("ZOTERO_API_KEY", "").strip()
    if not api_key:
        api_key = os.environ.get("ZOTERO_KEY", "").strip()
    return ZoteroAPIConfig(user_id=user_id, api_key=api_key)


@lru_cache(maxsize=1)
def get_webdav_config() -> WebDAVConfig:
    """WebDAV storage credentials for Zotero file sync.

    Optional — if unset, WebDAV upload tools return a clear error and
    arxiv_ingest_paper degrades to local-only sync.
    """
    return WebDAVConfig(
        url=os.environ.get("WEBDAV_URL", "").strip(),
        user=os.environ.get("WEBDAV_USER", "").strip(),
        password=os.environ.get("WEBDAV_PASS", "").strip(),
    )


@lru_cache(maxsize=1)
def get_recommendations_config() -> RecommendationsConfig:
    """Base location of zotero-arxiv-daily recommendation snapshots.

    Optional — if unset, defaults to the public fork's raw GitHub path.
    May be an ``http(s)`` directory URL or a local directory path.
    """
    return RecommendationsConfig(
        url=os.environ.get("RECOMMENDATIONS_URL", "").strip() or _DEFAULT_URL
    )
