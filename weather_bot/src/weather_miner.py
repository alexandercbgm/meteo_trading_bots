"""
weather_miner.py

Класс WeatherMiner — сбор почасовых прогнозов погоды (сырые значения, без
нормализации единиц/AM-PM) по городам из CITIES с Wunderground и Windy, а также
ставок Polymarket (температура + цена Buy Yes, через Gamma API) по тому же
городу/дате — в моменты, заданные config.weather_miner.snapshot_slots (по
умолчанию 06:00, 09:00, 12:00 по ЛОКАЛЬНОМУ времени каждого города). Каждый
источник пишется построчно (JSONL) в СВОЮ ОТДЕЛЬНУЮ папку (по файлу на сутки):
config.paths.wunderground_forecast_dir / windy_forecast_dir / polymarket_gamma_dir
(см. SOURCE_DIR_ATTR) — дата в имени файла и внутри записи — ЛОКАЛЬНАЯ дата
города на момент снятия.

В отличие от исходного weather_miner.py, здесь нет собственного бесконечного
цикла с owned-сном: run_cycle() делает ОДИН проход по всем городам и
возвращается — этим управляет Orchestrator (пункт 1/6 ТЗ: непрерывный майнинг
с паузой на остальные скрипты). run() оставлен как обёртка для автономного
запуска (вне Orchestrator) — бесконечный цикл run_cycle() + сон.
"""

import json
import os
import re
import time
from datetime import datetime

import pytz
import requests
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .base_miner import BaseSeleniumMiner, build_logger

TIME_CELL_RE = re.compile(r"^\d{1,2}\s*(am|pm)$", re.IGNORECASE)


class WeatherMiner(BaseSeleniumMiner):
    # Источник -> папка, где хранятся ЕГО записи (по файлу на сутки, как и
    # раньше) — раньше все три источника писались вперемешку в один
    # weather_<дата>.jsonl, теперь каждый в свою папку (см. config.paths).
    SOURCE_DIR_ATTR = {
        "wunderground": "wunderground_forecast_dir",
        "windy": "windy_forecast_dir",
        "polymarket": "polymarket_gamma_dir",
    }
    SOURCE_FILE_PREFIX = {
        "wunderground": "wunderground_forcast",
        "windy": "windy_forcast",
        "polymarket": "polymarket_gamma",
    }
    # Суточный файл источника: "<префикс>_YYYY_MM_DD.jsonl" — СТРОГО этот
    # паттерн (не просто startswith(prefix)), чтобы для polymarket_gamma_dir
    # не задеть заодно и МЕСЯЧНЫЙ файл факта (polymarket_gamma_<YYYY>_<MM>.jsonl,
    # см. PolymarketActualsMiner) — та же папка, три поля даты вместо двух,
    # и СОВСЕМ другая схема записи (нет "source"/"snapshot_slot" вовсе).
    _DAILY_FILE_RE = {
        source: re.compile(rf"^{re.escape(prefix)}_\d{{4}}_\d{{2}}_\d{{2}}\.jsonl$")
        for source, prefix in {
            "wunderground": "wunderground_forcast", "windy": "windy_forcast", "polymarket": "polymarket_gamma",
        }.items()
    }

    def __init__(self, config, cities, telegram, logger=None, price_monitor=None):
        self.cities = cities
        self.price_monitor = price_monitor  # опционально: PriceMonitor, см. price_monitor.py
        self.source_dirs = {
            source: getattr(config.paths, attr) for source, attr in self.SOURCE_DIR_ATTR.items()
        }
        self.snapshot_slots = config.weather_miner.snapshot_slots  # {int hour: "HH:MM"}
        self.last_slot_label = config.weather_miner.last_slot_label
        self.send_daily_summary = config.weather_miner.send_daily_summary
        self.send_global_daily_summary = config.weather_miner.send_global_daily_summary
        self.global_summary_trigger_hour_utc = config.weather_miner.global_summary_trigger_hour_utc
        self.global_summary_trigger_minute_window = config.weather_miner.global_summary_trigger_minute_window
        self.windy_cutoff_label = config.weather_miner.windy_cutoff_label
        self.page_load_wait_sec = config.weather_miner.page_load_wait_sec
        # "api" — новый безбраузерный метод (scrape_wunderground_api, ниже),
        # "chrome" — старый Selenium-метод (scrape_wunderground, не тронут).
        self.wunderground_mining_method = getattr(
            config.weather_miner, "wunderground_mining_method", "chrome"
        )
        self.windy_enabled = getattr(config.weather_miner, "windy_enabled", True)
        # Gamma-майнинг ставок Polymarket (WeatherMiner.scrape_polymarket*)
        # УБРАН ОТСЮДА ЦЕЛИКОМ (см. git-историю): раньше он был нужен, чтобы
        # накопить poly_bets_raw заранее и передать его в
        # price_monitor.check_city_snapshot*/check_city_snapshot_max_bet, но
        # выяснилось, что эти данные всё равно читались только с ОДНОГО
        # слота (price_monitor.monitor_slot) и только ТАМ и ТОГДА, когда
        # передавались в проверку сигнала -- то есть по факту это уже был
        # "живой запрос перед проверкой", просто оформленный как отдельный
        # майнинг с файлами/дедупом. Плюс PolymarketClobMiner для CLOB и так
        # НЕЗАВИСИМО резолвил тот же Gamma-эндпоинт под token_id -- получалось
        # два Gamma-запроса на одно и то же событие. Теперь PriceMonitor сам
        # запрашивает Gamma живьём в момент проверки, через тот же кэш, что
        # использует CLOB (см. price_monitor.py/gamma_bets_fetcher,
        # PolymarketClobMiner.fetch_gamma_bets) -- методы scrape_polymarket/
        # scrape_polymarket_api ниже оставлены в файле нетронутыми, просто
        # больше не вызываются (на случай отката).
        wu_api_cfg = getattr(config, "wunderground_api", None)
        self.wunderground_api_key = getattr(wu_api_cfg, "key", None) if wu_api_cfg else None
        self.wunderground_api_timeout_sec = getattr(wu_api_cfg, "request_timeout_sec", 15) if wu_api_cfg else 15
        # Нужен ли вообще Chrome в этом запуске — считаем один раз из флагов
        # выше: если прогноз WU майнится через api и Windy выключен, run_cycle()
        # ни разу не станет дёргать ensure_driver() (Chrome можно вообще не
        # запускать). Gamma в этот расчёт больше не входит -- WeatherMiner её
        # вообще не запрашивает (см. комментарий у windy_enabled выше), а
        # PolymarketClobMiner и её собственный Gamma-резолв ходят через
        # requests, без Chrome, независимо от этого флага. См. run_cycle().
        self.chrome_needed = self.wunderground_mining_method != "api" or self.windy_enabled
        logger = logger or build_logger(
            "WeatherMiner", os.path.join(config.paths.log_dir, "weather_miner"), "weather_miner.log"
        )
        super().__init__(config, telegram, logger)

        self.written_keys = self._load_existing_keys()
        self._last_global_summary_utc_date = None  # дата UTC, за которую сводка уже отправлена сегодня

        # Напоминание о выключенных источниках -- ОДИН раз при старте, а не на
        # каждый город каждого круга (см. mine_city): иначе при 49 городах и
        # круге раз в 5 минут в лог сыпалось бы по 2 одинаковые debug-строки
        # на город постоянно, без всякой пользы. ПОСЛЕ super().__init__, т.к.
        # self.logger появляется только там.
        if not self.windy_enabled:
            self.logger.info("⏭️ [Windy] windy_enabled=false в конфиге — майнинг Windy отключён для всего запуска")

    # ------------------------------------------------------------------ #
    # Инфраструктура вывода
    # ------------------------------------------------------------------ #

    def _output_path(self, source, date_str):
        d = self.source_dirs[source]
        os.makedirs(d, exist_ok=True)
        prefix = self.SOURCE_FILE_PREFIX[source]
        return os.path.join(d, f"{prefix}_{date_str.replace('-', '_')}.jsonl")

    def _load_existing_keys(self):
        """
        При старте сканирует СВОИ три папки (по одной на источник — см.
        source_dirs) и запоминает уже снятые снапшоты, чтобы не задублировать
        данные после перезапуска.
        Ключ: (date, icao, source, snapshot_slot)
        """
        keys = set()
        total_files = 0
        for source, d in self.source_dirs.items():
            if not os.path.isdir(d):
                continue
            file_re = self._DAILY_FILE_RE[source]
            for fname in os.listdir(d):
                if not file_re.match(fname):
                    continue
                total_files += 1
                path = os.path.join(d, fname)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            rec = json.loads(line)
                            keys.add((rec["date"], rec["icao"], rec["source"], rec["snapshot_slot"]))
                except Exception as e:
                    self.logger.warning(f"⚠️ Не удалось прочитать {path}: {e}")
        self.logger.info(
            f"📂 Загружено {len(keys)} уже снятых снапшотов из {len(self.source_dirs)} файлов weather_forcast"
        )
        return keys

    def _append_record(self, date_str, icao, source, snapshot_slot, snapshot_timestamp, forecast_raw):
        key = (date_str, icao, source, snapshot_slot)
        if key in self.written_keys:
            self.logger.debug(f"⏭️ Уже снято: {key}")
            return
        record = {
            "date": date_str,
            "icao": icao,
            "city": self.cities[icao]["name"],
            "source": source,
            "snapshot_slot": snapshot_slot,
            "snapshot_timestamp": snapshot_timestamp,
            "forecast_raw": forecast_raw,
        }
        path = self._output_path(source, date_str)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.written_keys.add(key)
        self.logger.info(f"💾 Записан снапшот: {icao} | {source} | {snapshot_slot} | {len(forecast_raw)} точек")

    # ------------------------------------------------------------------ #
    # Wunderground
    # ------------------------------------------------------------------ #

    def scrape_wunderground(self, icao, date_str):
        """
        Возвращает список сырых почасовых точек прогноза текущего дня:
        [{"time_text": "9 am", "temp_text": "72°F"}, ...]
        """
        url = self.cities[icao]["weather_url"] + f"/date/{date_str}"
        self.logger.info(f"🔗 [WU] Открываю: {url}")
        forecast = []
        try:
            self.driver.execute_script(f"window.open('{url}', '_blank');")
            self.driver.switch_to.window(self.driver.window_handles[-1])
            wait = WebDriverWait(self.driver, 10)

            try:
                wait.until(lambda d: date_str in d.current_url)
            except Exception:
                self.logger.error(f"❌ [WU] URL не соответствует {date_str}, пропуск")
                self._safe_close_tab()
                return forecast

            tbody_xpath = (
                "/html/body/app-root/app-hourly/one-column-layout/wu-header/sidenav/"
                "mat-sidenav-container/mat-sidenav-content/div[2]/section/div[3]/div[1]/"
                "div/div[1]/div/lib-city-hourly-forecast/div/div[4]/div[1]/table/tbody"
            )
            tbody = wait.until(EC.presence_of_element_located((By.XPATH, tbody_xpath)))
            time.sleep(self.page_load_wait_sec)  # даём дорисоваться всем строкам

            rows = tbody.find_elements(By.TAG_NAME, "tr")
            for i, row in enumerate(rows, start=1):
                try:
                    time_text = row.find_element(By.XPATH, "./td[1]/span").text.strip()
                    temp_text = row.find_element(By.XPATH, "./td[3]/lib-display-unit/span").text.strip()
                    if time_text and temp_text:
                        forecast.append({"time_text": time_text, "temp_text": temp_text})
                except Exception as e:
                    self.logger.debug(f"⚠️ [WU] Пропуск строки {i}: {e}")
                    continue

            self.logger.info(f"📈 [WU] {icao}: собрано {len(forecast)} точек")
            self._safe_close_tab()
            return forecast
        except Exception as e:
            self.logger.error(f"❌ [WU] Ошибка {icao}: {e}")
            if not self._check_vpn_dropped(f"scrape_wunderground:{icao}"):
                self.notify_error(f"scrape_wunderground:{icao}", e)
            self._safe_close_tab()
            return forecast

    # ------------------------------------------------------------------ #
    # Wunderground -- безбраузерный метод через api.weather.com (см.
    # config.wunderground_api). Два шага, как делает сам сайт:
    #   1. v3/location/point -- резолвим ICAO в точный geocode (lat/lon);
    #   2. v3/wx/forecast/hourly/15day -- прогноз ПО ЭТОМУ geocode (не по
    #      icaoCode напрямую -- см. переписку про расхождения с сайтом).
    # Возвращает ТОТ ЖЕ формат, что и scrape_wunderground (Selenium), чтобы
    # ничего ниже по цепочке (_append_record, price_monitor._forecast_max,
    # accuracy_report.parse_wu_value) не пришлось менять.
    # ------------------------------------------------------------------ #

    def scrape_wunderground_api(self, icao, date_str):
        """
        Возвращает список сырых почасовых точек прогноза текущего дня:
        [{"time_text": "9am", "temp_text": "72°F"}, ...] -- без Chrome.
        """
        forecast = []
        if not self.wunderground_api_key:
            self.logger.error("❌ [WU-API] wunderground_api.key не задан в конфиге, пропуск")
            return forecast
        try:
            point_url = (
                "https://api.weather.com/v3/location/point"
                f"?apiKey={self.wunderground_api_key}&language=en-US&icaoCode={icao}&format=json"
            )
            point_resp = requests.get(
                point_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=self.wunderground_api_timeout_sec
            )
            point_resp.raise_for_status()
            location = point_resp.json().get("location", {})
            lat, lon = location.get("latitude"), location.get("longitude")
            if lat is None or lon is None:
                self.logger.error(f"❌ [WU-API] {icao}: не удалось получить geocode из location/point")
                return forecast

            forecast_url = (
                "https://api.weather.com/v3/wx/forecast/hourly/15day"
                f"?apiKey={self.wunderground_api_key}&language=en-US&units=e&format=json&geocode={lat},{lon}"
            )
            resp = requests.get(
                forecast_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=self.wunderground_api_timeout_sec
            )
            resp.raise_for_status()
            data = resp.json()

            times = data.get("validTimeLocal", [])
            temps = data.get("temperature", [])
            for t, v in zip(times, temps):
                if not t.startswith(date_str) or v is None:
                    continue
                dt = datetime.fromisoformat(t)
                hour12 = dt.hour % 12 or 12
                ampm = "am" if dt.hour < 12 else "pm"
                time_text = f"{hour12}{ampm}"
                temp_text = f"{round(v)}°F"
                forecast.append({"time_text": time_text, "temp_text": temp_text})

            self.logger.info(f"📈 [WU-API] {icao}: собрано {len(forecast)} точек")
            return forecast
        except requests.exceptions.RequestException as e:
            self.logger.error(f"❌ [WU-API] Ошибка {icao}: {e}")
            self._report_api_connection_down("Wunderground")
            return forecast
        except Exception as e:
            self.logger.error(f"❌ [WU-API] Ошибка {icao}: {e}")
            self.notify_error(f"scrape_wunderground_api:{icao}", e)
            return forecast

    # ------------------------------------------------------------------ #
    # Windy
    # ------------------------------------------------------------------ #

    def scrape_windy(self, icao):
        """
        Возвращает список сырых почасовых точек прогноза (одна таблица, без
        разреза по моделям): [{"time_text": "9am", "temp_text": "28°"}, ...].
        Обрезает на windy_cutoff_label включительно (дальше — прогноз на завтра).
        """
        url = self.cities[icao]["windy_url"]
        self.logger.info(f"🔗 [Windy] Открываю: {url}")
        forecast = []
        try:
            self.driver.execute_script(f"window.open('{url}', '_blank');")
            self.driver.switch_to.window(self.driver.window_handles[-1])
            wait = WebDriverWait(self.driver, 10)

            hour_row_xpath = "/html/body/div[3]/span[5]/div/section/section[1]/div[2]/div/div/table/tbody/tr[2]"
            temp_row_xpath = "/html/body/div[3]/span[5]/div/section/section[1]/div[2]/div/div/table/tbody/tr[4]"

            hour_row = wait.until(EC.presence_of_element_located((By.XPATH, hour_row_xpath)))
            time.sleep(self.page_load_wait_sec)  # даём таблице догрузиться

            temp_row = self.driver.find_element(By.XPATH, temp_row_xpath)

            hour_cells = hour_row.find_elements(By.TAG_NAME, "td")
            temp_cells = temp_row.find_elements(By.TAG_NAME, "td")

            for time_cell, temp_cell in zip(hour_cells, temp_cells):
                time_text = time_cell.text.strip()
                temp_text = temp_cell.text.strip()

                normalized = time_text.replace(" ", "").lower()
                if not TIME_CELL_RE.match(normalized) or not temp_text:
                    continue

                forecast.append({"time_text": time_text, "temp_text": temp_text})
                if normalized == self.windy_cutoff_label:
                    break

            self.logger.info(f"📈 [Windy] {icao}: собрано {len(forecast)} точек")
            self._safe_close_tab()
            return forecast
        except Exception as e:
            self.logger.error(f"❌ [Windy] Ошибка {icao}: {e}")
            self.notify_error(f"scrape_windy:{icao}", e)
            self._safe_close_tab()
            return forecast

    # ------------------------------------------------------------------ #
    # Polymarket
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_temp_label(question):
        """
        "Will the highest temperature ... be between 78-79°F on August 24?" -> "78-79°F"
        "Will the highest temperature ... be 86°F or higher on August 24?" -> "86°F or higher"
        None, если паттерн температуры в вопросе не найден.
        """
        match = re.search(r"\d+-\d+°[CF]|\d+°[CF](?:\s+or\s+(?:below|above|higher|lower))?", question)
        return match.group(0) if match else None

    def scrape_polymarket(self, icao, now_local):
        """
        Возвращает ВСЕ ставки события Polymarket по городу/дате:
        [{"label": "78-79°F", "price": "0.4¢"}, ...], по возрастанию температуры.
        Читает Gamma API (events?slug=...) во вкладке того же Chrome, что и WU/Windy.
        """
        slug = self.cities[icao]["slug"]
        month, day, year = now_local.strftime("%B").lower(), now_local.day, now_local.year
        full_url = f"https://polymarket.com/event/highest-temperature-in-{slug}-on-{month}-{day}-{year}"
        api_url = f"https://gamma-api.polymarket.com/events?slug={full_url.rstrip('/').split('/event/')[-1].split('?')[0]}"
        self.logger.info(f"🔗 [Poly] Открываю: {api_url}")

        bets = []
        try:
            self.driver.execute_script(f"window.open('{api_url}', '_blank');")
            self.driver.switch_to.window(self.driver.window_handles[-1])
            wait = WebDriverWait(self.driver, 10)
            pre = wait.until(EC.presence_of_element_located((By.TAG_NAME, "pre")))

            events = json.loads(pre.text)
            if not events:
                self.logger.info(f"ℹ️ [Poly] Событие не найдено: {icao}")
                self._safe_close_tab()
                return bets

            event = events[0]
            raw_bets = []
            for market in event.get("markets", []):
                question = market["question"]
                outcomes = json.loads(market["outcomes"])
                prices = json.loads(market["outcomePrices"])
                if "Yes" not in outcomes:
                    continue

                yes_idx = outcomes.index("Yes")
                buy_yes_cents = round(float(prices[yes_idx]) * 100, 2)

                label = self._extract_temp_label(question)
                if not label:
                    continue

                t_match = re.search(r"-?\d+", label)
                if not t_match:
                    continue

                raw_bets.append({"t": int(t_match.group()), "label": label, "price": f"{buy_yes_cents}¢"})

            raw_bets.sort(key=lambda x: x["t"])
            bets = [{"label": b["label"], "price": b["price"]} for b in raw_bets]

            self.logger.info(f"📊 [Poly] {icao}: собрано {len(bets)} ставок")
            self._safe_close_tab()
            return bets
        except Exception as e:
            self.logger.error(f"❌ [Poly] Ошибка {icao}: {e}")
            if not self._check_vpn_dropped(f"scrape_polymarket:{icao}"):
                self.notify_error(f"scrape_polymarket:{icao}", e)
            self._safe_close_tab()
            return bets

    # ------------------------------------------------------------------ #
    # Polymarket Gamma -- безбраузерный метод (обычный requests.get к
    # gamma-api.polymarket.com). В отличие от Wunderground, Chrome тут
    # раньше использовался ЛИШЬ как HTTP-клиент (открывал ссылку на голый
    # публичный JSON-эндпоинт и читал текст из <pre>) -- никакого JS/рендера
    # сайта не требовалось, поэтому перенос на requests не меняет ни данные,
    # ни структуру ответа, только транспорт. Логика ниже — 1:1 копия разбора
    # ответа из scrape_polymarket, просто без Selenium.
    # ------------------------------------------------------------------ #

    def scrape_polymarket_api(self, icao, now_local):
        """
        Возвращает ВСЕ ставки события Polymarket по городу/дате:
        [{"label": "78-79°F", "price": "0.4¢"}, ...], по возрастанию температуры.
        Читает Gamma API (events?slug=...) напрямую, без Chrome.
        """
        slug = self.cities[icao]["slug"]
        month, day, year = now_local.strftime("%B").lower(), now_local.day, now_local.year
        full_url = f"https://polymarket.com/event/highest-temperature-in-{slug}-on-{month}-{day}-{year}"
        api_url = f"https://gamma-api.polymarket.com/events?slug={full_url.rstrip('/').split('/event/')[-1].split('?')[0]}"
        self.logger.info(f"🔗 [Poly-API] Открываю: {api_url}")

        bets = []
        try:
            resp = requests.get(api_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=self.wunderground_api_timeout_sec)
            resp.raise_for_status()
            events = resp.json()

            if not events:
                self.logger.info(f"ℹ️ [Poly-API] Событие не найдено: {icao}")
                return bets

            event = events[0]
            raw_bets = []
            for market in event.get("markets", []):
                question = market["question"]
                outcomes = json.loads(market["outcomes"])
                prices = json.loads(market["outcomePrices"])
                if "Yes" not in outcomes:
                    continue

                yes_idx = outcomes.index("Yes")
                buy_yes_cents = round(float(prices[yes_idx]) * 100, 2)

                label = self._extract_temp_label(question)
                if not label:
                    continue

                t_match = re.search(r"-?\d+", label)
                if not t_match:
                    continue

                raw_bets.append({"t": int(t_match.group()), "label": label, "price": f"{buy_yes_cents}¢"})

            raw_bets.sort(key=lambda x: x["t"])
            bets = [{"label": b["label"], "price": b["price"]} for b in raw_bets]

            self.logger.info(f"📊 [Poly-API] {icao}: собрано {len(bets)} ставок")
            return bets
        except requests.exceptions.RequestException as e:
            self.logger.error(f"❌ [Poly-API] Ошибка {icao}: {e}")
            self._report_api_connection_down("Polymarket")
            return bets
        except Exception as e:
            self.logger.error(f"❌ [Poly-API] Ошибка {icao}: {e}")
            self.notify_error(f"scrape_polymarket_api:{icao}", e)
            return bets

    # ------------------------------------------------------------------ #
    # Суточная сводка по одному городу (после его последнего слота)
    # ------------------------------------------------------------------ #

    def _iter_day_records(self, date_str):
        """Итерирует ВСЕ записи (по каждому из трёх источников — см.
        source_dirs) за date_str, по одному файлу на источник."""
        for source in self.source_dirs:
            path = self._output_path(source, date_str)
            if not os.path.exists(path):
                continue
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    yield json.loads(line)

    def _build_daily_summary(self, icao, date_str):
        city_name = self.cities[icao]["name"]
        records = list(self._iter_day_records(date_str))
        if not records:
            return f"⚠️ Сводка {city_name} ({icao}) за {date_str}: данных нет вообще."

        slots = {slot: {"wunderground": None, "windy": None, "polymarket": None} for slot in self.snapshot_slots.values()}
        for rec in records:
            if rec["icao"] != icao or rec["snapshot_slot"] not in slots:
                continue
            if rec["source"] == "wunderground":
                slots[rec["snapshot_slot"]]["wunderground"] = len(rec["forecast_raw"])
            elif rec["source"] == "windy":
                slots[rec["snapshot_slot"]]["windy"] = len(rec["forecast_raw"])
            elif rec["source"] == "polymarket":
                slots[rec["snapshot_slot"]]["polymarket"] = len(rec["forecast_raw"])

        has_wu_source = "wunderground.com" in self.cities[icao]["weather_url"]

        lines = [f"📋 <b>Сводка майнинга {city_name} ({icao})</b> за {date_str}:"]
        for slot in sorted(slots.keys()):
            wu = slots[slot]["wunderground"]
            wu_str = ("WU — (н/д)" if not has_wu_source else (f"WU {wu}✅" if wu else "WU ❌"))
            windy = slots[slot]["windy"]
            windy_str = f"Windy {windy}✅" if windy else "Windy ❌"
            poly = slots[slot]["polymarket"]
            poly_str = f"Poly {poly}✅" if poly else "Poly ❌"
            lines.append(f"{slot} — {wu_str} | {windy_str} | {poly_str}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Глобальная суточная сводка по ВСЕМ городам
    # ------------------------------------------------------------------ #

    def _build_global_daily_summary(self, date_str):
        lines = [f"📋 <b>Сводка прогноза</b> за {date_str} #Forecast_Summary"]
        records = list(self._iter_day_records(date_str))
        if not records:
            lines.append("Данных за эту дату нет вообще.")
            return "\n".join(lines)

        total_slots = len(self.snapshot_slots)
        counts = {icao: {"wunderground": 0, "windy": 0, "polymarket": 0} for icao in self.cities}

        for rec in records:
            icao = rec.get("icao")
            if icao not in counts or not rec.get("forecast_raw"):
                continue
            if rec["source"] in counts[icao]:
                counts[icao][rec["source"]] += 1

        for icao, info in self.cities.items():
            has_wu_source = "wunderground.com" in info["weather_url"]
            wu_count = counts[icao]["wunderground"]
            windy_count = counts[icao]["windy"]

            wu_str = "WU —(н/д)" if not has_wu_source else f"WU {wu_count}/{total_slots}{'✅' if wu_count == total_slots else '⚠️'}"
            parts = [wu_str]
            # Windy не показываем в сводке вообще, если майнинг Windy выключен
            # в конфиге (windy_enabled: false) -- строка по нему тогда всегда
            # была бы "0/N⚠️" и только шумела бы, а не отражала реальную проблему.
            if self.windy_enabled:
                windy_str = f"Windy {windy_count}/{total_slots}{'✅' if windy_count == total_slots else '⚠️'}"
                parts.append(windy_str)
            # Gamma (poly) в эту построчную сводку по прогнозу больше не входит --
            # это отдельный источник (ставки, не прогноз), см. #Forecast_Summary.

            lines.append(f"{info['name']}: {' '.join(parts)}")

        return "\n".join(lines)

    def _maybe_send_global_daily_summary(self):
        """
        Шлёт глобальную сводку раз в сутки, в фиксированное окно
        [global_summary_trigger_hour_utc:00, +global_summary_trigger_minute_window)
        по UTC — а не по факту смены календарной даты UTC (как было раньше,
        что фактически привязывало отправку к полуночному слоту Веллингтона).
        Сводка — за ТЕКУЩИЕ сутки UTC на момент срабатывания (см. пояснение в
        config.yaml), не за "вчера".
        """
        if not self.send_global_daily_summary:
            return
        now_utc = datetime.now(pytz.utc)
        today_utc = now_utc.strftime("%Y-%m-%d")
        if self._last_global_summary_utc_date == today_utc:
            return  # уже отправляли сегодня
        if now_utc.hour != self.global_summary_trigger_hour_utc:
            return
        if now_utc.minute >= self.global_summary_trigger_minute_window:
            return
        summary = self._build_global_daily_summary(today_utc)
        self.telegram.send_message(summary)
        self.logger.info(f"📋 Отправлена глобальная суточная сводка за {today_utc}")
        self._last_global_summary_utc_date = today_utc

    # ------------------------------------------------------------------ #
    # Снятие снапшота по городу
    # ------------------------------------------------------------------ #

    def _read_forecast(self, date_str, icao, source, slot_label):
        """
        Читает forecast_raw из уже записанного JSONL-файла дня для конкретных
        (icao, source, slot_label) — фолбэк для PriceMonitor на случай, если
        нужный источник был смайнен в ОДНОМ из предыдущих проходов run_cycle
        (например, Windy не ответил вовремя в тот же вызов, что и Polymarket),
        а не в текущем вызове mine_city. Возвращает None, если записи нет.
        """
        path = self._output_path(source, date_str)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("icao") == icao and rec.get("source") == source and rec.get("snapshot_slot") == slot_label:
                    return rec.get("forecast_raw")
        return None

    def mine_city(self, icao, now_local, slot_label):
        date_str = now_local.strftime("%Y-%m-%d")
        snapshot_ts = now_local.isoformat()

        wu_data = None
        windy_data = None

        if "wunderground.com" in self.cities[icao]["weather_url"]:
            wu_key = (date_str, icao, "wunderground", slot_label)
            if wu_key not in self.written_keys:
                if self.wunderground_mining_method == "api":
                    wu_data = self.scrape_wunderground_api(icao, date_str)
                else:
                    wu_data = self.scrape_wunderground(icao, date_str)
                if wu_data:
                    self._append_record(date_str, icao, "wunderground", slot_label, snapshot_ts, wu_data)
        else:
            self.logger.debug(f"⏭️ [WU] {icao}: источник не Wunderground, пропуск")

        if self.windy_enabled:
            windy_key = (date_str, icao, "windy", slot_label)
            if windy_key not in self.written_keys:
                windy_data = self.scrape_windy(icao)
                if windy_data:
                    self._append_record(date_str, icao, "windy", slot_label, snapshot_ts, windy_data)
        # windy_enabled=false -- пропуск без лога: флаг статичен на весь запуск,
        # напоминание об этом даётся ОДИН раз в __init__ (иначе строка сыпалась
        # бы в лог на КАЖДЫЙ город КАЖДОГО круга -- см. там же).

        # Gamma больше НЕ майнится WeatherMiner'ом отдельным запросом (см.
        # git-историю и price_monitor.py/PriceMonitor.gamma_bets_fetcher) —
        # PriceMonitor сам запрашивает её живьём, ровно в момент проверки
        # сигнала, тем же Gamma-кэшем, что и так используется под CLOB
        # (PolymarketClobMiner.fetch_gamma_bets/fetch_live_price/
        # fetch_live_top_bucket) — один запрос на событие в сутки вместо
        # двух. poly_data/poly_key больше не нужны.

        # Прогноз (WU/Windy) для проверки может быть взят и из более раннего
        # прохода — тогда читаем его из уже записанного файла.
        if self.price_monitor and slot_label == self.price_monitor.monitor_slot and icao in self.price_monitor.watched:
            self.price_monitor.check_city_snapshot(
                icao,
                self.cities[icao],
                date_str,
                wu_data or self._read_forecast(date_str, icao, "wunderground", slot_label),
                windy_data or self._read_forecast(date_str, icao, "windy", slot_label),
                now_local=now_local,
            )

        # Второй, независимый набор — таргет = бакет самой дорогой ставки
        # (без прогноза погоды вовсе), см. watched_icaos_max_bet.yaml.
        if (
            self.price_monitor
            and slot_label == self.price_monitor.monitor_slot
            and icao in self.price_monitor.watched_max_bet
        ):
            self.price_monitor.check_city_snapshot_max_bet(icao, self.cities[icao], now_local=now_local)

        if slot_label == self.last_slot_label and self.send_daily_summary:
            summary = self._build_daily_summary(icao, date_str)
            self.telegram.send_message(summary)

    # ------------------------------------------------------------------ #
    # Один проход и автономный цикл
    # ------------------------------------------------------------------ #

    def run_cycle(self):
        """
        Один проход по всем городам: для каждого проверяет текущий локальный
        час и, если он совпадает с одним из snapshot_slots, снимает снапшот.
        Не блокирует и не спит — вызывающая сторона (Orchestrator либо run())
        сама решает, когда делать паузу между проходами.
        """
        self._maybe_send_global_daily_summary()

        for icao, info in self.cities.items():
            try:
                if self.chrome_needed:
                    self.ensure_driver()

                tz = pytz.timezone(info["tz"])
                now_local = datetime.now(tz)

                slot_label = self.snapshot_slots.get(now_local.hour)
                if slot_label:
                    self.logger.info(f"🔎 {info['name']} — слот {slot_label} ({now_local.strftime('%Y-%m-%d %H:%M')})")
                    self.mine_city(icao, now_local, slot_label)

            except Exception as e:
                self.logger.error(f"⚠️ Ошибка на {icao}: {e}")
                self.notify_error(f"run_cycle:{icao}", e)
                continue

    def run(self, cycle_sleep_sec=None):
        """
        Автономный запуск (standalone-использование ВНЕ Orchestrator):
        бесконечный цикл run_cycle() + сон. Внутри проекта эту роль выполняет
        Orchestrator.run(), который сам вставляет паузы под остальные скрипты.
        """
        cycle_sleep_sec = cycle_sleep_sec or self.config.weather_miner.cycle_sleep_sec
        self.logger.info("🚀 Майнер погоды запущен (автономный режим).")
        self.telegram.send_message("✅ <b>Майнер погоды запущен</b>")
        try:
            while True:
                self.run_cycle()
                self.logger.info(f"Круг завершен. Спим {cycle_sleep_sec} сек.")
                time.sleep(cycle_sleep_sec)
        except KeyboardInterrupt:
            self.logger.info("⛔ Майнер погоды остановлен вручную.")
            self.telegram.send_message("⛔ <b>Майнер погоды остановлен вручную</b>")
