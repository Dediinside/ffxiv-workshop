"""Bounded, cancellable HTTP; market cache never falls back to stale prices."""
import hashlib
import json
import math
from pathlib import Path
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler
from .storage import atomic_json


class ApiError(RuntimeError):
    pass


class Cancelled(ApiError):
    pass


def check(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled('Остановлено')


def wait(seconds, cancel=None):
    if cancel is not None:
        if cancel.wait(seconds):
            raise Cancelled('Остановлено')
    else:
        time.sleep(seconds)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Http:
    def __init__(self, opener=None, rate=10, clock=time.monotonic, sleeper=wait):
        self.opener = opener or build_opener(NoRedirect())
        self.clock, self.sleeper, self.period = clock, sleeper, 1 / rate
        self.next_request, self.requests = 0, 0
        self.lock = threading.Lock()

    def get(self, url, cancel=None):
        for attempt in range(3):
            check(cancel)
            with self.lock:
                delay = max(0, self.next_request - self.clock())
                if delay:
                    self.sleeper(delay, cancel)
                self.next_request = self.clock() + self.period
                self.requests += 1
            request = Request(url, headers={'User-Agent': 'FFXIVResellerParadise/0.1', 'Accept': 'application/json'})
            try:
                with self.opener.open(request, timeout=20) as response:
                    body = response.read(32 * 1024 * 1024 + 1)
                check(cancel)
                if len(body) > 32 * 1024 * 1024:
                    raise ApiError('Ответ Universalis слишком большой; уменьшите пакет')
                return json.loads(body)
            except HTTPError as exc:
                code = exc.code
                retry_after = exc.headers.get('Retry-After', '') if exc.headers else ''
                exc.close()
                if code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise ApiError(f'Universalis HTTP {code}') from None
                try:
                    seconds = float(retry_after)
                    seconds = seconds if math.isfinite(seconds) and 0 <= seconds <= 60 else 60
                except ValueError:
                    seconds = 2 ** (attempt + 1)
                self.sleeper(seconds, cancel)
            except (URLError, TimeoutError, OSError) as exc:
                if attempt == 2:
                    raise ApiError('Universalis недоступен: сеть или тайм-аут') from None
                self.sleeper(2 ** attempt, cancel)
            except (ValueError, UnicodeError):
                raise ApiError('Universalis вернул некорректный JSON') from None


class Cache:
    def __init__(self, folder, http=None, now=time.time):
        self.folder, self.http, self.now = Path(folder), http or Http(), now

    def get(self, url, ttl=120, force=False, cancel=None):
        path = self.folder / (hashlib.sha256(url.encode()).hexdigest() + '.json')
        check(cancel)
        try:
            saved = json.loads(path.read_text(encoding='utf-8'))
            stamp = saved['time']
            if isinstance(stamp, bool) or not isinstance(stamp, (float, int)):
                raise ValueError()
            age = self.now() - stamp
            if not force and 0 <= age <= ttl:
                return saved['data'], stamp
        except (OSError, ValueError, KeyError, TypeError):
            pass
        value = self.http.get(url, cancel)
        check(cancel)
        stamp = self.now()
        try:
            atomic_json(path, {'time': stamp, 'data': value})
        except OSError:
            pass
        return value, stamp
