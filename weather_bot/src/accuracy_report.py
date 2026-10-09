"""
accuracy_report.py

Класс AccuracyReportBuilder — сравнивает прогнозы дневного максимума
температуры (Wunderground, Windy) из снапшотов weather_miner.py с фактическими
максимумами из actuals_miner.py — в ШКАЛЕ РЫНКА Polymarket того города
(Фаренгейт или Цельсий), а не в родной единице источника.

Шкала города определяется по последним доступным ставкам Polymarket в
data_mining/polymarket_gamma/ (по символу °F/°C в лейбле ставки). Ранние файлы,
где Polymarket ещё не майнился (или в конкретный день список ставок пуст),
пропускаются при определении шкалы — используется самая свежая доступная
запись. Если для города ни разу не нашлось ставок Polymarket — город целиком
исключается из отчёта (не с чем сверить шкалу).

Дни без факта (actuals) для конкретного города/даты также пропускаются.

МЕТРИКА (в БАКЕТАХ Polymarket, не в градусах): бакеты ставок Polymarket на
разных шкалах имеют разную ширину — на Фаренгейте бакет охватывает 2 градуса
(лейблы вида "78-79°F"), на Цельсии — 1 градус (лейблы вида "31°C"). Прогноз
даётся с шагом в 1 градус, поэтому ошибка считается в БАКЕТАХ:
bucket_index = floor(round(значение) / ширина_бакета) (ширина — 2 для F и 1
для C, см. config.accuracy_report.bucket_width), error = bucket_index(прогноз)
- bucket_index(факт). 0 — прогноз и факт попадают в один бакет ставки, +1/-1
и т.д. — промах на N бакетов.

Прогнозный максимум снапшота — простой max по всем часам этого снапшота (без
исключения одиночных выбросов, как в живом скринере).

СЛОТЫ: каждый снапшот weather_miner.py помечен snapshot_slot (например
"12:00"). Отчёт строит статистику и график отдельно по каждому слоту;
DEFAULT_SLOT_KEY (config.accuracy_report.default_slot_key) — слот по умолчанию
и в UI, и при отборе городов по критериям (criteria.py).

ДОПУЩЕНИЕ ПО Windy: сайт показывает температуру без буквы единицы ("28°") —
считается Фаренгейтом (профиль аккаунта настроен на F).
"""

import datetime as dt
import glob
import json
import os
import re
from collections import defaultdict
from zoneinfo import ZoneInfo

import pandas as pd

from . import city_metrics as cm
from .pyramid import validate_progression

# plotly импортируется лениво (внутри build_traces/build_rating_html), а не на
# уровне модуля: загрузка сырых данных (build_dataframe) и расчёт метрик не
# должны требовать plotly — это нужно только для построения самого HTML/графиков.

WU_TEMP_RE = re.compile(r"(-?\d+)\s*°?\s*([CFcf])")
WINDY_UNIT = "F"

SOURCE_LABELS = {"wunderground": "Wunderground", "windy": "Windy"}
SOURCE_COLORS = {"wunderground": "#e74c3c", "windy": "#3498db"}

RANGE_LABEL_RE = re.compile(r"^(-?\d+)-(-?\d+)°[CF]")
SINGLE_LABEL_RE = re.compile(r"^(-?\d+)°[CF]")
PRICE_NUM_RE = re.compile(r"[\d.]+")

MONTH_NAMES_EN = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]
MONTH_NAMES_RU_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


# ---------------------------------------------------------------------- #
# Свободные функции без состояния — используются и классом, и notifier.py
# (для ссылки на сегодняшнее событие Polymarket в сообщении в Telegram).
# ---------------------------------------------------------------------- #

def to_unit(value, source_unit, target_unit):
    """Конвертация градуса между F и C; если единицы совпадают — возвращает как есть."""
    if source_unit == target_unit:
        return value
    if source_unit == "F" and target_unit == "C":
        return (value - 32) / 1.8
    return value * 1.8 + 32


def parse_wu_value(temp_text):
    """"72°F" -> (72, "F"); (None, None), если не распарсилось."""
    m = WU_TEMP_RE.search(temp_text or "")
    if not m:
        return None, None
    return int(m.group(1)), m.group(2).upper()


def parse_windy_value(temp_text):
    """"28°" -> 28; None, если не распарсилось."""
    digits = re.sub(r"[^\d-]", "", temp_text or "")
    return int(digits) if digits else None


def parse_bucket_range(label):
    """
    "78-79°F" -> (78, 79); "31°C" -> (31, 31);
    "86°F or higher"/"36°C or above" -> (86, inf);
    "26°C or below"/"20°F or lower" -> (-inf, 26).
    """
    label = label or ""
    m = RANGE_LABEL_RE.match(label)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = SINGLE_LABEL_RE.match(label)
    if not m:
        return None, None
    val = int(m.group(1))
    low_label = label.lower()
    if "or higher" in low_label or "or above" in low_label:
        return val, float("inf")
    if "or below" in low_label or "or lower" in low_label:
        return float("-inf"), val
    return val, val


def parse_price_cents(price_str):
    """"40.5¢" -> 40.5; None, если не распарсилось."""
    m = PRICE_NUM_RE.search(price_str or "")
    return float(m.group()) if m else None


def polymarket_today(icao, cities):
    """
    Ссылка на СЕГОДНЯШНЕЕ событие Polymarket по городу + подпись "31 августа".
    "Сегодня" — по ЛОКАЛЬНОЙ дате города (tz из CITIES). (None, None), если для
    города нет slug/tz.
    """
    info = cities.get(icao, {})
    slug = info.get("slug")
    tz_name = info.get("tz")
    if not slug or not tz_name:
        return None, None
    try:
        now_local = dt.datetime.now(ZoneInfo(tz_name))
    except Exception:
        now_local = dt.datetime.now(dt.timezone.utc)
    month_en = MONTH_NAMES_EN[now_local.month - 1]
    url = f"https://polymarket.com/event/highest-temperature-in-{slug}-on-{month_en}-{now_local.day}-{now_local.year}"
    label = f"{now_local.day} {MONTH_NAMES_RU_GEN[now_local.month - 1]}"
    return url, label


def build_period_options(date_ts):
    """
    Список периодов для фильтра "Период": "Последние 30 дней" (по умолчанию),
    до 6 последних календарных месяцев ("YYYY-MM"), и "Последние 6 месяцев"
    (последние ~182 дня, даже если истории больше).
    """
    if date_ts.empty:
        return [("last30d", "Последние 30 дней", "relative", 30)]
    defs = [("last30d", "Последние 30 дней", "relative", 30)]
    months = sorted(date_ts.dt.to_period("M").unique())[-6:]
    for m in months:
        key = str(m)
        defs.append((key, key, "month", (m.start_time, m.end_time)))
    defs.append(("last6m", "Последние 6 месяцев", "relative", 182))
    return defs


def period_window(kind, payload, max_date):
    """(window_start, window_end) для периода по его kind/payload."""
    if kind is None:
        return None, None
    if kind == "relative":
        days = payload
        window_end = max_date
        window_start = window_end - pd.Timedelta(days=days - 1)
        return window_start, window_end
    return payload  # "month" — уже готовый (start, end)


def _price_stats_text(prices, total_count):
    """Текст всплывающей подсказки: среднее/медиана по цене ставки Yes — версия
    для Python (используется вспомогательно; основная логика продублирована в
    JS как priceStatsText, т.к. пересчитывается на лету при поправке/фильтре
    цены — см. build_html_report/build_rating_html)."""
    missing = total_count - len(prices)
    if not prices:
        return "Цена ставки Yes: нет данных" + (f" ({missing} без цены)" if missing else "")

    sorted_p = sorted(prices)
    n = len(sorted_p)
    mean_p = sum(sorted_p) / n
    median_p = sorted_p[n // 2] if n % 2 == 1 else (sorted_p[n // 2 - 1] + sorted_p[n // 2]) / 2

    missing_line = f"<br>Без цены: {missing}" if missing else ""
    return f"Цена ставки Yes — среднее {mean_p:.1f}¢, медиана {median_p:.1f}¢{missing_line}"


class AccuracyReportBuilder:
    def __init__(self, config, cities):
        self.config = config
        self.cities = cities
        self.data_dir = config.paths.data_dir
        self.wunderground_forecast_dir = config.paths.wunderground_forecast_dir
        self.wunderground_history_dir = config.paths.wunderground_history_dir
        self.windy_forecast_dir = config.paths.windy_forecast_dir
        self.polymarket_gamma_dir = config.paths.polymarket_gamma_dir
        self.polymarket_clob_dir = config.paths.polymarket_clob_dir
        self.report_dir = config.paths.report_dir
        self.report_filename = config.paths.report_filename
        self.default_slot_key = config.accuracy_report.default_slot_key
        self.bucket_width = {"F": config.accuracy_report.bucket_width.F, "C": config.accuracy_report.bucket_width.C}
        self.data_era_cutoff_date = config.accuracy_report.data_era_cutoff_date
        # Журнал РЕАЛЬНОЙ авто-торговли (см. TradingEngine/PositionStore в
        # trader.py) -- тот же путь, что и там, тот же принцип вычисления по
        # умолчанию (data_dir/trading). Читается ТОЛЬКО для наложения
        # фактической кривой баланса на график бэктеста (см.
        # _build_live_trades_map/build_rating_html) -- сам отчёт по
        # прогнозам/ценам это не использует ни для чего другого.
        self.trading_dir = getattr(config.paths, "trading_dir", os.path.join(config.paths.data_dir, "trading"))
        # Правила реальной торговли (TradingEngine/PriceMonitor), которые повторяют бэктест и панель
        # галочек/крестиков, чтобы отчёт совпадал с фактом: ставка дешевле min_bet_price_cents не
        # открывается; рынок "уже решён" (самый дорогой бакет >= decided_market_price_cents, и это не
        # наш бакет) -- не открывается; от min_bet_price_cents лот поднимается до min_order_usd / цена.
        pm_cfg = getattr(config, "price_monitor", None)
        self.min_bet_price_cents = float(getattr(pm_cfg, "min_bet_price_cents", 5) or 0)
        self.decided_price_cents = float(getattr(pm_cfg, "decided_market_price_cents", 95) or 0)
        self.min_order_usd = float(getattr(pm_cfg, "min_order_usd", 1.0) or 0)
        # Множитель лота пирамиды в бэктесте: лот = lot_size * множитель ** промахов. По умолчанию берётся
        # из конфига набора (price_monitor.<набор>.pyramid_multiplier -- то же значение, что у бота). Для
        # сценарных отчётов можно переопределить из кода: builder.backtest_pyramid_multiplier = 2 и/или
        # builder.backtest_pyramid_progression = "cumulative" (None = из конфига).
        self.backtest_pyramid_multiplier = None
        self.backtest_pyramid_progression = None   # "power" | "cumulative" | "custom" (None = из конфига)

    def _pyramid_params(self, strategy_set=None):
        """{"progression", "multiplier", "steps"} для бэктеста: переопределение из кода
        (backtest_pyramid_multiplier / backtest_pyramid_progression) -> настройки набора
        (weather_forecast/max_bet) из конфига -> power x2. Общий отчёт (без набора) берёт weather_forecast.
        Только множитель в переопределении (старый способ builder.backtest_pyramid_multiplier = 3) = прогрессия power."""
        pm_cfg = getattr(self.config, "price_monitor", None)
        scfg = getattr(pm_cfg, strategy_set or "weather_forecast", None)
        progression = getattr(scfg, "pyramid_progression", "power") if scfg else "power"
        mult = getattr(scfg, "pyramid_multiplier", getattr(scfg, "pyramid_min_multiplier", 2)) if scfg else 2
        steps = getattr(scfg, "pyramid_custom_steps", None) if scfg else None
        if self.backtest_pyramid_multiplier:
            mult = self.backtest_pyramid_multiplier
            if not self.backtest_pyramid_progression:
                progression = "power"
        if self.backtest_pyramid_progression:
            progression = self.backtest_pyramid_progression
        try:
            progression, steps = validate_progression(progression, steps, where="backtest")
            mult = float(mult) if float(mult) >= 1 else 2.0
        except (TypeError, ValueError):
            progression, steps, mult = "power", None, 2.0
        return {"progression": progression, "multiplier": mult, "steps": steps}

    # ------------------------------------------------------------------ #
    # Загрузка сырых данных
    # ------------------------------------------------------------------ #

    def bucket_index(self, value, scale):
        """Номер бакета ставки Polymarket для округлённого value на шкале scale."""
        width = self.bucket_width[scale]
        return round(value) // width

    def load_poly_bets(self):
        """{(icao, date, snapshot_slot): [(low, high, price_cents), ...]} — из
        polymarket_gamma_dir (см. WeatherMiner.scrape_polymarket). Паттерн
        глоба с "????_??_??" — только СУТОЧНЫЕ файлы живых цен слотов, НЕ
        месячные файлы факта (polymarket_gamma_<YYYY>_<MM>.jsonl, см.
        PolymarketActualsMiner) — та же папка, разные записи по имени файла."""
        bets = defaultdict(list)
        for path in sorted(glob.glob(os.path.join(self.polymarket_gamma_dir, "polymarket_gamma_????_??_??.jsonl"))):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    if rec.get("source") != "polymarket":
                        continue
                    key = (rec["icao"], rec["date"], rec.get("snapshot_slot"))
                    for b in rec.get("forecast_raw") or []:
                        low, high = parse_bucket_range(b.get("label", ""))
                        if low is None:
                            continue
                        price = parse_price_cents(b.get("price", ""))
                        if price is None:
                            continue
                        bets[key].append((low, high, price))
        return bets

    @staticmethod
    def match_bet_price(poly_bets, icao, date, slot, forecast_max):
        target = round(forecast_max)
        for low, high, price in poly_bets.get((icao, date, slot), []):
            if low <= target <= high:
                return price
        return None

    def load_actuals(self):
        """{(icao, date): max_temp_f} по всем actuals_<YYYY>_<MM>.jsonl в wunderground_history_dir."""
        actuals = {}
        for path in sorted(glob.glob(os.path.join(self.wunderground_history_dir, "actuals_*.jsonl"))):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    actuals[(rec["icao"], rec["date"])] = rec["max_temp_f"]
        return actuals

    def determine_city_scales(self):
        """{icao: "F"|"C"} по ПОСЛЕДНЕЙ непустой записи source=="polymarket" (polymarket_gamma_dir)."""
        scales = {}
        for path in sorted(glob.glob(os.path.join(self.polymarket_gamma_dir, "polymarket_gamma_????_??_??.jsonl"))):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    if rec.get("source") != "polymarket":
                        continue
                    bets = rec.get("forecast_raw") or []
                    if not bets:
                        continue
                    label = bets[0].get("label", "")
                    if "°F" in label:
                        scales[rec["icao"]] = "F"
                    elif "°C" in label:
                        scales[rec["icao"]] = "C"
        return scales

    def load_clob_bets(self):
        """
        {(icao, date, snapshot_slot): [(low, high, price_cents), ...]} —
        НОВЫЙ источник цены ставки: реальный bid/ask через CLOB API
        (polymarket_clob_dir, см. PolymarketClobMiner), а не Gamma-лейблы
        (load_poly_bets). Цена — ask_yes (цена ПОКУПКИ Yes, центы) — та же
        семантика "цены ставки Yes", что и у старого источника (Gamma отдавала
        последнюю цену/Buy Yes, здесь — реальная цена аска). low/high — В ТЕХ
        ЖЕ единицах, что и метка бакета (см. parse_bucket_range), НЕ номер
        бакета — тот же формат, что у load_poly_bets, чтобы match_bet_price
        работал одинаково для обоих источников.
        """
        bets = defaultdict(list)
        for path in sorted(glob.glob(os.path.join(self.polymarket_clob_dir, "polymarket_clob_*.jsonl"))):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    low, high = parse_bucket_range(rec.get("bucket", ""))
                    if low is None:
                        continue
                    ask_yes = rec.get("ask_yes")
                    if ask_yes is None:
                        continue
                    price_cents = round(float(ask_yes) * 100, 2)
                    key = (rec["icao"], rec["date"], rec.get("snapshot_slot"))
                    bets[key].append((low, high, price_cents))
        return bets

    def load_gamma_facts(self):
        """
        {(icao, date): (bucket_label, scale)} — НОВЫЙ источник факта:
        собственное разрешение события Polymarket (какой бакет реально
        выиграл — polymarket_gamma_<YYYY>_<MM>.jsonl, МЕСЯЧНЫЕ файлы, см.
        PolymarketActualsMiner), а не факт Wunderground (load_actuals).
        Глоб "????_??" — только месячные файлы факта, НЕ суточные файлы живых
        цен (polymarket_gamma_<YYYY>_<MM>_<DD>.jsonl) — та же папка, разные
        записи по имени файла (см. пояснение в config.yaml).
        """
        facts = {}
        for path in sorted(glob.glob(os.path.join(self.polymarket_gamma_dir, "polymarket_gamma_????_??.jsonl"))):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    facts[(rec["icao"], rec["date"])] = (rec["bucket"], rec["scale"])
        return facts

    def determine_city_scales_new(self):
        """{icao: "F"|"C"} по ПОСЛЕДНЕЙ известной записи факта Polymarket
        (load_gamma_facts) — аналог determine_city_scales для НОВОГО источника."""
        scales = {}
        for path in sorted(glob.glob(os.path.join(self.polymarket_gamma_dir, "polymarket_gamma_????_??.jsonl"))):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    scales[rec["icao"]] = rec["scale"]
        return scales

    @staticmethod
    def _effective_max(values):
        """
        "Эффективный" максимум списка значений: если чистый максимум
        встречается только один раз — считаем это слабым сигналом (случайный
        одиночный всплеск в почасовом прогнозе) и берём вместо него следующее
        по величине значение, даже если оно тоже единственное. Та же логика,
        что в живом скринере (poly_setup_screener.py/_effective_max) — здесь
        используется ОПЦИОНАЛЬНО, по галочке "1 час с максимумом не
        учитывается" в отчёте (см. build_dataframe(effective_max=...)); по
        умолчанию выключено — используется сырой max().
        """
        if not values:
            return None
        values = list(values)
        max_val = max(values)
        if values.count(max_val) > 1:
            return max_val
        rest = [v for v in values if v != max_val]
        return max(rest) if rest else max_val

    def build_dataframe(self, effective_max=False):
        """
        Возвращает (df, skip_info): построчный DataFrame (date, icao, city,
        region, source, snapshot_slot, scale, error, bet_price,
        forecast_bucket, era) и словарь с деталями пропущенных записей для
        print_skip_details. forecast_bucket — номер бакета РАВНО прогнозного
        значения (независимо от факта) — нужен блоку рейтингов, чтобы при
        поправке искать цену ставки на бакет (прогноз + поправка), а не только
        на исходный прогноз (см. build_rating_html).

        effective_max=True — прогнозный максимум снапшота считается через
        _effective_max (отбрасывает одиночный выброс в почасовых значениях)
        вместо сырого max(); используется только при построении HTML-отчётов
        (галочка "Не учитывать максимум, если по нему только 1 замер.") — отбор городов по
        критериям (CityCriteriaSelector) и PriceMonitor всегда работают с
        сырым максимумом (effective_max=False по умолчанию).

        era — "old" для дат <= self.data_era_cutoff_date: цена ставки и факт
        берутся из СТАРЫХ источников (load_poly_bets — Gamma-лейблы,
        load_actuals — Wunderground), как раньше; "new" для дат ПОСЛЕ
        cutoff: из НОВЫХ (load_clob_bets — реальный CLOB bid/ask,
        load_gamma_facts — собственное разрешение события Polymarket). В
        отчётах — галочки "Старые"/"Новые" (см. build_html_report/build_rating_html)
        фильтруют по этой колонке; при снятых обеих — данных не остаётся вовсе.
        """
        actuals_old = self.load_actuals()
        city_scales_old = self.determine_city_scales()
        poly_bets_old = self.load_poly_bets()

        gamma_facts_new = self.load_gamma_facts()
        city_scales_new = self.determine_city_scales_new()
        clob_bets_new = self.load_clob_bets()

        cutoff = self.data_era_cutoff_date
        unknown = sorted(set(self.cities) - set(city_scales_old) - set(city_scales_new))

        rows = []
        skipped_no_scale, skipped_no_actual, skipped_unparsable = [], [], []

        wu_paths = glob.glob(os.path.join(self.wunderground_forecast_dir, "wunderground_forcast_*.jsonl"))
        windy_paths = glob.glob(os.path.join(self.windy_forecast_dir, "windy_forcast_*.jsonl"))
        for path in sorted(wu_paths) + sorted(windy_paths):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    source = rec.get("source")
                    if source not in ("wunderground", "windy"):
                        continue

                    icao = rec["icao"]
                    date = rec["date"]
                    forecast_raw = rec.get("forecast_raw") or []
                    if not forecast_raw:
                        continue

                    era = "old" if date <= cutoff else "new"
                    city_scales = city_scales_old if era == "old" else city_scales_new

                    scale = city_scales.get(icao)
                    if scale is None:
                        skipped_no_scale.append({"icao": icao, "date": date, "source": source, "era": era})
                        continue

                    values = []
                    if source == "wunderground":
                        for p in forecast_raw:
                            v, u = parse_wu_value(p.get("temp_text"))
                            if v is not None:
                                values.append(to_unit(v, u, scale))
                    else:  # windy
                        for p in forecast_raw:
                            v = parse_windy_value(p.get("temp_text"))
                            if v is not None:
                                values.append(to_unit(v, WINDY_UNIT, scale))

                    if not values:
                        skipped_unparsable.append({"icao": icao, "date": date, "source": source, "era": era})
                        continue

                    forecast_max = self._effective_max(values) if effective_max else max(values)
                    forecast_bucket = self.bucket_index(forecast_max, scale)

                    if era == "old":
                        actual_f = actuals_old.get((icao, date))
                        if actual_f is None:
                            skipped_no_actual.append({"icao": icao, "date": date, "source": source, "era": era})
                            continue
                        actual_conv = to_unit(actual_f, "F", scale)
                        actual_bucket = self.bucket_index(actual_conv, scale)
                        bet_price = self.match_bet_price(poly_bets_old, icao, date, rec.get("snapshot_slot"), forecast_max)
                    else:  # new
                        fact = gamma_facts_new.get((icao, date))
                        if fact is None:
                            skipped_no_actual.append({"icao": icao, "date": date, "source": source, "era": era})
                            continue
                        fact_label, _fact_scale = fact
                        fact_low, fact_high = parse_bucket_range(fact_label)
                        if fact_low is None:
                            skipped_no_actual.append({"icao": icao, "date": date, "source": source, "era": era})
                            continue
                        fact_anchor = fact_low if fact_low != float("-inf") else fact_high
                        actual_bucket = self.bucket_index(fact_anchor, scale)
                        bet_price = self.match_bet_price(clob_bets_new, icao, date, rec.get("snapshot_slot"), forecast_max)

                    error = forecast_bucket - actual_bucket

                    city_info = self.cities.get(icao, {})
                    rows.append({
                        "date": date, "icao": icao,
                        "city": city_info.get("name", icao),
                        "region": city_info.get("tz", "?/?").split("/")[0],
                        "source": source, "snapshot_slot": rec.get("snapshot_slot"),
                        "scale": scale, "error": error, "bet_price": bet_price,
                        "forecast_bucket": forecast_bucket, "era": era,
                    })

        df = pd.DataFrame(rows)
        skip_info = {
            "unknown_scale_cities": unknown,
            "skipped_no_scale": skipped_no_scale,
            "skipped_no_actual": skipped_no_actual,
            "skipped_unparsable": skipped_unparsable,
        }
        return df, skip_info

    def build_market_target_dataframe(self, target_slot):
        """
        Строит DataFrame в ТОМ ЖЕ формате, что build_dataframe() (date, icao,
        city, region, source, snapshot_slot, scale, error, bet_price,
        forecast_bucket, era), но БЕЗ прогноза погоды вообще: таргетом на
        слоте target_slot берётся бакет САМОЙ ДОРОГОЙ ставки Polymarket этого
        дня — то есть проверяется, насколько точно сам рынок (его собственный
        топ-бакет) предсказывает факт, без всякого прогноза. source всегда
        "wunderground" — техническая метка: реального прогноза тут нет, но
        так этот df совместим с общим пайплайном отчёта/watched-механизмом
        (build_html_report/_build_corrected_df ожидают source
        "wunderground"/"windy"). Используется для отчёта
        accuracy_report_max_bet.html и сигналов PriceMonitor по городам из
        watched_icaos_max_bet.yaml.

        era — тот же принцип, что в build_dataframe: "old" для дат <=
        self.data_era_cutoff_date — "самая дорогая ставка" ищется среди
        СТАРЫХ Gamma-лейблов (load_poly_bets), факт — Wunderground
        (load_actuals); "new" для дат после cutoff — среди РЕАЛЬНОГО CLOB
        bid/ask (load_clob_bets, цена — ask_yes, цена ПОКУПКИ Yes — см.
        load_clob_bets), факт — собственное разрешение события Polymarket
        (load_gamma_facts).
        """
        poly_bets_old = self.load_poly_bets()
        actuals_old = self.load_actuals()
        scale_by_icao_old = self.determine_city_scales()

        clob_bets_new = self.load_clob_bets()
        gamma_facts_new = self.load_gamma_facts()
        scale_by_icao_new = self.determine_city_scales_new()

        cutoff = self.data_era_cutoff_date
        all_keys = set(poly_bets_old.keys()) | set(clob_bets_new.keys())

        rows = []
        for icao, date, slot in all_keys:
            if slot != target_slot:
                continue

            era = "old" if date <= cutoff else "new"
            if era == "old":
                bets = poly_bets_old.get((icao, date, slot))
                scale = scale_by_icao_old.get(icao)
            else:
                bets = clob_bets_new.get((icao, date, slot))
                scale = scale_by_icao_new.get(icao)
            if not bets or scale is None:
                continue

            low, high, price = max(bets, key=lambda b: b[2])
            anchor = low if low != float("-inf") else high
            target_bucket = self.bucket_index(anchor, scale)

            if era == "old":
                actual_f = actuals_old.get((icao, date))
                if actual_f is None:
                    continue
                actual_conv = to_unit(actual_f, "F", scale)
                actual_bucket = self.bucket_index(actual_conv, scale)
            else:
                fact = gamma_facts_new.get((icao, date))
                if fact is None:
                    continue
                fact_label, _fact_scale = fact
                fact_low, fact_high = parse_bucket_range(fact_label)
                if fact_low is None:
                    continue
                fact_anchor = fact_low if fact_low != float("-inf") else fact_high
                actual_bucket = self.bucket_index(fact_anchor, scale)

            error = target_bucket - actual_bucket

            city_info = self.cities.get(icao, {})
            rows.append({
                "date": date, "icao": icao,
                "city": city_info.get("name", icao),
                "region": city_info.get("tz", "?/?").split("/")[0],
                "source": "wunderground",
                "snapshot_slot": slot,
                "scale": scale, "error": error, "bet_price": price,
                "forecast_bucket": target_bucket, "era": era,
            })
        return pd.DataFrame(rows)

    def _build_maxbet_daily_rows(self, slot_keys):
        """
        {slot: {city: [[date, error, forecast_bucket, era], ...]}} — тот же
        построчный формат, что DAILY_ROWS в build_rating_html, но БЕЗ
        измерений mode (raw/effective — у "самой дорогой ставки" нет
        почасового прогноза, нечего фильтровать) и source (WU/Windy — тут
        одна-единственная серия на город, не привязанная к источнику
        прогноза). Строится ПО КАЖДОМУ слоту через build_market_target_dataframe
        отдельно (тот слот может отличаться от прогнозных, поэтому не
        переиспользует уже посчитанный df прогноза).

        Используется ТОЛЬКО для встраивания в ОСНОВНОЙ (неограниченный)
        отчёт — чтобы дот-переключатель "Стратегия: Прогноз/Максимум" в
        бэктесте мог выбирать между build_dataframe() (Прогноз) и ЭТИМ
        датасетом (Максимум) без пересборки всей страницы. Watched/max_bet
        отчёты в этом не нуждаются — у них таргет уже фиксирован самим
        отчётом (см. build_html_report(embed_maxbet_backtest=...)).
        """
        result = {}
        for slot in slot_keys:
            df = self.build_market_target_dataframe(slot)
            city_map = {}
            if not df.empty:
                df_sorted = df.sort_values("date")
                for city, g in df_sorted.groupby("city"):
                    rows = [
                        [row.date, int(row.error), int(row.forecast_bucket), row.era]
                        for row in g.itertuples()
                    ]
                    city_map[city] = rows
            result[slot] = city_map
        return result

    def _build_live_trades_map(self, strategy_set):
        """
        {city: [[date, pnl_net], ...]} по ФАКТИЧЕСКИ РАЗРЕШЁННЫМ (status ==
        "resolved") позициям авто-торговли для strategy_set ("weather_forecast"
        или "max_bet") -- одна точка на РЕАЛЬНУЮ сделку, отсортированная по
        дате. resolved_no_fill (заявка так и не исполнилась) сюда НЕ попадает
        -- без сделки нет ни P&L, ни точки на графике, как и в самом
        TradingEngine (пирамида в этом случае тоже не сбивается).

        Источник -- trades_<YYYY>_<MM>_<DD>.jsonl в self.trading_dir, ТОТ ЖЕ
        журнал, что читает TradingEngine.send_daily_summary (см. trader.py) --
        просто здесь агрегация не по дню, а по всей истории, и не в Telegram,
        а в JSON для графика (LIVE_TRADES в build_rating_html/JS).

        Используется ТОЛЬКО когда known strategy_set передан явно (watched-
        отчёт -- "weather_forecast", отчёт по самой дорогой ставке -- "max_bet")
        -- у основного (неограниченного) отчёта нет одного фиксированного
        strategy_set, поэтому там реальная кривая не накладывается вовсе.
        """
        result = defaultdict(list)
        if not strategy_set or not os.path.isdir(self.trading_dir):
            return {}
        for path in sorted(glob.glob(os.path.join(self.trading_dir, "trades_????_??_??.jsonl"))):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("strategy_set") != strategy_set or rec.get("status") != "resolved":
                        continue
                    city = rec.get("city") or rec.get("icao")
                    date_str = rec.get("opened_date")
                    pnl = rec.get("pnl_net")
                    if city is None or date_str is None or pnl is None:
                        continue
                    result[city].append([date_str, round(float(pnl), 4)])
        for city in result:
            result[city].sort(key=lambda r: r[0])
        return dict(result)

    def build_market_target_report(self, watched_max_bet, target_slot):
        """
        HTML-страница по принципу build_watched_report — только города из
        watched_max_bet (config/watched_icaos_max_bet.yaml), таргет = бакет
        самой дорогой ставки (build_market_target_dataframe), без бегунка
        поправки и без галочки "1 час с максимумом" (show_effective_max=False)
        — оба относятся к прогнозу погоды, которого здесь нет. Возвращает
        HTML-строку; None, если нет пересекающихся данных.
        """
        df_market = self.build_market_target_dataframe(target_slot)
        if df_market.empty:
            return None
        corrected = self._build_corrected_df(df_market, watched_max_bet)
        if corrected.empty:
            return None
        return self.build_html_report(
            {"raw": corrected, "effective": corrected}, watched=watched_max_bet, show_effective_max=False,
            live_strategy_set="max_bet",
        )

    def save_market_target_report(self, watched_max_bet, target_slot, out_path=None):
        """Как save_watched_report, но для build_market_target_report — см.
        config.paths.max_bet_report_filename. Возвращает путь к файлу или
        None, если отчёт не построен (нет данных)."""
        page = self.build_market_target_report(watched_max_bet, target_slot)
        if page is None:
            return None
        out_path = out_path or os.path.join(self.report_dir, self.config.paths.max_bet_report_filename)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(page)
        return out_path

    def print_skip_details(self, skip_info):
        """Печатает детализацию пропущенных записей — сколько и по каким
        городам/датам (не только итоговое число)."""

        def city_label(icao):
            name = self.cities.get(icao, {}).get("name", icao)
            return f"{name} ({icao})"

        if skip_info["unknown_scale_cities"]:
            print(f"⚠️ Нет данных Polymarket ни разу — шкала не определена, город исключён: "
                  f"{skip_info['unknown_scale_cities']}")

        skipped_no_scale = skip_info["skipped_no_scale"]
        print(f"\nПропущено записей — нет известной шкалы города: {len(skipped_no_scale)}")
        if skipped_no_scale:
            df_ns = pd.DataFrame(skipped_no_scale)
            for icao, cnt in df_ns.groupby("icao").size().sort_values(ascending=False).items():
                print(f"  {city_label(icao)}: {cnt} записей")

        skipped_no_actual = skip_info["skipped_no_actual"]
        print(f"\nПропущено записей — нет факта (actuals): {len(skipped_no_actual)}")
        if skipped_no_actual:
            df_na = pd.DataFrame(skipped_no_actual)
            unique_pairs = df_na.drop_duplicates(subset=["icao", "date"])
            print(f"  Уникальных пар город/дата без факта: {len(unique_pairs)}")
            by_city = (
                unique_pairs.groupby("icao")
                .agg(dates_without_actual=("date", "count"), min_date=("date", "min"), max_date=("date", "max"))
                .sort_values("dates_without_actual", ascending=False)
            )
            for icao, row in by_city.iterrows():
                print(f"  {city_label(icao)}: {int(row['dates_without_actual'])} дат без факта "
                      f"({row['min_date']} .. {row['max_date']})")

        skipped_unparsable = skip_info["skipped_unparsable"]
        if skipped_unparsable:
            print(f"\nПропущено записей — не распарсилось ни одно значение температуры: {len(skipped_unparsable)}")
            df_up = pd.DataFrame(skipped_unparsable)
            for icao, cnt in df_up.groupby("icao").size().sort_values(ascending=False).items():
                print(f"  {city_label(icao)}: {cnt} записей")

    # ------------------------------------------------------------------ #
    # Главный график (build_traces)
    # ------------------------------------------------------------------ #

    def _pick_default_slot_key(self, slot_keys):
        if self.default_slot_key in slot_keys:
            return self.default_slot_key
        return slot_keys[0] if slot_keys else None

    def build_traces(self, dfs):
        """
        dfs: {"raw": df_raw, "effective": df_effective} — тот же набор строк
        (отличаются только error/forecast_bucket/bet_price — от того, каким
        способом посчитан прогнозный максимум), но ОДИНАКОВЫЙ набор городов/
        регионов/слотов/периодов: галочка "Не учитывать максимум, если по нему только 1 замер."
        меняет ТОЛЬКО интерпретацию прогнозного максимума, не то, какие
        снапшоты вообще попали в отчёт. Строит ОДНУ фигуру с трассами ОБОИХ
        режимов сразу (ключи trace_map с префиксом "{mode}||...") — переключение
        режима в JS просто меняет, из каких (невидимых) трасс собирается
        сумма для видимых "дисплейных" (см. build_html_report).
        """
        import plotly.graph_objects as go

        reference_df = dfs["raw"]
        regions = sorted(reference_df["region"].unique())
        city_by_region = {r: sorted(reference_df.loc[reference_df["region"] == r, "city"].unique().tolist()) for r in regions}
        all_cities = sorted(reference_df["city"].unique())

        raw_slot_values = sorted(reference_df["snapshot_slot"].dropna().unique().tolist(), key=cm.sortable_slot_key)
        slot_keys = [str(v) for v in raw_slot_values]
        default_slot_key = self._pick_default_slot_key(slot_keys)

        # Диапазон ошибки — ОБЪЕДИНЕНИЕ обоих режимов (raw обычно шире, но не
        # гарантированно), чтобы ось X и её границы (используются и как
        # границы бегунка поправки) годились для любого выбранного режима.
        err_min = min(int(df["error"].min()) for df in dfs.values())
        err_max = max(int(df["error"].max()) for df in dfs.values())
        error_values = list(range(err_min, err_max + 1))

        prepared = {}
        for mode, df in dfs.items():
            df = df.copy()
            df["_date_ts"] = pd.to_datetime(df["date"])
            prepared[mode] = df
        period_options = build_period_options(prepared["raw"]["_date_ts"])
        max_date = prepared["raw"]["_date_ts"].max()

        fig = go.Figure()
        trace_map = {}  # {"{mode}||{period}||{slot}||city:{city}": [idx_wu, idx_windy]} — ВСЕ
        # invisible, чистое хранилище данных на (режим, город); реально
        # видимые трассы — две отдельные "дисплейные" (см. display_idxs
        # ниже), которые JS наполняет на лету СУММОЙ выбранных чекбоксами
        # городов ТЕКУЩЕГО режима — см. пояснение в build_html_report/getSelectedCities.

        hover_template = (
            "%{fullData.name}, ошибка %{x} ставок<br>"
            "Снапшотов: %{y}<br>%{customdata[0]}"
            "<br><i>Клик по столбцу — распределение цен</i><extra></extra>"
        )

        for mode, df in prepared.items():
            for period_key, period_label, kind, payload in period_options:
                window_start, window_end = period_window(kind, payload, max_date)
                period_df = df if window_start is None else df[(df["_date_ts"] >= window_start) & (df["_date_ts"] <= window_end)]

                for slot_val in raw_slot_values:
                    slot_key = str(slot_val)
                    slot_df = period_df[period_df["snapshot_slot"] == slot_val]

                    for city in all_cities:
                        subset = slot_df[slot_df["city"] == city]
                        idxs = []
                        for source in ("wunderground", "windy"):
                            source_df = subset[subset["source"] == source]
                            y, customdata = [], []
                            for ev in error_values:
                                bucket = source_df[source_df["error"] == ev]
                                y.append(len(bucket))
                                # Список снапшотов бакета (icao/дата/бакет ПРОГНОЗА/эра), а не готовая
                                # цена — цена ставки на (прогноз + поправка) ищется динамически в
                                # JS через BUCKET_PRICES при каждом изменении поправки/фильтра
                                # цены, точно так же, как уже сделано в панели рейтингов
                                # (checkmarks/crosses) — см. lookupPrice в build_html_report. era —
                                # для фильтра "Старые"/"Новые" (см. getSelectedEras).
                                snapshots = [
                                    [row.icao, row.date, int(row.forecast_bucket), row.era]
                                    for row in bucket[["icao", "date", "forecast_bucket", "era"]].itertuples()
                                ]
                                customdata.append([snapshots, source, ev])

                            fig.add_trace(
                                go.Bar(
                                    x=error_values, y=y, customdata=customdata,
                                    name=SOURCE_LABELS[source], marker_color=SOURCE_COLORS[source],
                                    opacity=0.9, visible=False, legendgroup=source, showlegend=False,
                                    hovertemplate=hover_template,
                                )
                            )
                            idxs.append(len(fig.data) - 1)
                        trace_map[f"{mode}||{period_key}||{slot_key}||city:{city}"] = idxs

        # Две ВИДИМЫЕ трассы — то, что реально рисуется. Изначально пустые:
        # JS наполняет их суммой по умолчанию ("Все города", режим raw) сразу
        # при загрузке страницы, тем же кодом, что и при любом изменении фильтров.
        display_idxs = []
        for source in ("wunderground", "windy"):
            fig.add_trace(
                go.Bar(
                    x=[], y=[], customdata=[],
                    name=SOURCE_LABELS[source], marker_color=SOURCE_COLORS[source],
                    opacity=0.9, visible=True, legendgroup=source, showlegend=True,
                    hovertemplate=hover_template,
                )
            )
            display_idxs.append(len(fig.data) - 1)

        default_slot_label = f"Слот {default_slot_key}" if default_slot_key else "нет данных"
        fig.update_layout(
            title=dict(
                text=f"<b>Распределение точности прогноза максимума — Все города, {default_slot_label}, Последние 30 дней</b>",
                x=0.02, xanchor="left",
            ),
            xaxis_title="Ошибка прогноза в ставках", yaxis_title="Количество снапшотов",
            barmode="overlay", bargap=0.05, legend_title="Источник", template="plotly_white",
        )
        fig.update_xaxes(dtick=1)

        return (
            fig, trace_map, regions, city_by_region, all_cities, slot_keys, period_options,
            default_slot_key, display_idxs, error_values, len(fig.data),
        )

    # ------------------------------------------------------------------ #
    # Блок «Рейтинги городов»
    # ------------------------------------------------------------------ #

    def _build_bucket_price_map(self, scale_by_icao):
        """
        {icao: {date_str: {slot: [[low_bucket_or_null, high_bucket_or_null, price], ...]}}}
        — список ставок Polymarket по каждому (город, дата, слот) В ТЕРМИНАХ
        НОМЕРОВ БАКЕТОВ (не сырых температур). Каждая ставка хранится
        ДИАПАЗОНОМ (низкая/высокая граница), а не одним числом-якорем: у
        открытых ставок ("104°F or higher") нет единого номера бакета, под
        который попадали бы ВСЕ значения прогноза внутри диапазона —
        bucket_index чисто арифметическая функция от самого значения
        (round(value)//width), и у сколь угодно большого прогноза внутри
        открытого диапазона будет СВОЙ, ещё больший номер. Раньше здесь
        хранился один "якорный" номер бакета (bucket_index от конечной
        границы), из-за чего lookupPrice в JS не находил цену для открытых
        ставок, если смещённый поправкой таргет оказывался ВЫШЕ (или ниже)
        этого якоря — визуально это выглядело как галочка/крестик без цены.
        Теперь ищем совпадение диапазоном — null означает открытую границу
        ("или выше"/"или ниже"), как и в raw-функции
        match_bet_price/parse_bucket_range, только в единицах бакетов.

        Объединяет ОБА источника цены — load_poly_bets (старый, Gamma-лейблы,
        даты era="old") и load_clob_bets (новый, реальный CLOB bid/ask, даты
        era="new") — так что lookupPrice в JS находит цену независимо от
        того, к какой эре относится дата (иначе фильтр по диапазону цены на
        новых датах всегда давал бы "нет цены").
        """
        poly_bets = defaultdict(list)
        for source_bets in (self.load_poly_bets(), self.load_clob_bets()):
            for key, bets in source_bets.items():
                poly_bets[key].extend(bets)

        result = {}
        for (icao, date, slot), bets in poly_bets.items():
            scale = scale_by_icao.get(icao)
            if scale is None:
                continue
            entries = []
            for low, high, price in bets:
                low_bucket = None if low == float("-inf") else self.bucket_index(low, scale)
                high_bucket = None if high == float("inf") else self.bucket_index(high, scale)
                entries.append([low_bucket, high_bucket, price])
            result.setdefault(icao, {}).setdefault(date, {})[slot] = entries
        return result

    def _build_city_corrections(self, icao_by_city, watched):
        """{city: {"corr": int, "min_price": float|None, "max_price": float|None,
        "comment": str, "strategy": "flat"|"pyramid", "loss_limit": int|None}} —
        для встраивания в JS (аннотация на графике при выборе города, колонки
        "Поправка"/"Мин. цена"/"Макс. цена" в таблицах рейтинга, а strategy/
        loss_limit — параметры бэктеста по умолчанию для watched/max_bet
        отчётов, см. build_rating_html) — только когда задан watched
        (второй/третий, "скорректированный" отчёт, см.
        build_watched_report/build_market_target_report).
        """
        if not watched:
            return {}
        result = {}
        for city, icao in icao_by_city.items():
            cfg = watched.get(icao)
            if not cfg:
                continue
            result[city] = {
                "corr": cfg["weather_corr"],
                "min_price": cfg["min_price_limit"],
                "max_price": cfg["max_price_limit"],
                "comment": cfg["comment"],
                "strategy": cfg.get("strategy", "flat"),
                "loss_limit": cfg.get("loss_limit"),
                "weather_source": cfg.get("weather_source", "wunderground"),
            }
        return result

    def build_rating_html(self, dfs, watched=None, bucket_price_map=None, show_effective_max=True,
                           embed_maxbet_backtest=False, live_strategy_set=None):
        """
        dfs: {"raw": df_raw, "effective": df_effective} — см. build_traces.

        live_strategy_set — "weather_forecast" | "max_bet" | None. Если
        задан, дополнительно встраивает LIVE_TRADES (см. _build_live_trades_map)
        — реальные, УЖЕ СОВЕРШЁННЫЕ сделки авто-торговли этого strategy_set —
        и график бэктеста рисует их отдельной линией поверх симуляции, с
        легендой (см. renderBacktestChart/gatherLiveTrades в JS ниже). None
        (по умолчанию, в т.ч. в основном неограниченном отчёте) — реальная
        линия не строится вовсе, LIVE_TRADES остаётся пустым {}.
        Строит ОДИН блок рейтингов с данными ОБОИХ режимов сразу
        (DAILY_ROWS с префиксом режима) — переключение галочки "1 час с
        максимумом не учитывается" в JS просто меняет, из какого среза
        DAILY_ROWS считаются попадания/промахи/серии; PERIOD_WINDOWS,
        CITY_LINKS, BUCKET_PRICES, ALL_CITIES общие для обоих режимов (набор
        городов/дат/ставок не зависит от того, как посчитан прогнозный
        максимум — отличаются только error/forecast_bucket).

        bucket_price_map — если уже посчитан вызывающим кодом (build_html_report
        строит его один раз и для главного графика, и для этого блока), можно
        передать готовым, чтобы не считать дважды.

        show_effective_max=False — скрывает галочку "1 час с максимумом не
        учитывается" (нечего фильтровать, если прогноза с почасовыми
        значениями нет вообще — см. build_market_target_report).

        embed_maxbet_backtest=True — ТОЛЬКО для основного (неограниченного)
        отчёта: дополнительно строит и встраивает DAILY_ROWS_MAXBET (см.
        _build_maxbet_daily_rows) — таргет "самая дорогая ставка", БЕЗ
        измерений mode/source. Даёт блоку бэктеста дот-переключатель
        "Стратегия: Прогноз/Максимум". Для watched/max_bet отчётов не нужен
        (там ОДИН таргет и так уже зафиксирован самим отчётом) — бэктест
        там использует единственный DAILY_ROWS напрямую, без переключателя, а
        параметры Тип/Количество шагов берёт из CITY_CORRECTIONS
        (strategy/loss_limit конкретного города, см. _build_city_corrections).
        """
        reference_df = dfs["raw"]
        if reference_df.empty:
            return "<p>Недостаточно данных для рейтингов городов.</p>"

        icao_by_city = reference_df.drop_duplicates("city").set_index("city")["icao"].to_dict()
        if bucket_price_map is None:
            scale_by_icao = reference_df.drop_duplicates("icao").set_index("icao")["scale"].to_dict()
            bucket_price_map = self._build_bucket_price_map(scale_by_icao)
        city_corrections = self._build_city_corrections(icao_by_city, watched)
        is_watched = bool(watched)

        raw_slot_values = sorted(reference_df["snapshot_slot"].dropna().unique().tolist(), key=cm.sortable_slot_key)
        slot_keys = [str(v) for v in raw_slot_values]
        default_slot_key = self._pick_default_slot_key(slot_keys)
        slot_options = [(str(v), f"Слот {v}") for v in raw_slot_values]

        maxbet_daily_rows = self._build_maxbet_daily_rows(slot_keys) if embed_maxbet_backtest else {}
        live_trades_map = self._build_live_trades_map(live_strategy_set)

        daily_by_slot_by_mode = {
            mode: {str(v): cm.build_daily(df, v) for v in raw_slot_values}
            for mode, df in dfs.items()
        }

        def build_period_defs(reference_daily):
            if reference_daily.empty:
                return []
            defs = [("last30d", "Последние 30 дней", "relative", 30)]
            months = sorted(reference_daily["date_ts"].dt.to_period("M").unique())[-6:]
            for m in months:
                key = str(m)
                defs.append((key, key, "month", (m.start_time, m.end_time)))
            defs.append(("last6m", "Последние 6 месяцев", "relative", 182))
            return defs

        # Период/слот — набор ОДИНАКОВ для обоих режимов (см. докстринг), поэтому
        # период_defs и period_windows_map считаются один раз по режиму "raw".
        reference_daily_by_slot = daily_by_slot_by_mode["raw"]
        reference_daily = reference_daily_by_slot.get(default_slot_key, pd.DataFrame())
        period_defs = build_period_defs(reference_daily)

        # Сырые данные по дням для КАЖДОГО (режим, слот, город, источник) —
        # единственный источник правды для JS. Графики/таблицы рейтинга и
        # панель детализации по клику на город считаются ЦЕЛИКОМ в браузере
        # (а не заранее в Python по каждой комбинации слот×период), потому что
        # поправка (сдвиг цели) и фильтр по цене ставки требуют пересчитывать
        # серию попаданий/промахов и процент попаданий на лету — это НЕЛЬЗЯ
        # сделать простым сдвигом уже агрегированных чисел (в отличие от
        # главного графика выше: череда промахов — не линейная функция от
        # отдельных дней).
        #
        # ВАЖНО: строка хранит forecast_bucket (номер бакета прогноза — уже с
        # учётом "запечённой" поправки города, если df пришёл из
        # build_watched_report, см. _build_corrected_df, и с учётом выбранного
        # режима raw/effective), а НЕ цену — цена каждый раз ищется динамически
        # в BUCKET_PRICES по этому (возможно ещё раз смещённому интерактивной
        # поправкой) бакету, а не берётся готовой из строки.
        daily_rows_map = {}
        for mode, daily_by_slot in daily_by_slot_by_mode.items():
            mode_map = {}
            for slot_key in slot_keys:
                daily = daily_by_slot[slot_key]
                city_map = {}
                if not daily.empty:
                    daily_sorted = daily.sort_values("date_ts")
                    for (city, source), g in daily_sorted.groupby(["city", "source"]):
                        rows = []
                        for _, row in g.iterrows():
                            rows.append([row["date"], int(row["error"]), int(row["forecast_bucket"]), row["era"]])
                        city_map.setdefault(city, {})[source] = rows
                mode_map[slot_key] = city_map
            daily_rows_map[mode] = mode_map

        # Окна периодов (в датах) ДЛЯ КАЖДОГО слота отдельно — "относительные"
        # периоды (последние 30 дней/6 месяцев) отсчитываются от последней даты
        # ИМЕННО этого слота (общей для обоих режимов); "календарные месяцы" —
        # от одних и тех же границ независимо от слота.
        period_windows_map = {}
        for slot_key in slot_keys:
            daily = reference_daily_by_slot[slot_key]
            if daily.empty:
                period_windows_map[slot_key] = {}
                continue
            slot_max_date = daily["date_ts"].max()
            windows = {}
            for period_key, _period_label, kind, payload in period_defs:
                if kind == "relative":
                    days = payload
                    window_end = slot_max_date
                    window_start = window_end - pd.Timedelta(days=days - 1)
                else:
                    window_start, window_end = payload
                windows[period_key] = [
                    pd.Timestamp(window_start).strftime("%Y-%m-%d"),
                    pd.Timestamp(window_end).strftime("%Y-%m-%d"),
                ]
            period_windows_map[slot_key] = windows

        city_links = {}
        for city, icao in icao_by_city.items():
            info = self.cities.get(icao, {})
            poly_url, poly_label = polymarket_today(icao, self.cities)
            city_links[city] = {
                "icao": icao,
                "wu_url": info.get("weather_url"),
                "windy_url": info.get("windy_url"),
                "poly_url": poly_url,
                "poly_label": poly_label,
            }

        err_min = min(int(df["error"].min()) for df in dfs.values())
        err_max = max(int(df["error"].max()) for df in dfs.values())
        prices_all = pd.concat([df["bet_price"] for df in dfs.values()]).dropna()
        price_min_str = f"{prices_all.min():.1f}" if not prices_all.empty else "0.0"
        price_max_str = f"{prices_all.max():.1f}" if not prices_all.empty else "100.0"

        slot_option_tags = "".join(
            f'<option value="{key}"{" selected" if key == default_slot_key else ""}>{label}</option>'
            for key, label in slot_options
        )
        period_option_tags = "".join(f'<option value="{key}">{label}</option>' for key, label, *_ in period_defs)

        correction_price_row = "" if is_watched else f"""
<div class="controls">
  <span><label for="ratingCorrectionRange">Поправка:</label>
  <input type="range" id="ratingCorrectionRange" min="{err_min}" max="{err_max}" step="1" value="0">
  <span id="ratingCorrectionValue" class="correction-value">0</span></span>
  <span><label for="ratingPriceMin">Цена от:</label>
  <input type="number" id="ratingPriceMin" class="price-input" step="0.1" value="{price_min_str}">
  <label for="ratingPriceMax">до:</label>
  <input type="number" id="ratingPriceMax" class="price-input" step="0.1" value="{price_max_str}">¢</span>
</div>
"""

        controls = f"""
<div class="controls">
  <span><label for="ratingPeriodSelect">Период:</label>
  <select id="ratingPeriodSelect">{period_option_tags}</select></span>
  <span><label for="ratingSlotSelect">Слот:</label>
  <select id="ratingSlotSelect">{slot_option_tags}</select></span>
  {'' if not show_effective_max else '<span><label class="checkbox-inline"><input type="checkbox" id="ratingEffectiveMaxCheckbox"> Не учитывать максимум, если по нему только 1 замер.</label></span>'}
  <span class="era-toggle">
    <label class="checkbox-inline"><input type="checkbox" id="ratingEraOldCheckbox" checked> Старые</label>
    <label class="checkbox-inline"><input type="checkbox" id="ratingEraNewCheckbox" checked> Новые</label>
  </span>
</div>
{correction_price_row}"""

        detail_panel = """
<div id="cityStreakDetail" class="city-streak-detail">
  <p class="streak-hint">Кликните по столбцу города на графике «Самая длинная череда промахов», чтобы увидеть детализацию попаданий и промахов за выбранный период.</p>
</div>
"""

        # Полные настройки (Стратегия/Тип/Количество шагов) — ТОЛЬКО в основном
        # (неограниченном) отчёте; у watched/max_bet отчётов таргет уже
        # зафиксирован самим отчётом, а Тип/Количество шагов берутся из
        # конфига конкретного города (CITY_CORRECTIONS.strategy/loss_limit,
        # см. getBacktestTypeAndSteps в JS) — тут им взяться неоткуда, город
        # может быть и не выбран (бэктест "по всем городам сразу").
        backtest_full_controls = """
  <span class="backtest-group">
    <span class="backtest-group-label">Стратегия:</span>
    <label class="radio-inline"><input type="radio" name="backtestTarget" value="forecast" checked> Прогноз погоды</label>
    <label class="radio-inline"><input type="radio" name="backtestTarget" value="maxbet"> Самая дорогая ставка</label>
  </span>
  <span class="backtest-group">
    <span class="backtest-group-label">Тип:</span>
    <span class="backtest-toggle-label">Плоская</span>
    <label class="toggle-switch"><input type="checkbox" id="backtestTypeToggle"><span class="toggle-slider"></span></label>
    <span class="backtest-toggle-label">Пирамида</span>
  </span>
  <span><label for="backtestSteps">Количество шагов:</label>
  <input type="number" id="backtestSteps" class="price-input" step="1" min="1" value="2"></span>
  <span class="backtest-linebreak"></span>
""" if not is_watched else ""

        backtest_panel = f"""
<div id="backtestSettings" class="controls backtest-controls">
{backtest_full_controls}
  <span><label for="backtestLotSize">Размер лота:</label>
  <input type="number" id="backtestLotSize" class="price-input" step="0.1" min="5" value="5.0"></span>
  <span><label for="backtestCommission">Комиссия на контракт:</label>
  <input type="number" id="backtestCommission" class="price-input" step="0.001" min="0" value="0.012">$</span>
  <span><label for="backtestBalance">Баланс:</label>
  <input type="number" id="backtestBalance" class="price-input" step="1" min="1" value="100">$</span>
</div>
<div id="backtestStats" class="backtest-stats">
  <button type="button" id="backtestResetCity" class="backtest-reset-btn">↺ Все города</button>
</div>
<div id="backtestChart"></div>
"""

        charts_skeleton = """
<div id="ratingStreakChart"></div>
<div id="ratingStreakDist"></div>
<div id="ratingHitrateChart"></div>
<div class="rank-tables-row">
  <div class="rank-table-col"><h4>Череда промахов</h4><div id="ratingStreakTable"></div></div>
  <div class="rank-table-col"><h4>Процент попаданий</h4><div id="ratingHitrateTable"></div></div>
</div>
"""

        daily_rows_json = json.dumps(daily_rows_map, ensure_ascii=False)
        maxbet_daily_rows_json = json.dumps(maxbet_daily_rows, ensure_ascii=False)
        live_trades_json = json.dumps(live_trades_map, ensure_ascii=False)
        period_windows_json = json.dumps(period_windows_map, ensure_ascii=False)
        city_links_json = json.dumps(city_links, ensure_ascii=False)
        period_labels_json = json.dumps({key: label for key, label, *_ in period_defs}, ensure_ascii=False)
        slot_labels_json = json.dumps(dict(slot_options), ensure_ascii=False)
        all_cities_json = json.dumps(sorted(icao_by_city.keys()), ensure_ascii=False)
        bucket_prices_json = json.dumps(bucket_price_map, ensure_ascii=False)
        city_corrections_json = json.dumps(city_corrections, ensure_ascii=False)

        rating_script = f"""
<script>
(function() {{
  const DAILY_ROWS = {daily_rows_json};
  const DAILY_ROWS_MAXBET = {maxbet_daily_rows_json};
  const LIVE_TRADES = {live_trades_json};
  const PERIOD_WINDOWS = {period_windows_json};
  const CITY_LINKS = {city_links_json};
  const PERIOD_LABELS = {period_labels_json};
  const SLOT_LABELS = {slot_labels_json};
  const ALL_CITIES = {all_cities_json};
  const BUCKET_PRICES = {bucket_prices_json};
  const MIN_BET_PRICE_CENTS = {json.dumps(self.min_bet_price_cents)};
  const DECIDED_PRICE_CENTS = {json.dumps(self.decided_price_cents)};
  const MIN_ORDER_USD = {json.dumps(self.min_order_usd)};
  const PYRAMID = {json.dumps(self._pyramid_params(live_strategy_set))};
  // Во сколько раз лот ставки с номером misses (с нуля) больше стартового -- та же формула, что в src/pyramid.py.
  function pyramidFactor(misses) {{
    if (PYRAMID.progression === "custom" && PYRAMID.steps && PYRAMID.steps.length) {{
      return PYRAMID.steps[Math.min(misses, PYRAMID.steps.length - 1)];
    }}
    const m = PYRAMID.multiplier;
    if (PYRAMID.progression === "cumulative") {{
      return m === 1 ? misses + 1 : (Math.pow(m, misses + 1) - 1) / (m - 1);
    }}
    return Math.pow(m, misses);
  }}
  const CITY_CORRECTIONS = {city_corrections_json};
  const WATCHED_MODE = {json.dumps(is_watched)};
  const SOURCE_LABELS_LOCAL = {json.dumps(SOURCE_LABELS, ensure_ascii=False)};
  const SOURCE_COLORS_LOCAL = {json.dumps(SOURCE_COLORS, ensure_ascii=False)};
  const DATA_ERA_CUTOFF_DATE = {json.dumps(self.data_era_cutoff_date)};
  const PRICE_DEFAULT_MIN = {price_min_str};
  const PRICE_DEFAULT_MAX = {price_max_str};

  const periodSel = document.getElementById("ratingPeriodSelect");
  const slotSel = document.getElementById("ratingSlotSelect");
  const effectiveMaxCheckbox = document.getElementById("ratingEffectiveMaxCheckbox");
  const eraOldCheckbox = document.getElementById("ratingEraOldCheckbox");
  const eraNewCheckbox = document.getElementById("ratingEraNewCheckbox");
  function getSelectedEras() {{
    const eras = [];
    if (eraOldCheckbox && eraOldCheckbox.checked) eras.push("old");
    if (eraNewCheckbox && eraNewCheckbox.checked) eras.push("new");
    return eras;
  }}
  const correctionRange = document.getElementById("ratingCorrectionRange");
  const correctionValueLabel = document.getElementById("ratingCorrectionValue");
  const priceMinInput = document.getElementById("ratingPriceMin");
  const priceMaxInput = document.getElementById("ratingPriceMax");
  const detailPanel = document.getElementById("cityStreakDetail");

  let lastCity = null;
  let streakClickBound = false;

  function currentMode() {{ return (effectiveMaxCheckbox && effectiveMaxCheckbox.checked) ? "effective" : "raw"; }}

  function fmtSigned(v) {{
    const n = Number(v);
    return (n > 0 ? "+" : "") + n;
  }}
  function fmtStreak(v) {{ return (v === null || v === undefined) ? "—" : `${{v}} дн.`; }}
  function fmtPct(v) {{ return (v === null || v === undefined) ? "—" : `${{v.toFixed(0)}}%`; }}

  function isPriceFilterActive() {{
    return Number(priceMinInput.value) !== PRICE_DEFAULT_MIN || Number(priceMaxInput.value) !== PRICE_DEFAULT_MAX;
  }}

  // Фильтр (поправка + порог цены), применяемый к КОНКРЕТНОМУ городу. В
  // обычном отчёте (WATCHED_MODE=false) — один и тот же, из глобальных
  // бегунков, для всех городов сразу (как раньше). Во втором,
  // "скорректированном" отчёте (WATCHED_MODE=true) — поправка уже "запечена"
  // в сами данные (df пришёл из build_watched_report/_build_corrected_df, см.
  // accuracy_report.py), поэтому здесь всегда 0, а порог цены — СВОЙ для
  // каждого города из watched_icaos_weather_forecast.yaml (или фильтр вовсе не активен, если
  // для города порог не задан).
  function getCityFilter(city) {{
    if (WATCHED_MODE) {{
      const cfg = CITY_CORRECTIONS[city];
      const hasMin = cfg && cfg.min_price !== null && cfg.min_price !== undefined;
      const hasMax = cfg && cfg.max_price !== null && cfg.max_price !== undefined;
      if (!hasMin && !hasMax) {{
        return {{ correction: 0, priceActive: false, priceMin: 0, priceMax: 100 }};
      }}
      return {{
        correction: 0, priceActive: true,
        priceMin: hasMin ? cfg.min_price : 0,
        priceMax: hasMax ? cfg.max_price : 100,
      }};
    }}
    return {{
      correction: Number(correctionRange.value),
      priceActive: isPriceFilterActive(),
      priceMin: Number(priceMinInput.value), priceMax: Number(priceMaxInput.value),
    }};
  }}

  function dateRange(startStr, endStr) {{
    const dates = [];
    let cursor = new Date(startStr + "T00:00:00Z");
    const end = new Date(endStr + "T00:00:00Z");
    while (cursor <= end) {{
      dates.push(cursor.toISOString().slice(0, 10));
      cursor = new Date(cursor.getTime() + 86400000);
    }}
    return dates;
  }}

  // Цена ставки НА БАКЕТ targetBucket (= прогноз[+запечённая поправка города]
  // + интерактивная поправка) в конкретный (город, дата, слот) — ищется
  // динамически среди ВСЕХ ставок Polymarket этого снапшота (BUCKET_PRICES), а
  // не берётся готовой из строки. BUCKET_PRICES общий для обоих режимов
  // raw/effective (набор ставок рынка не зависит от того, как посчитан
  // прогнозный максимум).
  function lookupPrice(icao, date, slotKey, targetBucket) {{
    const byDate = (BUCKET_PRICES[icao] || {{}})[date];
    if (!byDate) return null;
    const bySlot = byDate[slotKey];
    if (!bySlot) return null;
    for (const [lowB, highB, price] of bySlot) {{
      const loOk = (lowB === null) || (targetBucket >= lowB);
      const hiOk = (highB === null) || (targetBucket <= highB);
      if (loOk && hiOk) return price;
    }}
    return null;
  }}

  // Отсутствие цены на сдвинутый таргет (или цена вне диапазона при активном
  // фильтре) означает, что этот день ИСКЛЮЧАЕТСЯ из подсчёта попаданий/
  // промахов/череды и, как и день без записи в DAILY_ROWS, НЕ рвёт серию промахов:
  // такой день просто пропускается, как будто его не было МЕЖДУ соседними промахнутыми днями.
  function rowIncluded(price, priceActive, priceMin, priceMax) {{
    if (!priceActive) return true;
    if (price === null) return false;
    return price >= priceMin && price <= priceMax;
  }}

  // Рынок "уже решён": самый дорогой бакет снапшота стоит >= DECIDED_PRICE_CENTS, и это НЕ бакет
  // ставки (targetBucket) -- ставка заведомо проигрышная (~0.1c), бот её не открывает (см.
  // price_monitor.decided_market_price_cents). Если ставка именно на самый дорогой бакет -- не блокируем.
  function marketDecidedElsewhere(icao, date, slotKey, targetBucket) {{
    if (!DECIDED_PRICE_CENTS) return false;
    const bySlot = (((BUCKET_PRICES[icao] || {{}})[date]) || {{}})[slotKey];
    if (!bySlot || !bySlot.length) return false;
    let top = null;
    for (const e of bySlot) {{
      if (top === null || e[2] > top[2]) top = e;
    }}
    if (top[2] < DECIDED_PRICE_CENTS) return false;
    const inTop = (top[0] === null || targetBucket >= top[0]) && (top[1] === null || targetBucket <= top[1]);
    return !inTop;
  }}

  // Причина, по которой реальный бот НЕ открыл бы позицию (None -- ставка допустима):
  // цена < min_bet_price_cents (5c) либо рынок уже решён на другом бакете (>= 95c).
  // Такой день в бэктесте и в панели галочек/крестиков не считается ни попаданием, ни промахом
  // (как и у бота: нет позиции -- нет счётчика промахов пирамиды).
  function betBlockReason(icao, date, slotKey, targetBucket, price) {{
    if (price === null) return null;
    if (MIN_BET_PRICE_CENTS && price < MIN_BET_PRICE_CENTS - 1e-9) {{
      return "цена ниже " + MIN_BET_PRICE_CENTS + "\\u00a2 \\u2014 не покупаем";
    }}
    if (marketDecidedElsewhere(icao, date, slotKey, targetBucket)) {{
      return "рынок уже решён (самый дорогой бакет \\u2265 " + DECIDED_PRICE_CENTS + "\\u00a2) \\u2014 не покупаем";
    }}
    return null;
  }}

  // Лот не меньше min_order_usd / цена (минимальная сумма ордера Polymarket), округление вверх до 0.01.
  function floorLot(lot, price) {{
    if (!MIN_ORDER_USD || !(price > 0)) return lot;
    return Math.max(lot, Math.ceil(MIN_ORDER_USD / price * 100 - 1e-9) / 100);
  }}

  function computeCityMetrics(rows, correction, priceActive, priceMin, priceMax, icao, slotKey) {{
    let hitsCount = 0, consideredCount = 0;
    let longest = 0, current = 0;
    const streakLengths = [];
    rows.forEach(r => {{
      const [dateStr, error, forecastBucket] = r;
      // Дни без записи (нет прогноза/факта/цены) и дни, отфильтрованные по цене/правилам бота, серию
      // промахов НЕ рвут: считаем только дни, по которым была ставка, подряд друг за другом.
      const price = lookupPrice(icao, dateStr, slotKey, forecastBucket + correction);
      if (!rowIncluded(price, priceActive, priceMin, priceMax)
          || betBlockReason(icao, dateStr, slotKey, forecastBucket + correction, price) !== null) {{
        return; // отфильтровано по цене/правилам бота — пропускаем, серию НЕ рвём
      }}

      consideredCount += 1;
      const hit = (error + correction) === 0;
      if (hit) {{
        hitsCount += 1;
        if (current > 0) streakLengths.push(current);
        current = 0;
      }} else {{
        current += 1;
      }}
      longest = Math.max(longest, current);
    }});
    if (current > 0) streakLengths.push(current);
    return {{
      streak: consideredCount ? longest : null,
      hitrate: consideredCount ? (100 * hitsCount / consideredCount) : null,
      n: consideredCount, streakLengths,
    }};
  }}

  function buildOrder(metricsByCity, key, ascending) {{
    // Сортировка по Wunderground (лучшее -> худшее); Windy — подстраховка для
    // городов без данных по Wunderground. Город БЕЗ ДАННЫХ ни по одному
    // источнику за это окно — как и в исходной Python-версии — в рейтинг не
    // попадает вовсе (не показывается даже прочерком).
    const withVal = [];
    const leftover = [];
    ALL_CITIES.forEach(city => {{
      const m = metricsByCity[city] || {{}};
      const wu = m.wunderground, windy = m.windy;
      const hasAnyData = (wu && wu.n > 0) || (windy && windy.n > 0);
      if (!hasAnyData) return;
      const val = (wu && wu[key] !== null && wu[key] !== undefined) ? wu[key] : (windy ? windy[key] : null);
      if (val !== null && val !== undefined) {{
        withVal.push([city, val]);
      }} else {{
        leftover.push(city);
      }}
    }});
    withVal.sort((a, b) => ascending ? a[1] - b[1] : b[1] - a[1]);
    leftover.sort();
    return withVal.map(x => x[0]).concat(leftover);
  }}

  function computeAllMetrics(slotKey, periodKey) {{
    const mode = currentMode();
    const selectedEras = getSelectedEras();
    const window = (PERIOD_WINDOWS[slotKey] || {{}})[periodKey];
    const metricsByCity = {{}};
    const allStreakLengths = {{ wunderground: [], windy: [] }};
    if (!window) return {{ metricsByCity, allStreakLengths, window: null }};
    const [wStart, wEnd] = window;
    const startTs = Date.parse(wStart + "T00:00:00Z");
    const endTs = Date.parse(wEnd + "T00:00:00Z");

    ALL_CITIES.forEach(city => {{
      const icao = (CITY_LINKS[city] || {{}}).icao;
      const {{ correction, priceActive, priceMin, priceMax }} = getCityFilter(city);
      const cityRows = ((DAILY_ROWS[mode] || {{}})[slotKey] || {{}})[city] || {{}};
      const cityMetrics = {{}};
      ["wunderground", "windy"].forEach(source => {{
        // "Старые"/"Новые" — чистая календарная граница (см. getSelectedEras),
        // не "прозрачный пропуск" как у цены: снятая эра УБИРАЕТ строки
        // целиком, до расчёта серии — сама граница месяца/эры НЕ порождает
        // ложных разрывов, т.к. исключённые даты идут одним непрерывным
        // куском с одного края, а не вперемешку.
        const rows = (cityRows[source] || []).filter(r => {{
          const ts = Date.parse(r[0] + "T00:00:00Z");
          return ts >= startTs && ts <= endTs && selectedEras.includes(r[3]);
        }});
        const m = computeCityMetrics(rows, correction, priceActive, priceMin, priceMax, icao, slotKey);
        cityMetrics[source] = m;
        if (m.streakLengths.length) {{
          allStreakLengths[source] = allStreakLengths[source].concat(m.streakLengths);
        }}
      }});
      metricsByCity[city] = cityMetrics;
    }});
    return {{ metricsByCity, allStreakLengths, window }};
  }}

  function renderRankChart(divId, order, metricsByCity, key, title, ytitle) {{
    const traces = ["wunderground", "windy"].map(source => ({{
      x: order,
      y: order.map(c => {{ const m = (metricsByCity[c] || {{}})[source]; return m ? m[key] : null; }}),
      type: "bar", name: SOURCE_LABELS_LOCAL[source], marker: {{ color: SOURCE_COLORS_LOCAL[source] }},
    }}));
    Plotly.react(divId, traces, {{
      title: {{ text: `<b>${{title}}</b>`, x: 0.02, xanchor: "left" }}, barmode: "group", template: "plotly_white",
      yaxis: {{ title: ytitle }}, xaxis: {{ tickangle: -45 }}, legend: {{ title: {{ text: "Источник" }} }},
    }});
  }}

  function renderDistChart(divId, allStreakLengths, title) {{
    let maxLen = 1;
    ["wunderground", "windy"].forEach(s => {{ allStreakLengths[s].forEach(v => {{ if (v > maxLen) maxLen = v; }}); }});
    const traces = ["wunderground", "windy"].map(source => ({{
      x: allStreakLengths[source], type: "histogram", name: SOURCE_LABELS_LOCAL[source],
      marker: {{ color: SOURCE_COLORS_LOCAL[source] }}, opacity: 0.9,
      xbins: {{ start: 0.5, end: maxLen + 0.5, size: 1 }},
    }}));
    Plotly.react(divId, traces, {{
      title: {{ text: `<b>${{title}}</b>`, x: 0.02, xanchor: "left" }}, barmode: "overlay", bargap: 0.05, template: "plotly_white",
      xaxis: {{ title: "Длина череды промахов в днях", dtick: 1 }}, yaxis: {{ title: "Количество таких серий" }},
      legend: {{ title: {{ text: "Источник" }} }},
    }});
  }}

  function renderTable(divId, order, metricsByCity, key, fmt) {{
    const extraHeader = WATCHED_MODE ? "<th>Поправка</th><th>Мин. цена</th><th>Макс. цена</th>" : "";
    const rows = order.map(city => {{
      const links = CITY_LINKS[city] || {{}};
      const m = metricsByCity[city] || {{}};
      const wuText = fmt(m.wunderground ? m.wunderground[key] : null);
      const wiText = fmt(m.windy ? m.windy[key] : null);
      const wuCell = (links.wu_url && wuText !== "—")
        ? `<a href="${{links.wu_url}}" target="_blank" rel="noopener">${{wuText}}</a>` : wuText;
      const wiCell = (links.windy_url && wiText !== "—")
        ? `<a href="${{links.windy_url}}" target="_blank" rel="noopener">${{wiText}}</a>` : wiText;
      const polyCell = links.poly_url
        ? `<a href="${{links.poly_url}}" target="_blank" rel="noopener">${{links.poly_label}}</a>` : "—";
      let extraCells = "";
      if (WATCHED_MODE) {{
        const cfg = CITY_CORRECTIONS[city] || {{}};
        const corrText = cfg.corr ? fmtSigned(cfg.corr) : "0";
        const minPriceText = (cfg.min_price === null || cfg.min_price === undefined) ? "—" : `${{cfg.min_price}}¢`;
        const maxPriceText = (cfg.max_price === null || cfg.max_price === undefined) ? "—" : `${{cfg.max_price}}¢`;
        extraCells = `<td>${{corrText}}</td><td>${{minPriceText}}</td><td>${{maxPriceText}}</td>`;
      }}
      return `<tr><td>${{city}}</td><td>${{wuCell}}</td><td>${{wiCell}}</td><td>${{polyCell}}</td>${{extraCells}}</tr>`;
    }}).join("");
    document.getElementById(divId).innerHTML =
      "<table class='rank-table'><thead><tr><th>Город</th><th>Wunderground</th>" +
      "<th>Windy</th><th>Polymarket</th>" + extraHeader + "</tr></thead><tbody>" + rows + "</tbody></table>";
  }}

  function dayMarkClass(v) {{
    if (v === true) return "streak-day-hit";
    if (v === false) return "streak-day-miss";
    return "streak-day-none";
  }}
  function dayMarkText(v) {{
    if (v === true) return "\\u2713";
    if (v === false) return "\\u2717";
    return "\\u2013";
  }}
  function dayPriceText(p) {{
    return (p === null || p === undefined) ? "\\u2013" : `${{p.toFixed(1)}}\\u00a2`;
  }}

  function renderDetail(city, slotKey, periodKey) {{
    lastCity = city;
    const mode = currentMode();
    const selectedEras = getSelectedEras();
    const icao = (CITY_LINKS[city] || {{}}).icao;
    const {{ correction, priceActive, priceMin, priceMax }} = getCityFilter(city);
    const window = (PERIOD_WINDOWS[slotKey] || {{}})[periodKey];
    let heading = "Попадания и промахи \\u2014 " + [city, SLOT_LABELS[slotKey] || slotKey, PERIOD_LABELS[periodKey] || periodKey].join(", ");
    if (WATCHED_MODE) {{
      const cfg = CITY_CORRECTIONS[city];
      if (cfg) {{
        const parts = [];
        if (cfg.corr) parts.push(`поправка ${{fmtSigned(cfg.corr)}}`);
        if (cfg.min_price !== null && cfg.min_price !== undefined) parts.push(`мин. цена ${{cfg.min_price}}¢`);
        if (cfg.max_price !== null && cfg.max_price !== undefined) parts.push(`макс. цена ${{cfg.max_price}}¢`);
        if (cfg.comment) parts.push(cfg.comment);
        if (parts.length) heading += ` (${{parts.join(", ")}})`;
      }}
    }}
    if (!window) {{
      detailPanel.innerHTML = `<h4>${{heading}}</h4><p style='color:#888;'>Нет данных за выбранный период.</p>`;
      return;
    }}
    const dateStrs = dateRange(window[0], window[1]);
    const cityRows = ((DAILY_ROWS[mode] || {{}})[slotKey] || {{}})[city] || {{}};

    // День сразу ПОСЛЕ даты отсечки (DATA_ERA_CUTOFF_DATE) — перед ним
    // вставляется визуальный разделитель "старые | новые" (см. CSS
    // .streak-era-separator), независимо от того, какие галочки сейчас
    // отмечены — граница показывается всегда, если попадает в отображаемый
    // диапазон дат.
    const cutoffTs = Date.parse(DATA_ERA_CUTOFF_DATE + "T00:00:00Z");
    const nextDayAfterCutoff = new Date(cutoffTs + 86400000).toISOString().slice(0, 10);
    const separatorHtml = `<div class="streak-era-separator">
      <span class="streak-era-label streak-era-label-old">старые</span>
      <span class="streak-era-label streak-era-label-new">новые</span>
    </div>`;

    const rows = ["wunderground", "windy"].map(source => {{
      const byDate = {{}};
      (cityRows[source] || []).forEach(r => {{ byDate[r[0]] = r; }});
      let days = "";
      dateStrs.forEach(d => {{
        if (d === nextDayAfterCutoff) {{
          days += separatorHtml;
        }}
        const r = byDate[d];
        let mark = null, price = null, blockReason = null;
        // Не прошедший фильтр эры день — прочерк везде (как маркер, так и
        // цена — этого дня в этом режиме как будто нет вовсе). Не прошедший
        // фильтр ЦЕНЫ день — прочерк ТОЛЬКО у маркера (не считается ни
        // попаданием, ни промахом), но сама цена остаётся видна — чтобы
        // можно было увидеть, что там реально было и почему день отфильтрован.
        if (r && selectedEras.includes(r[3])) {{
          const forecastBucket = r[2];
          price = lookupPrice(icao, d, slotKey, forecastBucket + correction);
          blockReason = betBlockReason(icao, d, slotKey, forecastBucket + correction, price);
          if (rowIncluded(price, priceActive, priceMin, priceMax) && blockReason === null) {{
            mark = (r[1] + correction) === 0;
          }}
        }}
        const shortDate = d.slice(5);
        days += `<div class="streak-day">
          <span class="streak-day-date">${{shortDate}}</span>
          <span class="streak-day-mark ${{dayMarkClass(mark)}}">${{dayMarkText(mark)}}</span>
          <span class="streak-day-price"${{blockReason ? ` title="${{blockReason}}"` : ""}}>${{dayPriceText(price)}}</span>
        </div>`;
      }});
      return `<div class="streak-source-row">
        <div class="streak-source-label">${{SOURCE_LABELS_LOCAL[source]}}</div>
        <div class="streak-days-row">${{days}}</div>
      </div>`;
    }}).join("");
    detailPanel.innerHTML = `<h4>${{heading}}</h4>` + rows;
  }}

  // ================== БЭКТЕСТ ==================
  // Симуляция сделок по историческим данным. Цена сделки — та же, что уже
  // используется для маркера/цены в детальной панели (lookupPrice по
  // BUCKET_PRICES на скорректированный бакет) — то есть реальная цена
  // бакета, на который был бы сигнал в тот день, не средняя/произвольная.
  // Покупаем Yes по этой цене за lotSize акций + комиссия (commission *
  // lotSize). При попадании получаем lotSize (по $1/акция); при промахе —
  // 0. P&L = payout - cost.
  //
  // Пирамида: лот начинается с lotSize, растёт по прогрессии PYRAMID (power/cumulative/custom, см. pyramid.py) при каждом
  // промахе подряд (например lotSize, 3×, 7×, 15× при cumulative m=2) до достижения steps промахов
  // подряд включительно — после чего (или раньше, при попадании) лот
  // сбрасывается обратно к lotSize. Счётчик промахов — ОТДЕЛЬНЫЙ по каждому
  // городу (город = своя независимая последовательность ставок), даже если
  // бэктест агрегирует несколько городов в одну кривую баланса.

  function getBacktestTargetType() {{
    if (WATCHED_MODE) return null; // не используется -- таргет уже зафиксирован самим отчётом
    const radio = document.querySelector('input[name="backtestTarget"]:checked');
    return radio ? radio.value : "forecast";
  }}

  function getBacktestTypeAndSteps(city) {{
    if (!WATCHED_MODE) {{
      const toggle = document.getElementById("backtestTypeToggle");
      const type = toggle && toggle.checked ? "pyramid" : "flat";
      const steps = Math.max(1, parseInt(document.getElementById("backtestSteps").value, 10) || 2);
      return {{ type, steps }};
    }}
    // watched/max_bet отчёт -- Тип/Количество шагов берутся из конфига
    // КОНКРЕТНОГО города (strategy/loss_limit), а не из общего переключателя
    // (которого тут и нет) -- у разных городов они могут отличаться, даже
    // если бэктест сейчас агрегирует несколько городов сразу.
    const cfg = CITY_CORRECTIONS[city] || {{}};
    const type = cfg.strategy === "pyramid" ? "pyramid" : "flat";
    const steps = cfg.loss_limit && cfg.loss_limit > 0 ? cfg.loss_limit : 2;
    return {{ type, steps }};
  }}

  function gatherBacktestRows(cities, slotKey, periodKey) {{
    const window = (PERIOD_WINDOWS[slotKey] || {{}})[periodKey];
    if (!window) return [];
    const [wStart, wEnd] = window;
    const startTs = Date.parse(wStart + "T00:00:00Z");
    const endTs = Date.parse(wEnd + "T00:00:00Z");
    const selectedEras = getSelectedEras();
    const targetType = getBacktestTargetType();
    const mode = currentMode();

    const rows = [];
    cities.forEach(city => {{
      const icao = (CITY_LINKS[city] || {{}}).icao;
      const {{ correction: globalCorrection, priceActive, priceMin, priceMax }} = getCityFilter(city);
      const {{ type, steps }} = getBacktestTypeAndSteps(city);

      let citySeries;
      let correction = globalCorrection;
      if (!WATCHED_MODE && targetType === "maxbet") {{
        // "Максимум" -- нет понятия поправки прогноза (таргет и так "своя
        // самая дорогая ставка рынка"), поэтому поправка игнорируется, даже
        // если бегунок сдвинут.
        citySeries = (DAILY_ROWS_MAXBET[slotKey] || {{}})[city] || [];
        correction = 0;
      }} else {{
        // Источник прогноза для бэктеста: в watched-отчёте -- ИМЕННО тот,
        // что настроен этому городу (weather_source), как у реального
        // PriceMonitor; в основном отчёте единого источника на город нет
        // (город может быть не watched вовсе) -- берём Wunderground.
        const source = WATCHED_MODE
          ? ((CITY_CORRECTIONS[city] || {{}}).weather_source || "wunderground")
          : "wunderground";
        citySeries = (((DAILY_ROWS[mode] || {{}})[slotKey] || {{}})[city] || {{}})[source] || [];
      }}

      citySeries.forEach(r => {{
        const [dateStr, error, forecastBucket, era] = r;
        const ts = Date.parse(dateStr + "T00:00:00Z");
        if (ts < startTs || ts > endTs) return;
        if (!selectedEras.includes(era)) return;

        const price = lookupPrice(icao, dateStr, slotKey, forecastBucket + correction);
        if (price === null) return; // нет цены на этот бакет -- нечего покупать, не сигнал
        if (priceActive && (price < priceMin || price > priceMax)) return; // вне диапазона фильтра -- не сигнал
        // Правила бота: цена < min_bet_price_cents или рынок уже решён на другом бакете -- позиции нет
        // (и счётчик промахов пирамиды не меняется, как у реального бота).
        if (betBlockReason(icao, dateStr, slotKey, forecastBucket + correction, price) !== null) return;

        rows.push({{
          date: dateStr, city, hit: (error + correction) === 0,
          price: price / 100, // центы -> доли доллара (45c -> 0.45)
          type, steps,
        }});
      }});
    }});

    rows.sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : a.city.localeCompare(b.city)));
    return rows;
  }}

  function runBacktest(rows, lotSize, commission, startBalance) {{
    // equityCurve -- ОДНА точка на ДАТУ (не на сделку): если в один день
    // сигнал сработал у нескольких городов сразу, их P&L суммируется в ОДНУ
    // точку баланса на эту дату (см. ось Х = Дата, п.2 задачи). entries —
    // разбивка по городам ВНУТРИ этой даты (город + его P&L) — нужна для
    // подсказки при наведении.
    const perCityMiss = {{}};
    let balance = startBalance;
    const equityCurve = [{{ date: null, balance, entries: [] }}]; // "Старт"
    let peak = balance;
    let maxDrawdown = 0;
    let maxDrawdownPct = 0;

    let i = 0;
    while (i < rows.length) {{
      const date = rows[i].date;
      const entries = [];
      while (i < rows.length && rows[i].date === date) {{
        const r = rows[i];
        let lot = lotSize;
        if (r.type === "pyramid") {{
          const misses = perCityMiss[r.city] || 0;
          lot = lotSize * pyramidFactor(misses);
        }}
        // пол по минимальной сумме ордера (min_order_usd): как у бота (_compute_lot) -- после пирамиды
        lot = floorLot(lot, r.price);
        const cost = r.price * lot + commission * lot;
        const payout = r.hit ? lot : 0;
        const pnl = payout - cost;
        balance += pnl;
        entries.push({{ city: r.city, pnl }});

        if (r.type === "pyramid") {{
          const misses = perCityMiss[r.city] || 0;
          perCityMiss[r.city] = (r.hit || misses + 1 >= r.steps) ? 0 : misses + 1;
        }}
        i++;
      }}
      equityCurve.push({{ date, balance, entries }});

      if (balance > peak) peak = balance;
      const dd = peak - balance;
      if (dd > maxDrawdown) {{
        maxDrawdown = dd;
        maxDrawdownPct = peak !== 0 ? (dd / peak) * 100 : 0;
      }}
    }}

    const finalPnl = balance - startBalance;
    const finalPnlPct = startBalance !== 0 ? (finalPnl / startBalance) * 100 : 0;
    return {{
      equityCurve, finalBalance: balance, maxDrawdown, maxDrawdownPct,
      finalPnl, finalPnlPct, tradeCount: rows.length,
    }};
  }}

  // ---- Фактическая торговля (LIVE_TRADES) -- реальные, УЖЕ СОВЕРШЁННЫЕ
  // сделки авто-торговли (TradingEngine), а не симуляция. Только для
  // отчётов по стратегиям (watched/max_bet) -- LIVE_TRADES пуст в основном
  // отчёте (см. build_rating_html(live_strategy_set=...)). ----

  function gatherLiveTrades(cities, slotKey, periodKey) {{
    const window = (PERIOD_WINDOWS[slotKey] || {{}})[periodKey];
    if (!window) return [];
    const [wStart, wEnd] = window;
    const startTs = Date.parse(wStart + "T00:00:00Z");
    const endTs = Date.parse(wEnd + "T00:00:00Z");

    const rows = [];
    cities.forEach(city => {{
      (LIVE_TRADES[city] || []).forEach(([dateStr, pnl]) => {{
        const ts = Date.parse(dateStr + "T00:00:00Z");
        if (ts < startTs || ts > endTs) return;
        rows.push({{ date: dateStr, city, pnl }});
      }});
    }});
    rows.sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : a.city.localeCompare(b.city)));
    return rows;
  }}

  function runLiveEquity(rows, startBalance) {{
    // Тот же формат equityCurve, что и runBacktest() -- одна точка на дату,
    // с разбивкой по городам в entries -- НО баланс здесь считается по
    // РЕАЛЬНОМУ pnl_net сделки, без пересчёта лота/цены/комиссии (те уже
    // "зашиты" в pnl_net на момент разрешения позиции ботом).
    let balance = startBalance;
    const equityCurve = [{{ date: null, balance, entries: [] }}];
    let i = 0;
    while (i < rows.length) {{
      const date = rows[i].date;
      const entries = [];
      while (i < rows.length && rows[i].date === date) {{
        const r = rows[i];
        balance += r.pnl;
        entries.push({{ city: r.city, pnl: r.pnl }});
        i++;
      }}
      equityCurve.push({{ date, balance, entries }});
    }}
    const finalPnl = balance - startBalance;
    const finalPnlPct = startBalance !== 0 ? (finalPnl / startBalance) * 100 : 0;
    return {{ equityCurve, finalBalance: balance, finalPnl, finalPnlPct, tradeCount: rows.length }};
  }}

  // Цвета линии -- та же пара, что уже используется в проекте: #e74c3c
  // (красный, столбцы Wunderground на остальных графиках) и #27ae60
  // (зелёный из той же "плоской" цветовой палитры, для парности).
  const BACKTEST_COLOR_UP = "#27ae60";
  const BACKTEST_COLOR_DOWN = "#e74c3c";

  function smoothSeries(pts, perSeg) {{
    // Сглаживание кубическим эрмитовым сплайном (Catmull-Rom: наклон в точке =
    // разность соседей) по ВСЕЙ кривой сразу -- в отличие от shape:"spline" Plotly,
    // который сглаживает каждую трассу отдельно, из-за чего короткие
    // красные/синие участки (2 точки) выходили прямыми. x -- числовая позиция
    // категории, строго возрастающая. Возвращает плотный список точек {{x, y}}.
    const sorted = pts.slice().sort((a, b) => a.x - b.x);
    const uniq = [];
    sorted.forEach(p => {{
      if (uniq.length && uniq[uniq.length - 1].x === p.x) uniq[uniq.length - 1] = p; else uniq.push(p);
    }});
    const n = uniq.length;
    if (n < 2) return uniq;
    const m = new Array(n);
    for (let i = 0; i < n; i++) {{
      const a = uniq[Math.max(0, i - 1)], b = uniq[Math.min(n - 1, i + 1)];
      m[i] = (b.y - a.y) / (b.x - a.x);
    }}
    const dense = [];
    for (let i = 0; i < n - 1; i++) {{
      const p0 = uniq[i], p1 = uniq[i + 1], h = p1.x - p0.x;
      for (let k = 0; k < perSeg; k++) {{
        const t = k / perSeg, t2 = t * t, t3 = t2 * t;
        const y = (2 * t3 - 3 * t2 + 1) * p0.y + (t3 - 2 * t2 + t) * h * m[i]
                + (-2 * t3 + 3 * t2) * p1.y + (t3 - t2) * h * m[i + 1];
        dense.push({{ x: p0.x + t * h, y: y }});
      }}
    }}
    dense.push({{ x: uniq[n - 1].x, y: uniq[n - 1].y }});
    return dense;
  }}

  function buildSmoothSegments(pts, thresholdY, upColor, downColor) {{
    // Плотная сглаженная кривая, раскрашенная выше/ниже стартового баланса;
    // смена цвета -- ровно в точке пересечения со стартовым уровнем, а не в
    // ближайшей точке данных. Каждый сегмент (кроме первого) начинается с
    // точки пересечения предыдущего, чтобы линия не рвалась.
    const up = upColor || BACKTEST_COLOR_UP;
    const down = downColor || BACKTEST_COLOR_DOWN;
    const colorFor = y => (y >= thresholdY ? up : down);
    const dense = smoothSeries(pts, 16);
    if (!dense.length) return [];
    const segments = [];
    let segColor = colorFor(dense[0].y);
    let xs = [dense[0].x], ys = [dense[0].y];
    for (let i = 1; i < dense.length; i++) {{
      const prev = dense[i - 1], cur = dense[i];
      const c = colorFor(cur.y);
      if (c !== segColor) {{
        const t = (thresholdY - prev.y) / (cur.y - prev.y);
        const cx = prev.x + t * (cur.x - prev.x);
        xs.push(cx); ys.push(thresholdY);
        segments.push({{ color: segColor, xs: xs, ys: ys }});
        segColor = c;
        xs = [cx]; ys = [thresholdY];
      }}
      xs.push(cur.x); ys.push(cur.y);
    }}
    segments.push({{ color: segColor, xs: xs, ys: ys }});
    return segments;
  }}

  function buildBacktestHoverText(point) {{
    if (point.date === null) {{
      return `Старт<br>Баланс: $${{point.balance.toFixed(2)}}`;
    }}
    const lines = [point.date, `Баланс: $${{point.balance.toFixed(2)}}`];
    if (point.entries.length > 1) {{
      const totalPnl = point.entries.reduce((sum, e) => sum + e.pnl, 0);
      const sign = totalPnl >= 0 ? "+" : "";
      const summaryLine = `${{point.entries.length}} ${{pluralCities(point.entries.length)}}: ${{sign}}$${{totalPnl.toFixed(2)}}`;
      lines.push(summaryLine);
      if (WATCHED_MODE) {{
        // Отчёты по стратегиям (watched/max_bet) -- городов немного, список
        // читаем целиком: сверху сводная строка (под датой и балансом), ниже
        // -- построчно по каждому городу.
        point.entries.forEach(e => {{
          const eSign = e.pnl >= 0 ? "+" : "";
          lines.push(`${{e.city}}: ${{eSign}}$${{e.pnl.toFixed(2)}}`);
        }});
      }}
      // Основной отчёт -- городов десятки, построчный список нечитаем (и,
      // судя по всему, вообще ломает показ подсказки у Plotly) -- там
      // остаётся только сводная строка выше.
    }} else {{
      point.entries.forEach(e => {{
        const sign = e.pnl >= 0 ? "+" : "";
        lines.push(`${{e.city}}: ${{sign}}$${{e.pnl.toFixed(2)}}`);
      }});
    }}
    return lines.join("<br>");
  }}

  function pluralCities(n) {{
    const mod10 = n % 10, mod100 = n % 100;
    if (mod10 === 1 && mod100 !== 11) return "город";
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return "города";
    return "городов";
  }}

  // Цвет РЕАЛЬНОЙ (уже совершённой) торговли -- ОДИН, не завязан на
  // выше/ниже старта (в отличие от симуляции с BACKTEST_COLOR_UP/DOWN) --
  // так линия факта визуально сразу отличается от симуляции, даже не
  // читая легенду.
  const LIVE_TRADES_COLOR = "#2980b9";

  function buildLiveHoverText(point) {{
    if (point.date === null) {{
      return `Старт (факт)<br>Баланс: $${{point.balance.toFixed(2)}}`;
    }}
    const lines = [`${{point.date}} (факт)`, `Баланс: $${{point.balance.toFixed(2)}}`];
    if (point.entries.length > 1) {{
      const totalPnl = point.entries.reduce((sum, e) => sum + e.pnl, 0);
      const sign = totalPnl >= 0 ? "+" : "";
      const summaryLine = `${{point.entries.length}} ${{pluralCities(point.entries.length)}}: ${{sign}}$${{totalPnl.toFixed(2)}}`;
      lines.push(summaryLine);
      if (WATCHED_MODE) {{
        // Как и у бэктеста -- построчный список по городу читаем только в
        // watched/max_bet отчётах (немного городов); в основном отчёте
        // остаётся только сводная строка выше.
        point.entries.forEach(e => {{
          const eSign = e.pnl >= 0 ? "+" : "";
          lines.push(`${{e.city}}: ${{eSign}}$${{e.pnl.toFixed(2)}}`);
        }});
      }}
    }} else {{
      point.entries.forEach(e => {{
        const sign = e.pnl >= 0 ? "+" : "";
        lines.push(`${{e.city}}: ${{sign}}$${{e.pnl.toFixed(2)}}`);
      }});
    }}
    return lines.join("<br>");
  }}

  function renderBacktestChart(result, liveResult, startBalance, lotSize) {{
    const statsDiv = document.getElementById("backtestStats");
    const resetBtn = document.getElementById("backtestResetCity");
    const hasBacktest = result.tradeCount > 0;
    const hasLive = !!liveResult && liveResult.tradeCount > 0;

    if (!hasBacktest && !hasLive) {{
      Plotly.purge("backtestChart");
      document.getElementById("backtestChart").innerHTML = "";
      statsDiv.innerHTML = "<p style='color:#888;'>Нет сигналов за выбранный период с текущими настройками.</p>";
      statsDiv.appendChild(resetBtn);
      return;
    }}

    let statsHtml = "";
    if (hasBacktest) {{
      const pnlSign = result.finalPnl >= 0 ? "+" : "";
      const pnlClass = result.finalPnl >= 0 ? "streak-day-hit" : "streak-day-miss";
      statsHtml += `
        <div class="backtest-stats-row">
          <span class="backtest-stats-label">Бэктест:</span>
          <span>Сделок: <b>${{result.tradeCount}}</b></span>
          <span>Баланс: <b>$${{result.finalBalance.toFixed(2)}}</b></span>
          <span>P&amp;L: <b class="${{pnlClass}}">${{pnlSign}}$${{result.finalPnl.toFixed(2)}} (${{pnlSign}}${{result.finalPnlPct.toFixed(1)}}%)</b></span>
          <span>Макс. просадка: <b>$${{result.maxDrawdown.toFixed(2)}} (${{result.maxDrawdownPct.toFixed(1)}}%)</b></span>
        </div>`;
    }}
    if (hasLive) {{
      const pnlSign = liveResult.finalPnl >= 0 ? "+" : "";
      const pnlClass = liveResult.finalPnl >= 0 ? "streak-day-hit" : "streak-day-miss";
      statsHtml += `
        <div class="backtest-stats-row">
          <span class="backtest-stats-label">Факт:</span>
          <span>Сделок: <b>${{liveResult.tradeCount}}</b></span>
          <span>Баланс: <b>$${{liveResult.finalBalance.toFixed(2)}}</b></span>
          <span>P&amp;L: <b class="${{pnlClass}}">${{pnlSign}}$${{liveResult.finalPnl.toFixed(2)}} (${{pnlSign}}${{liveResult.finalPnlPct.toFixed(1)}}%)</b></span>
        </div>`;
    }}
    statsDiv.innerHTML = statsHtml;
    statsDiv.appendChild(resetBtn);

    const traces = [];

    // Ось Х -- "Старт" + даты по порядку (категориальная ось, не числовая
    // и не "дата"-тип, чтобы Plotly не пытался парсить "Старт" как дату).
    // points объявлен вне if(hasBacktest), чтобы live-серия ниже могла
    // найти в нём свою настоящую стартовую позицию (см. п.2).
    let points = [];
    const lineSpecs = [];  // [{{points, up, down, group}}] -- линии equity, строятся после расчёта оси

    if (hasBacktest) {{
      points = result.equityCurve.map(p => ({{
        x: p.date === null ? "Старт" : p.date, y: p.balance, raw: p,
      }}));
      // Линии строятся ПОСЛЕ расчёта оси (см. lineSpecs ниже): им нужны
      // числовые позиции категорий, чтобы сгладить кривую целиком.
      lineSpecs.push({{ points: points, up: undefined, down: undefined, group: "backtest" }});
      traces.push({{
        x: points.map(p => p.x), y: points.map(p => p.y),
        mode: "markers", type: "scatter",
        marker: {{ size: 7, color: points.map(p => (p.y >= startBalance ? BACKTEST_COLOR_UP : BACKTEST_COLOR_DOWN)) }},
        text: points.map(p => buildBacktestHoverText(p.raw)), hoverinfo: "text",
        showlegend: false, legendgroup: "backtest",
      }});
      // Отдельный "пустой" след ТОЛЬКО ради подписи в легенде -- сама
      // симуляция раскрашена по сегментам (зелёный/красный выше/ниже
      // старта); цвет и значок образца в легенде совпадают с фактически
      // преобладающим цветом графика (зелёный, линия+точка).
      traces.push({{
        x: [null], y: [null], mode: "lines+markers", type: "scatter",
        line: {{ color: BACKTEST_COLOR_UP, width: 3 }}, marker: {{ size: 7, color: BACKTEST_COLOR_UP }},
        name: "Бэктест (симуляция)",
        showlegend: true, legendgroup: "backtest", hoverinfo: "skip",
      }});
    }}

    if (hasLive) {{
      const livePoints = liveResult.equityCurve.map(p => ({{
        x: p.date === null ? "Старт" : p.date, y: p.balance, raw: p,
      }}));
      // Если у факта есть настоящая (не "Старт") первая точка, найдём для
      // неё место на оси Х бэктеста -- позицию ПЕРЕД ней, а не общий
      // "Старт" всего периода. Иначе, когда факт начинается намного позже
      // начала бэктеста, от нуля через весь график тянется пустая "сопля".
      if (points.length > 0 && livePoints.length > 1 && livePoints[0].x === "Старт") {{
        const categories = points.map(p => p.x);
        const idx = categories.indexOf(livePoints[1].x);
        if (idx > 0) {{
          livePoints[0].x = categories[idx - 1];
        }}
      }}
      // Факт красят синим выше старта (фирменный цвет факта) и красным
      // ниже -- как у бэктеста, но с синим вместо зелёного для "выше".
      lineSpecs.push({{ points: livePoints, up: LIVE_TRADES_COLOR, down: BACKTEST_COLOR_DOWN, group: "live" }});
      traces.push({{
        x: livePoints.map(p => p.x), y: livePoints.map(p => p.y),
        mode: "markers", type: "scatter",
        marker: {{ size: 7, color: livePoints.map(p => (p.y >= startBalance ? LIVE_TRADES_COLOR : BACKTEST_COLOR_DOWN)) }},
        text: livePoints.map(p => buildLiveHoverText(p.raw)), hoverinfo: "text",
        showlegend: false, legendgroup: "live",
      }});
      traces.push({{
        x: [null], y: [null], mode: "lines+markers", type: "scatter",
        line: {{ color: LIVE_TRADES_COLOR, width: 3 }}, marker: {{ size: 7, color: LIVE_TRADES_COLOR }},
        name: "Фактическая торговля", showlegend: true, legendgroup: "live", hoverinfo: "skip",
      }});
    }}

    const scopeLabel = lastCity ? lastCity : "Все города";
    const title = `<b>Бэктест — ${{scopeLabel}}, Лот $${{lotSize}}, Баланс $${{startBalance}}</b>`;

    // Ось Х категориальная (type: "category") -- БЕЗ явного categoryarray
    // Plotly сортирует категории по порядку ПЕРВОГО ПОЯВЛЕНИЯ среди трасс
    // (не по значению!). Бэктест и факт могут иметь РАЗНЫЕ наборы дат
    // (бэктест пропускает дни без сигнала/цены -- см. gatherBacktestRows,
    // факт -- только дни реальных сделок): если факт вводит дату, которой
    // ещё не было в категориях бэктеста, Plotly добавляет её В КОНЕЦ оси —
    // визуально "дата из прошлого" оказывается правее всех остальных. Чтобы
    // категории всегда шли по возрастанию даты независимо от того, из какой
    // трассы каждая дата впервые встретилась, строим categoryarray явно:
    // "Старт" (если есть) первым, дальше все уникальные даты по возрастанию
    // (сравнение строк "YYYY-MM-DD" лексикографически == хронологически).
    const seenDates = new Set();
    let hasStart = false;
    traces.forEach(t => (t.x || []).forEach(v => {{
      if (v === null || v === undefined) return;
      if (v === "Старт") {{ hasStart = true; return; }}
      seenDates.add(v);
    }}));
    const categoryArray = (hasStart ? ["Старт"] : []).concat(Array.from(seenDates).sort());

    // Ось Х -- ЧИСЛОВАЯ (позиции категорий 0..n-1) с подписями дат через
    // tickvals/ticktext: так линию можно рисовать плотной сглаженной кривой с
    // дробными x (на категориальной оси их нет). Маркеры остаются в целых
    // позициях. Подписи прореживаем, чтобы они не слипались на длинном периоде.
    const catIndex = new Map(categoryArray.map((c, i) => [c, i]));
    traces.forEach(t => {{
      if (t.x && t.x.length && t.x[0] !== null && t.x[0] !== undefined) t.x = t.x.map(v => catIndex.get(v));
    }});
    const lineTraces = [];
    lineSpecs.forEach(spec => {{
      const pts = spec.points
        .map(p => ({{ x: catIndex.get(p.x), y: p.y }}))
        .filter(p => p.x !== undefined);
      buildSmoothSegments(pts, startBalance, spec.up, spec.down).forEach(seg => lineTraces.push({{
        x: seg.xs, y: seg.ys, mode: "lines", type: "scatter", line: {{ color: seg.color, width: 3 }},
        hoverinfo: "skip", showlegend: false, legendgroup: spec.group,
      }}));
    }});
    const allTraces = lineTraces.concat(traces);
    const tickStep = Math.max(1, Math.ceil(categoryArray.length / 24));
    const tickVals = [], tickText = [];
    categoryArray.forEach((c, i) => {{
      if (i % tickStep === 0 || i === categoryArray.length - 1) {{ tickVals.push(i); tickText.push(c); }}
    }});

    Plotly.react("backtestChart", allTraces, {{
      title: {{ text: title, x: 0.02, xanchor: "left" }},
      template: "plotly_white",
      paper_bgcolor: "#fff", plot_bgcolor: "#fff",
      margin: {{ t: 50, r: 20, b: 90, l: 60 }},
      xaxis: {{
        title: "Дата", type: "linear", tickangle: -45, fixedrange: true,
        tickmode: "array", tickvals: tickVals, ticktext: tickText,
        range: [-0.5, Math.max(categoryArray.length - 1, 1) + 0.5], zeroline: false,
      }},
      yaxis: {{ title: "Баланс в $", fixedrange: true }},
      dragmode: false,
      shapes: [{{
        type: "line", x0: 0, x1: 1, xref: "paper", y0: startBalance, y1: startBalance,
        line: {{ color: "#999", width: 1, dash: "dot" }},
      }}],
      height: 340,
    }}, {{ responsive: true, displayModeBar: false, scrollZoom: false }});
  }}

  function renderBacktest(slotKey, periodKey) {{
    const cities = lastCity ? [lastCity] : ALL_CITIES.slice();
    const resetBtn = document.getElementById("backtestResetCity");
    if (!cities.length) {{
      Plotly.purge("backtestChart");
      document.getElementById("backtestChart").innerHTML = "";
      document.getElementById("backtestStats").innerHTML = "<p style='color:#888;'>Нет городов для бэктеста.</p>";
      document.getElementById("backtestStats").appendChild(resetBtn);
      return;
    }}
    const lotSize = Math.max(5, Number(document.getElementById("backtestLotSize").value) || 5);
    const commission = Math.max(0, Number(document.getElementById("backtestCommission").value) || 0);
    const startBalance = Math.max(0, Number(document.getElementById("backtestBalance").value) || 0);

    const rows = gatherBacktestRows(cities, slotKey, periodKey);
    const result = runBacktest(rows, lotSize, commission, startBalance);

    const liveRows = gatherLiveTrades(cities, slotKey, periodKey);
    const liveResult = runLiveEquity(liveRows, startBalance);

    renderBacktestChart(result, liveResult, startBalance, lotSize);
  }}

  function resetBacktestCity() {{
    lastCity = null;
    document.getElementById("cityStreakDetail").innerHTML =
      '<p class="streak-hint">Кликните по столбцу города на графике «Самая длинная череда промахов», чтобы увидеть детализацию попаданий и промахов за выбранный период.</p>';
    render();
  }}

  function render() {{
    const slotKey = slotSel.value, periodKey = periodSel.value;
    if (!WATCHED_MODE) {{
      correctionValueLabel.textContent = fmtSigned(Number(correctionRange.value));
    }}

    renderBacktest(slotKey, periodKey);

    const slotLabel = SLOT_LABELS[slotKey] || slotKey;
    const periodLabel = PERIOD_LABELS[periodKey] || periodKey;
    const {{ metricsByCity, allStreakLengths, window }} = computeAllMetrics(slotKey, periodKey);

    if (!window) {{
      ["ratingStreakChart", "ratingHitrateChart", "ratingStreakDist"].forEach(id => {{
        Plotly.purge(id); document.getElementById(id).innerHTML = "";
      }});
      document.getElementById("ratingStreakTable").innerHTML = `<p>Нет данных для ${{slotLabel.toLowerCase()}}.</p>`;
      document.getElementById("ratingHitrateTable").innerHTML = "";
      return;
    }}

    const streakOrder = buildOrder(metricsByCity, "streak", true);
    const hitrateOrder = buildOrder(metricsByCity, "hitrate", false);

    renderRankChart("ratingStreakChart", streakOrder, metricsByCity, "streak",
      `Самая длинная череда промахов — ${{slotLabel}}, ${{periodLabel}}`, "Промахов подряд");
    renderDistChart("ratingStreakDist", allStreakLengths,
      `Распределение длин череды промахов — ${{slotLabel}}, ${{periodLabel}}`);
    renderRankChart("ratingHitrateChart", hitrateOrder, metricsByCity, "hitrate",
      `Процент попаданий — ${{slotLabel}}, ${{periodLabel}}`, "% попаданий");
    renderTable("ratingStreakTable", streakOrder, metricsByCity, "streak", fmtStreak);
    renderTable("ratingHitrateTable", hitrateOrder, metricsByCity, "hitrate", fmtPct);

    if (!streakClickBound) {{
      streakClickBound = true;
      document.getElementById("ratingStreakChart").on("plotly_click", function(data) {{
        if (!data.points || !data.points.length) return;
        const city = data.points[0].x;
        renderDetail(city, slotSel.value, periodSel.value);
        renderBacktest(slotSel.value, periodSel.value);
      }});
    }}

    if (lastCity) {{
      renderDetail(lastCity, slotKey, periodKey);
    }}
  }}

  periodSel.addEventListener("change", render);
  slotSel.addEventListener("change", render);
  if (effectiveMaxCheckbox) {{
    effectiveMaxCheckbox.addEventListener("change", render);
  }}
  eraOldCheckbox.addEventListener("change", render);
  eraNewCheckbox.addEventListener("change", render);
  if (!WATCHED_MODE) {{
    correctionRange.addEventListener("input", render);
    priceMinInput.addEventListener("change", render);
    priceMaxInput.addEventListener("change", render);
  }}

  // Настройки бэктеста -- пересчитывают только сам бэктест (не весь
  // render(), чтобы не перестраивать графики/таблицы рейтинга зря).
  const backtestRecompute = () => renderBacktest(slotSel.value, periodSel.value);
  document.getElementById("backtestLotSize").addEventListener("change", backtestRecompute);
  document.getElementById("backtestCommission").addEventListener("change", backtestRecompute);
  document.getElementById("backtestBalance").addEventListener("change", backtestRecompute);
  document.getElementById("backtestResetCity").addEventListener("click", resetBacktestCity);
  if (!WATCHED_MODE) {{
    document.querySelectorAll('input[name="backtestTarget"]').forEach(el => {{
      el.addEventListener("change", backtestRecompute);
    }});
    const stepsInput = document.getElementById("backtestSteps");
    const typeToggle = document.getElementById("backtestTypeToggle");
    function updateStepsEnabled() {{
      stepsInput.disabled = !typeToggle.checked;
    }}
    typeToggle.addEventListener("change", () => {{ updateStepsEnabled(); backtestRecompute(); }});
    stepsInput.addEventListener("change", backtestRecompute);
    updateStepsEnabled();
  }}

  render();
}})();
</script>
"""

        return "<h2></h2>" + controls + detail_panel + backtest_panel + charts_skeleton + rating_script
    def build_html_report(self, dfs, watched=None, show_effective_max=True, embed_maxbet_backtest=False,
                           live_strategy_set=None):
        """dfs: {"raw": df_raw, "effective": df_effective} — см. build_traces/build_rating_html.
        show_effective_max=False — скрывает галочку "1 час с максимумом не
        учитывается" (см. build_rating_html/build_market_target_report).
        embed_maxbet_backtest=True — только для основного отчёта, см.
        build_rating_html. live_strategy_set — прокидывается напрямую в
        build_rating_html, см. её докстринг."""
        if dfs["raw"].empty:
            raise ValueError(
                "Нет пересекающихся данных прогноза и факта — проверь, что "
                "wunderground_forcast/windy_forcast/wunderground_history содержат данные "
                "за одни и те же даты/города."
            )

        (fig, trace_map, regions, city_by_region, all_cities, slot_keys, period_options,
         default_slot_key, display_idxs, error_values, total_traces) = self.build_traces(dfs)
        period_keys = [p[0] for p in period_options]
        period_labels = {p[0]: p[1] for p in period_options}

        err_min = min(int(df["error"].min()) for df in dfs.values())
        err_max = max(int(df["error"].max()) for df in dfs.values())
        prices_all = pd.concat([df["bet_price"] for df in dfs.values()]).dropna()
        price_min_str = f"{prices_all.min():.1f}" if not prices_all.empty else "0.0"
        price_max_str = f"{prices_all.max():.1f}" if not prices_all.empty else "100.0"

        icao_by_city_all = dfs["raw"].drop_duplicates("city").set_index("city")["icao"].to_dict()
        scale_by_icao_all = dfs["raw"].drop_duplicates("icao").set_index("icao")["scale"].to_dict()
        bucket_price_map = self._build_bucket_price_map(scale_by_icao_all)
        city_corrections = self._build_city_corrections(icao_by_city_all, watched)
        is_watched = bool(watched)

        chart_html = fig.to_html(full_html=False, include_plotlyjs="cdn", div_id="chart")
        rating_html = self.build_rating_html(
            dfs, watched=watched, bucket_price_map=bucket_price_map, show_effective_max=show_effective_max,
            embed_maxbet_backtest=embed_maxbet_backtest, live_strategy_set=live_strategy_set,
        )

        page = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Распределение точности прогноза максимума</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Arial, sans-serif; margin: 24px; }}
  .controls {{ margin-bottom: 12px; display: flex; gap: 20px; align-items: center; flex-wrap: wrap; }}
  select {{ padding: 4px 10px; font-size: 14px; }}
  label {{ font-size: 13px; color: #555; margin-right: 6px; }}
  .correction-value {{ display: inline-block; min-width: 28px; text-align: center; font-weight: bold; }}
  .price-input {{ width: 70px; padding: 2px 6px; font-size: 14px; }}
  input[type="range"] {{ vertical-align: middle; }}
  .checkbox-filters {{ align-items: flex-start; }}
  .checkbox-item-master {{ font-weight: bold; border-bottom: 1px solid #ddd; padding-bottom: 4px; margin-bottom: 3px; }}
  .checkbox-inline {{ font-size: 13px; color: #333; display: flex; align-items: center; gap: 6px; cursor: pointer; }}
  .era-toggle {{ display: flex; gap: 14px; }}
  .backtest-controls {{ align-items: center; }}
  .backtest-group {{ display: flex; align-items: center; gap: 8px; }}
  .backtest-group-label {{ font-size: 13px; color: #333; }}
  .radio-inline {{ font-size: 13px; color: #333; display: flex; align-items: center; gap: 4px; cursor: pointer; }}
  .backtest-stats {{ font-size: 13px; color: #333; margin: 4px 0 8px; display: flex; flex-direction: column;
    gap: 4px; align-items: flex-start; }}
  .backtest-stats-row {{ display: flex; gap: 20px; flex-wrap: wrap; align-items: center; }}
  .backtest-stats-label {{ font-weight: bold; color: #111; min-width: 62px; }}
  .backtest-stats b {{ color: #111; }}
  .backtest-reset-btn {{ font-size: 12px; color: #333; background: #fff; border: 1px solid #ccc; border-radius: 4px;
    padding: 3px 10px; cursor: pointer; align-self: flex-end; margin-left: auto; }}
  .backtest-reset-btn:hover {{ background: #f0f0f0; }}
  .backtest-linebreak {{ flex-basis: 100%; height: 0; }}
  .backtest-toggle-label {{ font-size: 13px; color: #333; }}
  .toggle-switch {{ position: relative; display: inline-block; width: 38px; height: 20px; cursor: pointer; }}
  .toggle-switch input {{ opacity: 0; width: 0; height: 0; }}
  .toggle-slider {{ position: absolute; top: 0; left: 0; right: 0; bottom: 0; background-color: #bbb;
    transition: .2s; border-radius: 20px; }}
  .toggle-slider:before {{ position: absolute; content: ""; height: 14px; width: 14px; left: 3px; bottom: 3px;
    background-color: #fff; transition: .2s; border-radius: 50%; }}
  .toggle-switch input:checked + .toggle-slider {{ background-color: #2b6cb0; }}
  .toggle-switch input:checked + .toggle-slider:before {{ transform: translateX(18px); }}
  #backtestChart {{ margin-bottom: 10px; }}
  .checkbox-group {{ display: flex; flex-direction: column; gap: 4px; }}
  .checkbox-group-label {{ font-size: 13px; color: #555; }}
  .checkbox-list {{ max-height: 150px; overflow-y: auto; border: 1px solid #ddd; border-radius: 4px;
    padding: 6px 10px; display: flex; flex-direction: column; gap: 3px; min-width: 160px; background: #fafafa; }}
  .checkbox-item {{ font-size: 13px; display: flex; align-items: center; gap: 6px; cursor: pointer; white-space: nowrap; }}
  h2 {{ margin-top: 40px; border-top: 1px solid #ddd; padding-top: 20px; }}
  table.rank-table {{ border-collapse: collapse; margin: 8px 0 28px; font-size: 13px; }}
  table.rank-table th, table.rank-table td {{ border: 1px solid #ddd; padding: 4px 10px; text-align: left; }}
  table.rank-table th {{ background: #f5f5f5; }}
  .rank-tables-row {{ display: flex; gap: 24px; flex-wrap: wrap; margin-top: 12px; }}
  .rank-table-col {{ flex: 1 1 380px; min-width: 320px; }}
  .rank-table-col table.rank-table {{ width: 100%; }}
  .rank-table-col h4 {{ margin-bottom: 4px; }}
  #priceDist {{ margin: 4px 0 20px; }}
  #priceDistHint {{ color: #888; font-size: 13px; margin: 4px 0 20px; }}
  .chart-controls-row {{ margin: 4px 0 12px; display: flex; gap: 8px; align-items: center; }}
  .chart-btn {{ font-size: 12px; padding: 3px 10px; border: 1px solid #ccc; border-radius: 4px;
    background: #fff; color: #333; cursor: pointer; }}
  .chart-btn:hover {{ background: #f0f0f0; }}
  .streak-hint {{ color: #888; font-size: 13px; margin: 4px 0; }}
  .city-streak-detail {{ margin: 8px 0 28px; }}
  .city-streak-detail h4 {{ margin: 4px 0 8px; }}
  .streak-source-row {{ margin-bottom: 10px; }}
  .streak-source-label {{ font-size: 13px; color: #555; margin-bottom: 4px; }}
  .streak-days-row {{ display: flex; gap: 1px; overflow-x: auto; padding-bottom: 6px; }}
  .streak-era-separator {{ flex: 0 0 auto; width: 2px; background: #999; margin: 0 10px; align-self: stretch;
    position: relative; min-height: 40px; }}
  .streak-era-label {{ position: absolute; top: -16px; font-size: 9px; color: #888; white-space: nowrap; }}
  .streak-era-label-old {{ right: 4px; }}
  .streak-era-label-new {{ left: 4px; }}
  .streak-day {{ display: inline-flex; flex: 0 0 auto; flex-direction: column; align-items: center; width: 36px; }}
  .streak-day-date {{ font-size: 10px; color: #888; white-space: nowrap; }}
  .streak-day-price {{ font-size: 10px; color: #888; margin-top: 2px; white-space: nowrap; }}
  .streak-day-mark {{ display: inline-block; width: 22px; height: 22px; line-height: 22px;
    border-radius: 4px; font-weight: bold; text-align: center; }}
  .streak-day-hit {{ background: #e8f8ee; color: #1e7e34; }}
  .streak-day-miss {{ background: #fdecea; color: #c0392b; }}
  .streak-day-none {{ background: #f5f5f5; color: #bbb; }}
</style>
</head>
<body>
<div class="controls">
  <span><label for="periodSelect">Период:</label>
  <select id="periodSelect"></select></span>
  <span><label for="slotSelect">Слот:</label>
  <select id="slotSelect"></select></span>
  {'' if not show_effective_max else '<span><label class="checkbox-inline"><input type="checkbox" id="effectiveMaxCheckbox"> Не учитывать максимум, если по нему только 1 замер.</label></span>'}
  <span class="era-toggle">
    <label class="checkbox-inline"><input type="checkbox" id="eraOldCheckbox" checked> Старые</label>
    <label class="checkbox-inline"><input type="checkbox" id="eraNewCheckbox" checked> Новые</label>
  </span>
</div>
<div class="controls checkbox-filters">
  <div class="checkbox-group">
    <div class="checkbox-group-label">Регионы:</div>
    <div id="regionCheckboxes" class="checkbox-list"></div>
  </div>
  <div class="checkbox-group">
    <div class="checkbox-group-label">Города:</div>
    <div id="cityCheckboxes" class="checkbox-list"></div>
  </div>
</div>
{"" if is_watched else f'''<div class="controls">
  <span><label for="correctionRange">Поправка:</label>
  <input type="range" id="correctionRange" min="{err_min}" max="{err_max}" step="1" value="0">
  <span id="correctionValue" class="correction-value">0</span></span>
  <span><label for="priceMinInput">Цена от:</label>
  <input type="number" id="priceMinInput" class="price-input" step="0.1" value="{price_min_str}">
  <label for="priceMaxInput">до:</label>
  <input type="number" id="priceMaxInput" class="price-input" step="0.1" value="{price_max_str}">¢</span>
</div>'''}
{chart_html}
<p id="priceDistHint">Кликните по столбцу графика, чтобы увидеть распределение цен ставок Yes в нём. Удерживайте Ctrl, чтобы добавить столбцы или Shift, чтобы выбрать диапазон.</p>
<div class="chart-controls-row">
  <button id="selectAllBtn" type="button" class="chart-btn">Выбрать все столбцы</button>
  <button id="clearSelectionBtn" type="button" class="chart-btn">Сбросить выбор</button>
</div>
<div id="priceDist"></div>
{rating_html}
<script>
const TRACE_MAP = {json.dumps(trace_map, ensure_ascii=False)};
const DISPLAY_IDXS = {json.dumps(display_idxs)};
const ERROR_VALUES = {json.dumps(error_values)};
const CITY_BY_REGION = {json.dumps(city_by_region, ensure_ascii=False)};
const ALL_CITIES = {json.dumps(all_cities, ensure_ascii=False)};
const REGIONS = {json.dumps(regions, ensure_ascii=False)};
const SLOT_KEYS = {json.dumps(slot_keys, ensure_ascii=False)};
const DEFAULT_SLOT_KEY_JS = {json.dumps(default_slot_key)};
const PERIOD_KEYS = {json.dumps(period_keys, ensure_ascii=False)};
const PERIOD_LABELS_MAIN = {json.dumps(period_labels, ensure_ascii=False)};
const WATCHED_MODE = {json.dumps(is_watched)};
const CITY_CORRECTIONS = {json.dumps(city_corrections, ensure_ascii=False)};
const BUCKET_PRICES = {json.dumps(bucket_price_map, ensure_ascii=False)};
const DATA_ERA_CUTOFF_DATE = {json.dumps(self.data_era_cutoff_date)};

const periodSelect = document.getElementById("periodSelect");
const slotSelect = document.getElementById("slotSelect");
const effectiveMaxCheckbox = document.getElementById("effectiveMaxCheckbox");
function currentMode() {{ return (effectiveMaxCheckbox && effectiveMaxCheckbox.checked) ? "effective" : "raw"; }}
const eraOldCheckbox = document.getElementById("eraOldCheckbox");
const eraNewCheckbox = document.getElementById("eraNewCheckbox");
// "Старые"/"Новые" — какие даты (относительно DATA_ERA_CUTOFF_DATE) вообще
// учитывать. В отличие от фильтра цены, это НЕ "прозрачный пропуск" — это
// чистая календарная граница (даты <= cutoff — старые, > cutoff — новые, без
// перемешивания), так что снятая галочка просто убирает эти снапшоты из
// подсчёта целиком, как будто их не было. Обе сняты — данных не остаётся.
function getSelectedEras() {{
  const eras = [];
  if (eraOldCheckbox && eraOldCheckbox.checked) eras.push("old");
  if (eraNewCheckbox && eraNewCheckbox.checked) eras.push("new");
  return eras;
}}
const chartDiv = document.getElementById("chart");
const priceDistDiv = document.getElementById("priceDist");
const priceDistHint = document.getElementById("priceDistHint");
const correctionRange = document.getElementById("correctionRange");
const correctionValueLabel = document.getElementById("correctionValue");
const priceMinInput = document.getElementById("priceMinInput");
const priceMaxInput = document.getElementById("priceMaxInput");
const regionCheckboxesDiv = document.getElementById("regionCheckboxes");
const cityCheckboxesDiv = document.getElementById("cityCheckboxes");

const PRICE_DEFAULT_MIN = {price_min_str};
const PRICE_DEFAULT_MAX = {price_max_str};

function fmtSigned(v) {{
  const n = Number(v);
  return (n > 0 ? "+" : "") + n;
}}

function isPriceFilterActive() {{
  return Number(priceMinInput.value) !== PRICE_DEFAULT_MIN || Number(priceMaxInput.value) !== PRICE_DEFAULT_MAX;
}}

// Список городов уже отфильтрован по отмеченным регионам (см. rebuildCityList)
// — просто читаем, какие ИЗ ВИДИМЫХ городских чекбоксов отмечены.
function getSelectedCities() {{
  return Array.from(cityCheckboxesDiv.querySelectorAll("input.item-cb:checked")).map(el => el.value);
}}

// Фильтр (поправка + порог цены), применяемый к главному графику. В обычном
// отчёте (WATCHED_MODE=false) — из глобальных бегунков (как раньше), одним
// значением на любой набор выбранных городов. Во втором,
// "скорректированном" отчёте (WATCHED_MODE=true) — поправка уже "запечена"
// в сами данные (см. build_watched_report/_build_corrected_df), поэтому
// здесь всегда 0; порог цены применяется, только когда выбран РОВНО ОДИН
// город (для смеси нескольких городов с разными порогами единый фильтр не
// имеет смысла) и для него задан min_price_limit.
function getActiveFilter() {{
  if (WATCHED_MODE) {{
    const selected = getSelectedCities();
    if (selected.length !== 1) {{
      return {{ correction: 0, priceActive: false, priceMin: 0, priceMax: 100 }};
    }}
    const cfg = CITY_CORRECTIONS[selected[0]];
    const hasMin = cfg && cfg.min_price !== null && cfg.min_price !== undefined;
    const hasMax = cfg && cfg.max_price !== null && cfg.max_price !== undefined;
    if (!hasMin && !hasMax) {{
      return {{ correction: 0, priceActive: false, priceMin: 0, priceMax: 100 }};
    }}
    return {{
      correction: 0, priceActive: true,
      priceMin: hasMin ? cfg.min_price : 0,
      priceMax: hasMax ? cfg.max_price : 100,
    }};
  }}
  return {{
    correction: Number(correctionRange.value),
    priceActive: isPriceFilterActive(),
    priceMin: Number(priceMinInput.value), priceMax: Number(priceMaxInput.value),
  }};
}}

function priceStatsText(prices, totalCount) {{
  const missing = totalCount - prices.length;
  if (!prices.length) {{
    return "Цена ставки Yes: нет данных" + (missing ? ` (${{missing}} без цены)` : "");
  }}
  const sorted = prices.slice().sort((a, b) => a - b);
  const n = sorted.length;
  const mean = sorted.reduce((a, b) => a + b, 0) / n;
  const median = n % 2 === 1 ? sorted[(n - 1) / 2] : (sorted[n / 2 - 1] + sorted[n / 2]) / 2;
  const missingLine = missing ? `<br>Без цены: ${{missing}}` : "";
  return `Цена ставки Yes — среднее ${{mean.toFixed(1)}}¢, медиана ${{median.toFixed(1)}}¢${{missingLine}}`;
}}

// Цена ставки НА БАКЕТ targetBucket (= прогноз + поправка) в конкретный
// (город, дата, слот) — ищется динамически среди ВСЕХ ставок Polymarket
// этого снапшота (BUCKET_PRICES), а не берётся готовой из строки: та же
// логика, что уже используется в панели рейтингов (checkmarks/crosses) —
// см. lookupPrice в build_rating_html.
function lookupPrice(icao, date, slotKey, targetBucket) {{
  const byDate = (BUCKET_PRICES[icao] || {{}})[date];
  if (!byDate) return null;
  const bySlot = byDate[slotKey];
  if (!bySlot) return null;
  for (const [lowB, highB, price] of bySlot) {{
    const loOk = (lowB === null) || (targetBucket >= lowB);
    const hiOk = (highB === null) || (targetBucket <= highB);
    if (loOk && hiOk) return price;
  }}
  return null;
}}

// Кэш ИСХОДНЫХ (без поправки/фильтра цены/выбора городов) customdata КАЖДОЙ
// городской (невидимой) трассы — берём один раз сразу после отрисовки
// исходной фигуры. x у всех трасс одинаковый (ERROR_VALUES), поэтому его
// кэшировать по трассам не нужно. Каждый элемент customdata теперь — не
// готовая цена, а список снапшотов бакета ([icao, date, forecastBucket]) —
// сама цена на (прогноз + поправка) ищется на лету через lookupPrice, а НЕ
// берётся из "застывшей" цены сырого прогноза (иначе после поправки в
// распределении оставались бы "перекрашенные" старые ставки — та же ошибка,
// что раньше была в панели рейтингов, и там же был исправлена).
const ORIGINAL_TRACE_CUSTOMDATA = chartDiv.data.map(t => t.customdata.map(cd => cd.slice()));

// Суммирует данные ВЫБРАННЫХ ЧЕКБОКСАМИ городов (getSelectedCities) для
// одного источника (WU или Windy) за текущие режим/период/слот, затем для
// КАЖДОГО снапшота ищет цену НА СКОРРЕКТИРОВАННЫЙ таргет (bucket + correction)
// и применяет фильтр цены — точно так же, как уже работает панель рейтингов.
// При priceActive=false высота столбца — это ВСЕ снапшоты бакета (форма
// распределения не меняется, сдвигается только позиция по X от поправки);
// при активном диапазоне цены — только те снапшоты, чья цена на
// скорректированный таргет попала в выбранный диапазон.
function computeCombinedSource(source, correction, priceActive, priceMin, priceMax) {{
  const selectedCities = getSelectedCities();
  const selectedEras = getSelectedEras();
  const key = `${{currentMode()}}||${{periodSelect.value}}||${{slotSelect.value}}`;
  const n = ERROR_VALUES.length;
  const sumSnapshotCount = new Array(n).fill(0);
  const sumPrices = Array.from({{ length: n }}, () => []);
  const slotKey = slotSelect.value;

  selectedCities.forEach(city => {{
    const idxs = TRACE_MAP[`${{key}}||city:${{city}}`];
    if (!idxs) return;
    const idx = (source === "wunderground") ? idxs[0] : idxs[1];
    const origCD = ORIGINAL_TRACE_CUSTOMDATA[idx];
    for (let i = 0; i < n; i++) {{
      const snapshots = origCD[i][0].filter(s => selectedEras.includes(s[3]));
      sumSnapshotCount[i] += snapshots.length;
      snapshots.forEach(s => {{
        const icao = s[0], date = s[1], forecastBucket = s[2];
        const price = lookupPrice(icao, date, slotKey, forecastBucket + correction);
        if (price !== null) sumPrices[i].push(price);
      }});
    }}
  }});

  const newX = ERROR_VALUES.map(v => v + correction);
  const newY = [];
  const newCD = [];
  for (let i = 0; i < n; i++) {{
    let prices = sumPrices[i];
    let count = sumSnapshotCount[i];
    if (priceActive) {{
      prices = prices.filter(p => p >= priceMin && p <= priceMax);
      count = prices.length;
    }}
    newY.push(count);
    newCD.push([priceStatsText(prices, sumSnapshotCount[i]), prices, source, newX[i]]);
  }}
  return {{ x: newX, y: newY, customdata: newCD }};
}}

function applyCorrectionAndPriceFilter() {{
  const {{ correction, priceActive, priceMin, priceMax }} = getActiveFilter();
  ["wunderground", "windy"].forEach((source, i) => {{
    const {{ x, y, customdata }} = computeCombinedSource(source, correction, priceActive, priceMin, priceMax);
    Plotly.restyle(chartDiv, {{ x: [x], y: [y], customdata: [customdata] }}, [DISPLAY_IDXS[i]]);
  }});
}}

let selectedErrors = new Set();
let lastAnchorError = null;

function fillPeriodOptions() {{
  PERIOD_KEYS.forEach(k => {{
    const opt = document.createElement("option");
    opt.value = k;
    opt.textContent = PERIOD_LABELS_MAIN[k] || k;
    periodSelect.appendChild(opt);
  }});
}}

function fillSlotOptions() {{
  SLOT_KEYS.forEach(k => {{
    const opt = document.createElement("option");
    opt.value = k;
    opt.textContent = `Слот ${{k}}`;
    if (k === DEFAULT_SLOT_KEY_JS) opt.selected = true;
    slotSelect.appendChild(opt);
  }});
}}

// Мастер-галочка "Все ..." — ставим: отмечаются все элементы списка;
// снимаем: снимаются все. Обратная синхронизация (см. syncMaster): если
// мастер стоит (всё отмечено) и снять галочку с одного элемента — мастер
// автоматически снимается; если вручную отметить все элементы — мастер
// автоматически ставится.
function buildMasterRow(labelText) {{
  const label = document.createElement("label");
  label.className = "checkbox-item checkbox-item-master";
  const input = document.createElement("input");
  input.type = "checkbox";
  input.className = "master-cb";
  input.checked = true;
  label.appendChild(input);
  const b = document.createElement("b");
  b.textContent = labelText;
  label.appendChild(b);
  return {{ label, input }};
}}

function buildItemRow(value) {{
  const label = document.createElement("label");
  label.className = "checkbox-item";
  const input = document.createElement("input");
  input.type = "checkbox";
  input.className = "item-cb";
  input.value = value;
  input.checked = true;
  label.appendChild(input);
  label.appendChild(document.createTextNode(" " + value));
  return {{ label, input }};
}}

function syncMaster(masterInput, container) {{
  const items = Array.from(container.querySelectorAll("input.item-cb"));
  masterInput.checked = items.length > 0 && items.every(cb => cb.checked);
}}

function buildRegionList() {{
  regionCheckboxesDiv.innerHTML = "";
  const master = buildMasterRow("Все регионы");
  regionCheckboxesDiv.appendChild(master.label);
  master.input.addEventListener("change", () => {{
    const checked = master.input.checked;
    regionCheckboxesDiv.querySelectorAll("input.item-cb").forEach(cb => {{ cb.checked = checked; }});
    rebuildCityList();
    updateVisibility();
  }});
  REGIONS.forEach(r => {{
    const item = buildItemRow(r);
    item.input.addEventListener("change", () => {{
      syncMaster(master.input, regionCheckboxesDiv);
      rebuildCityList();
      updateVisibility();
    }});
    regionCheckboxesDiv.appendChild(item.label);
  }});
}}

// Список городов пересобирается заново при любом изменении отмеченных
// регионов — показывает ТОЛЬКО города отмеченных регионов (объединение, если
// отмечено несколько), заново отмеченные по умолчанию. Регион не отмечен ни
// один — города не показываются вовсе.
function rebuildCityList() {{
  const checkedRegions = Array.from(regionCheckboxesDiv.querySelectorAll("input.item-cb:checked")).map(el => el.value);
  const citiesToShow = checkedRegions.length
    ? Array.from(new Set(checkedRegions.flatMap(r => CITY_BY_REGION[r] || []))).sort()
    : [];

  cityCheckboxesDiv.innerHTML = "";
  const master = buildMasterRow("Все города");
  cityCheckboxesDiv.appendChild(master.label);
  master.input.addEventListener("change", () => {{
    const checked = master.input.checked;
    cityCheckboxesDiv.querySelectorAll("input.item-cb").forEach(cb => {{ cb.checked = checked; }});
    updateVisibility();
  }});
  citiesToShow.forEach(city => {{
    const item = buildItemRow(city);
    item.input.addEventListener("change", () => {{
      syncMaster(master.input, cityCheckboxesDiv);
      updateVisibility();
    }});
    cityCheckboxesDiv.appendChild(item.label);
  }});
}}

function scopeLabel() {{
  const selected = getSelectedCities();
  if (selected.length === ALL_CITIES.length) return "Все города";
  if (selected.length === 1) return `Город: ${{selected[0]}}`;
  return `Городов: ${{selected.length}}`;
}}

function slotLabel() {{ return `Слот ${{slotSelect.value}}`; }}
function periodLabel() {{ return PERIOD_LABELS_MAIN[periodSelect.value] || periodSelect.value; }}

function updateBarHighlight(idxs) {{
  if (!idxs.length) return;
  const xVals = chartDiv.data[idxs[0]].x;
  const lineColors = xVals.map(v => selectedErrors.has(v) ? "#333333" : "rgba(0,0,0,0)");
  const lineWidths = xVals.map(v => selectedErrors.has(v) ? 2 : 0);
  Plotly.restyle(
    chartDiv,
    {{ "marker.line.color": idxs.map(() => lineColors), "marker.line.width": idxs.map(() => lineWidths) }},
    idxs
  );
}}

function resetPriceDist() {{
  Plotly.purge(priceDistDiv);
  priceDistDiv.innerHTML = "";
  priceDistHint.style.display = "";
  selectedErrors = new Set();
  lastAnchorError = null;
  updateBarHighlight(DISPLAY_IDXS);
}}

function renderPriceDistForSelection(idxs) {{
  if (!idxs.length || selectedErrors.size === 0) {{
    resetPriceDist();
    return;
  }}
  priceDistHint.style.display = "none";
  const distTraces = [];
  let totalSnapshots = 0;
  let anyPrices = false;

  idxs.forEach(idx => {{
    const trace = chartDiv.data[idx];
    let prices = [];
    let source = null;
    trace.x.forEach((v, xi) => {{
      if (!selectedErrors.has(v)) return;
      const cd = trace.customdata[xi];
      source = cd[2];
      totalSnapshots += trace.y[xi];
      if (cd[1] && cd[1].length) prices = prices.concat(cd[1]);
    }});
    if (prices.length) {{
      anyPrices = true;
      distTraces.push({{
        x: prices, type: "histogram", name: SOURCE_LABELS_JS[source],
        marker: {{ color: SOURCE_COLORS_JS[source] }}, opacity: 0.9, xbins: {{ size: 10 }}, legendgroup: source,
      }});
    }}
  }});

  if (!anyPrices) {{
    priceDistDiv.innerHTML = `<p style="color:#888;">Нет цен ставок Yes для выбранных столбцов.</p>`;
    return;
  }}

  const sortedSel = Array.from(selectedErrors).sort((a, b) => a - b);
  const errLabel = sortedSel.join(", ");
  const columnsLabel = sortedSel.length > 1 ? `, ${{sortedSel.length}} столбцов` : "";

  Plotly.newPlot(priceDistDiv, distTraces, {{
    title: {{
      text: `<b>Распределение цен ставки Yes — Ошибка ${{errLabel}} ставок${{columnsLabel}}, ${{totalSnapshots}} снапшотов</b>`,
      x: 0.02, xanchor: "left",
    }},
    xaxis: {{ title: "Цена в ¢" }}, yaxis: {{ title: "Количество снапшотов" }},
    barmode: "overlay", bargap: 0.05, legend: {{ title: {{ text: "Источник" }} }},
    template: "plotly_white", height: 320,
  }}, {{ displayModeBar: false }});
}}

function updateTitle() {{
  let suffix = "";
  if (!WATCHED_MODE) {{
    const correction = Number(correctionRange.value);
    suffix = correction !== 0 ? `, поправка ${{fmtSigned(correction)}}` : "";
  }} else {{
    const selected = getSelectedCities();
    if (selected.length === 1) {{
      const cfg = CITY_CORRECTIONS[selected[0]];
      if (cfg) {{
        const parts = [];
        if (cfg.corr) parts.push(`поправка ${{fmtSigned(cfg.corr)}}`);
        if (cfg.min_price !== null && cfg.min_price !== undefined) parts.push(`мин. цена ${{cfg.min_price}}¢`);
        if (cfg.max_price !== null && cfg.max_price !== undefined) parts.push(`макс. цена ${{cfg.max_price}}¢`);
        if (cfg.comment) parts.push(cfg.comment);
        if (parts.length) suffix = `, ${{parts.join(", ")}}`;
      }}
    }}
  }}
  const title = `<b>Распределение точности прогноза максимума — ${{scopeLabel()}}, ${{slotLabel()}}, ${{periodLabel()}}${{suffix}}</b>`;
  Plotly.relayout(chartDiv, {{ "title.text": title }});
}}

function updateVisibility() {{
  applyCorrectionAndPriceFilter();
  updateTitle();
  resetPriceDist();
}}

if (!WATCHED_MODE) {{
  correctionRange.addEventListener("input", () => {{
    correctionValueLabel.textContent = fmtSigned(correctionRange.value);
    updateVisibility();
  }});
  priceMinInput.addEventListener("change", updateVisibility);
  priceMaxInput.addEventListener("change", updateVisibility);
}}

const SOURCE_LABELS_JS = {json.dumps(SOURCE_LABELS, ensure_ascii=False)};
const SOURCE_COLORS_JS = {json.dumps(SOURCE_COLORS, ensure_ascii=False)};

chartDiv.on("plotly_click", function(data) {{
  if (!data.points || !data.points.length) return;
  const pt = data.points[0];
  const customdata = pt.customdata;
  if (!customdata) return;
  const errorVal = customdata[3];
  const evt = data.event || {{}};
  const ctrlLike = evt.ctrlKey || evt.metaKey;
  const shiftLike = evt.shiftKey;

  const idxs = DISPLAY_IDXS;

  if (shiftLike && lastAnchorError !== null) {{
    const lo = Math.min(lastAnchorError, errorVal);
    const hi = Math.max(lastAnchorError, errorVal);
    const xVals = chartDiv.data[idxs[0]].x;
    xVals.forEach(v => {{ if (v >= lo && v <= hi) selectedErrors.add(v); }});
  }} else if (ctrlLike) {{
    if (selectedErrors.has(errorVal)) {{ selectedErrors.delete(errorVal); }} else {{ selectedErrors.add(errorVal); }}
    lastAnchorError = errorVal;
  }} else {{
    selectedErrors = new Set([errorVal]);
    lastAnchorError = errorVal;
  }}

  updateBarHighlight(idxs);
  renderPriceDistForSelection(idxs);
}});

slotSelect.addEventListener("change", updateVisibility);
periodSelect.addEventListener("change", updateVisibility);
if (effectiveMaxCheckbox) {{
  effectiveMaxCheckbox.addEventListener("change", updateVisibility);
}}
eraOldCheckbox.addEventListener("change", updateVisibility);
eraNewCheckbox.addEventListener("change", updateVisibility);

document.getElementById("selectAllBtn").addEventListener("click", () => {{
  const idxs = DISPLAY_IDXS;
  const xVals = chartDiv.data[idxs[0]].x;
  selectedErrors = new Set(xVals);
  lastAnchorError = null;
  updateBarHighlight(idxs);
  renderPriceDistForSelection(idxs);
}});
document.getElementById("clearSelectionBtn").addEventListener("click", () => {{ resetPriceDist(); }});

fillPeriodOptions();
fillSlotOptions();
buildRegionList();
rebuildCityList();
updateVisibility();
</script>
</body>
</html>
"""
        return page

    def save_report(self, dfs, out_path=None):
        """
        Сохраняет отчёт в ОДИН фиксированный файл (перезаписывается каждый
        день — весит много, история не нужна, см. config.paths.report_filename).
        dfs: {"raw": df_raw, "effective": df_effective} — см. build_traces.
        """
        out_path = out_path or os.path.join(self.report_dir, self.report_filename)
        page = self.build_html_report(dfs, embed_maxbet_backtest=True)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(page)
        return out_path

    def _build_corrected_df(self, df, watched):
        """
        Второй ("скорректированный") отчёт — только по городам из
        config/watched_icaos_weather_forecast.yaml, и только по ИХ настроенному источнику
        прогноза (weather_source) — второй источник для каждого такого города
        отбрасывается, чтобы отчёт показывал ровно то, что реально
        мониторится (см. PriceMonitor). Поправка (weather_corr) "запекается"
        прямо в error/forecast_bucket — ЦЕНА при этом НЕ удаляет строки:
        фильтрация по min_price_limit применяется ДИНАМИЧЕСКИ в JS (как и
        цена в первом отчёте), с фиксированным на город порогом вместо
        бегунка — если бы вместо этого мы просто выбросили строки по цене
        здесь, они стали бы неотличимы от ПО-НАСТОЯЩЕМУ отсутствующих дней и
        ложно рвали бы серию промахов в блоке рейтингов (см. build_rating_html).
        """
        parts = []
        for icao, cfg in watched.items():
            sub = df[(df["icao"] == icao) & (df["source"] == cfg["weather_source"])]
            if sub.empty:
                continue
            sub = sub.copy()
            sub["error"] = sub["error"] + cfg["weather_corr"]
            sub["forecast_bucket"] = sub["forecast_bucket"] + cfg["weather_corr"]
            parts.append(sub)
        if not parts:
            return df.iloc[0:0]
        return pd.concat(parts, ignore_index=True)

    def build_watched_report(self, dfs, watched):
        """
        Вторая HTML-страница — только города из watched (config/watched_icaos_weather_forecast.yaml),
        с уже применённой поправкой к прогнозу (см. _build_corrected_df, применяется
        ОТДЕЛЬНО к каждому режиму raw/effective) и БЕЗ интерактивных бегунков
        поправки/цены (порог цены задаётся ПО ГОРОДУ из того же файла,
        применяется автоматически). dfs: {"raw": df_raw, "effective": df_effective}.
        Возвращает HTML-строку; None, если после фильтрации по watched не
        осталось пересекающихся данных прогноза и факта.
        """
        corrected = {mode: self._build_corrected_df(df, watched) for mode, df in dfs.items()}
        if corrected["raw"].empty:
            return None
        return self.build_html_report(corrected, watched=watched, live_strategy_set="weather_forecast")

    def save_watched_report(self, dfs, watched, out_path=None):
        """Как save_report, но для build_watched_report — см. config.paths.watched_report_filename.
        dfs: {"raw": df_raw, "effective": df_effective}. Возвращает путь к файлу
        или None, если отчёт не построен (нет данных)."""
        page = self.build_watched_report(dfs, watched)
        if page is None:
            return None
        out_path = out_path or os.path.join(self.report_dir, self.config.paths.watched_report_filename)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(page)
        return out_path
