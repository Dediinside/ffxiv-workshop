"""Explainable market estimates, never forecasts of a guaranteed sale."""
from collections import Counter, defaultdict, deque
from datetime import datetime
import math
import statistics
import time

from .market import Snapshot, best_purchase, demo_market

ET_HOUR_SECONDS = 175.0
ET_DAY_SECONDS = 4200.0
HISTORY_DAYS = 7


def item_url(item):
    return f'https://universalis.app/market/{int(item)}'


def next_window(source, now=None):
    """Return earliest usable window; UTC epoch maps to ET, wraps midnight."""
    now = time.time() if now is None else now
    if source.get('kind') == 'always':
        return dict(wait=0.0, start=now, end=None, active=True)
    if source.get('kind') == 'unknown' or not source.get('windows'):
        return None
    base = math.floor(now / ET_DAY_SECONDS) * ET_DAY_SECONDS
    choices = []
    for window in source['windows']:
        hour, duration = window['start_hour'], window['duration_hours']
        if not 0 <= hour < 24 or not 0 < duration <= 24:
            continue
        for shift in (-1, 0, 1):
            start = base + shift * ET_DAY_SECONDS + hour * ET_HOUR_SECONDS
            end = start + duration * ET_HOUR_SECONDS
            if end > now:
                choices.append(dict(wait=max(0, start - now), start=start, end=end, active=start <= now))
    return min(choices, key=lambda w: (w['wait'], w['start'])) if choices else None


def hhmm(hours):
    minutes = round(hours * 60) % 1440
    return f'{minutes // 60:02d}:{minutes % 60:02d}'


def schedule_text(source, now=None):
    kind = source.get('kind', 'unknown')
    if kind == 'always':
        return 'Обычный узел: расписание не ограничено временем ET.'
    if not source.get('windows'):
        return 'Условия/время появления неизвестны; постоянная доступность не предполагается.'
    label = 'Временный (ephemeral)' if kind == 'ephemeral' else 'Узел по расписанию'
    windows = ', '.join(f"{hhmm(w['start_hour'])}–{hhmm(w['start_hour'] + w['duration_hours'])} ET"
                        for w in source['windows'])
    upcoming = next_window(source, now)
    if not upcoming:
        return f'{label}: {windows}; следующее окно неизвестно.'
    status = 'открыто сейчас' if upcoming['active'] else f"через {math.ceil(upcoming['wait'] / 60)} мин"
    local_start = datetime.fromtimestamp(upcoming['start']).strftime('%d.%m %H:%M:%S')
    local_end = datetime.fromtimestamp(upcoming['end']).strftime('%H:%M:%S')
    return f'{label}: {windows}; {status}. Местное время окна: {local_start}–{local_end}.'


def demand(snapshot, quality, now=None):
    now = time.time() if now is None else now
    hq = quality == 'HQ'
    sales = [s for s in snapshot.sales if s.hq == hq and now - HISTORY_DAYS * 86400 <= s.timestamp <= now
             and s.price > 0 and s.quantity > 0]
    listing = [l for l in snapshot.listings if l.hq == hq and l.price > 0 and l.quantity > 0]
    historical = statistics.median(s.price for s in sales) if sales else None
    current = min((l.price for l in listing), default=None)
    # A listing at 1 gil remains 1; zero is never an invented free item.
    price = min(historical, max(1, current - 1)) if historical is not None and current is not None else historical or current
    return dict(price=int(price) if price is not None else None, historical=historical,
                sales_day=sum(s.quantity for s in sales) / HISTORY_DAYS if sales else None,
                transactions_day=len(sales) / HISTORY_DAYS if sales else None,
                sales_count=len(sales), last_sale=max((s.timestamp for s in sales), default=None), listings_count=len(listing),
                competing_units=sum(l.quantity for l in listing if price is not None and l.price <= price),
                total_stock=sum(l.quantity for l in listing), truncated=snapshot.truncated)


def common_costs(settings, mode, crafts, units, profile):
    result = [(label, settings.get(key, 0)) for key, label in
              [('food', 'Еда (вручную/партия)'), ('medicine', 'Зелья/cordial (вручную/партия)'),
               ('repair', 'Ремонт (вручную/партия)'), ('travel', 'Дорога (вручную/партия)'),
               ('other', 'Прочее (вручную/партия)')]]
    if mode == 'craft':
        result.append(('Дополнительные затраты/крафт', settings.get('craft_per', 0) * crafts))
    else:
        result.append(('Дополнительные затраты/единицу сбора', profile.get('per_unit', 0) * units))
    return result


def estimate(candidate, quality, data, settings, sale_snapshot, purchase, now=None):
    now = time.time() if now is None else now
    item, job, mode = candidate['item'], candidate['job'], candidate['mode']
    profile = dict(settings['professions'][job])
    measured = settings.get('item_profiles', {}).get(str(item))
    if measured:
        profile.update(measured)
    recipe = candidate.get('recipe')
    source = candidate.get('source', {})
    crafts = math.ceil(settings['batch_size'] / recipe['yield']) if recipe else 0
    units = crafts * recipe['yield'] if recipe else settings['batch_size']
    row = dict(item_id=item, name=data['items'][item]['name'], job=job, level=candidate['level'], mode=mode,
               quality=quality, units=units, cost=None, revenue=None, profit=None, margin=None, hourly=None,
               window_wait=None, windows_text='', issues=[], warnings=[], inputs=[], details='',
               confidence='Низкая', recipe_id=recipe['id'] if recipe else None)
    if not recipe:
        row['_source'] = source
    stats = demand(sale_snapshot, quality, now)
    row.update({k: v for k, v in stats.items() if k not in ('truncated',)})
    reasons = []
    if stats['sales_count'] < settings.get('min_deals_week', 3):
        reasons.append(f"Сделок за неделю {stats['sales_count']} < {settings.get('min_deals_week', 3)}")
    if stats['sales_day'] is None or stats['sales_day'] < settings.get('min_units_day', 1):
        reasons.append('Недостаточно проданных единиц в день')
    if stats['last_sale'] is None or now-stats['last_sale'] > settings.get('max_sale_age_hours', 72)*3600:
        reasons.append('Нет свежих продаж в заданном периоде')
    row['liquid'] = not reasons
    row['demand_reasons'] = reasons
    row['warnings'].extend(sale_snapshot.warnings)
    row['warnings'].extend(candidate.get('conditions', []))
    row['warnings'].extend(reasons)
    row['issues'].extend(sale_snapshot.errors)
    if not stats['sales_count']:
        row['warnings'].append(f'Нет наблюдаемых продаж {quality} за 7 дней; спрос неизвестен.')
    if sale_snapshot.truncated:
        row['warnings'].append('История достигла лимита API: спрос показан как нижняя граница.')
    if not sale_snapshot.uploaded or now - sale_snapshot.uploaded > 86400:
        row['warnings'].append('Рыночное обновление неизвестно или старше суток.')
    if stats['price'] is None:
        row['issues'].append('Нет ни истории цен, ни активных предложений этого качества.')
    elif stats['historical'] is None:
        row['warnings'].append('Цена взята только из предложений; реальные продажи не подтверждены.')
    if stats['sales_day'] is not None and units > stats['sales_day'] * 0.25:
        row['warnings'].append('Партия превышает 25% наблюдаемых суточных продаж мира. Это ориентир риска, не прогноз.')
    row['uploaded'] = sale_snapshot.uploaded
    row['fetched'] = sale_snapshot.fetched
    required = Counter()
    if recipe:
        for ing, qty in recipe['ingredients'].items():
            required[(ing, False)] += qty * crafts
        if recipe.get('stars'):
            row['warnings'].append(f"Рецепт {recipe['stars']}★: проверьте экипировку.")
        if recipe.get('craftsmanship') or recipe.get('control'):
            row['warnings'].append(f"Требования: craftsmanship {recipe.get('craftsmanship', 0)}, control {recipe.get('control', 0)}.")
        if quality == 'HQ':
            row['warnings'].append('Расчёт предполагает гарантированный HQ из NQ-материалов; неудачи не учтены.')
    for entry in settings.get('consumables', []):
        if entry['mode'] in ('all', mode):
            required[(entry['item'], entry['hq'])] += entry['quantity']
    market_cost = 0
    if recipe and settings.get('recursive_craft'):
        from workshop.crafting import CraftEngine
        engine = CraftEngine(data, settings['professions'], settings['buy_tax'])
        try:
            needs = {(item, quality == 'HQ'): units}
            for entry in settings.get('consumables', []):
                if entry['mode'] in ('all', mode):
                    key = (entry['item'], entry['hq'])
                    needs[key] = needs.get(key, 0) + entry['quantity']
            quoted = engine.quote(needs, {i: s.listings for i, s in purchase.items() if not s.errors},
                                  force={(item, quality == 'HQ'): recipe['id']})
            market_cost = quoted.cost
            crafts = sum(c['count'] for c in quoted.crafts)
            row['craft_steps'] = quoted.crafts
            row['warnings'].extend(quoted.warnings)
            for buy in quoted.shopping:
                ing, bought = buy['item'], buy['purchase']
                row['inputs'].append(dict(item=ing, name=data['items'][ing]['name'], required=buy['required'],
                                          quality='HQ' if any(l.hq for l in bought.stacks) else 'NQ',
                                          purchased=bought.quantity, cost=bought.cost, stacks=bought.stacks, reason=''))
        except ValueError as exc:
            row['issues'].append(str(exc))
            row['inputs'].append(dict(item=item, name=row['name'], required=units, quality=quality,
                                      purchased=None, cost=None, stacks=[], reason=str(exc)))
        required.clear()
    for (ing, hq), qty in sorted(required.items()):
        title = data['items'].get(ing, {}).get('name', f'Предмет #{ing}')
        snapshot = purchase.get(ing, Snapshot())
        stock = [l for l in snapshot.listings if l.hq == hq]
        available = sum(l.quantity for l in stock)
        reason = ''
        bought = None
        if snapshot.errors:
            reason = f'API/данные: {"; ".join(snapshot.errors)}'
        elif not stock:
            reason = f'Нет предложений {"HQ" if hq else "NQ"}'
        elif available < qty:
            reason = f'Не хватает количества: нужно {qty}, доступно {available}'
        else:
            try:
                bought = best_purchase(stock, qty, settings['buy_tax'])
            except ValueError as exc:
                reason = str(exc)
        if not bought and not reason:
            reason = 'Закупка недоступна'
        if bought:
            market_cost += bought.cost
        else:
            row['issues'].append(f'{title} #{ing}: {reason}')
        row['warnings'].extend(snapshot.warnings)
        if stock and (not snapshot.uploaded or now - snapshot.uploaded > 86400):
            row['warnings'].append(f'{title}: обновление рынка неизвестно/старше суток.')
        row['inputs'].append(dict(item=ing, name=title, required=qty, quality='HQ' if hq else 'NQ',
                                  purchased=bought.quantity if bought else None,
                                  cost=bought.cost if bought else None,
                                  stacks=bought.stacks if bought else [], reason=reason))
    extra = common_costs(settings, mode, crafts, units, profile)
    if not recipe:
        row['windows_text'] = schedule_text(source, now)
        window = next_window(source, now)
        row['window_wait'] = window['wait'] if window else None
        row['warnings'].extend(source.get('conditions', []))
        if source.get('book'):
            row['warnings'].append('Требуется подтверждённая книга Folklore и доступ к региону.')
        if source.get('quest'):
            row['warnings'].append(f"Проверьте условие открытия (код в игровой таблице {source['quest']}).")
        rod = job == 'Fisher' and source.get('fishing_method') not in ('spear', 'spearfishing')
        bait = profile.get('bait_per_unit')
        if rod:
            if bait is None:
                row['issues'].append('Не задана средняя стоимость приманок на пойманную целевую рыбу (включая неудачные попытки).')
            else:
                extra.append(('Приманки с учётом неудачных попыток', units * bait))
        if source.get('kind') == 'unknown':
            row['warnings'].append('Условия появления не полностью известны; расписание и gil/час не рассчитаны.')
        if source.get('kind') in ('timed', 'ephemeral') and not profile.get('window_yield'):
            row['warnings'].append('Укажите урожай за окно: объём доступного сбора сейчас неизвестен.')
        elif source.get('kind') in ('timed', 'ephemeral'):
            row['warnings'].append(f"Личный урожай/окно: {profile['window_yield']} шт.; партия требует минимум {math.ceil(units / profile['window_yield'])} окон.")
    row['expenses'] = extra
    cost_known = not any(i['cost'] is None for i in row['inputs']) and not (not recipe and
        job == 'Fisher' and source.get('fishing_method') not in ('spear', 'spearfishing') and profile.get('bait_per_unit') is None)
    if cost_known:
        row['cost'] = market_cost + sum(amount for _, amount in extra)
    if row['price'] is not None:
        gross = units * row['price']
        row['sale_tax'] = math.ceil(gross * settings['sell_tax'] / 100)
        row['revenue'] = gross - row['sale_tax']
        if row['cost'] is not None:
            row['profit'] = row['revenue'] - row['cost']
            row['margin'] = row['profit'] / row['cost'] * 100 if row['cost'] else None
    if settings['budget'] is not None and row['cost'] is not None and row['cost'] > settings['budget']:
        row['issues'].append('Не хватает бюджета на полную закупку партии.')
    rate = profile.get('rate', 0)
    if not recipe and rate > 0 and row['profit'] is not None:
        if source.get('kind') == 'always' and job != 'Fisher':
            row['hourly'] = row['profit'] / (units / rate)
            row['warnings'].append('gil/час активного сбора по вашему профилю; дорога, ожидание и время продажи в час не включены.')
        elif measured and source.get('kind') == 'ephemeral' and profile.get('window_yield'):
            row['hourly'] = row['profit'] / (units / rate)
            row['warnings'].append('gil/час активного сбора только внутри окна по вашему замеру; ожидание не включено.')
        # Timed nodes exhaust: no invented continuous yield or hourly score.
    if stats['sales_count'] >= 20 and not row['warnings'] and not row['issues']:
        row['confidence'] = 'Средняя'
    elif not row['issues'] and stats['sales_count'] >= 5 and not sale_snapshot.truncated and sale_snapshot.uploaded and now-sale_snapshot.uploaded <= 86400:
        row['confidence'] = 'Средняя' if not sale_snapshot.warnings else 'Низкая'
    row['warnings'] = list(dict.fromkeys(row['warnings']))
    row['issues'] = list(dict.fromkeys(row['issues']))
    if not row['liquid'] or row['issues'] or source.get('kind') == 'unknown':
        row['confidence'] = 'Низкая'
    if measured and not recipe:
        row['warnings'].append('Замер сохранён для товара; применяйте его только к тому же месту и способу добычи.')
    row['status'] = ('Малоликвидный — сравнение' if not row['liquid'] else
                     'Есть ограничения' if row['issues'] else
                     'Убыточно' if row['profit'] is not None and row['profit'] <= 0 else 'Спрос подтверждён')
    row['details'] = describe(row, candidate, settings, now)
    if row.get('craft_steps'):
        row['details'] += '\n\nВыбранная цепочка изготовления (уже включена в стоимость):\n' + '\n'.join(
            f"{data['items'][c['item']]['name']}: {c['count']} крафтов × {c['recipe']['yield']} = {c['produced']} шт. · {c['recipe']['job']} Lv {c['recipe']['level']}"
            for c in row['craft_steps'])
    return row


def signed(value):
    return '—' if value is None else f'{value:+,.0f}'.replace(',', ' ')


def describe(row, candidate, settings, now=None):
    lines = [f"{row['name']} #{row['item_id']} · {row['job']} Lv {row['level']} · {row['quality']}",
             item_url(row['item_id']),
             f"Мир продажи: {settings['sale_world']}; закупка: {settings['purchase_scope']}",
             f"Расчёт выполнен: {datetime.fromtimestamp(now or time.time()).strftime('%d.%m.%Y %H:%M:%S')}",
             f"Партия: {row['units']} шт. · Цена единицы (оценка): {row['price'] if row['price'] is not None else '—'} gil",
             f"Затраты: {signed(-row['cost']) if row['cost'] is not None else '—'}; выручка после налога: {signed(row['revenue'])}; прибыль: {signed(row['profit'])}",
             f"Спрос за 7 дней: {row['sales_day']:.2f} ед./день, {row['transactions_day']:.2f} сделок/день; наблюдений {row['sales_count']}" if row['sales_day'] is not None else 'Спрос: нет наблюдаемых продаж за 7 дней.',
             f"Предложений: {row['listings_count']}; запас всего: {row['total_stock']} шт.; по цене не выше оценки: {row['competing_units']} шт.",
             f"Свежесть рынка: {datetime.fromtimestamp(row['uploaded']).strftime('%d.%m.%Y %H:%M')}" if row.get('uploaded') else 'Дата обновления рынка неизвестна.',
             'Цена: минимум медианы сделок и самого дешёвого предложения − 1 gil; если история отсутствует, только предложение.',
             '']
    if candidate.get('recipe'):
        r = candidate['recipe']
        lines.append(f"Рецепт #{r['id']}, выход {r['yield']}; крафтов {math.ceil(settings['batch_size']/r['yield'])}.")
    else:
        lines.extend([f"Место: {candidate.get('source',{}).get('location','неизвестно')}", row['windows_text']])
    lines.append('\nЗакупки целых стаков (ингредиенты, кристаллы, заданные расходники):')
    for entry in row['inputs']:
        lines.append(f"{entry['name']} #{entry['item']} {entry['quality']}: нужно {entry['required']}, купить {entry['purchased'] if entry['purchased'] is not None else '—'}, затраты {signed(-entry['cost']) if entry['cost'] is not None else '—'} gil. {entry['reason']}")
        lines.append(item_url(entry['item']))
        for stack in entry['stacks']:
            lines.append(f'  {stack.quantity} × {stack.price} gil · {stack.world}; проверка {datetime.fromtimestamp(stack.reviewed).strftime("%d.%m %H:%M") if stack.reviewed else "неизвестно"}')
    lines.append('\nДополнительные расходы партии; 0 = не используем/не учитываем:')
    lines.extend(f'{name}: {amount:g} gil' for name, amount in row['expenses'])
    lines.append(f"Налог покупки: {settings['buy_tax']:g}%; продажи: {settings['sell_tax']:g}%.")
    if row['hourly'] is not None:
        lines.append(f"Активный сбор: {row['hourly']:,.0f} gil/час по вашим измерениям.")
    lines.extend('\nОграничение: ' + issue for issue in row['issues'])
    lines.extend('\nДопущение: ' + warning for warning in row['warnings'])
    lines.append('\nОценка денежного результата отдельной партии; время и собственные материалы не считаются бесплатными экономически. Срок продажи не прогнозируется.')
    return '\n'.join(lines)


def select_items(candidates, limit, search=''):
    buckets = defaultdict(dict)
    for c in candidates:
        buckets[(c['job'], c['mode'], c['level']//10)][c['item']] = c['item']
    queues = [deque(sorted(items)) for _, items in sorted(buckets.items())]
    selected, seen = [], set()
    while queues and len(selected) < limit:
        next_queues = []
        for q in queues:
            while q and q[0] in seen:
                q.popleft()
            if q and len(selected) < limit:
                item = q.popleft()
                seen.add(item)
                selected.append(item)
            if q:
                next_queues.append(q)
        queues = next_queues
    return selected


def select_finalists(candidates, items, velocities, limit):
    """Reserve demand-ranked turns for each job/source, avoiding gatherer starvation."""
    available = set(items)
    buckets = defaultdict(set)
    for candidate in candidates:
        if candidate['item'] in available:
            buckets[(candidate['job'], candidate['mode'])].add(candidate['item'])
    rank = lambda item: (-velocities.get(item, -1), item)
    groups = [(key, deque(sorted(values, key=rank))) for key, values in buckets.items()]
    groups.sort(key=lambda group: (rank(group[1][0]), group[0]))
    selected, seen = [], set()
    while groups and len(selected) < limit:
        remaining = []
        for key, queue in groups:
            while queue and queue[0] in seen:
                queue.popleft()
            if queue and len(selected) < limit:
                item = queue.popleft()
                selected.append(item)
                seen.add(item)
            if queue:
                remaining.append((key, queue))
        groups = remaining
    return selected


def analyze(data, settings, market, demo=False, force=False, progress=lambda s: None, cancel=None):
    from .catalog import eligible
    from .settings import normalize_settings
    settings = normalize_settings(settings)
    def stopped():
        if cancel is not None and cancel.is_set():
            raise InterruptedError('Анализ отменён')
    stopped()
    candidates = eligible(data, settings)
    if settings['analysis_mode'] != 'all':
        candidates = [c for c in candidates if c['mode'] == settings['analysis_mode']]
    if settings['analysis_job'] != 'all':
        candidates = [c for c in candidates if c['job'] == settings['analysis_job']]
    if settings['catalog_search']:
        candidates = [c for c in candidates if settings['catalog_search'].casefold() in data['items'][c['item']]['name'].casefold()]
    total_items = len({c['item'] for c in candidates})
    items = select_items(candidates, settings['scan_limit'])
    progress(f'Доступно {total_items} товаров; первичный просмотр {len(items)} (лимит {settings["scan_limit"]})…')
    warnings = []
    if demo:
        velocities = {i: (i % 17) + 1 for i in items}
    else:
        velocities = market.aggregate(settings['sale_world'], items, force=force, progress=progress, cancel=cancel)
        warnings.extend(getattr(market, 'warnings', []))
    stopped()
    if not velocities and items:
        warnings.append('Предварительные показатели спроса недоступны; подробный просмотр ограничен исходной выборкой.')
    finalists = select_finalists(candidates, items, velocities, settings['detail_limit'])
    finalist_set = set(finalists)
    # One cheapest evaluated alternative per source type/job/item/quality, with distinct timed source conditions.
    candidates = [c for c in candidates if c['item'] in finalist_set]
    output = demo_market(finalists) if demo else market.fetch(settings['sale_world'], finalists,
             history=True, force=force, progress=progress, cancel=cancel)
    inputs = set()
    for c in candidates:
        if c.get('recipe'):
            inputs.update(c['recipe']['ingredients'])
    inputs.update(x['item'] for x in settings['consumables'])
    if settings.get('recursive_craft'):
        from workshop.crafting import CraftEngine
        roots = inputs | {c['item'] for c in candidates if c.get('recipe')}
        inputs = CraftEngine(data, settings['professions'], settings['buy_tax']).ids(roots)
    stopped()
    buying = demo_market(inputs) if demo else market.fetch(settings['purchase_scope'], inputs,
             history=False, force=force, progress=progress, cancel=cancel)
    rows = []
    for c in candidates:
        stopped()
        qualities = ['NQ']
        if c.get('recipe') and c['recipe']['hq'] and data['items'][c['item']]['hq'] and settings['professions'][c['job']]['hq']:
            qualities.append('HQ')
        for quality in qualities:
            rows.append(estimate(c, quality, data, settings, output.get(c['item'], Snapshot()), buying))
    dedup = {}
    for row in rows:
        key = (row['item_id'], row['job'], row['mode'], row['quality'], row['windows_text'] if row['mode']=='gather' else '')
        previous = dedup.get(key)
        if previous is None or (len(row['issues']), row['cost'] if row['cost'] is not None else math.inf) < (len(previous['issues']), previous['cost'] if previous['cost'] is not None else math.inf):
            dedup[key] = row
    rows = sorted(dedup.values(), key=lambda r: (r['profit'] is None, bool(r['issues']), -(r['profit'] or 0), -(r['sales_day'] or 0)))
    rejected = sum(not r['liquid'] for r in rows)
    if not settings['show_illiquid']:
        rows = [r for r in rows if r['liquid']]
    warnings.append(f'Малоликвидных вариантов: {rejected}; пороги: {settings["min_units_day"]:g} шт./день, {settings["min_deals_week"]} сделки/7 дней, последняя продажа ≤ {settings["max_sale_age_hours"]:g} ч.')
    if total_items > len(items):
        warnings.append('Рассмотрена часть каталога, сбалансированная по профессиям и уровням. Увеличьте лимит или отключите лишние профессии для большего охвата.')
    if demo:
        for row in rows:
            row['details'] = 'DEMO: все цены и продажи искусственные.\n\n' + row['details']
        warnings.append('DEMO: искусственные цены/история; расписание из настоящего каталога.')
    return rows, dict(eligible=total_items, screened=len(items), detailed=len(finalists),
                     analyzed_jobs=sorted({c['job'] for c in candidates}),
                     analysis_mode=settings['analysis_mode'], analysis_job=settings['analysis_job'],
                     warnings=warnings)
