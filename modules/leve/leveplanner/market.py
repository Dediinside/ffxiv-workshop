"""Universalis V2 listings; no historical average substituted for missing stock."""
from dataclasses import dataclass
import math
from urllib.parse import quote
from .net import ApiError

BASE = "https://universalis.app/api/v2/"


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


def best_purchase(listings, required, tax=5.0):
    """Exact 0/1 covering knapsack: buy whole stacks; each listing at most once."""
    if required <= 0:
        return Purchase(0, 0, [])
    if required > 20000:
        raise ValueError("План превышает 20 000 единиц предмета; уменьшите диапазон")
    if not 0 <= tax <= 20:
        raise ValueError("Налог должен быть от 0 до 20%")
    stock = [l for l in listings if l.quantity > 0 and l.price > 0]
    if sum(l.quantity for l in stock) < required:
        return None
    dp = [None] * (required + 1)
    dp[0] = (0, 0, ())
    for index, listing in enumerate(stock):
        # In-game purchase tax is rounded down per purchased listing.
        raw_cost = listing.price * listing.quantity
        cost = raw_cost + math.floor(raw_cost * tax / 100)
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


def validate_payload(data):
    if not isinstance(data, dict) or not ("items" in data or "itemID" in data):
        raise ValueError("Universalis вернул неизвестную схему")
    entries = data.get("items", {str(data.get("itemID")): data})
    if not isinstance(entries, dict):
        raise ValueError("Некорректный список предметов API")
    for entry in entries.values():
        if not isinstance(entry, dict) or not isinstance(entry.get("listings", []), list):
            raise ValueError("Некорректные listings")
        for listing in entry.get("listings", []):
            if not isinstance(listing, dict) or not isinstance(listing.get("hq"), bool):
                raise ValueError("Некорректное качество listing")
            if not all(isinstance(listing.get(k), int) and listing[k] > 0 for k in ("pricePerUnit", "quantity")):
                raise ValueError("Некорректная цена / количество API")


class Market:
    def __init__(self, cache):
        self.cache = cache
        self.warnings = []
        self.uploads = {}

    def fetch(self, scope, item_ids, force=False, progress=lambda s: None):
        result = {i: [] for i in item_ids}
        ids = sorted(set(item_ids))
        for offset in range(0, len(ids), 50):
            batch = ids[offset:offset + 50]
            progress(f"Рынок: {min(offset + 50, len(ids))}/{len(ids)} предметов…")
            # Fetch separately to prevent cheap NQ listings crowding HQ out of the response.
            for hq in (False, True):
                url = BASE + quote(str(scope), safe="") + "/" + ",".join(map(str, batch))
                # Omit the listing limit: zero asks Universalis to return no listings.
                url += f"?entries=0&hq={str(hq).lower()}"
                try:
                    data = self.cache.get(url, ttl=120, max_stale=0, force=force, validate=validate_payload)
                    entries = data.get("items", {str(data.get("itemID")): data})
                    for key, item in entries.items():
                        item_id = int(key)
                        if item_id not in result:
                            continue
                        self.uploads[item_id] = item.get("lastUploadTime", 0)
                        for l in item.get("listings", []):
                            if l["hq"] == hq and not l.get("onMannequin", False):
                                result[item_id].append(Listing(l["pricePerUnit"], l["quantity"], hq,
                                    l.get("worldName", str(l.get("worldID", scope))), l.get("lastReviewTime", 0)))
                except (ApiError, ValueError, TypeError) as exc:
                    self.warnings.append(f"Цены {'HQ' if hq else 'NQ'} недоступны ({len(batch)} предметов): {exc}")
        self.warnings.extend(self.cache.warnings)
        return result
