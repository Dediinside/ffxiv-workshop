"""Dark desktop interface; workers communicate with Tk through a queue."""
import copy
import csv
import json
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

from . import __version__, analysis, catalog
from .market import Market
from .net import JsonCache
from .results import PRIORITIES, item_url, number, order_indices
from .settings import default_settings, load_settings, save_settings, settings_path as default_path

BG, PANEL, TEXT, MUTED, ACCENT = "#181c24", "#242b36", "#e5eaf2", "#a8b4c7", "#315d88"
GATHER = {"Miner", "Botanist", "Fisher"}


class Application:
    def __init__(self, root, demo=False, base_dir=None, settings_path=None):
        self.root, self.demo = root, demo
        self.base_dir = Path(base_dir or Path(__file__).resolve().parent.parent)
        self.settings_path = Path(settings_path or default_path())
        self.settings, warnings = load_settings(self.settings_path)
        self.busy, self.rows, self.catalog = False, [], None
        self.queue = queue.Queue()
        self.cancel = threading.Event()
        self.generation, self.closed, self.save_timer = 0, False, None
        self.selected = None
        self.analyzed_scope = None
        self.sort_column, self.descending = "profit", True
        self.vars, self.profiles = {}, {}
        self.scopes = json.loads((self.base_dir / "data" / "scopes.json").read_text(encoding="utf-8"))
        self.worlds = [label for label in self.scopes if label.startswith("Мир")]
        self.root.title("FFXIV Market Scout " + __version__ + (" · DEMO" if demo else ""))
        self.root.geometry(self.settings.get("geometry", "1400x860"))
        self.root.minsize(1040, 670)
        self.theme()
        self.build()
        self.status.set("; ".join(warnings) or "Укажите реальные уровни профессий, затем запустите анализ.")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self.poll)
        self.root.after(30000, self.update_windows)

    def theme(self):
        self.root.configure(bg=BG)
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=TEXT, fieldbackground=PANEL,
                        bordercolor="#404a59", lightcolor=PANEL, darkcolor=BG)
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=27)
        style.configure("Treeview.Heading", background="#303a49", foreground=TEXT)
        style.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", "#ffffff")])
        style.map("TCombobox", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", TEXT)],
                  selectbackground=[("readonly", PANEL)], selectforeground=[("readonly", TEXT)])
        style.map("TButton", background=[("active", ACCENT)])
        style.map("TNotebook.Tab", background=[("selected", PANEL)])
        self.root.option_add("*TCombobox*Listbox.background", PANEL)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)

    def variable(self, key, default=""):
        value = self.settings.get(key, default)
        var = tk.StringVar(self.root, "" if value is None else str(value))
        self.vars[key] = var
        return var

    def entry(self, parent, var, width=10):
        widget = ttk.Entry(parent, textvariable=var, width=width)
        widget.bind("<FocusOut>", self.schedule_save)
        widget.bind("<Return>", self.schedule_save)
        return widget

    def scope_label(self, saved, sale=False):
        options = self.worlds if sale else list(self.scopes)
        for label in options:
            name = label.split(" · ", 1)[1].split(" (")[0]
            if str(saved) in (name, str(self.scopes[label]), label):
                return label
        return str(saved)

    def build(self):
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)
        if self.demo:
            ttk.Label(outer, text="DEMO: искусственные цены и продажи. Это проверка интерфейса, не торговая рекомендация.",
                      foreground="#f3c66c").pack(anchor="w", pady=(0, 8))
        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True)
        main, jobs, costs = [ttk.Frame(notebook, padding=10) for _ in range(3)]
        for frame, title in zip((main, jobs, costs), ("Рекомендации", "Профессии", "Расходы и настройки")):
            notebook.add(frame, text=title)
        controls = ttk.Frame(main)
        controls.pack(fill="x")
        for column, (key, title, options, sale) in enumerate((
            ("sale_world", "Мир продажи", self.worlds, True),
            ("purchase_scope", "Закупка материалов", list(self.scopes), False))):
            ttk.Label(controls, text=title).grid(row=0, column=column, sticky="w")
            var = self.variable(key)
            var.set(self.scope_label(self.settings[key], sale))
            cb = ttk.Combobox(controls, textvariable=var, values=options, state="readonly", width=30)
            cb.grid(row=1, column=column, padx=(0, 10), sticky="w")
            cb.bind("<<ComboboxSelected>>", self.schedule_save)
        for column, (key, title) in enumerate((("budget", "Бюджет gil (пусто = без лимита)"),
                                                ("batch_size", "Партия, шт.")), 2):
            ttk.Label(controls, text=title).grid(row=0, column=column, sticky="w")
            self.entry(controls, self.variable(key), 21 if key == "budget" else 9).grid(row=1, column=column, padx=(0, 10), sticky="w")
        self.force = tk.BooleanVar(self.root, False)
        ttk.Checkbutton(controls, text="Свежие цены", variable=self.force).grid(row=1, column=4)
        buttons = ttk.Frame(main)
        buttons.pack(fill="x", pady=10)
        self.analyze_button = ttk.Button(buttons, text="Анализировать рынок", command=self.analyze)
        self.analyze_button.pack(side="left")
        self.cancel_button = ttk.Button(buttons, text="Отмена", command=self.stop, state="disabled")
        self.cancel_button.pack(side="left", padx=8)
        ttk.Button(buttons, text="Сохранить настройки", command=self.save).pack(side="left")
        ttk.Button(buttons, text="Экспорт CSV", command=self.export).pack(side="left", padx=8)
        ttk.Button(buttons, text="Открыть товар в Universalis ↗", command=self.open_selected).pack(side="right")
        filters = ttk.Frame(main)
        filters.pack(fill="x", pady=(0, 8))
        self.filters = {}
        for key, title, values in (("priority", "Приоритет", list(PRIORITIES)),
                                   ("mode", "Источник", ["Все", "Крафт", "Сбор"]),
                                   ("job", "Профессия", ["Все"] + list(catalog.JOBS)),
                                   ("quality", "Качество", ["Все", "NQ", "HQ"])):
            ttk.Label(filters, text=title).pack(side="left", padx=(0, 4))
            initial = values[0]
            if key == "mode":
                initial = {"all": "Все", "craft": "Крафт", "gather": "Сбор"}[self.settings['analysis_mode']]
            elif key == "job":
                initial = "Все" if self.settings['analysis_job'] == 'all' else self.settings['analysis_job']
            var = tk.StringVar(self.root, initial)
            self.filters[key] = var
            cb = ttk.Combobox(filters, textvariable=var, values=values, width=21 if key == "priority" else 12, state="readonly")
            cb.pack(side="left", padx=(0, 8))
            cb.bind("<<ComboboxSelected>>", self.filter_change)
        extras = ttk.Frame(main)
        extras.pack(fill="x", pady=(0, 8))
        for key, title in (("search", "Название / поиск в каталоге"), ("min_profit", "Мин. прибыль gil")):
            ttk.Label(extras, text=title).pack(side="left", padx=(0, 4))
            var = tk.StringVar(self.root, self.settings.get("catalog_search", "") if key == "search" else "0")
            self.filters[key] = var
            if key == "search":
                self.vars["catalog_search"] = var
            entry = ttk.Entry(extras, textvariable=var, width=28 if key == "search" else 12)
            entry.pack(side="left", padx=(0, 10))
            entry.bind("<KeyRelease>", self.filter_change)
            entry.bind("<FocusOut>", self.schedule_save)
        self.complete = tk.BooleanVar(self.root, False)
        ttk.Checkbutton(extras, text="Только известная себестоимость", variable=self.complete, command=self.redraw).pack(side="left")
        ttk.Button(extras, text="Сбросить фильтры", command=self.reset_filters).pack(side="right")
        demand = ttk.Frame(main)
        demand.pack(fill="x", pady=(0, 8))
        for key, title, default in (("min_units_day", "Мин. шт./день", 1),
                                     ("min_deals_week", "Мин. покупок за 7 дней", 3),
                                     ("max_sale_age_hours", "Последняя продажа ≤ часов", 72)):
            ttk.Label(demand, text=title).pack(side="left", padx=(0, 4))
            var = self.variable(key, default)
            self.entry(demand, var, 7).pack(side="left", padx=(0, 12))
        self.filters["min_sales"] = self.vars["min_units_day"]
        self.show_illiquid = tk.BooleanVar(self.root, self.settings.get("show_illiquid", False))
        ttk.Checkbutton(demand, text="Показать малоликвидные для сравнения", variable=self.show_illiquid,
                        command=self.schedule_save).pack(side="left")
        self.status = tk.StringVar(self.root)
        ttk.Label(main, textvariable=self.status, wraplength=1280, foreground=MUTED).pack(fill="x", pady=(0, 6))
        self.result_note = tk.StringVar(self.root)
        ttk.Label(main, textvariable=self.result_note, wraplength=1280, foreground="#f3c66c").pack(fill="x", pady=(0, 6))
        panes = ttk.Panedwindow(main, orient="vertical")
        panes.pack(fill="both", expand=True)
        table_frame, details = ttk.Frame(panes), ttk.Frame(panes)
        panes.add(table_frame, weight=3)
        panes.add(details, weight=2)
        self.columns = [("name", "Товар ↗", 235), ("job", "Профессия", 95), ("level", "Lv", 40),
                        ("quality", "HQ/NQ", 55), ("units", "Шт.", 45), ("sales_day", "Шт./день", 75),
                        ("transactions_day", "Покупок/день", 90), ("competing_units", "Конкуренты, шт.", 110),
                        ("price", "Цена/шт.", 90), ("cost", "Расход партии", 100),
                        ("revenue", "Выручка партии", 100), ("profit", "Прибыль партии", 105),
                        ("margin", "ROI %", 65), ("hourly", "gil/час", 90),
                        ("window_wait", "До окна, мин.", 100), ("status", "Статус", 190),
                        ("confidence", "Данные", 110)]
        self.table = ttk.Treeview(table_frame, columns=[c[0] for c in self.columns], show="headings", selectmode="browse")
        for key, title, width in self.columns:
            self.table.heading(key, text=title, command=lambda k=key: self.sort(k))
            self.table.column(key, width=width, minwidth=40, anchor="w" if key in {"name", "job", "status", "confidence"} else "e", stretch=False)
        self.table.grid(row=0, column=0, sticky="nsew")
        sy = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        sx = ttk.Scrollbar(table_frame, orient="horizontal", command=self.table.xview)
        sy.grid(row=0, column=1, sticky="ns")
        sx.grid(row=1, column=0, sticky="ew")
        self.table.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.table.bind("<<TreeviewSelect>>", self.show_detail)
        self.table.bind("<Double-1>", lambda e: self.open_selected())
        self.detail = tk.Text(details, bg=PANEL, fg=TEXT, insertbackground=TEXT, selectbackground=ACCENT,
                              relief="flat", wrap="word", padx=10, pady=8, height=10)
        self.detail.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(details, command=self.detail.yview)
        scrollbar.pack(side="right", fill="y")
        self.detail.configure(yscrollcommand=scrollbar.set, state="disabled")
        self.build_measure(main)
        self.build_profiles(jobs)
        self.build_costs(costs)
        ttk.Label(outer, text="Цены Universalis обновляются игроками. Прибыль — оценка; доступность рецептов и спрос проверяйте в игре.",
                  foreground=MUTED).pack(anchor="w", pady=(8, 0))

    def build_profiles(self, parent):
        ttk.Label(parent, text="Настройки всех профессий сохраняются автоматически. Lv 1 по умолчанию: впишите уровни персонажа.").pack(anchor="w", pady=(0, 12))
        frame = ttk.Frame(parent)
        frame.pack(anchor="w")
        headings = ["Профессия", "Включена", "Уровень", "Могу HQ", "Книги открыты", "Специалист"]
        for column, title in enumerate(headings):
            ttk.Label(frame, text=title).grid(row=0, column=column, padx=9, pady=5)
        for row, job in enumerate(catalog.JOBS, 1):
            profile = self.settings["professions"][job]
            fields = {}
            self.profiles[job] = fields
            ttk.Label(frame, text=job).grid(row=row, column=0, sticky="w", padx=9, pady=5)
            for column, key in enumerate(("enabled", "level", "hq", "books", "specialist"), 1):
                var = tk.StringVar(self.root, str(profile[key])) if key == "level" else tk.BooleanVar(self.root, profile[key])
                fields[key] = var
                if key == "level":
                    self.entry(frame, var, 7).grid(row=row, column=column, padx=9)
                elif job not in GATHER or key in {"enabled", "books"}:
                    ttk.Checkbutton(frame, variable=var, command=self.schedule_save).grid(row=row, column=column)
        ttk.Label(parent, text="HQ: только если можете стабильно крафтить HQ. Книги: допуск к рецептам/ресурсам с особыми условиями; проверяйте конкретные условия в деталях.",
                  wraplength=1100, foreground=MUTED).pack(anchor="w", pady=12)
        rates = ttk.Frame(parent)
        rates.pack(anchor="w")
        labels = ["Сборщик", "Шт./час (обычный сбор)", "Доп. gil/шт.", "Урожай/окно", "Приманка gil/рыбу"]
        for column, title in enumerate(labels):
            ttk.Label(rates, text=title).grid(row=0, column=column, padx=10, pady=5)
        for row, job in enumerate(("Miner", "Botanist", "Fisher"), 1):
            ttk.Label(rates, text=job).grid(row=row, column=0, sticky="w")
            for column, key in enumerate(("rate", "per_unit", "window_yield", "bait_per_unit"), 1):
                value = self.settings["professions"][job].get(key)
                var = tk.StringVar(self.root, "" if value is None else str(value))
                self.profiles[job][key] = var
                self.entry(rates, var, 16).grid(row=row, column=column, padx=10, pady=5)
        ttk.Label(parent, text="Неизвестная скорость = неизвестная прибыль/час. Для рыбы сохраняйте замер конкретного товара под таблицей рекомендаций.",
                  foreground=MUTED).pack(anchor="w", pady=10)
        ttk.Button(parent, text="Сохранить профессии", command=self.save).pack(anchor="w")

    def build_costs(self, parent):
        ttk.Label(parent, text="Расходы gil на всю партию. Нулевое значение означает, что соответствующий расход не учтён.").pack(anchor="w", pady=(0, 10))
        frame = ttk.Frame(parent)
        frame.pack(anchor="w")
        fields = [("buy_tax", "Налог покупки %"), ("sell_tax", "Налог продажи %"), ("food", "Еда gil/партию"),
                  ("medicine", "Зелья gil/партию"), ("repair", "Ремонт gil/партию"), ("travel", "Дорога gil/партию"),
                  ("other", "Прочее gil/партию"), ("craft_per", "Доп. gil/крафт"),
                  ("scan_limit", "Лимит первичного сканирования"), ("detail_limit", "Подробно рассчитать кандидатов")]
        for row, (key, label) in enumerate(fields):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
            self.entry(frame, self.variable(key), 18).grid(row=row, column=1, sticky="w")
        ttk.Label(parent, text="Расходники по рыночной цене: количество на партию. Не включайте одновременно ту же еду/зелье в фиксированную сумму выше.",
                  foreground=MUTED, wraplength=1100).pack(anchor="w", pady=12)
        self.consumables = copy.deepcopy(self.settings.get("consumables", []))
        self.consumable_table = ttk.Treeview(parent, columns=("item", "quantity", "hq", "mode"), show="headings", height=4)
        for key, title in (("item", "ID товара ↗"), ("quantity", "Количество"), ("hq", "HQ"), ("mode", "Источник")):
            self.consumable_table.heading(key, text=title)
        self.consumable_table.pack(fill="x")
        self.consumable_table.bind("<Double-1>", self.open_consumable)
        bar = ttk.Frame(parent)
        bar.pack(anchor="w", pady=8)
        self.consumable_vars = {"item": tk.StringVar(self.root), "quantity": tk.StringVar(self.root, "1"),
                                "hq": tk.BooleanVar(self.root, False), "mode": tk.StringVar(self.root, "all")}
        for key, label in (("item", "ID"), ("quantity", "Шт.")):
            ttk.Label(bar, text=label).pack(side="left", padx=5)
            ttk.Entry(bar, textvariable=self.consumable_vars[key], width=10).pack(side="left")
        ttk.Checkbutton(bar, text="HQ", variable=self.consumable_vars["hq"]).pack(side="left", padx=8)
        ttk.Combobox(bar, textvariable=self.consumable_vars["mode"], values=("all", "craft", "gather"), state="readonly", width=9).pack(side="left")
        ttk.Button(bar, text="Добавить", command=self.add_consumable).pack(side="left", padx=8)
        ttk.Button(bar, text="Удалить выбранный", command=self.remove_consumable).pack(side="left")
        self.render_consumables()
        ttk.Label(parent, text="all = все варианты; craft = крафт; gather = сбор. Настройки и кэш: " + str(self.settings_path.parent),
                  foreground=MUTED, wraplength=1100).pack(anchor="w", pady=8)
        ttk.Button(parent, text="Сохранить настройки", command=self.save).pack(anchor="w")

    def build_measure(self, parent):
        bar = ttk.LabelFrame(parent, text="Личный замер выделенного ресурса (сохраняется по ID)", padding=6)
        bar.pack(fill="x", pady=(8, 0))
        self.measure = {key: tk.StringVar(self.root, "") for key in ("minutes", "units", "bait", "yield")}
        for key, label in (("minutes", "Минут"), ("units", "Собрано шт."), ("bait", "Приманки gil всего (пусто = ? )"), ("yield", "Урожай/окно")):
            ttk.Label(bar, text=label).pack(side="left", padx=(0, 4))
            ttk.Entry(bar, textvariable=self.measure[key], width=9).pack(side="left", padx=(0, 10))
        ttk.Button(bar, text="Сохранить замер", command=self.save_measure).pack(side="left")

    @staticmethod
    def numeric(text, key, integer=False, nullable=False):
        text = str(text).strip().replace(" ", "").replace(",", ".")
        if not text and nullable:
            return None
        try:
            number_value = float(text)
            if number_value < 0 or number_value != number_value or number_value == float("inf"):
                raise ValueError()
            if integer and not number_value.is_integer():
                raise ValueError()
            return int(number_value) if integer else number_value
        except ValueError:
            raise ValueError(f"{key}: введите неотрицательное число") from None

    def collect(self):
        result = copy.deepcopy(self.settings)
        for key, var in self.vars.items():
            text = var.get()
            if key == "catalog_search":
                result[key] = text.strip()
            elif key in {"sale_world", "purchase_scope"}:
                if text not in self.scopes:
                    raise ValueError("Выберите доступный мир/область закупки")
                result[key] = text.split(" · ", 1)[1].split(" (")[0]
            else:
                result[key] = self.numeric(text, key, integer=key in {"batch_size", "scan_limit", "detail_limit", "min_deals_week"}, nullable=key == "budget")
        result["show_illiquid"] = self.show_illiquid.get()
        result["analysis_mode"], result["analysis_job"] = self.current_scope()
        for key in ("batch_size", "scan_limit", "detail_limit"):
            if result[key] < 1:
                raise ValueError(f"{key}: значение должно быть больше нуля")
        for key in ("buy_tax", "sell_tax"):
            if result[key] > 20:
                raise ValueError("Налог должен быть от 0 до 20%")
        for job, fields in self.profiles.items():
            for key, var in fields.items():
                if key in {"enabled", "hq", "books", "specialist"}:
                    result["professions"][job][key] = var.get()
                else:
                    result["professions"][job][key] = self.numeric(var.get(), f"{job}: {key}", integer=key in {"level", "window_yield"}, nullable=key == "bait_per_unit")
            if not 1 <= result["professions"][job]["level"] <= 100:
                raise ValueError(f"{job}: уровень должен быть от 1 до 100")
        result["consumables"] = copy.deepcopy(self.consumables)
        result["geometry"] = self.root.winfo_geometry()
        return result

    def save(self, quiet=False):
        try:
            self.settings = self.collect()
            save_settings(self.settings_path, self.settings)
            if not quiet:
                self.status.set("Настройки профессий, рынков и расходов сохранены.")
            return True
        except (ValueError, OSError) as exc:
            self.status.set(str(exc))
            if not quiet:
                messagebox.showerror("Настройки", str(exc), parent=self.root)
            return False

    def schedule_save(self, event=None):
        if self.save_timer is not None:
            self.root.after_cancel(self.save_timer)
        self.save_timer = self.root.after(450, lambda: self.save(quiet=True))

    def analyze(self):
        if self.busy or not self.save():
            return
        self.busy = True
        self.generation += 1
        token, settings = self.generation, copy.deepcopy(self.settings)
        self.cancel = threading.Event()
        cancel, force = self.cancel, self.force.get()
        messages, base_dir = self.queue, self.base_dir
        cache_path, demo = self.settings_path.parent / "cache", self.demo
        self.analyze_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.status.set("Загружаю каталог и анализирую рынок…")
        def worker():
            try:
                data = catalog.load(base_dir / "data" / "catalog.json")
                market = Market(JsonCache(cache_path))
                rows, summary = analysis.analyze(data, settings, market, demo=demo, force=force,
                                                 progress=lambda s: messages.put(("progress", token, str(s))), cancel=cancel)
                messages.put(("done", token, rows, summary, data))
            except Exception as exc:
                messages.put(("error", token, str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def stop(self):
        self.cancel.set()
        self.status.set("Отмена запрошена; текущий запрос завершится перед остановкой.")

    def poll(self):
        if self.closed:
            return
        try:
            while True:
                event = self.queue.get_nowait()
                if event[1] != self.generation:
                    continue
                if event[0] == "progress":
                    self.status.set(event[2])
                else:
                    self.busy = False
                    self.analyze_button.configure(state="normal")
                    self.cancel_button.configure(state="disabled")
                    if event[0] == "done":
                        self.rows, summary, self.catalog = event[2:]
                        self.summary = summary
                        self.analyzed_scope = (summary.get('analysis_mode', 'all'), summary.get('analysis_job', 'all'))
                        warnings = summary.get("warnings", [])
                        self.status.set(f"Доступно {summary.get('eligible', '?')} · просмотрено {summary.get('screened', '?')} · подробно {summary.get('detailed', '?')}" +
                                        (" · " + "; ".join(map(str, warnings)) if warnings else ""))
                        self.redraw()
                    else:
                        self.status.set("Анализ не завершён: " + event[2])
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def filter_change(self, event=None):
        if event is not None and getattr(event, "widget", None) is not None:
            if event.widget.cget("textvariable") == str(self.filters["priority"]):
                self.sort_column, self.descending = PRIORITIES[self.filters["priority"].get()]
        self.schedule_save()
        self.redraw()

    def current_scope(self):
        return ({"Все": "all", "Крафт": "craft", "Сбор": "gather"}[self.filters['mode'].get()],
                'all' if self.filters['job'].get() == 'Все' else self.filters['job'].get())

    def reset_filters(self):
        for key in ('mode', 'job', 'quality'):
            self.filters[key].set('Все')
        self.filters['search'].set('')
        self.filters['min_profit'].set('')
        self.complete.set(False)
        self.filter_change()

    def sort(self, column):
        self.descending = not self.descending if column == self.sort_column else column not in {"name", "job", "level", "cost", "competing_units"}
        self.sort_column = column
        self.redraw()

    def visible_indices(self):
        values = {key: var.get() for key, var in self.filters.items() if key != "priority"}
        values["min_sales"] = self.numeric(values["min_sales"], "Продажи", nullable=True) or 0
        if self.show_illiquid.get():
            values["min_sales"] = 0
        values["min_profit"] = self.numeric(values["min_profit"], "Прибыль", nullable=True)
        return order_indices(self.rows, self.sort_column, self.descending, only_complete=self.complete.get(), **values)

    def redraw(self):
        try:
            indices = self.visible_indices()
        except ValueError:
            return
        self.table.delete(*self.table.get_children())
        note = f"Показано {len(indices)} из {len(self.rows)} рассчитанных вариантов."
        outdated = self.analyzed_scope is not None and self.current_scope() != self.analyzed_scope
        if outdated:
            note += ' Отбор профессии/источника изменён: нажмите «Анализировать рынок» для нового поиска.'
        self.result_note.set(note)
        for index in indices:
            row = self.rows[index]
            values = []
            for key, _, _ in self.columns:
                value = row.get(key)
                if key in {"name", "job", "quality", "status", "confidence"}:
                    values.append("—" if value is None else str(value))
                else:
                    if key == "cost" and value is not None:
                        value = -value
                    if key == "window_wait" and value is not None:
                        value /= 60
                    values.append(number(value, signed=key in {"cost", "revenue", "profit", "hourly"}, digits=1 if key in {"sales_day", "transactions_day", "margin"} else 0))
            self.table.insert("", "end", iid=str(index), values=values)
        if self.selected is not None and str(self.selected) in self.table.get_children():
            self.table.selection_set(str(self.selected))
        else:
            self.selected = None
            if self.rows:
                text = 'Нет строк по выбранным фильтрам. '
                text += ('Выберите профессию и источник, затем нажмите «Анализировать рынок»: старый расчёт использовал другой отбор.'
                         if outdated else 'Очистите минимальную прибыль или нажмите «Сбросить фильтры», чтобы увидеть убытки и неизвестную себестоимость.')
            elif self.analyzed_scope is not None:
                text = ('В рассмотренной выборке нет вариантов с подтверждённым спросом. Проверьте уровни и доступ профессий; '
                        'для разбора включите «Показать малоликвидные для сравнения», очистите минимальную прибыль и повторите анализ.')
            else:
                text = 'Запустите анализ. Для сбора прибыль/час появится после личного замера скорости.'
            self.set_detail(text)

    def set_detail(self, text, links=()):
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("end", text)
        for index, (label, item) in enumerate(links):
            tag = f"link{index}"
            self.detail.insert("end", "\n" + label + " ↗", tag)
            self.detail.tag_configure(tag, foreground="#83c5ff", underline=True)
            self.detail.tag_bind(tag, "<Button-1>", lambda e, i=item: self.open_item(i))
            self.detail.tag_bind(tag, "<Enter>", lambda e: self.detail.configure(cursor="hand2"))
            self.detail.tag_bind(tag, "<Leave>", lambda e: self.detail.configure(cursor=""))
        self.detail.configure(state="disabled")

    def show_detail(self, event=None):
        selected = self.table.selection()
        if not selected:
            return
        self.selected = int(selected[0])
        row = self.rows[self.selected]
        self.render_detail(row)
        for var in self.measure.values():
            var.set("")

    def render_detail(self, row):
        text = row.get("details", "")
        if not text:
            text = f"{row['name']} · {row['job']} Lv {row['level']} · {row['quality']}\n"
            text += "\n".join(row.get("issues", []) + row.get("warnings", []))
        source = row.get("_source")
        if source is not None:
            current = analysis.schedule_text(source)
            original = row.get("windows_text", "")
            if original and original in text:
                text = text.replace(original, current)
            else:
                text += "\n" + current
        text += "\n\nНеизвестные значения обозначены «—». Расходники и личные замеры меняются во вкладках настроек."
        text += "\nЦены и статистика продаж относятся к моменту анализа; расписание обновляется каждые 30 секунд."
        links = [(row["name"], row["item_id"])]
        for entry in row.get("inputs", []):
            item = entry.get("item_id", entry.get("item"))
            if item is not None:
                links.append((entry.get("name", f"Ингредиент #{item}"), item))
        self.set_detail(text, links)

    def update_windows(self):
        """Advance only timetable cells; price data and user measurement stay intact."""
        if self.closed:
            return
        for index, row in enumerate(self.rows):
            source = row.get("_source")
            if row.get("mode") != "gather" or source is None:
                continue
            upcoming = analysis.next_window(source)
            wait = upcoming["wait"] if upcoming is not None else None
            row["window_wait"] = wait
            iid = str(index)
            if self.table.exists(iid):
                self.table.set(iid, "window_wait", number(wait / 60 if wait is not None else None))
        if self.selected is not None and self.selected < len(self.rows):
            self.render_detail(self.rows[self.selected])
        self.root.after(30000, self.update_windows)

    def open_item(self, item_id):
        webbrowser.open(item_url(item_id))

    def open_selected(self):
        selected = self.table.selection()
        if selected:
            self.open_item(self.rows[int(selected[0])]["item_id"])

    def save_measure(self):
        if self.selected is None or self.rows[self.selected]["mode"] != "gather":
            self.status.set("Выделите ресурс сборщика для сохранения замера.")
            return
        try:
            minutes = self.numeric(self.measure["minutes"].get(), "Минуты")
            units = self.numeric(self.measure["units"].get(), "Количество", integer=True)
            bait = self.numeric(self.measure["bait"].get(), "Приманки", nullable=True)
            window = self.numeric(self.measure["yield"].get(), "Урожай/окно", integer=True, nullable=True) or 0
            if not minutes or not units:
                raise ValueError("Минуты и собранное количество должны быть больше нуля")
            item = str(self.rows[self.selected]["item_id"])
            profiles = self.settings.setdefault("item_profiles", {})
            inherited = self.settings["professions"][self.rows[self.selected]["job"]].get("per_unit", 0)
            profiles[item] = {"rate": units / minutes * 60, "per_unit": profiles.get(item, {}).get("per_unit", inherited),
                              "window_yield": window, "bait_per_unit": None if bait is None else bait / units}
            if self.save(quiet=True):
                self.status.set("Замер ресурса сохранён. Перезапустите анализ для пересчёта gil/час.")
        except ValueError as exc:
            self.status.set(str(exc))

    def render_consumables(self):
        self.consumable_table.delete(*self.consumable_table.get_children())
        for index, row in enumerate(self.consumables):
            self.consumable_table.insert("", "end", iid=str(index), values=(row["item"], row["quantity"], "HQ" if row["hq"] else "NQ", row["mode"]))

    def add_consumable(self):
        try:
            item = self.numeric(self.consumable_vars["item"].get(), "ID", integer=True)
            quantity = self.numeric(self.consumable_vars["quantity"].get(), "Количество", integer=True)
            if item < 1 or quantity < 1:
                raise ValueError("ID и количество должны быть больше нуля")
            self.consumables.append({"item": item, "quantity": quantity, "hq": self.consumable_vars["hq"].get(), "mode": self.consumable_vars["mode"].get()})
            self.render_consumables()
            self.save(quiet=True)
        except ValueError as exc:
            self.status.set(str(exc))

    def remove_consumable(self):
        selected = self.consumable_table.selection()
        if selected:
            del self.consumables[int(selected[0])]
            self.render_consumables()
            self.save(quiet=True)

    def open_consumable(self, event=None):
        selected = self.consumable_table.selection()
        if selected:
            self.open_item(self.consumables[int(selected[0])]["item"])

    def export(self):
        if not self.rows:
            self.status.set("Сначала выполните анализ.")
            return
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".csv", initialfile="market_scout.csv", filetypes=[("CSV", "*.csv")])
        if path:
            try:
                keys = ["window_wait_minutes" if c[0] == "window_wait" else c[0] for c in self.columns] + ["universalis"]
                with open(path, "w", encoding="utf-8-sig", newline="") as stream:
                    writer = csv.DictWriter(stream, keys, delimiter=";")
                    writer.writeheader()
                    for index in self.visible_indices():
                        row = self.rows[index]
                        record = {key: row.get(key) for key in keys[:-1]}
                        if row.get("cost") is not None:
                            record["cost"] = -row["cost"]
                        wait = row.get("window_wait")
                        record["window_wait_minutes"] = wait / 60 if wait is not None else None
                        record["universalis"] = item_url(row["item_id"])
                        writer.writerow(record)
                self.status.set("CSV сохранён: " + str(path))
            except (OSError, ValueError) as exc:
                self.status.set(str(exc))

    def close(self):
        self.save(quiet=True)
        self.closed = True
        self.cancel.set()
        self.root.destroy()


def main(demo=False):
    root = tk.Tk()
    Application(root, demo=demo)
    root.mainloop()
