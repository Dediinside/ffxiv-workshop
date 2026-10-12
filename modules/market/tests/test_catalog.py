"""Regression tests for actual source joins, timing and access restrictions."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from marketscout import catalog


def fixture():
    sheets = {name: [] for name in catalog.SHEETS}
    sheets["Item"] = [
        {"#": str(i), "Name": name, "ItemUICategory": "1", "ItemSearchCategory": "1",
         "IsUntradable": "False", "CanBeHq": "True", "AlwaysCollectable": "False"}
        for i, name in ((2, "Fire Shard"), (8, "Fire Crystal"), (100, "Log"),
                        (101, "Lumber"), (102, "Fish"), (103, "Spear Fish"))]
    sheets["ItemUICategory"] = [{"#": "1", "Name": "Materials"}]
    sheets["RecipeLevelTable"] = [{"#": "500", "ClassJobLevel": "100", "Stars": "2"}]
    recipe = {"#": "5", "CraftType": "0", "ItemResult": "101", "RecipeLevelTable": "500",
              "AmountResult": "3", "CanHq": "True", "SecretRecipeBook": "0"}
    for i, (item, quantity) in enumerate(((100, 2), (100, 1), (0, 0), (0, 0),
                                         (0, 0), (0, 0), (2, 5), (8, 2))):
        recipe[f"Ingredient[{i}]"] = str(item)
        recipe[f"AmountIngredient[{i}]"] = str(quantity)
    sheets["Recipe"] = [recipe]
    sheets["GatheringItemLevelConvertTable"] = [
        {"#": "1", "GatheringItemLevel": "1", "Stars": "0"},
        {"#": "600", "GatheringItemLevel": "90", "Stars": "1"}]
    sheets["GatheringItem"] = [{"#": "30", "Item": "100", "GatheringItemLevel": "600",
                               "PerceptionReq": "2000", "RequiredQuest": "0"}]
    sheets["GatheringPointBase"] = [{"#": "7", "GatheringType": "2", "Item[0]": "30", "GatheringLevel": "90"}]
    sheets["GatheringPoint"] = [{"#": "30999", "GatheringPointBase": "7", "PlaceName": "1",
                               "TerritoryType": "135", "Type": "2", "GatheringSubCategory": "0"}]
    sheets["GatheringPointTransient"] = [{"#": "30999", "GatheringRarePopTimeTable": "1",
                                         "EphemeralStartTime": "65535", "EphemeralEndTime": "65535"}]
    sheets["GatheringRarePopTimeTable"] = [{"#": "1", "StartTime[0]": "2300", "Duration[0]": "160",
                                           "StartTime[1]": "1100", "Duration[1]": "160",
                                           "StartTime[2]": "65535", "Duration[2]": "0"}]
    sheets["PlaceName"] = [{"#": "1", "Name": "Test Zone"}]
    sheets["FishParameter"] = [{"#": "1", "Item": "102", "GatheringItemLevel": "600", "FishingSpot": "1"}]
    sheets["FishingSpot"] = [{"#": "1", "PlaceName": "1", "TerritoryType": "135", "Rare": "True"}]
    sheets["SpearfishingItem"] = [{"#": "20000", "Item": "103", "GatheringItemLevel": "600", "SpearfishingNotebook": "0"}]
    sheets["SpearfishingNotebook"] = [{"#": "0", "PlaceName": "1", "TerritoryType": "135", "IsShadowNode": "True"}]
    return sheets


class CatalogTests(unittest.TestCase):
    def test_all_ingredient_slots_quantities_and_yield(self):
        data = catalog.normalize(fixture(), {})
        recipe = data["recipes"][0]
        self.assertEqual(recipe["ingredients"], {100: 3, 2: 5, 8: 2})
        self.assertEqual(recipe["yield"], 3)
        self.assertEqual(recipe["level"], 100)
        self.assertEqual(recipe["stars"], 2)

    def test_gathering_ids_join_to_items_and_convert_levels(self):
        source = next(s for s in catalog.normalize(fixture(), {})["gathering"] if s["job"] == "Botanist")
        self.assertEqual(source["item"], 100)  # Base.Item=30 is a GatheringItem key.
        self.assertEqual(source["level"], 90)  # raw 600 is never compared to character level.
        self.assertEqual(source["location"], "Test Zone")
        self.assertEqual(source["perception"], 2000)

    def test_two_rare_windows_and_legacy_duration(self):
        source = next(s for s in catalog.normalize(fixture(), {})["gathering"] if s["job"] == "Botanist")
        self.assertEqual(source["kind"], "timed")
        self.assertEqual(source["windows"], [{"start_hour": 23, "duration_hours": 2},
                                            {"start_hour": 11, "duration_hours": 2}])
        sheets = fixture()
        sheets["GatheringRarePopTimeTable"][0]["Duration[0]"] = "300"
        data = catalog.normalize(sheets, {})
        self.assertEqual(data["gathering"][0]["windows"][0]["duration_hours"], 3)

    def test_ephemeral_window_crosses_midnight(self):
        kind, windows, conditions = catalog.node_schedule({"Type": "4"},
            {"EphemeralStartTime": "2000", "EphemeralEndTime": "0"}, {})
        self.assertEqual(kind, "ephemeral")
        self.assertEqual(windows, [{"start_hour": 20, "duration_hours": 4}])
        self.assertEqual(conditions, [])

    def test_absent_or_ambiguous_window_not_always(self):
        self.assertEqual(catalog.node_schedule({"Type": "2"}, None, {})[0], "unknown")
        self.assertEqual(catalog.node_schedule({"Type": "4"},
            {"EphemeralStartTime": "0", "EphemeralEndTime": "0"}, {})[0], "unknown")
        self.assertEqual(catalog.node_schedule({"Type": "1"},
            {"EphemeralStartTime": "65535", "EphemeralEndTime": "65535"}, {})[0], "always")

    def test_unknown_rare_duration_rejects_refresh_schema(self):
        sheets = fixture()
        sheets["GatheringRarePopTimeTable"][0]["Duration[0]"] = "999"
        with self.assertRaisesRegex(ValueError, "RarePop"):
            catalog.normalize(sheets, {})

    def test_fishing_does_not_invent_bait_or_permanent_availability(self):
        sources = [s for s in catalog.normalize(fixture(), {})["gathering"] if s["job"] == "Fisher"]
        self.assertEqual(len(sources), 2)
        for source in sources:
            self.assertEqual(source["kind"], "unknown")
            self.assertTrue(source["conditions_unknown"])
            self.assertEqual(source["windows"], [])
        self.assertEqual({s["fishing_method"] for s in sources}, {"rod", "spear"})

    def test_duplicate_node_sources_collapsed(self):
        sheets = fixture()
        point = dict(sheets["GatheringPoint"][0], **{"#": "31000"})
        transient = dict(sheets["GatheringPointTransient"][0], **{"#": "31000"})
        sheets["GatheringPoint"].append(point)
        sheets["GatheringPointTransient"].append(transient)
        self.assertEqual(len(catalog.normalize(sheets, {})["gathering"]), 3)

    def test_level_book_and_specialist_access_independent(self):
        data = catalog.normalize(fixture(), {})
        data["recipes"][0].update(book=8, specialist=True, craftsmanship=4000)
        settings = {"professions": {"Carpenter": {"enabled": True, "level": 99, "books": True, "specialist": True},
                                    "Botanist": {"enabled": True, "level": 90},
                                    "Fisher": {"enabled": False, "level": 100}}}
        self.assertEqual([c["job"] for c in catalog.eligible(data, settings)], ["Botanist"])
        profile = settings["professions"]["Carpenter"]
        profile.update(level=100, books=False)
        self.assertEqual(len(catalog.eligible(data, settings)), 1)
        profile.update(books=True, specialist=False)
        self.assertEqual(len(catalog.eligible(data, settings)), 1)
        profile["specialist"] = True
        craft = next(c for c in catalog.eligible(data, settings) if c["mode"] == "craft")
        self.assertIn("Craftsmanship: не менее 4000", craft["conditions"])

    def test_load_restores_integer_keys(self):
        data = catalog.normalize(fixture(), {})
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "catalog.json"
            file.write_text(json.dumps(data), encoding="utf-8")
            loaded = catalog.load(file)
        self.assertIn(100, loaded["items"])
        self.assertEqual(loaded["recipes"][0]["ingredients"][100], 3)

    def test_failed_refresh_keeps_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "catalog.json"
            file.write_text("original working catalog", encoding="utf-8")
            commit = {"sha": "a" * 40}
            with patch.object(catalog, "download", side_effect=[json.dumps([commit]).encode(), catalog.ApiError("offline")]):
                with self.assertRaises(catalog.ApiError):
                    catalog.refresh(file)
            self.assertEqual(file.read_text(encoding="utf-8"), "original working catalog")

    def test_invalid_output_quantity_and_window_rejected(self):
        data = catalog.normalize(fixture(), {})
        data["recipes"][0]["yield"] = 0
        with self.assertRaises(ValueError):
            catalog.validate(data)
        data = catalog.normalize(fixture(), {})
        data["gathering"][0]["windows"][0]["duration_hours"] = 25
        with self.assertRaises(ValueError):
            catalog.validate(data)

    def test_bundled_catalog_covers_professions_and_timed_materials(self):
        data = catalog.load(Path(__file__).resolve().parents[1] / "data" / "catalog.json")
        self.assertEqual({r["job"] for r in data["recipes"]}, set(catalog.CRAFT_JOBS))
        self.assertEqual({s["job"] for s in data["gathering"]}, set(catalog.GATHER_JOBS))
        self.assertGreater(len(data["recipes"]), 9000)
        self.assertGreater(len(data["gathering"]), 2000)
        self.assertGreater(sum(s["kind"] == "timed" for s in data["gathering"]), 200)
        self.assertGreater(sum(s["kind"] == "ephemeral" for s in data["gathering"]), 90)
        self.assertEqual(len(data["metadata"]["commit"]), 40)
        self.assertTrue(any(2 in r["ingredients"] for r in data["recipes"]))
        self.assertTrue(any(s["item"] == 2 for s in data["gathering"]))


if __name__ == "__main__":
    unittest.main()
