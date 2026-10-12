"""Validated persistent settings independent of the unpacked app directory."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path

JOBS = ('Carpenter', 'Blacksmith', 'Armorer', 'Goldsmith', 'Leatherworker', 'Weaver',
        'Alchemist', 'Culinarian', 'Miner', 'Botanist', 'Fisher')


def settings_path():
    folder = Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local' / 'share'))
    return folder / 'FFXIVMarketScout' / 'settings.json'


def default_settings():
    return dict(schema=1, sale_world='Odin', purchase_scope='Light', budget=None,
                batch_size=10, scan_limit=600, detail_limit=30, buy_tax=5, sell_tax=5,
                food=0, medicine=0, repair=0, travel=0, other=0, craft_per=0,
                min_units_day=1, min_deals_week=3, max_sale_age_hours=72,
                show_illiquid=False, catalog_search='', analysis_mode='all', analysis_job='all',
                consumables=[], item_profiles={}, geometry='1320x880',
                professions={job: dict(enabled=True, level=1, hq=False, books=False,
                                      specialist=False, rate=0, per_unit=0,
                                      window_yield=0, bait_per_unit=None) for job in JOBS})


def number(value, name, minimum=0, maximum=1e12, integer=False, nullable=False):
    if nullable and (value is None or isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError(f'{name}: требуется число')
    try:
        n = float(str(value).strip().replace(' ', '').replace(',', '.'))
    except (TypeError, ValueError):
        raise ValueError(f'{name}: требуется число') from None
    if not math.isfinite(n) or not minimum <= n <= maximum or integer and n != int(n):
        raise ValueError(f'{name}: допустимо {minimum:g}–{maximum:g}' + (' целых' if integer else ''))
    return int(n) if integer else n


def normalize_settings(raw):
    if not isinstance(raw, dict) or raw.get('schema', 1) != 1:
        raise ValueError('Неизвестная версия настроек')
    result = default_settings()
    result['recursive_craft'] = bool(raw.get('recursive_craft', False))
    for key in ('sale_world', 'purchase_scope'):
        value = str(raw.get(key, result[key])).strip()
        if not value or len(value) > 80 or any(c in value for c in '/?#'):
            raise ValueError(f'{key}: некорректный мир/DC')
        result[key] = value
    result['budget'] = number(raw.get('budget'), 'Бюджет', nullable=True)
    for key, maximum in [('batch_size', 20000), ('scan_limit', 5000), ('detail_limit', 100)]:
        result[key] = number(raw.get(key, result[key]), key, 1, maximum, integer=True)
    for key in ('buy_tax', 'sell_tax'):
        result[key] = number(raw.get(key, result[key]), key, 0, 20)
    for key in ('food', 'medicine', 'repair', 'travel', 'other', 'craft_per'):
        result[key] = number(raw.get(key, 0), key)
    for key in ('min_units_day', 'max_sale_age_hours'):
        result[key] = number(raw.get(key, result[key]), key, 0, 100000)
    result['min_deals_week'] = number(raw.get('min_deals_week', 3), 'Минимум сделок/неделю', 1, 99999, True)
    if not isinstance(raw.get('show_illiquid', False), bool):
        raise ValueError('Некорректный фильтр спроса')
    result['show_illiquid'] = raw.get('show_illiquid', False)
    result['catalog_search'] = str(raw.get('catalog_search', '')).strip()[:200]
    for key, allowed in (('analysis_mode', {'all', 'craft', 'gather'}),
                         ('analysis_job', {'all', *JOBS})):
        value = raw.get(key, 'all')
        if value not in allowed:
            raise ValueError(f'{key}: некорректный отбор')
        result[key] = value
    profiles = raw.get('professions', {})
    if not isinstance(profiles, dict):
        raise ValueError('Некорректные настройки профессий')
    for job in JOBS:
        profile = profiles.get(job, {})
        if not isinstance(profile, dict):
            raise ValueError(f'Некорректные настройки {job}')
        for key in ('enabled', 'hq', 'books', 'specialist'):
            value = profile.get(key, result['professions'][job][key])
            if not isinstance(value, bool):
                raise ValueError(f'{job}/{key}: требуется галочка')
            result['professions'][job][key] = value
        p = result['professions'][job]
        p['level'] = number(profile.get('level', 1), f'{job} Lv', 1, 100, integer=True)
        for key in ('rate', 'per_unit'):
            p[key] = number(profile.get(key, 0), f'{job}/{key}')
        p['window_yield'] = number(profile.get('window_yield', 0), f'{job}/урожай', 0, 20000, integer=True)
        p['bait_per_unit'] = number(profile.get('bait_per_unit'), f'{job}/приманки', nullable=True)
    consumables = raw.get('consumables', [])
    if not isinstance(consumables, list) or len(consumables) > 40:
        raise ValueError('Допустимо до 40 расходников')
    for entry in consumables:
        if not isinstance(entry, dict) or entry.get('mode', 'all') not in ('all', 'craft', 'gather'):
            raise ValueError('Некорректный расходник')
        if not isinstance(entry.get('hq', False), bool):
            raise ValueError('Некорректное качество расходника')
        result['consumables'].append(dict(item=number(entry.get('item'), 'ID расходника', 1, 1000000, True),
                                         quantity=number(entry.get('quantity'), 'Кол-во расходника', 1, 20000, True),
                                         hq=entry.get('hq', False), mode=entry.get('mode', 'all')))
    item_profiles = raw.get('item_profiles', {})
    if not isinstance(item_profiles, dict):
        raise ValueError('Некорректные личные замеры')
    for item, profile in item_profiles.items():
        item = str(number(item, 'ID замера', 1, 1000000, True))
        if not isinstance(profile, dict):
            raise ValueError('Некорректный личный замер')
        result['item_profiles'][item] = dict(rate=number(profile.get('rate', 0), 'Скорость'),
                                             per_unit=number(profile.get('per_unit', 0), 'Затраты/шт'),
                                             window_yield=number(profile.get('window_yield', 0), 'Урожай/окно', 0, 20000, True),
                                             bait_per_unit=number(profile.get('bait_per_unit'), 'Приманки/рыбу', nullable=True))
    geometry = str(raw.get('geometry', result['geometry']))
    import re
    if re.fullmatch(r'\d{3,4}x\d{3,4}(?:[+-]\d+[+-]\d+)?', geometry):
        result['geometry'] = geometry
    return result


def save_settings(path, settings):
    from .net import atomic_json
    value = normalize_settings(deepcopy(settings))
    atomic_json(path, value)
    return value


def load_settings(path):
    path = Path(path)
    if not path.exists():
        return default_settings(), []
    try:
        return normalize_settings(json.loads(path.read_text(encoding='utf-8-sig'))), []
    except (ValueError, OSError, TypeError) as exc:
        warning = f'Настройки восстановлены по умолчанию: {exc}'
        try:
            backup = path.with_name(path.name + '.broken-' + datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S'))
            backup.write_bytes(path.read_bytes())
            warning += '; исходный файл сохранён рядом как .broken'
        except OSError:
            pass
        return default_settings(), [warning]
