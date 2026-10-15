# ===== Проверка подбора гиперпараметров скользящим форвардом (walk-forward) =====
# Запуск: python walk_forward_check.py (скрипт лежит в корне проекта рядом с config/ и src/).
#
# Идея. Для каждой даты d ("якоря") делаем подбор так же, как в adhoc.py: окно 45 дней, заканчивающееся в d
# (бек 30 дней + форвард 15 дней). Потом смотрим, как выбранные варианты торговали на следующих EVAL_DAYS днях,
# которых подбор НЕ видел. Повторяем для многих d и сравниваем способы выбора:
#   A  "Профит за окно"        : окно 45 дней целиком, фильтры и ср. профит за 30 дней по всему окну (старый способ).
#   B  "Худший из бека/форварда": фильтры на обоих периодах, критерий = min(профит бека, профит форварда) (новый способ).
#   C  "Без подбора"           : фиксированный вариант (FIXED_*) для всех городов, без фильтров -- контрольная группа.
import os, sys, math, json
import numpy as np, pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
from src.config import Config
from src.cities import CITIES
from src.accuracy_report import AccuracyReportBuilder
from src.pyramid import pyramid_factor

# ---------------- настройки (те же, что в adhoc.py) ----------------
SLOT          = None
SOURCE        = "wunderground"
CORRECTIONS   = [-1, 0, 1]
MIN_PRICES    = [10, 20, 30, 35, 40]
TYPES         = [("flat", 0), ("pyramid", 2), ("pyramid", 3)]
MAX_PRICE     = 66
LOT, COMMISSION, START_BALANCE = 5.0, 0.012, 100.0
WINDOW_DAYS   = 14
FLAT_STREAK_LEN = 3
BACK_DAYS, FWD_DAYS = 30, 15
# Можно переопределить из командной строки: python walk_forward_check.py 20 10 5  (бек, форвард, дней проверки)
_EVAL_ARG = None
if len(sys.argv) >= 3:
    BACK_DAYS, FWD_DAYS = int(sys.argv[1]), int(sys.argv[2])
    if len(sys.argv) >= 4:
        _EVAL_ARG = int(sys.argv[3])
# минимум сделок масштабируется от базовых 8 (30 дн.) и 4 (15 дн.)
MIN_TRADES_BACK = max(3, round(8 * BACK_DAYS / 30))
MIN_TRADES_FWD = max(2, round(4 * FWD_DAYS / 15))
FILTER_GIVEBACK, FILTER_DD_PCT, FILTER_RISK_SHARE = 0.5, 15, 0.5
FILTER_GAP_DAYS, FILTER_MAX_STREAK, FILTER_MAX_PYR_STEPS = 7, 5, 2
# ---------------- настройки проверки ----------------
EVAL_DAYS     = _EVAL_ARG or 7                # сколько дней после якоря смотрим работу выбранных вариантов
ANCHOR_STEP   = 1                # шаг между якорями, дни (1 -- окна проверки перекрываются, см. примечание в отчёте)
FIXED         = dict(strategy="forecast", corr=0, min_price=30, kind="flat", steps=0)   # контрольная группа C
OUT_HTML      = "walk_forward_check.html"

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


# ---------------- метрики периода (как в adhoc.py) ----------------
def seg_metrics(sub, seg_start, seg_end, kind, steps, pyr):
    span_days = max(1, (seg_end - seg_start).days + 1)
    balance = peak = START_BALANCE
    max_dd, streaks, cur_len, prev_date = 0.0, [], 0, None
    for date, hit, pc, cost, pnl in sub:
        balance += pnl
        peak = max(peak, balance)
        max_dd = max(max_dd, peak - balance)
        if hit:
            if cur_len:
                streaks.append((cur_len, prev_date))
            cur_len = 0
        else:
            cur_len += 1
        prev_date = date
    if cur_len:
        streaks.append((cur_len, prev_date))
    max_price = max(r[2] for r in sub) / 100
    if kind == "pyramid":
        risk = sum(floor_lot(LOT * pyramid_factor(k, pyr["progression"], pyr["multiplier"], pyr["steps"]), max_price)
                   * (max_price + COMMISSION) for k in range(steps))
    else:
        risk = floor_lot(LOT, max_price) * (max_price + COMMISSION)
    dts = pd.to_datetime([r[0] for r in sub])
    cal = pd.date_range(seg_start, seg_end)
    per_day = pd.Series(1, index=dts).groupby(level=0).sum().reindex(cal, fill_value=0)
    gap, max_gap = 0, 0
    for t in (per_day > 0).tolist():
        gap = 0 if t else gap + 1
        max_gap = max(max_gap, gap)
    profit_total = balance - START_BALANCE
    profit30 = profit_total / span_days * 30
    gain_peak = peak - START_BALANCE
    max_streak = max([x[0] for x in streaks], default=0)
    flags = [
        gain_peak > 0 and (peak - balance) > FILTER_GIVEBACK * gain_peak,
        max_dd > START_BALANCE * FILTER_DD_PCT / 100,
        max_dd > profit30,
        risk > FILTER_RISK_SHARE * profit30,
        max_gap >= FILTER_GAP_DAYS,
        max_streak > FILTER_MAX_STREAK,
        profit_total < 0,
        kind == "pyramid" and steps > FILTER_MAX_PYR_STEPS,
    ]
    return profit30, any(flags)


def simulate(rows, strategy, corr, min_price, kind, steps, anchor, e_end):
    """rows: данные города от начала окна до e_end. Торговля непрерывная; выбор по данным до anchor,
    результат проверки -- сделки в (anchor, e_end]. Возвращает None, если на беке/форварде мало сделок."""
    pyr = PYR[strategy]
    recs, miss = [], 0
    first_date = pd.Timestamp(rows[0][0])
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
    back_start = anchor - pd.Timedelta(days=BACK_DAYS + FWD_DAYS - 1)
    fwd_start = anchor - pd.Timedelta(days=FWD_DAYS - 1)
    back_end = fwd_start - pd.Timedelta(days=1)
    ts = [pd.Timestamp(r[0]) for r in recs]
    sub_b = [r for r, t in zip(recs, ts) if back_start <= t <= back_end]
    sub_f = [r for r, t in zip(recs, ts) if fwd_start <= t <= anchor]
    if len(sub_b) < MIN_TRADES_BACK or len(sub_f) < MIN_TRADES_FWD or first_date > back_end:
        return None
    p_b, bad_b = seg_metrics(sub_b, max(back_start, first_date), back_end, kind, steps, pyr)
    p_f, bad_f = seg_metrics(sub_f, fwd_start, anchor, kind, steps, pyr)
    p_w, bad_w = seg_metrics(sub_b + sub_f, max(back_start, first_date), anchor, kind, steps, pyr)
    ev = [(r[0], r[4], r[3]) for r, t in zip(recs, ts) if anchor < t <= e_end]
    return dict(key_b=min(p_b, p_f), bad_b=bad_b or bad_f, key_a=p_w, bad_a=bad_w, p_b=p_b, p_f=p_f, ev=ev, ntr=len(sub_b) + len(sub_f))


# ---------------- якоря ----------------
all_rows = {}                                  # (strategy, icao) -> (city, rows)
for strategy, src_df in (("forecast", df_fc), ("max", df_mx)):
    for (icao, city), g in src_df.groupby(["icao", "city"]):
        g = g.sort_values("date").drop_duplicates("date", keep="last")
        all_rows[(strategy, icao)] = (city, list(zip(g["date"], g["icao"], g["error"], g["forecast_bucket"].astype(int))))
dates_all = pd.to_datetime([r[0] for _, rows in all_rows.values() for r in rows])
FIRST, LAST = dates_all.min(), dates_all.max()
win = BACK_DAYS + FWD_DAYS
anchors = list(pd.date_range(FIRST + pd.Timedelta(days=win - 1), LAST - pd.Timedelta(days=EVAL_DAYS), freq=f"{ANCHOR_STEP}D"))
print(f"История: {FIRST.date()} .. {LAST.date()} ({(LAST - FIRST).days + 1} дн.), окно подбора {win} дн., проверка {EVAL_DAYS} дн., якорей: {len(anchors)}")
if len(anchors) < 3:
    raise SystemExit("Слишком мало якорей: запустите с меньшими окнами, например: python walk_forward_check.py 20 10 5")

# ---------------- прогон ----------------
METHODS = {"A": "A. Профит за окно (старый)", "B": "B. Худший из бека и форварда (новый)", "C": "C. Без подбора (фиксированный)",
           "E": "E. Сжатие оценок к общему среднему",            "G": "G. Поправка по моде ошибок + мин. цена/тип",
           "H": "H. Поправка по моде + пирамида 2 + мин. цена", "I": "I. Поправка по моде + пирамида 2 + 30¢ (контроль)",
           "J": "J. Как I, но flat (эффект пирамиды)", "K": "K. Как I, но поправка 0 (эффект поправки)"}
G_MARGIN = 0.10                                # поправка меняется с 0, только если соседняя корзина обгоняет нулевую на столько долей дней
G_MIN_DAYS = 20                                # минимум дней истории ошибок для смены поправки
RULE_STRATEGY = "forecast"                     # стратегия для правил I/J/K и таблицы чувствительности: фиксирована, без выбора между Прогноз и Макс
SHRINK_K = 10                                  # вес собственных сделок города в сжатии: w = n / (n + K)
STRAT_MP = [30, 40]
for _mp in STRAT_MP:
    METHODS[f"S{_mp}"] = f"S{_mp}. Стратегия по городу (профит окна), {_mp}¢, пирамида 2"
    METHODS[f"T{_mp}"] = f"T{_mp}. Стратегия по городу (худший бек/форвард), {_mp}¢, пирамида 2"
    METHODS[f"X{_mp}"] = f"X{_mp}. Как S{_mp}, но без городов с убытком на обеих стратегиях в окне подбора"
EXTRA = {}
for _mp in MIN_PRICES:
    EXTRA[f"I{_mp}"] = f"I, мин. цена {_mp}¢ (мода + пирамида 2)"
    EXTRA[f"J{_mp}"] = f"J, мин. цена {_mp}¢ (мода + flat)"
for _mp in MIN_PRICES:
    EXTRA[f"M{_mp}"] = f"Макс, мин. цена {_mp}¢, пирамида 2"
    EXTRA[f"N{_mp}"] = f"Макс, мин. цена {_mp}¢, flat"
ALL = {**METHODS, **EXTRA}
picks = {m: {} for m in ALL}               # метод -> якорь -> {город: (конфиг, ожидание, факт, сделок)}
for ai, anchor in enumerate(anchors):
    e_end = anchor + pd.Timedelta(days=EVAL_DAYS)
    w_start = anchor - pd.Timedelta(days=win - 1)
    cand = {}                                  # город -> список вариантов
    corr_g = {}                                # город -> поправка по моде ошибок (только Прогноз)
    for (strategy, icao), (city, rows) in all_rows.items():
        rr = [r for r in rows if w_start <= pd.Timestamp(r[0]) <= e_end]
        if not rr or pd.Timestamp(rr[0][0]) > anchor:
            continue
        if strategy == "forecast":
            errs = [r[2] for r in rows if pd.Timestamp(r[0]) <= anchor]
            c0 = 0
            if len(errs) >= G_MIN_DAYS:
                cnt = {c: sum(1 for e in errs if e + c == 0) for c in CORRECTIONS}
                cb = max(CORRECTIONS, key=lambda c: cnt[c])
                if cb != 0 and (cnt[cb] - cnt[0]) / len(errs) >= G_MARGIN:
                    c0 = cb
            corr_g[city] = c0
        for corr in (CORRECTIONS if strategy == "forecast" else [0]):
            for mp in MIN_PRICES:
                for kind, steps in TYPES:
                    m = simulate(rr, strategy, corr, mp, kind, steps, anchor, e_end)
                    if m is None:
                        continue
                    m.update(g=(strategy, corr, kind, steps), mp=mp, label=f"{'Прогноз' if strategy == 'forecast' else 'Макс'}, попр. {corr}, мин. {mp}¢, {kind if kind == 'flat' else f'pyramid-{steps}'}",
                             fixed=(strategy == FIXED["strategy"] and corr == FIXED["corr"] and mp == FIXED["min_price"]
                                    and kind == FIXED["kind"] and steps == FIXED["steps"]))
                    cand.setdefault(city, []).append(m)
    scale = EVAL_DAYS / 30
    n_city = max(1, len(cand))
    # общие оценки по всем городам: по полному конфигу (label) и по конфигу без мин. цены (g)
    by_label, by_g, cov_label, cov_g = {}, {}, {}, {}
    mu_all = {}
    for city, lst in cand.items():
        for m in lst:
            mu_all.setdefault(m["label"], []).append(m["key_b"])
            if m["bad_b"]:                     # общий конфиг выбираем только среди вариантов, прошедших фильтры
                continue
            by_label.setdefault(m["label"], []).append(m["key_b"])
            by_g.setdefault(m["g"], []).append(m["key_b"])
            cov_label.setdefault(m["label"], set()).add(city)
            cov_g.setdefault(m["g"], set()).add(city)
    mu_label = {k: float(np.mean(v)) for k, v in mu_all.items()}
        # критерий общего конфига: сумма key_b по прошедшим фильтры городам / все города (не прошедший город даёт 0)
    g_best = max(by_g, key=lambda g: sum(by_g[g]) / n_city) if by_g else None
    l_best = max(by_label, key=lambda l: sum(by_label[l]) / n_city) if by_label else None
    for city, lst in cand.items():
        ok_a = [m for m in lst if not m["bad_a"]]
        ok_b = [m for m in lst if not m["bad_b"]]
        fx = [m for m in lst if m["fixed"]]
        pool_d = [m for m in ok_b if g_best is not None and m["g"] == g_best]
        pool_g = [m for m in ok_b if m["g"][0] == "max" or m["g"][1] == corr_g.get(city, 0)]
        pool_h = [m for m in pool_g if m["g"][2] == "pyramid" and m["g"][3] == 2]
        rs = [m for m in lst if m["g"][0] == RULE_STRATEGY and m["g"][1] == corr_g.get(city, 0)]   # поправка по моде, стратегия фиксирована
        pool_i = [m for m in rs if m["g"][2] == "pyramid" and m["g"][3] == 2 and m["mp"] == FIXED["min_price"]]
        pool_j = [m for m in rs if m["g"][2] == "flat" and m["mp"] == FIXED["min_price"]]
        pool_k = [m for m in lst if m["g"][0] == RULE_STRATEGY and m["g"][1] == 0
                  and m["g"][2] == "pyramid" and m["g"][3] == 2 and m["mp"] == FIXED["min_price"]]
        extra_pools = {}
        for _mp in MIN_PRICES:
            extra_pools[f"I{_mp}"] = [m for m in rs if m["g"][2] == "pyramid" and m["g"][3] == 2 and m["mp"] == _mp]
            extra_pools[f"J{_mp}"] = [m for m in rs if m["g"][2] == "flat" and m["mp"] == _mp]
            extra_pools[f"M{_mp}"] = [m for m in lst if m["g"][0] == "max" and m["g"][2] == "pyramid" and m["g"][3] == 2 and m["mp"] == _mp]
            extra_pools[f"N{_mp}"] = [m for m in lst if m["g"][0] == "max" and m["g"][2] == "flat" and m["mp"] == _mp]
        strat_pools = {}
        for _mp in STRAT_MP:
            sp = [m for m in rs if m["g"][2] == "pyramid" and m["g"][3] == 2 and m["mp"] == _mp] + \
                 [m for m in lst if m["g"][0] == "max" and m["g"][2] == "pyramid" and m["g"][3] == 2 and m["mp"] == _mp]
            strat_pools[f"S{_mp}"] = (sp, "key_a")
            strat_pools[f"T{_mp}"] = (sp, "key_b")
            strat_pools[f"X{_mp}"] = (sp, "key_a")
        pool_f = [m for m in ok_b if l_best is not None and m["label"] == l_best]
        def shrunk(m):
            w = m["ntr"] / (m["ntr"] + SHRINK_K)
            return w * m["key_b"] + (1 - w) * mu_label[m["label"]]
        for meth, pool, key in (("A", ok_a, lambda m: m["key_a"]), ("B", ok_b, lambda m: m["key_b"]), ("C", fx, lambda m: m["key_a"]),
                                ("D", pool_d, lambda m: m["key_b"]), ("E", ok_b, shrunk), ("F", pool_f, lambda m: m["key_b"]), ("G", pool_g, lambda m: m["key_b"]), ("H", pool_h, lambda m: m["key_b"]), ("I", pool_i, lambda m: m["key_a"]),
                                ("J", pool_j, lambda m: m["key_a"]), ("K", pool_k, lambda m: m["key_a"]),
                                *[(k, v, lambda m: m["key_a"]) for k, v in extra_pools.items()],
                                *[(k, v[0], (lambda kk: (lambda m: m[kk]))(v[1])) for k, v in strat_pools.items()]):
            if meth not in ALL or not pool:
                continue
            best = max(pool, key=key)
            if meth.startswith("X") and meth[1:].isdigit() and best["key_a"] <= 0:
                continue                                  # обе стратегии в минусе на окне подбора: город исключён
            picks[meth].setdefault(anchor, {})[city] = (best["label"], key(best) * scale, sum(e[1] for e in best["ev"]), best["ev"])
    print(f"  якорь {ai + 1}/{len(anchors)} {anchor.date()}: " + ", ".join(f"{k}={len(picks[k].get(anchor, {}))}" for k in METHODS), flush=True)

# ---------------- агрегаты ----------------
rows_out = []                                  # по якорям и методам
for meth in ALL:
    for anchor in anchors:
        sel = picks[meth].get(anchor, {})
        by_date = {}
        for city, (label, exp, real, ev) in sel.items():
            for d, pnl, cost in ev:
                x = by_date.setdefault(d, [0.0, 0.0]); x[0] += pnl; x[1] += cost
        bal = peak = dd = 0.0
        for d in sorted(by_date):
            bal += by_date[d][0]; peak = max(peak, bal); dd = max(dd, peak - bal)
        rows_out.append(dict(meth=meth, anchor=str(anchor.date()), n=len(sel), exp=sum(v[1] for v in sel.values()),
                             real=sum(v[2] for v in sel.values()), dd=dd, load=max([v[1] for v in by_date.values()], default=0.0),
                             trades=sum(len(v[3]) for v in sel.values()),
                             pos=sum(1 for v in sel.values() if v[2] > 0), withtr=sum(1 for v in sel.values() if v[3])))
R = pd.DataFrame(rows_out)


def summary(meth):
    d = R[(R.meth == meth) & (R.n > 0)]
    if d.empty:
        return None
    per_city = d.real.sum() / d.n.sum()
    return {"Якорей": int(len(d)), "Городов в среднем": round(d.n.mean(), 1), "Сделок за окно проверки (в среднем)": round(d.trades.mean(), 1),
            "Средний P&L окна проверки, $": round(d.real.mean(), 2), "Медиана P&L, $": round(d.real.median(), 2),
            "Средний P&L на город, $": round(per_city, 3), "Разброс P&L (станд. откл.), $": round(d.real.std(ddof=0), 2),
            "Доля якорей с прибылью, %": round((d.real > 0).mean() * 100, 1), "Худший якорь, $": round(d.real.min(), 2),
            "Средняя макс. просадка окна, $": round(d.dd.mean(), 2), "Средняя нагрузка на депозит, $": round(d.load.mean(), 2),
            "Доля городов с прибылью, %": round(d.pos.sum() / max(1, d.withtr.sum()) * 100, 1),
            "P&L на $100 нагрузки на депозит, $": round(d.real.sum() / max(1e-9, d.load.sum()) * 100, 2),
            "Ожидание по окну подбора, $": round(d.exp.mean(), 2), "Переоценка (ожидание − факт), $": round(d.exp.mean() - d.real.mean(), 2)}


SUM = {m: summary(m) for m in ALL}
common = R[R.n > 0].pivot(index="anchor", columns="meth", values="real")
paired = {}
for x, y in (("B", "A"), ("B", "C"), ("A", "C"), ("D", "B"), ("E", "B"), ("F", "B"), ("F", "C"), ("G", "B"), ("G", "E"), ("H", "G"), ("H", "B"), ("I", "H"), ("I", "C"), ("I", "J"), ("I", "K"), ("J", "C"),
                 ("S30", "I30"), ("S30", "M30"), ("S40", "I40"), ("S40", "M40"), ("T30", "S30"), ("T40", "S40"), ("X30", "S30"), ("X40", "S40")):
    if x in common and y in common and len(common[[x, y]].dropna()) > 2:
        d = (common[x] - common[y]).dropna()
        paired[f"{x} − {y}"] = dict(mean=float(d.mean()), se=float(d.std(ddof=1) / math.sqrt(len(d))), share=float((d > 0).mean() * 100), n=int(len(d)))

# ---------------- отчёт ----------------
anchor_s = [str(a.date()) for a in anchors]
series = {m: [float(R[(R.meth == m) & (R.anchor == a)].real.sum()) if len(R[(R.meth == m) & (R.anchor == a) & (R.n > 0)]) else None for a in anchor_s] for m in METHODS}
cities_n = {m: [int(R[(R.meth == m) & (R.anchor == a)].n.sum()) for a in anchor_s] for m in METHODS}
details = {}
for meth in METHODS:
    for anchor in anchors:
        sel = picks[meth].get(anchor, {})
        details[f"{meth}|{anchor.date()}"] = sorted(([c, v[0], round(v[1], 2), round(v[2], 2), len(v[3])] for c, v in sel.items()), key=lambda x: -x[3])
SENS = []
for k, name in EXTRA.items():
    sm = SUM.get(k)
    if sm:
        SENS.append(dict(name=name, n=sm["Городов в среднем"], pnl=sm["Средний P&L окна проверки, $"], std=sm["Разброс P&L (станд. откл.), $"],
                         worst=sm["Худший якорь, $"], dd=sm["Средняя макс. просадка окна, $"], load=sm["Средняя нагрузка на депозит, $"],
                         per100=sm["P&L на $100 нагрузки на депозит, $"], over_city=round(sm["Переоценка (ожидание − факт), $"] / max(0.1, sm["Городов в среднем"]), 2),
                         pos=sm["Доля якорей с прибылью, %"]))
for _mp in STRAT_MP:
    a = R[R.meth == f"I{_mp}"].set_index("anchor").real
    b2 = R[R.meth == f"M{_mp}"].set_index("anchor").real
    if len(a) and len(b2):
        SENS.append(dict(name=f"Ожидание при случайном выборе стратегии: среднее I{_mp} и Макс {_mp}¢", n="–", pnl=round(float(((a + b2) / 2).mean()), 2),
                         std="–", worst="–", dd="–", load="–", per100="–", over_city="–", pos="–"))
DATA = dict(sens=SENS, methods=METHODS, anchors=anchor_s, series=series, cities=cities_n, summary=SUM, paired=paired, details=details,
            eval_days=EVAL_DAYS, back=BACK_DAYS, fwd=FWD_DAYS, step=ANCHOR_STEP)
COL = {"A": "#e67e22", "B": "#2980b9", "C": "#95a5a6", "D": "#8e44ad", "E": "#16a085", "F": "#c0392b", "G": "#d35400", "H": "#2c3e50", "I": "#7f8c8d", "J": "#27ae60", "K": "#f39c12",
       "S30": "#1abc9c", "S40": "#e84393", "T30": "#00b894", "T40": "#fd79a8", "X30": "#6c5ce7", "X40": "#d63031"}

PAGE = r"""
const D = __DATA__, COL = __COL__;
const fmt = v => (v === null || v === undefined) ? "–" : ((v >= 0 ? "+" : "-") + "$" + Math.abs(v).toFixed(2));
const lay = (title, extra) => Object.assign({ title: { text: "<b>" + title + "</b>", x: 0.02, xanchor: "left" }, template: "plotly_white", height: 380,
  margin: { t: 50, r: 20, b: 80, l: 60 }, legend: { orientation: "h", y: -0.25 } }, extra || {});
const ids = Object.keys(D.methods);
Plotly.newPlot("c1", ids.map(m => ({ type: "bar", name: D.methods[m], x: D.anchors, y: D.series[m], marker: { color: COL[m] } })),
  lay(`P&L выбранных вариантов за ${D.eval_days} дн. после даты подбора (по датам подбора)`, { barmode: "group", yaxis: { title: "P&L, $" }, xaxis: { tickangle: -45 } }),
  { responsive: true, displayModeBar: false });
const names = ids.map(m => D.methods[m]);
Plotly.newPlot("c2", [
  { type: "bar", name: "Ожидание по окну подбора", x: names, y: ids.map(m => D.summary[m] ? D.summary[m]["Ожидание по окну подбора, $"] : null), marker: { color: "#bdc3c7" } },
  { type: "bar", name: "Факт на невидимых днях", x: names, y: ids.map(m => D.summary[m] ? D.summary[m]["Средний P&L окна проверки, $"] : null), marker: { color: "#27ae60" } },
], lay("Ожидание vs факт: средний P&L окна проверки, $ (разница = переоценка при подборе)", { barmode: "group", yaxis: { title: "$" } }), { responsive: true, displayModeBar: false });
Plotly.newPlot("c3", ids.map(m => ({ type: "box", name: D.methods[m], y: D.series[m].filter(v => v !== null), marker: { color: COL[m] }, boxpoints: "all", jitter: 0.4, pointpos: 0 })),
  lay("Разброс P&L по якорям, $", { showlegend: false, yaxis: { title: "P&L окна проверки, $" }, margin: { t: 50, r: 20, b: 120, l: 60 } }), { responsive: true, displayModeBar: false });
Plotly.newPlot("c4", ids.map(m => ({ type: "scatter", mode: "lines+markers", name: D.methods[m], x: D.anchors, y: D.cities[m], line: { color: COL[m] } })),
  lay("Сколько городов выбрано (прошло фильтры) по датам подбора", { yaxis: { title: "городов" }, xaxis: { tickangle: -45 } }), { responsive: true, displayModeBar: false });

const keys = Object.keys(D.summary[ids[0]] || D.summary[ids[1]]);
document.getElementById("sum").innerHTML = "<table class='t'><tr><th>Показатель</th>" + ids.map(m => `<th style="color:${COL[m]}">${D.methods[m]}</th>`).join("") + "</tr>" +
  keys.map(k => `<tr><td>${k}</td>` + ids.map(m => `<td>${D.summary[m] ? D.summary[m][k] : "–"}</td>`).join("") + "</tr>").join("") + "</table>";
document.getElementById("sens").innerHTML = "<table class='t'><tr><th>Вариант</th><th>Городов</th><th>P&L, $</th><th>Разброс</th><th>Худший якорь</th><th>Просадка</th><th>Нагрузка</th><th>P&L на $100 нагрузки</th><th>Переоценка на город</th><th>Якорей с прибылью, %</th></tr>" +
  D.sens.map(r => `<tr><td>${r.name}</td><td>${r.n}</td><td>${r.pnl}</td><td>${r.std}</td><td>${r.worst}</td><td>${r.dd}</td><td>${r.load}</td><td>${r.per100}</td><td>${r.over_city}</td><td>${r.pos}</td></tr>`).join("") + "</table>";
document.getElementById("paired").innerHTML = Object.keys(D.paired).map(k => { const p = D.paired[k];
  return `<li><b>${k}</b>: средняя разница P&L окна проверки ${fmt(p.mean)} ± ${p.se.toFixed(2)} (станд. ошибка), метод слева лучше на ${p.share.toFixed(0)}% якорей (n = ${p.n})</li>`; }).join("");
const sel = document.getElementById("anchorSel"), box = document.getElementById("det");
D.anchors.forEach(a => { const o = document.createElement("option"); o.value = a; o.textContent = a; sel.appendChild(o); });
sel.value = D.anchors[D.anchors.length - 1];
function showAnchor() {
  box.innerHTML = ids.map(m => { const rows = D.details[m + "|" + sel.value] || [];
    const tot = rows.reduce((a, r) => a + r[3], 0);
    return `<div class="col"><h4 style="color:${COL[m]}">${D.methods[m]} — городов ${rows.length}, итого ${fmt(tot)}</h4><table class="t"><tr><th>Город</th><th>Вариант</th><th>Ожидание</th><th>Факт</th><th>Сделок</th></tr>` +
      rows.map(r => `<tr><td>${r[0]}</td><td>${r[1]}</td><td>${fmt(r[2])}</td><td class="${r[3] >= 0 ? "g" : "r"}">${fmt(r[3])}</td><td>${r[4]}</td></tr>`).join("") + "</table></div>"; }).join("");
}
sel.addEventListener("change", showAnchor); showAnchor();
"""
CSS = """body{font-family:sans-serif;margin:16px;max-width:1500px}h2{margin:28px 0 6px}.hint{color:#666;font-size:14px}.t{border-collapse:collapse;font-size:13px;margin:6px 0}
.t td,.t th{border-bottom:1px solid #eee;padding:3px 14px 3px 0;text-align:left}.g{color:#1e7e34}.r{color:#c0392b}
#det{display:flex;flex-wrap:wrap;gap:12px 28px}.col{flex:1 1 440px}select{font-size:14px;padding:3px}"""
note = (f"<p class='hint'>Слот {slot}. Лот ${LOT}, комиссия ${COMMISSION}, потолок цены {MAX_PRICE}¢. История {FIRST.date()} … {LAST.date()}. "
        f"Для каждой даты подбора окно {win} дн. (бек {BACK_DAYS} + форвард {FWD_DAYS}); варианты выбираются как в adhoc.py и затем торгуют {EVAL_DAYS} дн., "
        f"которых подбор не видел (состояние пирамиды переходит из окна подбора в окно проверки, как в живой торговле). "
        f"Способы D, E, F уменьшают свободу подбора: D -- общий конфиг (поправка, тип) для всех городов, по городу выбирается только мин. цена; "
        f"E -- оценка города сжимается к среднему по всем городам для того же конфига (вес своих сделок n/(n+{SHRINK_K})); F -- один общий конфиг для всех городов (города, не прошедшие фильтры на нём, не торгуют). "
        f"G -- поправка города фиксируется по самой частой ошибке прогноза в истории до даты подбора (меняется с 0, только если соседняя корзина обгоняет нулевую минимум на {G_MARGIN:.0%} дней), подбираются только мин. цена и тип. "
        f"H -- то же, но тип зафиксирован (пирамида 2), подбирается только мин. цена; I -- контроль: поправка по моде, пирамида 2, мин. цена {FIXED['min_price']}¢, без фильтров и подбора. "
        f"J -- как I, но flat вместо пирамиды 2 (показывает вклад пирамиды); K -- как I, но поправка 0 вместо моды (показывает вклад поправки). "
        f"Строка 'P&L на $100 нагрузки' приводит способы к одинаковой нагрузке на депозит (в скрипте нет общего лимита депозита, города суммируются). "
        f"Главный показатель -- переоценка (ожидание минус факт): чем она меньше, тем лучше подбор переносится на живую торговлю. "
        f"Шаг дат подбора {ANCHOR_STEP} дн.: окна проверки соседних дат перекрываются, поэтому якоря не независимы, "
        f"эффективное число независимых проверок около {max(1, len(anchors) * ANCHOR_STEP // EVAL_DAYS)}. Станд. ошибка в парных сравнениях оптимистична.</p>")
with open(OUT_HTML, "w", encoding="utf-8") as fh:
    fh.write(f"<html><head><meta charset='utf-8'><title>Проверка подбора</title><script src='https://cdn.plot.ly/plotly-2.35.2.min.js'></script><style>{CSS}</style></head><body>")
    fh.write("<h1>Проверка подбора гиперпараметров: скользящий форвард</h1>" + note)
    fh.write("<h2>Итоги по способам выбора</h2><div id='sum'></div><h2>Парные сравнения по якорям</h2><ul id='paired'></ul>")
    fh.write("<h2>Чувствительность правил к минимальной цене (I, J -- Прогноз с поправкой по моде; Макс -- стратегия Макс)</h2><p class='hint'>Правило без подбора: стратегия Прогноз, поправка по моде, одна мин. цена для всех городов. I -- пирамида 2, J -- flat.</p><div id='sens'></div>")
    fh.write("<h2>Графики</h2><div id='c1'></div><div id='c2'></div><div id='c3'></div><div id='c4'></div>")
    fh.write("<h2>Что выбрано и как сработало</h2><p class='hint'>Дата подбора: <select id='anchorSel'></select> (ожидание = критерий × дней проверки / 30; факт = P&L за окно проверки)</p><div id='det'></div>")
    fh.write("<script>" + PAGE.replace("__DATA__", json.dumps(DATA, ensure_ascii=False)).replace("__COL__", json.dumps(COL)) + "</script></body></html>")
print("Готово:", os.path.join(ROOT, OUT_HTML))
for m in METHODS:
    print(METHODS[m], SUM[m])
for k, v in paired.items():
    print(k, v)
