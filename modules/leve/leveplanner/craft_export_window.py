"""Copyable Teamcraft/Artisan fields and a raw material list for a route snapshot."""
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .craft_export import crafting_list


class CraftExportWindow:
    def __init__(self, parent):
        self.parent = parent
        self.route, self.data, self.job = parent.result, parent.data, parent.settings.job
        self.selected = int(parent.table.selection()[0]) if parent.table.selection() else None
        self.result, self.auto_name = None, ''
        self.root = tk.Toplevel(parent.root)
        self.root.title('Список крафта · Teamcraft / Artisan' + (' · DEMO' if parent.demo else ''))
        self.root.geometry('1060x820')
        self.root.minsize(900, 680)
        wrap = ttk.Frame(self.root, padding=14)
        wrap.pack(fill='both', expand=True)
        ttk.Label(wrap, text='Список крафта для маршрута', font=('Segoe UI', 17, 'bold')).pack(anchor='w')
        ttk.Label(wrap, text='Скопируйте List Name и два списка в одноимённые поля Teamcraft Import в Artisan. Названия предметов — EN.').pack(anchor='w', pady=(4, 10))
        options = ttk.Frame(wrap)
        options.pack(fill='x')
        self.scope = tk.StringVar(self.root, 'Весь маршрут')
        self.source = tk.StringVar(self.root, 'Все предметы для сдачи')
        self.intermediates = tk.BooleanVar(self.root, True)
        scope_values = ['Весь маршрут'] + (['Выбранный этап'] if self.selected is not None else [])
        for var, values, width in [(self.scope, scope_values, 19),
                                   (self.source, ['Все предметы для сдачи', 'Только крафт маршрута'], 26)]:
            box = ttk.Combobox(options, textvariable=var, values=values, state='readonly', width=width)
            box.pack(side='left', padx=(0, 12))
            box.bind('<<ComboboxSelected>>', lambda event: self.generate())
        ttk.Checkbutton(options, text='Крафтить полуфабрикаты', variable=self.intermediates, command=self.generate).pack(side='left')
        name_row = ttk.Frame(wrap)
        name_row.pack(fill='x', pady=10)
        self.name = tk.StringVar(self.root)
        ttk.Label(name_row, text='List Name').pack(side='left', padx=(0, 8))
        ttk.Entry(name_row, textvariable=self.name, width=50).pack(side='left', fill='x', expand=True)
        ttk.Button(name_row, text='Копировать имя', command=lambda: self.copy_text(self.name.get())).pack(side='left', padx=8)
        self.status = tk.StringVar(self.root)
        ttk.Label(wrap, textvariable=self.status, wraplength=1000).pack(fill='x', pady=(0, 8))
        blocks = ttk.Frame(wrap)
        blocks.pack(fill='both', expand=True)
        blocks.columnconfigure(0, weight=1)
        blocks.columnconfigure(1, weight=1)
        blocks.rowconfigure(0, weight=1)
        self.pre = self.text_block(blocks, 'Pre-craft Items', 0)
        self.final = self.text_block(blocks, 'Final Items', 1)
        self.pre_button = ttk.Button(blocks, text='Копировать Pre-craft Items', command=lambda: self.copy_field('pre'))
        self.pre_button.grid(row=1, column=0, sticky='ew', padx=(0, 6), pady=6)
        self.final_button = ttk.Button(blocks, text='Копировать Final Items', command=lambda: self.copy_field('final'))
        self.final_button.grid(row=1, column=1, sticky='ew', padx=(6, 0), pady=6)
        notes = ttk.LabelFrame(wrap, text='Рецепты, базовые материалы и требования', padding=6)
        notes.pack(fill='both', expand=True, pady=8)
        self.detail = tk.Text(notes, height=12, wrap='word', font=('Consolas', 10))
        detail_scroll = ttk.Scrollbar(notes, command=self.detail.yview)
        self.detail.configure(yscrollcommand=detail_scroll.set)
        self.detail.pack(side='left', fill='both', expand=True)
        detail_scroll.pack(side='right', fill='y')
        actions = ttk.Frame(wrap)
        actions.pack(fill='x')
        self.save_button = ttk.Button(actions, text='Сохранить список TXT', command=self.save)
        self.save_button.pack(side='left')
        ttk.Button(actions, text='Закрыть', command=self.root.destroy).pack(side='right')
        self.generate()

    def text_block(self, frame, title, column):
        block = ttk.LabelFrame(frame, text=title, padding=6)
        block.grid(row=0, column=column, sticky='nsew', padx=(0, 6) if column == 0 else (6, 0))
        text = tk.Text(block, height=10, wrap='none', font=('Consolas', 10))
        vertical = ttk.Scrollbar(block, command=text.yview)
        horizontal = ttk.Scrollbar(block, orient='horizontal', command=text.xview)
        text.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        text.grid(row=0, column=0, sticky='nsew')
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        block.rowconfigure(0, weight=1)
        block.columnconfigure(0, weight=1)
        return text

    @staticmethod
    def fill(widget, text):
        widget.configure(state='normal')
        widget.delete('1.0', 'end')
        widget.insert('1.0', text)
        widget.configure(state='disabled')

    def generate(self):
        try:
            result = crafting_list(self.route, self.data, self.job,
                                   'all' if self.source.get() == 'Все предметы для сдачи' else 'craft',
                                   self.intermediates.get(), self.selected if self.scope.get() == 'Выбранный этап' else None)
            if self.parent.demo and not result.name.endswith(' DEMO'):
                result.name += ' DEMO'
            if not self.name.get() or self.name.get() == self.auto_name:
                self.name.set(result.name)
            self.auto_name = result.name
            self.result = result
            self.fill(self.pre, result.pre)
            self.fill(self.final, result.final)
            self.fill(self.detail, result.details(self.data))
            self.pre_button.configure(state='normal' if result.pre else 'disabled')
            self.final_button.configure(state='normal' if result.final else 'disabled')
            self.save_button.configure(state='normal')
            self.status.set(f'Рецептов: {len(result.recipes)} · Базовых материалов: {len(result.materials)} · ' +
                            ('Есть требования/замечания — см. ниже.' if result.warnings else 'Списки готовы. Требования HQ и уровней — ниже.'))
        except ValueError as exc:
            self.result = None
            self.fill(self.pre, '')
            self.fill(self.final, '')
            self.fill(self.detail, str(exc))
            for button in (self.pre_button, self.final_button, self.save_button):
                button.configure(state='disabled')
            self.status.set(str(exc))

    def copy_field(self, field):
        if self.result is not None:
            self.copy_text(getattr(self.result, field))

    def copy_text(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status.set('Скопировано в буфер обмена.')

    def save(self):
        if self.result is None:
            return
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension='.txt', initialfile='leve_crafting_list.txt', filetypes=[('Текст', '*.txt')])
        if not path:
            return
        try:
            from pathlib import Path
            self.result.name = self.name.get().strip() or self.auto_name
            Path(path).write_text(self.result.document(self.data), encoding='utf-8-sig')
            self.status.set('Список сохранён: ' + path)
        except OSError as exc:
            messagebox.showerror('Сохранение списка', str(exc), parent=self.root)
