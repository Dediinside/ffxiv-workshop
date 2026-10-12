import json
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from marketscout.market import Listing, Market, best_purchase, demo_market, HISTORY_LIMIT
from marketscout.net import ApiError, Cancelled, JsonCache, download


NOW = 1_800_000_000


def listing(price=100, quantity=3, hq=False):
    return {"pricePerUnit": price, "quantity": quantity, "hq": hq, "lastReviewTime": NOW - 60}


def current(item_id=42, rows=None):
    return {"itemID": item_id, "lastUploadTime": NOW * 1000, "listings": rows if rows is not None else [listing()]}


def history(item_id=42, rows=None):
    return {"itemID": item_id, "lastUploadTime": NOW * 1000,
            "entries": rows if rows is not None else [{"pricePerUnit": 120, "quantity": 9, "hq": False, "timestamp": NOW - 100}]}


class MarketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def market(self, fetch):
        return Market(JsonCache(self.temp.name, fetch=lambda url: json.dumps(fetch(url)).encode(), now=lambda: NOW),
                      throttle=0, now=lambda: NOW)

    def test_current_all_listings_and_hq(self):
        urls = []
        def fetch(url):
            urls.append(url)
            return current(rows=[listing(hq=False), listing(200, 7, True)])
        snap = self.market(fetch).fetch("Light", [42])[42]
        self.assertNotIn("listings=", urls[0])
        self.assertEqual([l.hq for l in snap.listings], [False, True])
        self.assertEqual(snap.uploaded, NOW)
        self.assertEqual(snap.listings[0].reviewed, NOW - 60)
        self.assertEqual(snap.fetched, NOW)

    def test_history_window_and_units_not_transactions(self):
        urls = []
        def fetch(url):
            urls.append(url)
            return history() if "/history/" in url else current()
        snap = self.market(fetch).fetch(33, [42], history=True)[42]
        self.assertEqual(len(snap.sales), 1)
        self.assertEqual(snap.sales[0].quantity, 9)
        self.assertEqual(snap.sales[0].timestamp, NOW - 100)
        self.assertIn("entriesWithin=604800", urls[1])
        self.assertIn("statsWithin=604800000", urls[1])
        self.assertIn("entriesToReturn=1800", urls[1])

    def test_complete_listings_over_one_hundred(self):
        snap = self.market(lambda url: current(rows=[listing()] * 150)).fetch(33, [42])[42]
        self.assertEqual(len(snap.listings), 150)

    def test_history_source_age_conservative(self):
        older = dict(history(), lastUploadTime=(NOW - 90000) * 1000)
        snap = self.market(lambda url: older if "/history/" in url else current()).fetch(33, [42], True)[42]
        self.assertEqual(snap.uploaded, NOW - 90000)

    def test_history_truncation_and_filter_out_of_window(self):
        rows = [{"pricePerUnit": 20, "quantity": 1, "hq": False, "timestamp": NOW - 100}] * HISTORY_LIMIT
        rows[-1] = dict(rows[-1], timestamp=NOW - 604801)
        snap = self.market(lambda url: history(rows=rows) if "/history/" in url else current()).fetch(33, [42], True)[42]
        self.assertTrue(snap.truncated)
        self.assertEqual(len(snap.sales), HISTORY_LIMIT - 1)

    def test_multi_and_missing_item(self):
        snap = self.market(lambda url: {"items": {"42": current()}, "unresolvedItems": [43]}).fetch(33, [42, 43])
        self.assertEqual(len(snap[42].listings), 1)
        self.assertTrue(snap[43].errors)
        self.assertEqual(snap[43].listings, [])

    def test_missing_prices_and_sales_are_unknown(self):
        snap = self.market(lambda url: history(rows=[]) if "/history/" in url else current(rows=[])).fetch(33, [42], True)[42]
        self.assertEqual(snap.listings, [])
        self.assertEqual(snap.sales, [])
        self.assertTrue(any("неизвестен" in w for w in snap.warnings))

    def test_malformed_price_and_quality_rejected(self):
        for bad in [listing(price=0), listing(quantity=-2), listing(hq=1), listing(price=True)]:
            snap = self.market(lambda url: current(rows=[bad])).fetch(33, [42], force=True)[42]
            self.assertTrue(snap.errors)
            self.assertEqual(snap.listings, [])

    def test_history_error_preserves_listing(self):
        def fetch(url):
            if "/history/" in url:
                raise ApiError("offline")
            return current()
        snap = self.market(fetch).fetch(33, [42], True)[42]
        self.assertEqual(len(snap.listings), 1)
        self.assertEqual(snap.sales, [])
        self.assertTrue(any("История" in e for e in snap.errors))

    def test_aggregate_world_quality_sum(self):
        def quality(n):
            return {"dailySaleVelocity": {"world": {"quantity": n}, "dc": {"quantity": 999}}}
        response = {"results": [{"itemId": 42, "nq": quality(12), "hq": quality(3)},
                               {"itemId": 43, "nq": quality(0), "hq": quality(0)}], "failedItems": [44]}
        result = self.market(lambda url: response).aggregate(33, [42, 43, 44])
        self.assertEqual(result, {42: 15, 43: 0})

    def test_cancelled(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(Cancelled):
            self.market(lambda url: current()).fetch(33, [42], cancel=event)

    def test_demo_deterministic_and_marked(self):
        first = demo_market([42], NOW)[42]
        self.assertEqual(first, demo_market([42], NOW)[42])
        self.assertTrue(first.sales)
        self.assertTrue(any("ДЕМО" in w for w in first.warnings))


class PurchaseTests(unittest.TestCase):
    def test_whole_stacks_optimal_not_cheapest_unit(self):
        result = best_purchase([Listing(1, 99, False), Listing(10, 3, False)], 3, tax=0)
        self.assertEqual((result.cost, result.quantity), (30, 3))

    def test_each_stack_once_and_tax_rounding(self):
        result = best_purchase([Listing(7, 3, False), Listing(9, 2, False)], 5, tax=5)
        self.assertEqual((result.cost, result.quantity), (40, 5))
        self.assertEqual(len(result.stacks), 2)
        self.assertIsNone(best_purchase([Listing(7, 3, False)], 4))

    def test_overshoot_and_limits(self):
        result = best_purchase([Listing(7, 10, False)], 2, tax=0)
        self.assertEqual((result.cost, result.quantity), (70, 10))
        self.assertEqual(best_purchase([], 0).cost, 0)
        with self.assertRaises(ValueError):
            best_purchase([], 20001)
        with self.assertRaises(ValueError):
            best_purchase([], 3, tax=float("nan"))


class CacheTests(unittest.TestCase):
    def test_fresh_force_stale_fallback_and_expiry(self):
        with tempfile.TemporaryDirectory() as folder:
            clock, calls, failing = [1000], [], [False]
            def fetch(url):
                calls.append(url)
                if failing[0]:
                    raise ApiError("offline")
                return b'{"value": 1}'
            cache = JsonCache(folder, fetch, now=lambda: clock[0])
            self.assertEqual(cache.get("https://example.test", ttl=10), {"value": 1})
            cache.get("https://example.test", ttl=10)
            self.assertEqual(len(calls), 1)
            cache.get("https://example.test", force=True)
            self.assertEqual(len(calls), 2)
            clock[0] = 1020
            failing[0] = True
            self.assertEqual(cache.get("https://example.test", ttl=10), {"value": 1})
            self.assertIn("старый кэш", cache.warnings[-1])
            self.assertEqual(cache.saved_times["https://example.test"], 1000)
            with self.assertRaises(ApiError):
                cache.get("https://example.test", ttl=10, max_stale=15)

    def test_malformed_response_not_saved(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = JsonCache(folder, fetch=lambda url: b'{broken')
            with self.assertRaises(ApiError):
                cache.get("bad")
            self.assertEqual(list(cache.folder.glob("*.json")), [])

    def test_cancellation_does_not_use_stale_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = JsonCache(folder, fetch=lambda url: b'{}')
            cache.get("one")
            cache.fetch = lambda url: (_ for _ in ()).throw(Cancelled("stop"))
            with self.assertRaises(Cancelled):
                cache.get("one", force=True)

    def test_retry_after_cap_and_user_agent(self):
        calls, waits = [], []
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self): return b'{}'
        def opener(request, timeout):
            calls.append(request)
            if len(calls) == 1:
                raise HTTPError(request.full_url, 429, "rate limit", {"Retry-After": "999"}, None)
            return Response()
        self.assertEqual(download("https://example.test", opener=opener, sleeper=waits.append), b'{}')
        self.assertEqual(sum(waits), 15)
        self.assertIn("FFXIVMarketScout", calls[0].get_header("User-agent"))

    def test_non_retryable_http(self):
        calls = []
        def opener(request, timeout):
            calls.append(request)
            raise HTTPError(request.full_url, 404, "missing", {}, None)
        with self.assertRaises(ApiError):
            download("https://example.test", opener=opener)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
