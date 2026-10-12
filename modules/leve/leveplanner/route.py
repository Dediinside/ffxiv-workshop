"""Stage-wise leve route. Quotes reserve whole listings and carry purchased leftovers."""
from collections import Counter
from dataclasses import dataclass, field, replace
import itertools
import math

from .catalog import JOBS, available
from .market import best_purchase
from .planner import Plan, recipes_for, simulate


@dataclass
class RouteStep:
    start_level: int
    start_xp: int
    plan: Plan
    used: Counter = field(default_factory=Counter)
    leftovers: Counter = field(default_factory=Counter)


@dataclass
class Route:
    target: int
    needed_xp: int
    start_level: int
    start_xp: int
    steps: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    leftovers: Counter = field(default_factory=Counter)

    @property
    def end_level(self):
        return self.steps[-1].plan.progress.end_level if self.steps else self.start_level

    @property
    def end_xp(self):
        return self.steps[-1].plan.progress.end_xp if self.steps else self.start_xp

    @property
    def complete(self):
        return self.end_level >= self.target

    @property
    def spent(self):
        return sum(s.plan.cost for s in self.steps)

    @property
    def cost(self):
        return self.spent if self.complete else None

    @property
    def reward(self):
        return sum(s.plan.reward for s in self.steps)

    @property
    def allowances(self):
        return sum(s.plan.allowances for s in self.steps)

    @property
    def gained_xp(self):
        return sum(s.plan.progress.xp for s in self.steps)


def validate_request(data, settings, target, budget, priority, method, quality):
    if settings.job not in JOBS or not 1 <= settings.level < target <= data['level_cap']:
        raise ValueError(f"Проверьте профессию и уровни: 1 ≤ текущий < желаемый ≤ {data['level_cap']}")
    if not 0 <= settings.current_xp < data['xp'][settings.level]:
        raise ValueError(f"EXP внутри уровня: 0–{data['xp'][settings.level] - 1}")
    if budget is not None and (not math.isfinite(budget) or budget < 0):
        raise ValueError('Бюджет должен быть неотрицательным; пусто — без лимита')
    if not 0 <= settings.allowances <= 100 or not 0 <= settings.tax <= 20 or settings.travel < 0:
        raise ValueError('Allowances 0–100, налог 0–20%, дорога ≥ 0')
    if priority not in ('cost', 'allowances') or method not in ('Все', 'Покупка', 'Крафт') or quality not in ('Все', 'HQ', 'NQ'):
        raise ValueError('Некорректный приоритет, способ или качество')


def route_market_ids(data, settings, target):
    leves = [l for l in data['leves'] if l['job'] == settings.job and l['level'] < target]
    outputs = {i for l in leves for i in l['items']}
    if settings.professions is not None:
        from workshop.crafting import CraftEngine, stage_professions
        profiles = stage_professions(replace(settings, level=target - 1))
        return CraftEngine(data, profiles, settings.tax).ids(outputs | {e['item'] for e in settings.consumables})
    ids = set(outputs)
    for recipe in data['recipes']:
        if recipe['item'] in outputs and recipe['job'] == settings.job and recipe['level'] < target:
            if not recipe['book'] and not recipe['specialist']:
                ids.update(recipe['ingredients'])
    return {i for i in ids if data['items'][i]['marketable']}


def buy_with_inventory(required, prices, inventory, tax, hq, used=None):
    used, leftovers, shopping = Counter(used or {}), Counter(), []
    for item, qty in sorted(required.items()):
        key = (item, hq)
        reuse = min(qty, max(0, inventory[key] - used[key]))
        used[key] += reuse
        need = qty - reuse
        purchase = best_purchase([l for l in prices.get(item, []) if l.hq == hq], need, tax)
        if purchase is None:
            raise ValueError(f'#{item}: нет цены или недостаточно {"HQ" if hq else "NQ"} ({need} шт.)')
        leftovers[key] += purchase.quantity - need
        shopping.append(dict(item=item, required=need, total_required=qty, reused=reuse, purchase=purchase))
    return sum(s['purchase'].cost for s in shopping), shopping, used, leftovers


def quote(data, required, settings, prices, inventory, hq, method):
    if method == 'Покупка':
        cost, shopping, used, leftovers = buy_with_inventory(required, prices, inventory, settings.tax, hq)
        return cost, shopping, [], used, leftovers
    if settings.professions is not None:
        from workshop.crafting import leve_craft_quote
        priced = leve_craft_quote(data, required, settings, prices, hq, inventory)
        return priced.cost, priced.shopping, priced.crafts, priced.used, priced.leftovers
    used, remaining = Counter(), {}
    for item, qty in required.items():
        used[(item, hq)] = min(qty, inventory[(item, hq)])
        if qty > used[(item, hq)]:
            remaining[item] = qty - used[(item, hq)]
    # Recipe access is evaluated at the beginning of this stage, without assuming +5 levels.
    choices = [[r for r in recipes_for(data, item, settings, hq) if r['level'] <= settings.level]
               for item in remaining]
    if any(not c for c in choices):
        raise ValueError('Нет рецепта, доступного на начале этапа')
    best, errors = None, []
    for recipes in itertools.product(*choices):
        ingredients, crafts, outputs = Counter(), [], Counter()
        for (item, qty), recipe in zip(remaining.items(), recipes):
            count = math.ceil(qty / recipe['yield'])
            ingredients.update({i: q * count for i, q in recipe['ingredients'].items()})
            outputs[(item, hq)] += count * recipe['yield'] - qty
            crafts.append(dict(item=item, recipe=recipe, count=count,
                               produced=count * recipe['yield'], required=qty))
        try:
            cost, shopping, consumed, extras = buy_with_inventory(ingredients, prices, inventory, settings.tax, False, used)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        extras.update(outputs)
        candidate = cost, shopping, crafts, consumed, extras
        if best is None or cost < best[0]:
            best = candidate
    if best is None:
        raise ValueError('; '.join(sorted(set(errors))))
    return best


def build_route(data, settings, target, prices, budget=None, priority='cost', method='Все', quality='Все',
                progress=lambda s: None, cancelled=lambda: False):
    validate_request(data, settings, target, budget, priority, method, quality)
    result = Route(target, sum(data['xp'][i] for i in range(settings.level, target)) - settings.current_xp,
                   settings.level, settings.current_xp)
    inventory, stock = Counter(), {i: list(rows) for i, rows in prices.items()}
    tiers = sorted({l['level'] for l in data['leves'] if l['job'] == settings.job} | {target})
    while result.end_level < target:
        if cancelled():
            raise ValueError('Расчёт отменён')
        current = replace(settings, level=result.end_level, current_xp=result.end_xp)
        checkpoint = min(t for t in tiers if t > current.level)
        checkpoint = min(checkpoint, target)
        progress(f'Маршрут: Lv {current.level} → {checkpoint}…')
        candidates, errors = [], []
        # Nearby tiers first; older leves are considered when nearby ones cannot be priced.
        nearby = available(data, settings.job, current.level)
        older = [l for l in available(data, settings.job, current.level, True) if l not in nearby]
        for group in (nearby, older):
            for leve in group:
                if cancelled():
                    raise ValueError('Расчёт отменён')
                can_hq = all(data['items'][i]['hq'] for i in leve['items'])
                quotes = {}
                for hq in (False, True) if can_hq else (False,):
                    label = 'HQ' if hq else 'NQ'
                    if quality != 'Все' and quality != label:
                        continue
                    for repeats in range(1, leve['max_turnins'] + 1):
                        try:
                            p = simulate(leve, current, data['xp'], data['level_cap'], hq, repeats, checkpoint)
                        except ValueError as exc:
                            errors.append(str(exc))
                            continue
                        required = {i: qty * p.turnins for i, qty in leve['items'].items()}
                        for mode in ('Покупка', 'Крафт') if method == 'Все' else (method,):
                            if mode == 'Крафт' and hq and not settings.craft_hq:
                                continue
                            key = (hq, p.turnins, mode)
                            if key not in quotes:
                                try:
                                    quotes[key] = quote(data, required, current, stock, inventory, hq, mode)
                                except ValueError as exc:
                                    quotes[key] = None
                                    errors.append(str(exc))
                            priced = quotes[key]
                            if priced is None:
                                continue
                            cost, shopping, crafts, used, leftovers = priced
                            plan = Plan(leve, mode, label, repeats, p, p.accepts * leve['allowances'],
                                        leve['gil'] * (2 if hq else 1) * p.turnins,
                                        cost + settings.travel, shopping, crafts)
                            plan.notes = [note for c in crafts for note in c.get('notes', [])]
                            candidates.append(RouteStep(current.level, current.current_xp, plan, used, leftovers))
            if candidates:
                break
        if not candidates:
            reason = 'Для HQ-крафта включите гарантию HQ. ' if method == 'Крафт' and quality == 'HQ' and not settings.craft_hq else ''
            result.issues.append(f'Маршрут остановлен на Lv {current.level}: {reason}нет варианта с известной стоимостью. ' + '; '.join(sorted(set(errors))[:5]))
            break
        def rank(step):
            p = step.plan
            return (p.cost, p.allowances, p.progress.turnins) if priority == 'cost' else (p.allowances, p.cost, p.progress.turnins)
        chosen = min(candidates, key=rank)
        result.steps.append(chosen)
        inventory.subtract(chosen.used)
        inventory.update(chosen.leftovers)
        # Every selected whole listing is reserved once for the entire route.
        reserved = Counter(id(l) for s in chosen.plan.shopping for l in s['purchase'].stacks)
        for item, rows in stock.items():
            kept = []
            for listing in rows:
                if reserved[id(listing)]:
                    reserved[id(listing)] -= 1
                else:
                    kept.append(listing)
            stock[item] = kept
    result.leftovers = +inventory
    if budget is not None and result.spent > budget:
        result.issues.append(f'Бюджета не хватает: ещё {result.spent - budget:,} gil для рассчитанной части.')
    if result.allowances > settings.allowances:
        shortage = result.allowances - settings.allowances
        result.issues.append(f'Нужно ещё {shortage} allowances: пополнений по 3 — {math.ceil(shortage / 3)}.')
    return result
