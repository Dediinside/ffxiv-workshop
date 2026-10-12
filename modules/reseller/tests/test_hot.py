import copy
from dataclasses import replace
from pathlib import Path
import sqlite3
import ssl
import tempfile
import threading
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from reseller.hot import parse_event, Pending, Observations, Ledger, Stream, HotMonitor
from reseller.market import offer_keys, Listing, Market
from reseller.net import Cancelled, ApiError
from reseller.storage import defaults
from reseller.telegram import Notifier
from test_reseller import NOW, ITEMS, WORLDS, REGIONS, snapshot, deals


def raw(identity='123', price=300, quantity=10):
    return dict(listingID=identity, pricePerUnit=price, quantity=quantity, hq=False,
                retainerID='same-retainer', lastReviewTime=NOW, worldID=None)


def offer(row=None, world=66, item=10):
    _, result = parse_event(dict(event='listings/add', item=item, world=world, listings=[row or raw()]), [66, 36], ITEMS, defaults(), NOW)
    return result[0]


def listing(o):
    return Listing(o.item, o.world, o.raw['pricePerUnit'], o.raw['quantity'], o.raw['hq'], NOW,
                   str(o.raw.get('listingID') or ''), o.key, o.base)


class IdentityTests(unittest.TestCase):
    def test_bson_envelope_world_and_filters(self):
        import bson
        message = bson.decode(bson.encode(dict(event='listings/add', item=10, world=66, listings=[raw()])))
        _, values = parse_event(message, [66], ITEMS, defaults(), NOW)
        self.assertEqual(values[0].world, 66)
        for change in [dict(world=97), dict(item=999), dict(listings=[{**raw(), 'onMannequin': True}]), dict(listings=[{**raw(), 'quantity': 0}])]:
            self.assertFalse(parse_event({**message, **change}, [66], ITEMS, defaults(), NOW)[1])

    def test_dirty_time_change_is_not_a_new_offer(self):
        a = offer(raw('dirty:one'))
        b = offer({**raw('dirty:two'), 'lastReviewTime': NOW + 100})
        self.assertEqual(a.key, b.key)
        self.assertNotEqual(a.key, offer(raw('dirty:three', price=200)).key)

    def test_stable_id_drop_increase_and_restart_dedup(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'seen.sqlite3'
            store = Observations(path, now=lambda: NOW)
            self.assertIn('Впервые', store.accept([offer()])[0].kind)
            self.assertFalse(store.accept([offer()]))
            self.assertIn('Цена снизилась', store.accept([offer(raw(price=200))])[0].kind)
            self.assertFalse(store.accept([offer(raw(price=250))]))
            store.close()
            store = Observations(path, now=lambda: NOW + 1)
            self.assertFalse(store.accept([offer(raw(price=200))]))
            store.close()

    def test_weak_identity_does_not_conflate_retainer_stacks(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Observations(Path(folder) / 'seen.sqlite3', now=lambda: NOW)
            self.assertTrue(store.accept([offer(raw('dirty:a', 100))]))
            second = store.accept([offer(raw('dirty:b', 150))])
            self.assertTrue(second)
            self.assertIn('неоднозначен', second[0].kind)
            store.close()

    def test_baseline_old_rows_do_not_become_events_and_queued_exclusion(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Observations(Path(folder) / 'seen.sqlite3', now=lambda: NOW)
            old, pending = offer(), offer(raw('456'))
            snap = snapshot()
            snap.listings = [listing(old), listing(pending)]
            store.baseline({10: snap}, exclude={pending.key})
            self.assertFalse(store.accept([old]))
            self.assertTrue(store.accept([pending]))
            store.close()


class QueueTests(unittest.TestCase):
    def test_two_deltas_merge_and_remove_only_exact_revision(self):
        q = Pending(now=lambda: NOW)
        a, b = offer(), offer(raw(price=200))
        q.push('listings/add', [a])
        q.push('listings/add', [b])
        q.push('listings/remove', [replace(a, received=NOW + 1)])
        self.assertEqual([o.key for o in q.take()], [b.key])
        self.assertFalse(q.active(a))
        self.assertTrue(q.active(b))

    def test_removed_after_take_is_cancelled(self):
        q = Pending(now=lambda: NOW)
        a = offer()
        q.push('listings/add', [a])
        taken = q.take()[0]
        q.push('listings/remove', [replace(a, received=NOW + .1)])
        self.assertFalse(q.active(taken))

    def test_capacity_expiry_and_defer_do_not_extend_original_age(self):
        clock = [NOW]
        q = Pending(capacity=2, now=lambda: clock[0])
        q.push('listings/add', [offer(raw(str(i))) for i in range(3)])
        self.assertEqual(len(q.rows), 2)
        self.assertEqual(q.dropped, 1)
        values = q.take()
        q.defer(values, 60)
        self.assertFalse(q.take())
        clock[0] += 91
        self.assertFalse(q.take())
        self.assertEqual(q.expired, 2)

    def test_extra_delta_for_selected_item_is_not_lost(self):
        q = Pending(now=lambda: NOW)
        q.push('listings/add', [offer(), offer(raw('456'), item=11)])
        first = q.take(item_limit=1)
        q.push('listings/add', [offer(raw('789'))])
        extra = q.take(item_limit=0, item_ids={first[0].item})
        self.assertEqual([o.key for o in extra], [offer(raw('789')).key])
        self.assertTrue(q.rows)

    def test_duplicate_wire_frame_does_not_discard_accepted_retry(self):
        q = Pending(now=lambda: NOW)
        accepted = offer()
        accepted.kind = 'Впервые'
        q.push('listings/add', [offer()])
        q.defer([accepted], 5)
        self.assertIs(q.rows[accepted.key], accepted)
        q.push('listings/remove', [replace(accepted, received=NOW + 1)])
        q.defer([accepted], 5)
        self.assertNotIn(accepted.key, q.rows)


class LedgerTests(unittest.TestCase):
    def test_total_budget_is_shared_across_batches_and_restart(self):
        d = deals()[0][0]
        s = {**defaults(), 'budget': 8000, 'chat_id': '123', 'max_days': 30}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'ledger.sqlite3'
            ledger = Ledger(path, now=lambda: NOW)
            for item in (1, 2):
                changed = replace(d, item=item)
                self.assertTrue(ledger.allowed(changed, s)[0])
                ledger.record(changed, s)
            self.assertFalse(ledger.allowed(replace(d, item=3), s)[0])
            ledger.close()
            ledger = Ledger(path, now=lambda: NOW)
            self.assertEqual(ledger.spent(s), 6300)
            ledger.close()

    def test_reset_budget_keeps_minute_rate_and_improvement(self):
        d = deals()[0][0]
        s = {**defaults(), 'chat_id': '123', 'alert_limit': 1}
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder) / 'ledger.sqlite3', now=lambda: NOW)
            ledger.record(d, s)
            ledger.reset(s)
            self.assertEqual(ledger.spent(s), 0)
            self.assertFalse(ledger.allowed(replace(d, item=999), s)[0])
            self.assertGreaterEqual(ledger.slot_delay(s), 60)
            self.assertTrue(ledger.improved(replace(d, buy_price=200), s))
            self.assertFalse(ledger.improved(replace(d, buy_price=290), s))
            ledger.close()

    def test_total_volume_of_item_is_shared_across_source_worlds(self):
        d = deals()[0][0]
        s = {**defaults(), 'chat_id': '123', 'market_share': 1}
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder) / 'ledger.sqlite3', now=lambda: NOW)
            ledger.record(d, s)
            self.assertFalse(ledger.allowed(replace(d, buy_world_id=36), s)[0])
            ledger.close()

    def test_confirmed_send_reserves_budget_before_cancellable_pause(self):
        d = deals()[0][0]
        s = {**defaults(), 'chat_id': '123'}
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder) / 'ledger.sqlite3', now=lambda: NOW)
            def interrupted(*args):
                raise Cancelled('Stopped')
            notifier = Notifier(Path(folder) / 'alerts.json', now=lambda: NOW, sleeper=interrupted)
            bot = SimpleNamespace(send=lambda *args: True)
            with self.assertRaises(Cancelled):
                notifier.dispatch([d], s, bot, on_delivered=lambda row: ledger.record(row, s))
            self.assertEqual(ledger.spent(s), d.cost)
            ledger.close()


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.scanner = SimpleNamespace(items=ITEMS, regions=REGIONS, cache=None,
                         world=lambda name: (67, REGIONS['dcs'][0]), demo=False)
        self.store = Observations(Path(self.temp.name) / 'seen.sqlite3', now=lambda: NOW)
        self.addCleanup(self.store.close)

    def test_event_scoped_confirm_does_not_include_old_cheap_stack(self):
        o = offer()
        o.kind = 'Впервые замечено в потоке'
        snap = snapshot()
        snap.listings = [listing(o), listing(offer(raw('old'))), snap.listings[-1]]
        market = SimpleNamespace(detailed=lambda *a, **k: {10: snap})
        monitor = HotMonitor(self.scanner, self.temp.name, None, now=lambda: NOW, market=market)
        result, _ = monitor.evaluate([o], defaults(), threading.Event(), self.store)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].offer_key, o.key)
        from reseller.analysis import analyze
        normal, _ = analyze(ITEMS, {10: snap}, defaults(), WORLDS, [67, 66, 36], NOW)
        self.assertIn(result[0].fingerprint, {d.fingerprint for d in normal})

    def test_rest_publication_race_retries_then_confirmed(self):
        o = offer()
        o.kind = 'Впервые'
        old = snapshot()
        new = snapshot()
        new.listings[0] = listing(o)
        market = SimpleNamespace(detailed=unittest.mock.Mock(side_effect=[{10: old}, {10: new}]))
        monitor = HotMonitor(self.scanner, self.temp.name, None, now=lambda: NOW, market=market)
        with patch('reseller.hot.wait'):
            self.assertTrue(monitor.evaluate([o], defaults(), threading.Event(), self.store)[0])
        self.assertEqual(market.detailed.call_count, 2)

    def test_missing_revision_deferred_but_not_notified(self):
        o = offer()
        o.kind = 'Впервые'
        market = SimpleNamespace(detailed=lambda *a, **k: {10: snapshot()})
        monitor = HotMonitor(self.scanner, self.temp.name, None, now=lambda: NOW, market=market)
        q = Pending(now=lambda: NOW)
        with patch('reseller.hot.wait'):
            self.assertFalse(monitor.evaluate([o], defaults(), threading.Event(), self.store, q)[0])
        self.assertIn(o.key, q.rows)
        self.assertEqual(q.rows[o.key].received, NOW)
        self.assertEqual(self.store.accept([o]), [o])

    def test_removed_and_expired_candidates_avoid_http(self):
        o = offer()
        q = Pending(now=lambda: NOW)
        q.push('listings/remove', [replace(o, received=NOW + .1)])
        market = SimpleNamespace(detailed=unittest.mock.Mock())
        monitor = HotMonitor(self.scanner, self.temp.name, None, now=lambda: NOW, market=market)
        self.assertFalse(monitor.evaluate([o], defaults(), threading.Event(), self.store, q)[0])
        self.assertFalse(monitor.evaluate([replace(o, received=NOW - 91)], defaults(), threading.Event(), self.store)[0])
        market.detailed.assert_not_called()

    def test_temporary_history_error_keeps_event_in_bounded_retry(self):
        o = offer()
        o.kind = 'Впервые'
        snap = snapshot()
        snap.listings[0] = listing(o)
        snap.errors = ['History temporarily unavailable']
        market = SimpleNamespace(detailed=lambda *a, **k: {10: snap})
        monitor = HotMonitor(self.scanner, self.temp.name, None, now=lambda: NOW, market=market)
        q = Pending(now=lambda: NOW)
        self.assertFalse(monitor.evaluate([o], defaults(), threading.Event(), self.store, q)[0])
        self.assertIn(o.key, q.rows)

    def test_per_item_history_reused_without_refreshing_timestamp(self):
        class Cache:
            def __init__(self):
                self.urls = []
            def get(self, url, *args):
                self.urls.append(url)
                if '/history/' in url:
                    return {'itemID': 10, 'entries': []}, NOW
                return {'itemID': 10, 'listings': [], 'worldUploadTimes': {'67': NOW * 1000}}, NOW
        cache, clock = Cache(), [NOW]
        market = Market(cache, now=lambda: clock[0])
        market.detailed('Light', 67, [10], True, reuse_history=True)
        clock[0] += 30
        snap = market.detailed('Light', 67, [10], True, reuse_history=True)[10]
        self.assertEqual(snap.history_fetched, NOW)
        self.assertEqual(len([u for u in cache.urls if '/history/' in u]), 1)
        clock[0] += 121
        market.detailed('Light', 67, [10], True, reuse_history=True)
        self.assertEqual(len([u for u in cache.urls if '/history/' in u]), 2)


class TransportTests(unittest.TestCase):
    def test_binary_subscriptions_tls_and_envelope(self):
        import bson
        import websocket
        cancel = threading.Event()
        sent, options = [], []
        class Socket:
            def send_binary(self, data):
                sent.append(bson.decode(data))
            def settimeout(self, value):
                pass
            def recv_data(self, **kwargs):
                cancel.set()
                return websocket.ABNF.OPCODE_BINARY, bson.encode(dict(event='listings/add', item=10, world=66, listings=[raw()]))
            def close(self, **kwargs):
                pass
        def connect(url, **kwargs):
            options.append(kwargs)
            return Socket()
        q = Pending(now=lambda: NOW)
        with patch('websocket.create_connection', side_effect=connect):
            Stream([66], ITEMS, defaults(), q).run(cancel)
        self.assertEqual({v['channel'] for v in sent}, {'listings/add{world=66}', 'listings/remove{world=66}'})
        self.assertEqual(options[0]['sslopt']['cert_reqs'], ssl.CERT_REQUIRED)
        self.assertTrue(options[0]['sslopt']['check_hostname'])
        self.assertEqual(q.events, 1)


class HotGuiTests(unittest.TestCase):
    def test_demo_hot_is_responsive_stops_and_never_sends(self):
        from reseller.app import Application
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        with tempfile.TemporaryDirectory() as folder:
            app = Application(root, demo=True, folder=Path(folder))
            try:
                with patch('reseller.app.Telegram') as bot, patch('websocket.create_connection') as socket:
                    app.start_hot()
                    until = time.monotonic() + 5
                    while not app.deals and time.monotonic() < until:
                        root.update()
                        time.sleep(.01)
                    self.assertTrue(app.deals)
                    self.assertTrue(app.busy)
                    self.assertTrue(app.hot_running)
                    app.stop()
                    until = time.monotonic() + 5
                    while app.busy and time.monotonic() < until:
                        root.update()
                        time.sleep(.01)
                    self.assertFalse(app.busy)
                    self.assertFalse(app.hot_running)
                    self.assertIsNone(app.monitor_timer)
                    bot.assert_not_called()
                    socket.assert_not_called()
                app.hot_running = True
                d = deals()[0][0]
                from reseller.history import FRESH, STALE
                d.availability = FRESH
                app.hot_rows = {d.fingerprint: (d, time.time() - 91)}
                app.deals = [d]
                app.render()
                root.update()
                root.after_cancel(app.poll_timer)
                app.poll()
                self.assertTrue(app.hot_rows)
                self.assertTrue(app.table.get_children())
                self.assertEqual(d.availability, STALE)
                app.hot_running = False
            finally:
                app.close()

    def test_removed_find_keeps_price_selection_and_durable_history(self):
        from reseller.app import Application
        from reseller.history import REMOVED
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        with tempfile.TemporaryDirectory() as folder:
            app = Application(root, demo=True, folder=Path(folder))
            try:
                d = replace(deals()[0][0], received=time.time(), offer_key='offer-one')
                payload = lambda rows: (rows, {}, 0, 1, 0, 0, [], 0)
                app.show_hot(payload([d]))
                app.table.selection_set('0')
                app.describe()
                price = d.sale_price
                other = replace(d, fingerprint='different', offer_key='offer-two', profit_day=d.profit_day + 100)
                app.show_hot(payload([other]))
                self.assertEqual(app.selected().fingerprint, d.fingerprint)
                self.assertIn(f'{price:,} gil/шт.', app.detail.get('1.0', 'end'))
                app.events.put(('hot_remove', ['offer-one']))
                root.after_cancel(app.poll_timer)
                app.poll()
                self.assertEqual(len(app.table.get_children()), 2)
                self.assertEqual(app.selected().availability, REMOVED)
                self.assertEqual(app.selected().sale_price, price)
                app.show_hot(payload([]))
                self.assertIn(REMOVED, app.detail.get('1.0', 'end'))
                app.copy_sale(app.selected())
                self.assertEqual(root.clipboard_get(), str(price))
                saved = {r.fingerprint: r for r in app.history.recent()}
                self.assertEqual(saved[d.fingerprint].sale_price, price)
                self.assertEqual(saved[d.fingerprint].availability, REMOVED)
                app.open_history()
                root.update()
            finally:
                app.close()


class HistoryTests(unittest.TestCase):
    def test_restart_preserves_original_quote_and_exact_removal(self):
        from reseller.history import History, REMOVED, STALE
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'history.sqlite3'
            history = History(path)
            d = replace(deals()[0][0], received=NOW, offer_key='first')
            changed = replace(d, fingerprint='second', offer_key='second', sale_price=d.sale_price + 300)
            history.add([d, changed])
            history.add([replace(d, sale_price=1)])
            history.remove(['first'])
            rows = {r.fingerprint: r for r in History(path).recent()}
            self.assertEqual(rows[d.fingerprint].sale_price, d.sale_price)
            self.assertEqual(rows[d.fingerprint].availability, REMOVED)
            self.assertEqual(rows['second'].availability, STALE)
            self.assertEqual(rows['second'].sale_price, changed.sale_price)

    def test_search_finds_older_quotes_beyond_visible_limit(self):
        from reseller.history import History
        with tempfile.TemporaryDirectory() as folder:
            history = History(Path(folder) / 'history.sqlite3')
            d = replace(deals()[0][0], name='Old Useful Item', received=NOW)
            history.add([d] + [replace(d, name='New Item', fingerprint=str(i), received=NOW + i + 1) for i in range(501)])
            self.assertEqual(len(history.recent()), 500)
            self.assertEqual(history.recent('old useful')[0].fingerprint, d.fingerprint)
