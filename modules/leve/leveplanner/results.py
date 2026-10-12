"""Display, filtering and numeric ordering of already calculated plans."""
PRIORITIES = {
    "Максимальная прибыль": ("balance", True),
    "Дешевле закупка": ("cost", False),
    "Меньше принятий leve": ("accepts", False),
    "Меньше allowances": ("allow", False),
    "Меньше сдач": ("turns", False),
}


def money(value, signed=False):
    if value is None:
        return "—"
    return (f"{value:+,}" if signed and value != 0 else f"{value:,}").replace(",", " ")


def column_value(plan, column):
    return {
        "name": plan.leve["name"].casefold(), "level": plan.leve["level"],
        "method": plan.method, "quality": plan.quality,
        "turns": plan.progress.turnins, "accepts": plan.progress.accepts,
        "allow": plan.allowances, "cost": plan.cost, "reward": plan.reward,
        "balance": plan.balance,
        "status": "Доступно" if plan.feasible else "Цена неизвестна" if plan.cost is None else "Лимиты",
    }[column]


def order_indices(plans, column="balance", descending=True, method="Все", quality="Все",
                  only_feasible=False, search=""):
    search = search.strip().casefold()
    indices = [i for i, p in enumerate(plans)
               if (method == "Все" or p.method == method)
               and (quality == "Все" or p.quality == quality)
               and (not only_feasible or p.feasible)
               and (not search or search in p.leve["name"].casefold())]
    # Stable passes: sort by raw values, never the formatted signed strings.
    known = [i for i in indices if column_value(plans[i], column) is not None]
    unknown = [i for i in indices if column_value(plans[i], column) is None]
    known.sort(key=lambda i: (plans[i].allowances, plans[i].progress.turnins))
    known.sort(key=lambda i: column_value(plans[i], column), reverse=descending)
    known.sort(key=lambda i: not plans[i].feasible)
    return known + unknown
