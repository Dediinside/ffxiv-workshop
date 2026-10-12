"""Russian Tkinter desktop UI; network work never runs on the Tk thread."""
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .catalog import JOBS, available, load, refresh
from .market import BASE, Listing, Market
from .net import JsonCache, atomic_json
from .planner import Settings, market_ids, recommend
from .results import PRIORITIES, money, order_indices

ROOT = Path(__file__).resolve().parent.parent


def get_catalog(progress=lambda s: None, force=False):
    cache_path = ROOT / "cache" / "catalog.json"
    data = None
    try:
        data = load(cache_path)
        if data['metadata'].get('recipe_scope') != 'leve-ingredient-closure-v1':
            data = load(ROOT / 'data' / 'catalog.json')
    except (OSError, ValueError, KeyError, TypeError):
        data = load(ROOT / "data" / "catalog.json")
    warning = ""
    if force or time.time() - data["metadata"].get("checked_at", 0) > 7 * 86400:
        try:
            data = refresh(cache_path, progress)
        except Exception as exc:
            warning = f"Обновление каталога не удалось. Используется снимок {data['metadata']['upstream_date']}: {exc}"
    return data, warning


def scopes(cache, force=False):
    def validate(value):
        if not isinstance(value, list) or not value or not all(isinstance(x, dict) and "name" in x for x in value):
            raise ValueError("Неверный список миров/DC")
    worlds = cache.get(BASE + "worlds", ttl=86400, max_stale=30 * 86400, force=force, validate=validate)
    dcs = cache.get(BASE + "data-centers", ttl=86400, max_stale=30 * 86400, force=force, validate=validate)
    return scope_map(worlds, dcs)


def scope_map(worlds, dcs):
    # English game sheets match the global client, not CN/KR releases.
    dcs = [x for x in dcs if x.get("region") in {"Japan", "Europe", "North-America", "Oceania"}]
    ids = {i for dc in dcs for i in dc["worlds"]}
    return {**{f"DC · {x['name']} ({x['region']})": x["name"] for x in dcs},
            **{f"Мир · {x['name']}": str(x["id"]) for x in sorted(worlds, key=lambda x: x["name"]) if x["id"] in ids}}


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


class Application:
    def __init__(self, root, demo=False):
        self.root, self.demo = root, demo
        self.messages = queue.Queue()
        self.busy = False
        self.data = load(ROOT / "data" / "catalog.json")
        self.plans = []
        self.sort_column, self.sort_descending = "balance", True
        self.visible_indices = []
        self.settings = None
        self.uploads = {}
        self.scope_map = json.loads((ROOT / "data" / "scopes.json").read_text(encoding="utf-8"))
        root.title("FFXIV · Tradecraft Leve Planner" + (" · DEMO" if demo else ""))
        root.geometry("1280x820")
        root.minsize(1000, 680)
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Treeview", rowheight=27)
        wrap = ttk.Frame(root, padding=16)
        wrap.pack(fill="both", expand=True)
        ttk.Label(wrap, text="Tradecraft Leves — следующий рубеж", font=("Segoe UI", 20, "bold")).pack(anchor="w")
        ttk.Label(wrap, text="Выберите профессию и рынок. Сравните закупку HQ/NQ и крафт по стоимости всей серии.").pack(anchor="w", pady=(4, 14))
        form = ttk.Frame(wrap)
        form.pack(fill="x")
        self.vars = {k: tk.StringVar(value=v) for k, v in {"job": JOBS[0], "level": "20", "xp": "0", "budget": "100000", "allowances": "100", "tax": "5", "travel": "0"}.items()}
        self.scope = tk.StringVar(value="DC · Light (Europe)")
        if self.scope.get() not in self.scope_map:
            self.scope.set(next(iter(self.scope_map)))
        self.job_box = self.field(form, "Профессия", 0, self.vars["job"], JOBS, 17)
        self.scope_box = self.field(form, "Мир / DC", 1, self.scope, list(self.scope_map), 29)
        for col, key, title, width in [(2, "level", "Уровень", 7), (3, "xp", "EXP внутри уровня", 14),
                                       (4, "budget", "Бюджет, gil", 13), (5, "allowances", "Allowances", 10)]:
            self.field(form, title, col, self.vars[key], width=width)
        options = ttk.Frame(wrap)
        options.pack(fill="x", pady=10)
        self.craft_hq = tk.BooleanVar(value=False)
        self.all_unlocked = tk.BooleanVar(value=False)
        self.force = tk.BooleanVar(value=False)
        ttk.Checkbutton(options, text="Могу гарантированно крафтить HQ", variable=self.craft_hq,
                        command=self.render_results).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(options, text="Все доступные уровни", variable=self.all_unlocked).pack(side="left", padx=8)
        ttk.Checkbutton(options, text="Свежие цены (обойти кэш)", variable=self.force).pack(side="left", padx=8)
        ttk.Label(options, text="Налог %").pack(side="left", padx=(12, 4))
        ttk.Entry(options, textvariable=self.vars["tax"], width=5).pack(side="left")
        ttk.Label(options, text="Дорога, gil").pack(side="left", padx=(12, 4))
        ttk.Entry(options, textvariable=self.vars["travel"], width=8).pack(side="left")
        buttons = ttk.Frame(wrap)
        buttons.pack(fill="x")
        self.calculate_button = ttk.Button(buttons, text="Найти выгодные leves", command=self.calculate)
        self.calculate_button.pack(side="left")
        self.refresh_button = ttk.Button(buttons, text="Обновить игровые данные", command=self.update_catalog)
        self.refresh_button.pack(side="left", padx=8)
        ttk.Button(buttons, text="Экспорт CSV", command=self.export).pack(side="left")
        ttk.Button(buttons, text="Маршрут прокачки…", command=self.open_route).pack(side="left", padx=8)
        filters = ttk.Frame(wrap)
        filters.pack(fill="x", pady=(12, 0))
        self.priority = tk.StringVar(value="Максимальная прибыль")
        ttk.Label(filters, text="Приоритет").pack(side="left", padx=(0, 5))
        priority_box = ttk.Combobox(filters, textvariable=self.priority, values=list(PRIORITIES), state="readonly", width=25)
        priority_box.pack(side="left", padx=(0, 14))
        priority_box.bind("<<ComboboxSelected>>", self.choose_priority)
        self.method_filter, self.quality_filter = tk.StringVar(value="Все"), tk.StringVar(value="Все")
        for label, variable, values, width in [("Способ", self.method_filter, ("Все", "Покупка", "Крафт"), 9),
                                               ("Качество", self.quality_filter, ("Все", "HQ", "NQ"), 6)]:
            ttk.Label(filters, text=label).pack(side="left", padx=(0, 5))
            box = ttk.Combobox(filters, textvariable=variable, values=values, state="readonly", width=width)
            box.pack(side="left", padx=(0, 14))
            box.bind("<<ComboboxSelected>>", lambda event: self.render_results())
        self.only_feasible = tk.BooleanVar(value=False)
        ttk.Checkbutton(filters, text="Только доступные", variable=self.only_feasible, command=self.render_results).pack(side="left")
        self.search = tk.StringVar(value="")
        ttk.Label(filters, text="Название").pack(side="left", padx=(14, 5))
        ttk.Entry(filters, textvariable=self.search, width=18).pack(side="left")
        self.search.trace_add("write", lambda *args: self.render_results())
        ttk.Button(filters, text="Сброс", command=self.reset_filters).pack(side="left", padx=8)
        self.status = tk.StringVar(value=self.catalog_status())
        ttk.Label(wrap, textvariable=self.status, wraplength=1200).pack(fill="x", pady=(10, 4))
        self.warning = tk.StringVar(value="DEMO: все цены искусственные." if demo else "Universalis обновляется игроками; проверяйте дату и предложения в игре.")
        ttk.Label(wrap, textvariable=self.warning, foreground="#9a5700", wraplength=1200).pack(fill="x", pady=(0, 8))
        pane = ttk.Panedwindow(wrap, orient="vertical")
        pane.pack(fill="both", expand=True)
        top = ttk.Frame(pane)
        cols = ("name", "level", "method", "quality", "turns", "accepts", "allow", "cost", "reward", "balance", "status")
        self.table = ttk.Treeview(top, columns=cols, show="headings", height=10)
        titles = ("Leve / сдач на принятие", "Lv", "Способ", "HQ/NQ", "Сдачи", "Принятия", "Allow.", "Расход gil", "Награда gil", "Баланс gil", "Доступность")
        widths = (260, 40, 70, 55, 55, 70, 55, 95, 95, 105, 130)
        self.column_titles = dict(zip(cols, titles))
        for col, title, width in zip(cols, titles, widths):
            self.table.heading(col, text=title, command=lambda column=col: self.sort_by_column(column))
            self.table.column(col, width=width, minwidth=35, stretch=col in ("name", "status"))
        scroll = ttk.Scrollbar(top, orient="vertical", command=self.table.yview)
        horizontal = ttk.Scrollbar(top, orient="horizontal", command=self.table.xview)
        self.table.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        self.table.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        top.rowconfigure(0, weight=1)
        top.columnconfigure(0, weight=1)
        self.table.bind("<<TreeviewSelect>>", self.show_details)
        bottom = ttk.Frame(pane)
        self.detail = tk.Text(bottom, wrap="word", font=("Consolas", 10), height=13, padx=10, pady=10)
        detail_scroll = ttk.Scrollbar(bottom, command=self.detail.yview)
        self.detail.configure(yscrollcommand=detail_scroll.set)
        self.detail.pack(side="left", fill="both", expand=True)
        detail_scroll.pack(side="right", fill="y")
        self.set_details("Выберите строку после расчёта, чтобы увидеть рецепты, количества и список покупок.\n\nВыберите приоритет или нажмите заголовок столбца для сортировки; повторное нажатие меняет направление. Доступные планы идут первыми, неизвестные суммы — в конце.\nБаланс: плюс означает прибыль, минус — убыток. Расходы показаны с минусом, награды — с плюсом.\nФильтры работают сразу, без новых запросов цен.\nКрафт использует купленные ингредиенты; промежуточные материалы не крафтятся рекурсивно.")
        pane.add(top, weight=3)
        pane.add(bottom, weight=2)
        root.after(100, self.poll)

    def open_route(self):
        from .route_window import RouteWindow
        if getattr(self, 'route_window', None) is not None and not self.route_window.closed:
            self.route_window.root.lift()
            self.route_window.root.focus_set()
        else:
            self.route_window = RouteWindow(self)

    def field(self, frame, label, col, variable, values=None, width=12):
        ttk.Label(frame, text=label).grid(row=0, column=col, sticky="w", padx=(0, 12))
        widget = ttk.Combobox(frame, textvariable=variable, values=values, state="readonly", width=width) if values else ttk.Entry(frame, textvariable=variable, width=width)
        widget.grid(row=1, column=col, sticky="w", padx=(0, 12), pady=4)
        return widget

    def catalog_status(self):
        m = self.data["metadata"]
        return f"Каталог: {len(self.data['leves'])} leves · {m['upstream_date'][:10]} · {m['upstream_message']} · {m['commit'][:10]}"

    def set_details(self, text):
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text)
        self.detail.configure(state="disabled")

    def run_worker(self, function):
        if self.busy:
            return
        self.busy = True
        self.calculate_button.configure(state="disabled")
        self.refresh_button.configure(state="disabled")
        def work():
            try:
                self.messages.put(("result", function()))
            except Exception as exc:
                self.messages.put(("error", str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def calculate(self):
        try:
            settings = Settings(self.vars["job"].get(), int(self.vars["level"].get()),
                                int(self.vars["xp"].get()), int(self.vars["budget"].get()),
                                int(self.vars["allowances"].get()), float(self.vars["tax"].get()),
                                self.craft_hq.get(), int(self.vars["travel"].get()))
            if not 1 <= settings.level < self.data["level_cap"]:
                raise ValueError(f"Уровень должен быть от 1 до {self.data['level_cap'] - 1}")
            if not 0 <= settings.current_xp < self.data["xp"][settings.level]:
                raise ValueError(f"EXP должен быть от 0 до {self.data['xp'][settings.level] - 1}")
            if settings.budget < 0 or settings.travel < 0 or not 0 <= settings.allowances <= 100 or not 0 <= settings.tax <= 20:
                raise ValueError("Бюджет/дорога ≥ 0, allowances 0–100, налог 0–20%")
        except ValueError as exc:
            messagebox.showerror("Проверьте параметры", str(exc))
            return
        scope_label = self.scope.get()
        scope = self.scope_map[scope_label]
        all_unlocked, force = self.all_unlocked.get(), self.force.get()
        progress = lambda s: self.messages.put(("progress", s))
        def work():
            data, warning = (self.data, "") if self.demo else get_catalog(progress)
            cache = JsonCache(ROOT / "cache" / "http")
            new_scopes = None
            warnings = [warning] if warning else []
            if not self.demo:
                try:
                    new_scopes = scopes(cache)
                except Exception as exc:
                    warnings.append(f"Список миров из встроенного снимка: {exc}")
            leves = available(data, settings.job, settings.level, all_unlocked)
            ids = market_ids(data, leves, settings)
            market = Market(cache)
            prices = demo_prices(data, ids) if self.demo else market.fetch(scope, ids, force, progress)
            warnings.extend(market.warnings)
            old = sum(1 for t in market.uploads.values() if t and time.time() * 1000 - t > 86400000)
            if old:
                warnings.append(f"У {old} предметов обновление Universalis старше суток; проверьте предложения в игре.")
            progress("Считаю стаки, крафты и повторения…")
            plans = recommend(data, leves, prices, settings)
            return ("plans", data, plans, settings, scope_label, market.uploads, warnings, new_scopes)
        self.run_worker(work)

    def update_catalog(self):
        progress = lambda s: self.messages.put(("progress", s))
        self.run_worker(lambda: ("catalog", refresh(ROOT / "cache" / "catalog.json", progress)))

    def poll(self):
        try:
            while True:
                kind, result = self.messages.get_nowait()
                if kind == "progress":
                    self.status.set(result)
                    continue
                self.busy = False
                self.calculate_button.configure(state="normal")
                self.refresh_button.configure(state="normal")
                if kind == "error":
                    self.status.set("Не удалось выполнить действие; предыдущие результаты сохранены.")
                    messagebox.showerror("Ошибка", result)
                elif result[0] == "catalog":
                    self.data = result[1]
                    self.status.set(self.catalog_status())
                else:
                    _, self.data, self.plans, self.settings, self.result_scope, self.uploads, warnings, new_scopes = result
                    if new_scopes:
                        self.scope_map = new_scopes
                        self.scope_box.configure(values=list(new_scopes))
                    self.render_results()
                    prefix = "DEMO — искусственные цены. " if self.demo else ""
                    self.warning.set(prefix + (" ".join(warnings)[:1800] if warnings else "До 100 предложений на предмет и качество; кэш 15 минут. Даты стаков — в деталях. Фильтры и сортировка не обновляют цены."))
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def choose_priority(self, event=None):
        self.sort_column, self.sort_descending = PRIORITIES[self.priority.get()]
        self.render_results()

    def sort_by_column(self, column):
        self.sort_descending = not self.sort_descending if self.sort_column == column else column in ("balance", "reward")
        self.sort_column = column
        self.priority.set("По столбцу")
        self.render_results()

    def reset_filters(self):
        self.method_filter.set("Все")
        self.quality_filter.set("Все")
        self.only_feasible.set(False)
        self.search.set("")
        self.priority.set("Максимальная прибыль")
        self.choose_priority()

    def render_results(self):
        if not hasattr(self, "table"):
            return
        selected = self.table.selection()
        self.visible_indices = order_indices(self.plans, self.sort_column, self.sort_descending,
            self.method_filter.get(), self.quality_filter.get(), self.only_feasible.get(), self.search.get())
        self.table.delete(*self.table.get_children())
        for index in self.visible_indices:
            p = self.plans[index]
            self.table.insert("", "end", iid=str(index), values=(f"{p.leve['name']} · {p.per_accept}/прин.",
                p.leve["level"], p.method, p.quality, p.progress.turnins, p.progress.accepts, p.allowances,
                money(-p.cost if p.cost is not None else None, True), money(p.reward, True),
                money(p.balance, True), "Доступно" if p.feasible else "Цена неизвестна" if p.cost is None else "Лимиты"))
        for column, title in self.column_titles.items():
            arrow = (" ▼" if self.sort_descending else " ▲") if column == self.sort_column else ""
            self.table.heading(column, text=title + arrow)
        if self.settings:
            pending = " · Настройка HQ изменена: нажмите «Найти выгодные leves»" if self.craft_hq.get() != self.settings.craft_hq else ""
            self.status.set(f"Цель Lv {self.settings.level} → {self.plans[0].progress.target if self.plans else '?'} · показано {len(self.visible_indices)} из {len(self.plans)} · доступно {sum(p.feasible for p in self.plans)} · {self.catalog_status()}{pending}")
        if self.visible_indices:
            chosen = selected[0] if selected and selected[0] in self.table.get_children() else str(self.visible_indices[0])
            self.table.selection_set(chosen)
            self.table.focus(chosen)
            self.table.see(chosen)
            self.show_details()
        elif self.settings:
            if self.method_filter.get() == "Крафт" and self.quality_filter.get() == "HQ" and not self.settings.craft_hq:
                instruction = ("Галочка уже включена. Теперь нажмите «Найти выгодные leves», чтобы рассчитать HQ-крафт."
                               if self.craft_hq.get() else
                               "Включите «Могу гарантированно крафтить HQ» и нажмите «Найти выгодные leves».")
                self.set_details("HQ-крафт не включён в текущий расчёт.\n\n" + instruction +
                                 "\n\nЭтот вариант предполагает получение HQ при каждом крафте из покупных NQ ингредиентов. "
                                 "Если HQ не гарантирован, выберите качество NQ или способ «Покупка».")
            else:
                self.set_details("Нет вариантов по выбранным фильтрам. Нажмите «Сброс», чтобы показать все результаты.")

    def show_details(self, event=None):
        selected = self.table.selection()
        if selected and self.settings:
            self.set_details(describe(self.plans[int(selected[0])], self.data, self.settings,
                                      self.result_scope, self.uploads, self.demo))

    def export(self):
        if not self.plans:
            messagebox.showinfo("Экспорт", "Сначала выполните расчёт.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", initialfile="leve_plans.csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["DEMO", "Рынок", "Leve ID", "Название", "Уровень", "Способ", "Качество", "Сдач/принятие", "Принятия", "Сдачи", "Allowances", "EXP", "Расход (−)", "Награда (+)", "Баланс (+ прибыль / − убыток)", "Ограничения"])
                for index in self.visible_indices:
                    p = self.plans[index]
                    writer.writerow([self.demo, self.result_scope, p.leve["id"], p.leve["name"], p.leve["level"], p.method,
                                     p.quality, p.per_accept, p.progress.accepts, p.progress.turnins, p.allowances,
                                     p.progress.xp, -p.cost if p.cost is not None else None, p.reward, p.balance, "; ".join(p.problems)])
            self.status.set(f"CSV сохранён: {path}")
        except OSError as exc:
            messagebox.showerror("Экспорт не удался", str(exc))


def launch(demo=False):
    root = tk.Tk()
    Application(root, demo)
    root.mainloop()
