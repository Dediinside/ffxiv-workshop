import itertools
import json
from pathlib import Path
import random
import tempfile
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from leveplanner.catalog import JOBS, available, load, normalize, validate
from leveplanner.market import Listing, Market, best_purchase, validate_payload
from leveplanner.net import ApiError, JsonCache, download
from leveplanner.planner import Settings, market_ids, next_target, price_craft, recommend, reward_xp, simulate

ROOT = Path(__file__).resolve().parents[1]


def leve(**overrides):
    result = dict(id=1, name="Example", job="Carpenter", level=1, exp=70,
                  gil=40, allowances=1, max_turnins=3, items={10: 3}, location="Test", description="")
    result.update(overrides)
    return result


def fixture():
    return dict(level_cap=100, xp={i: 100 for i in range(1, 101)}, leves=[leve()],
                items={i: dict(name=str(i), hq=True, marketable=True) for i in (10, 20, 21)},
                recipes=[dict(id=2, item=10, job="Carpenter", level=1, yield_=3,
                              **{"yield": 3}, ingredients={20: 2, 21: 1}, hq=True, book=0, specialist=False)])


class PurchaseTests(unittest.TestCase):
    def test_buy_whole_stack(self):
        p = best_purchase([Listing(2, 99, False)], 1, 0)
        self.assertEqual((p.cost, p.quantity), (198, 99))

    def test_full_stack_cost_can_beat_lower_unit_price(self):
        p = best_purchase([Listing(1, 99, False), Listing(10, 2, False)], 2, 0)
        self.assertEqual(p.cost, 20)

    def test_exact_not_greedy(self):
        p = best_purchase([Listing(1, 6, False), Listing(2, 10, False)], 10, 0)
        self.assertEqual(p.cost, 20)

    def test_insufficient_inventory(self):
        self.assertIsNone(best_purchase([Listing(1, 3, False)], 4))

    def test_listing_not_reused(self):
        self.assertIsNone(best_purchase([Listing(1, 3, False)], 6))

    def test_tax_rounded_per_stack(self):
        self.assertEqual(best_purchase([Listing(19, 1, False)], 1, 5).cost, 19)
        self.assertEqual(best_purchase([Listing(20, 1, False)], 1, 5).cost, 21)

    def test_zero_required(self):
        self.assertEqual(best_purchase([], 0).cost, 0)

    def test_missing_not_zero(self):
        self.assertIsNone(best_purchase([], 1))
        self.assertIsNone(best_purchase([Listing(0, 10, False)], 1))

    def test_invalid_tax_and_size(self):
        for tax in (-1, 21, float("nan")):
            with self.assertRaises(ValueError):
                best_purchase([], 1, tax)
        with self.assertRaises(ValueError):
            best_purchase([], 20001)

    def test_dynamic_program_matches_exhaustive_subsets(self):
        rng = random.Random(123)
        for _ in range(80):
            stock = [Listing(rng.randint(1, 15), rng.randint(1, 10), False) for _ in range(7)]
            qty = rng.randint(1, 45)
            expected = []
            for mask in itertools.product((0, 1), repeat=len(stock)):
                chosen = [l for yes, l in zip(mask, stock) if yes]
                count = sum(l.quantity for l in chosen)
                if count >= qty:
                    expected.append((sum(l.price * l.quantity for l in chosen), count))
            p = best_purchase(stock, qty, 0)
            self.assertEqual((p.cost, p.quantity) if p else None, min(expected) if expected else None)


class ProgressTests(unittest.TestCase):
    def test_targets(self):
        self.assertEqual([next_target(i, 100) for i in (1, 4, 5, 49, 52, 95, 99)], [5, 5, 10, 50, 55, 100, 100])

    def test_cap_rejected(self):
        for level in (0, 100, 101):
            with self.assertRaises(ValueError):
                next_target(level, 100)

    def test_partial_last_repeat(self):
        p = simulate(leve(), Settings("Carpenter", 1), fixture()["xp"], 100, False, 3)
        self.assertEqual((p.accepts, p.turnins, p.end_level, p.end_xp), (2, 6, 5, 20))
        p = simulate(leve(exp=80), Settings("Carpenter", 1), fixture()["xp"], 100, False, 3)
        self.assertEqual((p.accepts, p.turnins), (2, 5))

    def test_current_exp(self):
        p = simulate(leve(exp=400), Settings("Carpenter", 1, 90), fixture()["xp"], 100, False, 1)
        self.assertEqual((p.turnins, p.end_level, p.end_xp), (1, 5, 90))

    def test_hq_doubles_exp(self):
        s = Settings("Carpenter", 1)
        nq = simulate(leve(exp=50), s, fixture()["xp"], 100, False, 1)
        hq = simulate(leve(exp=50), s, fixture()["xp"], 100, True, 1)
        self.assertEqual((nq.turnins, hq.turnins), (8, 4))

    def test_expansion_caps_after_hq(self):
        for l, player in ((45, 50), (58, 60), (68, 70), (78, 80), (88, 90)):
            self.assertEqual(reward_xp(leve(level=l, exp=90000), player, True), 1000)
            self.assertEqual(reward_xp(leve(level=l, exp=90000), player - 1, True), 180000)
        self.assertEqual(reward_xp(leve(level=90, exp=90000), 90, True), 180000)

    def test_invalid_xp(self):
        for xp in (-1, 100):
            with self.assertRaises(ValueError):
                simulate(leve(), Settings("Carpenter", 1, xp), fixture()["xp"], 100, False, 1)

    def test_locked_and_invalid_repeat(self):
        with self.assertRaises(ValueError):
            simulate(leve(level=5), Settings("Carpenter", 1), fixture()["xp"], 100, False, 1)
        with self.assertRaises(ValueError):
            simulate(leve(), Settings("Carpenter", 1), fixture()["xp"], 100, False, 4)


class RecommendationTests(unittest.TestCase):
    def test_recipe_yield_rounding_and_crystals(self):
        data = fixture()
        prices = {20: [Listing(1, 4, False)], 21: [Listing(2, 2, False)]}
        cost, shopping, crafts, errors = price_craft(data, {10: 4}, Settings("Carpenter", 1, tax=0), prices, False)
        self.assertEqual((cost, crafts[0]["count"], crafts[0]["produced"]), (8, 2, 6))
        self.assertEqual({s["item"]: s["required"] for s in shopping}, {20: 4, 21: 2})
        self.assertFalse(errors)

    def test_cheaper_alternative_recipe(self):
        data = fixture()
        data["recipes"].append({**data["recipes"][0], "id": 3, "ingredients": {21: 1}})
        result = price_craft(data, {10: 3}, Settings("Carpenter", 1, tax=0), {21: [Listing(1, 1, False)]}, False)
        self.assertEqual(result[0], 1)
        self.assertEqual(result[2][0]["recipe"]["id"], 3)

    def test_missing_ingredient_invalidates_craft(self):
        result = price_craft(fixture(), {10: 3}, Settings("Carpenter", 1), {}, False)
        self.assertIsNone(result[0])
        self.assertTrue(result[3])

    def test_hq_filters_and_reward(self):
        d = fixture()
        p = recommend(d, [leve(exp=500)], {10: [Listing(3, 3, False), Listing(5, 3, True)]}, Settings("Carpenter", 1, tax=0))
        purchase = {x.quality: x for x in p if x.method == "Покупка" and x.per_accept == 1}
        self.assertEqual((purchase["HQ"].cost, purchase["HQ"].reward), (15, 80))
        self.assertEqual((purchase["NQ"].cost, purchase["NQ"].reward), (9, 40))
        self.assertFalse(any(x.method == "Крафт" and x.quality == "HQ" for x in p))

    def test_allowance_cost_large_and_partial(self):
        d = fixture()
        p = recommend(d, [leve(exp=80, allowances=10, max_turnins=6)], {10: [Listing(1, 99, False)]}, Settings("Carpenter", 1, tax=0))
        plan = next(x for x in p if x.method == "Покупка" and x.quality == "NQ" and x.per_accept == 6)
        self.assertEqual((plan.progress.accepts, plan.progress.turnins, plan.allowances, plan.reward), (1, 5, 10, 200))

    def test_budget_upfront_no_reward_reinvestment(self):
        p = recommend(fixture(), [leve(exp=500, gil=500)], {10: [Listing(5, 10, False)]}, Settings("Carpenter", 1, budget=49, tax=0))
        plan = next(x for x in p if x.method == "Покупка" and x.quality == "NQ")
        self.assertEqual(plan.cost, 50)
        self.assertFalse(plan.feasible)
        self.assertTrue(any("Бюджет" in x for x in plan.problems))

    def test_missing_prices_rank_below_feasible(self):
        p = recommend(fixture(), [leve(exp=500)], {10: [Listing(1, 10, False)]}, Settings("Carpenter", 1))
        self.assertTrue(p[0].feasible)
        self.assertTrue(any(x.cost is None for x in p))

    def test_allowance_shortage_and_travel(self):
        p = recommend(fixture(), [leve(exp=500)], {10: [Listing(1, 10, False)]}, Settings("Carpenter", 1, allowances=0, tax=0, travel=7))
        plan = next(x for x in p if x.method == "Покупка" and x.quality == "NQ")
        self.assertEqual(plan.cost, 17)
        self.assertFalse(plan.feasible)
        self.assertTrue(any("Allowances" in x for x in plan.problems))

    def test_hq_craft_requires_hq_capable_recipe(self):
        d = fixture()
        d["recipes"][0]["hq"] = False
        self.assertIsNone(price_craft(d, {10: 3}, Settings("Carpenter", 1), {}, True)[0])

    def test_restricted_recipes_excluded(self):
        for change in ({"book": 1}, {"specialist": True}, {"job": "Weaver"}, {"level": 7}):
            d = fixture()
            d["recipes"][0].update(change)
            self.assertIsNone(price_craft(d, {10: 3}, Settings("Carpenter", 1), {}, False)[0])


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = [1000]
        self.calls = []
        def fetch(url):
            self.calls.append(url)
            return b'{"x": 2}'
        self.cache = JsonCache(self.temp.name, fetch, lambda: self.clock[0])

    def test_ttl_and_force(self):
        self.assertEqual(self.cache.get("url"), {"x": 2})
        self.cache.get("url")
        self.assertEqual(len(self.calls), 1)
        self.cache.get("url", force=True)
        self.assertEqual(len(self.calls), 2)

    def test_stale_fallback_expiry(self):
        self.cache.get("url")
        self.clock[0] += 1000
        self.cache.fetch = lambda url: (_ for _ in ()).throw(ApiError("offline"))
        self.assertEqual(self.cache.get("url", ttl=10, max_stale=2000), {"x": 2})
        self.assertTrue(self.cache.warnings)
        self.clock[0] += 2000
        with self.assertRaises(ApiError):
            self.cache.get("url", ttl=10, max_stale=2000)

    def test_corrupt_cache_recovers(self):
        self.cache.get("url")
        next(Path(self.temp.name).glob("*.json")).write_text("broken")
        self.assertEqual(self.cache.get("url"), {"x": 2})
        self.assertEqual(len(self.calls), 2)

    def test_invalid_json_not_cached(self):
        self.cache.fetch = lambda url: b"<html>bad gateway</html>"
        with self.assertRaises(ApiError):
            self.cache.get("url")
        self.assertEqual(list(Path(self.temp.name).glob("*.json")), [])

    def test_invalid_schema_not_cached(self):
        self.cache.fetch = lambda url: b'{"error":"fail"}'
        with self.assertRaises(ApiError):
            self.cache.get("url", validate=validate_payload)
        self.assertEqual(list(Path(self.temp.name).glob("*.json")), [])

    def test_retry_429_bounded(self):
        calls, waits = [], []
        def opener(req, timeout):
            calls.append(req)
            raise HTTPError(req.full_url, 429, "slow down", {}, None)
        with self.assertRaises(ApiError):
            download("https://example.com", opener=opener, sleeper=waits.append)
        self.assertEqual((len(calls), waits), (3, [1, 2]))
        self.assertIn("FFXIVLevePlanner", calls[0].get_header("User-agent"))

    def test_no_retry_404(self):
        def opener(req, timeout):
            raise HTTPError(req.full_url, 404, "not found", {}, None)
        waits = []
        with self.assertRaises(ApiError):
            download("https://example.com", opener=opener, sleeper=waits.append)
        self.assertFalse(waits)


class ApiTests(unittest.TestCase):
    def test_global_scope_filter(self):
        from leveplanner.app import scope_map
        result = scope_map([{"id": 1, "name": "Global"}, {"id": 2, "name": "China"}],
                           [{"name": "Light", "region": "Europe", "worlds": [1]},
                            {"name": "CN", "region": "China", "worlds": [2]}])
        self.assertEqual(set(result.values()), {"Light", "1"})

    def test_bulk_and_single_schema(self):
        class Fake:
            warnings = []
            urls = []
            def get(self, url, **kwargs):
                self.urls.append(url)
                hq = url.endswith("true")
                item = {"itemID": 10, "lastUploadTime": 123000,
                        "listings": [dict(pricePerUnit=4, quantity=5, hq=hq, worldName="Test", lastReviewTime=123)]}
                result = {"items": {"10": item}, "unresolvedItemIds": [20]} if "," in url else item
                kwargs["validate"](result)
                return result
        f = Fake()
        m = Market(f)
        prices = m.fetch("Test DC", {10, 20})
        self.assertEqual(len(prices[10]), 2)
        self.assertEqual(prices[20], [])
        self.assertIn("Test%20DC", f.urls[0])
        self.assertEqual(len(m.fetch("74", {10})[10]), 2)

    def test_batches_at_most_50(self):
        class Fake:
            warnings = []
            counts = []
            def get(self, url, **kwargs):
                self.counts.append(len(url.split("/")[-1].split("?")[0].split(",")))
                return {"items": {}}
        f = Fake()
        self.assertEqual(len(Market(f).fetch("Light", set(range(1, 122)))), 121)
        self.assertEqual(f.counts, [50, 50, 50, 50, 21, 21])

    def test_api_failure_preserves_unknown(self):
        class Fake:
            warnings = []
            def get(self, url, **kwargs):
                raise ApiError("offline")
        m = Market(Fake())
        self.assertEqual(m.fetch("Light", {10}), {10: []})
        self.assertEqual(len(m.warnings), 2)

    def test_bad_listing_rejected(self):
        for bad in ({"hq": "false", "pricePerUnit": 1, "quantity": 1},
                    {"hq": False, "pricePerUnit": -1, "quantity": 1}):
            with self.assertRaises(ValueError):
                validate_payload({"itemID": 10, "listings": [bad]})


class RealCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load(ROOT / "data" / "catalog.json")

    def test_all_jobs_and_recipe_coverage(self):
        self.assertEqual({l["job"] for l in self.data["leves"]}, set(JOBS))
        outputs = {r["item"] for r in self.data["recipes"]}
        for l in self.data["leves"]:
            self.assertTrue(set(l["items"]) <= outputs, l["name"])

    def test_multiple_item_slots_summed(self):
        l = next(l for l in self.data["leves"] if l["name"] == "Sticks and Stones (L)")
        self.assertEqual(sum(l["items"].values()), 3)
        self.assertEqual(l["allowances"], 10)

    def test_repeat_large_and_regular(self):
        l = next(l for l in self.data["leves"] if l["name"] == "Living Bow to Mouth (L)")
        self.assertEqual((l["max_turnins"], l["allowances"]), (6, 10))
        l = next(l for l in self.data["leves"] if l["name"] == "Polearms Aplenty")
        self.assertEqual(l["max_turnins"], 3)

    def test_intermediate_tiers_not_dropped(self):
        levels = {l["level"] for l in available(self.data, "Carpenter", 54)}
        self.assertTrue({50, 52, 54} <= levels)
        self.assertNotIn(56, levels)

    def test_xp_real_level_one(self):
        self.assertEqual(self.data["xp"][1], 300)
        self.assertEqual(self.data["level_cap"], 100)

    def test_every_five_level_band_all_jobs_offline(self):
        from leveplanner.app import demo_prices
        for job in JOBS:
            for level in [1] + list(range(5, 100, 5)):
                with self.subTest(job=job, level=level):
                    settings = Settings(job, level, budget=10000000, craft_hq=True)
                    leves = available(self.data, job, level)
                    ids = market_ids(self.data, leves, settings)
                    plans = recommend(self.data, leves, demo_prices(self.data, ids), settings)
                    self.assertTrue(plans)
                    self.assertTrue(any(p.cost is not None for p in plans))
                    self.assertTrue(all(p.progress.end_level >= next_target(level, 100) for p in plans))


if __name__ == "__main__":
    unittest.main()
