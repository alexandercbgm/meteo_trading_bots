"""
polymarket_clob_miner.py

Класс PolymarketClobMiner — реальные bid/ask по КАЖДОМУ бакету (Yes и No)
события Polymarket, через CLOB API (POST /books) — обычные HTTP-запросы
(requests), БЕЗ Selenium/браузера: и Gamma API (каталог событий/бакетов), и
CLOB API (сам ордербук) — публичные, не требующие авторизации JSON-эндпоинты.

Слот — 15-минутная сетка (config.polymarket_clob_miner.slot_minutes, по
умолчанию :00/:15/:30/:45) по ЛОКАЛЬНОМУ времени КАЖДОГО города, только в
дневном окне [slot_start_hour, slot_end_hour) местного времени. На каждом
проходе run_cycle() сначала собирает СПИСОК городов, для которых наступил
ещё не снятый слот ("due"), затем РЕЗОЛВИТ бакеты каждого через Gamma API
(с кэшем на (icao, дата) — бакеты события не меняются в течение дня, повторно
не запрашиваются), и ОДНИМ батч-запросом (или несколькими по
clob_batch_chunk_size, если токенов больше лимита) забирает ордербуки ВСЕХ
токенов (Yes+No, по ВСЕМ due-городам сразу) через CLOB /books — то есть
несколько городов на один и тот же слот действительно объединяются в один
сетевой запрос, а не идут по одному.

Пишет по записи на (дата, город, слот, бакет) построчно (JSONL) в
config.paths.polymarket_clob_dir, по файлу на сутки (локальная дата города):
{"date", "icao", "city", "snapshot_slot", "snapshot_timestamp", "bucket",
 "bid_yes", "ask_yes", "bid_no", "ask_no"}.

Best bid/ask считаются ЯВНО через max()/min() по цене (а не по позиции в
массиве bids/asks) — официальные доки Polymarket утверждают, что bids
отсортированы по возрастанию, а asks по убыванию (т.е. "лучшая" запись
формально последняя), но полагаться на порядок рискованно: есть открытый баг
(Polymarket/py-clob-client issue #180), когда /book и /books иногда отдают
"заглушенный" ответ (0.01/0.99) при живых котировках.

Как и у WeatherMiner, run_cycle() делает ОДИН проход и возвращается — этим
управляет Orchestrator; run() — обёртка для автономного запуска (вне
Orchestrator) с собственным циклом сна.
"""

import json
import os
import re
import time
from datetime import datetime

import pytz
import requests

from .base_miner import VPN_ALERT_COOLDOWN_SEC, build_logger

GAMMA_BASE = "https://gamma-api.polymarket.com"


def _price_cents(price_str):
    """"40.5¢" -> 40.5; None, если не распарсилось (то же, что accuracy_report.parse_price_cents)."""
    m = re.search(r"\d+(?:\.\d+)?", price_str or "")
    return float(m.group()) if m else None

CLOB_BASE = "https://clob.polymarket.com"

# Ошибки TCP-соединения/таймаута (сервер недоступен, DNS не резолвится,
# "Connection refused" и т.п.) -- надёжный признак того, что до
# Polymarket не достучаться СЕТЕВЫМ уровнем, а не просто "API вернул
# ошибку". Этот майнер работает через requests (без Selenium/браузера,
# см. шапку модуля), поэтому BaseSeleniumMiner._check_vpn_dropped (который
# смотрит текст НА СТРАНИЦЕ браузера) сюда не подходит -- нужна отдельная,
# но по смыслу такая же проверка на отвалившийся VPN.
_CONNECTION_ERROR_TYPES = (requests.exceptions.ConnectionError, requests.exceptions.Timeout)

TEMP_LABEL_RE = re.compile(r"\d+-\d+°[CF]|\d+°[CF](?:\s+or\s+(?:below|above|higher|lower))?")


class PolymarketClobMiner:
    def __init__(self, config, cities, telegram, logger=None):
        self.config = config
        self.cities = cities
        self.telegram = telegram
        self.output_dir = config.paths.polymarket_clob_dir

        cm = config.polymarket_clob_miner
        self.slot_start_hour = cm.slot_start_hour
        self.slot_end_hour = cm.slot_end_hour
        self.slot_minutes = set(cm.slot_minutes)
        self.batch_chunk_size = cm.clob_batch_chunk_size
        self.cycle_sleep_sec = cm.cycle_sleep_sec

        self.logger = logger or build_logger(
            "PolymarketClobMiner", os.path.join(config.paths.log_dir, "polymarket_clob_miner"),
            "polymarket_clob_miner.log",
        )

        self._live_books_cache = {}  # {(icao, date, slot): books} -- см. _get_slot_books
        self._gamma_slot_prices = {}   # {(icao, date, slot): {label: "40.5¢"}} -- свежие цены Gamma на момент проверки
        self._gamma_slots = set()      # {(icao, date, slot)}: CLOB в этот слот недоступен, весь слот на ценах Gamma
        self._gamma_recorded = set()   # {(icao, date, slot, label)}: цена Gamma для бакета уже записана в журнал
        self.written_keys = self._load_existing_keys()  # {(date, icao, slot)}
        self._gamma_cache = {}  # {(icao, date): [{"label","yes_token","no_token"}, ...]}
        self._vpn_last_alert_ts = None  # time.time() последнего сетевого VPN-алерта -- см. _report_connection_down

    # ------------------------------------------------------------------ #
    # Инфраструктура вывода
    # ------------------------------------------------------------------ #

    def _output_path(self, date_str):
        os.makedirs(self.output_dir, exist_ok=True)
        return os.path.join(self.output_dir, f"polymarket_clob_{date_str.replace('-', '_')}.jsonl")

    def _load_existing_keys(self):
        """При старте сканирует свои polymarket_clob_*.jsonl и запоминает уже
        снятые (date, icao, slot) — чтобы не задваивать данные после
        перезапуска (гранулярность дедупа — слот, а не отдельный бакет:
        снапшот слота пишется/не пишется целиком)."""
        keys = set()
        if not os.path.isdir(self.output_dir):
            return keys
        for fname in os.listdir(self.output_dir):
            if not (fname.startswith("polymarket_clob_") and fname.endswith(".jsonl")):
                continue
            path = os.path.join(self.output_dir, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        rec = json.loads(line)
                        keys.add((rec["date"], rec["icao"], rec["snapshot_slot"]))
            except Exception as e:
                self.logger.warning(f"⚠️ Не удалось прочитать {path}: {e}")
        self.logger.info(f"📂 Загружено {len(keys)} уже снятых слотов из 1 файла max_bet")
        return keys

    # ------------------------------------------------------------------ #
    # Gamma API — резолв бакетов события в clobTokenIds
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_temp_label(question):
        match = TEMP_LABEL_RE.search(question or "")
        return match.group(0) if match else None

    def _fetch_event_markets(self, icao, now_local):
        """
        Gamma API: [{"label","price","yes_token","no_token"}, ...] по событию
        города на ТЕКУЩУЮ (для этого города) локальную дату — тот же шаблон
        ссылки, что раньше был в WeatherMiner.scrape_polymarket/scrape_polymarket_api.

        ВАЖНО: это ЕДИНСТВЕННОЕ место в проекте, которое ходит в Gamma
        /events (раньше WeatherMiner майнил Gamma ОТДЕЛЬНО, своим циклом —
        см. git-историю; это дублировало ровно этот же запрос на то же
        событие). Кэш self._gamma_cache используется и здесь (для
        yes_token/no_token под CLOB), и в fetch_gamma_bets ниже (для
        label+price, нужных PriceMonitor — см. price_monitor.py,
        gamma_bets_fetcher) — так что на практике Gamma запрашивается один
        раз в сутки на город, кто бы её первым ни затребовал.
        """
        slug = self.cities[icao]["slug"]
        month, day, year = now_local.strftime("%B").lower(), now_local.day, now_local.year
        event_slug = f"highest-temperature-in-{slug}-on-{month}-{day}-{year}"
        try:
            resp = requests.get(f"{GAMMA_BASE}/events", params={"slug": event_slug}, timeout=15)
            resp.raise_for_status()
            events = resp.json()
        except Exception as e:
            self.logger.error(f"❌ [CLOB-API] Gamma API ошибка {icao}: {e}")
            if isinstance(e, _CONNECTION_ERROR_TYPES):
                self._report_connection_down("Polymarket")
            return []
        if not events:
            self.logger.info(f"ℹ️ [CLOB-API] Событие не найдено: {icao} ({event_slug})")
            return []

        event = events[0]
        markets = []
        for market in event.get("markets", []):
            clob_ids_raw = market.get("clobTokenIds")
            if not clob_ids_raw:
                continue
            clob_ids = json.loads(clob_ids_raw) if isinstance(clob_ids_raw, str) else clob_ids_raw
            if len(clob_ids) < 2:
                continue
            question = market.get("question", "")
            label = self._extract_temp_label(question) or market.get("groupItemTitle") or question

            # Цена Yes из Gamma (последняя зафиксированная сделка/котировка,
            # НЕ реальный ask из ордербука) -- нужна только для
            # fetch_gamma_bets ниже (PriceMonitor определяет по ней шкалу
            # F/C и таргет-бакет); для token_id/CLOB она не используется
            # вовсе, поэтому её отсутствие/ошибка парсинга здесь не повод
            # выбрасывать весь бакет -- он всё равно нужен для CLOB.
            price = None
            try:
                outcomes = json.loads(market["outcomes"])
                prices = json.loads(market["outcomePrices"])
                if "Yes" in outcomes:
                    yes_idx = outcomes.index("Yes")
                    price = f"{round(float(prices[yes_idx]) * 100, 2)}¢"
            except (KeyError, ValueError, TypeError):
                pass

            markets.append({"label": label, "price": price, "yes_token": clob_ids[0], "no_token": clob_ids[1]})
        return markets

    def fetch_gamma_bets(self, icao, now_local):
        """
        Живой список ставок события Gamma -- [{"label","price"}, ...], тот
        же формат, что раньше отдавал WeatherMiner.scrape_polymarket/
        scrape_polymarket_api (Gamma-майнинг в WeatherMiner убран целиком,
        см. git-историю и комментарий в _fetch_event_markets выше).

        Используется PriceMonitor (price_monitor.py, self.gamma_bets_fetcher,
        инжектируется как Orchestrator._fetch_gamma_bets_now) для определения
        шкалы F/C и таргет-бакета -- РЕАЛЬНАЯ цена сигнала по-прежнему берётся
        отдельно, из CLOB (fetch_live_price/fetch_live_top_bucket ниже), цена
        Gamma тут используется только как запасной вариант на случай, если
        живой CLOB недоступен (см. price_monitor.py, _resolve_live_price/
        _resolve_top_bucket).

        Тот же self._gamma_cache на (icao, дата), что и fetch_live_price/
        fetch_live_top_bucket -- при штатной работе (что бы из трёх методов
        ни вызвалось первым на эту дату) отдельного Gamma-запроса для
        остальных двух уже не будет.
        """
        date_str = now_local.strftime("%Y-%m-%d")
        cache_key = (icao, date_str)
        if cache_key not in self._gamma_cache:
            self._gamma_cache[cache_key] = self._fetch_event_markets(icao, now_local)
        markets = self._gamma_cache[cache_key]

        # Цены Gamma в кэше события -- на момент ПЕРВОГО обращения за день (обычно ~06:00, первый
        # слот майнера), а не на момент проверки. Поэтому на слот проверки один раз (на город и
        # слот, оба набора стратегий делят результат) запрашиваем события заново и берём свежие
        # цены -- именно они идут в сигнал при откате на Gamma и в журнал (_record_gamma_fallback).
        # Токены/бакеты по-прежнему из кэша. Сбой запроса -- тихо остаёмся на цене из кэша.
        slot_label = self._current_slot_label(now_local)
        prices = {}
        if slot_label is not None:
            slot_key = (icao, date_str, slot_label)
            if slot_key not in self._gamma_slot_prices:
                for k in [k for k in self._gamma_slot_prices if k[1] != date_str]:
                    del self._gamma_slot_prices[k]
                fresh = self._fetch_event_markets(icao, now_local)
                self._gamma_slot_prices[slot_key] = {m["label"]: m["price"] for m in fresh if m.get("price")}
                if fresh and not markets:
                    # первый запрос за день упал и закэшировал пустой список -- не оставляем его на сутки
                    self._gamma_cache[cache_key] = markets = fresh
            prices = self._gamma_slot_prices[slot_key]
        return [
            {"label": m["label"], "price": prices.get(m["label"]) or m["price"]}
            for m in markets if (prices.get(m["label"]) or m.get("price")) is not None
        ]

    # ------------------------------------------------------------------ #
    # CLOB API — батч-ордербуки
    # ------------------------------------------------------------------ #

    def _fetch_order_books(self, token_ids):
        """{token_id: {"bids": [...], "asks": [...]}} — батч-запрос(ы), до
        batch_chunk_size токенов за раз (лимит API)."""
        result = {}
        for i in range(0, len(token_ids), self.batch_chunk_size):
            chunk = token_ids[i:i + self.batch_chunk_size]
            payload = [{"token_id": t} for t in chunk]
            try:
                resp = requests.post(f"{CLOB_BASE}/books", json=payload, timeout=20)
                resp.raise_for_status()
                for book in resp.json():
                    result[book["asset_id"]] = book
            except Exception as e:
                self.logger.error(f"❌ [CLOB-API] /books ошибка (чанк {i}-{i + len(chunk)}): {e}")
                if isinstance(e, _CONNECTION_ERROR_TYPES):
                    self._report_connection_down("Polymarket")
        return result

    # ------------------------------------------------------------------ #
    # VPN-алерт при сетевой недоступности Polymarket (см. константу
    # _CONNECTION_ERROR_TYPES выше) -- Telegram + звонок через CallMeBot,
    # тот же паттерн/дедуп, что у BaseSeleniumMiner._check_vpn_dropped, но
    # своим отдельным окном (этот майнер -- отдельный процесс/объект).
    # ------------------------------------------------------------------ #

    def _report_connection_down(self, resource):
        now = time.time()
        if self._vpn_last_alert_ts is not None and (now - self._vpn_last_alert_ts) < VPN_ALERT_COOLDOWN_SEC:
            return
        self._vpn_last_alert_ts = now

        if self.telegram is not None:
            self.telegram.send_message(f"🔴 Не удалось выполнить запрос к API {resource}, проверьте VPN.")

        vpn_cfg = getattr(self.config, "vpn_check", None)
        if vpn_cfg and getattr(vpn_cfg, "call_alert_enabled", False):
            self._call_alert(
                "VPN dropped",
                getattr(vpn_cfg, "call_user", None),
                getattr(vpn_cfg, "call_max_attempts", 3),
                getattr(vpn_cfg, "call_retry_delay_sec", 3),
            )

    def _call_alert(self, message, call_user, call_max_attempts, call_retry_delay_sec):
        """Звонок через CallMeBot -- тот же паттерн, что в BaseSeleniumMiner/
        PriceMonitor/TradingEngine. При сетевых сбоях (например, "Read timed
        out" -- CallMeBot иногда не отвечает вовремя) повторяет до
        call_max_attempts раз с паузой call_retry_delay_sec между попытками."""
        if not call_user:
            self.logger.warning(f"⚠️ [{type(self).__name__}] call_alert_enabled=true, но call_user не задан")
            return
        url = f"http://api.callmebot.com/start.php?source=web&user={call_user}&text={message}&lang=ru-RU-Standard-A"

        last_exc = None
        for attempt in range(1, call_max_attempts + 1):
            try:
                resp = requests.get(url, timeout=10)
                if resp.status_code == 200:
                    self.logger.info(f"📞 Звонок инициирован (попытка {attempt}/{call_max_attempts})")
                else:
                    self.logger.info(
                        f"📞 Звонок инициирован (попытка {attempt}/{call_max_attempts}) — "
                        f"ответ CallMeBot [{resp.status_code}]: {resp.text.strip()[:200]}"
                    )
                return
            except Exception as e:
                last_exc = e
                self.logger.warning(
                    f"⚠️ Не удалось инициировать звонок (попытка {attempt}/{call_max_attempts}): {e}"
                )
                if attempt < call_max_attempts:
                    time.sleep(call_retry_delay_sec)

        self.logger.error(f"🔴 Звонок не удался после {call_max_attempts} попыток: {last_exc}")

    @staticmethod
    def _best_bid_ask(book):
        """Best bid = максимум цены среди bids, best ask = минимум среди
        asks — считаем ЯВНО по значению (см. пояснение в шапке модуля)."""
        if not book:
            return None, None
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        best_bid = max((float(b["price"]) for b in bids), default=None)
        best_ask = min((float(a["price"]) for a in asks), default=None)
        return best_bid, best_ask

    # ------------------------------------------------------------------ #
    # Разовая живая цена бакета -- для PriceMonitor (см. ниже)
    # ------------------------------------------------------------------ #

    def fetch_live_price(self, icao, now_local, target_label):
        """
        Разовая (НЕ через слоты/дедуп/файл, в отличие от run_cycle) выборка
        РЕАЛЬНОЙ цены ask_yes (цена ПОКУПКИ Yes из настоящего ордербука, а не
        последняя цена сделки) бакета target_label события icao на текущую
        локальную дату now_local. Используется PriceMonitor, чтобы сигнал
        (и Telegram-сообщение, и сравнение с min/max_price_limit) опирался на
        РЕАЛЬНУЮ цену рынка, а не на цену из Gamma API
        (WeatherMiner.scrape_polymarket отдаёт только последнюю зафиксированную
        сделку/котировку -- на низколиквидных бакетах она может заметно
        отличаться от реальной цены покупки).

        target_label -- ТА ЖЕ метка бакета, что уже вернул Gamma-разбор
        ставок в PriceMonitor (bet_label из _match_bet_by_bucket/max(bets)) --
        Gamma по-прежнему используется, чтобы ОПРЕДЕЛИТЬ, какой бакет является
        таргетом (по прогнозу+поправке либо как самый дорогой), а этот метод
        только УТОЧНЯЕТ его цену через реальный стакан CLOB.

        Событие резолвится через тот же self._gamma_cache, что и run_cycle
        (кэш на (icao, дата) -- бакеты события не меняются в течение дня),
        так что при штатной работе PolymarketClobMiner.run_cycle() отдельного
        Gamma-запроса тут чаще всего не будет вовсе. Возвращает цену В
        ЦЕНТАХ (округлённую до 2 знаков) или None -- если бакет не нашёлся в
        событии Gamma, ask_yes пуст (нет предложений на продажу) или сам
        запрос не удался; вызывающая сторона (PriceMonitor) в этом случае
        сама решает, как поступить (см. _resolve_live_price).
        """
        date_str = now_local.strftime("%Y-%m-%d")
        cache_key = (icao, date_str)
        if cache_key not in self._gamma_cache:
            self._gamma_cache[cache_key] = self._fetch_event_markets(icao, now_local)
        markets = self._gamma_cache[cache_key]

        market = next((m for m in markets if m["label"] == target_label), None)
        if market is None:
            self.logger.warning(f"⚠️ [CLOB-API live] {icao}: бакет '{target_label}' не найден в событии Gamma")
            return None

        books = self._get_slot_books(icao, now_local, markets)
        _, ask_yes = self._best_bid_ask(books.get(market["yes_token"]))
        if ask_yes is None:
            self._record_gamma_fallback(icao, now_local, markets, [target_label])
            self.logger.warning(f"⚠️ [CLOB-API live] {icao}: нет ask_yes для бакета '{target_label}' (пустой стакан)")
            return None

        price_cents = round(ask_yes * 100, 2)
        self.logger.debug(f"📡 [CLOB-API live] {icao}: '{target_label}' ask_yes {price_cents}¢")
        return price_cents

    def _get_slot_books(self, icao, now_local, markets):
        """
        Ордербуки ВСЕХ токенов (Yes+No) события для живой проверки монитора.
        Один запрос на (город, дата, слот): повторный вызов в тот же слот (второй набор
        стратегий по тому же городу -- прогноз и max_bet) получает ТЕ ЖЕ книги, поэтому
        цена обоих сигналов совпадает с тем, что попадает в снапшот слота (и в отчёт /
        бэктест). При первом вызове книги сразу пишутся как снапшот слота, если майнер
        run_cycle его ещё не снял (см. _write_live_snapshot).
        """
        slot_label = self._current_slot_label(now_local)
        date_str = now_local.strftime("%Y-%m-%d")
        cache_key = (icao, date_str, slot_label)
        if slot_label is not None and cache_key in self._live_books_cache:
            return self._live_books_cache[cache_key]
        if slot_label is not None and cache_key in self._gamma_slots:
            return {}  # первый сигнал слота уже ушёл на цене Gamma -- второй должен быть на ней же

        all_tokens = []
        for m in markets:
            all_tokens.append(m["yes_token"])
            all_tokens.append(m["no_token"])
        books = self._fetch_order_books(list(dict.fromkeys(all_tokens)))

        if slot_label is not None and not books:
            self._record_gamma_fallback(icao, now_local, markets, None, whole_slot=True)
        if slot_label is not None and books:
            # чистим кэш прошлых дат, чтобы не рос бесконечно
            for k in [k for k in self._live_books_cache if k[1] != date_str]:
                del self._live_books_cache[k]
            self._live_books_cache[cache_key] = books
            self._write_live_snapshot(icao, now_local, markets, books)
        return books

    def fetch_live_top_bucket(self, icao, now_local):
        """
        Определяет бакет с САМОЙ ВЫСОКОЙ РЕАЛЬНОЙ ценой ask_yes среди ВСЕХ
        бакетов события icao на текущую локальную дату now_local --
        используется PriceMonitor для набора max_bet (см.
        check_city_snapshot_max_bet/_resolve_top_bucket), вместо выбора по
        последней цене сделки/котировке Gamma (там "самая дорогая ставка"
        может оказаться не той же самой, что и по-настоящему самая дорогая
        в живом стакане).

        ОДИН батч-запрос ко ВСЕМ yes_token события сразу (не по одному на
        бакет, как было бы при переиспользовании fetch_live_price для
        каждого бакета по очереди) — та же экономия сети, что у run_cycle().
        Событие резолвится через тот же self._gamma_cache (см.
        fetch_live_price) — при штатной работе run_cycle() отдельного
        Gamma-запроса тут чаще всего не будет вовсе.

        Возвращает (label, price_cents) бакета с максимальной ценой среди
        бакетов, у которых нашёлся хоть один ask (пустые бакеты просто
        пропускаются, а не считаются ценой 0 — иначе бакет без предложений
        мог бы "проиграть" реально дорогому, но результат не потеряет
        сигнал целиком), или None — если событие/бакеты Gamma не нашлись,
        либо ask_yes нет вообще ни у одного бакета (пустой рынок/сетевая
        ошибка); вызывающая сторона в этом случае откатывается на выбор по
        цене Gamma (см. PriceMonitor._resolve_top_bucket).
        """
        date_str = now_local.strftime("%Y-%m-%d")
        cache_key = (icao, date_str)
        if cache_key not in self._gamma_cache:
            self._gamma_cache[cache_key] = self._fetch_event_markets(icao, now_local)
        markets = self._gamma_cache[cache_key]
        if not markets:
            self.logger.warning(f"⚠️ [CLOB-API live] {icao}: событие Gamma не нашлось или без бакетов")
            return None

        books = self._get_slot_books(icao, now_local, markets)

        best_label, best_price = None, None
        for m in markets:
            _, ask_yes = self._best_bid_ask(books.get(m["yes_token"]))
            if ask_yes is None:
                continue
            price_cents = round(ask_yes * 100, 2)
            if best_price is None or price_cents > best_price:
                best_label, best_price = m["label"], price_cents

        if best_label is None:
            self._record_gamma_fallback(icao, now_local, markets, None)
            self.logger.warning(f"⚠️ [CLOB-API live] {icao}: ни у одного бакета события нет ask_yes")
            return None

        self.logger.debug(f"📡 [CLOB-API live] {icao}: самый дорогой бакет по CLOB — '{best_label}' за {best_price}¢")
        return best_label, best_price

    def _record_gamma_fallback(self, icao, now_local, markets, labels, whole_slot=False):
        """
        PriceMonitor откатывается на цену Gamma, когда живой CLOB недоступен (запрос упал,
        либо у бакета нет ask). Чтобы отчёт и бэктест видели ТУ ЖЕ цену, что и сигнал,
        записываем цену Gamma в журнал CLOB-снапшотов как ask_yes (bid'ов нет, поле
        "source": "gamma"). labels=None -- все бакеты события; whole_slot=True -- ещё и
        помечаем слот как "Gamma-слот": майнер run_cycle его уже не перепишет, а второй
        набор стратегий по этому городу получит тот же откат на Gamma (см. _get_slot_books).
        Цена Gamma берётся из того же кэша события, что и в PriceMonitor
        (fetch_gamma_bets), так что значения совпадают.
        """
        try:
            slot_label = self._current_slot_label(now_local)
            if slot_label is None:
                return
            date_str = now_local.strftime("%Y-%m-%d")
            slot_key = (icao, date_str, slot_label)
            wanted = None if labels is None else set(labels)
            records = []
            for m in markets:
                if wanted is not None and m["label"] not in wanted:
                    continue
                rec_key = slot_key + (m["label"],)
                price_cents = _price_cents(self._gamma_slot_prices.get(slot_key, {}).get(m["label"]) or m.get("price"))
                if price_cents is None or rec_key in self._gamma_recorded:
                    continue
                self._gamma_recorded.add(rec_key)
                records.append({
                    "date": date_str, "icao": icao, "city": self.cities[icao]["name"],
                    "snapshot_slot": slot_label, "snapshot_timestamp": now_local.isoformat(),
                    "bucket": m["label"],
                    "bid_yes": None, "ask_yes": round(price_cents / 100.0, 4),
                    "bid_no": None, "ask_no": None, "source": "gamma",
                })
            if whole_slot:
                self._gamma_slots.add(slot_key)
                self.written_keys.add((date_str, icao, slot_label))
            if records:
                with open(self._output_path(date_str), "a", encoding="utf-8") as f:
                    for r in records:
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")
                self.logger.warning(
                    f"⚠️ [CLOB-API live] {icao} | {slot_label}: CLOB недоступен -- в журнал записана цена Gamma "
                    f"({len(records)} бакетов), сигнал и отчёт используют её"
                )
        except Exception as e:
            self.logger.warning(f"⚠️ [CLOB-API live] {icao}: не удалось записать цену Gamma в журнал: {e}")

    def _write_live_snapshot(self, icao, now_local, markets, books):
        """
        Сохраняет ордербуки, по которым бот только что выбрал самый дорогой бакет, как
        обычный снапшот текущего слота (если он ещё не снят). Раньше решение бота и
        снапшот майнера (run_cycle, идёт в цикле ПОСЛЕ монитора, на 10-20 секунд позже)
        строились по разным запросам -- при почти равных верхних бакетах лидер успевал
        смениться, и бэктест (читает снапшот) выбирал не ту ставку, что реальный бот.
        Теперь run_cycle увидит ключ в written_keys и этот слот не перезапишет.

        Если цикл бота опоздал (первый проход часа пришёл в 12:15, а не в
        12:00-12:14), слот "HH:00" иначе остаётся пустым: бот торговал по живой
        цене, а отчёт/бэктест ищут именно "HH:00" и не находят цены (ячейка с
        прочерком). Поэтому тот же снимок решения пишется ещё и как "HH:00"
        этого часа, если он ещё не снят (и как текущий слот, например "12:15").
        """
        try:
            slot_label = self._current_slot_label(now_local)
            if slot_label is None:
                return
            date_str = now_local.strftime("%Y-%m-%d")
            labels = [slot_label]
            base_label = f"{now_local.hour:02d}:00"
            if base_label != slot_label and 0 in self.slot_minutes:
                labels.insert(0, base_label)
            for label in labels:
                if (date_str, icao, label) in self.written_keys:
                    continue
                self._write_snapshot(icao, date_str, label, now_local.isoformat(), markets, books)
        except Exception as e:
            self.logger.warning(f"⚠️ [CLOB-API live] {icao}: не удалось сохранить снапшот решения: {e}")

    # ------------------------------------------------------------------ #
    # Слоты
    # ------------------------------------------------------------------ #

    def _current_slot_label(self, now_local):
        """Ближайший ПРОШЕДШИЙ слот 15-минутной сетки в дневном окне
        [slot_start_hour, slot_end_hour) по местному времени города — или
        None вне окна. Округление вниз (а не точное совпадение минуты)
        означает, что слот не будет пропущен, даже если Orchestrator
        проверяет реже, чем раз в 15 минут — лишь бы чаще этого интервала;
        дедуп — через written_keys, так что один и тот же слот не пишется
        дважды, сколько бы раз ни попадал в это окно проверки."""
        if not (self.slot_start_hour <= now_local.hour < self.slot_end_hour):
            return None
        candidates = sorted(m for m in self.slot_minutes if m <= now_local.minute)
        if not candidates:
            return None
        slot_minute = candidates[-1]
        return now_local.replace(minute=slot_minute, second=0, microsecond=0).strftime("%H:%M")

    # ------------------------------------------------------------------ #
    # Снятие снапшота
    # ------------------------------------------------------------------ #

    def _write_snapshot(self, icao, date_str, slot_label, snapshot_ts, markets, books):
        key = (date_str, icao, slot_label)
        path = self._output_path(date_str)
        with open(path, "a", encoding="utf-8") as f:
            for m in markets:
                bid_yes, ask_yes = self._best_bid_ask(books.get(m["yes_token"]))
                bid_no, ask_no = self._best_bid_ask(books.get(m["no_token"]))
                record = {
                    "date": date_str, "icao": icao, "city": self.cities[icao]["name"],
                    "snapshot_slot": slot_label, "snapshot_timestamp": snapshot_ts,
                    "bucket": m["label"],
                    "bid_yes": bid_yes, "ask_yes": ask_yes,
                    "bid_no": bid_no, "ask_no": ask_no,
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.written_keys.add(key)
        self.logger.info(f"💾 [CLOB-API] {icao} | {slot_label} | {len(markets)} бакетов")

    # ------------------------------------------------------------------ #
    # Один проход и автономный цикл
    # ------------------------------------------------------------------ #

    def run_cycle(self):
        """
        Один проход по всем городам: собирает due-список (город, чей
        текущий 15-минутный слот ещё не снят), резолвит бакеты каждого через
        Gamma (с кэшем на сутки), и ОДНИМ батч-запросом к CLOB API забирает
        ордербуки ВСЕХ токенов ВСЕХ due-городов сразу. Не блокирует и не
        спит — вызывающая сторона (Orchestrator либо run()) сама решает,
        когда делать паузу между проходами.
        """
        due = []  # [(icao, now_local, slot_label, date_str), ...]
        for icao, info in self.cities.items():
            try:
                tz = pytz.timezone(info["tz"])
                now_local = datetime.now(tz)
                slot_label = self._current_slot_label(now_local)
                if slot_label is None:
                    continue
                date_str = now_local.strftime("%Y-%m-%d")
                key = (date_str, icao, slot_label)
                if key in self.written_keys:
                    continue
                due.append((icao, now_local, slot_label, date_str))
            except Exception as e:
                self.logger.error(f"⚠️ Ошибка определения слота {icao}: {e}")

        if not due:
            return

        self.logger.info(f"🔎 [CLOB-API] Слот наступил у {len(due)} городов")

        city_markets = {}
        for icao, now_local, slot_label, date_str in due:
            cache_key = (icao, date_str)
            if cache_key not in self._gamma_cache:
                self._gamma_cache[cache_key] = self._fetch_event_markets(icao, now_local)
            city_markets[icao] = self._gamma_cache[cache_key]

        # Собираем ВСЕ token_id (Yes+No) по ВСЕМ due-городам в ОДИН список —
        # это и есть объединение нескольких городов в один запрос.
        token_ids = []
        for icao, _, _, _ in due:
            for m in city_markets.get(icao, []):
                token_ids.append(m["yes_token"])
                token_ids.append(m["no_token"])
        token_ids = list(dict.fromkeys(token_ids))  # без дублей (пересечения кэша между городами маловероятны, но не помешает)

        if not token_ids:
            self.logger.info("ℹ️ [CLOB-API] Нет ни одного бакета ни у одного due-города — нечего запрашивать")
            return

        books = self._fetch_order_books(token_ids)
        self.logger.info(f"📊 [CLOB-API] Ордербуков получено: {len(books)} из {len(token_ids)} запрошенных токенов")

        for icao, now_local, slot_label, date_str in due:
            markets = city_markets.get(icao, [])
            if not markets:
                continue
            self._write_snapshot(icao, date_str, slot_label, now_local.isoformat(), markets, books)

    def run(self, cycle_sleep_sec=None):
        """Автономный запуск (standalone-использование ВНЕ Orchestrator):
        бесконечный цикл run_cycle() + сон."""
        cycle_sleep_sec = cycle_sleep_sec or self.cycle_sleep_sec
        self.logger.info("🚀 Майнер CLOB bid/ask запущен (автономный режим).")
        try:
            while True:
                self.run_cycle()
                time.sleep(cycle_sleep_sec)
        except KeyboardInterrupt:
            self.logger.info("⛔ Майнер CLOB bid/ask остановлен вручную.")
