import copy
import json
import math
from pathlib import Path
import tempfile
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from leveplanner.catalog import load
from leveplanner.craft_export import crafting_list
from leveplanner.planner import Plan, Progress
from leveplanner.route import Route, RouteStep

BASE = Path(__file__).resolve().parents[1]
SCOPE = 'leve-ingredient-closure-v1'


def data():
    names = {10: 'Final A', 11: 'Final B', 20: 'Shared Ingot', 21: 'Leather', 30: 'Ore', 31: 'Skin', 2: 'Fire Shard'}
    recipe = lambda rid, item, output, ingredients: dict(id=rid, item=item, job='Blacksmith', level=1,
        **{'yield': output}, ingredients=ingredients, book=0, specialist=False, hq=True)
    return dict(metadata={'recipe_scope': SCOPE}, items={i: dict(name=name, hq=i > 2, marketable=True) for i, name in names.items()},
                recipes=[recipe(1, 10, 1, {20: 1, 21: 1, 2: 1}), recipe(2, 11, 1, {20: 2}),
                         recipe(3, 20, 3, {30: 2, 2: 1}), recipe(4, 21, 1, {31: 2})])


def route(requireds, hqs=None, complete=True):
    result = Route(50, 1000, 40, 0)
    for index, required in enumerate(requireds):
        leve = dict(id=index, name='Test', job='Blacksmith', level=40, items=required, location='Test')
        p = Plan(leve, 'Покупка', 'HQ' if hqs and hqs[index] else 'NQ', 1,
                 Progress(45 + index, 1, 1, 100, 45 + index, 0), 1, 10, 20)
        result.steps.append(RouteStep(40 + index, 0, p))
    if complete:
        result.steps[-1].plan.progress.end_level = 50
    return result


class CraftListTests(unittest.TestCase):
    def test_shared_ingredients_round_yield_after_aggregating_demands(self):
        d = data()
        result = crafting_list(route([{10: 2}, {11: 2}]), d, 'Blacksmith')
        self.assertIn('6x Shared Ingot', result.pre)
        self.assertIn('2x Leather', result.pre)
        self.assertIn('2x Final A', result.final)
        self.assertIn('2x Final B', result.final)
        self.assertEqual(result.materials, {30: 4, 31: 4, 2: 4})
        row = next(r for r in result.recipes if r['recipe']['item'] == 20)
        self.assertEqual((row['crafts'], row['produced']), (2, 6))

    def test_raw_only_has_no_intermediates_and_counts_crystals(self):
        result = crafting_list(route([{10: 3}]), data(), 'Blacksmith', intermediates=False)
        self.assertEqual(result.pre, '')
        self.assertEqual(result.materials, {20: 3, 21: 3, 2: 3})

    def test_final_is_also_ingredient_not_duplicated_between_fields(self):
        result = crafting_list(route([{10: 1, 20: 1}]), data(), 'Blacksmith')
        self.assertIn('2x Shared Ingot', result.pre)
        self.assertNotIn('Shared Ingot', result.final)
        self.assertEqual(next(r for r in result.recipes if r['recipe']['item'] == 20)['crafts'], 1)

    def test_recursive_order_is_before_consumers(self):
        d = data()
        d['recipes'][0]['ingredients'] = {21: 1}
        d['recipes'][3]['ingredients'] = {20: 2}
        result = crafting_list(route([{10: 1}]), d, 'Blacksmith')
        self.assertLess(result.pre.index('Shared Ingot'), result.pre.index('Leather'))
        self.assertEqual(result.materials[30], 2)

    def test_same_final_across_stages_and_quality_use_plain_names(self):
        result = crafting_list(route([{10: 2}, {10: 3}], [True, False]), data(), 'Blacksmith')
        self.assertEqual(result.final, 'Items :\n5x Final A')
        self.assertEqual(result.qualities, {(10, True): 2, (10, False): 3})
        self.assertNotIn('HQ', result.final)
        self.assertIn('HQ/NQ', result.details(data()))

    def test_craft_only_uses_planned_synths_not_entire_leve_demand(self):
        d = data()
        d['recipes'][0]['yield'] = 3
        r = route([{10: 6}, {10: 2}])
        r.steps[0].plan.crafts = [dict(item=10, recipe=d['recipes'][0], count=2, produced=6, required=4)]
        result = crafting_list(r, d, 'Blacksmith', source='craft')
        self.assertEqual(result.final, 'Items :\n6x Final A')
        self.assertEqual(next(row for row in result.recipes if row['recipe']['item'] == 10)['crafts'], 2)

    def test_selected_stage_and_partial_route_are_explicit(self):
        result = crafting_list(route([{10: 2}, {11: 3}], complete=False), data(), 'Blacksmith', step_index=1)
        self.assertNotIn('Final A', result.final)
        self.assertIn('3x Final B', result.final)
        self.assertTrue(any('неполный' in line for line in result.warnings))

    def test_restricted_and_missing_recipes_stay_in_materials(self):
        d = data()
        d['recipes'][0]['book'] = 1
        result = crafting_list(route([{10: 1, 11: 1}]), d, 'Blacksmith')
        self.assertNotIn('Final A', result.final)
        self.assertEqual(result.materials[10], 1)
        self.assertTrue(any('нет обычного рецепта' in line for line in result.warnings))

    def test_cycles_and_old_catalog_do_not_generate_invalid_import(self):
        d = data()
        d['recipes'][2]['ingredients'] = {10: 1}
        with self.assertRaisesRegex(ValueError, 'Цикл'):
            crafting_list(route([{10: 1}]), d, 'Blacksmith')
        d = data()
        d['metadata'] = {}
        with self.assertRaisesRegex(ValueError, 'расширенный каталог'):
            crafting_list(route([{10: 1}]), d, 'Blacksmith')

    def test_inputs_unchanged_and_no_craft_error_is_helpful(self):
        d, r = data(), route([{10: 1}])
        saved_d, saved_r = copy.deepcopy(d), copy.deepcopy(r)
        crafting_list(r, d, 'Blacksmith')
        self.assertEqual(d, saved_d)
        self.assertEqual(r, saved_r)
        with self.assertRaisesRegex(ValueError, 'Все предметы для сдачи'):
            crafting_list(r, d, 'Blacksmith', source='craft')

    def test_real_screenshot_item_three_cesti_and_import_semantics(self):
        d = load(BASE / 'data' / 'catalog.json')
        result = crafting_list(route([{1728: 3}]), d, 'Leatherworker')
        self.assertEqual(result.final, 'Items :\n3x Peisteskin Cesti')
        self.assertIn('3x Steel Ingot', result.pre)
        self.assertIn('3x Peiste Leather', result.pre)
        self.assertEqual(result.materials[5322], 3)  # Raptor Sinew has no crafting recipe.
        self.assertTrue(any(row['recipe']['job'] == 'Blacksmith' for row in result.recipes))
        # Artisan parses "Nx Name", finds a recipe by exact name and ceil-divides by yield.
        by_name = {d['items'][row['recipe']['item']]['name']: row for row in result.recipes}
        parsed = {}
        for text in (result.pre, result.final):
            for line in text.splitlines():
                first, _, name = line.partition(' ')
                if not first.endswith('x'):
                    continue
                row = by_name[name]
                parsed[name] = math.ceil(int(first[:-1]) / row['recipe']['yield'])
        self.assertEqual(parsed, {name: row['crafts'] for name, row in by_name.items()})
        self.assertIn('не пересчитывает стоимость', result.details(d))

    def test_catalog_has_recursive_recipes_and_preserves_english_names(self):
        d = load(BASE / 'data' / 'catalog.json')
        self.assertEqual(d['metadata']['recipe_scope'], SCOPE)
        self.assertEqual(d['items'][1728]['name'], 'Peisteskin Cesti')
        self.assertGreater(len(d['recipes']), 1000)

    def test_old_cache_uses_bundled_recipe_closure_without_network(self):
        from leveplanner.app import get_catalog
        bundled = json.loads((BASE / 'data' / 'catalog.json').read_text(encoding='utf-8'))
        bundled['metadata']['checked_at'] = time.time()
        old = copy.deepcopy(bundled)
        del old['metadata']['recipe_scope']
        old['recipes'] = [r for r in old['recipes'] if r['id'] != 87]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'data').mkdir()
            (root / 'cache').mkdir()
            (root / 'data' / 'catalog.json').write_text(json.dumps(bundled), encoding='utf-8')
            (root / 'cache' / 'catalog.json').write_text(json.dumps(old), encoding='utf-8')
            with patch('leveplanner.app.ROOT', root), patch('leveplanner.app.refresh') as refresh:
                result, warning = get_catalog()
                refresh.assert_not_called()
            self.assertTrue(any(r['id'] == 87 for r in result['recipes']))
            self.assertFalse(warning)


class CraftListWindowTests(unittest.TestCase):
    def test_fields_copy_stage_source_and_txt_save(self):
        from leveplanner.craft_export_window import CraftExportWindow
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        self.addCleanup(root.destroy)
        d, r = data(), route([{10: 2}, {11: 3}])
        parent = SimpleNamespace(root=root, result=r, data=d, settings=SimpleNamespace(job='Blacksmith'),
                                 table=SimpleNamespace(selection=lambda: ('1',)), demo=False)
        window = CraftExportWindow(parent)
        window.root.withdraw()
        self.assertIn('2x Final A', window.final.get('1.0', 'end'))
        with patch.object(window.root, 'clipboard_clear') as clear, patch.object(window.root, 'clipboard_append') as append:
            window.copy_field('final')
            clear.assert_called_once()
            append.assert_called_once_with(window.result.final)
        window.scope.set('Выбранный этап')
        window.generate()
        self.assertNotIn('Final A', window.final.get('1.0', 'end'))
        self.assertIn('3x Final B', window.final.get('1.0', 'end'))
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'crafts.txt')
            with patch('leveplanner.craft_export_window.filedialog.asksaveasfilename', return_value=path):
                window.save()
            contents = Path(path).read_text(encoding='utf-8-sig')
            self.assertIn('Pre-craft Items', contents)
            self.assertIn('3x Final B', contents)
            self.assertIn('Базовые материалы', contents)
        window.source.set('Только крафт маршрута')
        window.generate()
        self.assertIsNone(window.result)
        self.assertEqual(str(window.final_button['state']), 'disabled')
        self.assertIn('Все предметы для сдачи', window.status.get())
