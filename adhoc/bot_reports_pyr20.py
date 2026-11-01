# ===== bot_reports_pyr20: отчёты по ботам (как accuracy_report_forecast_bot / max_bet_bot), но с другой лесенкой пирамиды =====
# Запуск: python bot_reports_pyr20.py   (скрипт лежит в adhoc/, работает с папкой бота ../weather_bot; результат пишется туда же).
#
# Бот и ночные отчёты считают лот пирамиды по config.yaml (cumulative, m=2): 1, 3, 7 стартовых лотов -> 5, 15, 35 акций.
# Здесь бэктест в отчётах идёт по custom-лесенке STEPS: 1, 3, 4 -> 5, 15, 20 акций, то есть у пирамиды 3 третья ставка
# 20 лотов вместо 35. У пирамиды 2 ничего не меняется (5, 15). Конфиг бота и сам бот не трогаются.
import os, sys

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "weather_bot")   # папка бота: config/, src/, data_mining/
os.chdir(ROOT)
sys.path.insert(0, ROOT)
from src.config import Config
from src.cities import CITIES
from src.accuracy_report import AccuracyReportBuilder
from src.watched_icaos import load_watched_icaos

# ---------------- настройки ----------------
STEPS            = [1, 3, 4]   # множители стартового лота по номеру ставки в серии: 5 -> 15 -> 20 при lot_size 5
OUT_FORECAST_BOT = "accuracy_report_forecast_bot_pyr20.html"
OUT_MAX_BET_BOT  = "accuracy_report_max_bet_bot_pyr20.html"

# ---------------- отчёты ----------------
cfg = Config("config/config.yaml")
pm = cfg.price_monitor
b = AccuracyReportBuilder(cfg, CITIES)
# Прогрессию пирамиды бэктест берёт из _pyramid_params (см. accuracy_report.py) -- подменяем её на custom STEPS для обоих наборов.
b._pyramid_params = lambda strategy_set=None: {"progression": "custom", "multiplier": 2.0, "steps": [float(x) for x in STEPS]}

watched = load_watched_icaos(cfg.resolve_config_path(pm.weather_forecast.watched_icaos_file),
                             loss_limit=getattr(pm.weather_forecast, "loss_limit", None))
watched_max_bet = load_watched_icaos(cfg.resolve_config_path(pm.max_bet.watched_icaos_file),
                                     loss_limit=getattr(pm.max_bet, "loss_limit", None))

df_raw, _ = b.build_dataframe()
if df_raw.empty:
    print("Нет пересекающихся данных прогноза и факта -- отчёт по прогнозу не построен.")
else:
    df_effective, _ = b.build_dataframe(effective_max=True)
    path = b.save_watched_report({"raw": df_raw, "effective": df_effective}, watched, out_path=os.path.join(ROOT, OUT_FORECAST_BOT))
    print(f"Отчёт по прогнозу: {path}" if path else "Отчёт по прогнозу не построен -- нет данных по городам конфига.")

path = b.save_market_target_report(watched_max_bet, pm.monitor_slot, out_path=os.path.join(ROOT, OUT_MAX_BET_BOT))
print(f"Отчёт по максимуму: {path}" if path else "Отчёт по максимуму не построен -- нет данных по городам конфига.")
