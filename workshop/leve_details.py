"""Leve details and offline fixtures without a desktop GUI dependency."""
from datetime import datetime
import time
from leveplanner.market import Listing
from leveplanner.results import money

def demo_prices(data, ids):
    # Artificial prices, not a bundled live market snapshot.
    now = int(time.time())
    return {i: [Listing(20 + i % 180, 99, False, "DEMO", now) for _ in range(5)] +
               [Listing(200 + i % 800, 10, True, "DEMO", now) for _ in range(8)]
            for i in ids}


def describe(plan, data, settings, scope, uploads, demo=False):
    name = lambda i: data["items"].get(i, {}).get("name", f"#{i}")
    p = plan.progress
    fmt = lambda n: f"{n:,}".replace(",", " ")
    lines = [f"{'[DEMO — искусственные цены] ' if demo else ''}{plan.leve['name']} · {plan.leve['job']} · Lv {plan.leve['level']}",
             f"Место получения: {plan.leve['location']} · рынок: {scope}",
             f"Способ: {plan.method} {plan.quality}; сдач на принятие: до {plan.per_accept}",
             f"Цель: Lv {settings.level} → {p.target}; итог: Lv {p.end_level}, EXP {fmt(p.end_xp)}",
             f"Принять задание {p.accepts} раз; сдач всего: {p.turnins}; allowances: {plan.allowances}",
             f"EXP от сдач: {fmt(p.xp)}; базовая награда за сдачу: {fmt(plan.leve['exp'])} EXP / {fmt(plan.leve['gil'])} gil",
             f"Награда: {money(plan.reward, True)} gil; расход: {money(-plan.cost if plan.cost is not None else None, True)} gil",
             f"Баланс (плюс = прибыль, минус = убыток): {money(plan.balance, True)} gil",
             f"Налог покупки: {settings.tax:g}%; дорога на весь план: {fmt(settings.travel)} gil",
             "Статус: " + ("Доступно в рамках бюджета и allowances" if plan.feasible else "; ".join(plan.problems)),
             "", "Сдать предметы:"]
    for i, q in plan.leve["items"].items():
        lines.append(f"  {name(i)} #{i}: {q} × {p.turnins} = {q * p.turnins} {plan.quality}")
    if plan.crafts:
        lines += ["", "Крафт (из покупных NQ ингредиентов, включая кристаллы):"]
        for c in plan.crafts:
            r = c["recipe"]
            lines.append(f"  {name(c['item'])}: рецепт #{r['id']} Lv {r['level']}, {c['count']} крафтов × {r['yield']} = {c['produced']} шт.")
            for i, q in r["ingredients"].items():
                lines.append(f"    {name(i)} #{i}: {q} × {c['count']} = {q * c['count']}")
        lines.append("  HQ-крафт предполагает получение HQ каждый раз. EXP самого крафта не добавлен.")
    lines += ["", "Купить (целые стаки; остатки сохраняются, доход от их продажи не учтён):"]
    for s in plan.shopping:
        buy = s["purchase"]
        lines.append(f"  {name(s['item'])} #{s['item']}: нужно {s['required']}, купить {buy.quantity}, остаток {buy.quantity - s['required']}; {fmt(buy.cost)} gil")
        for l in buy.stacks:
            date = datetime.fromtimestamp(l.reviewed).astimezone().strftime("%Y-%m-%d %H:%M") if l.reviewed else "неизвестно"
            lines.append(f"    {l.quantity} × {fmt(l.price)} · {'HQ' if l.hq else 'NQ'} · {l.world} · дата объявления {date}")
    dates = [uploads.get(s["item"], 0) for s in plan.shopping]
    if dates and all(dates):
        oldest = datetime.fromtimestamp(min(dates) / 1000).astimezone().strftime("%Y-%m-%d %H:%M")
        lines.append(f"Самое старое обновление купленного предмета в Universalis: {oldest} (местное время ПК)")
    lines += ["", "Бюджет проверен для закупки всей серии заранее. Gil-награды не финансируют закупку.",
              "Последнее принятие может иметь меньше сдач. Другие открывшиеся leves не подставляются автоматически.",
              "Рынок DC предполагает поездки между мирами. Доступ к NPC/заданиям проверьте в игре.",
              "Награды gil — базовая оценка; случайные предметы-награды, время, ремонт и EXP крафта не оценены."]
    return "\n".join(lines)


