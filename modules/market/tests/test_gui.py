"""Desktop behavior uses local settings and mocked analysis, never live prices."""
import csv
import json
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from marketscout.app import Application, PANEL
from marketscout.results import item_url, number, order_indices
from marketscout.settings import load_settings

BASE = Path(__file__).resolve().parents[1]


def sample_row(**changes):
    row = dict(item_id=42, name="Test resource", job="Miner", level=10,
               mode="gather", quality="NQ", units=10, price=500,
               cost=120, revenue=4750, profit=4630, margin=3858.3,
               sales_day=9, transactions_day=3, listings_count=5,
               competing_units=40, confidence="средняя", hourly=None, liquid=True, status="Есть спрос",
               window_wait=0, windows_text="", issues=[], warnings=[],
               inputs=[{"item_id": 7, "name": "Crystal"}], details="Проверенные детали")
    row.update(changes)
    return row


class ResultsTests(unittest.TestCase):
    def test_numeric_sort_keeps_unknown_last_in_both_directions(self):
        rows = [sample_row(profit=None), sample_row(profit=2), sample_row(profit=100)]
        self.assertEqual(order_indices(rows), [2, 1, 0])
        self.assertEqual(order_indices(rows, descending=False), [1, 2, 0])

    def test_filter_requires_known_demand_and_cost(self):
        rows = [sample_row(), sample_row(sales_day=None), sample_row(cost=None)]
        self.assertEqual(order_indices(rows, mode="Сбор", min_sales=2, only_complete=True), [0])
        self.assertEqual(order_indices(rows, mode="Крафт"), [])

    def test_unknown_and_signed_money_and_url(self):
        self.assertEqual(number(None), "—")
        self.assertEqual(number(-1000, signed=True), "-1 000")
        self.assertEqual(number(1000, signed=True), "+1 000")
        self.assertEqual(item_url(42), "https://universalis.app/market/42")
        with self.assertRaises(ValueError):
            item_url("42/other")

    def test_unusable_budget_option_cannot_displace_usable_plan(self):
        rows = [sample_row(profit=1000, issues=["Не хватает бюджета"]), sample_row(profit=100)]
        self.assertEqual(order_indices(rows), [1, 0])


class DesktopTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest("Tk display unavailable: " + str(exc))
        self.root.withdraw()
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "settings.json"
        self.app = Application(self.root, demo=True, base_dir=BASE, settings_path=self.path)
        self.root.update_idletasks()

    def tearDown(self):
        if hasattr(self, "app"):
            self.app.closed = True
        if hasattr(self, "root"):
            for timer in self.root.tk.call("after", "info"):
                self.root.after_cancel(timer)
            self.root.destroy()
        if hasattr(self, "temp"):
            self.temp.cleanup()

    def select(self, index=0):
        self.app.table.selection_set(str(index))
        self.app.show_detail()

    def test_professions_budget_and_markets_survive_restart(self):
        self.app.profiles["Carpenter"]["level"].set("73")
        self.app.profiles["Carpenter"]["hq"].set(True)
        self.app.profiles["Miner"]["enabled"].set(False)
        self.app.vars["budget"].set("")
        self.app.vars["sale_world"].set("Мир · Odin")
        self.assertTrue(self.app.save(quiet=True))
        saved, warnings = load_settings(self.path)
        self.assertEqual(saved["professions"]["Carpenter"]["level"], 73)
        self.assertTrue(saved["professions"]["Carpenter"]["hq"])
        self.assertFalse(saved["professions"]["Miner"]["enabled"])
        self.assertIsNone(saved["budget"])
        self.assertEqual(saved["sale_world"], "Odin")

    def test_dark_details_and_clickable_item_and_ingredient(self):
        self.assertEqual(self.app.detail.cget("background"), PANEL)
        self.app.rows = [sample_row()]
        self.app.redraw()
        self.select()
        self.assertIn("Проверенные детали", self.app.detail.get("1.0", "end"))
        self.assertIn("link1", self.app.detail.tag_names())
        with patch("marketscout.app.webbrowser.open") as opener:
            self.app.open_selected()
            self.app.open_item(7)
        self.assertEqual(opener.call_args_list[0].args, ("https://universalis.app/market/42",))
        self.assertEqual(opener.call_args_list[1].args, ("https://universalis.app/market/7",))

    def test_render_cost_is_negative_and_missing_profit_is_dash(self):
        self.app.filters["min_profit"].set("")
        self.app.rows = [sample_row(profit=None)]
        self.app.redraw()
        values = self.app.table.item("0", "values")
        keys = [column[0] for column in self.app.columns]
        self.assertEqual(values[keys.index("cost")], "-120")
        self.assertEqual(values[keys.index("revenue")], "+4 750")
        self.assertEqual(values[keys.index("profit")], "—")

    def test_measure_is_saved_per_resource(self):
        self.app.profiles["Fisher"]["per_unit"].set("7")
        self.assertTrue(self.app.save(quiet=True))
        self.app.rows = [sample_row(job="Fisher")]
        self.app.redraw()
        self.select()
        for key, value in {"minutes": "20", "units": "10", "bait": "300", "yield": "4"}.items():
            self.app.measure[key].set(value)
        self.app.save_measure()
        saved, _ = load_settings(self.path)
        measurement = saved["item_profiles"]["42"]
        self.assertEqual(measurement["rate"], 30)
        self.assertEqual(measurement["bait_per_unit"], 30)
        self.assertEqual(measurement["window_yield"], 4)
        self.assertEqual(measurement["per_unit"], 7)

    def test_demand_thresholds_persist_and_illiquid_comparison_is_explicit(self):
        self.app.vars["min_units_day"].set("2")
        self.app.vars["min_deals_week"].set("5")
        self.app.vars["max_sale_age_hours"].set("48")
        self.app.show_illiquid.set(True)
        self.assertTrue(self.app.save(quiet=True))
        saved, _ = load_settings(self.path)
        self.assertEqual(saved["min_units_day"], 2)
        self.assertEqual(saved["min_deals_week"], 5)
        self.assertEqual(saved["max_sale_age_hours"], 48)
        self.assertTrue(saved["show_illiquid"])
        self.app.rows = [sample_row(liquid=False, status="Малоликвидный", sales_day=0.5, window_wait=600)]
        self.app.redraw()
        self.assertEqual(len(self.app.table.get_children()), 1)
        keys = [column[0] for column in self.app.columns]
        values = self.app.table.item("0", "values")
        self.assertEqual(values[keys.index("window_wait")], "10")
        self.assertEqual(values[keys.index("status")], "Малоликвидный")

    def test_calendar_updates_without_price_request_or_measurement_reset(self):
        source = dict(kind="timed", windows=[dict(start_hour=1, duration_hours=1)])
        self.app.rows = [sample_row(_source=source, windows_text="Старое окно",
                                    details="Цена 500 gil.\nСтарое окно")]
        self.app.redraw()
        self.select()
        self.app.measure["minutes"].set("12")
        with patch("marketscout.app.analysis.next_window", return_value=dict(wait=600)), \
             patch("marketscout.app.analysis.schedule_text", return_value="Свежее окно"), \
             patch("marketscout.app.analysis.analyze") as engine:
            self.app.update_windows()
        self.assertEqual(self.app.table.set("0", "window_wait"), "10")
        self.assertEqual(self.app.measure["minutes"].get(), "12")
        text = self.app.detail.get("1.0", "end")
        self.assertIn("Свежее окно", text)
        self.assertNotIn("Старое окно", text)
        self.assertIn("Цена 500 gil", text)
        engine.assert_not_called()

    def test_default_profit_filter_hides_losses_but_can_be_cleared(self):
        self.app.rows = [sample_row(profit=-10), sample_row(profit=10)]
        self.app.redraw()
        self.assertEqual(self.app.table.get_children(), ("1",))
        self.app.filters["min_profit"].set("")
        self.app.redraw()
        self.assertEqual(len(self.app.table.get_children()), 2)

    def test_worker_returns_results_without_network_on_tk_thread(self):
        summary = dict(eligible=1, screened=1, detailed=1, warnings=[])
        with patch("marketscout.app.catalog.load", return_value={}), \
             patch("marketscout.app.analysis.analyze", return_value=([sample_row()], summary)) as engine:
            self.app.analyze()
            deadline = time.monotonic() + 3
            while self.app.busy and time.monotonic() < deadline:
                self.root.update()
                time.sleep(0.01)
        self.assertFalse(self.app.busy)
        self.assertEqual(len(self.app.rows), 1)
        self.assertEqual(len(self.app.table.get_children()), 1)
        self.assertTrue(engine.call_args.kwargs["demo"])
        self.assertIsNotNone(engine.call_args.kwargs["cancel"])

    def test_real_catalog_demo_completes_in_desktop(self):
        for job, profile in self.app.profiles.items():
            profile["enabled"].set(job in {"Miner", "Carpenter"})
        self.app.profiles["Miner"]["level"].set("80")
        self.app.profiles["Carpenter"]["level"].set("30")
        self.app.vars["scan_limit"].set("600")
        self.app.vars["detail_limit"].set("16")
        self.app.filters["min_profit"].set("")
        self.app.analyze()
        deadline = time.monotonic() + 20
        while self.app.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertFalse(self.app.busy)
        self.assertTrue(self.app.rows, self.app.status.get())
        self.assertEqual({row["mode"] for row in self.app.rows}, {"craft", "gather"})
        self.assertTrue(all(row["liquid"] for row in self.app.rows))
        index = int(self.app.table.get_children()[0])
        self.select(index)
        self.assertIn("DEMO", self.app.detail.get("1.0", "end"))
        self.assertIn("link0", self.app.detail.tag_names())

    def test_export_preserves_expense_sign_and_window_minutes(self):
        self.app.rows = [sample_row(window_wait=600)]
        path = Path(self.temp.name) / "results.csv"
        with patch("marketscout.app.filedialog.asksaveasfilename", return_value=str(path)):
            self.app.export()
        with path.open(encoding="utf-8-sig", newline="") as stream:
            row = next(csv.DictReader(stream, delimiter=";"))
        self.assertEqual(float(row["cost"]), -120)
        self.assertEqual(float(row["window_wait_minutes"]), 10)
        self.assertEqual(row["universalis"], item_url(42))

    def test_selected_profession_is_saved_and_used_in_real_demo(self):
        for profile in self.app.profiles.values():
            profile['level'].set('50')
        self.app.filters['mode'].set('Крафт')
        self.app.filters['job'].set('Carpenter')
        self.app.filters['min_profit'].set('')
        self.app.analyze()
        deadline = time.monotonic() + 20
        while self.app.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertFalse(self.app.busy)
        self.assertTrue(self.app.rows, self.app.status.get())
        self.assertEqual({r['job'] for r in self.app.rows}, {'Carpenter'})
        self.assertEqual({r['mode'] for r in self.app.rows}, {'craft'})
        saved, _ = load_settings(self.path)
        self.assertEqual(saved['analysis_job'], 'Carpenter')
        self.assertEqual(saved['analysis_mode'], 'craft')

    def test_empty_result_explains_old_scope_and_reset_restores_rows(self):
        self.app.rows = [sample_row()]
        self.app.analyzed_scope = ('all', 'all')
        self.app.filters['mode'].set('Крафт')
        self.app.filters['job'].set('Carpenter')
        self.app.redraw()
        self.assertFalse(self.app.table.get_children())
        self.assertIn('Показано 0 из 1', self.app.result_note.get())
        self.assertIn('Анализировать рынок', self.app.detail.get('1.0', 'end'))
        self.app.reset_filters()
        self.assertEqual(self.app.table.get_children(), ('0',))
        self.assertEqual(self.app.current_scope(), ('all', 'all'))

    def test_explicit_consumable_save_and_remove(self):
        self.app.consumable_vars["item"].set("7")
        self.app.consumable_vars["quantity"].set("2")
        self.app.add_consumable()
        saved, _ = load_settings(self.path)
        self.assertEqual(saved["consumables"][0]["quantity"], 2)
        self.app.consumable_table.selection_set("0")
        self.app.remove_consumable()
        saved, _ = load_settings(self.path)
        self.assertEqual(saved["consumables"], [])


if __name__ == "__main__":
    unittest.main()
