"""Shared batch buy-or-craft search; whole market stacks and shared ingredient demand."""
from collections import Counter, defaultdict
from dataclasses import dataclass, field
import math

from leveplanner.market import best_purchase


@dataclass
class CraftQuote:
    cost: int
    shopping: list = field(default_factory=list)
    crafts: list = field(default_factory=list)
    used: Counter = field(default_factory=Counter)
    leftovers: Counter = field(default_factory=Counter)
    warnings: list = field(default_factory=list)


class CraftEngine:
    def __init__(self, data, professions, tax=5, max_states=4000, cancel=lambda: False, craft_per=0):
        self.data, self.professions, self.tax = data, professions or {}, tax
        self.max_states, self.cancel, self.craft_per = max_states, cancel, craft_per
        self.recipes = defaultdict(list)
        for r in data['recipes']:
            p = self.professions.get(r['job'], {})
            if (p.get('enabled', True) and r['level'] <= p.get('level', 0)
                and (not r.get('book') or p.get('books'))
                and (not r.get('specialist') or p.get('specialist'))
                and r['yield'] > 0 and all(q > 0 for q in r['ingredients'].values())):
                self.recipes[r['item']].append(r)

    def ids(self, roots):
        found, pending = set(roots), list(roots)
        while pending:
            item = pending.pop()
            for r in self.recipes[item]:
                for ing in r['ingredients']:
                    if ing not in found:
                        found.add(ing)
                        pending.append(ing)
        return {i for i in found if self.data['items'].get(i, {}).get('marketable', True)}

    def quote(self, required, prices, inventory=None, force=None):
        """required/force use (item,hq) keys; force values are root recipe IDs or None.

        Compare buying the entire outstanding demand with crafting it. Does not mix
        buying and crafting portions of the same item. A bounded search is explicit.
        """
        inventory, force = Counter(inventory or {}), force or {}
        required = Counter({k: v for k, v in required.items() if v > 0})
        if not required:
            return CraftQuote(0)
        blocked, heights, visiting = set(), {}, []
        def height(item):
            if item in visiting:
                raise ValueError(tuple(visiting[visiting.index(item):]))
            if item in heights:
                return heights[item]
            visiting.append(item)
            value = 0
            for r in self.recipes[item]:
                if r['id'] not in blocked:
                    value = max(value, 1 + max((height(i) for i in r['ingredients']), default=0))
            visiting.pop()
            heights[item] = value
            return value
        cycles = False
        while True:
            try:
                for item, _ in required:
                    height(item)
                break
            except ValueError as exc:
                cycle = set(exc.args[0])
                for item in cycle:
                    blocked.update(r['id'] for r in self.recipes[item] if cycle.intersection(r['ingredients']))
                heights.clear()
                visiting.clear()
                cycles = True
        best, visited, capped, missing = None, 0, False, set()
        purchase_cache = {}
        def search(demand, cost, shopping, crafts, used, extras):
            nonlocal best, visited, capped
            if self.cancel():
                raise InterruptedError('Расчёт отменён')
            if visited >= self.max_states:
                capped = True
                return
            visited += 1
            if best is not None and cost >= best.cost:
                return
            if not demand:
                best = CraftQuote(cost, shopping, list(reversed(crafts)), used, extras)
                return
            key = max(demand, key=lambda k: (heights.get(k[0], 0), k[0], k[1]))
            item, hq = key
            demand = demand.copy()
            quantity = demand.pop(key)
            reuse = min(quantity, max(0, inventory[key] - used[key]))
            consumed = used.copy()
            consumed[key] += reuse
            needed = quantity - reuse
            if not needed:
                search(demand, cost, shopping, crafts, consumed, extras)
                return
            options = []
            if key not in force:
                cached_key = (key, needed)
                if cached_key not in purchase_cache:
                    purchase_cache[cached_key] = best_purchase([l for l in prices.get(item, []) if l.hq == hq], needed, self.tax)
                purchase = purchase_cache[cached_key]
                if purchase is not None:
                    options.append((purchase.cost, None, purchase))
            for recipe in self.recipes[item]:
                if recipe['id'] in blocked or hq and (not recipe['hq'] or not self.professions[recipe['job']].get('hq')):
                    continue
                if key in force and force[key] is not None and recipe['id'] != force[key]:
                    continue
                count = math.ceil(needed / recipe['yield'])
                # A lower bound orders choices; whole-stack costing happens in child states.
                hint = count * self.craft_per
                for ing, amount in recipe['ingredients'].items():
                    cheapest = min((l.price for l in prices.get(ing, []) if not l.hq), default=0)
                    hint += amount * count * cheapest
                options.append((hint, recipe, count))
            if not options:
                missing.add(f'#{item}: нет доступной покупки или рецепта для {needed} {"HQ" if hq else "NQ"}')
            for _, recipe, payload in sorted(options, key=lambda x: x[0]):
                leftover = extras.copy()
                if recipe is None:
                    leftover[key] += payload.quantity - needed
                    row = dict(item=item, required=needed, total_required=quantity, reused=reuse, purchase=payload)
                    search(demand, cost + payload.cost, shopping + [row], crafts, consumed, leftover)
                else:
                    count = payload
                    rest = demand.copy()
                    rest.update({(i, False): q * count for i, q in recipe['ingredients'].items()})
                    leftover[key] += count * recipe['yield'] - needed
                    row = dict(item=item, recipe=recipe, count=count, produced=count * recipe['yield'], required=needed,
                               quality='HQ' if hq else 'NQ', intermediate=key not in force)
                    search(rest, cost + count * self.craft_per, shopping, crafts + [row], consumed, leftover)
        search(required, 0, [], [], Counter(), Counter())
        if best is None:
            reason = 'Лимит поиска достигнут; не найден полностью оценённый вариант.' if capped else '; '.join(sorted(missing)[:8])
            raise ValueError(reason or 'Нет доступного варианта')
        if capped:
            best.warnings.append('Достигнут лимит сравнения цепочек; это лучший найденный вариант, минимум не доказан.')
        else:
            best.warnings.append('Сравнены покупка всей потребности и крафт каждого материала; смешанная покупка части материала и крафт остатка не оптимизируются.')
        if cycles:
            best.warnings.append('Циклические варианты рецептов исключены; базовые ресурсы не считаются бесплатными.')
        for row in best.crafts:
            r = row['recipe']
            if r.get('craftsmanship') or r.get('control'):
                best.warnings.append(f"Рецепт #{r['id']}: проверьте craftsmanship/control в игре.")
        return best


def stage_professions(settings):
    profiles = {job: dict(p) for job, p in (settings.professions or {}).items()}
    p = profiles.setdefault(settings.job, {})
    p.update(enabled=True, level=settings.level, hq=settings.craft_hq)
    return profiles


def leve_craft_quote(data, required, settings, prices, hq, inventory=None):
    roots = {(i, hq): q for i, q in required.items()}
    needs = Counter(roots)
    inventory = Counter(inventory or {})
    if any(q > inventory[key] for key, q in roots.items()):
        for entry in settings.consumables:
            if entry['mode'] in ('all', 'craft'):
                needs[entry['item'], entry['hq']] += entry['quantity']
    engine = CraftEngine(data, stage_professions(settings), settings.tax,
                         cancel=settings.cancelled or (lambda: False), craft_per=settings.craft_per)
    priced = engine.quote(needs, prices, inventory, force={key:None for key in roots})
    if priced.crafts:
        priced.cost += settings.batch_overhead
        if settings.batch_overhead:
            priced.warnings.append(f'Еда/зелья/ремонт/прочее на этап крафта: {settings.batch_overhead:g} gil.')
        priced.crafts[0]['notes'] = priced.warnings
    return priced
