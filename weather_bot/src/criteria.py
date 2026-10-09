"""
criteria.py

Класс CityCriteriaSelector — отбирает города, соответствующие критериям
качества прогноза (пункт 4 ТЗ), по уже посчитанному построчному df из
AccuracyReportBuilder.build_dataframe(). Метрики считаются для слота по
умолчанию (config.accuracy_report.default_slot_key — тот же, что и в
остальном отчёте), теми же функциями, что и блок «Рейтинги городов»
(city_metrics.py) — чтобы цифры совпадали с тем, что видно в HTML-отчёте.

Критерии (все пороги — в config.yaml, секция criteria):
  1. hit_rate_pct >= min_hit_rate_pct за последние hit_rate_window_days дней;
  2. средняя И медианная цена ставки Yes на бакет прогнозного максимума
     <= max_avg_price_cents / max_median_price_cents за последние
     price_window_days дней;
  3. самая длинная серия промахов подряд <= max_streak за последние
     streak_window_days дней.

СТРОГО Wunderground: в отличие от блока «Рейтинги городов» в HTML-отчёте (где
для города без данных по Wunderground подставляется Windy — см.
city_metrics.rank_order/primary_value), здесь никакого fallback на Windy нет —
город без данных по Wunderground за нужный период просто не попадает в отбор,
даже если по Windy у него всё хорошо.
"""

import pandas as pd

from . import city_metrics as cm

PRIMARY_SOURCE = "wunderground"


class CityCriteriaSelector:
    def __init__(self, config, cities):
        self.config = config
        self.cities = cities
        self.crit = config.criteria
        self.default_slot_key = config.accuracy_report.default_slot_key

    def select(self, df):
        """
        Возвращает список словарей — по одному на город, прошедший ВСЕ три
        критерия ПО WUNDERGROUND (без fallback на Windy), отсортированный по
        max_streak (число промахов подряд) по возрастанию — от меньшего к
        большему (пункт 5 ТЗ). Город без данных по Wunderground хотя бы за
        один из трёх периодов в отбор не попадает.
        """
        if df.empty:
            return []

        daily = cm.build_daily(df, self.default_slot_key)
        daily = daily[daily["source"] == PRIMARY_SOURCE]
        if daily.empty:
            return []

        max_date = daily["date_ts"].max()

        hitrate_window_start = max_date - pd.Timedelta(days=self.crit.hit_rate_window_days - 1)
        streak_window_start = max_date - pd.Timedelta(days=self.crit.streak_window_days - 1)
        price_window_start = max_date - pd.Timedelta(days=self.crit.price_window_days - 1)

        # Череда промахов и процент попаданий считаются compute_period-ом за
        # СВОИ окна — если оба критерия используют одно и то же окно (по
        # умолчанию оба 30 дней), второй вызов не нужен.
        streak_df, hitrate_df, _ = cm.compute_period(daily, streak_window_start, max_date)
        if self.crit.hit_rate_window_days != self.crit.streak_window_days:
            _, hitrate_df, _ = cm.compute_period(daily, hitrate_window_start, max_date)

        price_df = cm.compute_price_stats(daily, price_window_start, max_date)

        hitrate_by_city = cm.values_by_city(hitrate_df, "value")
        streak_by_city = cm.values_by_city(streak_df, "value")
        mean_price_by_city = cm.values_by_city(price_df, "mean")
        median_price_by_city = cm.values_by_city(price_df, "median")

        icao_by_city = daily.drop_duplicates("city").set_index("city")["icao"].to_dict()

        qualifying = []
        for city in sorted(daily["city"].unique()):
            hit_rate = hitrate_by_city.get(city, {}).get(PRIMARY_SOURCE)
            streak = streak_by_city.get(city, {}).get(PRIMARY_SOURCE)
            mean_price = mean_price_by_city.get(city, {}).get(PRIMARY_SOURCE)
            median_price = median_price_by_city.get(city, {}).get(PRIMARY_SOURCE)

            if hit_rate is None or streak is None or mean_price is None or median_price is None:
                continue  # недостаточно данных по Wunderground за один из периодов

            if hit_rate < self.crit.min_hit_rate_pct:
                continue
            if mean_price > self.crit.max_avg_price_cents:
                continue
            if median_price > self.crit.max_median_price_cents:
                continue
            if streak > self.crit.max_streak:
                continue

            qualifying.append({
                "city": city,
                "icao": icao_by_city.get(city),
                "source": PRIMARY_SOURCE,
                "hit_rate_pct": round(hit_rate, 1),
                "max_streak": int(streak),
                "avg_price_cents": round(mean_price, 1),
                "median_price_cents": round(median_price, 1),
            })

        qualifying.sort(key=lambda r: r["max_streak"])
        return qualifying

