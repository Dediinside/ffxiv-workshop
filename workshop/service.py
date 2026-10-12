"""Unified background jobs; shared request limiter, prices and craft engine."""
from collections import Counter
from dataclasses import asdict, is_dataclass, replace
import json
import math
from pathlib import Path
import threading
import time
import uuid

from leveplanner.catalog import load as load_leves, available, refresh as refresh_leves
from leveplanner.market import Market as LeveMarket
from leveplanner.net import JsonCache as LeveCache
from leveplanner.planner import Settings, market_ids, recommend
from leveplanner.route import build_route, route_market_ids
from marketscout.catalog import load as load_market, refresh as refresh_market
from marketscout.analysis import analyze as analyze_market
from marketscout.market import Market as ScoutMarket
from marketscout.net import JsonCache as ScoutCache
from reseller.catalog import load_catalog, load_worlds
from reseller.scanner import Scanner
from reseller.net import Cache, Http, ApiError, Cancelled
from reseller.storage import defaults, normalize, load_token, save_token
from reseller.telegram import Notifier, Telegram
from reseller.history import History, FRESH, STALE, REMOVED
from reseller.hot import HotMonitor, Ledger
from reseller.analysis import basket, analyze
from reseller.market import Market as ResellerMarket

from .profile import Profile

ROOT = Path(__file__).resolve().parent.parent


def serial(value):
    if is_dataclass(value):
        return serial(asdict(value))
    if isinstance(value, dict):
        return {str(k): serial(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serial(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def integer(value, default, minimum=0, maximum=100000000):
    if value in ('', None):
        return default
    if isinstance(value, bool):
        raise ValueError('Ожидалось число')
    number = float(str(value).replace(' ', '').replace(',', '.'))
    if not math.isfinite(number) or number != int(number) or not minimum <= number <= maximum:
        raise ValueError(f'Допустимо целое число {minimum}–{maximum}')
    return int(number)


def craft_text(crafts, data):
    pre, final = Counter(), Counter()
    dependencies = {i for c in crafts for i in c['recipe']['ingredients']}
    for c in crafts:
        (pre if c['item'] in dependencies else final)[c['item']] += c['produced']
    names = lambda rows: '\n'.join(f"{q}x {data['items'][i]['name']}" for i, q in rows.items())
    return dict(pre='Pre crafts :\n' + names(pre) if pre else '', final='Items :\n' + names(final) if final else '',
                note=('Это фактически выбранные крафты. ' if crafts else 'Крафт в выбранном плане отсутствует: предметы покупаются готовыми или берутся из остатков. ')
                     + 'Формат по имени не передаёт HQ/NQ и recipe ID; проверьте рецепт в Artisan. Выполняйте маршрут по этапам.')


class Service:
    def __init__(self, folder, demo=False):
        self.folder, self.demo = Path(folder), demo
        self.profile = Profile(folder, demo)
        self.http = Http(rate=5)
        self.tasks, self.lock = {}, threading.RLock()
        self.load_data()
        self.scanner = Scanner(self.reseller_items, self.regions, Cache(self.folder / 'cache' / 'reseller', self.http), demo)
        self.notifier = Notifier(self.folder / 'alerts.json')
        self.history = History(self.folder / 'hot-finds.sqlite3')
        self.last_character = None

    def load_data(self):
        self.catalog_warnings = []
        def safe_load(loader, name, module):
            try:
                return loader(self.folder / name)
            except FileNotFoundError:
                pass
            except (OSError, ValueError, KeyError, TypeError):
                self.catalog_warnings.append(f'Каталог {module} не прочитан; используется встроенный снимок.')
            return loader(ROOT / 'modules' / module / 'data/catalog.json')
        self.leves = safe_load(load_leves, 'catalog-leves.json', 'leve')
        self.market_data = safe_load(load_market, 'catalog-market.json', 'market')
        if self.leves['metadata']['commit'] == self.market_data['metadata']['commit']:
            self.leves['items'] = {**self.market_data['items'], **self.leves['items']}
            recipes = {r['id']:r for r in self.market_data['recipes']}
            recipes.update({r['id']:r for r in self.leves['recipes']})
            self.leves['recipes'] = list(recipes.values())
        else:
            self.catalog_warnings.append('Каталоги разных версий: рецепты производства не объединяются с цепочками leves.')
        self.regions = load_worlds(self.folder)
        self.reseller_items = {int(i): r for i, r in load_catalog(self.folder)['items'].items()}

    def metadata(self):
        profile = self.profile.get()
        if profile['bridge_enabled'] and not self.demo:
            try:
                raw = json.loads((self.folder / 'character.json').read_text(encoding='utf-8'))
                if raw.get('updated_at') != self.last_character:
                    profile = self.profile.character(raw)
                    self.last_character = raw['updated_at']
            except (OSError, ValueError, TypeError):
                pass
        return dict(version='0.1.0', demo=self.demo, profile=profile, warning=' '.join([self.profile.warning, *self.catalog_warnings]).strip(),
                    worlds=sorted(self.regions['worlds'].values()), dcs=[d['name'] for d in self.regions['dcs']],
                    leve_cap=self.leves['level_cap'], recipes=len(self.market_data['recipes']), leves=len(self.leves['leves']),
                    items=len(self.reseller_items), token_saved=(self.folder / 'telegram-secret.json').exists(),
                    bridge_path=str(self.folder / 'character.json'), catalog_date=self.leves['metadata']['upstream_date'][:10])

    def start(self, kind, options):
        if kind not in ('leve', 'route', 'market', 'reseller', 'monitor', 'hot', 'catalog'):
            raise ValueError('Неизвестный расчёт')
        with self.lock:
            conflict = ('hot', 'reseller', 'monitor') if kind in ('hot', 'reseller', 'monitor') else (kind, 'catalog')
            if any(t['status'] == 'running' and (t['kind'] in conflict or kind == 'catalog') for t in self.tasks.values()):
                raise ValueError('Сначала остановите текущий расчёт этого режима')
            ident = uuid.uuid4().hex
            task = dict(id=ident, kind=kind, status='running', progress='Подготовка расчёта…', result=None, cancel=threading.Event(), error='', started=time.time())
            self.tasks[ident] = task
            # Bound finished task memory without touching the durable find history.
            finished = [i for i, t in self.tasks.items() if t['status'] != 'running']
            for old in finished[:-20]:
                self.tasks.pop(old, None)
        profile = self.profile.get()
        def progress(text):
            with self.lock:
                task['progress'] = str(text)
        def work():
            try:
                result = getattr(self, kind)(options, profile, task['cancel'], progress, task)
                with self.lock:
                    if result is not None:
                        task['result'] = serial(result)
                    task['status'] = 'cancelled' if task['cancel'].is_set() else 'done'
                    task['progress'] = 'Остановлено' if task['cancel'].is_set() else 'Расчёт завершён'
            except (Cancelled, InterruptedError):
                task['status'], task['progress'] = 'cancelled', 'Остановлено'
            except (ValueError, ApiError) as exc:
                if task['cancel'].is_set():
                    task['status'], task['progress'] = 'cancelled', 'Остановлено'
                else:
                    task['status'], task['error'] = 'error', str(exc)
            except Exception:
                if task['cancel'].is_set():
                    task['status'], task['progress'] = 'cancelled', 'Остановлено'
                else:
                    task['status'], task['error'] = 'error', 'Не удалось обработать данные. Проверьте параметры, сеть и каталог.'
        threading.Thread(target=work, daemon=True).start()
        return self.snapshot(ident)

    def snapshot(self, ident):
        with self.lock:
            if ident not in self.tasks:
                raise ValueError('Расчёт не найден')
            task = self.tasks[ident]
            if task['kind'] == 'market' and task.get('result'):
                from marketscout.analysis import next_window, schedule_text
                for row in task['result'].get('rows', []):
                    source = row.get('_source')
                    if source:
                        window, text = next_window(source), schedule_text(source)
                        old = row.get('windows_text', '')
                        row['window_wait'] = window['wait'] if window else None
                        if old:
                            row['details'] = row['details'].replace(old, text)
                        row['windows_text'] = text
            return serial({k: v for k, v in self.tasks[ident].items() if k != 'cancel'})

    def cancel(self, ident):
        with self.lock:
            self.tasks[ident]['cancel'].set()

    def close(self):
        for task in list(self.tasks.values()):
            task['cancel'].set()

    def fetch_bytes(self, url, cancel):
        return json.dumps(self.http.get(url, cancel), ensure_ascii=False).encode()

    def leve_settings(self, options, profile):
        job = options.get('job', 'Carpenter')
        if job not in profile['professions'] or job in ('Miner', 'Botanist', 'Fisher'):
            raise ValueError('Выберите ремесленную профессию')
        level = integer(options.get('level'), profile['professions'][job]['level'], 1, self.leves['level_cap'] - 1)
        budget = integer(options.get('budget'), None, 0, 10**12)
        return Settings(job, level, integer(options.get('xp'), 0), budget if budget is not None else math.inf,
                        integer(options.get('allowances'), 100, 0, 100), float(options.get('tax', profile['market']['buy_tax'])),
                        bool(options.get('craft_hq', profile['professions'][job]['hq'])), integer(options.get('travel'), profile['market']['travel']), profile['professions'],
                        craft_per=profile['market']['craft_per'],
                        batch_overhead=sum(profile['market'][key] for key in ('food','medicine','repair','other')),
                        consumables=tuple(profile['market']['consumables']))

    def leve_prices(self, settings, ids, options, profile, cancel, progress):
        if self.demo:
            from .leve_details import demo_prices
            return demo_prices(self.leves, ids), ['DEMO: искусственные цены'], {}
        cache = LeveCache(self.folder / 'cache' / 'leve', fetch=lambda url: self.fetch_bytes(url, cancel))
        market = LeveMarket(cache)
        prices = market.fetch(options.get('scope', profile['purchase_scope']), ids, bool(options.get('fresh')), progress)
        old = [i for i, stamp in market.uploads.items() if not stamp or time.time() - stamp / 1000 > 86400]
        if old:
            market.warnings.append(f'У {len(old)} предметов обновление Universalis неизвестно или старше суток. Проверьте предложения в игре.')
        return prices, market.warnings, market.uploads

    def plan_row(self, plan, settings, index, uploads=None, scope='Рынок выбранного мира/DC'):
        from .leve_details import describe
        row = serial(plan)
        row.update(id=index, name=plan.leve['name'], level=plan.leve['level'], cost=plan.cost, reward=plan.reward,
                   balance=plan.balance, feasible=plan.feasible, allowances=plan.allowances, turnins=plan.progress.turnins,
                   accepts=plan.progress.accepts, details=describe(plan, self.leves, settings, scope, uploads or {}, self.demo),
                   craft_list=craft_text(plan.crafts, self.leves))
        if plan.notes:
            row['details'] += '\n\n' + '\n'.join(plan.notes)
        row['craft_list']['name'] = f'{settings.job} · {plan.leve["name"]}'
        return row

    def leve(self, options, profile, cancel, progress, task):
        settings = replace(self.leve_settings(options, profile), cancelled=cancel.is_set)
        leves = available(self.leves, settings.job, settings.level, bool(options.get('all_levels')))
        ids = market_ids(self.leves, leves, settings)
        prices, warnings, uploads = self.leve_prices(settings, ids, options, profile, cancel, progress)
        progress('Сравниваю покупку, крафт и цепочки заготовок…')
        if cancel.is_set():
            raise InterruptedError()
        plans = recommend(self.leves, leves, prices, settings)
        return dict(rows=[self.plan_row(p, settings, i, uploads, options.get('scope', profile['purchase_scope'])) for i, p in enumerate(plans)], warnings=warnings,
                    summary=dict(target=min(self.leves['level_cap'], (settings.level // 5 + 1) * 5), plans=len(plans)))

    def route(self, options, profile, cancel, progress, task):
        settings = replace(self.leve_settings(options, profile), cancelled=cancel.is_set)
        target = integer(options.get('target'), 50, settings.level + 1, self.leves['level_cap'])
        ids = route_market_ids(self.leves, settings, target)
        prices, warnings, uploads = self.leve_prices(settings, ids, options, profile, cancel, progress)
        result = build_route(self.leves, settings, target, prices, None if math.isinf(settings.budget) else settings.budget,
                             options.get('priority', 'cost'), options.get('method', 'Все'), options.get('quality', 'Все'),
                             progress, cancel.is_set)
        rows = []
        for i, step in enumerate(result.steps):
            row = self.plan_row(step.plan, replace(settings, level=step.start_level, current_xp=step.start_xp), i, uploads, options.get('scope', profile['purchase_scope']))
            row['stage'] = f'{step.start_level} → {step.plan.progress.end_level}'
            rows.append(row)
        crafts = [c for step in result.steps for c in step.plan.crafts]
        crafted = craft_text(crafts, self.leves)
        crafted['name'] = f'{settings.job} {settings.level}-{target}'
        return dict(rows=rows, warnings=warnings + result.issues,
                    summary=dict(target=target, end_level=result.end_level, complete=result.complete, needed_xp=result.needed_xp,
                                 gained_xp=result.gained_xp, cost=result.cost, spent=result.spent, reward=result.reward,
                                 allowances=result.allowances, stages=len(result.steps)), craft_list=crafted)

    def market(self, options, profile, cancel, progress, task):
        settings = {**profile['market'], **options, 'professions': profile['professions'], 'recursive_craft': True,
                    'sale_world': profile['sale_world'], 'purchase_scope': profile['purchase_scope']}
        cache = ScoutCache(self.folder / 'cache' / 'market', fetch=lambda url: self.fetch_bytes(url, cancel))
        rows, summary = analyze_market(self.market_data, settings, ScoutMarket(cache), self.demo, bool(options.get('fresh')), progress, cancel)
        for i, row in enumerate(rows):
            row['id'] = i
            row['craft_list'] = craft_text(row.get('craft_steps', []), self.market_data)
        return dict(rows=serial(rows), summary=summary, warnings=summary['warnings'])

    @property
    def market_data(self):
        return self._market_data

    @market_data.setter
    def market_data(self, value):
        self._market_data = value

    def reseller_settings(self, options, profile):
        return normalize({**defaults(), **profile['reseller'], **options, **profile['telegram'], 'sale_world': profile['sale_world']})

    def bot(self, settings):
        if not settings['notify'] or self.demo:
            return None
        token, warning = load_token(self.folder)
        if warning or not token:
            raise ValueError('Сначала сохраните токен Telegram в настройках нового приложения')
        return Telegram(token)

    def reseller(self, options, profile, cancel, progress, task):
        settings = self.reseller_settings(options, profile)
        bot = self.bot(settings)
        result = self.scanner.scan(settings, bool(options.get('fresh')), progress, cancel)
        if bot:
            ledger = Ledger(self.folder / 'hot-advice.sqlite3')
            try:
                selected = basket([d for d in self.notifier.eligible(result.deals, settings) if ledger.allowed(d, settings)[0]], settings)
            finally:
                ledger.close()
            world, dc = self.scanner.world(settings['sale_world'])
            current = ResellerMarket(self.scanner.cache).detailed(dc['name'], world, sorted({d.item for d in selected}), True, cancel=cancel) if selected else {}
            checked, _ = analyze(self.reseller_items, current, settings, {int(k): v for k, v in self.regions['worlds'].items()}, dc['worlds'])
            keys = {d.fingerprint for d in selected}
            confirmed = basket([d for d in checked if d.fingerprint in keys], settings)
            sent, errors = 0, []
            ledger = Ledger(self.folder / 'hot-advice.sqlite3')
            try:
                for d in confirmed:
                    allowed, reason = ledger.allowed(d, settings)
                    if not allowed:
                        errors.append(reason)
                        continue
                    count, failures = self.notifier.dispatch([d], settings, bot, cancel, on_delivered=lambda deal:ledger.record(deal, settings))
                    sent += count
                    errors.extend(failures)
                    if failures:
                        break
            finally:
                ledger.close()
            result.basket = confirmed
            result.warnings.extend(errors)
        else:
            sent = 0
        chosen = {d.fingerprint for d in result.basket}
        return dict(rows=[{**serial(d), 'id': d.fingerprint, 'recommended':d.fingerprint in chosen, 'details': d.detail(), 'buy_url': d.buy_url, 'sale_url': d.sale_url} for d in result.deals],
                    warnings=result.warnings, summary=dict(found=len(result.deals), checked=result.detailed, sent=sent, reasons=dict(result.reasons)))

    def monitor(self, options, profile, cancel, progress, task):
        interval = integer(options.get('interval_minutes'), 5, 1, 1440)
        while not cancel.is_set():
            result = self.reseller(options, profile, cancel, progress, task)
            with self.lock:
                task['result'] = serial(result)
            progress(f'Цикл завершён. Следующая часть каталога через {interval} мин.')
            if cancel.wait(interval * 60):
                break
        return None

    def hot(self, options, profile, cancel, progress, task):
        settings = self.reseller_settings(options, profile)
        bot = self.bot(settings)
        rows = {}
        def emit(kind, value):
            if kind == 'hot_connection':
                progress(value)
            elif kind == 'hot_remove':
                self.history.remove(value)
                for d in rows.values():
                    if d.offer_key in value:
                        d.availability = REMOVED
                publish()
            elif kind == 'hot':
                deals, reasons, sent, events, dropped, expired, errors, spent = value
                for d in deals:
                    if d.fingerprint not in rows:
                        d.received = d.received or time.time()
                        d.availability = FRESH
                        rows[d.fingerprint] = d
                if deals:
                    self.history.add(deals)
                publish(dict(sent=sent, events=events, dropped=dropped, expired=expired, spent=spent), errors)
        def publish(summary=None, warnings=None):
            for d in rows.values():
                if d.availability == FRESH and time.time() - d.received >= 90:
                    d.availability = STALE
            with self.lock:
                previous = task.get('result') or {}
                task['result'] = dict(rows=[{**serial(d), 'id': d.fingerprint, 'details': d.detail(), 'buy_url': d.buy_url, 'sale_url': d.sale_url} for d in rows.values()],
                                      summary=summary or previous.get('summary', {}), warnings=warnings or previous.get('warnings', []))
        HotMonitor(self.scanner, self.folder, self.notifier).run(settings, cancel, emit, bot)
        return None

    def catalog(self, options, profile, cancel, progress, task):
        if self.demo:
            raise ValueError('Обновление каталога отключено в DEMO')
        refresh_leves(self.folder / 'catalog-leves.json', progress)
        refresh_market(self.folder / 'catalog-market.json', progress)
        self.load_data()
        return dict(rows=[], warnings=[], summary=dict(leves=len(self.leves['leves']), recipes=len(self.market_data['recipes'])))
