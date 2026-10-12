import copy
import tempfile
import threading
import time
import unittest
from pathlib import Path

from marketscout.settings import default_settings, normalize_settings, load_settings, save_settings
from marketscout.market import Listing, Sale, Snapshot
from marketscout.analysis import estimate, demand, select_items, select_finalists, analyze, item_url

NOW = int(time.time())


def fixture():
    settings = default_settings()
    settings.update(batch_size=10, food=10, craft_per=1)
    settings['professions']['Carpenter']['level'] = 40
    recipe = dict(id=1, item=1, job='Carpenter', level=20, yield_=3,
                  ingredients={2: 2, 3: 3}, hq=True, book=0, specialist=False,
                  stars=0, craftsmanship=0, control=0)
    recipe['yield'] = recipe.pop('yield_')
    data = {'items': {1: {'name': 'Test Bow', 'hq': True}, 2: {'name': 'Wood'}, 3: {'name': 'Crystal'}},
            'recipes': [recipe], 'gathering': []}
    candidate = dict(item=1, job='Carpenter', mode='craft', level=20, recipe=recipe)
    sale = Snapshot([Listing(110, 50, False)], [Sale(100, 99, False, NOW-10)]*6, NOW, NOW)
    purchase = {2: Snapshot([Listing(5, 9, False)], uploaded=NOW),
                3: Snapshot([Listing(2, 12, False)], uploaded=NOW)}
    return data, settings, candidate, sale, purchase


class AnalysisTests(unittest.TestCase):
    def test_yield_whole_stacks_crystals_tax_and_consumables(self):
        data, settings, c, sale, buying = fixture()
        row = estimate(c, 'NQ', data, settings, sale, buying, NOW)
        self.assertEqual(row['units'], 12)
        self.assertEqual([(i['required'], i['purchased']) for i in row['inputs']], [(8, 9), (12, 12)])
        self.assertEqual(row['cost'], 47 + 25 + 10 + 4)
        self.assertEqual(row['revenue'], 1140)
        self.assertEqual(row['profit'], 1054)
        settings['consumables'] = [dict(item=2, quantity=2, hq=False, mode='craft')]
        missing = estimate(c, 'NQ', data, settings, sale, buying, NOW)
        self.assertIsNone(missing['cost'])
        self.assertIsNone(missing['profit'])
        self.assertIn('нужно 10, доступно 9', ' '.join(missing['issues']))

    def test_absent_price_different_from_short_stock_and_api_error(self):
        data, settings, c, sale, buying = fixture()
        for snapshot, expected in [(Snapshot(uploaded=NOW), 'Нет предложений'),
                                   (Snapshot([Listing(1, 1, False)], uploaded=NOW), 'Не хватает количества'),
                                   (Snapshot(errors=['offline']), 'API/данные')]:
            buying[2] = snapshot
            row = estimate(c, 'NQ', data, settings, sale, buying, NOW)
            self.assertIsNone(row['profit'])
            self.assertIn(expected, ' '.join(row['issues']))

    def test_history_units_transactions_window_and_quality(self):
        snap = Snapshot([Listing(50, 3, False), Listing(300, 2, True)],
                        [Sale(80, 99, False, NOW-1), Sale(400, 2, True, NOW-1),
                         Sale(1, 9999, False, NOW-8*86400), Sale(1, 2, False, NOW+1)], NOW, NOW)
        nq, hq = demand(snap, 'NQ', NOW), demand(snap, 'HQ', NOW)
        self.assertEqual(nq['sales_day'], 99/7)
        self.assertEqual(nq['transactions_day'], 1/7)
        self.assertEqual(nq['price'], 49)
        self.assertEqual(hq['price'], 299)
        self.assertEqual(hq['sales_day'], 2/7)
        self.assertIsNone(demand(Snapshot(), 'NQ', NOW)['sales_day'])

    def test_budget_is_optional_and_zero_is_real_limit(self):
        data, settings, c, sale, buying = fixture()
        self.assertFalse(estimate(c, 'NQ', data, settings, sale, buying, NOW)['issues'])
        settings['budget'] = 0
        self.assertIn('Не хватает бюджета', ' '.join(estimate(c, 'NQ', data, settings, sale, buying, NOW)['issues']))

    def test_fishing_bait_unknown_and_spear_no_bait(self):
        data, settings, _, sale, buying = fixture()
        source = dict(kind='unknown', windows=[], location='Spot', fishing_method='rod', conditions=[])
        c = dict(item=1, job='Fisher', mode='gather', level=10, source=source)
        row = estimate(c, 'NQ', data, settings, sale, buying, NOW)
        self.assertIsNone(row['profit'])
        settings['professions']['Fisher']['bait_per_unit'] = 7
        settings['professions']['Fisher']['rate'] = 100
        known = estimate(c, 'NQ', data, settings, sale, buying, NOW)
        self.assertEqual(known['cost'], 80)
        self.assertIsNone(known['hourly'])
        source['fishing_method'] = 'spear'
        settings['professions']['Fisher']['bait_per_unit'] = None
        self.assertEqual(estimate(c, 'NQ', data, settings, sale, buying, NOW)['cost'], 10)

    def test_timed_node_finite_yield_no_continuous_hourly(self):
        data, settings, _, sale, buying = fixture()
        c = dict(item=1, job='Miner', mode='gather', level=10,
                 source=dict(kind='timed', windows=[dict(start_hour=8, duration_hours=2)], location='Spot', conditions=[]))
        settings['professions']['Miner'].update(rate=100, window_yield=4)
        row = estimate(c, 'NQ', data, settings, sale, buying, NOW)
        self.assertIsNone(row['hourly'])
        self.assertIn('минимум 3 окон', ' '.join(row['warnings']))
        self.assertIn('ET', row['windows_text'])

    def test_screen_balances_profiles_deduplicates_and_caps(self):
        items = [dict(item=i, job='Miner' if i<10 else 'Carpenter', mode='gather', level=10) for i in range(1,20)]
        result = select_items(items + items, 4)
        self.assertEqual(len(set(result)), 4)
        self.assertTrue(any(i<10 for i in result) and any(i>=10 for i in result))
        self.assertEqual(item_url(5405), 'https://universalis.app/market/5405')

    def test_pipeline_distinct_sale_and_purchase_scopes(self):
        data, settings, _, sale, buying = fixture()
        calls = []
        class FakeMarket:
            warnings = []
            def aggregate(self, scope, ids, **kwargs):
                calls.append(('aggregate', scope, set(ids)))
                return {1: 100}
            def fetch(self, scope, ids, history=False, **kwargs):
                calls.append(('history' if history else 'ingredients', scope, set(ids)))
                return {1: sale} if history else buying
        rows, summary = analyze(data, settings, FakeMarket())
        self.assertEqual(calls[0], ('aggregate', 'Odin', {1}))
        self.assertEqual(calls[1], ('history', 'Odin', {1}))
        self.assertEqual(calls[2], ('ingredients', 'Light', {2,3}))
        self.assertEqual(summary['detailed'], 1)
        self.assertEqual(len(rows), 1)
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(InterruptedError):
            analyze(data, settings, FakeMarket(), cancel=cancel)

    def test_fast_selling_gatherables_do_not_displace_crafters(self):
        candidates = [dict(item=i, job='Miner', mode='gather') for i in range(1, 41)]
        candidates += [dict(item=100, job='Carpenter', mode='craft'),
                       dict(item=101, job='Carpenter', mode='craft')]
        velocities = {i: 10000-i for i in range(1, 41)}
        velocities.update({100: 4, 101: 3})
        chosen = select_finalists(candidates, list(velocities), velocities, 3)
        self.assertEqual(chosen, [1, 100, 2])
        candidates += [dict(item=1, job='Botanist', mode='gather')]
        chosen = select_finalists(candidates, list(velocities), velocities, 100)
        self.assertEqual(len(chosen), len(set(velocities)))
        self.assertEqual(len(chosen), len(set(chosen)))

    def test_profession_and_source_filter_before_any_market_screening(self):
        data, settings, _, sale, buying = fixture()
        data['items'][4] = dict(name='Ore', hq=False)
        data['gathering'] = [dict(id=4, item=4, job='Miner', level=1, book=0,
                                  kind='always', windows=[], location='Mine', conditions=[])]
        settings.update(analysis_job='Carpenter', analysis_mode='craft')
        calls = []
        class FakeMarket:
            warnings = []
            def aggregate(self, scope, ids, **kwargs):
                calls.append(set(ids))
                return {1: 1, 4: 100000}
            def fetch(self, scope, ids, history=False, **kwargs):
                return {i: (sale if history else buying[i]) for i in ids}
        rows, summary = analyze(data, settings, FakeMarket())
        self.assertEqual(calls, [{1}])
        self.assertEqual(summary['eligible'], 1)
        self.assertEqual(summary['analyzed_jobs'], ['Carpenter'])
        self.assertEqual({r['job'] for r in rows}, {'Carpenter'})
        settings['analysis_mode'] = 'gather'
        rows, summary = analyze(data, settings, FakeMarket())
        self.assertEqual(calls[-1], set())
        self.assertFalse(rows)
        self.assertEqual(summary['eligible'], 0)

    def test_expensive_without_buyers_is_hidden_by_default(self):
        data, settings, _, sale, buying = fixture()
        sale.listings = [Listing(999999, 100, False)]
        sale.sales = []
        class FakeMarket:
            warnings = []
            def aggregate(self, scope, ids, **kwargs):
                return {1: 10000}
            def fetch(self, scope, ids, history=False, **kwargs):
                return {1: sale} if history else buying
        rows, _ = analyze(data, settings, FakeMarket())
        self.assertFalse(rows)
        settings['show_illiquid'] = True
        rows, _ = analyze(data, settings, FakeMarket())
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]['liquid'])
        self.assertIn('Малоликвидный', rows[0]['status'])

    def test_old_or_single_large_trade_does_not_fake_popularity(self):
        data, settings, c, sale, buying = fixture()
        sale.sales = [Sale(100000, 999, False, NOW-10)]
        self.assertFalse(estimate(c, 'NQ', data, settings, sale, buying, NOW)['liquid'])
        sale.sales = [Sale(100000, 999, False, NOW-4*86400)]*5
        self.assertFalse(estimate(c, 'NQ', data, settings, sale, buying, NOW)['liquid'])


class SettingsTests(unittest.TestCase):
    def test_persistence_all_profiles_item_measurements_and_blank_budget(self):
        settings = default_settings()
        for index, p in enumerate(settings['professions'].values()):
            p.update(level=index+20, enabled=index % 2 == 0)
        settings['budget'] = ''
        settings['item_profiles']['5405'] = dict(rate=300, window_yield=40, per_unit=2, bait_per_unit=None)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'settings.json'
            save_settings(path, settings)
            loaded, warnings = load_settings(path)
            self.assertFalse(warnings)
            self.assertIsNone(loaded['budget'])
            self.assertEqual(loaded['professions'], settings['professions'])
            self.assertEqual(loaded['item_profiles'], settings['item_profiles'])

    def test_corrupt_settings_backup_and_invalid_numbers(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'settings.json'
            path.write_text('{broken', encoding='utf8')
            loaded, warnings = load_settings(path)
            self.assertTrue(warnings)
            self.assertEqual(len(list(path.parent.glob('*.broken-*'))), 1)
            self.assertEqual(loaded['professions']['Miner']['level'], 1)
        for bad in ('NaN', '-1', 'Infinity', True):
            settings = default_settings(); settings['budget'] = bad
            with self.assertRaises(ValueError):
                normalize_settings(settings)
        settings = default_settings(); settings['professions']['Miner']['level'] = 101
        with self.assertRaises(ValueError):
            normalize_settings(settings)


if __name__ == '__main__':
    unittest.main()
