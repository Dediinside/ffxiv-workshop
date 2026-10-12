"""Separate Workshop profile. Never writes into the three original apps."""
from copy import deepcopy
import json
import os
from pathlib import Path
import threading
import time

from marketscout.settings import default_settings, normalize_settings
from reseller.storage import atomic_json


def profile_home():
    return Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local' / 'share')) / 'FFXIVWorkshop'


class Profile:
    def __init__(self, folder, demo=False):
        self.folder, self.lock = Path(folder), threading.RLock()
        self.path = self.folder / 'profile.json'
        base = default_settings()
        base.update(sale_world='Shiva', recursive_craft=True)
        self.data = dict(schema=1, professions=base.pop('professions'), market=base,
                         sale_world='Shiva', purchase_scope='Light', character={}, bridge_enabled=False,
                         telegram=dict(chat_id='', notify=False), reseller={}, view_options={})
        self.warning = ''
        try:
            self.data = self.validate(json.loads(self.path.read_text(encoding='utf-8')))
        except FileNotFoundError:
            # Import only non-secret market preferences into a new profile once.
            old = profile_home().parent / 'FFXIVMarketScout' / 'settings.json'
            if not demo:
                try:
                    loaded = normalize_settings(json.loads(old.read_text(encoding='utf-8')))
                    self.data['professions'] = loaded['professions']
                    self.data['market'].update({k:v for k,v in loaded.items() if k not in ('professions','sale_world','purchase_scope')})
                    self.warning = 'Уровни профессий скопированы из Market Scout в отдельный профиль.'
                except (OSError, ValueError, TypeError):
                    pass
        except (OSError, ValueError, TypeError, KeyError):
            self.warning = 'Не удалось прочитать профиль; загружены начальные значения.'

    def validate(self, raw):
        if not isinstance(raw, dict) or raw.get('schema') != 1:
            raise ValueError('Неизвестный формат профиля')
        if not all(isinstance(raw.get(k, {}), dict) for k in ('market','professions','view_options','reseller','character','telegram')):
            raise ValueError('Некорректный формат настроек')
        normalized = normalize_settings({**raw.get('market', {}), 'professions': raw.get('professions', {}),
                                         'sale_world': raw.get('sale_world', 'Shiva'),
                                         'purchase_scope': raw.get('purchase_scope', 'Light'), 'recursive_craft': True})
        return dict(schema=1, professions=normalized.pop('professions'), market=normalized,
                    sale_world=normalized['sale_world'], purchase_scope=normalized['purchase_scope'],
                    character=raw.get('character', {}), bridge_enabled=bool(raw.get('bridge_enabled', False)),
                    telegram=dict(chat_id=str(raw.get('telegram', {}).get('chat_id', '')).strip(),
                                  notify=bool(raw.get('telegram', {}).get('notify', False))),
                    reseller=raw.get('reseller', {}), view_options=raw.get('view_options', {}))

    def get(self):
        with self.lock:
            return deepcopy(self.data)

    def save(self, raw):
        value = self.validate(raw)
        with self.lock:
            atomic_json(self.path, value)
            self.data = value
        return self.get()

    def character(self, raw):
        with self.lock:
            return self._character(raw)

    def _character(self, raw):
        if not isinstance(raw, dict) or raw.get('schema') != 1 or raw.get('source') != 'WorkshopBridge':
            raise ValueError('Нужен снимок WorkshopBridge формата 1')
        if not isinstance(raw.get('jobs'), dict) or not raw['jobs']:
            raise ValueError('В снимке нет профессий')
        stamp = raw.get('updated_at')
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not 0 <= time.time() - stamp <= 600:
            raise ValueError('Снимок персонажа старше 10 минут или имеет неверную дату')
        value = self.get()
        for job, row in raw['jobs'].items():
            if job not in value['professions']:
                continue
            if not isinstance(row, dict):
                raise ValueError('Некорректная запись профессии в снимке')
            level, xp = row.get('level'), row.get('xp')
            if (isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= 100
                or isinstance(xp, bool) or not isinstance(xp, int) or not 0 <= xp <= 100000000):
                raise ValueError(f'Некорректные данные {job}')
            previous_level = value['character'].get('jobs', {}).get(job, {}).get('level')
            enabled = False if level == 0 else True if previous_level == 0 else value['professions'][job]['enabled']
            value['professions'][job].update(level=max(1, level), enabled=enabled)
        name = str(raw.get('home_world', '')).strip()
        if name:
            value['sale_world'] = name
        value['character'] = dict(updated_at=stamp, current_job=raw.get('current_job', ''), jobs=raw['jobs'],
                                  home_world=name, current_world=raw.get('current_world', ''))
        return self.save(value)
