"""Local price snapshots for reference after a listing disappears. No alert replay."""
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3

from .analysis import Deal

FRESH = 'Недавно проверено'
STALE = 'Нужно перепроверить'
REMOVED = 'Снято или изменено'


class History:
    def __init__(self, path):
        self.path = Path(path)

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path)
        db.execute('CREATE TABLE IF NOT EXISTS finds (id TEXT PRIMARY KEY, offer TEXT, name TEXT, stamp REAL, data TEXT, removed INTEGER DEFAULT 0)')
        db.execute('CREATE INDEX IF NOT EXISTS finds_stamp ON finds(stamp)')
        db.execute('CREATE INDEX IF NOT EXISTS finds_offer ON finds(offer)')
        return db

    def add(self, deals):
        db = self.connect()
        try:
            with db:
                # Preserve the original price reference even if the same event is retried.
                db.executemany('INSERT OR IGNORE INTO finds VALUES(?,?,?,?,?,0)',
                               [(d.fingerprint, d.offer_key, d.name.casefold(), d.received,
                                 json.dumps(asdict(d), ensure_ascii=False)) for d in deals])
        finally:
            db.close()

    def remove(self, keys):
        db = self.connect()
        try:
            with db:
                db.executemany('UPDATE finds SET removed=1 WHERE offer=?', [(key,) for key in keys])
        finally:
            db.close()

    def recent(self, query='', limit=500):
        db = self.connect()
        try:
            rows = db.execute('SELECT data,removed FROM finds WHERE instr(name,?)>0 ORDER BY stamp DESC,rowid DESC LIMIT ?',
                              (query.casefold().strip(), limit)).fetchall()
        finally:
            db.close()
        result = []
        for raw, removed in rows:
            try:
                d = Deal(**json.loads(raw))
                d.availability = REMOVED if removed else STALE
                result.append(d)
            except (ValueError, TypeError):
                continue
        return result
