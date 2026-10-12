"""Normalize version-pinned extracted game sheets; keep no wiki-derived reward tables."""
import csv
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import time
from collections import Counter, defaultdict

from .net import ApiError, atomic_json, download

REPO = "https://raw.githubusercontent.com/xivapi/ffxiv-datamining/"
COMMIT_URL = "https://api.github.com/repos/xivapi/ffxiv-datamining/commits?path=csv/en&per_page=1"
JOBS = ("Carpenter", "Blacksmith", "Armorer", "Goldsmith", "Leatherworker", "Weaver", "Alchemist", "Culinarian")
SHEETS = ("Leve", "CraftLeve", "Recipe", "Item", "ParamGrow", "RecipeLevelTable", "PlaceName")


def normalize(sheets, metadata):
    def keyed(name):
        return {int(r["#"]): r for r in sheets[name]}
    leve_rows, item_rows = keyed("Leve"), keyed("Item")
    recipe_levels, places = keyed("RecipeLevelTable"), keyed("PlaceName")
    leves = []
    for c in sheets["CraftLeve"]:
        l = leve_rows[int(c["Leve"])]
        assignment = int(l["LeveAssignmentType"])
        if not 5 <= assignment <= 12 or not l["Name"]:
            continue
        required = Counter()
        for i in range(4):
            item, qty = int(c[f"Item[{i}]"]), int(c[f"ItemCount[{i}]"])
            if item > 0 and qty > 0:
                required[item] += qty
        leves.append({"id": int(l["#"]), "name": l["Name"], "job": JOBS[assignment - 5],
                      "level": int(l["ClassJobLevel"]), "exp": int(l["ExpReward"]),
                      "gil": int(l["GilReward"]), "allowances": int(l["AllowanceCost"]),
                      "max_turnins": int(c["Repeats"]) + 1,
                      "items": dict(required),
                      "location": places.get(int(l["PlaceNameIssued"]), {}).get("Name", ""),
                      "description": l["Description"]})
    target_items = {item for l in leves for item in l["items"]}
    by_item = defaultdict(list)
    for r in sheets["Recipe"]:
        item = int(r["ItemResult"])
        craft_type = int(r["CraftType"])
        if not item or not 0 <= craft_type < 8:
            continue
        if int(r["Quest"]) or int(r["RequiredQuality"]) or r["IsExpert"] == "True":
            continue
        ingredients = Counter()
        for i in range(8):
            ing, qty = int(r[f"Ingredient[{i}]"]), int(r[f"AmountIngredient[{i}]"])
            if ing > 0 and qty > 0:
                ingredients[ing] += qty
        by_item[item].append({"id": int(r["#"]), "item": item, "job": JOBS[craft_type],
                        "level": int(recipe_levels[int(r["RecipeLevelTable"])]["ClassJobLevel"]),
                        "yield": int(r["AmountResult"]), "ingredients": dict(ingredients),
                        "hq": r["CanHq"] == "True", "book": int(r["SecretRecipeBook"]),
                        "specialist": r["IsSpecializationRequired"] == "True"})
    recipes, needed, visited = [], set(target_items), set()
    pending = list(sorted(target_items))
    while pending:
        item = pending.pop()
        if item in visited:
            continue
        visited.add(item)
        for recipe in by_item[item]:
            recipes.append(recipe)
            needed.update(recipe['ingredients'])
            pending.extend(i for i in recipe['ingredients'] if i not in visited)
    recipes.sort(key=lambda r: r['id'])
    items = {i: {"name": item_rows[i]["Name"], "hq": item_rows[i]["CanBeHq"] == "True",
                 "marketable": int(item_rows[i]["ItemSearchCategory"]) > 0 and item_rows[i]["IsUntradable"] == "False"}
             for i in needed}
    xp = {int(r["#"]): int(r["ExpToNext"]) for r in sheets["ParamGrow"] if int(r["ExpToNext"]) > 0}
    cap = max(l["level"] for l in leves)
    cap = ((cap // 10) + 1) * 10
    result = {"schema": 1, "metadata": {**metadata, 'recipe_scope': 'leve-ingredient-closure-v1'}, "level_cap": cap, "leves": leves,
              "recipes": recipes, "items": items, "xp": xp}
    validate(result)
    return result


def validate(data):
    if data["schema"] != 1 or len(data["leves"]) < 500:
        raise ValueError("Неполный каталог / неизвестная схема")
    jobs = {l["job"] for l in data["leves"]}
    if jobs != set(JOBS):
        raise ValueError("В каталоге отсутствуют профессии")
    for l in data["leves"]:
        if l["exp"] <= 0 or l["gil"] < 0 or l["allowances"] <= 0 or not l["items"]:
            raise ValueError("Некорректная награда / количество предметов")
        if not 1 <= l["max_turnins"] <= 6:
            raise ValueError("Неизвестная механика повторных сдач")
        if not all(str(i) in {str(k) for k in data["items"]} for i in l["items"]):
            raise ValueError("Отсутствует предмет задания")
    if any(int(data["xp"].get(str(i), data["xp"].get(i, 0))) <= 0 for i in range(1, data["level_cap"])):
        raise ValueError("Неполная таблица опыта")
    item_ids = {int(i) for i in data['items']}
    for r in data['recipes']:
        if r['yield'] <= 0 or r['job'] not in JOBS or r['item'] not in item_ids:
            raise ValueError('Некорректный рецепт')
        if any(int(i) not in item_ids or qty <= 0 for i, qty in r['ingredients'].items()):
            raise ValueError('Неполные ингредиенты рецепта')


def load(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    validate(data)
    data["items"] = {int(k): v for k, v in data["items"].items()}
    data["xp"] = {int(k): v for k, v in data["xp"].items()}
    for l in data["leves"]:
        l["items"] = {int(k): v for k, v in l["items"].items()}
    for r in data["recipes"]:
        r["ingredients"] = {int(k): v for k, v in r["ingredients"].items()}
    return data


def refresh(path, progress=lambda s: None):
    commit = json.loads(download(COMMIT_URL))[0]
    sha = commit["sha"]
    if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
        raise ApiError("Некорректная версия источника")
    sheets = {}
    for sheet in SHEETS:
        progress(f"Игровые данные: {sheet}…")
        raw = download(f"{REPO}{sha}/csv/en/{sheet}.csv", timeout=60)
        sheets[sheet] = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    meta = {"commit": sha, "upstream_date": commit["commit"]["committer"]["date"],
            "upstream_message": commit["commit"]["message"].splitlines()[0],
            "fetched_at": datetime.now(timezone.utc).isoformat(), "checked_at": time.time(),
            "source": "https://github.com/xivapi/ffxiv-datamining"}
    result = normalize(sheets, meta)
    atomic_json(path, result)
    return load(path)


def available(data, job, level, all_unlocked=False):
    # Include actual two-level expansion tiers within the five-level planning window.
    lower = 1 if all_unlocked else max(1, (level // 5) * 5 - 5)
    return [l for l in data["leves"] if l["job"] == job and lower <= l["level"] <= level]
