"""Bounded HTTP retries and validated, atomic JSON cache."""
import hashlib
import json
import os
from pathlib import Path
import time
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ApiError(RuntimeError):
    pass


class Cancelled(ApiError):
    pass


def check_cancel(cancel):
    if cancel is not None and (cancel.is_set() if hasattr(cancel, "is_set") else cancel()):
        raise Cancelled("Анализ отменён")


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


def _retry_after(value, now=time.time):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - now()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, min(15.0, seconds))


def download(url, timeout=20, opener=urlopen, sleeper=time.sleep, cancel=None):
    for attempt in range(3):
        check_cancel(cancel)
        try:
            req = Request(url, headers={"User-Agent": "FFXIVMarketScout/1.0 (Python desktop)",
                                        "Accept": "application/json"})
            with opener(req, timeout=timeout) as response:
                result = response.read()
            check_cancel(cancel)
            return result
        except Cancelled:
            raise
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            retry = not isinstance(exc, HTTPError) or exc.code in (429, 500, 502, 503, 504)
            delay = None
            if isinstance(exc, HTTPError):
                delay = _retry_after(exc.headers.get("Retry-After")) if exc.headers else None
                exc.close()
            if not retry or attempt == 2:
                raise ApiError(f"API недоступен: {exc}") from exc
            remaining = delay if delay is not None else 2 ** attempt
            while remaining > 0:
                check_cancel(cancel)
                step = min(0.25, remaining)
                sleeper(step)
                remaining -= step


class JsonCache:
    def __init__(self, folder, fetch=download, now=time.time):
        self.folder, self.fetch, self.now = Path(folder), fetch, now
        self.warnings = []
        self.saved_times = {}

    def get(self, url, ttl=600, max_stale=172800, force=False, validate=None, cancel=None):
        check_cancel(cancel)
        path = self.folder / (hashlib.sha256(url.encode()).hexdigest() + ".json")
        saved = None
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(saved["saved_at"], bool) or not isinstance(saved["saved_at"], (int, float)):
                raise ValueError("invalid cache timestamp")
            if validate:
                validate(saved["data"])
        except (OSError, ValueError, KeyError, TypeError):
            saved = None
        age = self.now() - saved["saved_at"] if saved else float("inf")
        if saved and 0 <= age <= ttl and not force:
            self.saved_times[url] = saved["saved_at"]
            return saved["data"]
        try:
            check_cancel(cancel)
            # Preserve the simple fetch(url) injection contract for tests/offline sources.
            payload = self.fetch(url, cancel=cancel) if self.fetch is download else self.fetch(url)
            check_cancel(cancel)
            data = json.loads(payload)
            if validate:
                validate(data)
        except Cancelled:
            raise
        except (ApiError, ValueError, KeyError, TypeError, OSError) as exc:
            if saved and 0 <= age <= max_stale:
                self.warnings.append(f"API недоступен; старый кэш ({age / 3600:.1f} ч): {url}")
                self.saved_times[url] = saved["saved_at"]
                return saved["data"]
            raise ApiError(str(exc)) from exc
        self.saved_times[url] = self.now()
        try:
            atomic_json(path, {"saved_at": self.saved_times[url], "data": data})
        except OSError as exc:
            self.warnings.append(f"Не удалось записать кэш: {exc}")
        return data
