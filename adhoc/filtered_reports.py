# ===== filtered_reports: три общих отчёта (прогноз, макс, совпадение), но только с городами из заданных списков =====
# Запуск: python filtered_reports.py   (скрипт лежит в adhoc/, работает с папкой бота ../weather_bot; результат пишется туда же).
#
# Отчёты строятся так же, как ночные accuracy_report_forecast.html / accuracy_report_max.html и adhoc/match_report.py, но датафрейм
# каждого отчёта урезается до городов из его списка ниже, поэтому и сводные графики, и рейтинг, и бэктест считаются только по ним.
# Списки на 02.11.2026: из городов, где плоская ставка в бэктесте отчёта за "Последние 6 месяцев" (слот 12:00, бегунки по умолчанию)
# в плюсе, оставлены выбранные вручную. На новых данных состав может измениться -- тогда списки нужно пересчитать.
import os, sys

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "weather_bot")   # папка бота: config/, src/, data_mining/
os.chdir(ROOT)
sys.path.insert(0, ROOT)
from src.config import Config
from src.cities import CITIES
from src.accuracy_report import AccuracyReportBuilder

# ---------------- настройки ----------------
SOURCE = "wunderground"   # источник прогноза для отчёта "совпадение"
CITIES_FORECAST = ["Atlanta", "Jeddah", "Lucknow", "Mexico City", "NYC", "Panama City", "Tokyo", "Warsaw"]
CITIES_MAX = ["Ankara", "Austin", "Helsinki", "Jeddah", "Los Angeles", "Munich", "Toronto"]
CITIES_MATCH = ["Chongqing", "Jeddah", "Lucknow", "NYC", "Panama City", "Seoul"]
OUT_FORECAST = "accuracy_report_forecast_flat6m.html"
OUT_MAX      = "accuracy_report_max_flat6m.html"
OUT_MATCH    = "accuracy_report_match_flat6m.html"

# ---------------- отчёты ----------------
cfg = Config("config/config.yaml")
b = AccuracyReportBuilder(cfg, CITIES)
slot = str(cfg.price_monitor.monitor_slot)


def only(df, cities):
    return df[df["city"].isin(cities)].reset_index(drop=True)


# прогноз
df_raw, _ = b.build_dataframe()
df_effective, _ = b.build_dataframe(effective_max=True)
path = b.save_report({"raw": only(df_raw, CITIES_FORECAST), "effective": only(df_effective, CITIES_FORECAST)}, out_path=os.path.join(ROOT, OUT_FORECAST))
print(f"Прогноз: {path}")

# макс и совпадение -- через подмену датафрейма самой дорогой ставки
_build_max_df = b.build_market_target_dataframe
df_mx_all = _build_max_df(slot)
fc_bucket = (df_raw[(df_raw["source"] == SOURCE) & (df_raw["snapshot_slot"].astype(str) == slot)]
             .drop_duplicates(["icao", "date"], keep="last")[["icao", "date", "forecast_bucket"]]
             .rename(columns={"forecast_bucket": "fc_bucket"}))
df_match_all = df_mx_all.merge(fc_bucket, on=["icao", "date"], how="inner")
df_match_all = df_match_all[df_match_all["forecast_bucket"].astype(int) == df_match_all["fc_bucket"].astype(int)].drop(columns=["fc_bucket"])

b.build_market_target_dataframe = lambda target_slot: only(df_mx_all, CITIES_MAX)
path = b.save_market_target_all_report(slot, out_path=os.path.join(ROOT, OUT_MAX))
print(f"Макс: {path}")

b.build_market_target_dataframe = lambda target_slot: only(df_match_all, CITIES_MATCH)
path = b.save_market_target_all_report(slot, out_path=os.path.join(ROOT, OUT_MATCH))
print(f"Совпадение: {path}")
