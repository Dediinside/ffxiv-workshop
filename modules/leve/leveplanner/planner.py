"""Deterministic calculations, independent of GUI and HTTP."""
from collections import Counter
from dataclasses import dataclass, field
import itertools
import math

from .market import best_purchase


@dataclass(frozen=True)
class Settings:
    job: str
    level: int
    current_xp: int = 0
    budget: int = 100000
    allowances: int = 100
    tax: float = 5.0
    craft_hq: bool = False
    travel: int = 0
    professions: dict | None = None
    cancelled: object = None
    craft_per: float = 0
    batch_overhead: float = 0
    consumables: tuple = ()


@dataclass
class Progress:
    target: int
    accepts: int
    turnins: int
    xp: int
    end_level: int
    end_xp: int


@dataclass
class Plan:
    leve: dict
    method: str
    quality: str
    per_accept: int
    progress: Progress
    allowances: int
    reward: int
    cost: int | None = None
    shopping: list = field(default_factory=list)
    crafts: list = field(default_factory=list)
    problems: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def net(self):
        return None if self.cost is None else self.cost - self.reward

    @property
    def balance(self):
        return None if self.cost is None else self.reward - self.cost

    @property
    def feasible(self):
        return self.cost is not None and not self.problems


def next_target(level, cap):
    if not 1 <= level < cap:
        raise ValueError(f"Для прокачки выберите уровень от 1 до {cap - 1}")
    return min(cap, (level // 5 + 1) * 5)


def reward_xp(leve, player_level, hq):
    exp = leve["exp"] * (2 if hq else 1)
    # Prior-expansion leves have a 1000 EXP cap at the corresponding expansion boundary.
    boundary = max(50, (leve["level"] // 10 + 1) * 10)
    return min(exp, 1000) if player_level >= boundary else exp


def simulate(leve, settings, xp_table, cap, hq, turns_per_accept, target=None):
    target = next_target(settings.level, cap) if target is None else target
    if not settings.level < target <= cap:
        raise ValueError("Цель должна быть выше текущего уровня и не выше максимального")
    if not 1 <= turns_per_accept <= leve["max_turnins"]:
        raise ValueError("Некорректное число сдач на одно принятие")
    level, xp = settings.level, settings.current_xp
    if not 0 <= xp < xp_table[level]:
        raise ValueError("Текущий EXP должен быть меньше EXP до следующего уровня")
    if leve["level"] > level:
        raise ValueError("Leve ещё не доступен")
    accepts = turns = gained = 0
    while level < target:
        accepts += 1
        for _ in range(turns_per_accept):
            amount = reward_xp(leve, level, hq)
            xp += amount
            gained += amount
            turns += 1
            while level < cap and xp >= xp_table[level]:
                xp -= xp_table[level]
                level += 1
            if level >= target:
                break
        if turns > 100000:
            raise ValueError("Слишком много сдач; выберите более высокий leve")
    return Progress(target, accepts, turns, gained, level, xp if level < cap else 0)


def recipes_for(data, item, settings, hq=False):
    return [r for r in data["recipes"] if r["item"] == item and r["job"] == settings.job
            and r["level"] <= settings.level + 5 and not r["book"] and not r["specialist"]
            and (not hq or r["hq"])]


def market_ids(data, leves, settings):
    ids = {i for l in leves for i in l["items"]}
    if settings.professions is not None:
        from workshop.crafting import CraftEngine, stage_professions
        return CraftEngine(data, stage_professions(settings), settings.tax).ids(ids | {e['item'] for e in settings.consumables})
    for item in list(ids):
        for r in recipes_for(data, item, settings):
            ids.update(r["ingredients"])
    return {i for i in ids if data["items"][i]["marketable"]}


def buy_items(required, prices, tax, hq):
    shopping, missing = [], []
    for item, qty in sorted(required.items()):
        purchase = best_purchase([l for l in prices.get(item, []) if l.hq == hq], qty, tax)
        if purchase is None:
            missing.append(f"#{item}: нет цены или недостаточно {'HQ' if hq else 'NQ'} ({qty} шт.)")
        else:
            shopping.append({"item": item, "required": qty, "purchase": purchase})
    return shopping, missing


def price_craft(data, required, settings, prices, hq):
    if settings.professions is not None:
        from workshop.crafting import leve_craft_quote
        quoted = leve_craft_quote(data, required, settings, prices, hq)
        return quoted.cost, quoted.shopping, quoted.crafts, []
    choices = [recipes_for(data, item, settings, hq) for item in required]
    if any(not c for c in choices):
        return None, [], [], ["Нет доступного обычного рецепта выбранной профессии"]
    best, errors = None, []
    for recipes in itertools.product(*choices):
        ingredients, crafts = Counter(), []
        for (item, qty), recipe in zip(required.items(), recipes):
            n = math.ceil(qty / recipe["yield"])
            ingredients.update({i: q * n for i, q in recipe["ingredients"].items()})
            crafts.append({"item": item, "recipe": recipe, "count": n,
                           "produced": n * recipe["yield"], "required": qty})
        shopping, missing = buy_items(ingredients, prices, settings.tax, False)
        if missing:
            errors.extend(missing)
            continue
        cost = sum(s["purchase"].cost for s in shopping)
        if best is None or cost < best[0]:
            best = (cost, shopping, crafts, [])
    return best if best else (None, [], [], sorted(set(errors)))


def recommend(data, leves, prices, settings):
    if settings.budget < 0 or not 0 <= settings.allowances <= 100 or settings.travel < 0:
        raise ValueError("Бюджет/дорога неотрицательны; allowances от 0 до 100")
    if not 0 <= settings.tax <= 20:
        raise ValueError("Налог должен быть от 0 до 20%")
    # Validate early, even if there are no eligible leves.
    next_target(settings.level, data["level_cap"])
    if not 0 <= settings.current_xp < data["xp"][settings.level]:
        raise ValueError("Некорректный текущий EXP")
    plans = []
    for leve in leves:
        if settings.cancelled and settings.cancelled():
            raise InterruptedError('Расчёт отменён')
        can_hq = all(data["items"][i]["hq"] for i in leve["items"])
        for hq in (False, True) if can_hq else (False,):
            for per_accept in range(1, leve["max_turnins"] + 1):
                progress = simulate(leve, settings, data["xp"], data["level_cap"], hq, per_accept)
                required = {i: qty * progress.turnins for i, qty in leve["items"].items()}
                for method in ("Покупка", "Крафт"):
                    if method == "Крафт" and hq and not settings.craft_hq:
                        continue
                    plan = Plan(leve, method, "HQ" if hq else "NQ", per_accept, progress,
                                progress.accepts * leve["allowances"],
                                leve["gil"] * (2 if hq else 1) * progress.turnins)
                    try:
                        if method == "Покупка":
                            plan.shopping, plan.problems = buy_items(required, prices, settings.tax, hq)
                            if not plan.problems:
                                plan.cost = sum(s["purchase"].cost for s in plan.shopping)
                        else:
                            plan.cost, plan.shopping, plan.crafts, plan.problems = price_craft(
                                data, required, settings, prices, hq)
                            plan.notes = [note for c in plan.crafts for note in c.get('notes', [])]
                    except ValueError as exc:
                        plan.problems.append(str(exc))
                    if plan.cost is not None:
                        plan.cost += settings.travel
                        if plan.cost > settings.budget:
                            plan.problems.append(f"Бюджет: нужно ещё {plan.cost - settings.budget:,} gil")
                    if plan.allowances > settings.allowances:
                        shortage = plan.allowances - settings.allowances
                        plan.problems.append(f"Allowances: нужно ещё {shortage} (пополнений по 3: {math.ceil(shortage / 3)})")
                    plans.append(plan)
    return sorted(plans, key=lambda p: (not p.feasible, p.cost is None,
                                       p.net if p.net is not None else float("inf"),
                                       p.allowances, p.progress.turnins))
