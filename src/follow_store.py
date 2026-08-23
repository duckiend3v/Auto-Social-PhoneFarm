from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path


import sys

if getattr(sys, "frozen", False):
    ROOT_DIR = Path(sys.executable).resolve().parent
else:
    ROOT_DIR = Path(__file__).resolve().parents[1]

DATA_DIR = ROOT_DIR / "data"
FOLLOW_HISTORY_FILE = DATA_DIR / "follow_history.json"


class FollowHistoryStore:
    """Thread-safe store that tracks which pages each device has already followed."""

    def __init__(self, path: Path = FOLLOW_HISTORY_FILE) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._data: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return dict(raw)
        except (json.JSONDecodeError, Exception):
            return {}

    def _save(self) -> None:
        self.path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Normalize a page URL for consistent comparison."""
        url = url.strip().rstrip("/")
        # Remove trailing query params that don't affect identity
        for prefix in ("https://www.", "http://www.", "https://", "http://"):
            if url.lower().startswith(prefix):
                url = url[len(prefix):]
                break
        return url.lower()

    def get_followed_pages(self, serial: str) -> set[str]:
        """Return the set of normalized page URLs this device has followed."""
        with self._lock:
            entry = self._data.get(serial, {})
            return {self._normalize_url(u) for u in entry.get("followed_pages", [])}

    def mark_followed(self, serial: str, page_url: str) -> None:
        """Record that the device successfully followed a page."""
        normalized = self._normalize_url(page_url)
        with self._lock:
            entry = self._data.setdefault(serial, {"followed_pages": [], "last_updated": ""})
            existing = {self._normalize_url(u) for u in entry["followed_pages"]}
            if normalized not in existing:
                entry["followed_pages"].append(page_url.strip())
            entry["last_updated"] = datetime.now().isoformat(timespec="seconds")
            self._save()

    def get_remaining_pages(self, serial: str, all_pages: list[str]) -> list[str]:
        """Return pages from all_pages that this device has NOT yet followed."""
        followed = self.get_followed_pages(serial)
        return [url for url in all_pages if self._normalize_url(url) not in followed]

    def pick_random_pages(
        self,
        serial: str,
        all_pages: list[str],
        min_count: int = 3,
        max_count: int = 5,
    ) -> list[str]:
        """Pick a random subset of pages that the device hasn't followed yet."""
        import random

        remaining = self.get_remaining_pages(serial, all_pages)
        if not remaining:
            return []
        count = random.randint(min(min_count, max_count), max(min_count, max_count))
        count = min(count, len(remaining))
        return random.sample(remaining, count)

    def clear_history(self, serial: str) -> None:
        """Clear follow history for a specific device."""
        with self._lock:
            self._data.pop(serial, None)
            self._save()

    def clear_all(self) -> None:
        """Clear all follow history."""
        with self._lock:
            self._data.clear()
            self._save()

    def get_stats(self, serial: str, total_pages: int) -> tuple[int, int]:
        """Return (followed_count, total_pages) for display."""
        followed = len(self.get_followed_pages(serial))
        return followed, total_pages
