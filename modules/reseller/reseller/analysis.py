"""Conservative per-stack arbitrage using only destination-world executed sales."""
from collections import Counter
from dataclasses import dataclass, asdict
import hashlib
import json
import math
import statistics
import time
from urllib.parse import quote
from .market import WEEK


@dataclass
class Deal:
    item: int
    name: str
    category: str
    hq: bool
    buy_world: str
    buy_world_id: int
    sale_world: str
    sale_world_id: int
    quantity: int
    buy_price: int
    usual_price: int
    sale_price: int
    cost: int
    revenue: int
    profit: int
    roi: float
    discount: float
    units_day: float
    deals_week: int
    estimated_days: float
    competition: int
    upload_age: float
    fingerprint: str
    truncated: bool = False
    stress_profit: int = 0
    profit_day: float = 0
    capital_day: float = 0
    near_supply: int = 0
    supply_days: float = 0
    active_days: int = 0
    offer_key: str = ''
    signal: str = ''
    received: float = 0
    availability: str = ''

    @property
    def buy_url(self):
        return f'https://universalis.app/market/{self.item}?world={quote(self.buy_world)}'

    @property
    def sale_url(self):
        return f'https://universalis.app/market/{self.item}?world={quote(self.sale_world)}'

    def detail(self):
        quality = 'HQ' if self.hq else 'NQ'
        reference = (f'Статус объявления: {self.availability}\n'
                     f'Сохранённая цена продажи на {self.sale_world}: {self.sale_price:,} gil/шт.\n'
                     f'На момент находки: {time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(self.received))}\n'
                     'Цена и прибыль относятся к сохранённому расчёту. Перед выставлением проверьте рынок продажи.\n'
                     'Исчезновение объявления не подтверждает вашу покупку.\n\n') if self.availability else ''
        return (reference + (self.signal + '\n' if self.signal else '') + f'{self.name} #{self.item} · {quality}\n'
                f'Купить: {self.buy_world} · {self.quantity} шт. × {self.buy_price:,} gil\n'
                f'Обычная цена по продажам: {self.usual_price:,} gil/шт.\n'
                f'Предложенная продажа на {self.sale_world}: {self.sale_price:,} gil/шт.\n'
                f'Полный расход: −{self.cost:,}; выручка после налога: +{self.revenue:,}; прибыль: +{self.profit:,} gil\n'
                f'ROI: {self.roi:.1f}%; скидка к истории: {self.discount:.1f}%\n'
                f'Прибыль при дополнительном падении цены: +{self.stress_profit:,} gil; {self.profit_day:,.0f} gil/день по модели\n'
                f'Спрос на {self.sale_world}: {self.units_day:.1f} шт./день, {self.deals_week} сделок за 7 дней\n'
                f'Дней с продажами: {self.active_days}; близкие конкуренты: {self.near_supply} шт. / {self.supply_days:.1f} дней общего спроса\n'
                f'Конкуренты по цене не выше нашей: {self.competition} шт.; срок по модели: {self.estimated_days:.1f} дней\n'
                f'Возраст данных: {self.upload_age:.0f} мин.\n'
                + ('История достигла лимита: спрос — нижняя граница.\n' if self.truncated else '')
                + 'Каждая строка — отдельная покупка целого стака. Срок и прибыль — оценка, продажи не гарантированы.')


def weighted_median(sales):
    total = sum(s.quantity for s in sales)
    running = 0
    for sale in sorted(sales, key=lambda s: s.price):
        running += sale.quantity
        if running * 2 >= total:
            return sale.price
    raise ValueError('Нет продаж')


def rank(deals, settings):
    attr = {'Оборот прибыли': 'profit_day', 'Отдача капитала': 'capital_day', 'Прибыль стака': 'profit'}[settings['priority']]
    return sorted(deals, key=lambda d: (-getattr(d, attr), -d.stress_profit, d.estimated_days, d.fingerprint))


def basket(deals, settings):
    """Greedy reviewable purchase basket; one stack per item/quality, no global optimum claim."""
    selected, used, cost = [], set(), 0
    limit = None if settings['budget'] is None else settings['budget'] * (1 - settings['reserve_percent'] / 100)
    for deal in rank(deals, settings):
        key = (deal.item, deal.hq)
        if key in used or limit is not None and cost + deal.cost > limit:
            continue
        selected.append(deal)
        used.add(key)
        cost += deal.cost
        if len(selected) >= settings['alert_limit']:
            break
    return selected


def analyze(items, snapshots, settings, worlds, dc_worlds, now=None):
    now = time.time() if now is None else now
    target = next((i for i, name in worlds.items() if name == settings['sale_world']), None)
    if target not in dc_worlds:
        raise ValueError('Мир продажи не принадлежит выбранному DC')
    reasons, output = Counter(), []
    for item, snap in snapshots.items():
        if snap.errors:
            reasons['ошибка API / нет данных'] += 1
            continue
        if not all(0 <= now - t <= 300 for t in (snap.listing_fetched, snap.history_fetched)):
            reasons['старый снимок кэша'] += 1
            continue
        target_time = snap.uploads.get(target, 0)
        if not 0 <= now - target_time <= settings['max_upload_minutes'] * 60:
            reasons['старые цены мира продажи'] += 1
            continue
        qualities = (False, True) if settings['quality'] == 'Все' else (settings['quality'] == 'HQ',)
        for hq in qualities:
            sales = [s for s in snap.sales if s.hq == hq and now - WEEK <= s.timestamp <= now and s.price > 0 and s.quantity > 0]
            velocity = sum(s.quantity for s in sales) / 7
            if len(sales) < settings['min_deals_week'] or velocity < settings['min_units_day'] or not sales:
                reasons['недостаточный спрос'] += 1
                continue
            if now - max(s.timestamp for s in sales) > settings['max_sale_hours'] * 3600:
                reasons['нет недавних продаж'] += 1
                continue
            active_days = len({int((now - s.timestamp) // 86400) for s in sales})
            if active_days < settings['min_active_days']:
                reasons['усечённая история: не хватает дней для проверки стабильности' if snap.truncated else 'спрос сосредоточен в слишком малом числе дней'] += 1
                continue
            # Protect both against expensive tiny sales and against one oversized outlier.
            median = min(statistics.median(s.price for s in sales), weighted_median(sales))
            recent = [s.price for s in sales if now - s.timestamp <= 86400]
            if recent:
                median = min(median, statistics.median(recent))
            destination = [l for l in snap.listings if l.world == target and l.hq == hq]
            target_price = math.floor(median)
            if destination:
                target_price = min(target_price, min(l.price for l in destination) - settings['undercut'])
            if target_price <= 0:
                reasons['продажа по нулевой цене'] += 1
                continue
            ahead = sum(l.quantity for l in destination if l.price <= target_price)
            near = sum(l.quantity for l in destination if l.price <= target_price * (1 + settings['nearby_percent'] / 100))
            supply_days = near / velocity
            if supply_days > settings['max_supply_days']:
                reasons['слишком большой запас близких конкурентов'] += 1
                continue
            for listing in snap.listings:
                if listing.hq != hq or listing.world == target or listing.world not in dc_worlds:
                    continue
                # lastReviewTime is placement time, not observation time (Universalis Swagger).
                stamp = snap.uploads.get(listing.world, 0)
                age = (now - stamp) / 60
                if not 0 <= age <= settings['max_upload_minutes']:
                    reasons['старые цены покупки'] += 1
                    continue
                discount = (1 - listing.price / median) * 100
                raw = listing.price * listing.quantity
                cost = math.ceil(raw + math.floor(raw * settings['buy_tax'] / 100) + settings['travel'])
                gross = target_price * listing.quantity
                # One-gil conservative ceiling covers rounding after splitting a stack for resale.
                revenue = gross - math.ceil(gross * settings['sell_tax'] / 100)
                profit = revenue - cost
                roi = profit / cost * 100
                stress_gross = math.floor(target_price * (1 - settings['stress_drop'] / 100)) * listing.quantity
                stress_profit = stress_gross - math.ceil(stress_gross * settings['sell_tax'] / 100) - cost
                days = supply_days + listing.quantity / (velocity * settings['market_share'] / 100)
                if settings['budget'] is not None and cost > settings['budget']:
                    reasons['стак превышает бюджет'] += 1
                elif discount < settings['min_discount'] or profit <= 0 or profit < settings['min_profit'] or roi < settings['min_roi']:
                    reasons['мало прибыли / скидки'] += 1
                elif days > settings['max_days']:
                    reasons['слишком большой стак / срок продажи'] += 1
                elif stress_profit <= 0:
                    reasons['убыток при снижении цены'] += 1
                else:
                    identity = [item, listing.world, target, hq, listing.identity, listing.price, listing.quantity]
                    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
                    info = items.get(item, {'name': f'Item #{item}', 'category': 'Неизвестно'})
                    deal = Deal(item, info['name'], info['category'], hq, worlds[listing.world], listing.world,
                                       worlds[target], target, listing.quantity, listing.price, math.floor(median), target_price,
                                       cost, revenue, profit, roi, discount, velocity, len(sales), days, ahead,
                                       max(age, (now - target_time) / 60), key, snap.truncated)
                    deal.stress_profit, deal.near_supply, deal.supply_days, deal.active_days = stress_profit, near, supply_days, active_days
                    deal.offer_key = listing.offer_key
                    if deal.offer_key:
                        deal.fingerprint = hashlib.sha256(f'{deal.offer_key}:{target}'.encode()).hexdigest()
                    deal.profit_day = stress_profit / max(days, .25)
                    deal.capital_day = deal.profit_day / cost * 100
                    output.append(deal)
    # Do not duplicate a row when upstream repeats a listing.
    unique = {deal.fingerprint: deal for deal in output}
    return rank(list(unique.values()), settings), reasons


def telegram_text(deal):
    return ('Рай перекупа\n\n' + deal.detail() + '\n\nКупить: ' + deal.buy_url + '\nПродать: ' + deal.sale_url)
