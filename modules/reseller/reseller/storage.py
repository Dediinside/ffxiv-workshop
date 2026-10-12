"""Persistent profiles and Windows DPAPI protection for the bot token."""
import base64
import ctypes
from ctypes import wintypes
import json
import math
import os
from pathlib import Path


def home():
    return Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local' / 'share')) / 'FFXIVResellerParadise'


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


def defaults():
    return dict(schema=1, sale_world='Shiva', mode='all', watch_ids='', category='Все',
                quality='Все', budget=None, min_profit=1000, min_roi=20,
                min_discount=20, min_units_day=3, min_deals_week=5,
                max_sale_hours=24, max_upload_minutes=120, max_days=3,
                market_share=25, buy_tax=5, sell_tax=5, travel=0, undercut=1,
                interval_minutes=5, overview_limit=3000, detail_limit=100,
                notify=False, chat_id='', alert_limit=10, cooldown_minutes=120,
                stress_drop=10, reserve_percent=20, min_active_days=3,
                nearby_percent=5, max_supply_days=7, priority='Оборот прибыли', hot_improvement=25)


def normalize(raw):
    if not isinstance(raw, dict) or raw.get('schema', 1) != 1:
        raise ValueError('Неизвестный формат настроек')
    result = defaults()
    for key in ('sale_world', 'watch_ids', 'category', 'chat_id'):
        result[key] = str(raw.get(key, result[key])).strip()[:2000]
    for key, allowed in [('mode', ('all', 'watch')), ('quality', ('Все', 'NQ', 'HQ')),
                         ('priority', ('Оборот прибыли', 'Отдача капитала', 'Прибыль стака'))]:
        result[key] = raw.get(key, result[key])
        if result[key] not in allowed:
            raise ValueError('Некорректный режим / качество')
    if not isinstance(raw.get('notify', False), bool):
        raise ValueError('Некорректная настройка Telegram')
    result['notify'] = raw.get('notify', False)
    bounds = dict(budget=(0, 1e12), min_profit=(0, 1e12), min_roi=(0, 100000),
                  min_discount=(0, 99), min_units_day=(0, 1000000), min_deals_week=(1, 100000),
                  max_sale_hours=(1, 168), max_upload_minutes=(1, 1440), max_days=(.1, 30),
                  market_share=(1, 100), buy_tax=(0, 20), sell_tax=(0, 20), travel=(0, 1e9),
                  undercut=(0, 1000000), interval_minutes=(1, 1440), overview_limit=(100, 50000),
                  detail_limit=(1, 1000), alert_limit=(1, 30), cooldown_minutes=(1, 10080),
                  stress_drop=(0, 50), reserve_percent=(0, 90), min_active_days=(1, 7),
                  nearby_percent=(0, 50), max_supply_days=(.1, 100), hot_improvement=(5, 90))
    integers = {'min_deals_week', 'undercut', 'overview_limit', 'detail_limit', 'alert_limit', 'min_active_days'}
    for key, (lo, hi) in bounds.items():
        value = raw.get(key, result[key])
        if key == 'budget' and (value is None or str(value).strip() == ''):
            result[key] = None
            continue
        try:
            number = float(str(value).strip().replace(' ', '').replace(',', '.'))
        except (ValueError, TypeError):
            raise ValueError(f'{key}: требуется число') from None
        if isinstance(value, bool) or not math.isfinite(number) or not lo <= number <= hi or key in integers and number != int(number):
            raise ValueError(f'{key}: допустимо {lo:g}–{hi:g}' + (' целых' if key in integers else ''))
        result[key] = int(number) if key in integers else number
    if not result['sale_world']:
        raise ValueError('Выберите мир продажи')
    return result


def load_settings(folder=None):
    try:
        return normalize(json.loads(((folder or home()) / 'settings.json').read_text(encoding='utf-8'))), ''
    except FileNotFoundError:
        return defaults(), ''
    except (OSError, ValueError, TypeError):
        return defaults(), 'Настройки повреждены: загружены значения по умолчанию.'


def protect(value, decrypt=False):
    if os.name != 'nt':
        raise ValueError('Сохранение токена доступно на Windows. На этой системе токен можно вводить на каждый запуск.')
    class Blob(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]
    raw = base64.b64decode(value, validate=True) if decrypt else value.encode('utf-8')
    buf = ctypes.create_string_buffer(raw)
    src = Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    dest = Blob()
    api = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    # UI_FORBIDDEN; user account binding, no machine-wide flag.
    if not api(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(dest)):
        raise ValueError('Windows не удалось защитить / прочитать токен')
    try:
        result = ctypes.string_at(dest.pbData, dest.cbData)
        return result.decode('utf-8') if decrypt else base64.b64encode(result).decode('ascii')
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(dest.pbData)


def save_token(value, folder=None):
    path = (folder or home()) / 'telegram-secret.json'
    atomic_json(path, {'dpapi': protect(value)})


def load_token(folder=None):
    try:
        raw = json.loads(((folder or home()) / 'telegram-secret.json').read_text(encoding='utf-8'))
        return protect(raw['dpapi'], True), ''
    except FileNotFoundError:
        return '', ''
    except (OSError, ValueError, KeyError, TypeError):
        return '', 'Сохранённый токен не прочитан: введите его заново во вкладке Telegram.'
