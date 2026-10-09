"""
actuals_miner.py

Класс ActualsMiner — сбор фактических дневных максимумов температуры с
Wunderground history/monthly (с fallback на history/daily по дням, если
месячная таблица не открылась) — по каждому городу из CITIES.

Отличия от исходного actuals_miner.py (по ТЗ):
- ACTUALS_START_MONTH больше не задаётся отдельной константой — месяц для
  майнинга это просто месяц переданной target_date (всегда текущий месяц на
  момент запуска, как и требуется).
- День, ДО КОТОРОГО майнить (up_to_date), параметризован явно — это аргумент
  run()/mine_missing_for_month(), а не жёстко "вчера от системного времени".
  Orchestrator передаёт сюда текущую дату по Сиэтлу (см. Orchestrator._target_date).
- Файл месяца больше НЕ перезаписывается с нуля при каждом запуске: перед
  майнингом читаются уже сохранённые (icao, date) записи месяца, и майнятся
  ТОЛЬКО недостающие дни — это и есть требуемая проверка "уже было смайнено,
  чтобы не майнить лишнего" (в исходной версии её не было).
"""

import json
import os
import re
import time
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta

import requests

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .base_miner import BaseSeleniumMiner, build_logger

MAIN_TABLE_XPATH = "//lib-city-history-observation//table[contains(@class,'days')]"
DATE_TABLE_XPATH = MAIN_TABLE_XPATH + "/tbody/tr/td[1]/table"
TEMP_TABLE_XPATH = MAIN_TABLE_XPATH + "/tbody/tr/td[2]/table"
DAILY_TABLE_XPATH = "//lib-city-history-observation/div/div[2]/table"


class ActualsMiner(BaseSeleniumMiner):
    def __init__(self, config, cities, telegram, logger=None):
        self.cities = cities
        self.data_dir = config.paths.wunderground_history_dir
        self.page_load_wait_sec = config.actuals_miner.page_load_wait_sec
        self.actuals_table_wait_sec = config.actuals_miner.actuals_table_wait_sec
        self.actuals_max_retries = config.actuals_miner.actuals_max_retries
        self.actuals_retry_delay_sec = config.actuals_miner.actuals_retry_delay_sec
        self.send_summary = getattr(config.actuals_miner, "send_summary", True)
        # "api" — новый безбраузерный метод (fetch_month_actuals_api, ниже),
        # "chrome" — старый Selenium-метод (scrape_month_actuals/
        # _scrape_missing_days, не тронуты). См. config.actuals_miner.mining_method.
        self.mining_method = getattr(config.actuals_miner, "mining_method", "chrome")
        wu_api_cfg = getattr(config, "wunderground_api", None)
        self.wunderground_api_key = getattr(wu_api_cfg, "key", None) if wu_api_cfg else None
        self.wunderground_api_timeout_sec = getattr(wu_api_cfg, "request_timeout_sec", 15) if wu_api_cfg else 15

        logger = logger or build_logger(
            "ActualsMiner", os.path.join(config.paths.log_dir, "actuals_miner"), "actuals_miner.log"
        )
        super().__init__(config, telegram, logger)

    # ------------------------------------------------------------------ #
    # Инфраструктура вывода / проверка уже смайненного
    # ------------------------------------------------------------------ #

    def _month_output_path(self, year, month):
        os.makedirs(self.data_dir, exist_ok=True)
        return os.path.join(self.data_dir, f"actuals_{year}_{month:02d}.jsonl")

    def _load_existing_dates(self, year, month):
        """{icao: {date_str, ...}} — уже сохранённые записи месяца; используется,
        чтобы не майнить повторно то, что уже есть в файле."""
        existing = defaultdict(set)
        path = self._month_output_path(year, month)
        if not os.path.exists(path):
            return existing
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                existing[rec["icao"]].add(rec["date"])
        return existing

    @staticmethod
    def _last_day_to_mine(year, month, up_to_date):
        """
        Последний день месяца (year, month), который нужно смайнить, с учётом
        параметра up_to_date (день, ДО которого майнить, включительно) — см.
        заголовок модуля. Для месяцев ПОСЛЕ месяца up_to_date возвращает 0
        (майнить пока нечего).
        """
        days_in_month = monthrange(year, month)[1]
        if (year, month) == (up_to_date.year, up_to_date.month):
            return min(up_to_date.day, days_in_month)
        if (year, month) < (up_to_date.year, up_to_date.month):
            return days_in_month
        return 0

    @staticmethod
    def _parse_day(cell_text, fallback_day_number):
        text = cell_text.strip()
        return int(text) if text.isdigit() else fallback_day_number

    # ------------------------------------------------------------------ #
    # Wunderground history/monthly (эффективный путь — одна загрузка на месяц)
    # ------------------------------------------------------------------ #

    def scrape_month_actuals(self, icao, year, month):
        """
        Возвращает список {"date": "YYYY-MM-DD", "max_temp_f": int} по всем дням
        месяца, доступным в таблице. None — полный провал (нужен fallback по
        дням); [] — страница открылась, но данных за месяц легитимно нет.
        """
        base_url = self.cities[icao]["weather_url"].replace("/hourly/", "/history/monthly/")
        url = f"{base_url}/date/{year}-{month}"
        self.logger.info(f"🔗 [Actuals] Открываю: {url}")
        results = []
        step = "открытие вкладки"
        try:
            self.driver.execute_script(f"window.open('{url}', '_blank');")
            self.driver.switch_to.window(self.driver.window_handles[-1])
            wait = WebDriverWait(self.driver, 15)

            step = "ожидание нужного URL"
            try:
                wait.until(lambda d: f"{year}-{month}" in d.current_url)
            except Exception:
                self.logger.error(
                    f"❌ [Actuals] URL не соответствует {year}-{month} "
                    f"(текущий: {self.driver.current_url}), пропуск"
                )
                self._safe_close_tab()
                return results
            self.logger.debug(f"🪵 [Actuals] {icao}: URL подтверждён — {self.driver.current_url}")

            step = "поиск таблицы дат (DATE_TABLE_XPATH)"
            date_table = None
            for attempt in range(1, self.actuals_max_retries + 1):
                try:
                    date_table = WebDriverWait(self.driver, self.actuals_table_wait_sec).until(
                        EC.presence_of_element_located((By.XPATH, DATE_TABLE_XPATH))
                    )
                    break
                except Exception:
                    no_data = "no data record" in self.driver.page_source.lower()
                    self.logger.warning(
                        f"⚠️ [Actuals] {icao}: таблица не появилась за {self.actuals_table_wait_sec}с "
                        f"(попытка {attempt}/{self.actuals_max_retries}, 'No data record': {no_data})"
                    )
                    if attempt == self.actuals_max_retries:
                        raise
                    self.driver.refresh()
                    time.sleep(self.actuals_retry_delay_sec)
            self.logger.debug(f"🪵 [Actuals] {icao}: таблица дат найдена, жду {self.page_load_wait_sec}с на дорисовку")
            time.sleep(self.page_load_wait_sec)

            step = "поиск таблицы температур (TEMP_TABLE_XPATH)"
            temp_table = WebDriverWait(self.driver, self.actuals_table_wait_sec).until(
                EC.presence_of_element_located((By.XPATH, TEMP_TABLE_XPATH))
            )

            step = "чтение строк обеих таблиц"
            date_rows = date_table.find_elements(By.TAG_NAME, "tr")[1:]  # tr[1] — заголовок
            temp_rows = temp_table.find_elements(By.TAG_NAME, "tr")[1:]

            for row_idx, (date_row, temp_row) in enumerate(zip(date_rows, temp_rows), start=1):
                try:
                    day_text = date_row.find_element(By.TAG_NAME, "td").text.strip()
                    max_text = temp_row.find_elements(By.TAG_NAME, "td")[0].text.strip()
                    if not day_text or not max_text:
                        continue

                    day = self._parse_day(day_text, row_idx)
                    max_val = int(re.sub(r"[^\d-]", "", max_text))
                    results.append({"date": f"{year}-{month:02d}-{day:02d}", "max_temp_f": max_val})
                except Exception as e:
                    self.logger.debug(f"⚠️ [Actuals] Пропуск строки {row_idx}: {e}")
                    continue

            self.logger.info(f"📈 [Actuals] {icao} {year}-{month:02d}: собрано {len(results)} дней")
            self._safe_close_tab()
            return results
        except Exception as e:
            error_detail = f"[{type(e).__name__}] на шаге «{step}»: {e}"
            self.logger.error(f"❌ [Actuals] Ошибка {icao} {year}-{month:02d}: {error_detail}")
            if not self._check_vpn_dropped(f"scrape_month_actuals:{icao}:{year}-{month:02d}"):
                self.notify_error(f"scrape_month_actuals:{icao}:{year}-{month:02d}", error_detail)
            self._safe_close_tab()
            return None  # None = полный провал (нужен fallback по дням)

    # ------------------------------------------------------------------ #
    # Wunderground -- безбраузерный метод через api.weather.com (см.
    # config.wunderground_api). В отличие от прогноза, geocode-резолв здесь
    # не нужен -- это данные РЕАЛЬНОЙ станции, а не сетки модели, поэтому
    # icaoCode передаётся напрямую (так же, как делает сам сайт).
    #
    # ВНИМАНИЕ: эндпоинт найден в реальном network-логе сайта, но точные
    # имена полей ответа (дата/максимум) НЕ подтверждены живым тестом --
    # см. переписку. Метод пытается несколько вероятных вариантов имени поля
    # максимума и явно логирует, если ни один не подошёл, вместо того чтобы
    # тихо потерять данные. Поэтому config.actuals_miner.mining_method по
    # умолчанию остаётся "chrome", пока имена полей не сверены с сайтом.
    # ------------------------------------------------------------------ #

    _MAX_TEMP_FIELD_CANDIDATES = ("temperatureMax", "maxTempF", "max_temp", "temperature_max")

    def fetch_month_actuals_api(self, icao, year, month, missing_dates):
        """
        Возвращает список {"date": "YYYY-MM-DD", "max_temp_f": int} за диапазон
        min(missing_dates)..max(missing_dates), или None при неудаче (сетевая
        ошибка либо неопознанная структура ответа — см. предупреждение выше).
        """
        results = []
        if not self.wunderground_api_key:
            self.logger.error("❌ [Wunderground-API] wunderground_api.key не задан в конфиге, пропуск")
            return None
        if not missing_dates:
            return results

        start_date = min(missing_dates).replace("-", "")
        end_date = max(missing_dates).replace("-", "")
        url = (
            "https://api.weather.com/v3/wx/conditions/historical/dailysummary/30day"
            f"?apiKey={self.wunderground_api_key}&language=en-US&units=e&format=json"
            f"&icaoCode={icao}&startDate={start_date}&endDate={end_date}"
        )
        self.logger.info(f"🔗 [Wunderground-API] Открываю: {url}")
        try:
            resp = requests.get(
                url, headers={"User-Agent": "Mozilla/5.0"}, timeout=self.wunderground_api_timeout_sec
            )
            resp.raise_for_status()
            data = resp.json()

            valid_times = data.get("validTimeLocal", [])
            max_temps = None
            for candidate in self._MAX_TEMP_FIELD_CANDIDATES:
                if candidate in data and len(data[candidate]) == len(valid_times):
                    max_temps = data[candidate]
                    break

            if not valid_times or max_temps is None:
                self.logger.error(
                    f"❌ [Wunderground-API] {icao} {year}-{month:02d}: не опознана структура ответа "
                    f"(верхнеуровневые ключи: {list(data.keys())}) — поле максимума не найдено среди "
                    f"{self._MAX_TEMP_FIELD_CANDIDATES}"
                )
                return None

            for t, v in zip(valid_times, max_temps):
                if v is None:
                    continue
                results.append({"date": t[:10], "max_temp_f": round(v)})

            self.logger.info(f"📈 [Wunderground-API] {icao} {year}-{month:02d}: собрано {len(results)} дней")
            return results
        except requests.exceptions.RequestException as e:
            self.logger.error(f"❌ [Wunderground-API] Ошибка {icao} {year}-{month:02d}: {e}")
            self._report_api_connection_down("Wunderground")
            return None
        except Exception as e:
            error_detail = f"[{type(e).__name__}] {e}"
            self.logger.error(f"❌ [Wunderground-API] Ошибка {icao} {year}-{month:02d}: {error_detail}")
            self.notify_error(f"fetch_month_actuals_api:{icao}:{year}-{month:02d}", error_detail)
            return None

    # ------------------------------------------------------------------ #
    # Fallback: подневный майнинг (Wunderground history/daily)
    # ------------------------------------------------------------------ #

    def scrape_day_actual(self, icao, year, month, day):
        """Максимум температуры (°F) за конкретный день по часовым данным
        history/daily/, или None при неудаче."""
        base_url = self.cities[icao]["weather_url"].replace("/hourly/", "/history/daily/")
        url = f"{base_url}/date/{year}-{month}-{day}"
        self.logger.info(f"🔗 [Actuals/day] Открываю: {url}")
        step = "открытие вкладки"
        try:
            self.driver.execute_script(f"window.open('{url}', '_blank');")
            self.driver.switch_to.window(self.driver.window_handles[-1])
            wait = WebDriverWait(self.driver, self.actuals_table_wait_sec)

            step = "ожидание нужного URL"
            try:
                wait.until(lambda d: f"{year}-{month}-{day}" in d.current_url)
            except Exception:
                self.logger.error(f"❌ [Actuals/day] URL не соответствует {year}-{month}-{day}, пропуск")
                self._safe_close_tab()
                return None

            step = "поиск часовой таблицы (DAILY_TABLE_XPATH)"
            table = wait.until(EC.presence_of_element_located((By.XPATH, DAILY_TABLE_XPATH)))
            time.sleep(self.page_load_wait_sec)

            step = "чтение часовых значений"
            rows = table.find_elements(By.XPATH, ".//tbody/tr")
            values = []
            for row in rows:
                try:
                    cell = row.find_element(By.XPATH, "./td[2]/lib-display-unit/span")
                    text = cell.text.strip()
                    if not text:
                        continue
                    values.append(int(re.sub(r"[^\d-]", "", text)))
                except Exception:
                    continue

            if not values:
                self.logger.warning(
                    f"⚠️ [Actuals/day] {icao} {year}-{month:02d}-{day:02d}: "
                    f"не удалось прочитать ни одного часового значения"
                )
                self._safe_close_tab()
                return None

            max_val = max(values)
            self.logger.info(
                f"📈 [Actuals/day] {icao} {year}-{month:02d}-{day:02d}: "
                f"максимум {max_val}° из {len(values)} часовых значений"
            )
            self._safe_close_tab()
            return max_val
        except Exception as e:
            error_detail = f"[{type(e).__name__}] на шаге «{step}»: {e}"
            self.logger.error(f"❌ [Actuals/day] Ошибка {icao} {year}-{month:02d}-{day:02d}: {error_detail}")
            if not self._check_vpn_dropped(f"scrape_day_actual:{icao}:{year}-{month:02d}-{day:02d}"):
                self.notify_error(f"scrape_day_actual:{icao}:{year}-{month:02d}-{day:02d}", error_detail)
            self._safe_close_tab()
            return None

    def _scrape_missing_days(self, icao, year, month, missing_dates):
        """Fallback по дням, но ТОЛЬКО для недостающих дат (не всего диапазона
        месяца подряд, как было в исходной версии) — не тратит проходы на уже
        смайненные дни."""
        results = []
        for date_str in sorted(missing_dates):
            day = int(date_str.split("-")[2])
            self.ensure_driver()
            max_val = self.scrape_day_actual(icao, year, month, day)
            if max_val is not None:
                results.append({"date": date_str, "max_temp_f": max_val})
        return results

    # ------------------------------------------------------------------ #
    # Основной проход: только недостающие дни месяца, до up_to_date включительно
    # ------------------------------------------------------------------ #

    def _mine_dates_in_month(self, year, month, wanted_dates):
        """
        Дозаписывает (не перезаписывает!) файл месяца: для каждого города из
        CITIES с источником Wunderground майнит ТОЛЬКО те даты из wanted_dates
        (строки "YYYY-MM-DD" этого месяца), которых ещё нет в файле. Общая
        часть для mine_missing_for_month (диапазон "с 1 числа до up_to_date")
        и mine_missing_for_dates (произвольный явный набор дат). Возвращает
        число реально дозаписанных строк.
        """
        path = self._month_output_path(year, month)
        existing = self._load_existing_dates(year, month)

        written = 0
        with open(path, "a", encoding="utf-8") as f:
            for icao, info in self.cities.items():
                if "wunderground.com" not in info["weather_url"]:
                    self.logger.debug(f"⏭️ {icao}: источник не Wunderground, пропуск")
                    continue

                already = existing.get(icao, set())
                missing_dates = wanted_dates - already
                if not missing_dates:
                    self.logger.debug(f"⏭️ {icao} {year}-{month:02d}: уже смайнено всё нужное, пропуск")
                    continue

                if self.mining_method == "api":
                    days = self.fetch_month_actuals_api(icao, year, month, missing_dates)
                    if days is None:
                        self.logger.warning(
                            f"⚠️ [Wunderground-API] {icao} {year}-{month:02d}: безбраузерный метод не сработал — "
                            f"переходим на старый Selenium-метод (Chrome)"
                        )
                        self.ensure_driver()
                        days = self.scrape_month_actuals(icao, year, month)
                        if days is None:
                            days = self._scrape_missing_days(icao, year, month, missing_dates)
                else:
                    self.ensure_driver()
                    days = self.scrape_month_actuals(icao, year, month)
                    if days is None:
                        self.logger.warning(
                            f"⚠️ [Actuals] {icao} {year}-{month:02d}: месячная таблица не открылась после "
                            f"{self.actuals_max_retries} попыток — переходим на майнинг по недостающим дням"
                        )
                        days = self._scrape_missing_days(icao, year, month, missing_dates)

                for day_rec in days:
                    if day_rec["date"] not in missing_dates:
                        continue  # уже было смайнено раньше либо вне запрошенного набора дат
                    record = {
                        "date": day_rec["date"],
                        "icao": icao,
                        "city": info["name"],
                        "max_temp_f": day_rec["max_temp_f"],
                    }
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                    written += 1
                    existing.setdefault(icao, set()).add(day_rec["date"])

        self.logger.info(f"💾 {year}-{month:02d}: дозаписано {written} новых строк в {path}")
        return written

    def mine_missing_for_month(self, year, month, up_to_date):
        """
        Дозаписывает недостающие даты месяца в диапазоне [1, last_day_to_mine]
        (last_day_to_mine — см. _last_day_to_mine, ограничен up_to_date) —
        "догоняющий" режим для ежедневного автозапуска (Orchestrator): майнит
        весь месяц с 1 числа, а не только up_to_date, чтобы подхватить дни,
        пропущенные из-за прошлых сбоев/простоя.
        """
        last_day = self._last_day_to_mine(year, month, up_to_date)
        if last_day == 0:
            self.logger.info(f"⏭️ {year}-{month:02d}: месяц ещё не наступил относительно {up_to_date}, пропуск")
            return 0
        wanted_dates = {f"{year}-{month:02d}-{d:02d}" for d in range(1, last_day + 1)}
        return self._mine_dates_in_month(year, month, wanted_dates)

    def mine_missing_for_dates(self, dates):
        """
        Дозаписывает недостающие данные СТРОГО за переданный набор конкретных
        дат (date/datetime.date) — в отличие от mine_missing_for_month, не
        подхватывает дни МЕЖДУ ними ("дыры" в списке специально не трогает).
        Даты группируются по месяцам (диапазон/список может пересекать
        границу месяца). Возвращает суммарное число дозаписанных строк.
        """
        by_month = defaultdict(set)
        for d in dates:
            by_month[(d.year, d.month)].add(f"{d.year}-{d.month:02d}-{d.day:02d}")

        total_written = 0
        for (year, month), wanted_dates in sorted(by_month.items()):
            total_written += self._mine_dates_in_month(year, month, wanted_dates)
        return total_written

    # ------------------------------------------------------------------ #
    # Суточная сводка по конкретному дню (за который только что смайнили факт)
    # ------------------------------------------------------------------ #

    def _build_actuals_daily_summary(self, date_str):
        year, month, _day = (int(x) for x in date_str.split("-"))
        path = self._month_output_path(year, month)

        found = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    if rec.get("date") == date_str:
                        found[rec["icao"]] = rec["max_temp_f"]

        lines = [f"📋 <b>Сводка факта Wunderground</b> за {date_str} #Fact_Summary"]
        for icao, info in self.cities.items():
            if "wunderground.com" not in info["weather_url"]:
                continue
            if icao in found:
                lines.append(f"{info['name']}: {found[icao]}°F✅")
            else:
                lines.append(f"{info['name']}: ❌")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Точка входа
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_date(value):
        """Строка "YYYY-MM-DD" или date/datetime -> datetime.date."""
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return datetime.strptime(value, "%Y-%m-%d").date()

    def _run_explicit_dates(self, dates):
        """
        Майнит СТРОГО переданный набор конкретных дат (список или диапазон,
        уже развёрнутый в run() до списка) — "дыры" между датами специально
        не трогает (в отличие от одиночного target_date, см. run()). Каждая
        дата дозаписывается независимо и инкрементально.
        """
        if not dates:
            self.logger.warning("⚠️ Пустой список дат для майнинга факта, майнить нечего.")
            return

        label = (
            dates[0].isoformat()
            if len(dates) == 1
            else f"{len(dates)} дат ({dates[0].isoformat()} .. {dates[-1].isoformat()})"
        )
        self.logger.info(f"🚀 Майнер фактических максимумов: {label}")
        self.telegram.send_message(f"✅ <b>Майнер фактических максимумов запущен</b>\nЗа {label}")

        try:
            written = self.mine_missing_for_dates(dates)
            self.telegram.send_message(
                f"📋 <b>Майнер фактических максимумов завершил работу</b>\nЗа {label}: {written} новых строк"
            )
        except Exception as e:
            self.logger.error(f"⚠️ Ошибка майнинга за {label}: {e}")
            self.notify_error(f"mine_missing_for_dates:{label}", e)

        if self.send_summary:
            daily_summary = self._build_actuals_daily_summary(dates[-1].isoformat())
            self.telegram.send_message(daily_summary)
            self.logger.info(f"📋 Отправлена сводка фактических максимумов за {dates[-1].isoformat()}")

    def run(self, target_date=None):
        """
        target_date принимает один из форматов:
          - None (по умолчанию) — сегодняшняя дата системы;
          - ОДНА дата — date/datetime.date или строка "YYYY-MM-DD" — поведение
            как раньше: "догоняющий" режим, дозаписывает ВЕСЬ месяц с 1 числа
            до этой даты включительно (см. mine_missing_for_month) — так
            Orchestrator подхватывает дни, пропущенные из-за прошлых сбоев.
            Именно этот режим использует Orchestrator._run_daily_pipeline().
          - ДИАПАЗОН дат — кортеж РОВНО из двух элементов, обе границы
            включительно, порядок не важен:
              actuals_miner.run(target_date=("2026-09-01", "2026-09-06"))
          - явный СПИСОК дат (list, любой длины, включая 2) — майнит РОВНО
            эти дни, не трогая дни между ними:
              actuals_miner.run(target_date=["2026-09-01", "2026-09-02", "2026-09-05"])
            (кортеж => диапазон, список => явный перечень — это единственный
            способ отличить "с 1 по 6" от "именно 1-е и 6-е").
        """
        if isinstance(target_date, tuple):
            if len(target_date) != 2:
                raise ValueError(
                    "target_date как диапазон (tuple) должен содержать ровно 2 даты: (начало, конец)"
                )
            start, end = sorted(self._parse_date(v) for v in target_date)
            dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
            self._run_explicit_dates(dates)
            return

        if isinstance(target_date, list):
            dates = sorted({self._parse_date(v) for v in target_date})
            self._run_explicit_dates(dates)
            return

        target_date = self._parse_date(target_date) if target_date is not None else datetime.now().date()
        year, month = target_date.year, target_date.month

        self.logger.info(f"🚀 Майнер фактических максимумов: {year}-{month:02d} до {target_date.isoformat()}")
        self.telegram.send_message(
            f"✅ <b>Майнер фактических максимумов запущен</b>\n"
            f"За {year}-{month:02d} до {target_date.isoformat()} включительно"
        )

        try:
            written = self.mine_missing_for_month(year, month, target_date)
            self.telegram.send_message(
                f"📋 <b>Майнер фактических максимумов завершил работу</b>\n"
                f"{year}-{month:02d}: {written} новых строк"
            )
        except Exception as e:
            self.logger.error(f"⚠️ Ошибка майнинга {year}-{month:02d}: {e}")
            self.notify_error(f"mine_missing_for_month:{year}-{month:02d}", e)

        if self.send_summary:
            daily_summary = self._build_actuals_daily_summary(target_date.isoformat())
            self.telegram.send_message(daily_summary)
            self.logger.info(f"📋 Отправлена сводка фактических максимумов за {target_date.isoformat()}")
