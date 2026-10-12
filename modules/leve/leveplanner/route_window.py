"""Independent route window; main five-level results and inputs are left intact."""
import csv
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

from .catalog import JOBS
from .market import Market
from .net import JsonCache
from .planner import Settings
from .results import money
from .route import build_route, route_market_ids, validate_request

PRIORITIES = {'Дешевле закупка': 'cost', 'Меньше allowances': 'allowances'}


def route_summary(route, settings, budget, demo=False):
    fmt = lambda n: f'{n:,}'.replace(',', ' ')
    accepts = sum(s.plan.progress.accepts for s in route.steps)
    turns = sum(s.plan.progress.turnins for s in route.steps)
    heading = '[DEMO — искусственные цены]\n' if demo else ''
    lines = [heading + f'{settings.job}: Lv {settings.level} → {route.target}',
             f'До цели нужно {fmt(route.needed_xp)} EXP; рассчитанные сдачи дадут {fmt(route.gained_xp)} EXP.',
             f'Итог: Lv {route.end_level}, EXP внутри уровня {fmt(route.end_xp)}.',
             f'Этапов: {len(route.steps)}; принятия leve: {accepts}; сдачи: {turns}.',
             f'Allowances: {route.allowances}; сейчас доступно: {settings.allowances}.',
             f'Полная стоимость маршрута: {money(-route.cost if route.cost is not None else None, True)} gil.',
             f'Расход рассчитанной части: {money(-route.spent, True)}; награда: {money(route.reward, True)} gil.',
             f'Баланс рассчитанной части: {money(route.reward - route.spent, True)} gil.',
             'Бюджет: ' + ('без лимита.' if budget is None else fmt(budget) + ' gil; сравнивается с полной закупкой заранее.'),
             'Маршрут полный.' if route.complete else 'Маршрут неполный: стоимость до цели неизвестна.']
    if route.issues:
        lines += ['', *route.issues]
    lines += ['', 'Выбор по этапам до открытия следующего уровня leves; сравниваются ближайшие уровни.',
              'Более старые задания рассматриваются, если ближайшие нельзя оценить. Это поэтапная рекомендация,',
              'а не доказанный самый дешёвый маршрут среди всех сочетаний заданий.',
              'EXP крафта, случайные награды и время поездок не добавлены. Дорога начисляется на каждый этап.',
              'Закупленные стаки используются один раз; остатки переносятся между этапами.',
              'Цены — текущий снимок, будущие предложения не гарантированы. Доступ к leve/NPC проверьте в игре.',
              'Для старта с Lv 1 ремесленника leves уже должны быть открыты на персонаже.']
    return '\n'.join(lines)


def step_details(step, data):
    p = step.plan
    name = lambda item: data['items'][item]['name']
    lines = [f'Lv {step.start_level} (EXP {step.start_xp:,}) → Lv {p.progress.end_level} (EXP {p.progress.end_xp:,})',
             f'{p.leve["name"]} · Lv {p.leve["level"]} · {p.leve["location"]}',
             f'{p.method} {p.quality}; принять {p.progress.accepts} раз; сдач {p.progress.turnins}; allowances {p.allowances}',
             f'До {p.per_accept} сдач на принятие; последнее принятие может быть неполным.',
             f'EXP {p.progress.xp:,}; расход {money(-p.cost, True)}, награда {money(p.reward, True)}, баланс {money(p.balance, True)} gil.',
             '', 'Сдать:']
    for item, qty in p.leve['items'].items():
        lines.append(f'{name(item)} #{item}: {qty} × {p.progress.turnins} = {qty * p.progress.turnins} {p.quality}')
        lines.append(f'https://universalis.app/market/{item}')
    if step.used:
        lines += ['', 'Использовать остатки предыдущих этапов:']
        lines += [f'{name(item)} {"HQ" if hq else "NQ"}: {qty}' for (item, hq), qty in step.used.items() if qty]
    if p.crafts:
        lines += ['', 'Крафт:']
        for c in p.crafts:
            lines.append(f'{name(c["item"])}: рецепт #{c["recipe"]["id"]}, Lv {c["recipe"]["level"]}; {c["count"]} крафтов → {c["produced"]} шт.')
        lines.append('Рецепты доступны на начале этапа. EXP самого крафта не учтён. HQ предполагает гарантию HQ.')
    lines += ['', 'Закупить целые стаки:']
    for s in p.shopping:
        if not s['purchase'].quantity:
            continue
        buy = s['purchase']
        lines.append(f'{name(s["item"])} #{s["item"]}: нужно докупить {s["required"]}, купить {buy.quantity}; {money(-buy.cost, True)} gil')
        lines.append(f'https://universalis.app/market/{s["item"]}')
        lines += [f'  {l.quantity} × {l.price:,} · {"HQ" if l.hq else "NQ"} · {l.world}' for l in buy.stacks]
    if step.leftovers:
        lines += ['', 'Новые остатки для следующих этапов:']
        lines += [f'{name(item)} {"HQ" if hq else "NQ"}: {qty}' for (item, hq), qty in step.leftovers.items() if qty]
    return '\n'.join(lines)


class RouteWindow:
    def __init__(self, app):
        self.app, self.demo = app, app.demo
        self.root = tk.Toplevel(app.root)
        self.root.title('FFXIV · Маршрут прокачки' + (' · DEMO' if self.demo else ''))
        self.root.geometry('1260x830')
        self.root.minsize(1040, 700)
        self.messages, self.cancelled = queue.Queue(), threading.Event()
        self.busy, self.closed, self.result = False, False, None
        self.data = app.data
        self.vars = {key: tk.StringVar(self.root, value.get()) for key, value in app.vars.items()}
        level = int(self.vars['level'].get()) if self.vars['level'].get().isdigit() else 1
        self.target = tk.StringVar(self.root, str(min(self.data['level_cap'], max(50, level + 5))))
        self.scope_map = dict(app.scope_map)
        self.scope = tk.StringVar(self.root, app.scope.get())
        self.priority = tk.StringVar(self.root, 'Дешевле закупка')
        self.method, self.quality = tk.StringVar(self.root, 'Все'), tk.StringVar(self.root, 'Все')
        self.craft_hq, self.force = tk.BooleanVar(self.root, app.craft_hq.get()), tk.BooleanVar(self.root, False)
        wrap = ttk.Frame(self.root, padding=14)
        wrap.pack(fill='both', expand=True)
        ttk.Label(wrap, text='Маршрут от текущего до желаемого уровня', font=('Segoe UI', 17, 'bold')).pack(anchor='w')
        ttk.Label(wrap, text='Отдельный расчёт. EXP — число внутри текущего уровня. Пустой бюджет — без лимита.').pack(anchor='w', pady=(4, 10))
        form = ttk.Frame(wrap)
        form.pack(fill='x')
        for col, (label, var, values, width) in enumerate([
                ('Профессия', self.vars['job'], JOBS, 15), ('Мир / DC', self.scope, list(self.scope_map), 27),
                ('Текущий Lv', self.vars['level'], None, 9), ('Желаемый Lv', self.target, None, 10),
                ('EXP внутри Lv', self.vars['xp'], None, 13), ('Бюджет gil', self.vars['budget'], None, 13),
                ('Allowances', self.vars['allowances'], None, 10)]):
            app.field(form, label, col, var, values, width)
        options = ttk.Frame(wrap)
        options.pack(fill='x', pady=10)
        for label, var, values, width in [('Приоритет', self.priority, list(PRIORITIES), 20),
                                         ('Способ', self.method, ['Все', 'Покупка', 'Крафт'], 10),
                                         ('Качество', self.quality, ['Все', 'HQ', 'NQ'], 6)]:
            ttk.Label(options, text=label).pack(side='left', padx=(0, 4))
            ttk.Combobox(options, textvariable=var, values=values, state='readonly', width=width).pack(side='left', padx=(0, 12))
        for label, key in [('Налог %', 'tax'), ('Дорога на этап', 'travel')]:
            ttk.Label(options, text=label).pack(side='left', padx=(0, 4))
            ttk.Entry(options, textvariable=self.vars[key], width=7).pack(side='left', padx=(0, 10))
        actions = ttk.Frame(wrap)
        actions.pack(fill='x')
        self.button = ttk.Button(actions, text='Рассчитать маршрут', command=self.calculate)
        self.button.pack(side='left')
        self.cancel_button = ttk.Button(actions, text='Отмена', command=self.cancel, state='disabled')
        self.cancel_button.pack(side='left', padx=8)
        ttk.Button(actions, text='Экспорт маршрута CSV', command=self.export).pack(side='left', padx=8)
        ttk.Button(actions, text='Список крафта…', command=self.open_craft_list).pack(side='left', padx=8)
        ttk.Checkbutton(actions, text='Гарантированный HQ-крафт', variable=self.craft_hq).pack(side='left', padx=8)
        ttk.Checkbutton(actions, text='Свежие цены', variable=self.force).pack(side='left', padx=8)
        self.status = tk.StringVar(self.root, 'Выберите цель и нажмите «Рассчитать маршрут».')
        ttk.Label(wrap, textvariable=self.status, wraplength=1190).pack(fill='x', pady=10)
        pane = ttk.Panedwindow(wrap, orient='vertical')
        pane.pack(fill='both', expand=True)
        top, bottom = ttk.Frame(pane), ttk.Frame(pane)
        columns = ('levels', 'leve', 'location', 'method', 'turns', 'accepts', 'allowances', 'xp', 'cost', 'reward', 'balance')
        self.table = ttk.Treeview(top, columns=columns, show='headings', height=10)
        for key, title, width in zip(columns, ['Уровни', 'Leve', 'Место получения', 'Способ', 'Сдачи', 'Принятия', 'Allow.', 'EXP', 'Расход gil', 'Награда gil', 'Баланс gil'],
                                     [95, 210, 160, 105, 60, 70, 65, 100, 95, 95, 95]):
            self.table.heading(key, text=title)
            self.table.column(key, width=width, minwidth=50, stretch=False)
        self.table.grid(row=0, column=0, sticky='nsew')
        vertical = ttk.Scrollbar(top, orient='vertical', command=self.table.yview)
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal = ttk.Scrollbar(top, orient='horizontal', command=self.table.xview)
        horizontal.grid(row=1, column=0, sticky='ew')
        self.table.configure(xscrollcommand=horizontal.set, yscrollcommand=vertical.set)
        top.rowconfigure(0, weight=1)
        top.columnconfigure(0, weight=1)
        self.table.bind('<<TreeviewSelect>>', self.show_step)
        self.detail = tk.Text(bottom, wrap='word', font=('Consolas', 10), padx=10, pady=10)
        scroll = ttk.Scrollbar(bottom, command=self.detail.yview)
        self.detail.configure(yscrollcommand=scroll.set)
        self.detail.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        pane.add(top, weight=2)
        pane.add(bottom, weight=3)
        ttk.Button(wrap, text='Показать итог маршрута', command=self.show_summary).pack(anchor='w', pady=(8, 0))
        self.set_detail('Здесь появятся этапы прокачки, общая стоимость и список покупок. Основное окно продолжает считать привычные рубежи по 5 уровней.')
        self.timer = self.root.after(100, self.poll)
        self.root.protocol('WM_DELETE_WINDOW', self.close)

    def calculate(self):
        if self.busy:
            return
        try:
            budget = int(self.vars['budget'].get()) if self.vars['budget'].get().strip() else None
            settings = Settings(self.vars['job'].get(), int(self.vars['level'].get()), int(self.vars['xp'].get()),
                                budget if budget is not None else float('inf'), int(self.vars['allowances'].get()),
                                float(self.vars['tax'].get()), self.craft_hq.get(), int(self.vars['travel'].get()))
            target, priority = int(self.target.get()), PRIORITIES[self.priority.get()]
            method, quality = self.method.get(), self.quality.get()
            validate_request(self.data, settings, target, budget, priority, method, quality)
            scope_label, force = self.scope.get(), self.force.get()
            scope = self.scope_map[scope_label]
        except (ValueError, KeyError) as exc:
            messagebox.showerror('Проверьте маршрут', str(exc), parent=self.root)
            return
        self.busy = True
        self.cancelled.clear()
        self.button.configure(state='disabled')
        self.cancel_button.configure(state='normal')
        self.status.set('Загружаю цены для всего диапазона…')
        from .app import ROOT, demo_prices, get_catalog
        messages, cancel, demo, initial_data = self.messages, self.cancelled, self.demo, self.data
        progress = lambda text: messages.put(('progress', text))
        def work():
            try:
                data, warning = (initial_data, '') if demo else get_catalog(progress)
                ids = route_market_ids(data, settings, target)
                market = Market(JsonCache(ROOT / 'cache' / 'http'))
                prices = demo_prices(data, ids) if demo else market.fetch(scope, ids, force, progress)
                route = build_route(data, settings, target, prices, budget, priority, method, quality, progress, cancel.is_set)
                warnings = ([warning] if warning else []) + market.warnings
                old = sum(1 for timestamp in market.uploads.values() if timestamp and time.time() * 1000 - timestamp > 86400000)
                if old:
                    warnings.append(f'У {old} предметов обновление Universalis старше суток; проверьте предложения в игре.')
                messages.put(('result', (route, data, settings, budget, scope_label, warnings)))
            except Exception as exc:
                messages.put(('error', str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def cancel(self):
        self.cancelled.set()
        self.status.set('Отменяю расчёт после завершения текущего запроса/этапа…')

    def poll(self):
        if self.closed:
            return
        try:
            while True:
                kind, value = self.messages.get_nowait()
                if kind == 'progress':
                    if not self.cancelled.is_set():
                        self.status.set(value)
                    continue
                self.busy = False
                self.button.configure(state='normal')
                self.cancel_button.configure(state='disabled')
                if kind == 'error':
                    self.status.set(value + ' Предыдущий маршрут сохранён.')
                    continue
                self.result, self.data, self.settings, self.budget, self.result_scope, self.warnings = value
                self.table.delete(*self.table.get_children())
                for i, s in enumerate(self.result.steps):
                    p = s.plan
                    self.table.insert('', 'end', iid=str(i), values=(f'{s.start_level} → {p.progress.end_level}',
                        p.leve['name'], p.leve['location'], f'{p.method} {p.quality}', p.progress.turnins,
                        p.progress.accepts, p.allowances, f'{p.progress.xp:,}', money(-p.cost, True),
                        money(p.reward, True), money(p.balance, True)))
                self.status.set(('DEMO · ' if self.demo else '') + f'Lv {self.result.start_level} → {self.result.end_level} / цель {self.result.target} · {len(self.result.steps)} этапов · {self.result.allowances} allowances · ' + ('Маршрут полный' if self.result.complete else 'Маршрут неполный') + (' · Есть ограничения — см. итог' if self.result.issues else ''))
                self.show_summary()
        except queue.Empty:
            pass
        self.timer = self.root.after(100, self.poll)

    def set_detail(self, text):
        self.detail.configure(state='normal')
        self.detail.delete('1.0', 'end')
        self.detail.insert('1.0', text)
        self.detail.tag_configure('link', foreground='#1670c5', underline=True)
        self.detail.tag_bind('link', '<Enter>', lambda event: self.detail.configure(cursor='hand2'))
        self.detail.tag_bind('link', '<Leave>', lambda event: self.detail.configure(cursor=''))
        self.detail.tag_bind('link', '<Button-1>', self.open_link)
        position = '1.0'
        while True:
            found = self.detail.search('https://universalis.app/market/', position, stopindex='end')
            if not found:
                break
            end = self.detail.index(found + ' lineend')
            self.detail.tag_add('link', found, end)
            position = end
        self.detail.configure(state='disabled')

    def open_link(self, event):
        ranges = self.detail.tag_prevrange('link', self.detail.index(f'@{event.x},{event.y}') + '+1c')
        if ranges:
            webbrowser.open(self.detail.get(*ranges))

    def show_summary(self):
        if self.result is not None:
            self.set_detail(route_summary(self.result, self.settings, self.budget, self.demo) +
                            '\n\nРынок: ' + self.result_scope + ('\n\n' + '\n'.join(self.warnings) if self.warnings else ''))

    def show_step(self, event=None):
        selected = self.table.selection()
        if selected and self.result is not None:
            self.set_detail(step_details(self.result.steps[int(selected[0])], self.data))

    def open_craft_list(self):
        if self.result is None or not self.result.steps:
            messagebox.showinfo('Список крафта', 'Сначала рассчитайте маршрут с хотя бы одним этапом.', parent=self.root)
            return
        from .craft_export_window import CraftExportWindow
        self.craft_list_window = CraftExportWindow(self)

    def export(self):
        if self.result is None:
            messagebox.showinfo('Маршрут', 'Сначала рассчитайте маршрут.', parent=self.root)
            return
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension='.csv', initialfile='leve_route.csv', filetypes=[('CSV', '*.csv')])
        if not path:
            return
        try:
            with open(path, 'w', newline='', encoding='utf-8-sig') as file:
                writer = csv.writer(file)
                writer.writerow(['DEMO', 'Рынок', 'От Lv', 'До Lv', 'Leve ID', 'Название', 'Место', 'Способ', 'Качество', 'Принятия', 'Сдачи', 'Allowances', 'EXP', 'Расход gil', 'Награда gil', 'Баланс gil'])
                for step in self.result.steps:
                    p = step.plan
                    writer.writerow([self.demo, self.result_scope, step.start_level, p.progress.end_level,
                        p.leve['id'], p.leve['name'], p.leve['location'], p.method, p.quality,
                        p.progress.accepts, p.progress.turnins, p.allowances, p.progress.xp, -p.cost, p.reward, p.balance])
                writer.writerow([])
                writer.writerow(['ИТОГ', 'Полный' if self.result.complete else 'Неполный', 'Нужно EXP', self.result.needed_xp,
                                 'Полная стоимость', self.result.cost, 'Расход рассчитанной части', self.result.spent,
                                 'Награда', self.result.reward, 'Allowances', self.result.allowances])
                writer.writerow(['Ограничения', *self.result.issues])
            self.status.set('Маршрут сохранён: ' + path)
        except OSError as exc:
            messagebox.showerror('Экспорт', str(exc), parent=self.root)

    def close(self):
        self.closed = True
        self.cancelled.set()
        self.root.after_cancel(self.timer)
        self.root.destroy()
