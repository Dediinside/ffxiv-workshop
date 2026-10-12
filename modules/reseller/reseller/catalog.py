"""All marketable global-client items, plus independently refreshed DC membership."""
import csv
from datetime import datetime, timezone
import io
import json
from pathlib import Path
from .net import Http
from .storage import atomic_json

ROOT = Path(__file__).resolve().parents[1]
RAW = 'https://raw.githubusercontent.com/xivapi/ffxiv-datamining/'


def validate(data):
    if not isinstance(data, dict) or data.get('schema') != 1 or not isinstance(data.get('items'), dict) or len(data['items']) < 1000:
        raise ValueError('Неполный каталог предметов')
    for item, row in data['items'].items():
        if int(item) <= 0 or not row['name'] or not isinstance(row['hq'], bool) or not row['category']:
            raise ValueError('Некорректный предмет')
    return data


def load(folder):
    return validate(json.loads(Path(folder).read_text(encoding='utf-8')))


def load_catalog(folder):
    try:
        return load(Path(folder) / 'catalog.json')
    except (OSError, ValueError, KeyError, TypeError):
        return load(ROOT / 'data' / 'catalog.json')


def validate_worlds(data):
    if not isinstance(data, dict) or not isinstance(data.get('worlds'), dict) or not isinstance(data.get('dcs'), list):
        raise ValueError('Некорректные миры/DC')
    if not data['dcs'] or not all(dc.get('name') and dc.get('worlds') and all(str(w) in data['worlds'] for w in dc['worlds']) for dc in data['dcs']):
        raise ValueError('Неполный список миров/DC')
    return data


def load_worlds(folder):
    for path in (Path(folder) / 'worlds.json', ROOT / 'data' / 'worlds.json'):
        try:
            return validate_worlds(json.loads(path.read_text(encoding='utf-8')))
        except (OSError, ValueError, TypeError, KeyError):
            continue
    raise ValueError('Не удалось загрузить миры/DC')


def refresh_worlds(cache, folder, cancel=None):
    worlds, _ = cache.get('https://universalis.app/api/v2/worlds', 86400, True, cancel)
    dcs, _ = cache.get('https://universalis.app/api/v2/data-centers', 86400, True, cancel)
    if not isinstance(worlds, list) or not isinstance(dcs, list):
        raise ValueError('Некорректный список миров Universalis')
    valid = [d for d in dcs if d.get('region') in ('Japan', 'Europe', 'North-America', 'Oceania')]
    ids = {w for dc in valid for w in dc['worlds']}
    data = validate_worlds({'worlds': {str(w['id']): w['name'] for w in worlds if w['id'] in ids},
                            'dcs': valid, 'fetched_at': datetime.now(timezone.utc).isoformat()})
    atomic_json(Path(folder) / 'worlds.json', data)
    return data


def normalize(item_csv, category_csv, metadata):
    categories = {int(r['#']): r['Name'] for r in csv.DictReader(io.StringIO(category_csv))}
    items = {}
    for row in csv.DictReader(io.StringIO(item_csv)):
        item = int(row['#'])
        if item > 0 and row['Name'] and int(row['ItemSearchCategory']) > 0 and row['IsUntradable'] == 'False':
            items[str(item)] = {'name': row['Name'], 'category': categories.get(int(row['ItemUICategory'])) or 'Other',
                                'hq': row['CanBeHq'] == 'True'}
    return validate({'schema': 1, 'metadata': metadata, 'items': items})


def refresh_catalog(folder, cancel=None):
    # CSV transport uses the same bounded HTTP policy and rate limiter.
    from urllib.request import Request
    from .net import ApiError, check
    http = Http()
    commit = http.get('https://api.github.com/repos/xivapi/ffxiv-datamining/commits?path=csv/en&per_page=1', cancel)[0]
    sha = commit['sha']
    if len(sha) != 40 or any(c not in '0123456789abcdef' for c in sha):
        raise ValueError('Некорректная версия игровых данных')
    sheets = []
    for name in ('Item', 'ItemUICategory'):
        check(cancel)
        request = Request(RAW + sha + '/csv/en/' + name + '.csv', headers={'User-Agent': 'FFXIVResellerParadise/0.1'})
        try:
            with http.opener.open(request, timeout=30) as response:
                raw = response.read(32 * 1024 * 1024 + 1)
            if len(raw) > 32 * 1024 * 1024:
                raise ValueError('Игровая таблица слишком большая')
            sheets.append(raw.decode('utf-8-sig'))
        except OSError:
            raise ApiError('Игровые таблицы недоступны; сохранён прежний каталог') from None
    data = normalize(*sheets, dict(source='https://github.com/xivapi/ffxiv-datamining', commit=sha,
                                  upstream_date=commit['commit']['committer']['date'],
                                  fetched_at=datetime.now(timezone.utc).isoformat()))
    check(cancel)
    atomic_json(Path(folder) / 'catalog.json', data)
    return data
