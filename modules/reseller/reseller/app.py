"""Russian dark Tk UI. Network and monitoring run outside the UI thread."""
import copy
import csv
from pathlib import Path
import queue
import sqlite3
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

from . import __version__
from .catalog import load_catalog, load_worlds, refresh_catalog, refresh_worlds
from .net import Cache, Cancelled, ApiError
from .scanner import Scanner
from .storage import home, load_settings, load_token, normalize, atomic_json, save_token
from .telegram import Telegram, Notifier, credentials
from .analysis import analyze, basket, rank
from .market import Market
from .history import History, FRESH, STALE, REMOVED

STATUS_COLORS = {FRESH: '#8fe2bd', STALE: '#f6d279', REMOVED: '#ffaaaa'}


def status_colors(table):
    for status, color in STATUS_COLORS.items():
        table.tag_configure(status, foreground=color)


COLS = [('name', 'Товар ↗', 220), ('availability', 'Статус объявления', 160),
        ('sale_price', 'Продажа/шт.', 100), ('sale_world', 'Продать на', 80),
        ('buy_world', 'Купить на', 80), ('hq', 'HQ/NQ', 58),
        ('quantity', 'Шт.', 50), ('buy_price', 'Покупка/шт.', 90), ('usual_price', 'Обычно/шт.', 90),
        ('cost', 'Расход стака', 100), ('revenue', 'Выручка', 95),
        ('profit', 'Прибыль', 100), ('stress_profit', 'При падении', 100), ('profit_day', 'gil/день*', 90),
        ('roi', 'ROI %', 65), ('units_day', 'Шт./день', 75), ('near_supply', 'Близких, шт.', 90),
        ('deals_week', 'Сделок/7д', 78), ('estimated_days', 'Дней', 60), ('upload_age', 'Данные, мин.', 90)]


class Application:
    def __init__(self, root, demo=False, folder=None):
        self.root, self.demo, self.folder = root, demo, Path(folder) if folder else home()
        self.settings, warning = load_settings(self.folder)
        self.data = load_catalog(self.folder)
        self.items = {int(k): v for k, v in self.data['items'].items()}
        self.regions = load_worlds(self.folder)
        self.cache = Cache(self.folder / 'cache')
        self.scanner = Scanner(self.items, self.regions, self.cache, demo)
        self.notifier = Notifier(self.folder / 'alerts.json')
        self.events, self.cancel = queue.Queue(), threading.Event()
        self.busy = self.monitoring = self.closed = False
        self.hot_running = False
        self.hot_rows = {}
        self.history = History(self.folder / ('demo-finds.sqlite3' if demo else 'hot-finds.sqlite3'))
        self.history_error = ''
        self.hot_connection = ''
        self.monitor_timer = None
        self.deals, self.visible = [], []
        self.sort_key, self.reverse = 'profit_day', True
        self.basket = []
        self.basket_only = tk.BooleanVar(root, False)
        self.vars = {k: tk.BooleanVar(root, v) if isinstance(v, bool) else tk.StringVar(root, '' if v is None else str(v))
                     for k, v in self.settings.items() if k != 'schema'}
        self.mode = tk.StringVar(root, 'Ходовые товары всех категорий' if self.settings['mode'] == 'all' else 'Мой список товаров')
        token, token_warning = load_token(self.folder) if not demo else ('', '')
        self.token = tk.StringVar(root, token)
        self.remember_token = tk.BooleanVar(root, True)
        self.search = tk.StringVar(root)
        self.catalog_query = tk.StringVar(root)
        self.status = tk.StringVar(root, warning or token_warning or 'Готово. Выберите параметры и запустите поиск.')
        self.dc_text = tk.StringVar(root)
        self.build()
        from update_checker import UpdateBar
        self.update_bar = UpdateBar(root, 'Dediinside/ffxiv-reseller-paradise', __version__, demo=demo)
        self.set_dc()
        self.poll_timer = root.after(100, self.poll)
        root.protocol('WM_DELETE_WINDOW', self.close)

    def build(self):
        self.root.title('Рай перекупа ' + __version__ + (' · DEMO' if self.demo else ''))
        self.root.geometry('1390x910')
        self.root.minsize(1060, 730)
        bg, fg, field, edge = '#191d24', '#e6eaf0', '#242b35', '#455368'
        self.root.configure(bg=bg)
        style = ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('.', background=bg, foreground=fg, fieldbackground=field, bordercolor=edge)
        style.configure('TCombobox', fieldbackground=field, background=field, foreground=fg, arrowcolor=fg)
        style.map('TCombobox', fieldbackground=[('readonly', field)], foreground=[('readonly', fg)])
        style.configure('TEntry', fieldbackground=field, foreground=fg)
        style.configure('TButton', padding=6, background='#303c4e')
        style.map('TButton', background=[('active', '#42628c')])
        style.configure('Treeview', background=field, fieldbackground=field, foreground=fg, rowheight=27)
        style.configure('Treeview.Heading', background='#303c4e', foreground=fg)
        # Keep status-tag text colors visible when a row is selected.
        style.map('Treeview', background=[('selected', '#244268')], foreground=[('disabled', '#78818f')])
        self.root.option_add('*TCombobox*Listbox.background', field)
        self.root.option_add('*TCombobox*Listbox.foreground', fg)
        self.book = ttk.Notebook(self.root)
        self.book.pack(fill='both', expand=True, padx=12, pady=10)
        rec, settings, tg, catalog = [ttk.Frame(self.book, padding=10) for _ in range(4)]
        for frame, name in [(rec, 'Выгодные предложения'), (settings, 'Расходы и отбор'), (tg, 'Telegram'), (catalog, 'Каталог и мой список')]:
            self.book.add(frame, text=name)
        top = ttk.Frame(rec)
        top.pack(fill='x')
        worlds = sorted(self.regions['worlds'].values())
        self.entry(top, 'Мир продажи', 'sale_world', 0, values=worlds, width=18).bind('<<ComboboxSelected>>', lambda e: self.set_dc())
        ttk.Label(top, textvariable=self.dc_text).grid(row=1, column=1, padx=12, sticky='w')
        self.entry(top, 'Бюджет на стак (пусто = без лимита)', 'budget', 2, width=23)
        ttk.Label(top, text='Источник товаров').grid(row=0, column=3, padx=8, sticky='w')
        ttk.Combobox(top, textvariable=self.mode, values=['Ходовые товары всех категорий', 'Мой список товаров'], state='readonly', width=32).grid(row=1, column=3, padx=8)
        self.entry(top, 'Категория', 'category', 4, values=['Все'] + sorted({i['category'] for i in self.items.values()}), width=22)
        self.entry(top, 'Качество', 'quality', 5, values=['Все', 'NQ', 'HQ'], width=8)
        buttons = ttk.Frame(rec)
        buttons.pack(fill='x', pady=10)
        self.scan_button = ttk.Button(buttons, text='Найти выгодные стаки', command=self.scan)
        self.scan_button.pack(side='left')
        self.monitor_button = ttk.Button(buttons, text='Начать мониторинг', command=self.start_monitor)
        self.monitor_button.pack(side='left', padx=8)
        self.hot_button = ttk.Button(buttons, text='Горячие предложения', command=self.start_hot)
        self.hot_button.pack(side='left', padx=(0, 8))
        ttk.Button(buttons, text='Остановить', command=self.stop).pack(side='left')
        ttk.Button(buttons, text='Сохранить настройки', command=self.save).pack(side='left', padx=8)
        ttk.Button(buttons, text='Экспорт CSV', command=self.export).pack(side='left')
        self.force = tk.BooleanVar(self.root, False)
        ttk.Checkbutton(buttons, text='Свежий запрос API', variable=self.force).pack(side='left', padx=10)
        ttk.Label(rec, textvariable=self.status, wraplength=1300).pack(fill='x', pady=(0, 8))
        filt = ttk.Frame(rec)
        filt.pack(fill='x', pady=(0, 8))
        ttk.Label(filt, text='Название в результатах').pack(side='left')
        ttk.Entry(filt, textvariable=self.search, width=30).pack(side='left', padx=8)
        self.search.trace_add('write', lambda *a: self.render())
        ttk.Button(filt, text='Открыть покупку ↗', command=self.open_buy).pack(side='left', padx=8)
        ttk.Button(filt, text='Открыть продажу ↗', command=self.open_sale).pack(side='left')
        ttk.Checkbutton(filt, text='Только рекомендуемая корзина', variable=self.basket_only, command=self.render).pack(side='left', padx=12)
        table_frame = ttk.Frame(rec)
        table_frame.pack(fill='both', expand=True)
        self.table = ttk.Treeview(table_frame, columns=[c[0] for c in COLS], show='headings', height=12)
        for key, label, width in COLS:
            self.table.heading(key, text=label, command=lambda k=key: self.sort(k))
            self.table.column(key, width=width, stretch=key == 'name', anchor='w' if key in ('name', 'buy_world') else 'e')
        scroll = ttk.Scrollbar(table_frame, command=self.table.yview)
        horizontal = ttk.Scrollbar(table_frame, orient='horizontal', command=self.table.xview)
        self.table.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        self.table.tag_configure('basket', foreground='#8fe2bd')
        status_colors(self.table)
        self.table.grid(row=0, column=0, sticky='nsew')
        scroll.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        self.table.bind('<<TreeviewSelect>>', lambda e: self.describe())
        self.table.bind('<Double-1>', lambda e: self.open_buy())
        actions = ttk.Frame(rec)
        actions.pack(fill='x', pady=(6, 0))
        ttk.Button(actions, text='История находок', command=self.open_history).pack(side='left')
        ttk.Button(actions, text='Копировать цену продажи', command=lambda: self.copy_sale(self.selected())).pack(side='left', padx=8)
        ttk.Label(actions, text='Статус относится к покупке; цена продажи сохранена на момент находки.').pack(side='left')
        self.detail = self.text(rec, 9)
        self.fill_detail('Каждая строка — отдельный целый стак. Спрос и обычная цена оцениваются на мире продажи.\nDEMO использует искусственные цены и никогда не отправляет Telegram.' if self.demo else 'Каждая строка — отдельный целый стак. Налоги и дорога включены. Суммарная закупка нескольких строк не оптимизируется.')
        fields = [('Мин. прибыль стака, gil', 'min_profit'), ('Мин. ROI, %', 'min_roi'), ('Мин. скидка к истории, %', 'min_discount'),
                  ('Мин. спрос, шт./день', 'min_units_day'), ('Мин. сделок за 7 дней', 'min_deals_week'), ('Последняя продажа ≤ часов', 'max_sale_hours'),
                  ('Обновление мира/объявления ≤ минут', 'max_upload_minutes'), ('Срок продажи по модели ≤ дней', 'max_days'),
                  ('Наша доля спроса, %', 'market_share'), ('Налог покупки, %', 'buy_tax'), ('Налог продажи, %', 'sell_tax'),
                  ('Дорога за покупку стака, gil', 'travel'), ('Снижение цены конкурента, gil', 'undercut'),
                  ('Пауза между циклами, минут', 'interval_minutes'), ('Товаров в предварительном отборе за цикл', 'overview_limit'),
                  ('Товаров подробно за цикл', 'detail_limit'), ('Уведомлений за цикл / минуту потока', 'alert_limit'),
                  ('Пауза уведомлений об одном товаре/мире, мин.', 'cooldown_minutes'),
                  ('Проверить падение цены на, %', 'stress_drop'), ('Резерв бюджета корзины, %', 'reserve_percent'),
                  ('Минимум дней с продажами за неделю', 'min_active_days'), ('Близкие конкуренты: диапазон цены, %', 'nearby_percent'),
                  ('Запас близких конкурентов ≤ дней спроса', 'max_supply_days'),
                  ('Обойти паузу при снижении цены на, %', 'hot_improvement')]
        for index, (label, key) in enumerate(fields):
            row, col = divmod(index, 2)
            ttk.Label(settings, text=label).grid(row=row, column=col * 2, sticky='w', padx=8, pady=8)
            ttk.Entry(settings, textvariable=self.vars[key], width=12).grid(row=row, column=col * 2 + 1, sticky='w', padx=8)
        ttk.Label(settings, text='Приоритет корзины').grid(row=12, column=0, sticky='w', padx=8, pady=8)
        ttk.Combobox(settings, textvariable=self.vars['priority'], values=['Оборот прибыли', 'Отдача капитала', 'Прибыль стака'], state='readonly', width=22).grid(row=12, column=1, sticky='w')
        ttk.Label(settings, text='Оборот прибыли: прибыль при падении цены / расчётные дни. Отдача капитала: этот показатель / затраты.\nСрок = близкий запас конкурентов / общий спрос + наш стак / (спрос × наша доля). Это модель, не обещание.\nКорзина выбирает по одному стаку товара/HQ в пределах общего бюджета с резервом. Покупки и личный инвентарь не отслеживаются.\nКаталог обходится частями с чередованием; средние цены нужны только для отбора. Старый кэш при ошибке сети не подставляется.', wraplength=1100).grid(row=13, column=0, columnspan=4, sticky='w', padx=8, pady=16)
        ttk.Button(settings, text='Сохранить настройки', command=self.save).grid(row=14, column=0, sticky='w', padx=8)
        ttk.Button(settings, text='Сбросить лимит горячих рекомендаций', command=self.reset_hot_budget).grid(row=14, column=2, sticky='w', padx=8)
        ttk.Label(tg, text='Подключить уведомления', font=('Segoe UI', 16, 'bold')).pack(anchor='w')
        ttk.Label(tg, text='1. В Telegram откройте @BotFather → /newbot и создайте бота.\n2. Вставьте токен ниже, затем откройте своего бота и отправьте /start.\n3. Нажмите «Найти Chat ID», выберите свой чат и отправьте тест.\n4. Включите уведомления, сохраните настройки и начните мониторинг.\nПрограмма должна оставаться запущенной; при закрытии / сне компьютера мониторинг прекращается.', wraplength=1100).pack(anchor='w', pady=12)
        ttk.Label(tg, text='Токен бота (вводится только здесь)').pack(anchor='w')
        ttk.Entry(tg, textvariable=self.token, show='•', width=80).pack(anchor='w', pady=5)
        ttk.Checkbutton(tg, text='Сохранять токен с защитой Windows (снятие и сохранение удаляет прежний файл)', variable=self.remember_token).pack(anchor='w', pady=5)
        ttk.Label(tg, text='Chat ID').pack(anchor='w', pady=(10, 0))
        ttk.Entry(tg, textvariable=self.vars['chat_id'], width=32).pack(anchor='w', pady=5)
        row = ttk.Frame(tg)
        row.pack(anchor='w', pady=10)
        ttk.Button(row, text='Найти Chat ID', command=self.find_chat).pack(side='left')
        ttk.Button(row, text='Отправить тест', command=self.test_telegram).pack(side='left', padx=10)
        ttk.Button(row, text='Сохранить', command=self.save).pack(side='left')
        ttk.Checkbutton(tg, text='Отправлять найденные предложения в Telegram', variable=self.vars['notify']).pack(anchor='w', pady=10)
        ttk.Label(tg, text='В уведомлении: товар, HQ/NQ, мир покупки, весь стак, расходы, обычная/рекомендуемая цена,\nчистая прибыль, спрос и ссылки Universalis. Только прошедшие отбор свежие данные.\nЗащита от повторов сохраняется между запусками. DEMO отправки не выполняет.', wraplength=1100).pack(anchor='w', pady=10)
        search_row = ttk.Frame(catalog)
        search_row.pack(fill='x')
        ttk.Label(search_row, text='Название или ID').pack(side='left')
        ttk.Entry(search_row, textvariable=self.catalog_query, width=45).pack(side='left', padx=8)
        ttk.Button(search_row, text='Найти в каталоге', command=self.find_items).pack(side='left')
        ttk.Button(search_row, text='Добавить выделенное в мой список', command=self.add_watch).pack(side='left', padx=8)
        self.item_table = ttk.Treeview(catalog, columns=('id', 'name', 'category'), show='headings', height=10)
        for key, title, width in [('id', 'ID', 80), ('name', 'Название EN', 450), ('category', 'Категория', 220)]:
            self.item_table.heading(key, text=title)
            self.item_table.column(key, width=width)
        self.item_table.pack(fill='both', expand=True, pady=10)
        self.item_table.bind('<Double-1>', lambda e: self.add_watch())
        ttk.Label(catalog, text='Мой список: ID через запятую. Для него предварительный отбор по средней цене пропускается.').pack(anchor='w')
        ttk.Entry(catalog, textvariable=self.vars['watch_ids'], width=100).pack(fill='x', pady=8)
        row = ttk.Frame(catalog)
        row.pack(fill='x', pady=10)
        ttk.Button(row, text='Обновить игровые названия/категории', command=self.refresh_items).pack(side='left')
        ttk.Button(row, text='Обновить миры/DC', command=self.refresh_regions).pack(side='left', padx=8)
        ttk.Label(catalog, text=f'Встроенный каталог: {len(self.items):,} рыночных предметов. Названия EN; регионы глобального клиента.').pack(anchor='w')
        ttk.Label(self.root, text='Universalis обновляется игроками. Перепроверьте предложения в игре. Приложение не покупает предметы.', padding=(12, 4)).pack(fill='x')

    def text(self, frame, height):
        text = tk.Text(frame, height=height, bg='#242b35', fg='#e6eaf0', insertbackground='white', wrap='word', font=('Consolas', 10))
        text.pack(fill='both', expand=True, pady=10)
        return text

    def entry(self, parent, title, key, col, values=None, width=12):
        ttk.Label(parent, text=title).grid(row=0, column=col, padx=6, sticky='w')
        widget = ttk.Combobox(parent, textvariable=self.vars[key], values=values, state='readonly', width=width) if values else ttk.Entry(parent, textvariable=self.vars[key], width=width)
        widget.grid(row=1, column=col, padx=6, sticky='w')
        return widget

    def set_dc(self):
        try:
            _, dc = self.scanner.world(self.vars['sale_world'].get())
            self.dc_text.set('Покупка: DC ' + dc['name'])
        except ValueError as exc:
            self.dc_text.set(str(exc))

    def read(self):
        raw = {k: v.get() for k, v in self.vars.items()}
        raw['mode'] = 'all' if self.mode.get() == 'Ходовые товары всех категорий' else 'watch'
        result = normalize(raw)
        self.scanner.world(result['sale_world'])
        if result['notify'] and not self.demo:
            credentials(self.token.get(), result['chat_id'])
        return result

    def save(self, quiet=False):
        try:
            self.settings = self.read()
            atomic_json(self.folder / 'settings.json', self.settings)
            if not self.demo and self.remember_token.get() and self.token.get().strip():
                credentials(self.token.get())
                save_token(self.token.get().strip(), self.folder)
            elif not self.demo and not self.remember_token.get():
                (self.folder / 'telegram-secret.json').unlink(missing_ok=True)
            if not quiet:
                self.status.set('Настройки сохранены. ' + ('Токен защищён Windows. ' if self.token.get() and not self.demo and self.remember_token.get() else '') + ('Для применения к потоку остановите и запустите его заново.' if self.hot_running else ''))
            return True
        except (ValueError, OSError) as exc:
            if not quiet:
                messagebox.showerror('Настройки', str(exc), parent=self.root)
            return False

    def run_worker(self, work):
        if self.busy:
            return False
        self.busy = True
        self.cancel = threading.Event()
        self.scan_button.configure(state='disabled')
        self.monitor_button.configure(state='disabled')
        self.hot_button.configure(state='disabled')
        cancel = self.cancel
        def run():
            try:
                value = work(cancel)
                self.events.put(('done', value))
            except Cancelled:
                self.events.put(('cancel', 'Остановлено'))
            except Exception as exc:
                # Do not show arbitrary tracebacks or secret-containing exception URLs.
                self.events.put(('error', str(exc) if isinstance(exc, (ValueError, ApiError, OSError)) else 'Ошибка обработки данных. Попробуйте обновить каталог.'))
        threading.Thread(target=run, daemon=True).start()
        return True

    def scan(self):
        if self.busy or not self.save():
            return
        settings, token, force = copy.deepcopy(self.settings), self.token.get(), self.force.get()
        def work(cancel):
            result = self.scanner.scan(settings, force, lambda msg: self.events.put(('progress', msg)), cancel)
            sent, errors = 0, []
            if settings['notify'] and not self.demo:
                result.basket = basket(self.notifier.eligible(result.deals, settings), settings)
            if settings['notify'] and not self.demo and result.basket:
                self.events.put(('progress', 'Повторная проверка предложений перед Telegram…'))
                world, dc = self.scanner.world(settings['sale_world'])
                current = Market(self.cache).detailed(dc['name'], world, sorted({d.item for d in result.basket}), True, cancel=cancel)
                checked, _ = analyze(self.items, current, settings, {int(k): v for k, v in self.regions['worlds'].items()}, dc['worlds'])
                fingerprints = {d.fingerprint for d in result.basket}
                confirmed = basket([d for d in checked if d.fingerprint in fingerprints], settings)
                if len(confirmed) < len(result.basket):
                    result.warnings.append('Часть корзины не прошла повторную проверку перед Telegram; такие уведомления пропущены.')
                result.basket = confirmed
                latest = {d.fingerprint: d for d in confirmed}
                result.deals = [latest.get(d.fingerprint, d) for d in result.deals]
                sent, errors = self.notifier.dispatch(confirmed, settings, Telegram(token), cancel)
            return ('scan', result, sent, errors)
        self.run_worker(work)

    def start_monitor(self):
        if self.busy or not self.save():
            return
        self.monitoring = True
        self.scan()

    def start_hot(self):
        if self.busy or not self.save():
            return
        self.stop()
        self.hot_running = True
        settings, token = copy.deepcopy(self.settings), self.token.get()
        for deal, _ in self.hot_rows.values():
            if deal.availability != REMOVED:
                deal.availability = STALE
        self.deals, self.basket = [row[0] for row in self.hot_rows.values()], []
        self.basket_only.set(False)
        self.render()
        self.fill_detail('Горячий поток: только впервые замеченные или изменённые объявления.\nНаходки сохраняются: после 90 секунд статус меняется на «Нужно перепроверить», после удаления — «Снято или изменено».\nЦена продажи остаётся в таблице и истории после покупки и перезапуска.\nЛимит стоимости отправленных рекомендаций сохраняется до ручного сброса. Параметры применяются при запуске потока.')
        from .hot import HotMonitor
        def work(cancel):
            value = HotMonitor(self.scanner, self.folder, self.notifier).run(settings, cancel,
                         lambda kind, value: self.events.put((kind, value)),
                         Telegram(token) if settings['notify'] and not self.demo else None)
            return ('hot_done', value)
        self.run_worker(work)

    def reset_hot_budget(self):
        if self.busy:
            self.status.set('Сначала остановите мониторинг, чтобы сбросить лимит рекомендаций.')
            return
        try:
            from .hot import Ledger
            ledger = Ledger(self.folder / 'hot-advice.sqlite3')
            ledger.reset(self.read())
            ledger.close()
            self.status.set('Лимит стоимости/объёма рекомендаций сброшен. Лимит сообщений за минуту сохранён.')
        except (ValueError, OSError) as exc:
            self.status.set(str(exc))

    def stop(self):
        self.monitoring = False
        self.cancel.set()
        if self.monitor_timer is not None:
            self.root.after_cancel(self.monitor_timer)
            self.monitor_timer = None
        self.status.set('Останавливаю текущий запрос…' if self.busy else 'Мониторинг остановлен.')

    def poll(self):
        if self.closed:
            return
        if self.hot_rows:
            now = time.time()
            changed = False
            for deal, stamp in self.hot_rows.values():
                if deal.availability == FRESH and not 0 <= now - stamp < 90:
                    deal.availability = STALE
                    changed = True
            if changed:
                self.render()
        while not self.events.empty():
            kind, value = self.events.get_nowait()
            if kind == 'progress':
                self.status.set(value)
                continue
            if kind == 'hot':
                self.show_hot(value)
                continue
            if kind == 'hot_connection':
                self.hot_connection = value
                self.status.set(value)
                continue
            if kind == 'hot_remove':
                keys = set(value)
                for deal, _ in self.hot_rows.values():
                    if deal.offer_key in keys:
                        deal.availability = REMOVED
                self.history_write(self.history.remove, value)
                self.render()
                continue
            self.busy = False
            self.scan_button.configure(state='normal')
            self.monitor_button.configure(state='normal')
            self.hot_button.configure(state='normal')
            if self.hot_running:
                self.hot_running = False
                self.monitoring = False
            if kind == 'done':
                self.handle(value)
            else:
                self.status.set(value)
            if self.monitoring:
                if self.monitor_timer is not None:
                    self.root.after_cancel(self.monitor_timer)
                self.monitor_timer = self.root.after(int(self.settings['interval_minutes'] * 60000), self.monitor_tick)
                self.status.set(self.status.get() + f" · Следующий цикл через {self.settings['interval_minutes']:g} мин.")
        self.poll_timer = self.root.after(100, self.poll)

    def monitor_tick(self):
        self.monitor_timer = None
        if self.monitoring and not self.closed:
            self.scan()

    def handle(self, value):
        if value[0] == 'hot_done':
            self.status.set(value[1])
        elif value[0] == 'scan':
            _, result, sent, errors = value
            self.deals = result.deals
            self.basket = result.basket
            self.render()
            self.status.set(f'Каталог {result.universe:,} · отбор {result.overview:,} · кандидатов {result.candidates} · подробно {result.detailed} · выгодных стаков {len(result.deals)} · Telegram {sent}' + (' · DEMO' if self.demo else ''))
            cost, profit = sum(d.cost for d in result.basket), sum(d.stress_profit for d in result.basket)
            lines = [f'Рекомендуемая корзина: {len(result.basket)} стаков, расход −{cost:,} gil, прибыль при падении цены +{profit:,} gil.',
                     'Корзина выделена зелёным; Telegram сообщает только о ней. Выбор по приоритету, глобальный максимум не доказан.',
                     'Параметры спроса: ' + f"≥{self.settings['min_units_day']:g} шт./день, ≥{self.settings['min_deals_week']} сделок/нед., ≥{self.settings['min_active_days']} дней с продажами.",
                     '', 'Отсев при подробной проверке:'] + [f'{k}: {v}' for k, v in result.reasons.items()]
            lines += [''] + result.warnings[:12] + errors
            if not result.deals:
                lines += ['', 'Нет стаков, прошедших отбор в этой части каталога. Следующий цикл проверит другие товары.',
                          'Проверьте минимальную прибыль/спрос, свежесть данных и размер стака; свои ID можно проверить напрямую.']
            else:
                lines += ['', 'Выберите строку для подробностей. Каждая строка — независимая покупка целого стака.']
            self.fill_detail('\n'.join(lines))
        elif value[0] == 'test':
            self.status.set('Тестовое сообщение доставлено в Telegram.')
        elif value[0] == 'chats':
            chats = value[1]
            if not chats:
                self.status.set('Не найден личный чат. Отправьте /start своему боту и повторите. При активном webhook getUpdates недоступен.')
                return
            dialog = tk.Toplevel(self.root)
            dialog.title('Выберите свой чат')
            ttk.Label(dialog, text='Выберите адресата уведомлений:').pack(padx=20, pady=10)
            for chat, name in chats.items():
                def select(c=chat):
                    self.vars['chat_id'].set(c)
                    dialog.destroy()
                    self.status.set('Chat ID выбран. Отправьте тест и сохраните настройки.')
                ttk.Button(dialog, text=f'{name} · {chat}', command=select).pack(fill='x', padx=20, pady=5)
        elif value[0] == 'catalog':
            self.data = value[1]
            self.items = {int(k): v for k, v in self.data['items'].items()}
            self.scanner.items = self.items
            self.status.set(f'Каталог обновлён: {len(self.items):,} предметов. Новые категории доступны после перезапуска.')
        elif value[0] == 'regions':
            self.regions = value[1]
            self.scanner.regions = self.regions
            self.set_dc()
            self.status.set('Миры и DC обновлены. Новый список выбора доступен после перезапуска.')

    def show_hot(self, value):
        deals, reasons, sent, events, dropped, expired, errors, spent = value
        now = time.time()
        for deal in deals:
            if deal.fingerprint not in self.hot_rows:
                deal.received = deal.received or now
                deal.availability = FRESH if 0 <= now - deal.received < 90 else STALE
                self.hot_rows[deal.fingerprint] = (deal, deal.received)
        if deals:
            self.history_write(self.history.add, deals)
        self.deals = [row[0] for row in self.hot_rows.values()]
        self.basket = []
        self.basket_only.set(False)
        self.render()
        self.status.set(f'Горячий поток · событий {events} · находок в таблице {len(self.deals)} · Telegram {sent} · перегрузка {dropped} · просрочено {expired} · рекомендовано затрат {spent:,} gil' + (' · DEMO' if self.demo else ' · ' + self.hot_connection) + self.history_error)
        if not self.selected():
            self.fill_detail('Находки и цены продажи сохраняются в «Истории находок». Устаревшие и снятые объявления остаются как справка.\n' + '\n'.join(f'{k}: {v}' for k, v in reasons.items()) + '\n' + '\n'.join(errors[:8]))

    def render(self):
        selected = self.selected()
        selected_id = selected.fingerprint if selected else None
        scroll = self.table.yview()
        chosen = {d.fingerprint for d in self.basket}
        self.visible = [d for d in self.deals if self.search.get().casefold() in d.name.casefold() and (not self.basket_only.get() or d.fingerprint in chosen)]
        self.visible.sort(key=lambda d: getattr(d, self.sort_key), reverse=self.reverse)
        self.table.delete(*self.table.get_children())
        for index, deal in enumerate(self.visible):
            values = []
            for key, _, _ in COLS:
                value = getattr(deal, key)
                if key == 'hq':
                    value = 'HQ' if value else 'NQ'
                elif key == 'cost':
                    value = f'−{value:,}'.replace(',', ' ')
                elif key in ('revenue', 'profit', 'stress_profit'):
                    value = f'+{value:,}'.replace(',', ' ')
                elif isinstance(value, float):
                    value = f'{value:.1f}'
                elif isinstance(value, int):
                    value = f'{value:,}'.replace(',', ' ')
                values.append(value)
            tags = (deal.availability,) if deal.availability in STATUS_COLORS else ('basket',) if deal.fingerprint in chosen else ()
            self.table.insert('', 'end', iid=str(index), values=values, tags=tags)
            if deal.fingerprint == selected_id:
                self.table.selection_set(str(index))
        if scroll:
            self.table.yview_moveto(scroll[0])
        if self.selected():
            self.describe()

    def sort(self, key):
        self.reverse = not self.reverse if key == self.sort_key else key not in ('estimated_days', 'upload_age', 'cost', 'name', 'buy_world')
        self.sort_key = key
        self.render()

    def selected(self):
        chosen = self.table.selection()
        return self.visible[int(chosen[0])] if chosen and int(chosen[0]) < len(self.visible) else None

    def history_write(self, operation, values):
        try:
            operation(values)
            self.history_error = ''
        except (sqlite3.Error, OSError):
            self.history_error = ' · Не удалось сохранить историю на диск; строки доступны в текущем окне. Экспортируйте CSV.'
            self.status.set(self.status.get() + self.history_error)

    def copy_sale(self, deal):
        if deal:
            self.root.clipboard_clear()
            self.root.clipboard_append(str(deal.sale_price))
            self.status.set(f'Скопирована сохранённая цена: {deal.sale_price:,} gil/шт. на {deal.sale_world}.')

    def open_history(self):
        dialog = tk.Toplevel(self.root)
        dialog.title('История находок — сохранённые цены продажи')
        dialog.geometry('1100x700')
        query = tk.StringVar(dialog)
        bar = ttk.Frame(dialog, padding=10)
        bar.pack(fill='x')
        ttk.Label(bar, text='Название товара').pack(side='left')
        entry = ttk.Entry(bar, textvariable=query, width=35)
        entry.pack(side='left', padx=8)
        hint = ttk.Label(dialog, text='Последние 500 совпадений по всей истории. Цена относится к моменту находки; покупка не отслеживается.')
        hint.pack(fill='x', padx=10)
        frame = ttk.Frame(dialog, padding=10)
        frame.pack(fill='both', expand=True)
        columns = [('name', 'Товар', 240), ('availability', 'Статус', 155), ('sale_price', 'Продажа/шт.', 100),
                   ('sale_world', 'Продать на', 85), ('buy_world', 'Купить на', 85), ('hq', 'HQ/NQ', 60),
                   ('quantity', 'Шт.', 50), ('received', 'Найдено', 145)]
        table = ttk.Treeview(frame, columns=[c[0] for c in columns], show='headings', height=10)
        status_colors(table)
        for key, label, width in columns:
            table.heading(key, text=label)
            table.column(key, width=width, stretch=key == 'name')
        scrollbar = ttk.Scrollbar(frame, command=table.yview)
        horizontal = ttk.Scrollbar(frame, orient='horizontal', command=table.xview)
        table.configure(yscrollcommand=scrollbar.set, xscrollcommand=horizontal.set)
        table.grid(row=0, column=0, sticky='nsew')
        scrollbar.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        detail = self.text(dialog, 9)
        rows = []
        def selected():
            selection = table.selection()
            return rows[int(selection[0])] if selection else None
        def describe(event=None):
            d = selected()
            if d:
                detail.configure(state='normal')
                detail.delete('1.0', 'end')
                detail.insert('1.0', d.detail())
                detail.configure(state='disabled')
        def refresh(event=None):
            try:
                found = self.history.recent(query.get())
            except (sqlite3.Error, OSError):
                hint.configure(text='Не удалось прочитать историю. Проверьте доступ к папке настроек.')
                return
            table.delete(*table.get_children())
            rows[:] = found
            detail.configure(state='normal')
            detail.delete('1.0', 'end')
            detail.configure(state='disabled')
            for i, d in enumerate(rows):
                table.insert('', 'end', iid=str(i), values=(d.name, d.availability, d.sale_price, d.sale_world, d.buy_world,
                            'HQ' if d.hq else 'NQ', d.quantity, time.strftime('%d.%m.%y %H:%M:%S', time.localtime(d.received))),
                            tags=(d.availability,) if d.availability in STATUS_COLORS else ())
        ttk.Button(bar, text='Найти / обновить', command=refresh).pack(side='left')
        ttk.Button(bar, text='Копировать цену', command=lambda: self.copy_sale(selected())).pack(side='left', padx=8)
        ttk.Button(bar, text='Рынок продажи ↗', command=lambda: webbrowser.open(selected().sale_url) if selected() else None).pack(side='left')
        entry.bind('<Return>', refresh)
        table.bind('<<TreeviewSelect>>', describe)
        refresh()

    def fill_detail(self, value):
        self.detail.configure(state='normal')
        self.detail.delete('1.0', 'end')
        self.detail.insert('1.0', value)
        self.detail.configure(state='disabled')

    def describe(self):
        deal = self.selected()
        if deal:
            self.fill_detail(('DEMO · искусственные цены\n' if self.demo else '') + deal.detail())

    def open_buy(self):
        deal = self.selected()
        if deal:
            webbrowser.open(deal.buy_url)

    def open_sale(self):
        deal = self.selected()
        if deal:
            webbrowser.open(deal.sale_url)

    def export(self):
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension='.csv', initialfile='reseller_deals.csv', filetypes=[('CSV', '*.csv')])
        if path:
            try:
                from dataclasses import asdict
                with open(path, 'w', encoding='utf-8-sig', newline='') as out:
                    fields = list(asdict(self.visible[0])) if self.visible else [k for k, _, _ in COLS]
                    writer = csv.DictWriter(out, fieldnames=['demo'] + fields)
                    writer.writeheader()
                    for deal in self.visible:
                        writer.writerow({'demo': self.demo, **asdict(deal)})
                self.status.set('CSV сохранён: ' + path)
            except OSError as exc:
                messagebox.showerror('CSV', str(exc), parent=self.root)

    def find_chat(self):
        if self.demo or self.busy:
            self.status.set('Telegram отключён в DEMO.' if self.demo else 'Дождитесь завершения текущей операции.')
            return
        try:
            bot = Telegram(self.token.get())
            self.run_worker(lambda cancel: ('chats', bot.chats(cancel)))
        except ValueError as exc:
            self.status.set(str(exc))

    def test_telegram(self):
        if self.demo or self.busy:
            self.status.set('Telegram отключён в DEMO.' if self.demo else 'Дождитесь завершения текущей операции.')
            return
        try:
            bot = Telegram(self.token.get())
            chat = self.vars['chat_id'].get().strip()
            credentials(self.token.get(), chat)
            self.run_worker(lambda cancel: ('test', bot.send(chat, 'Рай перекупа: тестовое уведомление. Подключение работает.', cancel)))
        except ValueError as exc:
            self.status.set(str(exc))

    def find_items(self):
        query = self.catalog_query.get().casefold().strip()
        self.item_table.delete(*self.item_table.get_children())
        found = [(i, r) for i, r in self.items.items() if query in r['name'].casefold() or query == str(i)]
        for item, row in sorted(found, key=lambda x: x[1]['name'])[:200]:
            self.item_table.insert('', 'end', iid=str(item), values=(item, row['name'], row['category']))
        self.status.set(f'Найдено {len(found):,}; показаны первые 200. Двойной щелчок добавляет ID в мой список.')

    def add_watch(self):
        selection = self.item_table.selection()
        if selection:
            old = self.vars['watch_ids'].get().strip()
            self.vars['watch_ids'].set(','.join(filter(None, [old, *selection])))
            self.status.set('ID добавлены. Для прямой проверки выберите источник «Мой список товаров».')

    def refresh_items(self):
        if not self.demo:
            self.run_worker(lambda cancel: ('catalog', refresh_catalog(self.folder, cancel)))
        else:
            self.status.set('Обновление отключено в DEMO.')

    def refresh_regions(self):
        if not self.demo:
            self.run_worker(lambda cancel: ('regions', refresh_worlds(self.cache, self.folder, cancel)))
        else:
            self.status.set('Обновление отключено в DEMO.')

    def close(self):
        self.stop()
        self.save(quiet=True)
        self.closed = True
        self.root.after_cancel(self.poll_timer)
        self.root.destroy()
