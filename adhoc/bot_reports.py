# ===== bot_reports: отчёты по ботам (accuracy_report_forecast_bot.html / accuracy_report_max_bet_bot.html) прямо сейчас =====
# Запуск: python bot_reports.py   (скрипт лежит в adhoc/, работает с папкой бота ../weather_bot).
#
# Те же отчёты и те же файлы, что строит ночной цикл бота (data_mining/reports/), по текущим watched_icaos_*.yaml, --
# чтобы после смены списка городов не ждать ночи.
import os, sys

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "weather_bot")   # папка бота: config/, src/, data_mining/
os.chdir(ROOT)
sys.path.insert(0, ROOT)
from src.config import Config
from src.cities import CITIES
from src.accuracy_report import AccuracyReportBuilder
from src.watched_icaos import load_watched_icaos

cfg = Config("config/config.yaml")
pm = cfg.price_monitor
b = AccuracyReportBuilder(cfg, CITIES)

watched = load_watched_icaos(cfg.resolve_config_path(pm.weather_forecast.watched_icaos_file),
                             loss_limit=getattr(pm.weather_forecast, "loss_limit", None))
watched_max_bet = load_watched_icaos(cfg.resolve_config_path(pm.max_bet.watched_icaos_file),
                                     loss_limit=getattr(pm.max_bet, "loss_limit", None))

df_raw, _ = b.build_dataframe()
if df_raw.empty:
    print("Нет пересекающихся данных прогноза и факта -- отчёт по прогнозу не построен.")
else:
    df_effective, _ = b.build_dataframe(effective_max=True)
    path = b.save_watched_report({"raw": df_raw, "effective": df_effective}, watched)
    print(f"Отчёт по прогнозу: {path}" if path else "Отчёт по прогнозу не построен -- нет данных по городам конфига.")

path = b.save_market_target_report(watched_max_bet, pm.monitor_slot)
print(f"Отчёт по максимуму: {path}" if path else "Отчёт по максимуму не построен -- нет данных по городам конфига.")
