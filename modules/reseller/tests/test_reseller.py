from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from reseller.analysis import analyze, basket, rank, telegram_text
from reseller.market import Listing, Sale, Snapshot, Market, overview_score
from reseller.net import ApiError, Cancelled, Cache, Http
from reseller.scanner import Scanner, watch_ids
from reseller.storage import defaults, normalize, load_settings, atomic_json, protect, save_token, load_token
from reseller.telegram import Telegram, Notifier
from reseller.catalog import load_catalog, load_worlds

NOW = 1800000000
ITEMS = {10: {'name': 'Test Ingot', 'category': 'Metal', 'hq': True},
         11: {'name': 'Other', 'category': 'Other', 'hq': False}}
WORLDS = {67: 'Shiva', 66: 'Odin', 36: 'Lich', 97: 'Ragnarok'}
REGIONS = {'worlds': {str(k): v for k, v in WORLDS.items()},
           'dcs': [{'name': 'Light', 'worlds': [67, 66, 36]}, {'name': 'Chaos', 'worlds': [97]}]}
TOKEN = '123456:' + 'x' * 30  # Synthetic; all Telegram traffic is mocked.


def snapshot():
    sales = [Sale(1000, 10, False, NOW - 3600 - i * 20000) for i in range(25)]
    return Snapshot([Listing(10, 66, 300, 10, False, NOW, 'one'), Listing(10, 67, 1000, 2, False, NOW)],
                    sales, {67: NOW, 66: NOW, 36: NOW}, NOW, NOW)


def deals(s=None, snap=None):
    return analyze(ITEMS, {10: snap or snapshot()}, s or defaults(), WORLDS, [67, 66, 36], NOW)


class EconomicsTests(unittest.TestCase):
    def test_whole_stack_taxes_stress_near_supply_and_signs(self):
        result, _ = deals()
        self.assertEqual(len(result), 1)
        d = result[0]
        self.assertEqual((d.quantity, d.sale_price, d.cost, d.revenue, d.profit), (10, 999, 3150, 9490, 6340))
        self.assertEqual(d.stress_profit, 5390)
        self.assertEqual(d.near_supply, 2)
        self.assertGreater(d.estimated_days, 10 / (d.units_day * .25))
        self.assertIn('−3,150', d.detail())
        self.assertIn('+6,340', d.detail())
        self.assertIn('world=Odin', telegram_text(d))

    def test_slow_large_stack_is_not_profitable_recommendation(self):
        snap = snapshot()
        snap.listings[0] = replace(snap.listings[0], quantity=999)
        result, reason = deals(snap=snap)
        self.assertFalse(result)
        self.assertIn('слишком большой стак / срок продажи', reason)

    def test_quality_world_and_dc_are_not_mixed(self):
        snap = snapshot()
        snap.listings += [Listing(10, 97, 1, 10, False, NOW), Listing(10, 66, 1, 10, True, NOW)]
        result, _ = deals(snap=snap)
        self.assertEqual([d.buy_world for d in result], ['Odin'])
        self.assertFalse(result[0].hq)

    def test_no_sales_is_not_replaced_with_listing_price(self):
        snap = snapshot()
        snap.sales = []
        result, reasons = deals(snap=snap)
        self.assertFalse(result)
        self.assertIn('недостаточный спрос', reasons)

    def test_old_uploaded_world_rejected_but_old_placement_allowed(self):
        snap = snapshot()
        snap.listings[0] = replace(snap.listings[0], reviewed=NOW - 7 * 86400)
        self.assertTrue(deals(snap=snap)[0])
        snap.uploads[66] = NOW - 86400
        self.assertFalse(deals(snap=snap)[0])

    def test_stale_destination_cache_future_dates_and_api_failure_rejected(self):
        for attr, value in [('listing_fetched', NOW - 301), ('history_fetched', NOW + 1), ('errors', ['Failed'])]:
            snap = snapshot()
            setattr(snap, attr, value)
            self.assertFalse(deals(snap=snap)[0], attr)
        snap = snapshot()
        snap.uploads[67] = NOW - 86400
        self.assertFalse(deals(snap=snap)[0])

    def test_two_recent_cheap_volume_sales_prevent_false_profit(self):
        snap = snapshot()
        snap.listings = [Listing(10, 66, 200, 10, False, NOW)]
        snap.sales = [Sale(1000, 1, False, NOW - 2 * 86400 - i * 16000) for i in range(10)]
        snap.sales += [Sale(100, 100, False, NOW - 3600), Sale(100, 100, False, NOW - 7200)]
        self.assertFalse(deals(snap=snap)[0])

    def test_one_recent_decline_caps_reference_even_with_small_quantity(self):
        snap = snapshot()
        snap.listings = [Listing(10, 66, 200, 10, False, NOW)]
        snap.sales = [Sale(1000, 10, False, NOW - 2 * 86400 - i * 10000) for i in range(15)] + [Sale(100, 1, False, NOW - 3600)]
        self.assertFalse(deals(snap=snap)[0])

    def test_demand_stability_and_supply_filter(self):
        snap = snapshot()
        snap.sales = [Sale(1000, 10, False, NOW - i * 60) for i in range(20)]
        self.assertFalse(deals(snap=snap)[0])
        snap = snapshot()
        snap.listings[1] = replace(snap.listings[1], quantity=9999)
        self.assertFalse(deals(snap=snap)[0])

    def test_stress_filter_budget_and_trip(self):
        for settings in [dict(budget=3000), dict(travel=10000), dict(stress_drop=50, min_roi=0, min_discount=0)]:
            s = {**defaults(), **settings}
            snap = snapshot()
            if 'stress_drop' in settings:
                snap.listings[0] = replace(snap.listings[0], price=600)
            self.assertFalse(deals(s, snap)[0])

    def test_distinct_stable_ids_and_duplicate_listing(self):
        snap = snapshot()
        snap.listings += [replace(snap.listings[0], identity='two'), snap.listings[0]]
        result, _ = deals(snap=snap)
        self.assertEqual(len(result), 2)
        old_keys = {d.fingerprint for d in result}
        snap.listings = [replace(l, reviewed=NOW - 100) for l in snap.listings]
        self.assertEqual(old_keys, {d.fingerprint for d in deals(snap=snap)[0]})

    def test_basket_total_budget_reserve_and_one_quality(self):
        d = deals()[0][0]
        more = [d, replace(d, fingerprint='second', profit=999999, stress_profit=100, profit_day=100),
                replace(d, item=11, fingerprint='other')]
        s = {**defaults(), 'budget': 8000, 'reserve_percent': 20}
        chosen = basket(more, s)
        self.assertEqual(len(chosen), 2)
        self.assertLessEqual(sum(d.cost for d in chosen), 6400)
        self.assertEqual(len({(d.item, d.hq) for d in chosen}), 2)
        s['budget'] = 6000
        self.assertEqual(len(basket(more, s)), 1)

    def test_priority_changes_for_large_profit_slow_turnover(self):
        d = deals()[0][0]
        slow = replace(d, fingerprint='slow', profit=100000, profit_day=10, capital_day=.1)
        self.assertIs(rank([slow, d], defaults())[0], d)
        self.assertIs(rank([slow, d], {**defaults(), 'priority': 'Прибыль стака'})[0], slow)

    def test_capped_history_is_explicit(self):
        snap = snapshot()
        snap.truncated = True
        self.assertIn('нижняя граница', deals(snap=snap)[0][0].detail())


class SettingsTests(unittest.TestCase):
    def test_blank_budget_and_number_validation(self):
        self.assertIsNone(normalize({**defaults(), 'budget': ''})['budget'])
        self.assertEqual(normalize({**defaults(), 'min_roi': '20,5'})['min_roi'], 20.5)
        for key, value in [('budget', 'nan'), ('notify', 'True'), ('detail_limit', 1.5), ('stress_drop', 99), ('priority', 'invalid')]:
            with self.assertRaises(ValueError):
                normalize({**defaults(), key: value})

    def test_settings_persist_and_corruption_recovers(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            saved = {**defaults(), 'sale_world': 'Odin', 'budget': 100000}
            atomic_json(root / 'settings.json', saved)
            self.assertEqual(load_settings(root)[0]['sale_world'], 'Odin')
            (root / 'settings.json').write_text('broken')
            self.assertTrue(load_settings(root)[1])

    def test_windows_dpapi_token_roundtrip_has_no_plaintext(self):
        import os
        if os.name != 'nt':
            self.skipTest('DPAPI is Windows only')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            save_token(TOKEN, root)
            self.assertNotIn(TOKEN, (root / 'telegram-secret.json').read_text())
            self.assertEqual(load_token(root)[0], TOKEN)

    def test_watchlist_validation(self):
        self.assertEqual(watch_ids('10,11;10', ITEMS), [10, 11])
        for text in ('', 'xyz', '123'):
            with self.assertRaises(ValueError):
                watch_ids(text, ITEMS)

    def test_real_catalog_all_categories_and_light_membership(self):
        with tempfile.TemporaryDirectory() as folder:
            data, regions = load_catalog(folder), load_worlds(folder)
        self.assertGreater(len(data['items']), 16000)
        self.assertEqual(data['items']['5284']['name'], 'Peiste Leather')
        light = next(d for d in regions['dcs'] if d['name'] == 'Light')
        self.assertIn(67, light['worlds'])
        self.assertNotIn(97, light['worlds'])


class Response(io.BytesIO):
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.close()


class NetworkTests(unittest.TestCase):
    def test_rate_limiter_counts_actual_requests_and_retries_429(self):
        clock, sleeps, calls = [100.0], [], []
        def sleep(delay, cancel):
            sleeps.append(delay)
            clock[0] += delay
        class Opener:
            def open(self, request, timeout):
                calls.append(request.full_url)
                if len(calls) == 1:
                    raise HTTPError(request.full_url, 429, 'Limited', {'Retry-After': '2'}, None)
                return Response(b'{}')
        http = Http(Opener(), clock=lambda: clock[0], sleeper=sleep)
        self.assertEqual(http.get('https://universalis.app/api/v2/worlds'), {})
        http.get('https://universalis.app/api/v2/worlds')
        self.assertEqual(http.requests, 3)
        self.assertIn(2, sleeps)
        self.assertTrue(any(0 < v <= .101 for v in sleeps))

    def test_expired_cache_never_falls_back_on_error(self):
        class Fake:
            def get(self, url, cancel):
                raise ApiError('Offline')
        with tempfile.TemporaryDirectory() as folder:
            c = Cache(folder, Fake(), now=lambda: NOW)
            import hashlib
            path = Path(folder) / (hashlib.sha256(b'url').hexdigest() + '.json')
            atomic_json(path, {'time': NOW - 121, 'data': {'old': True}})
            with self.assertRaises(ApiError):
                c.get('url')
            atomic_json(path, {'time': NOW - 119, 'data': {'old': True}})
            self.assertEqual(c.get('url')[0], {'old': True})

    def test_cancel_avoids_network(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(Cancelled):
            Http().get('https://universalis.app/api/v2/worlds', event)

    def test_detail_single_multi_shapes_and_retainer_fallback(self):
        class Fake:
            def get(self, url, *args):
                if '/history/' in url:
                    row = {'itemID': 10, 'entries': [dict(pricePerUnit=1000, quantity=1, hq=False, timestamp=NOW)]}
                else:
                    row = {'itemID': 10, 'worldUploadTimes': {'67': NOW * 1000, '66': NOW * 1000},
                           'listings': [dict(worldID=66, pricePerUnit=300, quantity=10, hq=False, retainerName=name) for name in ('A', 'B')]}
                return row, NOW
        snap = Market(Fake(), now=lambda: NOW).detailed('Light', 67, [10])[10]
        self.assertFalse(snap.errors)
        self.assertEqual({l.identity for l in snap.listings}, {'A', 'B'})
        self.assertEqual(len(snap.sales), 1)

    def test_missing_items_invalid_schema_and_failed_overview_explicit(self):
        class Fake:
            def get(self, url, *args):
                return ({'results': [], 'failedItems': [10]} if '/aggregated/' in url else {'items': {}}), NOW
        market = Market(Fake(), now=lambda: NOW)
        self.assertEqual(market.overview(67, [10], defaults()), {})
        self.assertTrue(market.warnings)
        self.assertTrue(market.detailed('Light', 67, [10])[10].errors)

    def test_screening_never_accepts_no_demand(self):
        part = {'minListing': {'dc': {'price': 100}}, 'averageSalePrice': {'world': {'price': 1000}}, 'dailySaleVelocity': {'world': {'quantity': 0}}}
        self.assertIsNone(overview_score(part, defaults()))
        part['dailySaleVelocity']['world']['quantity'] = 10
        self.assertGreater(overview_score(part, defaults()), 0)

    def test_detailed_rotation_survives_score_reordering(self):
        items = {i: ITEMS[10] for i in (10, 11, 12)}
        scanner = Scanner(items, REGIONS, None)
        scanned = []
        def detail(dc, world, ids, *args, **kwargs):
            scanned.extend(ids)
            return {i: snapshot() for i in ids}
        s = {**defaults(), 'detail_limit': 1}
        with patch('reseller.scanner.Market.overview', side_effect=[{10: 100, 11: 50, 12: 20}, {11: 150, 10: 50, 12: 20}, {10: 200, 12: 50, 11: 20}]), patch('reseller.scanner.Market.detailed', side_effect=detail):
            for _ in range(3):
                scanner.scan(s)
        self.assertEqual(set(scanned), {10, 11, 12})

    def test_overview_chunks_rotate_and_watch_skips_averages(self):
        items = {i: ITEMS[10] for i in range(1, 251)}
        scanner = Scanner(items, REGIONS, None)
        batches = []
        def overview(world, ids, *args):
            batches.append(ids)
            return {}
        with patch('reseller.scanner.Market.overview', side_effect=overview):
            for _ in range(3):
                scanner.scan({**defaults(), 'overview_limit': 100})
        self.assertEqual(set(i for b in batches for i in b), set(items))
        with patch('reseller.scanner.Market.overview') as over, patch('reseller.scanner.Market.detailed', return_value={}):
            scanner.scan({**defaults(), 'mode': 'watch', 'watch_ids': '10'})
            over.assert_not_called()


class TelegramTests(unittest.TestCase):
    def test_new_eleventh_item_is_not_starved_by_already_sent_basket(self):
        d = deals()[0][0]
        all_deals = [replace(d, item=i, fingerprint=f'id-{i}', profit_day=1000 - i) for i in range(11)]
        s = {**defaults(), 'chat_id': '123', 'alert_limit': 10}
        class Bot:
            def send(self, *args):
                return True
        with tempfile.TemporaryDirectory() as folder:
            n = Notifier(Path(folder) / 'alerts.json', now=lambda: NOW, sleeper=lambda *a: None)
            first = basket(n.eligible(all_deals, s), s)
            self.assertEqual(n.dispatch(first, s, Bot())[0], 10)
            second = basket(n.eligible(all_deals, s), s)
            self.assertEqual([d.item for d in second], [10])
            self.assertEqual(n.dispatch(second, s, Bot())[0], 1)
    def test_post_payload_links_no_parse_mode_and_synthetic_token(self):
        requests = []
        class Fake:
            def open(self, request, timeout):
                requests.append(request)
                return Response(b'{"ok":true,"result":{"message_id":1}}')
        bot = Telegram(TOKEN, Fake())
        bot.send('123', telegram_text(deals()[0][0]))
        payload = json.loads(requests[0].data)
        self.assertEqual(payload['chat_id'], '123')
        self.assertNotIn('parse_mode', payload)
        self.assertIn('https://universalis.app', payload['text'])

    def test_token_never_appears_in_http_error(self):
        class Fake:
            def open(self, request, timeout):
                raise HTTPError(request.full_url, 401, 'Unauthorized', {}, None)
        with self.assertRaises(ApiError) as exc:
            Telegram(TOKEN, Fake()).send('123', 'test')
        self.assertNotIn(TOKEN, str(exc.exception))
        self.assertNotIn('api.telegram.org/bot', str(exc.exception))

    def test_dedup_restart_cooldown_and_failed_sends_not_recorded(self):
        d = deals()[0][0]
        s = {**defaults(), 'chat_id': '123'}
        class Bot:
            def __init__(self):
                self.calls = 0
            def send(self, *args):
                self.calls += 1
        bot = Bot()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'alerts.json'
            n = Notifier(path, now=lambda: NOW, sleeper=lambda *a: None)
            self.assertEqual(n.dispatch([d, replace(d, fingerprint='other')], s, bot)[0], 1)
            self.assertEqual(Notifier(path, now=lambda: NOW + 1).dispatch([d], s, bot)[0], 0)
            s['chat_id'] = '456'
            self.assertEqual(n.dispatch([d], s, bot)[0], 1)
            class Failed:
                def send(self, *args):
                    raise ApiError('failed')
            self.assertEqual(n.dispatch([replace(d, item=99, fingerprint='fail')], s, Failed())[0], 0)
            self.assertNotIn('456:listing:fail', n.state)

    def test_confirmed_delivery_counted_even_if_state_save_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            n = Notifier(Path(folder) / 'alerts.json', now=lambda: NOW, sleeper=lambda *a: None)
            class Bot:
                def send(self, *args):
                    return True
            with patch('reseller.telegram.atomic_json', side_effect=OSError()):
                count, errors = n.dispatch(deals()[0], {**defaults(), 'chat_id': '123'}, Bot())
            self.assertEqual(count, 1)
            self.assertTrue(errors)

    def test_cancellation_is_not_converted_to_delivery_error(self):
        with tempfile.TemporaryDirectory() as folder:
            n = Notifier(Path(folder) / 'alerts.json', now=lambda: NOW)
            class Bot:
                def send(self, *args):
                    raise Cancelled('Stopped')
            with self.assertRaises(Cancelled):
                n.dispatch(deals()[0], {**defaults(), 'chat_id': '123'}, Bot())


class GuiTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.temp = tempfile.TemporaryDirectory()
        from reseller.app import Application
        self.app = Application(self.root, demo=True, folder=Path(self.temp.name))
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        if self.root.winfo_exists():
            self.app.close()

    def test_demo_background_scan_sort_filter_basket_and_csv(self):
        self.app.scan()
        until = time.monotonic() + 10
        while self.app.busy and time.monotonic() < until:
            self.root.update()
            time.sleep(.01)
        self.assertFalse(self.app.busy)
        self.assertTrue(self.app.deals)
        self.assertTrue(self.app.basket)
        self.assertIn('DEMO', self.app.status.get())
        self.app.sort('cost')
        self.assertEqual([d.cost for d in self.app.visible], sorted(d.cost for d in self.app.visible))
        self.app.basket_only.set(True)
        self.app.render()
        self.assertEqual(len(self.app.visible), len(self.app.basket))
        self.app.table.selection_set('0')
        self.app.describe()
        self.assertIn('искусственные', self.app.detail.get('1.0', 'end'))
        path = Path(self.temp.name) / 'out.csv'
        with patch('reseller.app.filedialog.asksaveasfilename', return_value=str(path)):
            self.app.export()
        self.assertIn('stress_profit', path.read_text(encoding='utf-8-sig'))
        self.app.search.set('unfindable query')
        self.assertFalse(self.app.table.get_children())

    def test_demo_never_sends_telegram_and_settings_saved(self):
        self.app.vars['notify'].set(True)
        with patch('reseller.app.Telegram') as bot:
            self.app.test_telegram()
            self.app.find_chat()
            bot.assert_not_called()
        self.app.vars['sale_world'].set('Odin')
        self.assertTrue(self.app.save())
        self.assertEqual(load_settings(Path(self.temp.name))[0]['sale_world'], 'Odin')

    def test_monitor_stop_cancels_future_cycles(self):
        self.app.start_monitor()
        self.assertTrue(self.app.monitoring)
        self.app.stop()
        self.assertFalse(self.app.monitoring)
        until = time.monotonic() + 5
        while self.app.busy and time.monotonic() < until:
            self.root.update()
            time.sleep(.01)
        self.assertIsNone(self.app.monitor_timer)

    def test_manual_scan_between_cycles_leaves_only_one_monitor_timer(self):
        baseline = len(self.root.tk.call('after', 'info'))
        self.app.start_monitor()
        def finish():
            until = time.monotonic() + 5
            while self.app.busy and time.monotonic() < until:
                self.root.update()
                time.sleep(.01)
            self.assertFalse(self.app.busy)
        finish()
        old_timer = self.app.monitor_timer
        self.assertIsNotNone(old_timer)
        self.app.scan()
        finish()
        timers = self.root.tk.call('after', 'info')
        self.assertNotIn(old_timer, timers)
        self.assertIn(self.app.monitor_timer, timers)
        self.assertEqual(len(timers), baseline + 1)
        self.app.stop()
        self.assertEqual(len(self.root.tk.call('after', 'info')), baseline)

    def test_disabling_token_persistence_removes_previous_secret(self):
        self.app.demo = False
        self.app.token.set(TOKEN)
        self.assertTrue(self.app.save())
        path = Path(self.temp.name) / 'telegram-secret.json'
        self.assertTrue(path.exists())
        self.app.remember_token.set(False)
        self.assertTrue(self.app.save())
        self.assertFalse(path.exists())
