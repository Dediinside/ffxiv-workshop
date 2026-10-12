"""Non-blocking startup update check for Reseller Paradise."""
import json
import queue
import re
import threading
import urllib.error
import urllib.request
import webbrowser

REPOSITORIES = {'Dediinside/ffxiv-workshop', 'Dediinside/ffxiv-reseller-paradise', 'Dediinside/ffxiv-leve-planner', 'Dediinside/ffxiv-market-scout'}


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r'v?\d+(?:\.\d+){1,2}', value):
        raise ValueError('Номер версии не поддержан')
    pieces = tuple(int(n) for n in value.removeprefix('v').split('.'))
    return pieces + (0,) * (3 - len(pieces))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check_release(repository, current, opener=None):
    if repository not in REPOSITORIES:
        raise ValueError('Неизвестный репозиторий')
    version(current)
    fallback = 'https://github.com/' + repository + '/releases'
    result = dict(status='network', message='Проверка обновлений не завершена.', url=fallback)
    request = urllib.request.Request('https://api.github.com/repos/' + repository + '/releases/latest',
        headers={'Accept': 'application/vnd.github+json',
                 'X-GitHub-Api-Version': '2026-03-10', 'User-Agent': 'FFXIVTools-UpdateCheck/1.0'})
    opener = opener or urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=8) as response:
            body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise ValueError('Ответ слишком большой')
        data = json.loads(body)
        if not isinstance(data, dict) or data.get('draft') or data.get('prerelease'):
            raise ValueError('Ответ не является стабильным релизом')
        tag = data.get('tag_name')
        newer = version(tag) > version(current)
        result.update(status='available' if newer else 'current',
                      message=f'Доступна версия {tag}; установлена {current}.' if newer else f'Версия {current}: обновлений нет.',
                      url=f'https://github.com/{repository}/releases/tag/{tag}')
    except urllib.error.HTTPError as exc:
        result['status'] = 'access' if exc.code in (401, 403, 404) else 'network'
        result['message'] = {401: 'GitHub отклонил запрос обновления.',
                             403: 'GitHub временно отклонил проверку: возможно, достигнут лимит API.',
                             404: 'Проверка не выполнена: репозиторий недоступен или ещё нет опубликованного релиза.'}.get(exc.code, f'Не удалось проверить обновления: GitHub HTTP {exc.code}.')
    except (OSError, ValueError, TypeError, UnicodeError):
        result.update(status='network', message='Не удалось проверить обновления: сеть или некорректный ответ GitHub. Программа продолжает работать.')
    return result


class UpdateBar:
    def __init__(self, root, repository, current, demo=False):
        import tkinter as tk
        from tkinter import ttk
        self.root, self.repository, self.current = root, repository, current
        self.messages, self.busy, self.closed = queue.Queue(), False, False
        self.url = 'https://github.com/' + repository + '/releases'
        self.frame = ttk.Frame(root, padding=(12, 6))
        self.frame.pack(side='bottom', fill='x')
        self.text = tk.StringVar(root, 'DEMO: проверка обновлений отключена.' if demo else 'Проверка обновлений при запуске…')
        ttk.Label(self.frame, textvariable=self.text, wraplength=730).pack(side='left', fill='x', expand=True)
        self.button = ttk.Button(self.frame, text='Проверить обновление', command=self.start)
        self.button.pack(side='left', padx=6)
        ttk.Button(self.frame, text='Открыть релизы ↗', command=lambda: webbrowser.open(self.url)).pack(side='left', padx=6)
        self.poll_timer = root.after(100, self.poll)
        self.start_timer = root.after(700, self.start) if not demo else None
        self.frame.bind('<Destroy>', self.dispose)

    def dispose(self, event):
        if event.widget != self.frame:
            return
        self.closed = True
        for timer in (self.poll_timer, self.start_timer):
            if timer is not None:
                self.root.after_cancel(timer)

    def start(self):
        if self.busy or self.closed:
            return
        self.busy = True
        self.button.configure(state='disabled')
        self.text.set('Проверяю опубликованную версию GitHub…')
        repository, current, messages = self.repository, self.current, self.messages
        def worker():
            try:
                answer = check_release(repository, current)
            except (OSError, ValueError, UnicodeError):
                answer = dict(status='access', message='Не удалось проверить версию GitHub. Программа продолжает работать.',
                              url='https://github.com/' + repository + '/releases')
            messages.put(answer)
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        if self.closed:
            return
        try:
            answer = self.messages.get_nowait()
            self.busy = False
            self.button.configure(state='normal')
            self.text.set(answer['message'])
            self.url = answer['url']
        except queue.Empty:
            pass
        self.poll_timer = self.root.after(100, self.poll)

