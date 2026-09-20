"""Extension -> category classification, driven by config.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from utils import get_config_path

DEFAULT_CATEGORY = "Other"


class Categorizer:
    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path or get_config_path()
        self.categories_map: Dict[str, List[str]]
        self._date_folders_allowed: Dict[str, bool]
        self.categories_map, self._date_folders_allowed = self._load(self.config_path)
        if DEFAULT_CATEGORY not in self.categories_map:
            self.categories_map[DEFAULT_CATEGORY] = []
        self._ext_to_category: Dict[str, str] = {}
        for category, extensions in self.categories_map.items():
            if category == DEFAULT_CATEGORY:
                continue
            for ext in extensions:
                self._ext_to_category[ext.lower()] = category

    @staticmethod
    def _load(config_path: Path) -> tuple[Dict[str, List[str]], Dict[str, bool]]:
        """Parse config.json.

        Each category maps to either a plain list of extensions (the common
        case, --date-folders applies normally), or an object
        ``{"extensions": [...], "date_folders": false}`` for a category that
        must never be split into Year/MonYear subfolders even when
        --date-folders is passed — e.g. Executables, where an installed
        app's own files getting sorted into different date folders by their
        individual modified times would scatter the app's own dependencies
        relative to each other and break it.
        """
        try:
            with open(config_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(
                f"ERROR: Could not load configuration file '{config_path}': {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise SystemExit(f"ERROR: Configuration file '{config_path}' must contain a JSON object")

        extensions_by_category: Dict[str, List[str]] = {}
        date_folders_allowed: Dict[str, bool] = {}
        for category, value in data.items():
            if isinstance(value, list):
                extensions = value
                allow_date_folders = True
            elif isinstance(value, dict):
                extensions = value.get("extensions", [])
                if not isinstance(extensions, list):
                    raise SystemExit(
                        f"ERROR: Category '{category}' in '{config_path}': "
                        f"'extensions' must be a list"
                    )
                allow_date_folders = bool(value.get("date_folders", True))
            else:
                raise SystemExit(
                    f"ERROR: Category '{category}' in '{config_path}' must map to a list "
                    f"of extensions, or an object with an 'extensions' list"
                )
            extensions_by_category[category] = [str(ext).lower() for ext in extensions]
            date_folders_allowed[category] = allow_date_folders
        return extensions_by_category, date_folders_allowed

    def categorize(self, filename: str) -> str:
        ext = Path(filename).suffix.lower()
        return self._ext_to_category.get(ext, DEFAULT_CATEGORY)

    def category_names(self) -> List[str]:
        return list(self.categories_map.keys())

    def is_known_category(self, name: str) -> bool:
        return name in self.categories_map

    def allows_date_folders(self, category: str) -> bool:
        """False if this category must never be split into Year/MonYear subfolders."""
        return self._date_folders_allowed.get(category, True)
