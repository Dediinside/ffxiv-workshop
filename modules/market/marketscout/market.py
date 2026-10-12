"""Universalis data: whole-stack purchases, separate listing/history snapshots."""
from dataclasses import dataclass, field
import math
import time
from urllib.parse import quote
from .net import ApiError, Cancelled, check_cancel

BASE = "https://universalis.app/api/v2/"
HISTORY_LIMIT = 1800
HISTORY_SECONDS = 7 * 86400


@dataclass(frozen=True)
class Listing:
    price: int
    quantity: int
    hq: bool
    world: str = ""
    reviewed: int = 0


@dataclass
class Purchase:
    cost: int
    quantity: int
    stacks: list


@dataclass(frozen=True)
class Sale:
    price: int
    quantity: int
    hq: bool
    timestamp: int


@dataclass
class Snapshot:
    listings: list = field(default_factory=list)
    sales: list = field(default_factory=list)
    uploaded: float = 0
    fetched: float = 0
    truncated: bool = False
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def best_purchase(listings, required, tax=5.0):
    """Exact 0/1 covering knapsack: whole stacks, each listing at most once."""
    if not isinstance(required, int) or isinstance(required, bool) or required < 0:
        raise ValueError("Количество должно быть целым неотрицательным числом")
    if not math.isfinite(tax) or not 0 <= tax <= 20:
        raise ValueError("Налог должен быть от 0 до 20%")
    if required == 0:
        return Purchase(0, 0, [])
    if required > 20000:
        raise ValueError("План превышает 20 000 единиц; уменьшите партию")
    stock = [l for l in listings if l.quantity > 0 and l.price > 0]
    if sum(l.quantity for l in stock) < required:
        return None
    dp = [None] * (required + 1)
    dp[0] = (0, 0, ())
    for index, listing in enumerate(stock):
        raw = listing.price * listing.quantity
        cost = raw + math.floor(raw * tax / 100)
        for q in range(required - 1, -1, -1):
            old = dp[q]
            if old is None:
                continue
            dest = min(required, q + listing.quantity)
            candidate = (old[0] + cost, old[1] + listing.quantity, old[2] + (index,))
            if dp[dest] is None or candidate[:2] < dp[dest][:2]:
                dp[dest] = candidate
    result = dp[required]
    return Purchase(result[0], result[1], [stock[i] for i in result[2]]) if result else None


def _positive(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _timestamp(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
        raise ValueError("Некорректный timestamp Universalis")
    return value


def _entries(data):
    if not isinstance(data, dict):
        raise ValueError("Universalis вернул неизвестную схему")
    if "items" in data:
        if not isinstance(data["items"], dict):
            raise ValueError("Некорректный список предметов API")
        return data["items"]
    if _positive(data.get("itemID")):
        return {str(data["itemID"]): data}
    raise ValueError("Universalis вернул неизвестную схему")


def validate_payload(data, history=False):
    key = "entries" if history else "listings"
    for item in _entries(data).values():
        if not isinstance(item, dict) or not isinstance(item.get(key), list):
            raise ValueError(f"Некорректные {key}")
        _timestamp(item.get("lastUploadTime", 0))
        for entry in item[key]:
            if not isinstance(entry, dict) or not isinstance(entry.get("hq"), bool):
                raise ValueError("Некорректное качество API")
            if not all(_positive(entry.get(k)) for k in ("pricePerUnit", "quantity")):
                raise ValueError("Некорректная цена / количество API")
            _timestamp(entry.get("timestamp" if history else "lastReviewTime", 0))


def validate_aggregate(data):
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("Некорректная схема aggregated API")
    for row in data["results"]:
        if not isinstance(row, dict) or not _positive(row.get("itemId")):
            raise ValueError("Некорректный itemId aggregated API")
        for quality in ("nq", "hq"):
            part = row.get(quality)
            if not isinstance(part, dict):
                raise ValueError("Некорректное качество aggregated API")
            velocity = part.get("dailySaleVelocity")
            if not isinstance(velocity, dict):
                raise ValueError("Нет dailySaleVelocity")
            world = velocity.get("world")
            if world is not None:
                value = world.get("quantity") if isinstance(world, dict) else None
                if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
                    raise ValueError("Некорректная скорость продаж")


class Market:
    def __init__(self, cache, throttle=0.1, now=time.time, sleeper=time.sleep):
        self.cache, self.throttle, self.now, self.sleeper = cache, throttle, now, sleeper
        self.warnings = []

    def _get(self, url, force, validator, cancel, ttl=600):
        check_cancel(cancel)
        before = len(self.cache.warnings)
        data = self.cache.get(url, ttl=ttl, force=force, validate=validator, cancel=cancel)
        if self.throttle > 0:
            self.sleeper(self.throttle)
        check_cancel(cancel)
        warnings = self.cache.warnings[before:]
        self.warnings.extend(warnings)
        return data, warnings

    def fetch(self, scope, ids, history=False, force=False, progress=lambda s: None, cancel=None):
        ids = sorted(set(ids))
        result = {i: Snapshot() for i in ids}
        for offset in range(0, len(ids), 25):
            check_cancel(cancel)
            batch = ids[offset:offset + 25]
            progress(f"Рынок: {min(offset + 25, len(ids))}/{len(ids)} предметов…")
            suffix = quote(str(scope), safe="") + "/" + ",".join(map(str, batch))
            # Omit listings entirely: API returns all NQ/HQ listings by default.
            url = BASE + suffix + "?entries=0"
            try:
                data, warnings = self._get(url, force, validate_payload, cancel)
                items = _entries(data)
                for item_id in batch:
                    snap = result[item_id]
                    snap.warnings.extend(warnings)
                    item = items.get(str(item_id))
                    if item is None:
                        snap.errors.append("Предмет отсутствует в ответе Universalis")
                        continue
                    snap.uploaded = item.get("lastUploadTime", 0) / 1000
                    snap.fetched = getattr(self.cache, "saved_times", {}).get(url, self.now())
                    snap.listings = [Listing(l["pricePerUnit"], l["quantity"], l["hq"],
                        str(l.get("worldName") or l.get("worldID") or scope), int(l.get("lastReviewTime", 0)))
                        for l in item["listings"] if not l.get("onMannequin", False)]
                    if not snap.listings:
                        snap.warnings.append("Нет предложений на покупку")
            except Cancelled:
                raise
            except (ApiError, ValueError, TypeError, KeyError) as exc:
                for item_id in batch:
                    result[item_id].errors.append(f"Предложения недоступны: {exc}")
            if not history:
                continue
            history_url = BASE + "history/" + suffix + f"?entriesToReturn={HISTORY_LIMIT}&entriesWithin={HISTORY_SECONDS}&statsWithin={HISTORY_SECONDS * 1000}"
            try:
                data, warnings = self._get(history_url, force, lambda d: validate_payload(d, True), cancel)
                items = _entries(data)
                for item_id in batch:
                    snap = result[item_id]
                    snap.warnings.extend(warnings)
                    item = items.get(str(item_id))
                    if item is None:
                        snap.errors.append("История предмета отсутствует в ответе Universalis")
                        continue
                    history_upload = item.get("lastUploadTime", 0) / 1000
                    # Conservatively report the older source: fresh listings do not
                    # make a stale history-cache entry fresh as well.
                    uploads = [stamp for stamp in (snap.uploaded, history_upload) if stamp > 0]
                    snap.uploaded = min(uploads) if uploads else 0
                    history_fetched = getattr(self.cache, "saved_times", {}).get(history_url, self.now())
                    fetched = [stamp for stamp in (snap.fetched, history_fetched) if stamp > 0]
                    snap.fetched = min(fetched) if fetched else 0
                    raw = item["entries"]
                    snap.truncated = len(raw) >= HISTORY_LIMIT
                    snap.sales = [Sale(s["pricePerUnit"], s["quantity"], s["hq"], int(s["timestamp"]))
                                  for s in raw if not s.get("onMannequin", False)
                                  and self.now() - HISTORY_SECONDS <= s["timestamp"] <= self.now()]
                    if snap.truncated:
                        snap.warnings.append("История достигла лимита; спрос — нижняя граница")
                    if not snap.sales:
                        snap.warnings.append("Нет зарегистрированных продаж за 7 дней; спрос неизвестен")
            except Cancelled:
                raise
            except (ApiError, ValueError, TypeError, KeyError) as exc:
                for item_id in batch:
                    result[item_id].errors.append(f"История недоступна: {exc}")
        return result

    def aggregate(self, scope, ids, force=False, progress=lambda s: None, cancel=None):
        """4-day world units/day, only for screening; missing data omitted."""
        ids = sorted(set(ids))
        result = {}
        for offset in range(0, len(ids), 100):
            batch = ids[offset:offset + 100]
            progress(f"Предварительный спрос: {min(offset + 100, len(ids))}/{len(ids)}…")
            url = BASE + "aggregated/" + quote(str(scope), safe="") + "/" + ",".join(map(str, batch))
            try:
                data, _ = self._get(url, force, validate_aggregate, cancel, ttl=900)
                for row in data["results"]:
                    parts = [row[q]["dailySaleVelocity"].get("world") for q in ("nq", "hq")]
                    if row["itemId"] in batch and any(p is not None for p in parts):
                        result[row["itemId"]] = sum(p["quantity"] for p in parts if p is not None)
            except Cancelled:
                raise
            except (ApiError, ValueError, TypeError, KeyError) as exc:
                self.warnings.append(f"Предварительный спрос недоступен: {exc}")
        return result


def demo_market(ids, now=None):
    """Synthetic prices and sales, visibly marked; no real market claim."""
    now = int(time.time() if now is None else now)
    result = {}
    for item_id in ids:
        base = 25 + (item_id * 37) % 750
        listings = [Listing(base, 10, False, "ДЕМО", now), Listing(base + 10, 99, False, "ДЕМО", now),
                    Listing(base * 2, 5, True, "ДЕМО", now), Listing(base * 2 + 15, 50, True, "ДЕМО", now)]
        sales = [Sale(base + (i % 3) * 5, 1 + (item_id + i) % 8, False, now - 3600 - i * 14400)
                 for i in range(12 + item_id % 12)]
        sales += [Sale(base * 2 + (i % 3) * 5, 1 + i % 3, True, now - 3600 - i * 28800) for i in range(8)]
        result[item_id] = Snapshot(listings, sales, now, now, warnings=["ДЕМО: искусственные цены и продажи"])
    return result
