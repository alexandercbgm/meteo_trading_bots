# ===== Рейтинг городов по бэктесту: перебор параметров на каждый город =====
# Запуск: python backtest.py (скрипт лежит в adhoc/, работает с папкой бота ../weather_bot; результат пишется туда же).
import os, sys, math
import numpy as np, pandas as pd
import plotly.graph_objects as go

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "weather_bot")   # папка бота: config/, src/, data_mining/
os.chdir(ROOT)
sys.path.insert(0, ROOT)
from src.config import Config
from src.cities import CITIES
from src.accuracy_report import AccuracyReportBuilder
from src.pyramid import pyramid_factor

# ---------------- настройки ----------------
SLOT          = None          # None -> price_monitor.monitor_slot из конфига
SOURCE        = "wunderground"  # источник прогноза для стратегии "прогноз"
CORRECTIONS   = [-1, 0, 1]      # для "max" поправка не применяется (только 0)
MIN_PRICES    = [10, 20, 30, 35, 40]
TYPES         = [("flat", 0), ("pyramid", 2), ("pyramid", 3)]   # (тип, число шагов)
MAX_PRICE     = 66              # потолок цены, как max_price_limit в watched_*.yaml
LOT, COMMISSION, START_BALANCE = 5.0, 0.012, 100.0   # как в бэктесте отчётов
WINDOW_DAYS   = 14
FLAT_STREAK_LEN = 3             # для плоской: серии промахов >= этой длины считаются "длинными"
MIN_TRADES    = 15              # конфиги с меньшим числом сделок в рейтинги не попадают
# Фильтры вариантов (для общих бэктестов в конце отчёта)
FILTER_GIVEBACK = 0.5    # 1: "сливает" -- в конце баланс ниже пика больше чем на эту долю прироста до пика
FILTER_DD_PCT   = 15     # 2: просадка больше этого % от стартового депозита
FILTER_RISK_SHARE = 0.5  # 4: макс. риск больше этой доли ср. профита за 30 дней
FILTER_GAP_DAYS = 7      # 5: столько дней подряд без единой сделки
FILTER_MAX_STREAK = 5    # 6: серия промахов подряд больше этого
FILTER_MAX_PYR_STEPS = 2 # 8: пирамида больше этого числа шагов не проходит (плоская проходит)
OUT_HTML      = "city_rating.html"

# Веса итогового балла (сумма = 1). Каждая компонента -- перцентиль среди ВСЕХ конфигов ВСЕХ городов (0..1).
WEIGHTS = {
    "profit_on_risk":  0.30,   # ср. профит за 30 дней / макс. риск -- отдача на рискуемый капитал
    "profit_on_dd":    0.20,   # ср. профит за 30 дней / макс. просадка (как Calmar)
    "long_streaks":    0.15,   # меньше длинных серий промахов на 100 сделок (за всё время)
    "recent_streaks":  0.10,   # меньше длинных серий и короче макс. серия за последние 14 дней
    "min_trades_14d":  0.15,   # минимум сделок за 14 дней (стабильность потока сигналов)
    "mean_trades_14d": 0.10,   # среднее число сделок за 14 дней
}

# ---------------- данные ----------------
cfg = Config("config/config.yaml")
b = AccuracyReportBuilder(cfg, CITIES)
slot = str(SLOT or cfg.price_monitor.monitor_slot)

df_fc, _ = b.build_dataframe()
df_fc = df_fc[(df_fc["source"] == SOURCE) & (df_fc["snapshot_slot"].astype(str) == slot)]
df_mx = b.build_market_target_dataframe(slot)
df_mx = df_mx[df_mx["snapshot_slot"].astype(str) == slot]
scale_by_icao = pd.concat([df_fc, df_mx]).drop_duplicates("icao").set_index("icao")["scale"].to_dict()
PRICES = b._build_bucket_price_map(scale_by_icao)     # {icao: {date: {slot: [[lo_bucket, hi_bucket, price], ...]}}}
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
    in_top = (top[0] is None or bucket >= top[0]) and (top[1] is None or bucket <= top[1])
    return not in_top


def floor_lot(lot, price):
    if not MIN_ORDER or not price > 0:
        return lot
    return max(lot, math.ceil(MIN_ORDER / price * 100 - 1e-9) / 100)


# ---------------- симуляция одного конфига ----------------
def simulate(rows, strategy, corr, min_price, kind, steps, last_date, detail=False):
    """rows: [(date, icao, error, bucket)] по одному городу, отсортированы по дате."""
    pyr = PYR[strategy]
    trades = []                       # (date, hit, price_cents)
    days = []                         # для панели: [дата, 1 попал / 0 промах / None нет ставки, цена в центах]
    for date, icao, err, bucket in rows:
        tb = bucket + corr
        p = lookup_price(icao, date, tb)
        if p is None or p < min_price or p > MAX_PRICE or p < MIN_BET or \
                (strategy == "forecast" and decided_elsewhere(icao, date, tb)):
            days.append([date, None, p])
            continue
        trades.append((date, (err + corr) == 0, p))
        days.append([date, 1 if (err + corr) == 0 else 0, p])
    span_days = max(1, (last_date - pd.Timestamp(rows[0][0])).days + 1)   # период данных города, дни
    n = len(trades)
    if n == 0:
        return None

    balance, peak, max_dd, miss = START_BALANCE, START_BALANCE, 0.0, 0
    streaks = []                      # (длина, дата окончания) серии промахов подряд
    cur_len = 0
    eq = [["Старт", START_BALANCE]]   # кривая баланса: одна точка на сделку
    pnls = []                         # [дата, pnl] по каждой сделке (для общих бэктестов)
    max_price = max(t[2] for t in trades) / 100
    for date, hit, pc in trades:
        price = pc / 100
        f = pyramid_factor(miss, pyr["progression"], pyr["multiplier"], pyr["steps"]) if kind == "pyramid" else 1.0
        lot = floor_lot(LOT * f, price)
        cost = (price + COMMISSION) * lot
        pnl = (lot if hit else 0) - cost
        balance += pnl
        peak = max(peak, balance)
        max_dd = max(max_dd, peak - balance)
        eq.append([date, round(balance, 4), round(pnl, 4)])
        pnls.append([date, round(pnl, 4)])
        if hit:
            if cur_len:
                streaks.append((cur_len, prev_date))
            cur_len = 0
        else:
            cur_len += 1
        prev_date = date
        if kind == "pyramid":
            miss = 0 if (hit or miss + 1 >= steps) else miss + 1
    if cur_len:
        streaks.append((cur_len, prev_date))

    # макс. риск: потеря при полностью проваленной серии на максимальной цене конфига
    if kind == "pyramid":
        risk = sum(floor_lot(LOT * pyramid_factor(k, pyr["progression"], pyr["multiplier"], pyr["steps"]), max_price)
                   * (max_price + COMMISSION) for k in range(steps))
    else:
        risk = floor_lot(LOT, max_price) * (max_price + COMMISSION)

    long_len = steps if kind == "pyramid" else FLAT_STREAK_LEN
    cut = last_date - pd.Timedelta(days=WINDOW_DAYS - 1)
    long_all = [s for s in streaks if s[0] >= long_len]
    long_14 = [s for s in long_all if pd.Timestamp(s[1]) >= cut]
    st14 = [s[0] for s in streaks if pd.Timestamp(s[1]) >= cut]

    # скользящее окно 14 дней по календарю данных города
    dts = pd.to_datetime([t[0] for t in trades])
    cal = pd.date_range(dts.min(), last_date)
    per_day = pd.Series(1, index=dts).groupby(level=0).sum().reindex(cal, fill_value=0)
    roll = per_day.rolling(WINDOW_DAYS, min_periods=WINDOW_DAYS).sum().dropna()
    if roll.empty:
        roll = pd.Series([per_day.sum()])

    # самый длинный период подряд идущих дней без сделок (весь период данных города)
    full_cal = pd.date_range(pd.Timestamp(rows[0][0]), last_date)
    traded = pd.Series(1, index=dts).groupby(level=0).sum().reindex(full_cal, fill_value=0) > 0
    gap, max_gap = 0, 0
    for t in traded.tolist():
        gap = 0 if t else gap + 1
        max_gap = max(max_gap, gap)
    profit_total = balance - START_BALANCE
    profit30 = profit_total / span_days * 30
    gain_peak = peak - START_BALANCE
    max_streak = max([s[0] for s in streaks], default=0)
    flags = [
        gain_peak > 0 and (peak - balance) > FILTER_GIVEBACK * gain_peak,       # 1 сливает набранное
        max_dd > START_BALANCE * FILTER_DD_PCT / 100,                           # 2 просадка > 15% депо
        max_dd > profit30,                                                      # 3 просадка > ср. профита за 30 дней
        risk > FILTER_RISK_SHARE * profit30,                                    # 4 макс. риск > 50% ср. профита
        max_gap >= FILTER_GAP_DAYS,                                             # 5 есть 7 дней без сделок
        max_streak > FILTER_MAX_STREAK,                                         # 6 серия промахов > 5
        profit_total < 0,                                                       # 7 отрицательная доходность
        kind == "pyramid" and steps > FILTER_MAX_PYR_STEPS,                     # 8 пирамида длиннее допустимой
    ]
    out = dict(
        flags=sum(1 << i for i, f in enumerate(flags) if f), max_gap=max_gap, pnl=pnls,
        trades=n, hits=sum(1 for t in trades if t[1]), profit=balance - START_BALANCE, profit30=(balance - START_BALANCE) / span_days * 30,
        max_dd=max_dd, max_risk=risk,
        max_streak=max([s[0] for s in streaks], default=0), n_long=len(long_all),
        max_streak_14d=max(st14, default=0), n_long_14d=len(long_14),
        mean_trades_14d=float(roll.mean()), min_trades_14d=float(roll.min()),
    )
    out["_days"] = days
    return out


# ---------------- перебор ----------------
results = []
GROUPS = {}                           # (icao, strategy, corr, min_price) -> дни [дата, отметка, цена] -- общие для всех типов
ROWS = {}                             # (strategy, icao) -> (rows, last_date) -- для детализации по клику
for strategy, src_df in (("forecast", df_fc), ("max", df_mx)):
    if src_df.empty:
        continue
    last_date = pd.to_datetime(src_df["date"]).max()
    for (icao, city), g in src_df.groupby(["icao", "city"]):
        g = g.sort_values("date").drop_duplicates("date", keep="last")
        rows = list(zip(g["date"], g["icao"], g["error"], g["forecast_bucket"].astype(int)))
        ROWS[(strategy, icao)] = (rows, last_date)
        for corr in (CORRECTIONS if strategy == "forecast" else [0]):
            for mp in MIN_PRICES:
                for kind, steps in TYPES:
                    m = simulate(rows, strategy, corr, mp, kind, steps, last_date)
                    if m is None:
                        continue
                    gdays = m.pop("_days")
                    if m["trades"] < MIN_TRADES:
                        continue
                    GROUPS.setdefault((icao, strategy, corr, mp), [d[:3] for d in gdays])
                    results.append(dict(city=city, icao=icao, strategy=strategy, corr=corr, min_price=mp,
                                        type=kind if kind == "flat" else f"pyramid-{steps}", **m))

R = pd.DataFrame(results)
if R.empty:
    raise SystemExit("Нет конфигов с достаточным числом сделок (уменьшите MIN_TRADES).")
R["hit_rate"] = R["hits"] / R["trades"] * 100
R["config"] = (R["strategy"].map({"forecast": "Прогноз", "max": "Макс"}) + ", попр. " + R["corr"].astype(str)
               + ", мин. " + R["min_price"].astype(str) + "¢, " + R["type"])

# ---------------- итоговый балл (0..100) ----------------
pct = lambda s: s.rank(pct=True, method="average")
R["_por"] = R["profit30"] / R["max_risk"]
R["_pdd"] = R["profit30"] / R["max_dd"].replace(0, np.nan)
R["_pdd"] = R["_pdd"].fillna(R["profit30"].clip(lower=0) * 10)       # просадки не было
R["_ls"] = -(R["n_long"] / R["trades"] * 100)
R["_rs"] = -(R["n_long_14d"] * 10 + R["max_streak_14d"])
parts = {
    "profit_on_risk": pct(R["_por"]), "profit_on_dd": pct(R["_pdd"]), "long_streaks": pct(R["_ls"]),
    "recent_streaks": pct(R["_rs"]), "min_trades_14d": pct(R["min_trades_14d"]),
    "mean_trades_14d": pct(R["mean_trades_14d"]),
}
R["score"] = sum(WEIGHTS[k] * v for k, v in parts.items()) * 100
R.loc[R["profit30"] <= 0, "score"] = 0.0          # убыточный конфиг не может быть "подходящим"

best_profit = R.sort_values("profit30", ascending=False).drop_duplicates("city")
best_score = R.sort_values("score", ascending=False).drop_duplicates("city")


PAGE_TITLE = "Рейтинг городов"
PAGE_H1 = ""
NOTE_PREFIX = ""
VIRT_HINT = ""

YAML_NOTE = ""

# ---------------- графики ----------------
import json

# Данные для страницы. Фильтры, рейтинги, детализация и общие бэктесты считаются в браузере по одному набору
# отмеченных фильтров, поэтому внутрь кладём ВСЕ варианты всех городов.
FILTER_LABELS = [
    f"Не отдаёт набранное: просадка от пика в конце не более {int(FILTER_GIVEBACK * 100)}% прироста",
    f"Просадка не больше {FILTER_DD_PCT}% депо (${START_BALANCE * FILTER_DD_PCT / 100:.0f})",
    "Просадка не больше ср. профита за 30 дней",
    f"Макс. риск не больше {int(FILTER_RISK_SHARE * 100)}% ср. профита за 30 дней",
    f"Нет {FILTER_GAP_DAYS} дней подряд без сделок",
    f"Серия промахов не больше {FILTER_MAX_STREAK}",
    "Доходность не отрицательная",
    f"Пирамида не больше {FILTER_MAX_PYR_STEPS} шагов",
]
city_list = sorted(R["city"].unique())
city_idx = {c: i for i, c in enumerate(city_list)}
city_icao = R.drop_duplicates("city").set_index("city")["icao"].to_dict()
icao_list = [city_icao[c] for c in city_list]
labels = sorted(R["config"].unique())
label_idx = {c: i for i, c in enumerate(labels)}
all_dates = sorted({r[0] for rows_, _ in ROWS.values() for r in rows_})
date_idx = {d: i for i, d in enumerate(all_dates)}
group_list, group_idx = [], {}
for key, days_ in GROUPS.items():
    group_idx[key] = len(group_list)
    flat = []
    for d, mk, p in days_:
        flat += [date_idx[d], -1 if mk is None else mk, -1 if p is None else p]
    group_list.append(flat)
configs = []
for r in R.itertuples():
    t = []
    for d, v in r.pnl:
        t += [date_idx[d], v]
    configs.append({
        "c": city_idx[r.city], "g": group_idx[(r.icao, r.strategy, r.corr, r.min_price)], "l": label_idx[r.config],
        "p": round(r.profit30, 3), "s": round(r.score, 2), "f": int(r.flags), "t": t,
        "y": 0 if r.strategy == "forecast" else 1, "r": int(r.corr), "u": int(r.min_price),
        "k": int(r.type.split("-")[1]) if r.type.startswith("pyramid") else 0,
        "m": [round(x, 3) for x in (r.profit, r.max_dd, r.max_risk, r.trades, r.hit_rate, r.max_streak, r.n_long,
                                    r.max_streak_14d, r.n_long_14d, r.mean_trades_14d, r.min_trades_14d)],
    })

w_html = "".join(f"<li>{k}: {int(v * 100)}%</li>" for k, v in WEIGHTS.items())
note = (NOTE_PREFIX + f"<p>Слот {slot}. Лот ${LOT}, комиссия ${COMMISSION}, потолок цены {MAX_PRICE}¢. Конфиги с числом сделок &lt; {MIN_TRADES} "
        f"отброшены. Ср. профит за 30 дней = профит за весь период города / число дней данных × 30 (метрика для сортировки и баллов); "
        f"профит за весь период показан справочно. Макс. риск: потеря полностью проваленной серии на максимальной цене конфига "
        f"(плоская — одна позиция). Длинная серия: для пирамиды ≥ числа шагов, для плоской ≥ {FLAT_STREAK_LEN} промахов подряд.</p>"
        f"<p>Балл = взвешенная сумма перцентилей среди всех конфигов (0 для убыточных по ср. профиту за 30 дней):</p><ul>{w_html}</ul>")

PAGE_JS = r"""
const START_BALANCE = __START__;
const CITYLIST = __CITYLIST__;
const ICAOS = __ICAOS__;
const GENERATED = __GENERATED__;
const MAX_PRICE_DEFAULT = __MAXPRICE__;
const WEATHER_SOURCE = __WSOURCE__;
const YAML_NOTE = __YAMLNOTE__;
const LABELS = __LABELS__;
const DATES = __DATES__;
const GROUPS = __GROUPS__;
const CONFIGS = __CONFIGS__;
const UP = "#27ae60", DOWN = "#e74c3c";
const HINT = '<p class="hint">Кликните по столбцу города на графике ниже — здесь появятся попадания/промахи, метрики и график бэктеста.</p>';
const VIRT_HINT = __VIRT_HINT__;

// --- сглаживание и раскраска кривой: тот же алгоритм, что в основных отчётах ---
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
function buildSmoothSegments(pts, thr) {
  const colorFor = y => (y >= thr ? UP : DOWN);
  const dense = smoothSeries(pts, 16);
  if (!dense.length) return [];
  const segs = [];
  let segColor = colorFor(dense[0].y), xs = [dense[0].x], ys = [dense[0].y];
  for (let i = 1; i < dense.length; i++) {
    const prev = dense[i - 1], cur = dense[i], c = colorFor(cur.y);
    if (c !== segColor) {
      const t = (thr - prev.y) / (cur.y - prev.y), cx = prev.x + t * (cur.x - prev.x);
      xs.push(cx); ys.push(thr);
      segs.push({ color: segColor, xs: xs, ys: ys });
      segColor = c; xs = [cx]; ys = [thr];
    }
    xs.push(cur.x); ys.push(cur.y);
  }
  segs.push({ color: segColor, xs: xs, ys: ys });
  return segs;
}

function renderBacktest(divId, points, titleText) {
  // points: [{x: "Старт" | дата, y: баланс, entries: [[город, pnl], ...]}]
  const cats = points.map(p => p.x);
  const pts = points.map((p, i) => ({ x: i, y: p.y }));
  const lines = buildSmoothSegments(pts, START_BALANCE).map(sg => ({
    x: sg.xs, y: sg.ys, mode: "lines", type: "scatter", line: { color: sg.color, width: 3 },
    hoverinfo: "skip", showlegend: false,
  }));
  const hover = points.map(p => {
    if (p.x === "Старт") return `Старт<br>Баланс: $${p.y.toFixed(2)}`;
    const lines = [p.x, `Баланс: $${p.y.toFixed(2)}`];
    if (p.entries.length > 1) {
      const tot = p.entries.reduce((a, e) => a + e[1], 0);
      lines.push(`${p.entries.length} гор.: ${tot >= 0 ? "+" : ""}$${tot.toFixed(2)}`);
    }
    if (p.entries.length <= 12) p.entries.forEach(e => lines.push(`${e[0]}: ${e[1] >= 0 ? "+" : ""}$${e[1].toFixed(2)}`));
    return lines.join("<br>");
  });
  const markers = {
    x: pts.map(p => p.x), y: pts.map(p => p.y), mode: "markers", type: "scatter",
    marker: { size: 7, color: pts.map(p => (p.y >= START_BALANCE ? UP : DOWN)) },
    text: hover, hoverinfo: "text", showlegend: false,
  };
  const legend = {
    x: [null], y: [null], mode: "lines+markers", type: "scatter", line: { color: UP, width: 3 },
    marker: { size: 7, color: UP }, name: "Бэктест (симуляция)", showlegend: true, hoverinfo: "skip",
  };
  const step = Math.max(1, Math.ceil(cats.length / 24));
  const tv = [], tt = [];
  cats.forEach((c, i) => { if (i % step === 0 || i === cats.length - 1) { tv.push(i); tt.push(c); } });
  Plotly.newPlot(divId, lines.concat([markers, legend]), {
    title: { text: `<b>${titleText}</b>`, x: 0.02, xanchor: "left" },
    template: "plotly_white", paper_bgcolor: "#fff", plot_bgcolor: "#fff", margin: { t: 50, r: 20, b: 90, l: 60 },
    xaxis: { title: "Дата", type: "linear", tickangle: -45, fixedrange: true, tickmode: "array", tickvals: tv, ticktext: tt,
             range: [-0.5, Math.max(cats.length - 1, 1) + 0.5], zeroline: false },
    yaxis: { title: "Баланс в $", fixedrange: true }, dragmode: false,
    shapes: [{ type: "line", x0: 0, x1: 1, xref: "paper", y0: START_BALANCE, y1: START_BALANCE, line: { color: "#999", width: 1, dash: "dot" } }],
    height: 340,
  }, { responsive: true, displayModeBar: false, scrollZoom: false });
}

// ================= фильтры: общие для рейтингов, детализации и общих бэктестов =================
const BY_CITY = CITYLIST.map(() => []);
CONFIGS.forEach((c, i) => { c.i = i; BY_CITY[c.c].push(c); });
function passes(c, mask) { return (c.f & mask) === 0; }
function pickBest(ci, mask, key) {
  let best = null;
  BY_CITY[ci].forEach(c => { if (passes(c, mask) && (best === null || c[key] > best[key])) best = c; });
  return best;
}
const KEY = { 1: "p", 2: "s" };
// доли комбинированного портфеля: половина городов flat, от остатка треть пирамида 3, остальное пирамида 2
function mixQuotas(N) {
  const flat = Math.round(N / 2), rest = N - flat, p3 = Math.round(rest / 3);
  return { flat, p3, p2: rest - p3 };
}
function hungarian(cost) {            // минимизация, квадратная матрица n x n, индексы с 0
  const n = cost.length, INF = 1e18, u = new Array(n + 1).fill(0), v = new Array(n + 1).fill(0), p = new Array(n + 1).fill(0), way = new Array(n + 1).fill(0);
  for (let i = 1; i <= n; i++) {
    p[0] = i; let j0 = 0; const minv = new Array(n + 1).fill(INF), used = new Array(n + 1).fill(false);
    do {
      used[j0] = true; const i0 = p[j0]; let delta = INF, j1 = 0;
      for (let j = 1; j <= n; j++) if (!used[j]) {
        const cur = cost[i0 - 1][j - 1] - u[i0] - v[j];
        if (cur < minv[j]) { minv[j] = cur; way[j] = j0; }
        if (minv[j] < delta) { delta = minv[j]; j1 = j; }
      }
      for (let j = 0; j <= n; j++) { if (used[j]) { u[p[j]] += delta; v[j] -= delta; } else minv[j] -= delta; }
      j0 = j1;
    } while (p[j0] !== 0);
    do { const j1 = way[j0]; p[j0] = p[j1]; j0 = j1; } while (j0);
  }
  const res = new Array(n).fill(-1);
  for (let j = 1; j <= n; j++) if (p[j]) res[p[j] - 1] = j - 1;
  return res;
}
function mixSelection(mask) {
  // для каждого города лучший (по ср. профиту за 30 дн.) вариант каждого типа среди прошедших фильтры
  const types = [c => c.k === 0, c => c.k === 3, c => c.k === 2];     // flat, пирамида 3, пирамида 2
  const cand = [];   // [город][тип] -> конфиг или null
  CITYLIST.forEach((_, ci) => {
    const row = types.map(tf => { let b = null; BY_CITY[ci].forEach(c => { if (passes(c, mask) && tf(c) && (b === null || c.p > b.p)) b = c; }); return b; });
    if (row.some(x => x)) cand.push(row);
  });
  const N = cand.length; if (!N) return [];
  const q = mixQuotas(N), slots = [];
  [[0, q.flat], [1, q.p3], [2, q.p2]].forEach(([t, k]) => { for (let i = 0; i < k; i++) slots.push(t); });
  const BIG = 1e9;
  const cost = cand.map(row => slots.map(t => row[t] ? -row[t].p : BIG));
  const asg = hungarian(cost), out = [];
  asg.forEach((j, i) => { if (j >= 0 && cand[i][slots[j]]) out.push(cand[i][slots[j]]); });
  return out;
}
function selection(n, mask) {
  if (n === 3) return mixSelection(mask);
  const out = [];
  CITYLIST.forEach((_, ci) => { const c = pickBest(ci, mask, KEY[n]); if (c) out.push(c); });
  return out;
}
function currentMask() {
  let mask = 0;
  document.querySelectorAll(".flt").forEach(el => { if (el.checked) mask |= (1 << Number(el.dataset.bit)); });
  // галочка = условие ТРЕБУЕТСЯ; бит флага = условие НЕ выполнено, значит в маску идут отмеченные
  return mask;
}

function metricsText(c) {
  const m = c.m;
  return `Ср. профит за 30 дн.: $${c.p.toFixed(2)} (${(c.p / START_BALANCE * 100 >= 0 ? "+" : "")}${(c.p / START_BALANCE * 100).toFixed(1)}%)<br>` +
    `Профит за весь период: $${m[0].toFixed(2)}<br>Макс. просадка: $${m[1].toFixed(2)}<br>Макс. риск: $${m[2].toFixed(2)}<br>` +
    `Сделок: ${m[3]} (попаданий ${m[4].toFixed(0)}%)<br>Макс. серия промахов: ${m[5]} | длинных серий: ${m[6]}<br>` +
    `За 14 дней: макс. серия ${m[7]}, длинных серий ${m[8]}<br>Сделок за 14 дн.: среднее ${m[9].toFixed(1)}, минимум ${m[10].toFixed(0)}<br>` +
    `Балл: ${c.s.toFixed(1)}`;
}

const shownCity = { 1: null, 2: null };      // город, открытый в панели детализации рейтинга n

function renderRating(n) {
  const div = document.getElementById("rank" + n);
  const picks = selection(n, currentMask()).sort((a, b) => b[KEY[n]] - a[KEY[n]]);
  const isP = n === 1;
  const title = isP ? "Рейтинг 1 — ср. профит за 30 дней, лучший вариант города среди прошедших фильтры"
                    : "Рейтинг 2 — итоговый балл (0–100), лучший вариант города среди прошедших фильтры";
  if (!picks.length) {
    Plotly.react(div, [], { title: { text: "<b>" + title + "</b>", x: 0.02, xanchor: "left" }, template: "plotly_white", height: 300,
      annotations: [{ text: "Нет вариантов, прошедших выбранные фильтры", showarrow: false, x: 0.5, y: 0.5, xref: "paper", yref: "paper" }] });
    return;
  }
  Plotly.react(div, [{
    type: "bar", x: picks.map(c => CITYLIST[c.c]), y: picks.map(c => c[KEY[n]]), marker: { color: isP ? "#2e7d32" : "#9b59b6" },
    customdata: picks.map(c => c.i),
    hovertext: picks.map(c => `<b>${CITYLIST[c.c]}</b><br>Конфиг: ${LABELS[c.l]}<br>${metricsText(c)}<br><i>Клик — детализация</i>`),
    hoverinfo: "text",
  }], {
    title: { text: "<b>" + title + "</b>", x: 0.02, xanchor: "left" }, yaxis: { title: isP ? "Ср. профит за 30 дней, $" : "Балл" },
    xaxis: { tickangle: -45 }, template: "plotly_white", height: 600,
  });
}

function showDetail(n, c) {
  const box = document.getElementById("det" + n);
  shownCity[n] = c.c;
  lastShown[n] = c;
  const city = CITYLIST[c.c];
  const traded = new Set();
  for (let k = 0; k < c.t.length; k += 2) traded.add(c.t[k]);
  const g = GROUPS[c.g];
  let days = "";
  for (let k = 0; k < g.length; k += 3) {
    const di = g[k], mk = g[k + 1], pr = g[k + 2];
    const cls = mk === 1 ? "streak-day-hit" : mk === 0 ? "streak-day-miss" : "streak-day-none";
    const txt = mk === 1 ? "✓" : mk === 0 ? "✗" : "–";
    const price = pr < 0 ? "–" : pr.toFixed(1) + "¢";
    days += `<div class="streak-day${mk >= 0 && !traded.has(di) ? " virt" : ""}"><span class="streak-day-date">${DATES[di].slice(5)}</span>
      <span class="streak-day-mark ${cls}">${txt}</span><span class="streak-day-price">${price}</span></div>`;
  }
  const m = c.m, pct = v => `${v >= 0 ? "+" : ""}${(v / START_BALANCE * 100).toFixed(1)}%`;
  const grp = (t, items) => `<div class="mgroup"><div class="mtitle">${t}</div>` +
    items.map(i => `<div class="metric">${i[0] ? `<span class="ml">${i[0]}</span>` : ""}<b>${i[1]}</b></div>`).join("") + `</div>`;
  const metrics = grp("Конфиг", [["", LABELS[c.l]]]) +
    grp("Доходность", [["Ср. профит за 30 дней", `$${c.p.toFixed(2)} (${pct(c.p)})`], ["Профит за весь период", `$${m[0].toFixed(2)} (${pct(m[0])})`],
                       ["Баланс на конец", `$${(START_BALANCE + m[0]).toFixed(2)}`]]) +
    grp("Риск", [["Макс. просадка", `$${m[1].toFixed(2)}`], ["Макс. риск", `$${m[2].toFixed(2)}`]]) +
    grp("Сделки", [["Всего", `${m[3]} (попаданий ${m[4].toFixed(0)}%)`], ["За 14 дн., среднее", m[9].toFixed(1)], ["За 14 дн., минимум", m[10].toFixed(0)]]) +
    grp("Серии промахов", [["Макс. серия", m[5]], ["Длинных серий", m[6]], ["За 14 дн.: макс. серия", m[7]], ["За 14 дн.: длинных серий", m[8]]]) +
    grp("Итог", [["Балл", c.s.toFixed(1)]]);
  box.innerHTML = `<h4>Попадания и промахи — ${city}${VIRT_HINT}</h4>
    <div class="streak-days-row">${days}</div><div class="metrics">${metrics}</div><div id="bt${n}" style="height:340px"></div>` + yamlButton("det" + n);
  const row = box.querySelector(".streak-days-row");
  row.scrollLeft = row.scrollWidth;
  let bal = START_BALANCE;
  const pts = [{ x: "Старт", y: bal, entries: [] }];
  for (let k = 0; k < c.t.length; k += 2) { bal += c.t[k + 1]; pts.push({ x: DATES[c.t[k]], y: bal, entries: [[city, c.t[k + 1]]] }); }
  renderBacktest("bt" + n, pts, `Бэктест — ${city}, Лот $__LOT__, Баланс $${START_BALANCE}`);
}

function refreshDetail(n) {
  const box = document.getElementById("det" + n);
  if (shownCity[n] === null) { box.innerHTML = HINT; return; }
  const c = pickBest(shownCity[n], currentMask(), KEY[n]);
  if (c) showDetail(n, c);
  else box.innerHTML = `<h4>${CITYLIST[shownCity[n]]}</h4><p class="hint">Нет вариантов, прошедших выбранные фильтры.</p>`;
}

// ================= общие бэктесты =================
function runPortfolio(cfgs) {
  const byDate = new Map();
  let trades = 0;
  cfgs.forEach(c => {
    for (let k = 0; k < c.t.length; k += 2) {
      const di = c.t[k];
      if (!byDate.has(di)) byDate.set(di, []);
      byDate.get(di).push([CITYLIST[c.c], c.t[k + 1]]);
      trades++;
    }
  });
  const order = Array.from(byDate.keys()).sort((a, b) => a - b);
  let bal = START_BALANCE, peak = bal, maxDD = 0, maxDDPct = 0;
  const points = [{ x: "Старт", y: bal, entries: [] }];
  order.forEach(di => {
    const entries = byDate.get(di);
    entries.forEach(e => { bal += e[1]; });
    points.push({ x: DATES[di], y: bal, entries });
    if (bal > peak) peak = bal;
    const dd = peak - bal;
    if (dd > maxDD) { maxDD = dd; maxDDPct = peak ? dd / peak * 100 : 0; }
  });
  return { points, trades, bal, pnl: bal - START_BALANCE, pnlPct: (bal - START_BALANCE) / START_BALANCE * 100, maxDD, maxDDPct };
}
const PF_TITLES = { 1: "рейтинг 1", 2: "рейтинг 2", 3: "комбинированный" };
function updatePortfolio(n) {
  const staleOut = document.getElementById("pf" + n + "wrap"); if (staleOut) { const o = staleOut.querySelector(".yaml-out"); if (o) o.innerHTML = ""; }
  const cfgs = selection(n, currentMask());
  const sum = document.getElementById("pfsum" + n), det = document.getElementById("pfcities" + n);
  if (!cfgs.length) {
    Plotly.purge("pfchart" + n);
    document.getElementById("pfchart" + n).innerHTML = "";
    sum.innerHTML = "<p class='hint'>Нет вариантов, прошедших выбранные фильтры.</p>"; det.innerHTML = "";
    return;
  }
  const r = runPortfolio(cfgs);
  const sg = r.pnl >= 0 ? "+" : "", cls = r.pnl >= 0 ? "streak-day-hit" : "streak-day-miss";
  sum.innerHTML = `<div class="backtest-stats-row"><span class="backtest-stats-label">Бэктест:</span>
    <span>Городов: <b>${cfgs.length}</b>${n === 3 ? ` (flat ${cfgs.filter(c => c.k === 0).length}, пир.3 ${cfgs.filter(c => c.k === 3).length}, пир.2 ${cfgs.filter(c => c.k === 2).length})` : ""}</span><span>Сделок: <b>${r.trades}</b></span>
    <span>Баланс: <b>$${r.bal.toFixed(2)}</b></span>
    <span>P&amp;L: <b class="${cls}">${sg}$${r.pnl.toFixed(2)} (${sg}${r.pnlPct.toFixed(1)}%)</b></span>
    <span>Макс. просадка: <b>$${r.maxDD.toFixed(2)} (${r.maxDDPct.toFixed(1)}%)</b></span></div>`;
  renderBacktest("pfchart" + n, r.points, `Бэктест — ${PF_TITLES[n]}, ${cfgs.length} гор., Лот $__LOT__, Баланс $${START_BALANCE}`);
  const rows = cfgs.slice().sort((a, b) => b.p - a.p).map(c =>
    `<tr><td>${CITYLIST[c.c]}</td><td>${LABELS[c.l]}</td><td>$${c.p.toFixed(2)}</td><td>${c.s.toFixed(1)}</td></tr>`).join("");
  det.innerHTML = `<summary>Города и конфиги в бэктесте (${cfgs.length})</summary>
    <table class="pf-table"><tr><th>Город</th><th>Конфиг</th><th>Ср. профит 30 дн.</th><th>Балл</th></tr>${rows}</table>`;
}

// ================= генерация yaml для бота =================
const FLT_LABELS = __FLTLABELS__;
function activeFiltersText() {
  const on = [];
  document.querySelectorAll(".flt").forEach(el => { if (el.checked) on.push(FLT_LABELS[Number(el.dataset.bit)]); });
  return on.length ? "фильтры: " + on.join("; ") : "фильтры: нет";
}
function yamlEntry(c, forecast) {
  const lines = [`  ${ICAOS[c.c]}:`, `    city: "${CITYLIST[c.c]}"`, `    strategy: "${c.k ? "pyramid" : "flat"}"`];
  if (c.k) lines.push(`    loss_limit: ${c.k}`);
  if (forecast) {
    lines.push(`    weather_source: "${WEATHER_SOURCE}"`, `    weather_corr:${c.r ? " " + c.r : ""}`);
  }
  lines.push(`    min_price_limit: ${c.u}`, `    comment: ""`);
  return lines.join("\n") + `  # ср. профит за 30 дн. $${c.p.toFixed(2)}, балл ${c.s.toFixed(1)}`;
}
function buildYaml(cfgs, source) {
  // отдельные файлы для стратегии "прогноз" (watched_icaos_weather_forecast.yaml) и "max" (watched_icaos_max_bet.yaml)
  const out = {};
  [[0, "weather_forecast"], [1, "max_bet"]].forEach(([y, name]) => {
    const list = cfgs.filter(c => c.y === y).sort((a, b) => b.p - a.p);
    if (!list.length) return;
    const head = [`# watched_icaos_${name}.yaml -- сгенерировано adhoc (${source}), ${GENERATED}`, `# ${activeFiltersText()}`];
    if (YAML_NOTE) head.push("# " + YAML_NOTE);
    const defaults = y === 0
      ? ["defaults:", '  weather_source: ""', "  weather_corr:", "  min_price_limit:", `  max_price_limit: ${MAX_PRICE_DEFAULT}`, '  comment: ""']
      : ["defaults:", "  min_price_limit:", `  max_price_limit: ${MAX_PRICE_DEFAULT}`, '  comment: ""'];
    out[name] = head.concat([""], defaults, ["", "watched_icaos:"], list.map(c => yamlEntry(c, y === 0))).join("\n") + "\n";
  });
  return out;
}
function yamlPanel(scope, cfgs, source) {
  const files = buildYaml(cfgs, source), names = Object.keys(files);
  if (!names.length) return "<p class='hint'>Нет вариантов для yaml.</p>";
  return names.map(name => {
    const id = `y_${scope}_${name}`;
    return `<div class="yaml-file"><div class="yaml-head"><b>watched_icaos_${name}.yaml</b>
      <button class="yaml-act" data-act="copy" data-id="${id}">Копировать</button>
      <button class="yaml-act" data-act="save" data-id="${id}" data-name="watched_icaos_${name}.yaml">Скачать</button></div>
      <textarea id="${id}" class="yaml-text" readonly rows="${Math.min(30, files[name].split("\n").length + 1)}">${files[name].replace(/&/g, "&amp;").replace(/</g, "&lt;")}</textarea></div>`;
  }).join("");
}
function yamlButton(scope) { return `<div class="yaml-bar"><button class="yaml-gen" data-scope="${scope}">Сгенерировать yaml стратегии</button></div><div class="yaml-out"></div>`; }
const lastShown = { 1: null, 2: null };
document.addEventListener("click", ev => {
  const t = ev.target;
  if (!t || !t.classList) return;
  if (t.classList.contains("yaml-gen")) {
    const scope = t.dataset.scope, n = Number(scope.slice(-1));
    const out = t.parentElement.parentElement.querySelector(".yaml-out");
    let cfgs, source;
    if (scope.startsWith("pf")) { cfgs = selection(n, currentMask()); source = n === 3 ? "комбинированный общий бэктест (1/2 flat, 1/6 пирамида 3, 1/3 пирамида 2)" : "общий бэктест по рейтингу " + n; }
    else { cfgs = lastShown[n] ? [lastShown[n]] : []; source = "город " + (lastShown[n] ? CITYLIST[lastShown[n].c] : "") + ", рейтинг " + n; }
    out.innerHTML = yamlPanel(scope, cfgs, source);
  } else if (t.classList.contains("yaml-act")) {
    const area = document.getElementById(t.dataset.id);
    if (t.dataset.act === "copy") {
      area.select();
      const done = () => { const old = t.textContent; t.textContent = "Скопировано"; setTimeout(() => { t.textContent = old; }, 1200); };
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(area.value).then(done, () => { document.execCommand("copy"); done(); });
      else { document.execCommand("copy"); done(); }
    } else {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([area.value], { type: "text/yaml;charset=utf-8" }));
      a.download = t.dataset.name; document.body.appendChild(a); a.click(); a.remove();
    }
  }
});

function updateAll() {
  [1, 2].forEach(n => { renderRating(n); refreshDetail(n); });
  [1, 2, 3].forEach(n => updatePortfolio(n));
}
const master = document.getElementById("fltMaster"), boxes = document.querySelectorAll(".flt");
master.addEventListener("change", () => { boxes.forEach(b => { b.checked = master.checked; }); updateAll(); });
boxes.forEach(b => b.addEventListener("change", () => { master.checked = Array.from(boxes).every(x => x.checked); updateAll(); }));
[1, 2].forEach(n => {
  const el = document.getElementById("rank" + n);
  if (el && el.on) el.on("plotly_click", ev => {
    if (ev.points && ev.points.length) showDetail(n, CONFIGS[ev.points[0].customdata]);
  });
});
updateAll();
"""
CSS = """body{font-family:sans-serif;margin:16px}h2{margin:28px 0 4px}.hint{color:#888}.det{margin:8px 0 12px}
.det h4{margin:4px 0 8px}.streak-days-row{display:flex;flex-wrap:nowrap;gap:1px;overflow-x:auto;padding-bottom:6px}
.streak-day{display:inline-flex;flex:0 0 auto;flex-direction:column;align-items:center;width:36px}
.streak-day-date{font-size:10px;color:#888;white-space:nowrap}.streak-day-price{font-size:10px;color:#888;margin-top:2px;white-space:nowrap}
.streak-day-mark{display:inline-block;width:22px;height:22px;line-height:22px;border-radius:4px;font-weight:bold;text-align:center}
.virt{opacity:.4}.streak-day-hit{background:#e8f8ee;color:#1e7e34}.streak-day-miss{background:#fdecea;color:#c0392b}.streak-day-none{background:#f5f5f5;color:#bbb}
.metrics{display:flex;flex-wrap:wrap;gap:10px 28px;margin:10px 0 8px;font-size:14px}.mgroup{min-width:150px}
.mtitle{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:#888;border-bottom:1px solid #e5e5e5;margin-bottom:3px}
.metric{line-height:1.5}.metric .ml{color:#666;margin-right:6px}
.pf{margin:16px 0 28px}.flt-panel{position:sticky;top:0;z-index:5;background:#fff;border-bottom:1px solid #ddd;padding:6px 0 8px}
.flt-panel .flt-title{font-size:13px;color:#555;margin-bottom:4px}.flt-list{display:flex;flex-wrap:wrap;gap:4px 18px;font-size:13px}
.yaml-bar{margin:8px 0}.yaml-gen,.yaml-act{font-size:13px;padding:5px 12px;border:1px solid #9b59b6;background:#fff;color:#7d3c98;border-radius:4px;cursor:pointer}.yaml-gen:hover,.yaml-act:hover{background:#f5eef8}.yaml-file{margin:8px 0}.yaml-head{display:flex;gap:10px;align-items:center;margin-bottom:4px}.yaml-text{width:100%;box-sizing:border-box;font-family:monospace;font-size:12px;border:1px solid #ddd;padding:6px}.backtest-stats-row{display:flex;flex-wrap:wrap;gap:6px 22px;margin:4px 0;font-size:14px}
.backtest-stats-label{font-weight:bold}.pf-table{border-collapse:collapse;font-size:13px;margin-top:6px}
.pf-table td,.pf-table th{border-bottom:1px solid #eee;padding:2px 12px 2px 0;text-align:left}"""


def empty_fig_html(div_id, first):
    fig = go.Figure()
    fig.update_layout(height=600, template="plotly_white")
    return fig.to_html(full_html=False, include_plotlyjs="cdn" if first else False, div_id=div_id)


flt_html = ("<div class='flt-panel'><div class='flt-title'>Условия прохождения (отмеченные должны выполняться). "
            "Применяются к рейтингам, детализации и общим бэктестам.</div><div class='flt-list'>"
            "<label><input type='checkbox' id='fltMaster' checked> <b>Все фильтры</b></label>"
            + "".join(f"<label><input type='checkbox' class='flt' data-bit='{i}' checked> {t}</label>" for i, t in enumerate(FILTER_LABELS))
            + "</div></div>")
def yaml_btn(n):
    return f"<div class='yaml-bar'><button class='yaml-gen' data-scope='pf{n}'>Сгенерировать yaml стратегии</button></div><div class='yaml-out'></div>"


pf_html = ("<h2 style='margin-top:40px'>Общие бэктесты по рейтингам (портфель городов)</h2>"
           "<p class='hint'>Для каждого города берётся лучший вариант среди прошедших фильтры (рейтинг 1 — по ср. профиту за 30 дней, "
           "рейтинг 2 — по баллу). Все города торгуются на одном общем балансе. Комбинированный бэктест: "
           "половина городов — flat, от оставшейся половины треть — пирамида 3, остальное — пирамида 2; "
           "тип города подбирается так, чтобы суммарный ср. профит за 30 дней был максимальным.</p>"
           + "".join(f"<div class='pf' id='pf{n}wrap'><h3>{'Комбинированный бэктест (flat / пирамида 3 / пирамида 2)' if n == 3 else 'Бэктест по рейтингу ' + str(n)}</h3><div id='pfsum{n}'></div>"
                     f"<div id='pfchart{n}' style='height:340px'></div><details id='pfcities{n}'></details>{yaml_btn(n)}</div>" for n in (1, 2, 3)))
page_js = (PAGE_JS.replace("__CITYLIST__", json.dumps(city_list, ensure_ascii=False)).replace("__ICAOS__", json.dumps(icao_list))
           .replace("__GENERATED__", json.dumps(pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")))
           .replace("__MAXPRICE__", str(MAX_PRICE)).replace("__WSOURCE__", json.dumps("Windy" if SOURCE.lower() == "windy" else ""))
           .replace("__YAMLNOTE__", json.dumps(YAML_NOTE, ensure_ascii=False)).replace("__LABELS__", json.dumps(labels, ensure_ascii=False))
           .replace("__DATES__", json.dumps(all_dates)).replace("__GROUPS__", json.dumps(group_list, separators=(",", ":")))
           .replace("__CONFIGS__", json.dumps(configs, separators=(",", ":"))).replace("__FLTLABELS__", json.dumps(FILTER_LABELS, ensure_ascii=False)).replace("__VIRT_HINT__", json.dumps(VIRT_HINT, ensure_ascii=False))
           .replace("__START__", str(START_BALANCE)).replace("__LOT__", str(LOT)))
with open(OUT_HTML, "w", encoding="utf-8") as fh:
    fh.write(f"<html><head><meta charset='utf-8'><title>{PAGE_TITLE}</title><style>{CSS}</style></head><body>")
    fh.write(PAGE_H1 + flt_html)
    fh.write("<h2>Рейтинг 1 — лучший средний профит за 30 дней по перебору параметров</h2><div id='det1' class='det'></div>")
    fh.write(empty_fig_html("rank1", True))
    fh.write("<h2>Рейтинг 2 — итоговый балл (0–100)</h2><div id='det2' class='det'></div>")
    fh.write(empty_fig_html("rank2", False))
    fh.write(pf_html + note)
    fh.write("<script>" + page_js + "</script></body></html>")
print("Готово:", os.path.join(ROOT, OUT_HTML), "| конфигов:", len(R), "| городов:", R["city"].nunique())
best_score = R.sort_values("score", ascending=False).drop_duplicates("city")
print(best_score[["city", "config", "profit30", "profit", "max_dd", "max_risk", "trades", "score"]].head(15).to_string(index=False))
