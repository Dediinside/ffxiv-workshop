"""Formatting and filtering; values remain numeric until displayed."""
from urllib.parse import quote

PRIORITIES = {
    "Чистая прибыль партии": ("profit", True),
    "Рентабельность": ("margin", True),
    "Продажи в день": ("sales_day", True),
    "Меньше конкурентов": ("competing_units", False),
    "Дешевле партия": ("cost", False),
    "Прибыль в час": ("hourly", True),
    "Ближайшее окно": ("window_wait", False),
}


def number(value, signed=False, digits=0):
    if value is None:
        return "—"
    spec = f"{'+' if signed else ''},.{digits}f"
    return format(value, spec).replace(",", " ")


def item_url(item_id):
    item = int(item_id)
    if item <= 0:
        raise ValueError("ID товара должен быть положительным")
    return "https://universalis.app/market/" + quote(str(item), safe="")


def order_indices(rows, column="profit", descending=True, mode="Все", quality="Все",
                  job="Все", search="", only_complete=False, min_sales=0,
                  min_profit=None):
    modes = {"Крафт": "craft", "Сбор": "gather"}
    wanted = modes.get(mode, mode)
    indices = []
    for index, row in enumerate(rows):
        if mode != "Все" and row.get("mode") != wanted:
            continue
        if quality != "Все" and row.get("quality") != quality:
            continue
        if job != "Все" and row.get("job") != job:
            continue
        if search.strip().casefold() not in row.get("name", "").casefold():
            continue
        if only_complete and (row.get("cost") is None or row.get("profit") is None):
            continue
        if min_sales and (row.get("sales_day") is None or row["sales_day"] < min_sales):
            continue
        if min_profit is not None and (row.get("profit") is None or row["profit"] < min_profit):
            continue
        indices.append(index)
    def value(index):
        result = rows[index].get(column)
        return result.casefold() if isinstance(result, str) else result
    known = [i for i in indices if value(i) is not None]
    unknown = [i for i in indices if value(i) is None]
    known.sort(key=value, reverse=descending)
    # A budget/availability issue must not promote an unusable profitable plan.
    known.sort(key=lambda i: bool(rows[i].get("issues")))
    return known + unknown
