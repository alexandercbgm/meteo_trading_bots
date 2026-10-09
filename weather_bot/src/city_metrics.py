"""
city_metrics.py

Общие функции для расчёта пер-городских метрик точности прогноза (попадания/
промахи, самая длинная череда промахов подряд, статистика цены ставки Yes) —
используются и в HTML-отчёте (accuracy_report.py, блок «Рейтинги городов»), и
при отборе городов по критериям (criteria.py), чтобы не дублировать логику
расчёта в двух местах.

Все функции работают с ПОСТРОЧНЫМ df из AccuracyReportBuilder.build_dataframe()
(колонки: date, icao, city, region, source, snapshot_slot, scale, error,
bet_price) либо с его "дневным" срезом (build_daily) — по одной строке на
(icao, city, region, source, date).
"""

import pandas as pd


def sortable_slot_key(v):
    """Ключ сортировки слотов: числа впереди (по значению), строки — следом
    (по алфавиту)."""
    if isinstance(v, (int, float)):
        return (0, v, "")
    return (1, 0, str(v))


def build_daily(df, slot_filter):
    """
    Схлопывает построчный df до ОДНОЙ строки на (icao, city, region, source,
    date) для заданного слота (конкретное значение snapshot_slot, например
    "12:00") — единица наблюдения для серий попаданий/промахов, процента
    попаданий и статистики цены ставки. Wunderground и Windy остаются
    отдельными, не смешиваемыми сериями (колонка source).
    """
    d = df[df["snapshot_slot"] == slot_filter]
    cols = ["icao", "city", "region", "source", "date", "date_ts", "error", "hit", "bet_price", "forecast_bucket", "era"]
    if d.empty:
        return pd.DataFrame(columns=cols)
    d = d.copy()
    daily = d.groupby(["icao", "city", "region", "source", "date"], as_index=False).last()
    daily["date_ts"] = pd.to_datetime(daily["date"])
    daily["hit"] = daily["error"] == 0
    return daily[cols]


def compute_period(daily, window_start, window_end):
    """
    (streak_df, hitrate_df, streak_len_df) для окна дат [window_start,
    window_end] по (icao, city, source):
    - streak_df / hitrate_df: одна строка на (icao, city, source) со значением
      "самая длинная череда промахов подряд" / "% попаданий" за окно;
    - streak_len_df: одна строка на КАЖДУЮ завершившуюся серию промахов (для
      гистограммы распределения длин серий в отчёте).
    Пропущенные дни (нет факта/шкалы/цены и т.п.) серию промахов НЕ рвут: считаются
    только дни с данными, подряд друг за другом (так же, как в JS-блоке рейтингов отчёта).
    """
    sub = daily[(daily["date_ts"] >= window_start) & (daily["date_ts"] <= window_end)]
    streak_rows, hitrate_rows, streak_lengths = [], [], []

    for (icao, city, source), g in sub.groupby(["icao", "city", "source"]):
        g = g.sort_values("date_ts")
        dates = g["date_ts"].tolist()
        hits = g["hit"].tolist()
        n = len(hits)
        hit_rate = 100.0 * sum(hits) / n if n else None

        longest = 0
        current = 0
        for date_val, hit in zip(dates, hits):
            if hit:
                if current > 0:
                    streak_lengths.append({"source": source, "length": current})
                current = 0
            else:
                current += 1
            longest = max(longest, current)
        if current > 0:
            streak_lengths.append({"source": source, "length": current})

        streak_rows.append({"icao": icao, "city": city, "source": source, "value": longest, "n": n})
        hitrate_rows.append({"icao": icao, "city": city, "source": source, "value": hit_rate, "n": n})

    return (
        pd.DataFrame(streak_rows),
        pd.DataFrame(hitrate_rows),
        pd.DataFrame(streak_lengths, columns=["source", "length"]),
    )


def compute_price_stats(daily, window_start, window_end):
    """
    Средняя и медианная цена ставки Yes бакета, в который попал прогнозный
    максимум, по (icao, city, source), за окно дат [window_start, window_end] —
    считается по непустым (не NaN) значениям bet_price; дни без цены просто
    исключаются из среднего/медианы (не считаются нулём).
    """
    sub = daily[(daily["date_ts"] >= window_start) & (daily["date_ts"] <= window_end)]
    rows = []
    for (icao, city, source), g in sub.groupby(["icao", "city", "source"]):
        prices = g["bet_price"].dropna().tolist()
        if not prices:
            rows.append({"icao": icao, "city": city, "source": source, "mean": None, "median": None, "n": 0})
            continue
        s = pd.Series(prices, dtype=float)
        rows.append({
            "icao": icao, "city": city, "source": source,
            "mean": float(s.mean()), "median": float(s.median()), "n": len(prices),
        })
    return pd.DataFrame(rows)


def rank_order(metric_df, ascending):
    """Сортировка городов по значению Wunderground (лучшее -> худшее); Windy —
    подстраховка для городов без данных по Wunderground, чтобы город не выпадал
    из рейтинга."""
    if metric_df.empty:
        return []
    piv = metric_df.pivot_table(index="city", columns="source", values="value")
    wu = piv["wunderground"] if "wunderground" in piv.columns else pd.Series(index=piv.index, dtype=float)
    windy = piv["windy"] if "windy" in piv.columns else pd.Series(index=piv.index, dtype=float)
    order_key = wu.combine_first(windy).dropna()
    ordered = order_key.sort_values(ascending=ascending).index.tolist()
    leftover = sorted(c for c in piv.index if c not in order_key.index)
    return ordered + leftover


def values_by_city(metric_df, value_col="value"):
    """{city: {source: значение из value_col}} — удобный доступ для таблиц/
    отбора по критериям."""
    out = {}
    for _, row in metric_df.iterrows():
        out.setdefault(row["city"], {})[row["source"]] = row[value_col]
    return out


def primary_value(city_values, primary="wunderground", fallback="windy"):
    """
    Единое значение метрики города: источник primary, если для него есть
    значение (не None), иначе fallback; None, если нет ни одного. Используется
    везде, где городу нужно сопоставить ровно одно число (сортировка рейтинга,
    отбор по критериям) — тот же принцип, что и в rank_order.
    """
    if city_values is None:
        return None
    if primary in city_values and city_values[primary] is not None:
        return city_values[primary]
    return city_values.get(fallback)


def primary_source(city_values, primary="wunderground", fallback="windy"):
    """Имя источника, значение которого вернёт primary_value для тех же
    city_values — чтобы можно было показать, откуда взята метрика."""
    if city_values and city_values.get(primary) is not None:
        return primary
    if city_values and city_values.get(fallback) is not None:
        return fallback
    return None
