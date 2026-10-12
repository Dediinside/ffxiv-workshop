"""A version-pinned catalog from extracted game sheets, independent of market prices."""
from collections import Counter
import csv
from datetime import datetime, timezone
import io
import json
from pathlib import Path

from .net import ApiError, atomic_json, download

CRAFT_JOBS = ("Carpenter", "Blacksmith", "Armorer", "Goldsmith", "Leatherworker",
              "Weaver", "Alchemist", "Culinarian")
GATHER_JOBS = ("Miner", "Botanist", "Fisher")
JOBS = CRAFT_JOBS + GATHER_JOBS
SOURCE = "https://github.com/xivapi/ffxiv-datamining"
RAW = "https://raw.githubusercontent.com/xivapi/ffxiv-datamining/"
COMMIT_URL = "https://api.github.com/repos/xivapi/ffxiv-datamining/commits?path=csv/en&per_page=1"
SHEETS = ("Item", "ItemUICategory", "Recipe", "RecipeLevelTable", "GatheringItem",
          "GatheringItemLevelConvertTable", "GatheringPointBase", "GatheringPoint",
          "GatheringPointTransient", "GatheringRarePopTimeTable", "GatheringSubCategory",
          "PlaceName", "FishParameter", "FishingSpot", "SpearfishingItem", "SpearfishingNotebook")


def _integer(row, key, default=0):
    value = row.get(key, default)
    return int(value) if value not in (None, "") else default


def _true(value):
    return value is True or str(value).lower() == "true"


def _hour(value):
    """Sheet start/end times store HHMM. RarePop durations use a separate encoding."""
    value = int(value)
    hours, minutes = divmod(value, 100)
    if not 0 <= hours <= 24 or not 0 <= minutes < 60 or (hours == 24 and minutes):
        raise ValueError(f"Некорректное время ET: {value}")
    return hours + minutes / 60


def node_schedule(point, transient, rare_tables):
    """Preserve unknown schedules; sentinel and midnight are different values."""
    if transient is None:
        return "unknown", [], ["Расписание узла отсутствует в каталоге"]
    rare = _integer(transient, "GatheringRarePopTimeTable")
    if rare:
        table = rare_tables.get(rare)
        if table is None:
            raise ValueError(f"Отсутствует таблица расписания #{rare}")
        windows = []
        for i in range(3):
            start = _integer(table, f"StartTime[{i}]", 65535)
            duration = _integer(table, f"Duration[{i}]")
            if start != 65535 and duration:
                # These legacy encodings are also decoded by Teamcraft's nodes
                # extractor: 160 -> 120 ET minutes, 300 -> 180 ET minutes.
                # Refuse future unknown encodings rather than inventing a window.
                if duration not in (160, 300):
                    raise ValueError(f"Неизвестная длительность RarePop: {duration}")
                windows.append({"start_hour": _hour(start) % 24,
                                "duration_hours": {160: 2, 300: 3}[duration]})
        if windows:
            return "timed", windows, []
        return "unknown", [], ["Ограниченный узел без известного окна"]
    start = _integer(transient, "EphemeralStartTime", 65535)
    end = _integer(transient, "EphemeralEndTime", 65535)
    if start != 65535 and end != 65535:
        begin, finish = _hour(start) % 24, _hour(end) % 24
        duration = (finish - begin) % 24
        if duration:
            return "ephemeral", [{"start_hour": begin, "duration_hours": duration}], []
        return "unknown", [], ["Неоднозначное ephemeral-окно (начало совпадает с концом)"]
    if _integer(point, "Type") == 1:
        return "always", [], []
    return "unknown", [], ["Узел имеет дополнительные условия появления"]


def normalize(sheets, metadata):
    """Join actual sources and recipes rather than inferring gathering from item categories."""
    def keyed(name):
        return {int(row["#"]): row for row in sheets[name]}

    raw_items, categories = keyed("Item"), keyed("ItemUICategory")
    items = {}
    for item_id, row in raw_items.items():
        if item_id <= 0 or not row.get("Name"):
            continue
        items[item_id] = {"name": row["Name"], "hq": _true(row.get("CanBeHq")),
                          "marketable": (not _true(row.get("IsUntradable"))
                                         and _integer(row, "ItemSearchCategory") > 0
                                         and not _true(row.get("AlwaysCollectable"))),
                          "category": categories.get(_integer(row, "ItemUICategory"), {}).get("Name", "Other")}

    def marketable(item_id):
        return item_id in items and items[item_id]["marketable"]

    recipe_levels = keyed("RecipeLevelTable")
    recipes = []
    for row in sheets["Recipe"]:
        output, craft_type = _integer(row, "ItemResult"), _integer(row, "CraftType")
        if not marketable(output) or not 0 <= craft_type < len(CRAFT_JOBS):
            continue
        # Quest turn-ins, required states and expert/collectable recipes do not belong
        # in ordinary commercial craft recommendations.
        if any(_integer(row, field) for field in ("Quest", "RequiredQuality", "StatusRequired", "ItemRequired")):
            continue
        if _true(row.get("IsExpert")):
            continue
        level = recipe_levels[_integer(row, "RecipeLevelTable")]
        ingredients = Counter()
        for i in range(8):
            item, amount = _integer(row, f"Ingredient[{i}]"), _integer(row, f"AmountIngredient[{i}]")
            if item > 0 and amount > 0:
                ingredients[item] += amount
        if not ingredients or _integer(level, "ClassJobLevel") < 1:
            continue
        recipes.append({"id": _integer(row, "#"), "item": output, "job": CRAFT_JOBS[craft_type],
                        "level": _integer(level, "ClassJobLevel"), "stars": _integer(level, "Stars"),
                        "yield": _integer(row, "AmountResult"), "ingredients": dict(ingredients),
                        "hq": _true(row.get("CanHq")) and items[output]["hq"],
                        "book": _integer(row, "SecretRecipeBook"),
                        "specialist": _true(row.get("IsSpecializationRequired")),
                        "craftsmanship": _integer(row, "RequiredCraftsmanship"),
                        "control": _integer(row, "RequiredControl")})

    converted = keyed("GatheringItemLevelConvertTable")
    gathering_items = keyed("GatheringItem")
    bases, transients = keyed("GatheringPointBase"), keyed("GatheringPointTransient")
    rare, subcategories, places = keyed("GatheringRarePopTimeTable"), keyed("GatheringSubCategory"), keyed("PlaceName")
    sources = []

    def gather_level(row):
        index = _integer(row, "GatheringItemLevel")
        if index not in converted:
            raise ValueError(f"Отсутствует GatheringItemLevelConvertTable #{index}")
        result = converted[index]
        return _integer(result, "GatheringItemLevel"), _integer(result, "Stars")

    for point in sheets["GatheringPoint"]:
        base = bases.get(_integer(point, "GatheringPointBase"))
        if base is None or _integer(point, "TerritoryType") <= 1 or _integer(point, "PlaceName") <= 0:
            continue  # unused/client preview records carry no real location
        gather_type = _integer(base, "GatheringType")
        if gather_type not in (0, 1, 2, 3):
            continue  # spearfishing uses the separate notebook below
        job = "Miner" if gather_type < 2 else "Botanist"
        kind, windows, node_conditions = node_schedule(point, transients.get(_integer(point, "#")), rare)
        sub = subcategories.get(_integer(point, "GatheringSubCategory"), {})
        for i in range(8):
            row = gathering_items.get(_integer(base, f"Item[{i}]"))
            if row is None or not marketable(_integer(row, "Item")):
                continue
            level, stars = gather_level(row)
            if level <= 0:
                continue
            quest = _integer(row, "RequiredQuest") or _integer(sub, "Quest")
            conditions = list(node_conditions)
            if _true(row.get("IsHidden")):
                conditions.append("Скрытый предмет: наличие на каждом узле не гарантировано")
            if quest:
                conditions.append("Требуется выполненное задание / разблокировка")
            perception = _integer(row, "PerceptionReq")
            if perception:
                conditions.append(f"Perception: не менее {perception}")
            book = _integer(sub, "Item")
            if book:
                conditions.append("Требуется folklore-книга")
            sources.append({"id": 0, "item": _integer(row, "Item"), "job": job, "level": level,
                            "stars": stars, "location": places[_integer(point, "PlaceName")]["Name"],
                            "kind": kind, "windows": windows, "book": book, "quest": quest,
                            "conditions": conditions, "perception": perception,
                            "hidden": _true(row.get("IsHidden")), "node_id": _integer(point, "#"),
                            "gathering_item_id": _integer(row, "#"), "territory": _integer(point, "TerritoryType"),
                            "fishing_method": None, "conditions_unknown": kind == "unknown"})

    spots = keyed("FishingSpot")
    for row in sheets["FishParameter"]:
        item = _integer(row, "Item")
        if not marketable(item):
            continue
        level, stars = gather_level(row)
        if level <= 0:
            continue
        spot = spots.get(_integer(row, "FishingSpot"), {})
        place = _integer(spot, "PlaceName")
        sub = subcategories.get(_integer(row, "GatheringSubCategory"), {})
        conditions = ["Условия ловли, наживка, погода и вероятность не известны этому каталогу",
                      "Нужно указать расход наживки на одну пойманную рыбу"]
        if _true(row.get("IsHidden")) or _true(spot.get("Rare")):
            conditions.append("Редкая рыба: проверьте дополнительные условия ловли")
        sources.append({"id": 0, "item": item, "job": "Fisher", "level": level, "stars": stars,
                        "location": places.get(place, {}).get("Name", "Место ловли неизвестно"),
                        "kind": "unknown", "windows": [], "book": _integer(sub, "Item"),
                        "quest": _integer(sub, "Quest"), "conditions": conditions,
                        "node_id": _integer(row, "FishingSpot"), "fish_id": _integer(row, "#"),
                        "territory": _integer(spot, "TerritoryType"), "fishing_method": "rod",
                        "conditions_unknown": True})

    notebooks = keyed("SpearfishingNotebook")
    for row in sheets["SpearfishingItem"]:
        item = _integer(row, "Item")
        if not marketable(item):
            continue
        level, stars = gather_level(row)
        if level <= 0:
            continue
        notebook = notebooks.get(_integer(row, "SpearfishingNotebook"), {})
        shadow = _true(notebook.get("IsShadowNode"))
        place = _integer(notebook, "PlaceName")
        sub = subcategories.get(_integer(row, "GatheringSubCategory"), {})
        conditions = ["Spearfishing: требуется разблокировать подводную охоту"]
        if shadow:
            conditions.append("Shadow node: особые условия появления, расписание не известно")
        sources.append({"id": 0, "item": item, "job": "Fisher", "level": level, "stars": stars,
                        "location": places.get(place, {}).get("Name", "Место охоты неизвестно"),
                        "kind": "unknown" if shadow or not notebook else "always", "windows": [],
                        "book": _integer(sub, "Item"), "quest": _integer(sub, "Quest"),
                        "conditions": conditions, "node_id": _integer(notebook, "GatheringPointBase"),
                        "fish_id": _integer(row, "#"), "territory": _integer(notebook, "TerritoryType"),
                        "fishing_method": "spear", "conditions_unknown": shadow or not notebook})

    # Ordinary nodes are duplicated in the sheets for physical spawn positions.
    # Collapse equivalent routes, keeping at least one actual point ID for provenance.
    unique = {}
    for source in sources:
        identity = (source["item"], source["job"], source["level"], source["location"], source["kind"],
                    tuple((w["start_hour"], w["duration_hours"]) for w in source["windows"]),
                    source["book"], source["quest"], tuple(source["conditions"]), source["fishing_method"])
        unique.setdefault(identity, source)
    gathering = sorted(unique.values(), key=lambda s: (s["job"], s["item"], s["location"], s["node_id"]))
    for index, source in enumerate(gathering, 1):
        source["id"] = index
    needed = {r["item"] for r in recipes} | {i for r in recipes for i in r["ingredients"]} | {s["item"] for s in gathering}
    items = {item: items[item] for item in sorted(needed)}
    meta = dict(metadata)
    meta["counts"] = {"items": len(items), "recipes": len(recipes), "gathering": len(gathering),
                      "timed_sources": sum(s["kind"] == "timed" for s in gathering),
                      "ephemeral_sources": sum(s["kind"] == "ephemeral" for s in gathering)}
    result = {"schema": 1, "metadata": meta, "level_cap": max(r["level"] for r in recipes),
              "items": items, "recipes": recipes, "gathering": gathering}
    validate(result)
    return result


def validate(data):
    if data.get("schema") != 1:
        raise ValueError("Неизвестная схема каталога")
    items = {int(key): value for key, value in data["items"].items()}
    if not items or not data["recipes"] or not data["gathering"]:
        raise ValueError("Неполный каталог")
    cap = int(data["level_cap"])
    for recipe in data["recipes"]:
        if recipe["job"] not in CRAFT_JOBS or recipe["item"] not in items or not items[recipe["item"]]["marketable"]:
            raise ValueError("Некорректная профессия / предмет рецепта")
        if not 1 <= recipe["level"] <= cap or recipe["yield"] <= 0 or not recipe["ingredients"]:
            raise ValueError("Некорректный уровень / выход рецепта")
        if any(int(i) not in items or int(qty) <= 0 for i, qty in recipe["ingredients"].items()):
            raise ValueError("Отсутствует ингредиент / некорректное количество")
    for source in data["gathering"]:
        if source["job"] not in GATHER_JOBS or source["item"] not in items or not items[source["item"]]["marketable"]:
            raise ValueError("Некорректный источник сбора")
        if not 1 <= source["level"] <= cap or source["kind"] not in ("always", "timed", "ephemeral", "unknown"):
            raise ValueError("Некорректный уровень / тип узла")
        if source["kind"] in ("timed", "ephemeral") and not source["windows"]:
            raise ValueError("Ограниченный узел без расписания")
        if source["kind"] in ("always", "unknown") and source["windows"]:
            raise ValueError("Окна не соответствуют типу источника")
        for window in source["windows"]:
            if not 0 <= window["start_hour"] < 24 or not 0 < window["duration_hours"] <= 24:
                raise ValueError("Некорректное окно ET")


def load(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    data["items"] = {int(key): value for key, value in data["items"].items()}
    for recipe in data["recipes"]:
        recipe["ingredients"] = {int(key): int(value) for key, value in recipe["ingredients"].items()}
    validate(data)
    return data


def refresh(path, progress=lambda message: None):
    progress("Проверяем версию игровых данных…")
    commit = json.loads(download(COMMIT_URL))[0]
    sha = commit["sha"]
    if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
        raise ApiError("Некорректная версия источника")
    sheets = {}
    for index, name in enumerate(SHEETS, 1):
        progress(f"Игровые таблицы {index}/{len(SHEETS)}: {name}…")
        raw = download(f"{RAW}{sha}/csv/en/{name}.csv", timeout=60)
        sheets[name] = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    now = datetime.now(timezone.utc).isoformat()
    meta = {"source": SOURCE, "commit": sha,
            "upstream_date": commit["commit"]["committer"]["date"],
            "upstream_message": commit["commit"]["message"].splitlines()[0],
            "fetched_at": now, "checked_at": now}
    data = normalize(sheets, meta)
    # Protect a working catalog from an empty/broken extraction after a patch.
    if len(data["recipes"]) < 5000 or len(data["gathering"]) < 1000:
        raise ValueError("Источник вернул неполный каталог; сохранена предыдущая версия")
    atomic_json(path, data)
    return data


def eligible(data, settings):
    """Level/book/specialist eligibility; surface remaining stat/quest requirements."""
    candidates = []
    professions = settings.get("professions", {})
    for recipe in data["recipes"]:
        profile = professions.get(recipe["job"], {})
        if not profile.get("enabled") or recipe["level"] > int(profile.get("level", 0)):
            continue
        if recipe["book"] and not profile.get("books"):
            continue
        if recipe["specialist"] and not profile.get("specialist"):
            continue
        conditions = []
        if recipe["craftsmanship"]:
            conditions.append(f"Craftsmanship: не менее {recipe['craftsmanship']}")
        if recipe["control"]:
            conditions.append(f"Control: не менее {recipe['control']}")
        if recipe["book"]:
            conditions.append("Требуется соответствующая master-книга")
        if recipe["specialist"]:
            conditions.append("Требуется специалист")
        candidates.append({"item": recipe["item"], "job": recipe["job"], "level": recipe["level"],
                           "mode": "craft", "recipe": recipe, "conditions": conditions})
    for source in data["gathering"]:
        profile = professions.get(source["job"], {})
        if not profile.get("enabled") or source["level"] > int(profile.get("level", 0)):
            continue
        if source["book"] and not profile.get("books"):
            continue
        candidates.append({"item": source["item"], "job": source["job"], "level": source["level"],
                           "mode": "gather", "source": source, "conditions": list(source["conditions"])})
    return candidates
