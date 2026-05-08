"""Simple file-based key-value storage for Flet 0.84.0+.

Replaces the removed page.client_storage API.
"""

import json
import os
from pathlib import Path


class Storage:
    """File-based key-value store.

    Falls back to current directory if page.storage_paths is not available.
    """

    def __init__(self, page):
        self._file = self._resolve_path(page)
        self._data = self._load()

    @staticmethod
    def _resolve_path(page) -> Path:
        try:
            paths = page.storage_paths
            if paths and hasattr(paths, 'data'):
                return Path(paths.data) / "app_storage.json"
        except Exception:
            pass
        return Path("app_storage.json")

    def _load(self) -> dict:
        try:
            if self._file.exists():
                with open(self._file, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save(self):
        self._file.parent.mkdir(parents=True, exist_ok=True)
        with open(self._file, "w", encoding="utf-8") as f:
            json.dump(self._data, f)

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value):
        self._data[key] = value
        self._save()

    def remove(self, key: str):
        self._data.pop(key, None)
        self._save()
