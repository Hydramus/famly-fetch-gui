# src/famly_fetch/gui/state_store.py
import json
from pathlib import Path

class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self._data = {}

    def seen(self, user: str, photo_id: str) -> bool:
        return photo_id in self._data.get(user, {}).get("seen_ids", [])

    def mark(self, user: str, photo_id: str):
        self._data.setdefault(user, {}).setdefault("seen_ids", [])
        ids = self._data[user]["seen_ids"]
        ids.append(photo_id)
        if len(ids) > 50000:
            self._data[user]["seen_ids"] = ids[-50000:]

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data), encoding="utf-8")
        tmp.replace(self.path)
