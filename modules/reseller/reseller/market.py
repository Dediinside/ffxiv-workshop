"""Two-stage screening with separate destination sales and per-world listings."""
from dataclasses import dataclass, field
import math
import time
from urllib.parse import quote
import hashlib
import json
from .net import ApiError, Cancelled, check

BASE = 'https://universalis.app/api/v2/'
HISTORY_LIMIT = 1800
WEEK = 7 * 86400


@dataclass(frozen=True)
class Listing:
    item: int
    world: int
    price: int
    quantity: int
    hq: bool
    reviewed: int
    identity: str = ''
    offer_key: str = ''
    base_key: str = ''


def offer_keys(item, world, raw):
    identity = str(raw.get('listingID') or '')
    if identity and not identity.startswith('dirty:'):
        base = [item, world, identity]
    else:
        # Synthetic IDs can change solely when review time changes. Ignore their ID/time.
        retainer = str(raw.get('retainerID') or raw.get('retainerName') or raw.get('sellerID') or '')
        base = [item, world, retainer, raw.get('hq'), raw.get('materia') or [], raw.get('stainID', 0)]
    base_key = hashlib.sha256(json.dumps(base, sort_keys=True).encode()).hexdigest()
    revision = [base_key, raw.get('hq'), raw.get('pricePerUnit'), raw.get('quantity')]
    return hashlib.sha256(json.dumps(revision).encode()).hexdigest(), base_key


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
    uploads: dict = field(default_factory=dict)
    listing_fetched: float = 0
    history_fetched: float = 0
    truncated: bool = False
    errors: list = field(default_factory=list)


def positive(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def timestamp(value, millis=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('Некорректная дата Universalis')
    return value / 1000 if millis else value


def entries(data):
    if not isinstance(data, dict):
        raise ValueError('Некорректный ответ Universalis')
    if isinstance(data.get('items'), dict):
        return data['items']
    if positive(data.get('itemID')):
        return {str(data['itemID']): data}
    raise ValueError('Нет предметов в ответе Universalis')


def overview_score(part, s):
    try:
        price = part['minListing']['dc']['price']
        median_proxy = part['averageSalePrice']['world']['price']
        velocity = part['dailySaleVelocity']['world']['quantity']
        values = (price, median_proxy, velocity)
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in values):
            return None
        # Averages only screen candidates; detailed history/whole stacks determine deals.
        if velocity < s['min_units_day'] or price >= median_proxy * (1 - s['min_discount'] / 100):
            return None
        return (median_proxy - price) * min(velocity, 1000)
    except (KeyError, TypeError):
        return None


class Market:
    def __init__(self, cache, now=time.time):
        self.cache, self.now = cache, now
        self.warnings = []
        self.history = {}

    def overview(self, world, ids, settings, force=False, progress=lambda x: None, cancel=None):
        scores = {}
        for offset in range(0, len(ids), 100):
            check(cancel)
            batch = ids[offset:offset + 100]
            progress(f'Предварительный отбор: {min(offset + 100, len(ids))}/{len(ids)} товаров…')
            url = BASE + f'aggregated/{world}/' + ','.join(map(str, batch))
            try:
                value, _ = self.cache.get(url, 300, force, cancel)
                if not isinstance(value, dict) or not isinstance(value.get('results'), list):
                    raise ValueError('Некорректный aggregated API')
                if value.get('failedItems'):
                    self.warnings.append(f'Universalis не проверил {len(value["failedItems"])} товаров пакета {offset + 1}')
                for row in value['results']:
                    item = row.get('itemId')
                    if item not in batch:
                        continue
                    qs = ('nq', 'hq') if settings['quality'] == 'Все' else (settings['quality'].lower(),)
                    found = [overview_score(row.get(q, {}), settings) for q in qs]
                    found = [v for v in found if v is not None]
                    if found:
                        scores[item] = max(found)
            except Cancelled:
                raise
            except (ApiError, ValueError, TypeError, KeyError, AttributeError) as exc:
                self.warnings.append(f'Отбор пакета {offset + 1}: {exc}')
        return scores

    def detailed(self, dc, world, ids, force=False, progress=lambda x: None, cancel=None, reuse_history=False):
        self.history = {k: v for k, v in self.history.items() if 0 <= self.now() - v[0] <= 120}
        output = {item: Snapshot() for item in ids}
        for offset in range(0, len(ids), 10):
            batch = ids[offset:offset + 10]
            check(cancel)
            progress(f'Проверка стаков и продаж: {min(offset + 10, len(ids))}/{len(ids)}…')
            path = ','.join(map(str, batch))
            urls = [(False, BASE + quote(dc, safe='') + '/' + path + '?entries=0'),
                    (True, BASE + f'history/{world}/' + path + f'?entriesToReturn={HISTORY_LIMIT}&entriesWithin={WEEK}&statsWithin={WEEK * 1000}')]
            for history, url in urls:
                try:
                    wanted = batch
                    if history and reuse_history:
                        wanted = []
                        for item in batch:
                            cached = self.history.get((world, item))
                            if cached and 0 <= self.now() - cached[0] <= 120:
                                output[item].history_fetched, output[item].sales, output[item].truncated = cached
                            else:
                                wanted.append(item)
                        if not wanted:
                            continue
                        url = BASE + f'history/{world}/' + ','.join(map(str, wanted)) + f'?entriesToReturn={HISTORY_LIMIT}&entriesWithin={WEEK}&statsWithin={WEEK * 1000}'
                    value, fetched = self.cache.get(url, 120, force, cancel)
                    payload = entries(value)
                    for item in wanted:
                        snap = output[item]
                        try:
                            row = payload[str(item)]
                            if history:
                                raw = row['entries']
                                if not isinstance(raw, list):
                                    raise ValueError('Некорректная история')
                                snap.truncated = len(raw) >= HISTORY_LIMIT
                                snap.history_fetched = fetched
                                for sale in raw:
                                    if not isinstance(sale, dict) or not isinstance(sale.get('hq'), bool) or not positive(sale.get('pricePerUnit')) or not positive(sale.get('quantity')):
                                        raise ValueError('Некорректная продажа')
                                    stamp = timestamp(sale['timestamp'])
                                    if not sale.get('onMannequin', False) and self.now() - WEEK <= stamp <= self.now():
                                        snap.sales.append(Sale(sale['pricePerUnit'], sale['quantity'], sale['hq'], int(stamp)))
                                self.history[world, item] = (snap.history_fetched, snap.sales, snap.truncated)
                                if len(self.history) > 256:
                                    self.history.pop(next(iter(self.history)))
                            else:
                                raw = row['listings']
                                if not isinstance(raw, list):
                                    raise ValueError('Некорректные объявления')
                                snap.listing_fetched = fetched
                                uploads = row.get('worldUploadTimes', {})
                                if not isinstance(uploads, dict):
                                    raise ValueError('Нет времён обновления миров')
                                snap.uploads = {int(w): timestamp(v, True) for w, v in uploads.items()}
                                for listing in raw:
                                    if not isinstance(listing, dict) or not isinstance(listing.get('hq'), bool) or not all(positive(listing.get(k)) for k in ('pricePerUnit', 'quantity', 'worldID')):
                                        raise ValueError('Некорректное объявление')
                                    if not listing.get('onMannequin', False):
                                        identity = str(listing.get('listingID') or listing.get('retainerID') or listing.get('retainerName') or '')
                                        key, base_key = offer_keys(item, listing['worldID'], listing)
                                        snap.listings.append(Listing(item, listing['worldID'], listing['pricePerUnit'], listing['quantity'], listing['hq'], int(timestamp(listing.get('lastReviewTime', 0))), identity, key, base_key))
                        except (ValueError, KeyError, TypeError) as exc:
                            snap.errors.append(f'{"История" if history else "Предложения"}: {exc}')
                except Cancelled:
                    raise
                except (ApiError, ValueError, KeyError, TypeError) as exc:
                    for item in batch:
                        output[item].errors.append(f'API: {exc}')
        return output


def demo_market(ids, world, dc_worlds, now=None):
    now = int(time.time() if now is None else now)
    other = next(w for w in dc_worlds if w != world)
    output = {}
    for item in ids:
        base = 1000 + item % 500
        listings = [Listing(item, other, base // 3, 10, False, now, 'demo-' + str(item)),
                    Listing(item, world, base, 3, False, now)]
        sales = [Sale(base + i % 3 * 10, 10, False, now - 3600 - i * 12000) for i in range(25)]
        output[item] = Snapshot(listings, sales, {w: now for w in dc_worlds}, now, now)
    return output
