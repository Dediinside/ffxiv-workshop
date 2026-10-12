"""Widget and async flow smoke test; no UI opens visibly, no external API required."""
import time
import unittest


class GuiSmokeTests(unittest.TestCase):
    def test_create_calculate_select_and_describe(self):
        import tkinter as tk
        from leveplanner.app import Application
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        def close():
            for timer in root.tk.call('after', 'info'):
                root.after_cancel(timer)
            root.destroy()
        self.addCleanup(close)
        app = Application(root, demo=True)
        app.calculate()
        deadline = time.monotonic() + 20
        while app.busy and time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)
        self.assertFalse(app.busy, "Worker did not finish")
        self.assertTrue(app.plans)
        self.assertEqual(len(app.table.get_children()), len(app.plans))
        self.assertIn("DEMO", app.detail.get("1.0", "end"))
        self.assertIn("Купить", app.detail.get("1.0", "end"))
        self.assertIn("Баланс (плюс = прибыль", app.detail.get("1.0", "end"))
        app.method_filter.set("Крафт")
        app.quality_filter.set("HQ")
        app.render_results()
        self.assertFalse(app.visible_indices)
        self.assertIn("Включите «Могу гарантированно крафтить HQ»", app.detail.get("1.0", "end"))
        app.craft_hq.set(True)
        app.render_results()
        self.assertIn("Галочка уже включена", app.detail.get("1.0", "end"))
        self.assertIn("Настройка HQ изменена", app.status.get())
        app.vars["level"].set("45")
        app.vars["xp"].set("132")
        app.calculate()
        deadline = time.monotonic() + 20
        while app.busy and time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)
        self.assertFalse(app.busy)
        self.assertTrue(app.visible_indices)
        self.assertTrue(all(app.plans[i].quality == "HQ" and app.plans[i].method == "Крафт" for i in app.visible_indices))
        app.reset_filters()
        app.priority.set("Дешевле закупка")
        app.choose_priority()
        available = [app.plans[i] for i in app.visible_indices if app.plans[i].feasible]
        self.assertEqual([p.cost for p in available], sorted(p.cost for p in available))
        first = app.table.get_children()[0]
        self.assertLessEqual(int(str(app.table.set(first, "cost")).replace(" ", "")), 0)
        app.sort_by_column("accepts")
        self.assertFalse(app.sort_descending)
        app.sort_by_column("accepts")
        self.assertTrue(app.sort_descending)
        app.quality_filter.set("HQ")
        app.method_filter.set("Покупка")
        app.render_results()
        self.assertTrue(app.visible_indices)
        self.assertTrue(all(app.plans[i].quality == "HQ" and app.plans[i].method == "Покупка" for i in app.visible_indices))
        app.search.set("no matching leve")
        self.assertFalse(app.table.get_children())
        self.assertIn("Нет вариантов", app.detail.get("1.0", "end"))
        app.reset_filters()
        self.assertEqual(len(app.table.get_children()), len(app.plans))
        self.assertEqual(app.sort_column, "balance")
        import csv
        from pathlib import Path
        import tempfile
        from unittest.mock import patch
        app.quality_filter.set("HQ")
        app.render_results()
        with tempfile.TemporaryDirectory() as folder:
            exported = Path(folder) / "plans.csv"
            with patch("leveplanner.app.filedialog.asksaveasfilename", return_value=str(exported)):
                app.export()
            with exported.open(encoding="utf-8-sig", newline="") as f:
                rows = list(csv.reader(f))
            self.assertEqual(len(rows) - 1, len(app.visible_indices))
            self.assertTrue(all(row[6] == "HQ" for row in rows[1:]))
            for row, index in zip(rows[1:], app.visible_indices):
                plan = app.plans[index]
                self.assertEqual(int(row[12]) if row[12] else None, -plan.cost if plan.cost is not None else None)
                self.assertEqual(int(row[14]) if row[14] else None, plan.balance)


if __name__ == "__main__":
    unittest.main()
