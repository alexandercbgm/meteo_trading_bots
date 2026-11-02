# ===== match_report: стандартный общий отчёт бота (как accuracy_report_max.html), но сделка открывается, только если макс совпадает с прогнозом =====
# Запуск: python match_report.py   (скрипт лежит в adhoc/, работает с папкой бота ../weather_bot; результат пишется туда же).
#
# Берётся тот же датафрейм, что у отчёта по самой дорогой ставке (build_market_target_dataframe, слот monitor_slot), и в нём
# остаются только дни города, где бакет самой дорогой ставки совпал с бакетом прогноза Wunderground в том же слоте. В остальные
# дни сделки нет. Дальше -- обычный полный отчёт (build_market_target_all_report): все города, бегунки, фильтры, бэктест.
# Бегунок поправки оставляйте на 0: со сдвигом ставка уходит с совпавшего бакета.
import os, sys

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "weather_bot")   # папка бота: config/, src/, data_mining/
os.chdir(ROOT)
sys.path.insert(0, ROOT)
from src.config import Config
from src.cities import CITIES
from src.accuracy_report import AccuracyReportBuilder

# ---------------- настройки ----------------
SOURCE   = "wunderground"            # источник прогноза для сравнения с самой дорогой ставкой
OUT_HTML = "accuracy_report_match.html"

# ---------------- отчёт ----------------
cfg = Config("config/config.yaml")
b = AccuracyReportBuilder(cfg, CITIES)
slot = str(cfg.price_monitor.monitor_slot)

df_fc, _ = b.build_dataframe()
fc_bucket = (df_fc[(df_fc["source"] == SOURCE) & (df_fc["snapshot_slot"].astype(str) == slot)]
             .drop_duplicates(["icao", "date"], keep="last")[["icao", "date", "forecast_bucket"]]
             .rename(columns={"forecast_bucket": "fc_bucket"}))

_build_max_df = b.build_market_target_dataframe


def build_match_dataframe(target_slot):
    """Датафрейм самой дорогой ставки, урезанный до дней, где её бакет совпал с прогнозом."""
    df_mx = _build_max_df(target_slot)
    if df_mx.empty:
        return df_mx
    df = df_mx.merge(fc_bucket, on=["icao", "date"], how="inner")
    df = df[df["forecast_bucket"].astype(int) == df["fc_bucket"].astype(int)].drop(columns=["fc_bucket"])
    print(f"Совпадение: {len(df)} дней из {len(df_mx)} дней со ставками (слот {target_slot})")
    return df.reset_index(drop=True)


b.build_market_target_dataframe = build_match_dataframe
path = b.save_market_target_all_report(slot, out_path=os.path.join(ROOT, OUT_HTML))
print(f"Готово: {path}" if path else "Отчёт не построен -- нет дней с совпадением.")
