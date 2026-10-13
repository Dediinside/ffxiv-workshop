"use strict";
document.addEventListener("submit", (ev) => ev.preventDefault());
const sessionToken =
  location.hash.slice(1) || sessionStorage.getItem("workshop-session") || "";
sessionStorage.setItem("workshop-session", sessionToken);
history.replaceState(null, "", location.pathname);
const $ = (s) => document.querySelector(s),
  e = (v) =>
    String(v ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
const num = (v) =>
  v == null
    ? "—"
    : Number(v).toLocaleString("ru-RU", { maximumFractionDigits: 1 });
let meta,
  page = "leve",
  tasks = {},
  results = {},
  selected = {},
  filters = {},
  sorting = {},
  currentCraft;
const pages = {
  leve: [
    "Leves · 5 уровней",
    "Прокачка через leves",
    "Следующий рубеж с учётом EXP, allowances и реальной стоимости цепочки крафта.",
  ],
  route: [
    "Маршрут прокачки",
    "От текущего уровня до цели",
    "Поэтапный маршрут, общая закупка и точный список выбранных заготовок.",
  ],
  market: [
    "Крафт и сбор",
    "Что производить на продажу",
    "Спрос на вашем мире, все материалы, расходники и расписание узлов.",
  ],
  reseller: [
    "Рай перекупа",
    "Найдите выгодную разницу",
    "Покупка на мирах вашего DC и осторожная оценка перепродажи на домашнем мире.",
  ],
  history: [
    "История находок",
    "Цена продажи всегда под рукой",
    "Сохранённые расчёты остаются после покупки, снятия объявления и перезапуска.",
  ],
  profile: [
    "Мои профессии",
    "Один профиль для всех расчётов",
    "Уровни, доступ к книгам и возможность HQ используются во всех инструментах.",
  ],
  settings: [
    "Настройки",
    "Рынки, расходы и уведомления",
    "Общие предпочтения и отдельный доступ к Telegram для нового приложения.",
  ],
};
const icons = {
  leve: "M5 4h14v16H5z M8 8h8 M8 12h8 M8 16h5",
  route: "M5 5h14v5H5z M12 10v5 M5 15h14v5H5z",
  market: "M4 19V9 M10 19V4 M16 19v-7 M3 20h18",
  reseller: "M4 7h16l-2 10H6z M9 7V4h6v3 M9 20h0 M17 20h0",
  history: "M4 7a9 9 0 1 1-1 8 M4 3v5h5 M12 7v6l4 2",
  profile: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8 M4 21v-2a8 8 0 0 1 16 0v2",
  settings: "M4 7h16 M4 17h16 M8 4v6 M16 14v6",
};
async function api(path, data) {
  const response = await fetch("/api/" + path, {
    method: data ? "POST" : "GET",
    headers: {
      Authorization: "Bearer " + sessionToken,
      ...(data ? { "Content-Type": "application/json" } : {}),
    },
    body: data ? JSON.stringify(data) : undefined,
  });
  const body = await response.json();
  if (!response.ok) throw Error(body.error || "Не удалось выполнить запрос");
  return body;
}
function toast(text) {
  $("#toast").textContent = text;
  $("#toast").style.display = "block";
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => ($("#toast").style.display = "none"), 6000);
}
function options(values, value) {
  return values
    .map(
      (x) =>
        `<option value="${e(x)}" ${String(x) === String(value) ? "selected" : ""}>${e(x)}</option>`,
    )
    .join("");
}
function field(label, name, value, type = "number", attrs = "") {
  return `<label>${e(label)}<input name="${name}" type="${type}" value="${e(value)}" ${attrs}></label>`;
}
function select(label, name, values, value) {
  return `<label>${label}<select name="${name}">${options(values, value)}</select></label>`;
}
function check(label, name, value) {
  return `<label class="check"><input type="checkbox" name="${name}" ${value ? "checked" : ""}>${e(label)}</label>`;
}
function scopeOptions() {
  return [...meta.worlds, ...meta.dcs];
}
function profileJob(job) {
  return meta.profile.professions[job] || { level: 1, hq: false };
}
function formValues(form) {
  const result = {};
  for (const input of form.elements) {
    if (!input.name) continue;
    result[input.name] =
      input.type === "checkbox"
        ? input.checked
        : input.type === "number"
          ? input.value === ""
            ? null
            : Number(input.value)
          : input.value;
  }
  return result;
}
function go(name) {
  page = name;
  document
    .querySelectorAll("nav button")
    .forEach((b) => b.classList.toggle("active", b.dataset.page === name));
  $("#title").textContent = pages[name][1];
  $("#subtitle").textContent = pages[name][2];
  $("#crumb").textContent = pages[name][0];
  $("#eyebrow").textContent = ["leve", "route"].includes(name) ? "ПРОКАЧКА ПРОФЕССИЙ" : name === "profile" ? "ВАШ ПЕРСОНАЖ" : "ИНСТРУМЕНТЫ МАСТЕРСКОЙ";
  $("#profile-shortcut").hidden = name === "profile";
  render();
}
function render() {
  const view = $("#view");
  if (page === "profile") {
    view.innerHTML = profilePage();
    bindProfile();
    return;
  }
  if (page === "settings") {
    view.innerHTML = settingsPage();
    bindSettings();
    bindConsumables();
    return;
  }
  if (page === "history") {
    view.innerHTML = `<div class="panel"><div class="section-title">Найти сохранённый расчёт <small>Последние 500 совпадений по всей истории</small></div><div class="form-actions"><input id="history-search" placeholder="Название товара" style="max-width:400px"><button id="history-find">Найти</button></div></div><div id="results"></div>`;
    $("#history-find").onclick = loadHistory;
    $("#history-search").onkeydown = (ev) => {
      if (ev.key === "Enter") loadHistory();
    };
    loadHistory();
    return;
  }
  view.innerHTML = calculationForm() + `<div id="results"></div>`;
  $("#calculate").onclick = () => run(page);
  if ($("#hot-start")) $("#hot-start").onclick = () => run("hot");
  if ($("#monitor-start")) $("#monitor-start").onclick = () => run("monitor");
  $("#stop").onclick = async () => {
    if (tasks[page]) await api("cancel", { id: tasks[page] });
  };
  const job = $("select[name=job]");
  if (job)
    job.onchange = () => {
      const p = profileJob(job.value);
      $("input[name=level]").value = p.level;
      const xp = meta.profile.character?.jobs?.[job.value]?.xp || 0;
      $("input[name=xp]").value = xp;
      $("input[name=craft_hq]").checked = p.hq;
      if ($("input[name=target]"))
        $("input[name=target]").value = Math.min(
          meta.leve_cap,
          Math.max(50, p.level + 5),
        );
    };
  renderResults();
  if (["leve", "market"].includes(page)) {
    for (const name of ["method", "quality", "min_profit", "only_known"]) {
      const input = $("#calculation-form").elements[name];
      if (input) input.addEventListener("change", renderResults);
    }
  }
}
function calculationForm() {
  const p = meta.profile,
    saved = p.view_options?.[page] || {},
    job = saved.job || "Carpenter",
    level = saved.level ?? profileJob(job).level,
    isLeve = page === "leve" || page === "route";
  let fields = "",
    advanced = "",
    actions = "";
  if (isLeve) {
    fields =
      select(
        "Профессия",
        "job",
        Object.keys(p.professions).filter(
          (j) => !["Miner", "Botanist", "Fisher"].includes(j),
        ),
        job,
      ) +
      select(
        "Закупка материалов",
        "scope",
        scopeOptions(),
        saved.scope || p.purchase_scope,
      ) +
      field("Текущий уровень", "level", level, "number", 'min="1" max="99"') +
      field(
        "EXP внутри уровня",
        "xp",
        saved.xp ?? p.character?.jobs?.[job]?.xp ?? 0,
        "number",
        'min="0"',
      );
    if (page === "route")
      fields += field(
        "Желаемый уровень",
        "target",
        saved.target ?? Math.min(meta.leve_cap, Math.max(50, level + 5)),
        "number",
        'min="2" max="100"',
      );
    fields +=
      field(
        "Бюджет gil · пусто = без лимита",
        "budget",
        saved.budget ?? "",
        "number",
        'min="0"',
      ) +
      field(
        "Доступные allowances",
        "allowances",
        saved.allowances ?? 100,
        "number",
        'min="0" max="100"',
      ) +
      select(
        "Способ",
        "method",
        ["Все", "Покупка", "Крафт"],
        saved.method || "Все",
      ) +
      select(
        "Качество",
        "quality",
        ["Все", "NQ", "HQ"],
        saved.quality || "Все",
      );
    if (page === "route")
      fields += `<label>Приоритет<select name="priority"><option value="cost" ${saved.priority === "allowances" ? "" : "selected"}>Дешевле закупка</option><option value="allowances" ${saved.priority === "allowances" ? "selected" : ""}>Меньше allowances</option></select></label>`;
    advanced =
      field(
        "Налог покупки, %",
        "tax",
        saved.tax ?? p.market.buy_tax,
        "number",
        'min="0" max="20" step="0.1"',
      ) +
      field(
        "Дорога за этап, gil",
        "travel",
        saved.travel ?? p.market.travel,
        "number",
        'min="0"',
      );
    actions =
      check(
        "Гарантирую HQ конечного предмета",
        "craft_hq",
        saved.craft_hq ?? profileJob(job).hq,
      ) +
      (page === "leve"
        ? check("Все доступные уровни leves", "all_levels", saved.all_levels)
        : "");
  }
  if (page === "market") {
    fields =
      select(
        "Источник",
        "analysis_mode",
        ["all", "craft", "gather"],
        saved.analysis_mode || "all",
      )
        .replace(">all<", ">Крафт и сбор<")
        .replace(">craft<", ">Крафт<")
        .replace(">gather<", ">Сбор<") +
      select(
        "Профессия",
        "analysis_job",
        ["all", ...Object.keys(p.professions)],
        saved.analysis_job || "all",
      ).replace(">all<", ">Все профессии<") +
      field(
        "Партия, шт.",
        "batch_size",
        saved.batch_size ?? p.market.batch_size,
        "number",
        'min="1" max="20000"',
      ) +
      field(
        "Бюджет gil · пусто = без лимита",
        "budget",
        saved.budget ?? "",
        "number",
        'min="0"',
      ) +
      field(
        "Поиск по названию в каталоге",
        "catalog_search",
        saved.catalog_search || "",
        "text",
      ) +
      field(
        "Мин. продано шт./день",
        "min_units_day",
        saved.min_units_day ?? p.market.min_units_day,
        "number",
        'min="0" step="0.1"',
      ) +
      field(
        "Мин. сделок за неделю",
        "min_deals_week",
        saved.min_deals_week ?? p.market.min_deals_week,
        "number",
        'min="1"',
      ) +
      field(
        "Последняя продажа ≤ часов",
        "max_sale_age_hours",
        saved.max_sale_age_hours ?? p.market.max_sale_age_hours,
        "number",
        'min="1"',
      );
    fields += select("Качество результата", "quality", ["Все", "NQ", "HQ"], saved.quality || "Все")
      + field("Фильтр: мин. прибыль · пусто = все", "min_profit", saved.min_profit ?? 0, "number")
      + check("Только известная себестоимость", "only_known", saved.only_known);
    advanced =
      field(
        "Товаров для отбора",
        "scan_limit",
        saved.scan_limit ?? p.market.scan_limit,
        "number",
        'min="1" max="5000"',
      ) +
      field(
        "Подробно проверить",
        "detail_limit",
        saved.detail_limit ?? p.market.detail_limit,
        "number",
        'min="1" max="100"',
      );
    actions = check(
      "Показать малоликвидные для сравнения",
      "show_illiquid",
      saved.show_illiquid,
    );
  }
  if (page === "reseller") {
    fields =
      field(
        "Общий бюджет · пусто = без лимита",
        "budget",
        saved.budget ?? "",
        "number",
        'min="0"',
      ) +
      select(
        "Качество",
        "quality",
        ["Все", "NQ", "HQ"],
        saved.quality || "Все",
      ) +
      field(
        "Минимальная прибыль стака",
        "min_profit",
        saved.min_profit ?? 1000,
        "number",
        'min="0"',
      ) +
      field(
        "Минимальный ROI, %",
        "min_roi",
        saved.min_roi ?? 20,
        "number",
        'min="0"',
      ) +
      field(
        "Мин. продано шт./день",
        "min_units_day",
        saved.min_units_day ?? 3,
        "number",
        'min="0" step="0.1"',
      ) +
      field(
        "Мин. сделок за неделю",
        "min_deals_week",
        saved.min_deals_week ?? 5,
        "number",
        'min="1"',
      ) +
      field(
        "Последняя продажа ≤ часов",
        "max_sale_hours",
        saved.max_sale_hours ?? p.reseller.max_sale_hours ?? 72,
        "number",
        'min="1" max="168" step="1" title="Давность последней продажи на домашнем мире; после простоя серверов можно увеличить до 48–72 часов"',
      ) +
      field(
        "Срок продажи ≤ дней",
        "max_days",
        saved.max_days ?? 3,
        "number",
        'min="0.1" step="0.1"',
      ) +
      field(
        "Мой список · ID через запятую",
        "watch_ids",
        saved.watch_ids || "",
        "text",
      );
    advanced =
      field(
        "Пауза между циклами, минут",
        "interval_minutes",
        saved.interval_minutes ?? 5,
        "number",
        'min="1" max="1440"',
      ) +
      field(
        "Скидка к истории, %",
        "min_discount",
        saved.min_discount ?? 20,
        "number",
        'min="0" max="99"',
      ) +
      field(
        "Уведомлений за цикл / минуту",
        "alert_limit",
        saved.alert_limit ?? 10,
        "number",
        'min="1" max="30"',
      ) +
      field(
        "Резерв бюджета, %",
        "reserve_percent",
        saved.reserve_percent ?? 20,
        "number",
        'min="0" max="90"',
      ) +
      field(
        "Проверить падение цены на, %",
        "stress_drop",
        saved.stress_drop ?? 10,
        "number",
        'min="0" max="50"',
      ) +
      field(
        "Дорога за покупку, gil",
        "travel",
        saved.travel ?? 0,
        "number",
        'min="0"',
      ) +
      field(
        "Налог покупки, %",
        "buy_tax",
        saved.buy_tax ?? 5,
        "number",
        'min="0" max="20"',
      ) +
      field(
        "Налог продажи, %",
        "sell_tax",
        saved.sell_tax ?? 5,
        "number",
        'min="0" max="20"',
      ) +
      field(
        "Товаров для отбора",
        "overview_limit",
        saved.overview_limit ?? 3000,
        "number",
        'min="100" max="50000"',
      ) +
      field(
        "Подробно проверить",
        "detail_limit",
        saved.detail_limit ?? 100,
        "number",
        'min="1" max="1000"',
      );
  }
  return `<form id="calculation-form" class="panel"><div class="section-title">Параметры расчёта <small>${isLeve ? "Заготовки: купить или изготовить дешевле" : "Продажа: " + e(p.sale_world) + " · закупка: " + e(p.purchase_scope)}</small></div><div class="form-grid">${fields}</div><details><summary>Дополнительные параметры</summary><div class="form-grid" style="margin-top:15px">${advanced}</div></details><div class="form-actions"><button id="calculate" type="button">${page === "route" ? "Построить маршрут" : page === "leve" ? "Рассчитать leves" : page === "market" ? "Анализировать рынок" : "Найти выгодные стаки"}</button>${page === "reseller" ? '<button id="hot-start" class="secondary" type="button">Горячие предложения</button><button id="monitor-start" class="secondary" type="button">Периодический поиск</button>' : ""}<button id="stop" class="secondary" type="button">Остановить</button>${check("Свежие цены", "fresh", saved.fresh)}${actions}</div><div class="note-line">${isLeve ? "EXP крафта не добавляется. Доступность рецептов проверяется по общему профилю и уровню начала этапа." : "Никакие покупки в игре не выполняются. Высокая цена без спроса не становится рекомендацией."}</div></form>`;
}
async function run(kind) {
  try {
    const form = $("#calculation-form");
    if (!form.reportValidity()) return;
    const opts = formValues(form);
    if (["hot", "reseller", "monitor"].includes(kind))
      opts.mode = opts.watch_ids?.trim() ? "watch" : "all";
    meta.profile.view_options ??= {};
    meta.profile.view_options[page] = opts;
    await api("profile", meta.profile);
    const task = await api("start", { kind, options: opts });
    tasks[page] = task.id;
    results[page] = task;
    selected[page] = null;
    renderResults();
    pollTask(page, task.id);
  } catch (err) {
    toast(err.message);
  }
}
async function pollTask(view, id) {
  try {
    const task = await api("tasks/" + id);
    if (tasks[view] !== id) return;
    results[view] = task;
    if (page === view) renderResults();
    if (task.status === "running") setTimeout(() => pollTask(view, id), 1400);
  } catch (err) {
    toast(err.message);
  }
}
function statusClass(row) {
  const status = row.availability;
  return row.recommended
    ? "status-fresh"
    : status === "Снято или изменено"
      ? "status-removed"
      : status === "Нужно перепроверить"
        ? "status-stale"
        : status === "Недавно проверено"
          ? "status-fresh"
          : "";
}
function columns() {
  if (page === "leve" || page === "route")
    return [
      ...(page === "route" ? [["stage", "Этап"]] : []),
      ["name", "Задание"],
      ["level", "Lv"],
      ["method", "Способ"],
      ["quality", "HQ/NQ"],
      ["cost", "Расход gil"],
      ["balance", "Баланс gil"],
      ["allowances", "Allow."],
      ["turnins", "Сдачи"],
      ["feasible", "Доступность"],
    ];
  if (page === "market")
    return [
      ["name", "Товар"],
      ["job", "Профессия"],
      ["level", "Lv"],
      ["quality", "HQ/NQ"],
      ["units", "Шт."],
      ["cost", "Расход"],
      ["profit", "Прибыль"],
      ["sales_day", "Шт./день"],
      ["hourly", "gil/час"],
      ["window_wait", "До окна, мин."],
      ["status", "Оценка"],
    ];
  return [
    ["name", "Товар"],
    ["availability", "Статус"],
    ["sale_price", "Продажа/шт."],
    ["sale_world", "Продать на"],
    ["buy_world", "Купить на"],
    ["hq", "HQ/NQ"],
    ["quantity", "Шт."],
    ["cost", "Расход"],
    ["profit", "Прибыль"],
    ["units_day", "Шт./день"],
    ["recommended", "Корзина"],
  ];
}
function renderResults() {
  const host = $("#results");
  if (!host) return;
  const task = results[page],
    result = task?.result,
    cols = columns();
  let rows = (result?.rows || []).map((r) => {
    if (
      r.availability === "Недавно проверено" &&
      Date.now() / 1000 - r.received >= 90
    )
      return {
        ...r,
        availability: "Нужно перепроверить",
        details: r.details?.replace(
          "Статус объявления: Недавно проверено",
          "Статус объявления: Нужно перепроверить",
        ),
      };
    return r;
  });
  const searchFocus = $("#result-search") === document.activeElement;
  const caret = searchFocus ? $("#result-search").selectionStart : 0;
  if (searchFocus) filters[page] = $("#result-search").value;
  const term = (filters[page] || "").toLowerCase();
  rows = rows.filter((r) => (r.name || "").toLowerCase().includes(term));
  if (page === "leve") {
    const form = $("#calculation-form");
    const mode = form?.elements.method?.value,
      quality = form?.elements.quality?.value;
    rows = rows.filter(
      (r) =>
        (!mode || mode === "Все" || r.method === mode) &&
        (!quality || quality === "Все" || r.quality === quality),
    );
  }
  if (page === "market") {
    const form = $("#calculation-form"), quality = form?.elements.quality?.value;
    const minProfit = form?.elements.min_profit?.value;
    rows = rows.filter(r => (!quality || quality === "Все" || r.quality === quality)
      && (minProfit === "" || minProfit == null || r.profit != null && r.profit >= Number(minProfit))
      && (!form?.elements.only_known?.checked || r.cost != null));
  }
  const sort = sorting[page];
  if (sort)
    rows.sort((a, b) => {
      const av = a[sort.key],
        bv = b[sort.key];
      if (av == null) return 1;
      if (bv == null) return -1;
      return (
        (typeof av === "number"
          ? av - bv
          : String(av).localeCompare(String(bv), "ru")) * (sort.desc ? -1 : 1)
      );
    });
  const active = rows.find((r) => String(r.id) === String(selected[page]));
  const stats = result?.summary || {};
  let cards = "";
  if (page === "route")
    cards =
      stat("Стоимость маршрута", num(stats.cost) + " gil") +
      stat("Нужно EXP", num(stats.needed_xp)) +
      stat("Allowances", num(stats.allowances)) +
      stat("Этапов", num(stats.stages));
  else if (page === "market")
    cards =
      stat("Доступных товаров", num(stats.eligible)) +
      stat("Просмотрено", num(stats.screened)) +
      stat("Подробно проверено", num(stats.detailed)) +
      stat("Вариантов", num(rows.length));
  else if (page === "leve")
    cards =
      stat("Следующий рубеж", "Lv " + num(stats.target)) +
      stat("Рассчитано вариантов", num(stats.plans)) +
      stat("Профессия", $("select[name=job]")?.value || "—") +
      stat("Цепочки заготовок", "Включены");
  else if (page === "reseller")
    cards =
      stat("Найдено", num(rows.length)) +
      stat("Проверено / событий", num(stats.events ?? stats.checked)) +
      stat("Telegram", num(stats.sent)) +
      stat("Рекомендовано затрат", num(stats.spent) + " gil");
  let progress = "";
  if (task)
    progress = `<div class="progress ${task.status === "error" ? "error" : ""}">${task.status === "running" ? '<span class="spinner"></span>' : ""}<span>${e(task.error || task.progress)}</span></div>`;
  const reasonNotes = Object.entries(stats.reasons || {}).map(([reason,count]) => `${reason}: ${count}`);
  const warnings = [...(result?.warnings || []), ...reasonNotes]
    .slice(0, 6)
    .map((w) => `<div class="warning">${e(w)}</div>`)
    .join("");
  const table = rows.length
    ? `<div class="table-wrap"><table><thead><tr>${cols.map(([key, title]) => `<th data-sort="${key}">${e(title)} ${sort?.key === key ? (sort.desc ? "↓" : "↑") : ""}</th>`).join("")}</tr></thead><tbody>${rows
        .map(
          (r) =>
            `<tr data-id="${e(r.id)}" class="${statusClass(r)} ${String(r.id) === String(selected[page]) ? "selected" : ""}">${cols
              .map(([key]) => {
                let value = r[key];
                if (key === "hq") value = value ? "HQ" : "NQ";
                if (key === "feasible")
                  value = value ? page === "route" ? "Цена оценена" : "Доступно" : "Есть ограничения";
                if (key === "recommended")
                  value = value == null ? "—" : value ? "Рекомендовано" : "Альтернатива";
                if (key === "window_wait" && value != null) value /= 60;
                if (typeof value === "number")
                  value =
                    (key === "cost" && value > 0
                      ? "−"
                      : ["profit", "balance"].includes(key) && value > 0
                        ? "+"
                        : "") + num(value);
                return `<td class="${typeof r[key] === "number" ? "numeric" : ""}">${e(value ?? "—")}</td>`;
              })
              .join("")}</tr>`,
        )
        .join("")}</tbody></table></div>`
    : `<div class="empty"><strong>${task?.status === "running" ? "Ищем подходящие варианты…" : result ? "Нет строк по выбранным условиям" : "Здесь появится ваш расчёт"}</strong>${task?.kind === "hot" ? "Ожидаем новые события Universalis. Старые объявления не подставляются." : result ? "Проверьте параметры, фильтры и причины отсева выше." : "Задайте параметры и запустите расчёт. Никаких запросов до запуска."}</div>`;
  host.innerHTML =
    progress +
    (cards ? `<div class="stats">${cards}</div>` : "") +
    warnings +
    `<div class="result-layout ${active ? "with-detail" : ""}"><section class="panel result-panel"><div class="result-bar"><h2>${page === "history" ? "Сохранённые находки" : "Результаты"} <small style="color:var(--muted);font-weight:400">· ${rows.length}</small></h2><input id="result-search" placeholder="Фильтр по названию" value="${e(filters[page] || "")}"><button id="export-csv" class="quiet">Экспорт CSV ↗</button>${result?.craft_list ? '<button id="route-crafts" class="quiet">Список маршрута ↗</button>' : ""}</div>${table}</section>${active ? detail(active) : ""}</div>`;
  $("#result-search").oninput = (ev) => {
    filters[page] = ev.target.value;
    renderResults();
  };
  if (searchFocus) {
    $("#result-search").focus();
    $("#result-search").setSelectionRange(caret, caret);
  }
  document.querySelectorAll("[data-sort]").forEach(
    (th) =>
      (th.onclick = () => {
        sorting[page] = {
          key: th.dataset.sort,
        desc: sort?.key === th.dataset.sort ? !sort.desc : !["cost","allowances","turnins","level","window_wait","name"].includes(th.dataset.sort),
        };
        renderResults();
      }),
  );
  document.querySelectorAll("tr[data-id]").forEach(
    (tr) =>
      (tr.onclick = () => {
        selected[page] = tr.dataset.id;
        renderResults();
      }),
  );
  $("#export-csv").onclick = () => exportCSV(rows);
  if ($("#route-crafts"))
    $("#route-crafts").onclick = () => showCraft(result.craft_list);
  if (active) {
    if ($("#copy-price"))
      $("#copy-price").onclick = () =>
        copy(String(active.sale_price ?? active.price));
    if ($("#row-craft"))
      $("#row-craft").onclick = () => showCraft(active.craft_list);
    if ($("#save-measure"))
      $("#save-measure").onclick = () => saveMeasurement(active);
  }
}
function stat(label, value) {
  return `<div class="stat"><small>${e(label)}</small><strong>${e(value)}</strong></div>`;
}
function detail(row) {
  const price = row.sale_price ?? row.price,
    url =
      row.sale_url ||
      `https://universalis.app/market/${row.item_id || (row.leve?.items && Object.keys(row.leve.items)[0]) || 0}`;
  return `<aside class="panel detail-panel"><div class="section-title">${e(row.name)}</div>${price != null ? `<div class="detail-price">${num(price)} gil / шт.</div><p>Цена продажи · ${e(row.sale_world || meta.profile.sale_world)}</p>` : ""}<div class="detail-buttons">${price != null ? '<button id="copy-price" class="secondary">Копировать цену</button>' : ""}${row.craft_list?.final || row.craft_list?.pre ? '<button id="row-craft" class="secondary">Список крафта</button>' : ""}</div><a href="${e(url)}" target="_blank" rel="noreferrer">Рынок продажи в Universalis ↗</a>${row.buy_url ? `<br><a href="${e(row.buy_url)}" target="_blank" rel="noreferrer">Рынок покупки ↗</a>` : ""}<pre>${e(row.details || "")}</pre>${row.mode === "gather" ? measurementForm(row) : ""}</aside>`;
}
function showCraft(value) {
  currentCraft = value;
  $("#craft-name").value = value.name || "FFXIV Workshop";
  $("#craft-pre").value = value.pre || "";
  $("#craft-final").value = value.final || "";
  $("#craft-note").textContent = value.note || "";
  $("#craft-modal").showModal();
}
async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("Скопировано");
  } catch {
    toast("Не удалось скопировать. Выделите текст и нажмите Ctrl+C.");
  }
}
function download(name, text, mime = "text/plain;charset=utf-8") {
  const url = URL.createObjectURL(new Blob([text], { type: mime }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function exportCSV(rows) {
  const cols = columns(),
    quote = (v) => '"' + String(v ?? "").replaceAll('"', '""') + '"';
  const lines = [
    cols.map((c) => quote(c[1])).join(";"),
    ...rows.map((r) => cols.map((c) => quote(r[c[0]])).join(";")),
  ];
  download(
    "workshop-" + page + ".csv",
    "\ufeff" + lines.join("\r\n"),
    "text/csv;charset=utf-8",
  );
}
async function loadHistory() {
  try {
    const result = await api(
      "history?q=" + encodeURIComponent($("#history-search").value),
    );
    results.history = { status: "done", progress: "История загружена", result };
    renderResults();
  } catch (err) {
    toast(err.message);
  }
}
const jobNames = {
  Carpenter: "Плотник",
  Blacksmith: "Кузнец",
  Armorer: "Бронник",
  Goldsmith: "Ювелир",
  Leatherworker: "Кожевник",
  Weaver: "Ткач",
  Alchemist: "Алхимик",
  Culinarian: "Кулинар",
  Miner: "Шахтёр",
  Botanist: "Ботаник",
  Fisher: "Рыбак",
};
function profilePage() {
  return `<section class="panel"><div class="section-title">Доступные профессии <small>Книги и HQ подтверждаются вручную</small></div><div class="table-wrap"><table class="profile-table"><thead><tr><th>Профессия</th><th>Использовать</th><th>Уровень</th><th>Гарантирую HQ</th><th>Master / Folklore</th><th>Специалист</th></tr></thead><tbody>${Object.entries(
    meta.profile.professions,
  )
    .map(
      ([job, p]) =>
        `<tr data-job="${job}"><td class="job-name">${jobNames[job]}<small>${job}</small></td><td><input type="checkbox" data-key="enabled" ${p.enabled ? "checked" : ""}></td><td><input type="number" data-key="level" min="1" max="100" value="${p.level}"></td><td><input type="checkbox" data-key="hq" ${p.hq ? "checked" : ""} ${["Miner", "Botanist", "Fisher"].includes(job) ? "disabled" : ""}></td><td><input type="checkbox" data-key="books" ${p.books ? "checked" : ""}></td><td><input type="checkbox" data-key="specialist" ${p.specialist ? "checked" : ""}></td></tr>`,
    )
    .join(
      "",
    )}</tbody></table></div><div class="form-actions"><button id="save-professions">Сохранить профессии</button><span class="hint">Заготовка недоступной профессии будет куплена.</span></div></section>${gatherProfile()}<section class="panel bridge-panel"><div><div class="section-title">Данные персонажа через Dalamud</div><p>WorkshopBridge сохраняет уровни и EXP в локальный файл. Команда в игре: <b>/workshop</b>. Для автоматического обновления: <b>/workshop auto</b>.</p><code>${e(meta.bridge_path)}</code><p id="character-note">${meta.profile.character?.updated_at ? "Последний снимок: " + new Date(meta.profile.character.updated_at * 1000).toLocaleString("ru-RU") : "Снимок ещё не импортирован. Плагин устанавливается отдельно; автоматизация игры не выполняется."}</p>${check("Подхватывать локальный файл персонажа", "bridge_enabled", meta.profile.bridge_enabled)}</div><div><input id="character-file" class="file-input" type="file" accept=".json"><div class="settings-buttons"><button id="import-character" class="secondary">Импортировать снимок</button></div></div></section>`;
}
function bindProfile() {
  $("#save-professions").onclick = async () => {
    try {
      document.querySelectorAll("tr[data-job]").forEach((tr) => {
        const p = meta.profile.professions[tr.dataset.job];
        tr.querySelectorAll("[data-key]").forEach(
          (input) =>
            (p[input.dataset.key] =
              input.type === "checkbox" ? input.checked : Number(input.value)),
        );
      });
      collectGather();
      meta.profile.bridge_enabled = $("input[name=bridge_enabled]").checked;
      meta.profile = await api("profile", meta.profile);
      toast("Общий профиль сохранён");
    } catch (err) {
      toast(err.message);
    }
  };
  if ($("#save-gather"))
    $("#save-gather").onclick = () => $("#save-professions").click();
  $("#import-character").onclick = async () => {
    try {
      const file = $("#character-file").files[0];
      if (!file) throw Error("Выберите JSON-файл персонажа");
      meta.profile = await api("character", JSON.parse(await file.text()));
      toast("Уровни и EXP импортированы");
      go("profile");
    } catch (err) {
      toast(err.message);
    }
  };
}
function settingsPage() {
  const p = meta.profile;
  return `<form id="settings-form" class="panel"><div class="section-title">Общие рынки и расходы <small>Сохраняются отдельно от прежних приложений</small></div><div class="form-grid">${select("Домашний мир / продажа", "sale_world", meta.worlds, p.sale_world)}${select("Закупка материалов", "purchase_scope", scopeOptions(), p.purchase_scope)}${field("Налог покупки, %", "buy_tax", p.market.buy_tax, "number", 'min="0" max="20" step="0.1"')}${field("Налог продажи, %", "sell_tax", p.market.sell_tax, "number", 'min="0" max="20" step="0.1"')}${field("Еда на партию, gil", "food", p.market.food)}${field("Зелья / cordial на партию", "medicine", p.market.medicine)}${field("Ремонт на партию, gil", "repair", p.market.repair)}${field("Дорога на партию, gil", "travel", p.market.travel)}${field("Дополнительные затраты / крафт", "craft_per", p.market.craft_per)}${field("Прочие расходы партии", "other", p.market.other)}</div><div class="form-actions"><button id="save-settings" type="button">Сохранить настройки</button></div></form><form id="telegram-form" class="panel"><div class="section-title">Telegram <small>${meta.token_saved ? "Токен сохранён с защитой Windows" : "Токен ещё не задан"}</small></div><p>Создайте бота через <a href="https://t.me/BotFather" target="_blank" rel="noreferrer">BotFather</a>, отправьте ему /start и укажите Chat ID. Токен вводится только здесь.</p><div class="form-grid two" style="margin-top:17px">${field("Токен бота · сохранённый не показывается", "token", "", "password", 'autocomplete="off"')}${field("Chat ID", "chat_id", p.telegram.chat_id, "text")}</div><div class="form-actions"><button id="save-telegram" type="button">Сохранить Telegram</button>${check("Уведомлять о выгодных покупках", "notify", p.telegram.notify)}</div><p class="note-line">После успешного уведомления стоимость и объём занимают общий лимит рекомендаций. Программа должна оставаться запущенной.</p></form>${consumablesPanel()}<section class="panel"><div class="section-title">Данные и обслуживание</div><p>Обновление каталога загрузит новые leves, рецепты и расписания из FFXIV данных. Рыночные цены обновляются при расчётах.</p><div class="settings-buttons"><button id="refresh-catalog" class="secondary">Обновить игровой каталог</button><button id="reset-budget" class="secondary">Сбросить лимит рекомендаций</button><button id="check-update" class="secondary">Проверить обновление</button></div><div id="settings-task"></div></section>`;
}
function bindSettings() {
  $("#save-settings").onclick = async () => {
    try {
      const form = $("#settings-form");
      if (!form.reportValidity()) return;
      const v = formValues(form);
      meta.profile.sale_world = v.sale_world;
      meta.profile.purchase_scope = v.purchase_scope;
      for (const [key, value] of Object.entries(v))
        if (!["sale_world", "purchase_scope"].includes(key))
          meta.profile.market[key] = value;
      meta.profile = await api("profile", meta.profile);
      updateHeader();
      toast("Настройки сохранены");
    } catch (err) {
      toast(err.message);
    }
  };
  $("#save-telegram").onclick = async () => {
    try {
      const v = formValues($("#telegram-form"));
      if (v.token) await api("token", { token: v.token, chat_id: v.chat_id });
      meta.profile.telegram = { chat_id: v.chat_id, notify: v.notify };
      meta.profile = await api("profile", meta.profile);
      $("#telegram-form input[name=token]").value = "";
      meta = await api("meta");
      toast("Настройки Telegram сохранены");
      go("settings");
    } catch (err) {
      toast(err.message);
    }
  };
  $("#reset-budget").onclick = async () => {
    try {
      await api("reset-budget", {});
      toast("Лимит стоимости и объёма сброшен");
    } catch (err) {
      toast(err.message);
    }
  };
  $("#refresh-catalog").onclick = async () => {
    try {
      const task = await api("start", { kind: "catalog", options: {} });
      async function poll() {
        const t = await api("tasks/" + task.id);
        if ($("#settings-task"))
          $("#settings-task").textContent = t.error || t.progress;
        if (t.status === "running") setTimeout(poll, 1500);
        else meta = await api("meta");
      }
      poll();
    } catch (err) {
      toast(err.message);
    }
  };
  $("#check-update").onclick = checkUpdate;
}
function updateHeader() {
  $("#world-tag").textContent =
    meta.profile.sale_world + " · " + meta.profile.purchase_scope;
  $("#mode-tag").textContent = meta.demo
    ? "DEMO · искусственные цены"
    : "Локальное приложение";
  $("#version").textContent = "Версия " + meta.version;
}
async function checkUpdate() {
  try {
    if (meta.demo) {
      $("#update-note").textContent = "DEMO · обновления отключены";
      return;
    }
    const answer = await api("update");
    $("#update-note").textContent = answer.message;
    if (answer.status === "available")
      $("#update-note").innerHTML =
        `<a href="${e(answer.url)}" target="_blank" rel="noreferrer">${e(answer.message)} ↗</a>`;
  } catch {
    $("#update-note").textContent = "Проверка обновлений недоступна";
  }
}
$("#profile-shortcut").onclick = () => go("profile");
$("#modal-close").onclick = () => $("#craft-modal").close();
$("#copy-pre").onclick = () => copy($("#craft-pre").value);
$("#copy-final").onclick = () => copy($("#craft-final").value);
$("#copy-name").onclick = () => copy($("#craft-name").value);
$("#save-craft").onclick = () =>
  download(
    "workshop-crafting.txt",
    "List Name\n" + $("#craft-name").value + "\n\nPre-craft Items\n" +
      $("#craft-pre").value +
      "\n\nFinal Items\n" +
      $("#craft-final").value +
      "\n\n" +
      (currentCraft.note || ""),
  );
$("#shutdown").onclick = async () => {
  await api("shutdown", {});
  $("#view").innerHTML =
    '<div class="empty"><strong>Приложение завершено</strong>Окно браузера можно закрыть.</div>';
};
(async () => {
  try {
    meta = await api("meta");
    $("#nav").innerHTML = Object.entries(pages)
      .map(
        ([key, value]) =>
          `<button data-page="${key}"><svg viewBox="0 0 24 24"><path d="${icons[key]}"></path></svg>${value[0]}</button>`,
      )
      .join("");
    document
      .querySelectorAll("nav button")
      .forEach((b) => (b.onclick = () => go(b.dataset.page)));
    updateHeader();
    go("leve");
    if (meta.warning) toast(meta.warning);
    checkUpdate();
  } catch (err) {
    $("#view").innerHTML =
      `<div class="progress error">${e(err.message)}. Запустите приложение через start.bat.</div>`;
  }
})();

function gatherProfile() {
  return `<section class="panel"><div class="section-title">Личный темп сбора <small>Нулевой темп = прибыль в час неизвестна</small></div>${[
    "Miner",
    "Botanist",
    "Fisher",
  ]
    .map((job) => {
      const p = profileJob(job);
      return `<div data-gather-job="${job}" style="margin-bottom:20px"><h2 style="font-size:14px">${jobNames[job]}</h2><div class="form-grid">${field("Собираю шт./час", "rate", p.rate || 0)}${field("Урожай за окно", "window_yield", p.window_yield || 0)}${field("Доп. расход / единицу", "per_unit", p.per_unit || 0)}${job === "Fisher" ? field("Приманки / целевую рыбу · пусто = неизвестно", "bait_per_unit", p.bait_per_unit ?? "") : ""}</div></div>`;
    })
    .join(
      "",
    )}<p>Стоимость приманок включает неудачные попытки. Ожидание временных узлов и дорога не превращаются в непрерывный сбор.</p><button id="save-gather" class="secondary">Сохранить темп сбора</button></section>`;
}
function collectGather() {
  document.querySelectorAll("[data-gather-job]").forEach((div) => {
    const p = meta.profile.professions[div.dataset.gatherJob];
    div
      .querySelectorAll("input")
      .forEach(
        (input) =>
          (p[input.name] = input.value === "" ? null : Number(input.value)),
      );
  });
}
function consumablesPanel() {
  const entries = meta.profile.market.consumables || [];
  return `<section class="panel"><div class="section-title">Расходники по рыночной цене <small>Включаются в закупку партии</small></div><p>ID виден в ссылке Universalis: /market/ID. Еду и зелья можно указать здесь вместо фиксированной суммы выше.</p><div class="form-grid" style="margin-top:15px">${field("ID предмета", "consumable-id", "", "number", 'min="1"')}${field("Количество на партию", "consumable-quantity", 1, "number", 'min="1"')}${select("Когда используется", "consumable-mode", ["all", "craft", "gather"], "all").replace(">all<", ">Всегда<").replace(">craft<", ">Крафт<").replace(">gather<", ">Сбор<")}${check("HQ расходник", "consumable-hq", false)}</div><div class="form-actions"><button id="add-consumable" class="secondary">Добавить расходник</button></div><div id="consumable-list">${entries.map((entry, i) => `<p>Предмет #${entry.item} · ${entry.quantity} ${entry.hq ? "HQ" : "NQ"} · ${entry.mode === "all" ? "Всегда" : entry.mode === "craft" ? "Крафт" : "Сбор"} <button data-remove-consumable="${i}" class="quiet">Убрать</button></p>`).join("")}</div></section>`;
}
function bindConsumables() {
  $("#add-consumable").onclick = async () => {
    try {
      const item = Number($("input[name=consumable-id]").value),
        quantity = Number($("input[name=consumable-quantity]").value);
      if (
        !Number.isInteger(item) ||
        item <= 0 ||
        !Number.isInteger(quantity) ||
        quantity <= 0
      )
        throw Error("Укажите ID и количество расходника");
      meta.profile.market.consumables ??= [];
      meta.profile.market.consumables.push({
        item,
        quantity,
        mode: $("select[name=consumable-mode]").value,
        hq: $("input[name=consumable-hq]").checked,
      });
      meta.profile = await api("profile", meta.profile);
      toast("Расходник сохранён");
      go("settings");
    } catch (err) {
      toast(err.message);
    }
  };
  document.querySelectorAll("[data-remove-consumable]").forEach(
    (button) =>
      (button.onclick = async () => {
        meta.profile.market.consumables.splice(
          Number(button.dataset.removeConsumable),
          1,
        );
        try {
          meta.profile = await api("profile", meta.profile);
          go("settings");
        } catch (err) {
          toast(err.message);
        }
      }),
  );
}
setInterval(async () => {
  if (!meta?.profile.bridge_enabled || meta.demo) return;
  try {
    const updated = await api("meta");
    if (!["profile", "settings"].includes(page)) {
      meta = updated;
      updateHeader();
    }
  } catch {}
}, 10000);

setInterval(async () => {
  if (page === "market" && tasks.market && results.market?.status === "done") {
    try {
      results.market = await api("tasks/" + tasks.market);
      renderResults();
    } catch {}
  }
  if (page === "reseller" && results.reseller?.status !== "running") {
    let changed = false;
    for (const row of results.reseller?.result?.rows || []) {
      if (row.availability === "Недавно проверено" && Date.now()/1000-row.received >= 90) {
        row.availability = "Нужно перепроверить";
        row.details = row.details?.replace("Статус объявления: Недавно проверено", "Статус объявления: Нужно перепроверить");
        changed = true;
      }
    }
    if (changed) renderResults();
  }
}, 30000);

function measurementForm(row) {
  return `<details><summary>Мой замер этого ресурса</summary><div style="display:grid;gap:10px;margin-top:15px">${field("Минуты активного сбора", "measure-minutes", "", "number", 'min="0.1" step="0.1"')}${field("Получено целевого ресурса, шт.", "measure-units", "", "number", 'min="1"')}${field("Все приманки, gil · пусто = неизвестно", "measure-bait", "", "number", 'min="0"')}${field("Урожай за окно, шт.", "measure-window", 0, "number", 'min="0"')}</div><button id="save-measure" class="secondary" style="margin-top:12px">Сохранить замер</button><p class="note-line">Замер сохраняется по ID предмета. Применяйте его к тому же месту и способу сбора.</p></details>`;
}
async function saveMeasurement(row) {
  try {
    const minutes = Number($("input[name=measure-minutes]").value),
      units = Number($("input[name=measure-units]").value),
      bait = $("input[name=measure-bait]").value,
      windowYield = Number($("input[name=measure-window]").value);
    if (
      !(minutes > 0) ||
      !Number.isInteger(units) ||
      units < 1 ||
      !Number.isInteger(windowYield) ||
      windowYield < 0
    )
      throw Error("Укажите время, количество и урожай за окно");
    meta.profile.market.item_profiles ??= {};
    meta.profile.market.item_profiles[String(row.item_id)] = {
      rate: (units / minutes) * 60,
      window_yield: windowYield,
      bait_per_unit: bait === "" ? null : Number(bait) / units,
    };
    meta.profile = await api("profile", meta.profile);
    toast("Замер сохранён. Повторите анализ для нового расчёта.");
  } catch (err) {
    toast(err.message);
  }
}
