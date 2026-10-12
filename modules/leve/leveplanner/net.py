"""Small HTTP client: bounded retries, atomic disk cache, explicit stale fallback."""
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ApiError(RuntimeError):
    pass


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


def download(url, timeout=25, opener=urlopen, sleeper=time.sleep):
    for attempt in range(3):
        try:
            req = Request(url, headers={"User-Agent": "FFXIVLevePlanner/1.0 (Python desktop)",
                                        "Accept": "application/json,text/csv"})
            with opener(req, timeout=timeout) as response:
                return response.read()
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            retry = not isinstance(exc, HTTPError) or exc.code in (429, 500, 502, 503, 504)
            if isinstance(exc, HTTPError):
                exc.close()
            if not retry or attempt == 2:
                raise ApiError(f"Не удалось получить {url}: {exc}") from exc
            sleeper(2 ** attempt)


class JsonCache:
    def __init__(self, folder, fetch=download, now=time.time):
        self.folder, self.fetch, self.now = Path(folder), fetch, now
        self.warnings = []

    def get(self, url, ttl=900, max_stale=172800, force=False, validate=None):
        path = self.folder / (hashlib.sha256(url.encode()).hexdigest() + ".json")
        saved = None
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(saved["saved_at"], (int, float)):
                raise ValueError("invalid cache timestamp")
            if validate:
                validate(saved["data"])
        except (OSError, ValueError, KeyError, TypeError):
            saved = None
        age = self.now() - saved["saved_at"] if saved else float("inf")
        if saved and 0 <= age <= ttl and not force:
            return saved["data"]
        try:
            data = json.loads(self.fetch(url))
            if validate:
                validate(data)
            atomic_json(path, {"saved_at": self.now(), "data": data})
            return data
        except (ApiError, ValueError, KeyError, TypeError) as exc:
            if saved and 0 <= age <= max_stale:
                self.warnings.append(f"API недоступен; старый кэш ({age / 3600:.1f} ч): {url}")
                return saved["data"]
            raise ApiError(str(exc)) from exc
