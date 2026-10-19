# ===== backtest_v2: простое правило вместо подбора гиперпараметров по городам =====
# Запуск: python backtest_v2.py [форвард_дней] [тест_дней]   (скрипт лежит в adhoc/, работает с папкой бота ../weather_bot; результат пишется туда же).
#
# Три периода: бек -- вся история до форварда; форвард -- FWD_DAYS дней (10); тест -- последние TEST_DAYS дней (10). Стратегия города выбирается ТОЛЬКО по
# профиту на беке, данные теста в выборе не участвуют (в том числе в поправке по моде ошибок). Результат показывается и на беке
# (завышен, это данные выбора), и на тесте (честная проверка на невидимых днях). Тест 0 -- всё в беке, для итогового yaml.
#
# Правило. Для каждого города выбирается ТОЛЬКО стратегия: "Прогноз" (weather_forecast) или "Макс" (max_bet). Всё остальное одинаково:
#   * тип ставки и мин. цена ПЕРЕБИРАЮТСЯ по городу: плоская / пирамида 2 / пирамида 3 и 10,20,30,35,40 (галочками можно сузить);
#   * поправка прогноза: по самой частой ошибке прогноза за ВЕСЬ бек города (меняется с 0, только если соседняя
#     корзина обгоняет нулевую минимум на G_MARGIN долей дней); у стратегии Макс поправки нет.
# Фильтры в отчёте -- те же 8 условий прохождения, что в city_rating.html (галочки, пересчёт на лету в браузере), плюс галочка
# "применять фильтры на форварде" (по умолчанию выкл.: выбор по одному беку). Подбора мин. цены/типа по городу нет: walk-forward проверка (walk_forward_check.py)
# показала, что подбор по городу переоценивает результат, а выбор между двумя стратегиями -- нет.
# Результат: city_rating_v2.html (портфели на беке и тесте, таблица городов, yaml с кнопками копирования/скачивания).
import os, sys, math, json
import numpy as np, pandas as pd

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "weather_bot")   # папка бота: config/, src/, data_mining/
os.chdir(ROOT)
sys.path.insert(0, ROOT)
from src.config import Config
from src.cities import CITIES
from src.accuracy_report import AccuracyReportBuilder
from src.pyramid import pyramid_factor

# ---------------- настройки ----------------
SLOT          = None
SOURCE        = "wunderground"
FWD_DAYS      = 10               # форвард -- N дней перед тестом (фильтры можно применять и на нём)
TEST_DAYS     = 10               # тест -- последние N дней истории, фильтры и выбор его не видят; бек -- всё, что раньше (0 -- без теста)
WATCH_CITIES = ["Jeddah", "Warsaw", "Munich", "Milan", "Paris", "Taipei", "Seattle", "Toronto", "Helsinki", "Busan", "Lucknow"]   # города отчёта (пусто -- все города)
FIXED = None   # {город: (стратегия, шагов пирамиды, мин. цена, поправка)}: заданный конфиг вместо подбора (см. backtest_v2_config.py)
MIN_PRICES    = [10, 20, 30, 35, 40]   # перебор минимальной цены, ¢
TYPES         = [("flat", 0), ("pyramid", 2), ("pyramid", 3)]   # перебор типа ставки: (тип, шагов); pyramid N = loss_limit N
MAX_PRICE     = 66
LOT, COMMISSION, START_BALANCE = 5.0, 0.012, 100.0
CORRECTIONS   = [-1, 0, 1]
G_MARGIN      = 0.10             # поправка меняется с 0, только если соседняя корзина обгоняет нулевую на столько долей дней
G_MIN_DAYS    = 20               # минимум дней истории ошибок для смены поправки
MIN_TRADES    = 8                # минимум сделок на беке, чтобы стратегия города считалась (галочка в отчёте)
MIN_TRADES_FWD = 4              # если фильтры применяются на форварде: меньше сделок на форварде -- вариант отбрасывается (как в city_rating)
FILTER_GIVEBACK, FILTER_DD_PCT, FILTER_RISK_SHARE, FILTER_GAP_DAYS, FILTER_MAX_STREAK, FILTER_MAX_PYR_STEPS = 0.5, 15, 0.5, 7, 5, 2
OUT_HTML      = "city_rating_v2.html"
if len(sys.argv) >= 2:
    FWD_DAYS = int(sys.argv[1])
if len(sys.argv) >= 3:
    TEST_DAYS = int(sys.argv[2])

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


def simulate(rows, strategy, corr, min_price, kind, steps):
    """rows: данные города за окно. Тип ставки kind (flat / pyramid steps), мин. цена min_price. Возвращает список (date, hit, price_cents, cost, pnl)."""
    pyr = PYR[strategy]
    recs, miss = [], 0
    for date, icao, err, bucket in rows:
        tb = bucket + corr
        p = lookup_price(icao, date, tb)
        if p is None or p < min_price or p > MAX_PRICE or p < MIN_BET or \
                (strategy == "forecast" and decided_elsewhere(icao, date, tb)):
            continue
        hit = (err + corr) == 0
        price = p / 100
        f = pyramid_factor(miss, pyr["progression"], pyr["multiplier"], pyr["steps"]) if kind == "pyramid" else 1.0
        lot = floor_lot(LOT * f, price)
        cost = (price + COMMISSION) * lot
        recs.append((date, hit, p, cost, (lot if hit else 0) - cost))
        if kind == "pyramid":
            miss = 0 if (hit or miss + 1 >= steps) else miss + 1
    return recs



# ---------------- бек / тест ----------------
all_rows = {}
for strategy, src_df in (("forecast", df_fc), ("max", df_mx)):
    for (icao, city), g in src_df.groupby(["icao", "city"]):
        if WATCH_CITIES and city not in WATCH_CITIES:
            continue
        g = g.sort_values("date").drop_duplicates("date", keep="last")
        all_rows[(strategy, icao)] = (city, list(zip(g["date"], g["icao"], g["error"], g["forecast_bucket"].astype(int))))
_missing = [c for c in WATCH_CITIES if c not in {c_ for c_, _ in all_rows.values()}]
if _missing:
    print("ВНИМАНИЕ: нет данных по городам:", ", ".join(_missing))
dates_all = pd.to_datetime([r[0] for _, rows in all_rows.values() for r in rows])
FIRST, LAST = dates_all.min(), dates_all.max()
N_DAYS = (LAST - FIRST).days + 1
TEST_DAYS = max(0, TEST_DAYS)
FWD_DAYS = max(0, min(FWD_DAYS, N_DAYS - 10 - TEST_DAYS))   # на беке остаётся минимум 10 дней
TEST_DAYS = min(TEST_DAYS, N_DAYS - 10 - FWD_DAYS)
TEST_IDX = N_DAYS - TEST_DAYS                                # индекс первого дня теста (N_DAYS -- теста нет)
FWD_IDX = TEST_IDX - FWD_DAYS                                # индекс первого дня форварда (= конец бека)
TEST_START = FIRST + pd.Timedelta(days=TEST_IDX)
FWD_START = FIRST + pd.Timedelta(days=FWD_IDX)
BACK_SPAN = FWD_IDX
DATES = [(FIRST + pd.Timedelta(days=i)).strftime("%Y-%m-%d") for i in range(N_DAYS)]
print(f"История: {FIRST.date()} .. {LAST.date()} ({N_DAYS} дн.); бек {FIRST.date()} .. {(FWD_START - pd.Timedelta(days=1)).date()} ({BACK_SPAN} дн.), форвард {FWD_DAYS} дн., "
      f"тест {TEST_START.date() if TEST_DAYS else '-'} .. {LAST.date() if TEST_DAYS else '-'} ({TEST_DAYS} дн.); перебор: типы {[k for _, k in TYPES]}, мин. цены {MIN_PRICES}")
if BACK_SPAN < 20:
    print("ВНИМАНИЕ: бек короче 20 дней, выбор будет шумным.")

cities = sorted({(icao, c) for (s, icao), (c, _) in all_rows.items()}, key=lambda x: x[1])
CITY_DATA = []
for icao, city in cities:
    item = dict(icao=icao, city=city, corr={}, v=[])
    for strategy in ("forecast", "max"):
        key = (strategy, icao)
        if key not in all_rows:
            continue
        rows_all = all_rows[key][1]
        back_rows = [r for r in rows_all if pd.Timestamp(r[0]) < FWD_START]
        corr = 0
        if strategy == "forecast":
            errs = [r[2] for r in back_rows]                 # поправка -- только по беку (форвард и тест не участвуют)
            if len(errs) >= G_MIN_DAYS:
                cnt = {c: sum(1 for e in errs if e + c == 0) for c in CORRECTIONS}
                cb = max(CORRECTIONS, key=lambda c: cnt[c])
                if cb != 0 and (cnt[cb] - cnt[0]) / len(errs) >= G_MARGIN:
                    corr = cb
        if FIXED and city in FIXED and FIXED[city][0] == strategy == "forecast":
            corr = FIXED[city][3]                            # поправка задана конфигом
        item["corr"][strategy] = corr
        for mp in sorted(set(MIN_PRICES) | ({FIXED[city][2]} if FIXED and city in FIXED else set())):
            for kind, steps in TYPES:
                recs = simulate(rows_all, strategy, corr, mp, kind, steps)   # непрерывная серия пирамиды по всей истории
                trades = [[(pd.Timestamp(r[0]) - FIRST).days, round(r[4], 4), round(r[3], 4), r[2]] for r in recs]
                item["v"].append(dict(s=strategy, k=steps, m=mp, t=trades))
    CITY_DATA.append(item)

# ---------------- расчёт в Python: то же, что делает JS при начальных галочках (для консоли и для сверки) ----------------
def stat_py(trades, a, b_):
    n = b_ - a
    if n <= 0:
        return dict(profit=0.0, p30=0.0, dd=0.0, load=0.0, n=0, hit=None)
    pnl, cost = [0.0] * n, [0.0] * n
    cnt = hits = 0
    for d, p, c, *_ in trades:
        if a <= d < b_:
            pnl[d - a] += p
            cost[d - a] += c
            cnt += 1
            hits += 1 if p > 0 else 0
    bal = peak = START_BALANCE
    dd = profit = load = 0.0
    for i in range(n):
        bal += pnl[i]
        profit += pnl[i]
        peak = max(peak, bal)
        dd = max(dd, peak - bal)
        load = max(load, cost[i])
    return dict(profit=profit, p30=profit / n * 30, dd=dd, load=load, n=cnt, hit=(hits / cnt * 100) if cnt else None)


def select_py():
    """Для консоли: лучший по профиту/30 дн. на беке вариант города (без фильтров, только минимум сделок на беке)."""
    best_rows = []
    for c in CITY_DATA:
        cands = []
        for v in c["v"]:
            bs = stat_py(v["t"], 0, FWD_IDX)
            if bs["n"] >= MIN_TRADES:
                cands.append((bs["p30"], v))
        if cands:
            best_rows.append((c["city"], max(cands, key=lambda x: x[0])[1]))
    allt = [t for _, v in best_rows for t in v["t"]]
    return best_rows, dict(back=stat_py(allt, 0, FWD_IDX), fwd=stat_py(allt, FWD_IDX, TEST_IDX), test=stat_py(allt, TEST_IDX, N_DAYS))


_best, _pf = select_py()
print(f"Городов с вариантом: {len(_best)} из {len(CITY_DATA)} (без фильтров); вариантов на город: {len(MIN_PRICES) * len(TYPES) * 2}")
for k, v in _pf.items():
    print(f"  {k}: профит ${v['profit']:.2f} (/30д ${v['p30']:.2f}), просадка ${v['dd']:.2f}, нагрузка ${v['load']:.2f}, сделок {v['n']}")

# ---------------- фильтры (как в city_rating.html) и таблица риска ----------------
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
# макс. риск: потеря полностью проваленной серии пирамиды на цене p¢ (индекс -- цена в центах)
RISK = {s: {str(k): [((sum(floor_lot(LOT * pyramid_factor(i, PYR[s]["progression"], PYR[s]["multiplier"], PYR[s]["steps"]), p / 100) * (p / 100 + COMMISSION)
                                 for i in range(k)) if k else floor_lot(LOT, p / 100) * (p / 100 + COMMISSION)) if p > 0 else 0.0) for p in range(101)]
             for _, k in TYPES} for s in ("forecast", "max")}
FLT = dict(labels=FILTER_LABELS, giveback=FILTER_GIVEBACK, dd_pct=FILTER_DD_PCT, risk_share=FILTER_RISK_SHARE, gap=FILTER_GAP_DAYS,
           streak=FILTER_MAX_STREAK, pyr_steps=FILTER_MAX_PYR_STEPS, risk=RISK)

# ---------------- отчёт ----------------
NOW = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")
DATA = dict(fixed=({c: list(v[:3]) for c, v in FIXED.items()} if FIXED else None), cities=CITY_DATA, dates=DATES, n_days=N_DAYS, test_idx=TEST_IDX, fwd_idx=FWD_IDX, fwd_days=FWD_DAYS, back_span=BACK_SPAN, test_days=TEST_DAYS, lot=LOT, commission=COMMISSION,
            start_balance=START_BALANCE, max_price=MAX_PRICE, mps=MIN_PRICES, kinds=[k for _, k in TYPES], min_trades=MIN_TRADES, margin=G_MARGIN,
            min_trades_fwd=MIN_TRADES_FWD, flt=FLT, wsrc="Windy" if SOURCE.lower() == "windy" else "", now=NOW, slot=slot)

PAGE = r"""
const D = __DATA__;
const NDAYS = D.n_days, FS = D.fwd_idx, TS = D.test_idx, HAS_FWD = FS < TS, HAS_TEST = TS < NDAYS;
const CB = "#27ae60", CF = "#2980b9", CT = "#8e44ad";
const period = x => (x >= D.dates[TS] && HAS_TEST) ? "тест" : ((x >= D.dates[FS] && HAS_FWD) ? "форвард" : "бек");
const fmt = v => (v === null || v === undefined) ? "–" : ((v >= 0 ? "+" : "-") + "$" + Math.abs(v).toFixed(2));
const money = v => (v < 0 ? "-" : "") + "$" + Math.abs(v).toFixed(2);
const cls = v => v === null || v === undefined ? "" : (v >= 0 ? "g" : "r");
const SNAME = { forecast: "Прогноз", max: "Макс" };
const COL = { "Выбор по городу": "#2980b9", "Прогноз для всех": "#e67e22", "Макс для всех": "#27ae60", "Заданный конфиг": "#2980b9", "Только Прогноз": "#e67e22", "Только Макс": "#27ae60" };
const MPN = m => m ? m + "¢" : "нет";
const el = id => document.getElementById(id);
const esc = s => s.replace(/&/g, "&amp;").replace(/</g, "&lt;");

// ---- статистика по списку сделок [[день, pnl, затраты], ...] на отрезке дней [a, b) ----
function stat(trades, a, b, startBal) {
  if (startBal === undefined) startBal = D.start_balance;
  const n = b - a;
  if (n <= 0) return { profit: 0, p30: 0, dd: 0, ddPct: 0, load: 0, n: 0, hit: null, bal: startBal };
  const pnl = new Array(n).fill(0), cost = new Array(n).fill(0);
  let cnt = 0, hits = 0;
  trades.forEach(t => { const d = t[0]; if (d >= a && d < b) { pnl[d - a] += t[1]; cost[d - a] += t[2]; cnt++; if (t[1] > 0) hits++; } });
  let bal = startBal, peak = bal, dd = 0, ddPct = 0, profit = 0, load = 0;
  for (let i = 0; i < n; i++) {
    bal += pnl[i]; profit += pnl[i];
    if (bal > peak) peak = bal;
    if (peak - bal > dd) { dd = peak - bal; ddPct = dd / peak * 100; }
    if (cost[i] > load) load = cost[i];
  }
  return { profit: profit, p30: profit / n * 30, dd: dd, ddPct: ddPct, bal: bal, load: load, n: cnt, hit: cnt ? hits / cnt * 100 : null };
}

// ---- условия прохождения (как в city_rating.html): список номеров нарушенных условий на отрезке [a, b) ----
function failed(trades, strategy, kind, a, b) {
  const F = D.flt, n = b - a, tr = trades.filter(t => t[0] >= a && t[0] < b);
  let bal = D.start_balance, peak = bal, dd = 0, profit = 0, maxP = 0, miss = 0, maxStreak = 0, gap = 0, maxGap = 0;
  const days = new Set();
  tr.forEach(t => {
    bal += t[1]; profit += t[1];
    if (bal > peak) peak = bal;
    if (peak - bal > dd) dd = peak - bal;
    if (t[3] > maxP) maxP = t[3];
    if (t[1] > 0) miss = 0; else { miss++; if (miss > maxStreak) maxStreak = miss; }
    days.add(t[0]);
  });
  for (let d = a; d < b; d++) { if (days.has(d)) gap = 0; else { gap++; if (gap > maxGap) maxGap = gap; } }
  const profit30 = n > 0 ? profit / n * 30 : 0, risk = F.risk[strategy][String(kind)][maxP] || 0, gainPeak = peak - D.start_balance;
  const flags = [
    gainPeak > 0 && (peak - bal) > F.giveback * gainPeak,
    dd > D.start_balance * F.dd_pct / 100,
    dd > profit30,
    risk > F.risk_share * profit30,
    maxGap >= F.gap,
    maxStreak > F.streak,
    profit < 0,
    kind > F.pyr_steps,
  ];
  return { bits: flags.map((f, i) => f ? i : -1).filter(i => i >= 0), n: tr.length };
}
const KNAME = k => k ? "пирамида " + k : "плоская";
function checkedVals(cls_) { return Array.from(document.querySelectorAll("." + cls_)).filter(x => x.checked).map(x => Number(x.dataset.v)); }
function checkedBits() { return Array.from(document.querySelectorAll(".flt")).filter(x => x.checked).map(x => Number(x.dataset.bit)); }

// ---- выбор стратегии по городу ----
// кандидат (город, стратегия) допускается, если на беке сделок не меньше min_trades и не нарушено ни одно отмеченное условие
// (при галочке "на форварде" -- то же на форварде, и там сделок не меньше min_trades_fwd). Среди допущенных -- лучший профит/30 дн. на беке.
const better = arr => arr.reduce((x, y) => (x === null || y.b.p30 > x.b.p30) ? y : x, null);
// все варианты города (30 шт.) со статистикой и флагами; allowed -- разрешён галочками перебора
function evalCity(c) {
  const bits = checkedBits(), onFwd = el("cbFwd").checked && HAS_FWD, kinds = checkedVals("kbox"), mps = checkedVals("mbox");
  return c.v.map(v => {
    const b = stat(v.t, 0, FS), fb = failed(v.t, v.s, v.k, 0, FS).bits.filter(i => bits.includes(i));
    let ff = [], fwdN = null;
    if (onFwd) { const r = failed(v.t, v.s, v.k, FS, TS); ff = r.bits.filter(i => bits.includes(i)); fwdN = r.n; }
    const allowed = kinds.includes(v.k) && mps.includes(v.m);
    const enough = b.n >= D.min_trades && (!onFwd || fwdN >= D.min_trades_fwd);
    return { s: v.s, k: v.k, m: v.m, t: v.t, b: b, allowed: allowed, enough: enough, ok: allowed && enough && !fb.length && !ff.length, fb: fb, ff: ff, corr: c.corr[v.s] };
  });
}
function selectFixed() {
  const rows = [];
  D.cities.forEach(c => {
    const fx = D.fixed[c.city];
    if (!fx) return;
    const v = evalCity(c).find(x => x.s === fx[0] && x.k === fx[1] && x.m === fx[2]);
    if (!v) return;
    const cand = { forecast: null, max: null }; cand[v.s] = v;
    rows.push({ icao: c.icao, city: c.city, strategy: v.s, k: v.k, m: v.m, corr: v.corr, b: v.b, cand: cand, t: v.t, nvar: 1, fwd: stat(v.t, FS, TS), test: stat(v.t, TS, NDAYS) });
  });
  rows.sort((x, y) => y.b.p30 - x.b.p30);
  const pf = { "Заданный конфиг": rows.map(r => [r.city, r.t]), "Только Прогноз": rows.filter(r => r.strategy === "forecast").map(r => [r.city, r.t]), "Только Макс": rows.filter(r => r.strategy === "max").map(r => [r.city, r.t]) };
  return { rows, skipped: [], excluded: [], pf };
}
function select() {
  if (D.fixed) return selectFixed();
  const rows = [], skipped = [], excluded = [];
  D.cities.forEach(c => {
    const vs = evalCity(c).filter(v => v.allowed);
    const enough = vs.filter(v => v.enough);
    if (!enough.length) { skipped.push({ city: c.city, n: vs.length ? Math.max.apply(null, vs.map(v => v.b.n)) : 0 }); return; }
    const okv = enough.filter(v => v.ok);
    if (!okv.length) {
      const why = ["forecast", "max"].map(s => { const x = better(enough.filter(v => v.s === s)); return x ? SNAME[s] + " " + MPN(x.m) + " " + KNAME(x.k) + ": " + x.fb.map(i => "№" + (i + 1)).concat(x.ff.map(i => "№" + (i + 1) + "ф")).join(", ") : null; }).filter(Boolean);
      excluded.push({ city: c.city, why: why.join("; ") });
      return;
    }
    const best = better(okv), cand = {};
    ["forecast", "max"].forEach(s => { cand[s] = better(okv.filter(v => v.s === s)) || better(enough.filter(v => v.s === s)); });
    rows.push({ icao: c.icao, city: c.city, strategy: best.s, k: best.k, m: best.m, corr: best.corr, b: best.b, cand: cand, t: best.t, nvar: okv.length,
                fwd: stat(best.t, FS, TS), test: stat(best.t, TS, NDAYS) });
  });
  rows.sort((x, y) => y.b.p30 - x.b.p30);
  const pf = {
    "Выбор по городу": rows.map(r => [r.city, r.t]),
    "Прогноз для всех": rows.filter(r => r.cand.forecast && r.cand.forecast.ok).map(r => [r.city, r.cand.forecast.t]),
    "Макс для всех": rows.filter(r => r.cand.max && r.cand.max.ok).map(r => [r.city, r.cand.max.t]),
  };
  return { rows, skipped, excluded, pf };
}

// ---- портфель: метрики на беке и тесте + точки кривой баланса по дням со сделками ----
function portfolio(items) {
  const all = [], byDay = {};
  items.forEach(it => it[1].forEach(t => {
    all.push(t);
    byDay[t[0]] = byDay[t[0]] || {};
    byDay[t[0]][it[0]] = (byDay[t[0]][it[0]] || 0) + t[1];
  }));
  const points = [{ x: "Старт", y: D.start_balance, entries: [] }];
  let bal = D.start_balance;
  for (let d = 0; d < NDAYS; d++) {
    if (!byDay[d]) continue;
    const entries = Object.keys(byDay[d]).map(c => [c, Math.round(byDay[d][c] * 100) / 100]).sort((p, q) => Math.abs(q[1]) - Math.abs(p[1]));
    bal += entries.reduce((a, e) => a + e[1], 0);
    points.push({ x: D.dates[d], y: Math.round(bal * 100) / 100, entries: entries });
  }
  const sb = stat(all, 0, FS), sf = stat(all, FS, TS, sb.bal), st = stat(all, TS, NDAYS, sf.bal);
  return { n: items.length, back: sb, fwd: sf, test: st, points: points };
}

// ---- сглаживание кривой: тот же алгоритм, что в основных отчётах ----
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

function renderChart(pfs, div, title) {
  div = div || "pfchart";
  const names = Object.keys(pfs).filter(n => pfs[n].n > 0);
  if (!names.length) { if (window.Plotly) Plotly.purge(div); el(div).innerHTML = "<p class='hint'>Нет городов по выбранным фильтрам.</p>"; return; }
  const dateSet = new Set();
  names.forEach(n => pfs[n].points.forEach(p => { if (p.x !== "Старт") dateSet.add(p.x); }));
  const cats = ["Старт"].concat(Array.from(dateSet).sort());
  const idx = {}; cats.forEach((c, i) => { idx[c] = i; });
  const traces = [];
  names.forEach(n => {
    const col = COL[n] || "#2980b9", points = pfs[n].points;
    const pts = points.map(p => ({ x: idx[p.x], y: p.y }));
    const dense = smoothSeries(pts, 16);
    if (dense.length > 1) traces.push({ x: dense.map(d => d.x), y: dense.map(d => d.y), mode: "lines", type: "scatter", line: { color: col, width: 3 }, hoverinfo: "skip", showlegend: false, legendgroup: n });
    const hover = points.map(p => {
      if (p.x === "Старт") return `${n}<br>Старт<br>Баланс: $${p.y.toFixed(2)}`;
      const ls = [`<b>${n}</b>`, p.x + " (" + period(p.x) + ")", `Баланс: $${p.y.toFixed(2)}`];
      if (p.entries.length > 1) ls.push(`${p.entries.length} гор.: ${fmtPnl(p.entries.reduce((a, e) => a + e[1], 0))}`);
      if (p.entries.length <= 50) {
        const items = p.entries.map(e => `${e[0]}: ${fmtPnl(e[1])}`), per = Math.max(1, Math.ceil(items.length / 10));
        for (let i = 0; i < items.length; i += per) ls.push(items.slice(i, i + per).join("  |  "));
      }
      return ls.join("<br>");
    });
    traces.push({ x: pts.map(p => p.x), y: pts.map(p => p.y), mode: "markers", type: "scatter", marker: { size: 7, color: col }, text: hover, hoverinfo: "text", showlegend: false, legendgroup: n });
    traces.push({ x: [null], y: [null], mode: "lines+markers", type: "scatter", line: { color: col, width: 3 }, marker: { size: 7, color: col }, name: div === "pfchart" ? `${n} (${pfs[n].n} гор.)` : n, showlegend: true, hoverinfo: "skip", legendgroup: n });
  });
  const step = Math.max(1, Math.ceil(cats.length / 24));
  const tv = [], tt = [];
  cats.forEach((c, i) => { if (i % step === 0 || i === cats.length - 1) { tv.push(i); tt.push(c); } });
  const shapes = [{ type: "line", x0: 0, x1: 1, xref: "paper", y0: D.start_balance, y1: D.start_balance, line: { color: "#999", width: 1, dash: "dot" } }];
  const annotations = [];
  const firstIdx = d => cats.findIndex((c, i) => i > 0 && c >= d);
  const segs = [["Бек, " + D.back_span + " дн.", CB, 0, HAS_FWD ? firstIdx(D.dates[FS]) : (HAS_TEST ? firstIdx(D.dates[TS]) : -1)]];
  if (HAS_FWD) segs.push(["Форвард, " + D.fwd_days + " дн.", CF, firstIdx(D.dates[FS]), HAS_TEST ? firstIdx(D.dates[TS]) : -1]);
  if (HAS_TEST) segs.push(["Тест, " + D.test_days + " дн.", CT, firstIdx(D.dates[TS]), -1]);
  segs.forEach(([label, color, a, b], k) => {
    const x0 = k === 0 ? -0.5 : a - 0.5, x1 = b < 0 ? cats.length - 0.5 : b - 0.5;
    if (k > 0 && a <= 0) return;
    if (k > 0) {
      shapes.push({ type: "rect", xref: "x", yref: "paper", x0: x0, x1: x1, y0: 0, y1: 1, fillcolor: color === CF ? "rgba(41,128,185,0.08)" : "rgba(142,68,173,0.09)", line: { width: 0 }, layer: "below" });
      shapes.push({ type: "line", xref: "x", yref: "paper", x0: x0, x1: x0, y0: 0, y1: 1, line: { color: color, width: 1, dash: "dot" } });
    }
    annotations.push({ xref: "x", yref: "paper", x: (x0 + x1) / 2, y: 1, xanchor: "center", yanchor: "top", showarrow: false, font: { size: 12, color: color }, text: `<b>${label}</b>` });
  });
  Plotly.react(div, traces, {
    title: { text: `<b>${title || "Бэктест портфелей: бек / форвард / тест"}, Лот $${D.lot}, Баланс $${D.start_balance}</b>`, x: 0.02, xanchor: "left" },
    template: "plotly_white", paper_bgcolor: "#fff", plot_bgcolor: "#fff", margin: { t: 50, r: 230, b: 90, l: 60 },
    xaxis: { title: "Дата", type: "linear", tickangle: -45, fixedrange: true, tickmode: "array", tickvals: tv, ticktext: tt, range: [-0.5, Math.max(cats.length - 1, 1) + 0.5], zeroline: false },
    yaxis: { title: "Баланс в $", fixedrange: true }, dragmode: false, shapes: shapes, annotations: annotations, height: 420, legend: { orientation: "v", x: 1.02, xanchor: "left", y: 1, yanchor: "top" },
  }, { responsive: true, displayModeBar: false, scrollZoom: false });
}
const fmtPnl = v => { const r = Math.round(v * 100) / 100; return (r >= 0 ? "+" : "-") + "$" + Math.abs(r).toFixed(2); };

// ---- yaml по текущему выбору ----
function yamlText(strategy, rows, rule) {
  const name = strategy === "forecast" ? "weather_forecast" : "max_bet";
  const rs = rows.filter(r => r.strategy === strategy);
  if (!rs.length) return null;
  const head = [`# watched_icaos_${name}.yaml -- сгенерировано backtest_v2, ${D.now}`, `# ${rule}`];
  const defaults = strategy === "forecast"
    ? ["defaults:", `  weather_source: "${D.wsrc}"`, "  weather_corr:", "  min_price_limit:", `  max_price_limit: ${D.max_price}`, '  comment: ""']
    : ["defaults:", "  min_price_limit:", `  max_price_limit: ${D.max_price}`, '  comment: ""'];
  const out = rs.map(r => {
    const lines = [`  ${r.icao}:`, `    city: "${r.city}"`, `    strategy: "${r.k ? "pyramid" : "flat"}"`];
    if (r.k) lines.push(`    loss_limit: ${r.k}`);
    if (strategy === "forecast") lines.push(`    weather_source: "${D.wsrc}"`, `    weather_corr:${r.corr ? " " + r.corr : ""}`);
    lines.push(`    min_price_limit: ${r.m}`, '    comment: ""');
    const other = strategy === "forecast" ? (r.cand.max ? r.cand.max.b.p30 : null) : (r.cand.forecast ? r.cand.forecast.b.p30 : null);
    const testNote = (HAS_FWD ? `, форвард ${money(r.fwd.profit)} (${r.fwd.n} сд.)` : "") + (HAS_TEST ? `, тест ${money(r.test.profit)} (${r.test.n} сд.)` : "");
    return lines.join("\n") + `  # бек за 30 дн. ${money(r.b.p30)} (другая стратегия: ${other === null ? "нет" : money(other)}), сделок ${r.b.n}${testNote}`;
  });
  return [head.concat([""], defaults, ["", "watched_icaos:"], out).join("\n") + "\n", name];
}

function panel(title, color, n, s) {
    if (!s.n) return `<div class="pcol" style="border-top:3px solid ${color}"><div class="mhead">${title}</div><span class="hint">нет сделок</span></div>`;
    const pct = v => (v >= 0 ? "+" : "") + (v / D.start_balance * 100).toFixed(1) + "%";
    return `<div class="pcol" style="border-top:3px solid ${color}"><div class="mhead">${title}</div><div class="prow">` +
      `<span>Городов: <b>${n}</b></span><span>Сделок: <b>${s.n}</b></span><span>Баланс: <b>$${s.bal.toFixed(2)}</b></span>` +
      `<span>P&amp;L: <b class="${cls(s.profit)}">${fmt(s.profit)} (${pct(s.profit)})</b></span>` +
      `<span>Профит / 30 дн.: <b class="${cls(s.p30)}">${fmt(s.p30)}</b></span>` +
      `<span>Макс. просадка: <b>$${s.dd.toFixed(2)} (${s.ddPct.toFixed(1)}%)</b></span>` +
      `<span>Нагрузка на депозит: <b>$${s.load.toFixed(2)} (${(s.load / D.start_balance * 100).toFixed(1)}%)</b></span></div></div>`;
}


// ---- просмотр одного города: график и все 30 вариантов ----
let CITY_SEL = null, CITY_VAR = null;
function renderCity(sel) {
  const names = D.cities.map(c => c.city);
  if (!names.length) return;
  const selEl = el("citySel");
  if (!CITY_SEL || !names.includes(CITY_SEL)) CITY_SEL = (sel.rows[0] && sel.rows[0].city) || names[0];
  const inRows = {}; sel.rows.forEach(r => { inRows[r.city] = true; });
  selEl.innerHTML = names.map(n => `<option value="${n}"${n === CITY_SEL ? " selected" : ""}>${n}${inRows[n] ? "" : " (не выбран)"}</option>`).join("");
  const c = D.cities.find(x => x.city === CITY_SEL), ev = evalCity(c), row = sel.rows.find(r => r.city === CITY_SEL);
  const key = v => `${v.s}|${v.k}|${v.m}`;
  let shown = CITY_VAR && ev.find(v => key(v) === CITY_VAR);
  if (!shown) shown = row ? ev.find(v => v.s === row.strategy && v.k === row.k && v.m === row.m) : better(ev.filter(v => v.enough)) || better(ev);
  const status = v => D.fixed ? (key(v) === (row && `${row.strategy}|${row.k}|${row.m}`) ? "<b>в конфиге</b>" : "") : key(v) === (row && `${row.strategy}|${row.k}|${row.m}`) ? "<b>выбран</b>" : !v.allowed ? "отключён галочкой перебора" : !v.enough ? "мало сделок" : v.ok ? "допущен" : "не прошёл: " + v.fb.map(i => "№" + (i + 1)).concat(v.ff.map(i => "№" + (i + 1) + "ф")).join(", ");
  const head = row ? `${D.fixed ? "Вариант из конфига" : "Выбран вариант"}: <b>${SNAME[row.strategy]}, ${KNAME(row.k)}, ${MPN(row.m)}</b>${row.strategy === "forecast" ? ", поправка " + (row.corr > 0 ? "+" + row.corr : row.corr) : ""}.` : (D.fixed ? "Города нет в конфиге." : "Город не выбран: ни один разрешённый вариант не прошёл фильтры или мало сделок.");
  el("cityInfo").innerHTML = `<p class="hint">${head} Ниже показан вариант <b>${SNAME[shown.s]}, ${KNAME(shown.k)}, ${MPN(shown.m)}</b> (клик по строке таблицы меняет вариант).</p>`;
  const pf = portfolio([[CITY_SEL, shown.t]]);
  el("citySum").innerHTML = `<div class="pwrap">${panel(`Бек — ${D.back_span} дн.`, CB, 1, pf.back)}` + (HAS_FWD ? panel(`Форвард — ${D.fwd_days} дн.`, CF, 1, pf.fwd) : "") + (HAS_TEST ? panel(`Тест — ${D.test_days} дн.`, CT, 1, pf.test) : "") + "</div>";
  const one = {}; one[`${CITY_SEL}: ${SNAME[shown.s]}, ${KNAME(shown.k)}, ${MPN(shown.m)}`] = pf;
  renderChart(one, "citychart", `${CITY_SEL}`);
  const sorted = ev.slice().sort((x, y) => y.b.p30 - x.b.p30);
  let t = "<table class='t vt'><tr><th>Стратегия</th><th>Тип</th><th>Мин. цена</th><th>Бек: профит / 30 дн.</th><th>Бек: сделок</th>" + (HAS_FWD ? "<th class='tb'>Форвард: профит</th>" : "") + (HAS_TEST ? "<th class='tb'>Тест: профит</th>" : "") + "<th>Статус</th></tr>";
  sorted.forEach(v => {
    const f = stat(v.t, FS, TS), te = stat(v.t, TS, NDAYS);
    t += `<tr data-var="${key(v)}"${key(v) === key(shown) ? " class='cur'" : ""}><td>${SNAME[v.s]}</td><td>${KNAME(v.k)}</td><td>${MPN(v.m)}</td><td class="${cls(v.b.p30)}">${fmt(v.b.p30)}</td><td>${v.b.n}</td>` +
      (HAS_FWD ? `<td class="tb ${cls(f.profit)}">${f.n ? fmt(f.profit) : "–"}</td>` : "") + (HAS_TEST ? `<td class="tb ${cls(te.profit)}">${te.n ? fmt(te.profit) : "–"}</td>` : "") + `<td>${status(v)}</td></tr>`;
  });
  el("cityVars").innerHTML = t + "</table>";
}
el("citySel").addEventListener("change", ev => { CITY_SEL = ev.target.value; CITY_VAR = null; render(); });
el("cityVars").addEventListener("click", ev => { const tr = ev.target.closest ? ev.target.closest("tr") : null; if (tr && tr.dataset && tr.dataset.var) { CITY_VAR = tr.dataset.var; render(); } });
el("cities").addEventListener("click", ev => { const tr = ev.target.closest ? ev.target.closest("tr") : null; if (tr && tr.dataset && tr.dataset.city) { CITY_SEL = tr.dataset.city; CITY_VAR = null; render(); el("cityTop").scrollIntoView && el("cityTop").scrollIntoView(); } });

let YAMLS = {};
function render() {
  const sel = select();
  const pfs = {};
  Object.keys(sel.pf).forEach(n => { pfs[n] = portfolio(sel.pf[n]); });

  // сводка по портфелям: панели Бек (зелёная) и Форвард (синяя)
  let html = "";
  Object.keys(pfs).forEach(n => {
    const p = pfs[n];
    if (!p.n) return;
    html += `<h3 style="color:${COL[n]};margin:14px 0 2px">${n}</h3><div class="pwrap">${panel(`Бек — ${D.back_span} дн.`, CB, p.n, p.back)}` +
      (HAS_FWD ? panel(`Форвард — ${D.fwd_days} дн.`, CF, p.n, p.fwd) : "") + (HAS_TEST ? panel(`Тест — ${D.test_days} дн.`, CT, p.n, p.test) : "") + `</div>`;
  });
  el("sum").innerHTML = html;
  renderChart(pfs);

  // таблица городов
  const nFc = sel.rows.filter(r => r.strategy === "forecast").length, nMx = sel.rows.length - nFc;
  el("title").textContent = (D.fixed ? "Заданный конфиг" : "Правило выбора стратегии по городам") + ` (Прогноз ${nFc} / Макс ${nMx})`;
  let ct = "<table class='t'><tr><th>Город</th><th>ICAO</th><th>Стратегия</th><th>Тип</th><th>Мин. цена</th><th>Поправка</th><th>Бек: профит / 30 дн.</th><th>Прогноз</th><th>Макс</th><th>Бек: сделок</th><th>Попаданий, %</th>" +
    (HAS_FWD ? "<th class='tb'>Форвард: профит</th><th>сделок</th>" : "") + (HAS_TEST ? "<th class='tb'>Тест: профит</th><th>сделок</th>" : "") + "</tr>";
  sel.rows.forEach(r => {
    const f = r.cand.forecast, m = r.cand.max;
    ct += `<tr data-city="${r.city}" style="cursor:pointer"><td>${r.city}</td><td>${r.icao}</td><td><b>${SNAME[r.strategy]}</b></td><td>${KNAME(r.k)}</td><td>${MPN(r.m)}</td><td>${r.strategy === "forecast" ? (r.corr > 0 ? "+" + r.corr : r.corr) : "–"}</td>` +
      `<td class="${cls(r.b.p30)}">${fmt(r.b.p30)}</td><td class="${f ? cls(f.b.p30) : ""}">${f ? fmt(f.b.p30) : "–"}${f && !f.ok && !D.fixed ? "*" : ""}</td><td class="${m ? cls(m.b.p30) : ""}">${m ? fmt(m.b.p30) : "–"}${m && !m.ok && !D.fixed ? "*" : ""}</td>` +
      `<td>${r.b.n}</td><td>${r.b.hit === null ? "–" : r.b.hit.toFixed(1)}</td>` +
      (HAS_FWD ? `<td class="tb ${cls(r.fwd.profit)}">${r.fwd.n ? fmt(r.fwd.profit) : "–"}</td><td>${r.fwd.n}</td>` : "") + (HAS_TEST ? `<td class="tb ${cls(r.test.profit)}">${r.test.n ? fmt(r.test.profit) : "–"}</td><td>${r.test.n}</td>` : "") + `</tr>`;
  });
  el("cities").innerHTML = ct + `</table><p class="hint">${D.fixed ? "Вариант города взят из заданного конфига." : "Прогноз/Макс -- лучший вариант этой стратегии (тип и цена подбираются), * -- ни один вариант не прошёл фильтры или мало сделок (на беке меньше " + D.min_trades + ")."} «Форвард/Тест: профит» -- результат выбранной стратегии за период.</p>`;
  let extra = "";
  if (sel.excluded.length) extra += `<p class='hint'>Не прошли фильтры (${sel.excluded.length}; в скобках номера нарушенных условий, «ф» -- на форварде): ` + sel.excluded.map(e => `${e.city} (${e.why})`).join("; ") + ".</p>";
  if (sel.skipped.length) extra += `<p class='hint'>Города без кандидатов (мало сделок, ${sel.skipped.length}): ` + sel.skipped.map(s => `${s.city} (макс. сделок на беке: ${s.n})`).join("; ") + ".</p>";
  el("skipped").innerHTML = extra;

  // yaml
  const rule = `правило backtest_v2: стратегия по городу -- по профиту на беке (${D.back_span} дн.); тип ставки и мин. цена подбираются по городу на беке; поправка прогноза по моде ошибок на беке` +
    "; фильтры: " + (checkedBits().map(i => i + 1).join(",") || "нет") + (el("cbFwd").checked ? " (и на форварде)" : " (на беке)");
  YAMLS = {};
  ["forecast", "max"].forEach(s => { const y = yamlText(s, sel.rows, rule); if (y) YAMLS[`watched_icaos_${y[1]}.yaml`] = y[0]; });
  const names = Object.keys(YAMLS);
  el("yaml").innerHTML = names.length ? names.map((name, i) => `<div class="yf"><div><b>${name}</b> ` +
    `<button data-act="copy" data-i="${i}">Копировать</button> <button data-act="save" data-i="${i}">Скачать</button></div>` +
    `<textarea readonly rows="${Math.min(30, YAMLS[name].split("\n").length + 1)}">${esc(YAMLS[name])}</textarea></div>`).join("") : "<p class='hint'>Нет городов по выбранным фильтрам.</p>";
  renderCity(sel);
  window.lastResult = { sel: sel, pfs: pfs };
}

el("yaml").addEventListener("click", ev => {
  const t = ev.target; if (!t.dataset || !t.dataset.act) return;
  const names = Object.keys(YAMLS), i = Number(t.dataset.i), txt = YAMLS[names[i]];
  if (t.dataset.act === "copy") { navigator.clipboard && navigator.clipboard.writeText(txt); t.textContent = "Скопировано"; }
  else { const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([txt], { type: "text/yaml" })); a.download = names[i]; a.click(); }
});
const boxes = document.querySelectorAll(".flt"), master = el("fltMaster");
master.addEventListener("change", () => { boxes.forEach(b => { b.checked = master.checked; }); render(); });
boxes.forEach(b => b.addEventListener("change", () => { master.checked = Array.from(boxes).every(x => x.checked); render(); }));
el("cbFwd").addEventListener("change", render);
document.querySelectorAll(".kbox,.mbox").forEach(b => b.addEventListener("change", render));
render();
"""
CSS = """body{font-family:sans-serif;margin:16px;max-width:1400px}h2{margin:28px 0 6px}.hint{color:#666;font-size:14px}.t{border-collapse:collapse;font-size:13px;margin:6px 0}
.t td,.t th{border-bottom:1px solid #eee;padding:3px 14px 3px 0;text-align:left}.g{color:#1e7e34}.r{color:#c0392b}.tb{border-left:2px solid #2980b9;padding-left:10px!important}
textarea{width:100%;font-family:monospace;font-size:12px}.yf{margin:10px 0}.pwrap{display:flex;flex-wrap:wrap;gap:12px 28px;margin:6px 0}.pcol{flex:1 1 380px;min-width:300px}.mhead{font-weight:bold;margin:4px 0 2px}.prow{display:flex;flex-wrap:wrap;gap:4px 22px;font-size:14px}button{margin-left:6px}.vt tr{cursor:pointer}.vt tr.cur{background:#eef5fb}#cities tr:hover td{background:#f6f8fa}
.filters{display:flex;flex-wrap:wrap;gap:6px 22px;padding:8px 12px;background:#f6f8fa;border:1px solid #e1e4e8;border-radius:6px}.filters label{cursor:pointer;font-size:14px}.filters .ft{flex:1 0 100%;font-size:13px;color:#555}.filters .fw{flex:1 0 100%;margin-top:4px;padding-top:6px;border-top:1px solid #e1e4e8}.filters input{margin-right:6px}"""
test_note = (f"Бек -- {BACK_SPAN} дн. ({FIRST.date()} … {(FWD_START - pd.Timedelta(days=1)).date()}); "
             + (f"форвард -- {FWD_DAYS} дн. ({FWD_START.date()} … {(TEST_START - pd.Timedelta(days=1)).date()}); " if FWD_DAYS else "")
             + (f"тест -- последние {TEST_DAYS} дн. ({TEST_START.date()} … {LAST.date()}). " if TEST_DAYS else "тест отключён. "))
note = (f"<p class='hint'>Слот {slot}. {test_note}Лот ${LOT}, комиссия ${COMMISSION}, потолок цены {MAX_PRICE}¢. "
        f"Правило: стратегия по городу выбирается по профиту на беке; для города перебираются стратегия (Прогноз/Макс), тип ставки (плоская, пирамида 2, пирамида 3) и мин. цена ({', '.join(str(x) for x in MIN_PRICES)}¢) -- {len(MIN_PRICES) * len(TYPES) * 2} вариантов; "
        f"поправка прогноза -- по самой частой ошибке прогноза на беке города (меняется с 0, только если соседняя корзина обгоняет нулевую минимум на {G_MARGIN:.0%} дней). "
        f"Перебор 30 вариантов на город сильно завышает бек (walk-forward проверка: факт 30–55% от бека), поэтому смотрите на тест.</p>"
        f"<p class='hint'><b>Как читать.</b> Бек -- данные, по которым выбрана стратегия, поэтому он завышен (в walk-forward проверке факт составил примерно 30–55% от бека). "
        f"Форвард -- дни после бека: на них можно (галочкой) проверять фильтры. Тест -- последние {TEST_DAYS} дн., ни выбор, ни фильтры, ни поправка прогноза его не видят: это единственная честная проверка, но один короткий отрезок шумный, и прибыль/просадку на нём не стоит принимать за прогноз. "
        f"Нагрузка на депозит -- максимальные суточные затраты всех позиций без общих лимитов бота. Галочки пересчитывают выбор, таблицу, график и yaml на лету; на беке всегда нужно не меньше {MIN_TRADES} сделок.</p>")
VARIANTS = ("<div class='filters'><div class='ft'>Перебор вариантов на город (разрешённые для выбора; лучший по профиту на беке среди прошедших фильтры)</div>"
            "<span>Тип ставки:</span>" + "".join(f"<label><input type='checkbox' class='kbox' data-v='{k}' checked> {'плоская' if k == 0 else 'пирамида ' + str(k)}</label>" for _, k in TYPES)
            + "<span>Мин. цена:</span>" + "".join(f"<label><input type='checkbox' class='mbox' data-v='{m}' checked> {m}¢</label>" for m in MIN_PRICES) + "</div>")
FILTERS = ("<div class='filters'><div class='ft'>Условия прохождения (отмеченные должны выполняться), как в city_rating.html. Стратегия города -- лучшая по профиту на беке среди прошедших.</div>"
           "<label><input type='checkbox' id='fltMaster' checked> <b>Все фильтры</b></label>"
           + "".join(f"<label><input type='checkbox' class='flt' data-bit='{i}' checked> {t}</label>" for i, t in enumerate(FILTER_LABELS))
           + "<label class='fw'><input type='checkbox' id='cbFwd'> <b>Применять фильтры на форварде</b> (вариант должен пройти их и на форварде, там не меньше "
           + f"{MIN_TRADES_FWD} сделок; форвард тогда участвует в выборе; тест фильтры не видят)</label></div>")
if FIXED:
    note = (f"<p class='hint'>Слот {slot}. {test_note}Лот ${LOT}, комиссия ${COMMISSION}, потолок цены {MAX_PRICE}¢. "
            f"Конфиг задан вручную (стратегия, пирамида, мин. цена и поправка по каждому городу), подбора по данным здесь нет: считается, как бы он торговал на всей истории. "
            f"Периоды бек / форвард / тест -- просто срезы истории; конфиг выбирался по прежним отчётам на этих же днях, поэтому тест здесь НЕ честная проверка вне выборки. "
            f"Серия пирамиды идёт непрерывно через границы периодов; нагрузка -- максимальные суточные затраты без общих лимитов бота. "
            f"В блоке «Один город» ниже таблицы показаны и все альтернативные варианты города для сравнения.</p>")
HIDE = "<div style='display:none'>" if FIXED else "<div>"
with open(OUT_HTML, "w", encoding="utf-8") as fh:
    fh.write(f"<html><head><meta charset='utf-8'><title>{'Конфиг по городам' if FIXED else 'Рейтинг городов v2'}</title><script src='https://cdn.plot.ly/plotly-2.35.2.min.js'></script><style>{CSS}</style></head><body>")
    fh.write("<h1 id='title'>Правило выбора стратегии по городам</h1>" + note)
    fh.write(HIDE + "<h2>Перебор</h2>" + VARIANTS + "<h2>Фильтры</h2>" + FILTERS + "</div>")
    fh.write("<h2>Портфель</h2><div id='sum'></div><div id='chart'><div id='pfchart' style='height:420px'></div></div><h2>Города</h2><p class='hint'>Клик по строке города открывает его график ниже.</p><div id='cities'></div><div id='skipped'></div><h2 id='cityTop'>Один город</h2><div class='filters'><label>Город: <select id='citySel'></select></label></div><div id='cityInfo'></div><div id='citySum'></div><div id='citychart' style='height:420px'></div><div id='cityVars'></div>" + HIDE + "<h2>yaml для бота</h2><div id='yaml'></div></div>")
    fh.write("<script>" + PAGE.replace("__DATA__", json.dumps(DATA, ensure_ascii=False)) + "</script></body></html>")
print("Готово:", os.path.join(ROOT, OUT_HTML))
