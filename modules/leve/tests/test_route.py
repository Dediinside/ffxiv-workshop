import csv
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from leveplanner.catalog import load
from leveplanner.market import Listing
from leveplanner.planner import Settings, simulate
from leveplanner.route import build_route, route_market_ids
from leveplanner.route_window import route_summary

BASE = Path(__file__).resolve().parents[1]


def sample():
    leves = [dict(id=level, name=f'Leve {level}', job='Carpenter', level=level, exp=100, gil=10,
                  allowances=1, max_turnins=1, items={10: 1}, location='Test') for level in (1, 5)]
    return dict(level_cap=10, xp={i: 100 for i in range(1, 11)}, leves=leves,
                items={i: dict(name=f'Item {i}', hq=True, marketable=True) for i in (10, 11, 20)},
                recipes=[dict(id=20, item=10, job='Carpenter', level=1, ingredients={20: 2},
                              **{'yield': 3}, hq=True, book=0, specialist=False)])


class RouteTests(unittest.TestCase):
    def test_whole_stack_leftovers_and_exp_across_arbitrary_target(self):
        d = sample()
        stock = {10: [Listing(10, 8, False)]}
        r = build_route(d, Settings('Carpenter', 1, tax=0), 8, stock, method='Покупка', quality='NQ')
        self.assertTrue(r.complete)
        self.assertEqual((r.needed_xp, r.gained_xp, r.end_level, r.end_xp), (700, 700, 8, 0))
        self.assertEqual([s.plan.cost for s in r.steps], [80, 0])
        self.assertEqual(r.leftovers[(10, False)], 1)
        self.assertEqual(sum(len(s['purchase'].stacks) for step in r.steps for s in step.plan.shopping), 1)
        self.assertEqual(len(stock[10]), 1, 'Input stock must remain unchanged')

    def test_lowest_cost_and_fewest_allowances_choose_different_quality(self):
        d = sample()
        stock = {10: [Listing(1, 4, False), Listing(10, 2, True)]}
        settings = Settings('Carpenter', 1, tax=0)
        cheap = build_route(d, settings, 5, stock)
        fast = build_route(d, settings, 5, stock, priority='allowances')
        self.assertEqual((cheap.steps[0].plan.quality, cheap.cost, cheap.allowances), ('NQ', 4, 4))
        self.assertEqual((fast.steps[0].plan.quality, fast.cost, fast.allowances), ('HQ', 20, 2))

    def test_overflow_skips_unlocks_and_keeps_starting_xp(self):
        d = sample()
        for l in d['leves']:
            l['exp'] = 250
        r = build_route(d, Settings('Carpenter', 1, 50, tax=0), 8,
                        {10: [Listing(1, 3, False)]}, quality='NQ', method='Покупка')
        self.assertEqual((r.needed_xp, r.gained_xp, r.end_level, r.end_xp), (650, 750, 9, 0))
        self.assertEqual(r.steps[1].start_level, 6)
        self.assertEqual(r.steps[1].start_xp, 50)

    def test_missing_later_price_keeps_partial_and_unknown_total(self):
        d = sample()
        d['leves'][1]['items'] = {11: 1}
        r = build_route(d, Settings('Carpenter', 1, tax=0), 8,
                        {10: [Listing(1, 4, False)]}, quality='NQ', method='Покупка')
        self.assertFalse(r.complete)
        self.assertIsNone(r.cost)
        self.assertEqual((r.end_level, r.spent), (5, 4))
        self.assertTrue(r.issues)

    def test_craft_yield_and_shared_ingredient_leftovers(self):
        r = build_route(sample(), Settings('Carpenter', 1, tax=0), 8,
                        {20: [Listing(10, 6, False)]}, method='Крафт', quality='NQ')
        self.assertTrue(r.complete)
        self.assertEqual(r.cost, 60)
        self.assertEqual(sum(c['count'] for s in r.steps for c in s.plan.crafts), 3)
        self.assertEqual(r.leftovers[(10, False)], 2)
        self.assertEqual(r.leftovers[(20, False)], 0)

    def test_recipe_unlock_not_assumed_before_stage(self):
        d = sample()
        d['recipes'][0]['level'] = 5
        r = build_route(d, Settings('Carpenter', 1), 8, {20: [Listing(1, 99, False)]}, method='Крафт')
        self.assertFalse(r.complete)
        self.assertEqual(r.steps, [])

    def test_hq_craft_requires_guarantee(self):
        r = build_route(sample(), Settings('Carpenter', 1), 8, {}, method='Крафт', quality='HQ')
        self.assertFalse(r.complete)
        self.assertIn('гарантию HQ', r.issues[0])

    def test_budget_and_allowances_report_route_total_and_travel_each_stage(self):
        settings = Settings('Carpenter', 1, allowances=2, tax=0, travel=10)
        r = build_route(sample(), settings, 8, {10: [Listing(1, 7, False)]}, budget=20, method='Покупка', quality='NQ')
        self.assertEqual((r.cost, r.allowances, r.reward), (27, 7, 70))
        self.assertEqual(len(r.issues), 2)
        self.assertIn('без лимита', route_summary(r, settings, None))
        self.assertIn('27', route_summary(r, settings, 20))

    def test_invalid_target_current_xp_and_budget(self):
        for target in (0, 1, 11):
            with self.assertRaises(ValueError):
                build_route(sample(), Settings('Carpenter', 1), target, {})
        with self.assertRaises(ValueError):
            build_route(sample(), Settings('Carpenter', 1, 100), 8, {})
        with self.assertRaises(ValueError):
            build_route(sample(), Settings('Carpenter', 1), 8, {}, budget=-1)

    def test_cap_and_cancel(self):
        r = build_route(sample(), Settings('Carpenter', 1, tax=0), 10,
                        {10: [Listing(1, 9, False)]}, quality='NQ')
        self.assertEqual((r.end_level, r.end_xp, r.needed_xp), (10, 0, 900))
        with self.assertRaisesRegex(ValueError, 'отменён'):
            build_route(sample(), Settings('Carpenter', 1), 8, {}, cancelled=lambda: True)

    def test_default_five_level_simulation_unchanged(self):
        d = sample()
        self.assertEqual(simulate(d['leves'][0], Settings('Carpenter', 1), d['xp'], 10, False, 1).target, 5)
        self.assertEqual(simulate(d['leves'][0], Settings('Carpenter', 1), d['xp'], 10, False, 1, 8).target, 8)

    def test_real_catalog_level_one_to_fifty(self):
        from leveplanner.app import demo_prices
        d = load(BASE / 'data' / 'catalog.json')
        settings = Settings('Carpenter', 1, tax=5, craft_hq=True)
        prices = demo_prices(d, route_market_ids(d, settings, 50))
        r = build_route(d, settings, 50, prices)
        self.assertTrue(r.complete, r.issues)
        self.assertEqual(r.needed_xp, sum(d['xp'][i] for i in range(1, 50)))
        self.assertGreaterEqual(r.gained_xp, r.needed_xp)
        for step in r.steps:
            self.assertLessEqual(step.plan.leve['level'], step.start_level)
            for c in step.plan.crafts:
                self.assertLessEqual(c['recipe']['level'], step.start_level)


class RouteWindowTests(unittest.TestCase):
    def test_separate_window_demo_route_and_export_leave_main_untouched(self):
        from leveplanner.app import Application
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        def cleanup():
            for timer in root.tk.call('after', 'info'):
                root.after_cancel(timer)
            root.destroy()
        self.addCleanup(cleanup)
        app = Application(root, demo=True)
        before = {k: v.get() for k, v in app.vars.items()}
        app.open_route()
        window = app.route_window
        window.root.withdraw()
        window.vars['level'].set('1')
        window.vars['budget'].set('')
        window.target.set('50')
        window.calculate()
        deadline = time.monotonic() + 25
        while window.busy and time.monotonic() < deadline:
            root.update()
            time.sleep(.01)
        self.assertFalse(window.busy)
        self.assertIsNotNone(window.result, window.status.get())
        self.assertTrue(window.result.complete)
        self.assertEqual({k: v.get() for k, v in app.vars.items()}, before)
        self.assertEqual(app.plans, [])
        self.assertIn('DEMO', window.detail.get('1.0', 'end'))
        self.assertIn('без лимита', window.detail.get('1.0', 'end'))
        window.table.selection_set('0')
        window.show_step()
        self.assertTrue(window.detail.tag_ranges('link'))
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'route.csv')
            with patch('leveplanner.route_window.filedialog.asksaveasfilename', return_value=path):
                window.export()
            with open(path, encoding='utf-8-sig', newline='') as file:
                rows = list(csv.reader(file))
            self.assertEqual(rows[1][2], '1')
            self.assertTrue(any('ИТОГ' in row for row in rows))
        window.close()
        self.assertTrue(window.closed)
        self.assertTrue(root.winfo_exists())
