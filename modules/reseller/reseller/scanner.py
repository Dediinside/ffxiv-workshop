"""Rotating overview coverage prevents permanent preference for low item IDs."""
from dataclasses import dataclass, field
from .analysis import analyze, basket
from .market import Market, demo_market
from .net import check


@dataclass
class Result:
    deals: list = field(default_factory=list)
    reasons: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    universe: int = 0
    overview: int = 0
    candidates: int = 0
    detailed: int = 0
    wrapped: bool = False
    basket: list = field(default_factory=list)


def watch_ids(text, items):
    text = text.replace(';', ',').replace('\n', ',').replace(' ', ',')
    try:
        values = sorted({int(p) for p in text.split(',') if p})
    except ValueError:
        raise ValueError('В список отслеживания введите ID предметов через запятую') from None
    if not values:
        raise ValueError('Список ID пуст')
    missing = [i for i in values if i not in items]
    if missing:
        raise ValueError('В каталоге нет рыночных предметов: ' + ', '.join(map(str, missing[:20])))
    return values


class Scanner:
    def __init__(self, items, regions, cache, demo=False):
        self.items, self.regions, self.cache, self.demo = items, regions, cache, demo
        self.cursors = {}
        self.checked = {}
        self.cycle = 0

    def world(self, name):
        for dc in self.regions['dcs']:
            for world in dc['worlds']:
                if self.regions['worlds'].get(str(world)) == name:
                    return world, dc
        raise ValueError('Мир продажи отсутствует в каталоге DC')

    def scan(self, s, force=False, progress=lambda x: None, cancel=None):
        world, dc = self.world(s['sale_world'])
        ids = watch_ids(s['watch_ids'], self.items) if s['mode'] == 'watch' else sorted(self.items)
        if s['category'] != 'Все':
            ids = [i for i in ids if self.items[i]['category'] == s['category']]
        if not ids:
            raise ValueError('Нет товаров в выбранной категории')
        key = (world, s['mode'], s['watch_ids'], s['category'])
        cursor = self.cursors.get(key, 0) % len(ids)
        count = min(len(ids), s['overview_limit'])
        batch = (ids + ids)[cursor:cursor + count]
        result = Result(universe=len(ids), overview=len(batch), wrapped=cursor + count >= len(ids))
        market = Market(self.cache)
        if self.demo or s['mode'] == 'watch':
            candidates = batch
        else:
            scores = market.overview(world, batch, s, force, progress, cancel)
            candidates = sorted(scores, key=lambda i: (-scores[i], i))
            if market.warnings:
                result.warnings += market.warnings
        # Oldest unchecked candidates first; score breaks ties. Stable across score reordering.
        result.candidates = len(candidates)
        positions = {item: index for index, item in enumerate(candidates)}
        candidates.sort(key=lambda item: (self.checked.get((world, item), -1), positions[item]))
        detail = candidates[:s['detail_limit']]
        snapshots = demo_market(detail, world, dc['worlds']) if self.demo else market.detailed(dc['name'], world, detail, force, progress, cancel)
        check(cancel)
        result.detailed = len(detail)
        result.deals, result.reasons = analyze(self.items, snapshots, s,
                                              {int(k): v for k, v in self.regions['worlds'].items()}, dc['worlds'])
        result.basket = basket(result.deals, s)
        self.cycle += 1
        for item in detail:
            self.checked[world, item] = self.cycle
        self.cursors[key] = (cursor + count) % len(ids)
        # Avoid growth from old candidate-set keys over long monitoring sessions.
        if len(self.cursors) > 200:
            self.cursors = {key: self.cursors[key]}
        return result
