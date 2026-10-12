"""Event-scoped stream, bounded processing, durable observations and alert budget."""
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
from pathlib import Path
import random
import sqlite3
import ssl
import threading
import time

from .analysis import analyze, rank
from .market import Market, positive, offer_keys, demo_market
from .net import ApiError, Cancelled, check, wait

URL = 'wss://universalis.app/api/ws'


@dataclass
class Offer:
    item: int
    world: int
    key: str
    base: str
    raw: dict
    received: float
    kind: str = ''
    ready: float = 0


def parse_event(message, allowed_worlds, items, settings, now):
    if not isinstance(message, dict):
        raise ValueError('Некорректное событие потока')
    event, item, world = message.get('event'), message.get('item'), message.get('world')
    if event == 'subscribe/error':
        raise ApiError('Universalis отклонил подписку потока')
    if event not in ('listings/add', 'listings/remove') or world not in allowed_worlds or item not in items:
        return event, []
    if settings['category'] != 'Все' and items[item]['category'] != settings['category']:
        return event, []
    raw_list = message.get('listings')
    if not isinstance(raw_list, list):
        raise ValueError('Нет объявлений в событии потока')
    output = []
    for raw in raw_list:
        if not isinstance(raw, dict) or not isinstance(raw.get('hq'), bool) or not positive(raw.get('pricePerUnit')) or not positive(raw.get('quantity')):
            continue
        if raw.get('onMannequin') or settings['quality'] != 'Все' and raw['hq'] != (settings['quality'] == 'HQ'):
            continue
        key, base = offer_keys(item, world, raw)
        output.append(Offer(item, world, key, base, raw, now))
    return event, output


class Pending:
    def __init__(self, capacity=512, ttl=90, now=time.time):
        self.rows, self.lock = OrderedDict(), threading.Lock()
        self.capacity, self.ttl, self.now = capacity, ttl, now
        self.dropped = self.expired = self.events = 0
        self.removed = OrderedDict()

    def push(self, event, offers):
        with self.lock:
            self.events += 1
            for offer in offers:
                if event == 'listings/remove':
                    self.rows.pop(offer.key, None)
                    self.removed[offer.key] = offer.received
                    if len(self.removed) > self.capacity * 2:
                        self.removed.popitem(last=False)
                    continue
                # Deltas merge; repeated frames never extend candidate age indefinitely.
                if offer.key not in self.rows:
                    self.rows[offer.key] = offer
                if len(self.rows) > self.capacity:
                    self.rows.popitem(last=False)
                    self.dropped += 1

    def active(self, offer):
        with self.lock:
            return self.removed.get(offer.key, -1) < offer.received

    def queued_keys(self):
        with self.lock:
            return set(self.rows)

    def defer(self, offers, seconds):
        with self.lock:
            for offer in offers:
                if self.removed.get(offer.key, -1) >= offer.received:
                    continue
                offer.ready = self.now() + seconds
                # A repeated wire frame must not replace accepted retry context with a seen-only duplicate.
                self.rows[offer.key] = offer
                if len(self.rows) > self.capacity:
                    self.rows.popitem(last=False)
                    self.dropped += 1

    def take(self, item_limit=10, item_ids=None):
        selected, ids = [], set(item_ids or ())
        with self.lock:
            for key, offer in list(self.rows.items()):
                if not 0 <= self.now() - offer.received <= self.ttl:
                    del self.rows[key]
                    self.expired += 1
                elif offer.ready <= self.now() and (offer.item in ids or len(ids) < item_limit):
                    ids.add(offer.item)
                    selected.append(self.rows.pop(key))
                    if len(selected) >= 100:
                        break
        return selected


class Observations:
    def __init__(self, path, now=time.time):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.now = now
        self.db.execute('CREATE TABLE IF NOT EXISTS seen (revision TEXT PRIMARY KEY, base TEXT, price INTEGER, seen REAL)')
        self.db.execute('CREATE INDEX IF NOT EXISTS base_idx ON seen(base, seen)')
        self.db.execute('CREATE INDEX IF NOT EXISTS seen_time ON seen(seen)')
        self.db.execute('DELETE FROM seen WHERE seen < ?', (now() - 30 * 86400,))
        self.db.commit()

    def accept(self, offers):
        output = []
        for offer in offers:
            if offer.kind:
                output.append(offer)  # Already accepted; bounded deferred delivery retry.
                continue
            if self.db.execute('SELECT 1 FROM seen WHERE revision=?', (offer.key,)).fetchone():
                continue
            previous = self.db.execute('SELECT price FROM seen WHERE base=? ORDER BY seen DESC,rowid DESC LIMIT 1', (offer.base,)).fetchone()
            # A new quantity at an equal price is a changed version, not a new game posting.
            identity = str(offer.raw.get('listingID') or '')
            strong = bool(identity and not identity.startswith('dirty:'))
            offer.kind = ('Впервые замечена редакция · ID неоднозначен' if not strong
                          else 'Цена снизилась' if previous and offer.raw['pricePerUnit'] < previous[0]
                          else 'Изменился стак' if previous else 'Впервые замечено в потоке')
            self.db.execute('INSERT OR IGNORE INTO seen VALUES(?,?,?,?)', (offer.key, offer.base, offer.raw['pricePerUnit'], self.now()))
            if not strong or not previous or offer.raw['pricePerUnit'] <= previous[0]:
                output.append(offer)
        self.db.execute('DELETE FROM seen WHERE revision IN (SELECT revision FROM seen ORDER BY seen DESC LIMIT -1 OFFSET 100000)')
        self.db.commit()
        return output

    def baseline(self, snapshots, exclude=()):
        for snap in snapshots.values():
            for listing in snap.listings:
                if listing.offer_key and listing.offer_key not in exclude:
                    self.db.execute('INSERT OR IGNORE INTO seen VALUES(?,?,?,?)', (listing.offer_key, listing.base_key, listing.price, self.now()))
        self.db.commit()

    def close(self):
        self.db.close()


class Ledger:
    """Persistent recommendation reservations, not actual player purchases."""
    def __init__(self, path, now=time.time):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db, self.now = sqlite3.connect(path), now
        self.db.execute('CREATE TABLE IF NOT EXISTS advice (scope TEXT, item INTEGER, hq INTEGER, source INTEGER, price INTEGER, cost INTEGER, quantity INTEGER, sent REAL)')
        self.db.execute('CREATE INDEX IF NOT EXISTS advice_scope ON advice(scope,item,hq,sent)')
        self.db.commit()

    @staticmethod
    def scope(s):
        return s['sale_world'] + ':' + s['chat_id']

    def spent(self, s):
        return self.db.execute('SELECT coalesce(sum(cost),0) FROM advice WHERE scope=?', (self.scope(s),)).fetchone()[0]

    def improved(self, d, s):
        row = self.db.execute('SELECT price FROM advice WHERE scope=? AND item=? AND hq=? AND source=? ORDER BY sent DESC LIMIT 1',
                              (self.scope(s), d.item, int(d.hq), d.buy_world_id)).fetchone()
        return bool(row and d.buy_price <= row[0] * (1 - s['hot_improvement'] / 100))

    def allowed(self, d, s):
        scope = self.scope(s)
        recent = self.db.execute('SELECT count(*) FROM advice WHERE scope=? AND sent>?', (scope, self.now() - 60)).fetchone()[0]
        if recent >= s['alert_limit']:
            return False, 'Лимит уведомлений за минуту'
        if s['budget'] is not None and self.spent(s) + d.cost > s['budget'] * (1 - s['reserve_percent'] / 100):
            return False, 'Лимит стоимости рекомендаций исчерпан — сбросьте его после проверки своих покупок'
        qty = self.db.execute('SELECT coalesce(sum(quantity),0) FROM advice WHERE scope=? AND item=? AND hq=?', (scope, d.item, int(d.hq))).fetchone()[0]
        available = max(0, (s['max_days'] - d.supply_days) * d.units_day * s['market_share'] / 100)
        if qty + d.quantity > available:
            return False, 'Суммарно рекомендованный объём товара превышает долю спроса'
        return True, ''

    def record(self, d, s):
        try:
            self.db.execute('INSERT INTO advice VALUES(?,?,?,?,?,?,?,?)',
                            (self.scope(s), d.item, int(d.hq), d.buy_world_id, d.buy_price, d.cost, d.quantity, self.now()))
            self.db.commit()
        except sqlite3.Error:
            raise ApiError('Telegram доставлен, но лимит рекомендаций не записан. Поток остановлен; проверьте папку настроек.') from None

    def slot_delay(self, s):
        stamps = [row[0] for row in self.db.execute('SELECT sent FROM advice WHERE scope=? AND sent>? ORDER BY sent', (self.scope(s), self.now() - 60))]
        return max(.5, stamps[0] + 60 - self.now() + .1) if len(stamps) >= s['alert_limit'] else 1

    def reset(self, s):
        # Keep recent send timestamps for sliding-minute rate limit despite budget reset.
        self.db.execute('UPDATE advice SET cost=0,quantity=0 WHERE scope=?', (self.scope(s),))
        self.db.commit()

    def close(self):
        self.db.close()


class Stream:
    def __init__(self, worlds, items, settings, pending, status=lambda s: None, removed=lambda keys: None):
        self.worlds, self.items, self.settings, self.pending, self.status = worlds, items, settings, pending, status
        self.removed = removed

    def run(self, cancel):
        try:
            import bson
            import websocket
        except ImportError:
            self.status('Поток не запущен: откройте start.bat для установки зависимостей.')
            cancel.set()
            return
        attempt = 0
        while not cancel.is_set():
            socket = None
            connected_at = time.monotonic()
            try:
                self.status('Подключение к потоку Universalis…')
                socket = websocket.create_connection(URL, timeout=10, sslopt={'cert_reqs': ssl.CERT_REQUIRED, 'check_hostname': True},
                                                     header=['User-Agent: FFXIVResellerParadise/0.2'], enable_multithread=True)
                for world in self.worlds:
                    for event in ('listings/add', 'listings/remove'):
                        socket.send_binary(bson.encode({'event': 'subscribe', 'channel': f'{event}{{world={world}}}'}))
                socket.settimeout(1)
                self.status('Поток подключён. Ожидание обновлений миров покупки…')
                ping_at = time.monotonic()
                while not cancel.is_set():
                    try:
                        opcode, frame = socket.recv_data(control_frame=True)
                    except websocket.WebSocketTimeoutException:
                        if time.monotonic() - ping_at > 20:
                            socket.ping()
                            ping_at = time.monotonic()
                        continue
                    if opcode == websocket.ABNF.OPCODE_CLOSE:
                        raise ApiError('Соединение закрыто сервером')
                    if opcode != websocket.ABNF.OPCODE_BINARY:
                        continue
                    if len(frame) > 4 * 1024 * 1024:
                        raise ApiError('Событие потока слишком большое')
                    try:
                        event, offers = parse_event(bson.decode(frame), self.worlds, self.items, self.settings, time.time())
                    except ValueError:
                        self.status('Пропущено некорректное событие потока.')
                        continue
                    self.pending.push(event, offers)
                    if event == 'listings/remove' and offers:
                        self.removed([o.key for o in offers])
            except Exception:
                if cancel.is_set():
                    break
                if time.monotonic() - connected_at > 30:
                    attempt = 0
                attempt += 1
                delay = min(60, 2 ** min(attempt, 6)) + random.uniform(0, 1)
                self.status(f'Поток отключён: переподключение через {delay:.0f} сек. Во время разрыва события могут быть пропущены.')
                if cancel.wait(delay):
                    break
            finally:
                if socket is not None:
                    try:
                        socket.close(timeout=1)
                    except Exception:
                        pass


class HotMonitor:
    def __init__(self, scanner, folder, notifier, now=time.time, market=None):
        self.scanner, self.folder, self.notifier, self.now = scanner, Path(folder), notifier, now
        self.market = market or Market(scanner.cache, now=now)

    def evaluate(self, offers, s, cancel, store, pending=None):
        world, dc = self.scanner.world(s['sale_world'])
        active = [o for o in offers if 0 <= self.now() - o.received <= 90 and (pending is None or pending.active(o))]
        if not active:
            return [], {}
        keys = {o.key: o for o in active}
        ids = sorted({o.item for o in active})
        current = self.market.detailed(dc['name'], world, ids, True, cancel=cancel, reuse_history=True)
        # Event publication can precede DB/cache update; give missing revisions one bounded retry.
        found = {l.offer_key for snap in current.values() for l in snap.listings}
        if set(keys) - found:
            wait(1, cancel)
            current = self.market.detailed(dc['name'], world, ids, True, cancel=cancel, reuse_history=True)
        found = {l.offer_key for snap in current.values() for l in snap.listings}
        if pending is not None:
            # Missing event versions can reflect the publication-before-write race or temporary API failure.
            pending.defer([o for o in active if (o.key not in found or current.get(o.item) is None or current[o.item].errors) and self.now() - o.received < 85], 5)
        result, reasons = analyze(self.scanner.items, current, s, {int(k): v for k, v in self.scanner.regions['worlds'].items()}, dc['worlds'], self.now())
        output = []
        for d in result:
            if d.offer_key in keys and 0 <= self.now() - keys[d.offer_key].received <= 90 and (pending is None or pending.active(keys[d.offer_key])):
                d.signal = keys[d.offer_key].kind + ' · обновление Universalis; время выставления в игре неизвестно'
                d.received = keys[d.offer_key].received
                output.append(d)
        # Old profitable rows in a REST response are only a baseline, never event candidates.
        store.baseline(current, pending.queued_keys() if pending is not None else ())
        return rank(output, s), reasons

    def run(self, s, cancel, emit, bot=None):
        if not self.scanner.demo:
            try:
                import bson
                import websocket
            except ImportError:
                raise ApiError('Для потока нужны зависимости: закройте программу и запустите start.bat.') from None
        store = Observations(self.folder / 'hot-seen.sqlite3', self.now)
        ledger = Ledger(self.folder / 'hot-advice.sqlite3', self.now)
        pending = Pending(now=self.now)
        receiver = None
        try:
            world, dc = self.scanner.world(s['sale_world'])
            items = self.scanner.items
            if s['mode'] == 'watch':
                from .scanner import watch_ids
                ids = watch_ids(s['watch_ids'], items)
                items = {i: items[i] for i in ids}
            if self.scanner.demo:
                # Exercise economics/UI without socket/Telegram. Existing demo snapshot only.
                data = demo_market(sorted(items)[:30], world, dc['worlds'])
                demo_deals, reasons = analyze(items, data, s, {int(k): v for k, v in self.scanner.regions['worlds'].items()}, dc['worlds'])
                for d in demo_deals:
                    d.signal = 'DEMO · искусственное событие'
                emit('hot', (demo_deals, reasons, 0, 0, 0, 0, [], 0))
                cancel.wait()
                return 'DEMO-поток остановлен'
            receiver = threading.Thread(target=Stream([w for w in dc['worlds'] if w != world], items, s, pending,
                                                       lambda msg: emit('hot_connection', msg), lambda keys: emit('hot_remove', keys)).run, args=(cancel,), daemon=True)
            receiver.start()
            total_sent = checked_count = 0
            while not cancel.is_set():
                offers = pending.take()
                if not offers:
                    if cancel.wait(.25):
                        break
                    continue
                # Brief batching and server-write allowance; newer deltas merge without losing old ones.
                wait(.75, cancel)
                extra = pending.take(item_limit=0, item_ids={o.item for o in offers})
                offers.extend(extra)
                fresh = store.accept(offers)
                if not fresh:
                    continue
                checked_count += len(fresh)
                deals, reasons = self.evaluate(fresh, s, cancel, store, pending)
                errors = []
                chosen = []
                if s['notify'] and bot is not None:
                    for d in deals:
                        check(cancel)
                        improved = ledger.improved(d, s)
                        if not improved and not self.notifier.eligible([d], s):
                            continue
                        allowed, reason = ledger.allowed(d, s)
                        if not allowed:
                            errors.append(reason)
                            if reason == 'Лимит уведомлений за минуту':
                                pending.defer([o for o in fresh if o.key == d.offer_key], ledger.slot_delay(s))
                            continue
                        # Target this exact version again just before delivery; history stays bounded and cached.
                        matching = [o for o in fresh if o.key == d.offer_key]
                        confirmed, _ = self.evaluate(matching, s, cancel, store, pending)
                        if not confirmed:
                            continue
                        d = confirmed[0]
                        allowed, reason = ledger.allowed(d, s)
                        if not allowed:
                            errors.append(reason)
                            continue
                        sent, delivery_errors = self.notifier.dispatch([d], s, bot, cancel, bypass_group=improved,
                                                                       on_delivered=lambda deal: ledger.record(deal, s))
                        if sent:
                            total_sent += sent
                            chosen.append(d)
                        errors += delivery_errors
                        if delivery_errors:
                            if any('HTTP 429' in message for message in delivery_errors):
                                pending.defer([o for o in fresh if o.key in {x.offer_key for x in deals}], 15)
                            else:
                                # Preserve the unattempted rest of the batch, never automatically repeat ambiguous delivery.
                                pending.defer([o for o in fresh if o.key != d.offer_key and o.key in {x.offer_key for x in deals}], 15)
                            break
                emit('hot', (deals, reasons, total_sent, pending.events, pending.dropped, pending.expired, errors, ledger.spent(s)))
            return 'Горячий мониторинг остановлен'
        finally:
            cancel.set()
            if receiver is not None:
                receiver.join(timeout=2)
            store.close()
            ledger.close()
