"""Loopback-only server. Session capability required for every API request."""
import csv
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import threading
from urllib.parse import urlsplit, parse_qs
from urllib.request import Request, build_opener
import webbrowser

from reseller.storage import save_token
from reseller.hot import Ledger
from .service import Service, ROOT


class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, service):
        super().__init__(address, Handler)
        self.service, self.token = service, secrets.token_urlsafe(32)
        self.host = f'127.0.0.1:{self.server_port}'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Do not log paths, tokens or Telegram form bodies.

    def send(self, status, payload, mime='application/json; charset=utf-8'):
        data = json.dumps(payload, ensure_ascii=False).encode() if mime.startswith('application/json') else payload
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        return (self.headers.get('Host') == self.server.host
                and self.headers.get('Origin', 'http://' + self.server.host) == 'http://' + self.server.host
                and secrets.compare_digest(self.headers.get('Authorization', '').encode(), ('Bearer ' + self.server.token).encode()))

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path.startswith('/api/'):
            if not self.authorized():
                return self.send(403, dict(error='Откройте приложение через его ярлык'))
            try:
                if parsed.path == '/api/meta':
                    return self.send(200, self.server.service.metadata())
                if parsed.path.startswith('/api/tasks/'):
                    return self.send(200, self.server.service.snapshot(parsed.path.rsplit('/', 1)[1]))
                if parsed.path == '/api/history':
                    from .service import serial
                    rows = self.server.service.history.recent(parse_qs(parsed.query).get('q', [''])[0])
                    return self.send(200, dict(rows=[{**serial(d), 'id': d.fingerprint, 'details': d.detail(), 'buy_url': d.buy_url, 'sale_url': d.sale_url} for d in rows]))
                if parsed.path == '/api/update':
                    from update_checker import check_release
                    return self.send(200, check_release('Dediinside/ffxiv-workshop', '0.1.0'))
                return self.send(404, dict(error='Не найдено'))
            except (ValueError, OSError):
                return self.send(400, dict(error='Не удалось прочитать данные'))
        allowed = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                   '/style.css': ('style.css', 'text/css; charset=utf-8'), '/favicon.svg':('favicon.svg','image/svg+xml')}
        if parsed.path not in allowed or self.headers.get('Host') != self.server.host:
            return self.send(404, b'Not found', 'text/plain')
        name, mime = allowed[parsed.path]
        self.send(200, (ROOT / 'web' / name).read_bytes(), mime)

    def do_POST(self):
        if not self.authorized():
            return self.send(403, dict(error='Нет доступа'))
        try:
            length = int(self.headers.get('Content-Length', 0))
            if not 0 < length <= 4 * 1024 * 1024:
                raise ValueError('Слишком большой запрос')
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError('Некорректный запрос')
            path, service = urlsplit(self.path).path, self.server.service
            if path == '/api/profile':
                world = data.get('sale_world')
                scopes = set(service.regions['worlds'].values()) | {d['name'] for d in service.regions['dcs']}
                if world not in service.regions['worlds'].values() or data.get('purchase_scope') not in scopes:
                    raise ValueError('Выберите доступный мир и область закупки')
                for entry in data.get('market', {}).get('consumables', []):
                    if entry.get('item') not in service.market_data['items']:
                        raise ValueError('В каталоге нет этого расходника')
                old = service.profile.get()['telegram']
                new = data.get('telegram', {})
                if old.get('notify') and (not new.get('notify') or old.get('chat_id') != new.get('chat_id')):
                    for task in list(service.tasks.values()):
                        if task['kind'] in ('reseller', 'monitor', 'hot'):
                            task['cancel'].set()
                return self.send(200, service.profile.save(data))
            if path == '/api/start':
                return self.send(200, service.start(data.get('kind'), data.get('options', {})))
            if path == '/api/cancel':
                service.cancel(data['id'])
                return self.send(200, dict(ok=True))
            if path == '/api/character':
                if data.get('home_world') not in service.regions['worlds'].values():
                    raise ValueError('Мир персонажа отсутствует в глобальном каталоге')
                return self.send(200, service.profile.character(data))
            if path == '/api/token':
                token = str(data.get('token', '')).strip()
                from reseller.telegram import credentials
                credentials(token, str(data.get('chat_id', '')).strip())
                for task in list(service.tasks.values()):
                    if task['kind'] in ('reseller', 'monitor', 'hot'):
                        task['cancel'].set()
                save_token(token, service.folder)
                return self.send(200, dict(ok=True))
            if path == '/api/reset-budget':
                if any(t['kind'] in ('hot', 'reseller', 'monitor') and t['status'] == 'running' for t in service.tasks.values()):
                    raise ValueError('Сначала остановите перекуп и горячий поток')
                ledger = Ledger(service.folder / 'hot-advice.sqlite3')
                ledger.reset(service.reseller_settings({}, service.profile.get()))
                ledger.close()
                return self.send(200, dict(ok=True))
            if path == '/api/shutdown':
                service.close()
                self.send(200, dict(ok=True))
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            return self.send(404, dict(error='Не найдено'))
        except (ValueError, KeyError, TypeError, OSError) as exc:
            self.send(400, dict(error=str(exc) if isinstance(exc, ValueError) else 'Не удалось выполнить действие'))
        except Exception:
            self.send(500, dict(error='Не удалось сохранить данные. Проверьте папку профиля.'))


def serve(folder, demo=False, port=0, open_browser=True):
    folder = Path(folder)
    if open_browser:
        # Reopen a live instance rather than starting a second monitor process.
        try:
            previous = json.loads((folder / 'session.json').read_text(encoding='utf-8'))
            previous_port, previous_token = previous['port'], previous['token']
            if isinstance(previous_port, int) and 1024 <= previous_port <= 65535 and isinstance(previous_token, str) and len(previous_token) < 100:
                from reseller.net import NoRedirect
                address = f'http://127.0.0.1:{previous_port}'
                request = Request(address + '/api/meta', headers={'Authorization':'Bearer ' + previous_token})
                with build_opener(NoRedirect()).open(request, timeout=1) as response:
                    info = json.loads(response.read(1024 * 1024))
                if info.get('version') == '0.1.0' and info.get('demo') == demo:
                    webbrowser.open(address + '/#' + previous_token)
                    return
        except (OSError, ValueError, KeyError, TypeError):
            pass
    service = Service(folder, demo)
    server = Server(('127.0.0.1', port), service)
    url = f'http://{server.host}/#{server.token}'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'session.json').write_text(json.dumps(dict(port=server.server_port, token=server.token)), encoding='utf-8')
    # Local entry point allows reopening the UI without starting another server.
    (folder / 'open.html').write_text('<!doctype html><meta http-equiv="refresh" content="0;url=' + url + '">', encoding='utf-8')
    print('FFXIV Workshop:', url, flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.close()
        server.server_close()
