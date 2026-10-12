"""Recipe quantities for Artisan's Teamcraft text importer (item units, not synths)."""
from collections import Counter, defaultdict
from dataclasses import dataclass, field
import math


@dataclass
class CraftList:
    name: str
    pre: str = ''
    final: str = ''
    materials: Counter = field(default_factory=Counter)
    recipes: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    qualities: Counter = field(default_factory=Counter)

    def details(self, data):
        lines = list(self.warnings)
        lines += ['', 'Количество в импорте — предметы; число крафтов рассчитывается по выходу рецепта.',
                  'Английские названия: поля предназначены для клиента игры EN.',
                  'Качество HQ/NQ не передаётся этим текстовым форматом. Для HQ отключите Final Items → Quick Synth',
                  'и настройте получение HQ в Artisan. Требования качества маршрута перечислены ниже.',
                  'Список крафта не пересчитывает стоимость маршрута: при замене покупки на крафт или',
                  'изготовлении полуфабрикатов закупка материалов изменится.',
                  'Маршрут прокачки выполняйте по этапам: высокоуровневые рецепты откроются позже.',
                  'Профессии и доступ к рецептам полуфабрикатов проверяются вами; начальный инвентарь не вычитается.',
                  '', 'Рецепты (количество предметов / крафтов / выход):']
        for row in self.recipes:
            r = row['recipe']
            lines.append(f"{data['items'][r['item']]['name']}: нужно {row['needed']}, крафтов {row['crafts']} × {r['yield']} = {row['produced']} · {r['job']} Lv {r['level']} · #{r['id']}")
        lines += ['', 'Требования качества конечных предметов:']
        lines += [f"{data['items'][item]['name']}: {qty} {'HQ' if hq else 'NQ'}" for (item, hq), qty in sorted(self.qualities.items())]
        lines += ['', 'Базовые материалы / купить или собрать:']
        lines += [f"{qty}x {data['items'][item]['name']}" for item, qty in sorted(self.materials.items(), key=lambda x: data['items'][x[0]]['name'])]
        return '\n'.join(lines)

    def document(self, data):
        return f'List Name\n{self.name}\n\nPre-craft Items\n{self.pre}\n\nFinal Items\n{self.final}\n\n' + self.details(data)


def crafting_list(route, data, job, source='all', intermediates=True, step_index=None):
    if source not in ('all', 'craft'):
        raise ValueError('Неизвестный источник списка')
    if not route.steps:
        raise ValueError('В рассчитанном маршруте пока нет этапов')
    if step_index is not None and not 0 <= step_index < len(route.steps):
        raise ValueError('Выберите этап маршрута')
    steps = route.steps if step_index is None else [route.steps[step_index]]
    start, end = steps[0].start_level, steps[-1].plan.progress.end_level
    result = CraftList(f'{job} {start}-{end}' + (' DEMO' if any(l.world == 'DEMO' for s in steps for buy in s.plan.shopping for l in buy['purchase'].stacks) else ''))
    required, chosen_levels, selected_recipes = Counter(), defaultdict(list), defaultdict(set)
    for step in steps:
        p = step.plan
        hq = p.quality == 'HQ'
        if source == 'all':
            for item, qty in p.leve['items'].items():
                needed = qty * p.progress.turnins
                required[item] += needed
                result.qualities[item, hq] += needed
                chosen_levels[item].append(step.start_level)
        else:
            for craft in p.crafts:
                # Actual planned synths already exclude carried finished items and include yield leftovers.
                required[craft['item']] += craft['produced']
                result.qualities[craft['item'], hq] += craft['produced']
                chosen_levels[craft['item']].append(step.start_level)
                selected_recipes[craft['item']].add(craft['recipe']['id'])
    if not required:
        raise ValueError('В выбранной части нет крафта. Выберите «Все предметы для сдачи» или маршрут со способом «Крафт».')
    if not route.complete:
        result.warnings.append('Маршрут неполный: список содержит только рассчитанные этапы.')
    if source == 'all' and any(s.plan.method == 'Покупка' for s in steps):
        result.warnings.append('Включены также предметы, которые маршрут предлагал купить готовыми; теперь они запланированы для крафта.')
    if intermediates and data.get('metadata', {}).get('recipe_scope') != 'leve-ingredient-closure-v1':
        raise ValueError('Для полуфабрикатов нужен расширенный каталог. Перезапустите обновлённое приложение и пересчитайте маршрут.')
    by_item = defaultdict(list)
    for recipe in data['recipes']:
        if not recipe['book'] and not recipe['specialist']:
            by_item[recipe['item']].append(recipe)
    # Name import resolves the first recipe for this output; use the lowest ordinary row ID.
    for rows in by_item.values():
        rows.sort(key=lambda r: r['id'])
    chosen, colors, postorder = {}, {}, []
    def visit(item, path=()):
        if len(path) > 64 or colors.get(item) == 1:
            raise ValueError('Цикл или слишком глубокая цепочка рецептов')
        if colors.get(item) == 2:
            return
        if item not in data['items']:
            raise ValueError(f'Нет данных о предмете #{item}')
        colors[item] = 1
        rows = by_item[item] if intermediates or item in required else []
        if rows:
            recipe = rows[0]
            if recipe['yield'] <= 0 or any(q <= 0 for q in recipe['ingredients'].values()):
                raise ValueError('Некорректный выход или ингредиенты рецепта')
            chosen[item] = recipe
            if len(rows) > 1:
                result.warnings.append(f"{data['items'][item]['name']}: несколько рецептов; импорт по имени — проверьте выбранную профессию/рецепт в Artisan.")
            if selected_recipes[item] and selected_recipes[item] != {recipe['id']}:
                result.warnings.append(f"{data['items'][item]['name']}: рецепт импорта по имени может отличаться от рецепта расчёта маршрута.")
            if chosen_levels[item] and (recipe['job'] != job or recipe['level'] > min(chosen_levels[item])):
                result.warnings.append(f"{data['items'][item]['name']}: нужен {recipe['job']} Lv {recipe['level']}; на начале одного из этапов этот рецепт может быть недоступен.")
            for ingredient in sorted(recipe['ingredients']):
                visit(ingredient, path + (item,))
        elif item in required:
            result.warnings.append(f"{data['items'][item]['name']}: нет обычного рецепта; предмет не включён в поля крафта, оставлен в закупке.")
        colors[item] = 2
        postorder.append(item)
    for item in sorted(required):
        visit(item)
    demand = Counter(required)
    calculations = {}
    # All parent demands are combined before rounding the yield of a shared ingredient.
    for item in reversed(postorder):
        if item not in chosen:
            result.materials[item] = demand[item]
            continue
        recipe = chosen[item]
        count = math.ceil(demand[item] / recipe['yield'])
        calculations[item] = dict(recipe=recipe, needed=demand[item], crafts=count, produced=count * recipe['yield'])
        demand.update({i: qty * count for i, qty in recipe['ingredients'].items()})
    dependencies = {i for r in chosen.values() for i in r['ingredients'] if i in chosen}
    pre, final = [], []
    for item in postorder:
        if item not in chosen:
            continue
        line = f"{demand[item]}x {data['items'][item]['name']}"
        (pre if item in dependencies else final).append(line)
        result.recipes.append(calculations[item])
        if item in required and item in dependencies:
            result.warnings.append(f"{data['items'][item]['name']}: также используется как полуфабрикат; весь объём помещён в Pre-craft Items, без дублирования в Final Items.")
    result.pre = ('Pre crafts :\n' + '\n'.join(pre)) if pre else ''
    result.final = ('Items :\n' + '\n'.join(final)) if final else ''
    if not chosen:
        result.warnings.append('Нет предметов, пригодных для импорта крафта. Список закупки находится ниже.')
    return result
