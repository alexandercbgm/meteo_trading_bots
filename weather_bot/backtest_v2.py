# ===== backtest_v2: простое правило вместо подбора гиперпараметров по городам =====
# Запуск: python backtest_v2.py [окно_дней] [мин_цена] [исключать_в_минусе: 0|1]   (скрипт лежит в корне проекта рядом с config/ и src/).
#
# Что делает. Для каждого города выбирается ТОЛЬКО стратегия: "Прогноз" (weather_forecast) или "Макс" (max_bet) --
# по профиту за последние WINDOW_DAYS дней. Всё остальное одинаково для всех городов:
#   * тип ставки: пирамида 2 (loss_limit: 2);
#   * минимальная цена: MIN_PRICE для всех;
#   * поправка прогноза: по самой частой ошибке прогноза за ВСЮ историю города (меняется с 0, только если соседняя
#     корзина обгоняет нулевую минимум на G_MARGIN долей дней); у стратегии Макс поправки нет.
# Никаких фильтров и подбора мин. цены/типа по городу нет: walk-forward проверка (walk_forward_check.py) показала, что
# подбор по городу переоценивает результат, а выбор между двумя стратегиями -- нет.
# Результат: city_rating_v2.html (таблица, бектест портфеля, yaml) и готовые файлы в папке yaml_v2/.
import os, sys, math, json
import numpy as np, pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
from src.config import Config
from src.cities import CITIES
from src.accuracy_report import AccuracyReportBuilder
from src.pyramid import pyramid_factor

# ---------------- настройки ----------------
SLOT          = None
SOURCE        = "wunderground"
WINDOW_DAYS   = 45               # окно выбора стратегии и бектеста, дни
MIN_PRICE     = 40               # одна мин. цена для всех городов, ¢ (в walk_forward_check проверены 30 и 40; 30 даёт тот же вывод)
PYR_STEPS     = 2                # пирамида 2 (loss_limit в yaml)
MAX_PRICE     = 66
LOT, COMMISSION, START_BALANCE = 5.0, 0.012, 100.0
CORRECTIONS   = [-1, 0, 1]
G_MARGIN      = 0.10             # поправка меняется с 0, только если соседняя корзина обгоняет нулевую на столько долей дней
G_MIN_DAYS    = 20               # минимум дней истории ошибок для смены поправки
MIN_TRADES    = 8                # минимум сделок за окно, чтобы стратегия города считалась
EXCLUDE_NEGATIVE = False          # True: города, у которых лучшая из двух стратегий дала убыток за окно, не попадают в yaml и портфель
OUT_HTML      = "city_rating_v2.html"
OUT_DIR       = "yaml_v2"
if len(sys.argv) >= 2:
    WINDOW_DAYS = int(sys.argv[1])
if len(sys.argv) >= 3:
    MIN_PRICE = int(sys.argv[2])
if len(sys.argv) >= 4:
    EXCLUDE_NEGATIVE = sys.argv[3].strip().lower() in ('1', 'true', 'да', 'yes')

# ---------------- данные ----------------
cfg = Config("config/config.yaml")
b = AccuracyReportBuilder(cfg, CITIES)
slot = str(SLOT or cfg.price_monitor.monitor_slot)
df_fc, _ = b.build_dataframe()
df_fc = df_fc[(df_fc["source"] == SOURCE) & (df_fc["snapshot_slot"].astype(str) == slot)]
df_mx = b.build_market_target_dataframe(slot)
df_mx = df_mx[df_mx["snapshot_slot"].astype(str) == slot]
scale_by_icao = pd.concat([df_fc, df_mx]).drop_duplicates("icao").set_index("icao")["scale"].to_dict()
PRICES = b._build_bucket_price_map(scale_by_icao)
MIN_BET, DECIDED, MIN_ORDER = b.min_bet_price_cents, b.decided_price_cents, b.min_order_usd
PYR = {"forecast": b._pyramid_params("weather_forecast"), "max": b._pyramid_params("max_bet")}


def lookup_price(icao, date, bucket):
    for lo, hi, p in (PRICES.get(icao, {}).get(date, {}).get(slot) or []):
        if (lo is None or bucket >= lo) and (hi is None or bucket <= hi):
            return p
    return None


def decided_elsewhere(icao, date, bucket):
    entries = PRICES.get(icao, {}).get(date, {}).get(slot) or []
    if not entries or not DECIDED:
        return False
    top = max(entries, key=lambda e: e[2])
    if top[2] < DECIDED:
        return False
    return not ((top[0] is None or bucket >= top[0]) and (top[1] is None or bucket <= top[1]))


def floor_lot(lot, price):
    if not MIN_ORDER or not price > 0:
        return lot
    return max(lot, math.ceil(MIN_ORDER / price * 100 - 1e-9) / 100)


def simulate(rows, strategy, corr):
    """rows: данные города за окно. Пирамида 2, мин. цена MIN_PRICE. Возвращает список (date, hit, price_cents, cost, pnl)."""
    pyr = PYR[strategy]
    recs, miss = [], 0
    for date, icao, err, bucket in rows:
        tb = bucket + corr
        p = lookup_price(icao, date, tb)
        if p is None or p < MIN_PRICE or p > MAX_PRICE or p < MIN_BET or \
                (strategy == "forecast" and decided_elsewhere(icao, date, tb)):
            continue
        hit = (err + corr) == 0
        price = p / 100
        lot = floor_lot(LOT * pyramid_factor(miss, pyr["progression"], pyr["multiplier"], pyr["steps"]), price)
        cost = (price + COMMISSION) * lot
        recs.append((date, hit, p, cost, (lot if hit else 0) - cost))
        miss = 0 if (hit or miss + 1 >= PYR_STEPS) else miss + 1
    return recs


def city_profit30(recs, span_days):
    return sum(r[4] for r in recs) / span_days * 30


def curve(items, start, end):
    """Портфель: суточные P&L и затраты по списку (город, сделки). Точки графика -- как в стандартном бэктесте: баланс по дням со сделками."""
    cal = pd.date_range(start, end)
    pnl = pd.Series(0.0, index=cal)
    cost = pd.Series(0.0, index=cal)
    n = 0
    day_entries = {}
    for city, recs in items:
        for date, hit, p, c, x in recs:
            t = pd.Timestamp(date)
            if t in pnl.index:
                pnl[t] += x
                cost[t] += c
                n += 1
                day_entries.setdefault(t, {})
                day_entries[t][city] = day_entries[t].get(city, 0.0) + x
    bal = START_BALANCE + pnl.cumsum()
    peak = bal.cummax().clip(lower=START_BALANCE)
    dd = float((peak - bal).max())
    span = len(cal)
    points = [dict(x="Старт", y=START_BALANCE, entries=[])]
    for t in cal:
        if t in day_entries:
            points.append(dict(x=t.strftime("%Y-%m-%d"), y=round(float(bal[t]), 2),
                               entries=[[c, round(v, 2)] for c, v in sorted(day_entries[t].items(), key=lambda e: -abs(e[1]))]))
    return dict(points=points,
                profit=round(float(pnl.sum()), 2), profit30=round(float(pnl.sum()) / span * 30, 2), dd=round(dd, 2),
                load=round(float(cost.max()), 2), trades=n, days=span)


# ---------------- выбор ----------------
all_rows = {}
for strategy, src_df in (("forecast", df_fc), ("max", df_mx)):
    for (icao, city), g in src_df.groupby(["icao", "city"]):
        g = g.sort_values("date").drop_duplicates("date", keep="last")
        all_rows[(strategy, icao)] = (city, list(zip(g["date"], g["icao"], g["error"], g["forecast_bucket"].astype(int))))
dates_all = pd.to_datetime([r[0] for _, rows in all_rows.values() for r in rows])
FIRST, LAST = dates_all.min(), dates_all.max()
W_START = max(FIRST, LAST - pd.Timedelta(days=WINDOW_DAYS - 1))
SPAN = (LAST - W_START).days + 1
print(f"История: {FIRST.date()} .. {LAST.date()}, окно {W_START.date()} .. {LAST.date()} ({SPAN} дн.), мин. цена {MIN_PRICE}¢, пирамида {PYR_STEPS}")
if SPAN < 20:
    print("ВНИМАНИЕ: окно короче 20 дней, выбор будет шумным.")

cities = sorted({(icao, c) for (s, icao), (c, _) in all_rows.items()}, key=lambda x: x[1])
table, skipped, excluded = [], [], []
chosen_recs, only_fc, only_mx = [], [], []
for icao, city in cities:
    cand = {}
    for strategy in ("forecast", "max"):
        key = (strategy, icao)
        if key not in all_rows:
            continue
        rows_all = all_rows[key][1]
        corr = 0
        if strategy == "forecast":
            errs = [r[2] for r in rows_all]
            if len(errs) >= G_MIN_DAYS:
                cnt = {c: sum(1 for e in errs if e + c == 0) for c in CORRECTIONS}
                cb = max(CORRECTIONS, key=lambda c: cnt[c])
                if cb != 0 and (cnt[cb] - cnt[0]) / len(errs) >= G_MARGIN:
                    corr = cb
        rr = [r for r in rows_all if pd.Timestamp(r[0]) >= W_START]
        recs = simulate(rr, strategy, corr) if rr else []
        cand[strategy] = dict(corr=corr, recs=recs, ok=len(recs) >= MIN_TRADES, p30=city_profit30(recs, SPAN) if recs else None)
    ok = {s: v for s, v in cand.items() if v["ok"]}
    if not ok:
        skipped.append(dict(icao=icao, city=city, trades={s: len(v["recs"]) for s, v in cand.items()}))
        continue
    best = max(ok, key=lambda s: ok[s]["p30"])
    v = ok[best]
    if EXCLUDE_NEGATIVE and v["p30"] <= 0:
        excluded.append(dict(icao=icao, city=city, p30_fc=None if cand.get("forecast", {}).get("p30") is None else round(cand["forecast"]["p30"], 2),
                             p30_mx=None if cand.get("max", {}).get("p30") is None else round(cand["max"]["p30"], 2)))
        continue
    hits = sum(1 for r in v["recs"] if r[1])
    table.append(dict(icao=icao, city=city, strategy=best, corr=v["corr"], p30=round(v["p30"], 2),
                      p30_fc=None if cand.get("forecast") is None or cand["forecast"]["p30"] is None else round(cand["forecast"]["p30"], 2),
                      p30_mx=None if cand.get("max") is None or cand["max"]["p30"] is None else round(cand["max"]["p30"], 2),
                      fc_ok=bool(cand.get("forecast", {}).get("ok")), mx_ok=bool(cand.get("max", {}).get("ok")),
                      trades=len(v["recs"]), hit=round(hits / len(v["recs"]) * 100, 1)))
    chosen_recs.append((city, v["recs"]))
    if cand.get("forecast", {}).get("ok"):
        only_fc.append((city, cand["forecast"]["recs"]))
    if cand.get("max", {}).get("ok"):
        only_mx.append((city, cand["max"]["recs"]))
table.sort(key=lambda r: -r["p30"])
PF = {"Выбор по городу": curve(chosen_recs, W_START, LAST), "Прогноз для всех": curve(only_fc, W_START, LAST), "Макс для всех": curve(only_mx, W_START, LAST)}
n_fc = sum(1 for r in table if r["strategy"] == "forecast")
n_mx = len(table) - n_fc
n_neg = sum(1 for r in table if r["p30"] < 0)
print(f"Городов: {len(table)} (Прогноз {n_fc}, Макс {n_mx}), отрицательный профит у {n_neg}, пропущено (мало сделок): {len(skipped)}"
      + (f", исключено (убыток на обеих стратегиях): {len(excluded)}" if EXCLUDE_NEGATIVE else ""))
for k, v in PF.items():
    print(f"  {k}: профит за окно ${v['profit']}, профит/30д ${v['profit30']}, просадка ${v['dd']}, нагрузка ${v['load']}, сделок {v['trades']}")

# ---------------- yaml ----------------
now = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")
RULE = (f"правило backtest_v2: стратегия по городу -- по профиту за {SPAN} дн.; пирамида {PYR_STEPS}, мин. цена {MIN_PRICE}¢ для всех; "
        f"поправка прогноза по моде ошибок за всю историю" + ("; города с убытком на обеих стратегиях исключены" if EXCLUDE_NEGATIVE else ""))
wsrc = "Windy" if SOURCE.lower() == "windy" else ""


def money(v):
    return ("-" if v < 0 else "") + "$%.2f" % abs(v)


def yaml_text(strategy):
    name = "weather_forecast" if strategy == "forecast" else "max_bet"
    rows = [r for r in table if r["strategy"] == strategy]
    head = [f"# watched_icaos_{name}.yaml -- сгенерировано backtest_v2, {now}", f"# {RULE}"]
    if strategy == "forecast":
        defaults = ["defaults:", f'  weather_source: "{wsrc}"', "  weather_corr:", "  min_price_limit:", f"  max_price_limit: {MAX_PRICE}", '  comment: ""']
    else:
        defaults = ["defaults:", "  min_price_limit:", f"  max_price_limit: {MAX_PRICE}", '  comment: ""']
    out = []
    for r in rows:
        lines = [f"  {r['icao']}:", f'    city: "{r["city"]}"', '    strategy: "pyramid"', f"    loss_limit: {PYR_STEPS}"]
        if strategy == "forecast":
            lines += [f'    weather_source: "{wsrc}"', f"    weather_corr:{' ' + str(r['corr']) if r['corr'] else ''}"]
        lines += [f"    min_price_limit: {MIN_PRICE}", '    comment: ""']
        alt = r["p30_mx"] if strategy == "forecast" else r["p30_fc"]
        out.append("\n".join(lines) + f"  # профит за 30 дн. {money(r['p30'])} (другая стратегия: {'нет' if alt is None else money(alt)}), сделок {r['trades']}")
    return "\n".join(head + [""] + defaults + ["", "watched_icaos:"] + out) + "\n", len(rows)


YAMLS = {}
os.makedirs(OUT_DIR, exist_ok=True)
for strategy, name in (("forecast", "weather_forecast"), ("max", "max_bet")):
    txt, n = yaml_text(strategy)
    if n:
        YAMLS[f"watched_icaos_{name}.yaml"] = txt
        with open(os.path.join(OUT_DIR, f"watched_icaos_{name}.yaml"), "w", encoding="utf-8") as fh:
            fh.write(txt)

# ---------------- отчёт ----------------
PF_CITIES = {'Выбор по городу': len(chosen_recs), 'Прогноз для всех': len(only_fc), 'Макс для всех': len(only_mx)}
DATA = dict(pf_cities=PF_CITIES, lot=LOT, start_balance=START_BALANCE, excluded=excluded, exclude_on=EXCLUDE_NEGATIVE, table=table, skipped=skipped, pf=PF, yamls=YAMLS, window=SPAN, start=str(W_START.date()), end=str(LAST.date()),
            min_price=MIN_PRICE, steps=PYR_STEPS, n_fc=n_fc, n_mx=n_mx, n_neg=n_neg, min_trades=MIN_TRADES, margin=G_MARGIN)
PAGE = r"""
const D = __DATA__;
const fmt = v => (v === null || v === undefined) ? "–" : ((v >= 0 ? "+" : "-") + "$" + Math.abs(v).toFixed(2));
const cls = v => v === null || v === undefined ? "" : (v >= 0 ? "g" : "r");
const pfNames = Object.keys(D.pf), COL = { "Выбор по городу": "#2980b9", "Прогноз для всех": "#e67e22", "Макс для всех": "#27ae60" };
document.getElementById("sum").innerHTML = "<table class='t'><tr><th>Портфель</th><th>Профит за окно</th><th>Профит / 30 дн.</th><th>Макс. просадка</th><th>Нагрузка на депозит</th><th>Сделок</th></tr>" +
  pfNames.map(n => { const p = D.pf[n]; return `<tr><td style="color:${COL[n]}"><b>${n}</b></td><td class="${cls(p.profit)}">${fmt(p.profit)}</td><td class="${cls(p.profit30)}">${fmt(p.profit30)}</td><td>$${p.dd}</td><td>$${p.load}</td><td>${p.trades}</td></tr>`; }).join("") + "</table>";
const UP = "#27ae60";
const fmtPnl = v => { const r = Math.round(v * 100) / 100; return (r >= 0 ? "+" : "-") + "$" + Math.abs(r).toFixed(2); };
// сглаживание кривой: тот же алгоритм, что в основных отчётах
function smoothSeries(pts, perSeg) {
  const sorted = pts.slice().sort((a, b) => a.x - b.x);
  const uniq = [];
  sorted.forEach(p => { if (uniq.length && uniq[uniq.length - 1].x === p.x) uniq[uniq.length - 1] = p; else uniq.push(p); });
  const n = uniq.length;
  if (n < 2) return uniq;
  const m = new Array(n);
  for (let i = 0; i < n; i++) {
    const a = uniq[Math.max(0, i - 1)], b = uniq[Math.min(n - 1, i + 1)];
    m[i] = (b.y - a.y) / (b.x - a.x);
  }
  const dense = [];
  for (let i = 0; i < n - 1; i++) {
    const p0 = uniq[i], p1 = uniq[i + 1], h = p1.x - p0.x;
    for (let k = 0; k < perSeg; k++) {
      const t = k / perSeg, t2 = t * t, t3 = t2 * t;
      const y = (2 * t3 - 3 * t2 + 1) * p0.y + (t3 - 2 * t2 + t) * h * m[i] + (-2 * t3 + 3 * t2) * p1.y + (t3 - t2) * h * m[i + 1];
      dense.push({ x: p0.x + t * h, y: y });
    }
  }
  dense.push({ x: uniq[n - 1].x, y: uniq[n - 1].y });
  return dense;
}
function renderMulti(divId, names, titleText) {
  // общая категориальная ось дат; у каждого портфеля точки в дни его сделок, кривая сглаженная (как в стандартном бэктесте)
  const dateSet = new Set();
  names.forEach(n => D.pf[n].points.forEach(p => { if (p.x !== "Старт") dateSet.add(p.x); }));
  const cats = ["Старт"].concat(Array.from(dateSet).sort());
  const idx = {}; cats.forEach((c, i) => { idx[c] = i; });
  const traces = [];
  names.forEach((n, k) => {
    const col = COL[n];
    const points = D.pf[n].points;
    const pts = points.map(p => ({ x: idx[p.x], y: p.y }));
    const dense = smoothSeries(pts, 16);
    if (dense.length > 1) traces.push({ x: dense.map(d => d.x), y: dense.map(d => d.y), mode: "lines", type: "scatter", line: { color: col, width: 3 }, hoverinfo: "skip", showlegend: false, legendgroup: n });
    const hover = points.map(p => {
      if (p.x === "Старт") return `${n}<br>Старт<br>Баланс: $${p.y.toFixed(2)}`;
      const ls = [`<b>${n}</b>`, p.x, `Баланс: $${p.y.toFixed(2)}`];
      if (p.entries.length > 1) ls.push(`${p.entries.length} гор.: ${fmtPnl(p.entries.reduce((a, e) => a + e[1], 0))}`);
      if (p.entries.length <= 50) {
        const items = p.entries.map(e => `${e[0]}: ${fmtPnl(e[1])}`), per = Math.max(1, Math.ceil(items.length / 10));
        for (let i = 0; i < items.length; i += per) ls.push(items.slice(i, i + per).join("  |  "));
      }
      return ls.join("<br>");
    });
    traces.push({ x: pts.map(p => p.x), y: pts.map(p => p.y), mode: "markers", type: "scatter", marker: { size: 7, color: col }, text: hover, hoverinfo: "text", showlegend: false, legendgroup: n });
    traces.push({ x: [null], y: [null], mode: "lines+markers", type: "scatter", line: { color: col, width: 3 }, marker: { size: 7, color: col }, name: `${n} (${D.pf_cities[n]} гор.)`, showlegend: true, hoverinfo: "skip", legendgroup: n });
  });
  const step = Math.max(1, Math.ceil(cats.length / 24));
  const tv = [], tt = [];
  cats.forEach((c, i) => { if (i % step === 0 || i === cats.length - 1) { tv.push(i); tt.push(c); } });
  const shapes = [{ type: "line", x0: 0, x1: 1, xref: "paper", y0: D.start_balance, y1: D.start_balance, line: { color: "#999", width: 1, dash: "dot" } }];
  Plotly.newPlot(divId, traces, {
    title: { text: `<b>${titleText}</b>`, x: 0.02, xanchor: "left" },
    template: "plotly_white", paper_bgcolor: "#fff", plot_bgcolor: "#fff", margin: { t: 50, r: 230, b: 90, l: 60 },
    xaxis: { title: "Дата", type: "linear", tickangle: -45, fixedrange: true, tickmode: "array", tickvals: tv, ticktext: tt, range: [-0.5, Math.max(cats.length - 1, 1) + 0.5], zeroline: false },
    yaxis: { title: "Баланс в $", fixedrange: true }, dragmode: false, shapes: shapes, height: 420, legend: { orientation: "v", x: 1.02, xanchor: "left", y: 1, yanchor: "top" },
  }, { responsive: true, displayModeBar: false, scrollZoom: false });
}
document.getElementById("chart").innerHTML = '<div id="pfchart" style="height:420px"></div>';
renderMulti("pfchart", pfNames, `Бэктест портфелей за окно, Лот $${D.lot}, Баланс $${D.start_balance}`);
document.getElementById("cities").innerHTML = "<table class='t'><tr><th>Город</th><th>ICAO</th><th>Стратегия</th><th>Поправка</th><th>Профит / 30 дн.</th><th>Прогноз</th><th>Макс</th><th>Сделок</th><th>Попаданий, %</th></tr>" +
  D.table.map(r => `<tr><td>${r.city}</td><td>${r.icao}</td><td><b>${r.strategy === "forecast" ? "Прогноз" : "Макс"}</b></td><td>${r.strategy === "forecast" ? (r.corr > 0 ? "+" + r.corr : r.corr) : "–"}</td>` +
    `<td class="${cls(r.p30)}">${fmt(r.p30)}</td><td class="${cls(r.p30_fc)}">${fmt(r.p30_fc)}${r.p30_fc !== null && !r.fc_ok ? "*" : ""}</td><td class="${cls(r.p30_mx)}">${fmt(r.p30_mx)}${r.p30_mx !== null && !r.mx_ok ? "*" : ""}</td><td>${r.trades}</td><td>${r.hit}</td></tr>`).join("") + "</table>" +
  `<p class="hint">* стратегия не рассматривалась: сделок меньше ${D.min_trades}.</p>`;
document.getElementById("skipped").innerHTML = (D.exclude_on ? "<p class='hint'>Исключены (лучшая стратегия в минусе за окно, " + D.excluded.length + "): " +
  D.excluded.map(e => `${e.city} (Прогноз ${fmt(e.p30_fc)}, Макс ${fmt(e.p30_mx)})`).join("; ") + ".</p>" : "") +  D.skipped.length ? "<p class='hint'>Города без стратегии (сделок меньше " + D.min_trades + " за окно): " +
  D.skipped.map(s => `${s.city} (${Object.entries(s.trades).map(([k, v]) => (k === "forecast" ? "Прогноз " : "Макс ") + v).join(", ")})`).join("; ") + ".</p>" : "";
const esc = s => s.replace(/&/g, "&amp;").replace(/</g, "&lt;");
document.getElementById("yaml").innerHTML = Object.keys(D.yamls).map((name, i) => `<div class="yf"><div><b>${name}</b> ` +
  `<button data-act="copy" data-i="${i}">Копировать</button> <button data-act="save" data-i="${i}">Скачать</button></div>` +
  `<textarea id="ta${i}" readonly rows="${Math.min(30, D.yamls[name].split("\n").length + 1)}">${esc(D.yamls[name])}</textarea></div>`).join("");
const names = Object.keys(D.yamls);
document.getElementById("yaml").addEventListener("click", ev => {
  const t = ev.target; if (!t.dataset.act) return;
  const i = Number(t.dataset.i), txt = D.yamls[names[i]];
  if (t.dataset.act === "copy") { navigator.clipboard && navigator.clipboard.writeText(txt); t.textContent = "Скопировано"; }
  else { const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([txt], { type: "text/yaml" })); a.download = names[i]; a.click(); }
});
"""
CSS = """body{font-family:sans-serif;margin:16px;max-width:1300px}h2{margin:28px 0 6px}.hint{color:#666;font-size:14px}.t{border-collapse:collapse;font-size:13px;margin:6px 0}
.t td,.t th{border-bottom:1px solid #eee;padding:3px 14px 3px 0;text-align:left}.g{color:#1e7e34}.r{color:#c0392b}
textarea{width:100%;font-family:monospace;font-size:12px}.yf{margin:10px 0}button{margin-left:6px}"""
note = (f"<p class='hint'>Слот {slot}. Окно {W_START.date()} … {LAST.date()} ({SPAN} дн.). Лот ${LOT}, комиссия ${COMMISSION}, потолок цены {MAX_PRICE}¢. "
        f"Правило: стратегия по городу выбирается по профиту за окно; одинаково для всех городов -- пирамида {PYR_STEPS}, мин. цена {MIN_PRICE}¢; "
        f"поправка прогноза -- по самой частой ошибке прогноза за всю историю города (меняется с 0, только если соседняя корзина обгоняет нулевую минимум на {G_MARGIN:.0%} дней). "
        f"Фильтров и подбора мин. цены/типа по городу нет.</p>"
        f"<p class='hint'><b>Как читать.</b> Это бектест на тех же данных, по которым выбрана стратегия, поэтому он завышен. "
        f"В walk-forward проверке (walk_forward_check.py, 15 дат, около 3 независимых) факт за следующие дни составил примерно 30–55% от бектеста такого правила; "
        f"это ориентир, а не гарантия. Нагрузка на депозит -- максимальные суточные затраты всех позиций без общих лимитов бота; реальные лимиты (30/10) урежут часть сделок. "
        + (f"Исключены города, у которых обе стратегии в минусе за окно: {len(excluded)}. В walk-forward это не улучшило результат на невидимых днях, включено как опция."
           if EXCLUDE_NEGATIVE else f"Города с отрицательным профитом за окно: {n_neg} из {len(table)}; правило их не отсекает (запуск с третьим аргументом 1 их исключит).") + "</p>")
with open(OUT_HTML, "w", encoding="utf-8") as fh:
    fh.write(f"<html><head><meta charset='utf-8'><title>Рейтинг городов v2</title><script src='https://cdn.plot.ly/plotly-2.35.2.min.js'></script><style>{CSS}</style></head><body>")
    fh.write(f"<h1>Правило выбора стратегии по городам (Прогноз {n_fc} / Макс {n_mx})</h1>" + note)
    fh.write("<h2>Портфель</h2><div id='sum'></div><div id='chart'></div><h2>Города</h2><div id='cities'></div><div id='skipped'></div><h2>yaml для бота</h2><div id='yaml'></div>")
    fh.write("<script>" + PAGE.replace("__DATA__", json.dumps(DATA, ensure_ascii=False)) + "</script></body></html>")
print("Готово:", os.path.join(ROOT, OUT_HTML), "| yaml:", os.path.join(ROOT, OUT_DIR))
