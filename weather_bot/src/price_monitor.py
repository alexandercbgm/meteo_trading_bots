"""
price_monitor.py

Класс PriceMonitor — «на лету», во время майнинга слота
config.price_monitor.monitor_slot, проверяет ДВА НЕЗАВИСИМЫХ набора городов
и шлёт им отдельные наборы сигналов:

1. weather_forecast (config/watched_icaos_weather_forecast.yaml,
   check_city_snapshot) — таргет = прогноз погоды (WU/Windy) + поправка
   города в "ставках"/бакетах. Настройки звонка — config.price_monitor.weather_forecast.

2. max_bet (config/watched_icaos_max_bet.yaml, check_city_snapshot_max_bet) —
   без прогноза погоды вообще: таргет = бакет САМОЙ ДОРОГОЙ ставки Polymarket
   этого снапшота (тот же принцип, что у AccuracyReportBuilder.build_market_target_report).
   Настройки звонка — config.price_monitor.max_bet (звонки по умолчанию выключены).

В обоих случаях сигнал шлётся, если цена ставки на таргет попадает в
диапазон [min_price_limit, max_price_limit] ЭТОГО города (любая из границ
может быть не задана — тогда с этой стороны ограничения нет, см.
src/watched_icaos.py), плюс опционально звонок через CallMeBot (как в
poly_setup_screener.py), с повторными попытками при сетевых сбоях.

ЦЕНА, по которой принимается решение (и которая показывается в Telegram) —
РЕАЛЬНАЯ цена ask_yes из ордербука CLOB, а НЕ цена из Gamma API (последняя
зафиксированная сделка/котировка — может заметно отличаться от реальной
цены покупки на низколиквидных бакетах). Устройство разное у двух наборов:
- **weather_forecast** — таргет-бакет определяется прогнозом+поправкой
  (Gamma тут вообще ни при чём — бакет известен заранее), CLOB
  (fetch_live_price/clob_price_fetcher) только уточняет ЦЕНУ этого одного
  уже известного бакета (см. _resolve_live_price).
- **max_bet** — таргет-бакет и есть "самая дорогая ставка", поэтому сам
  выбор ТОЖЕ идёт по CLOB: сравниваются реальные цены ВСЕХ бакетов события
  сразу (fetch_live_top_bucket/clob_top_bucket_fetcher, см.
  _resolve_top_bucket), а не последние цены сделок Gamma.
В обоих случаях, если живой запрос CLOB недоступен (fetcher не задан,
событие/бакет/цена не нашлись, сетевая ошибка) — тихий откат на данные
Gamma (poly_bets_raw), сигнал из-за сбоя ВТОРОГО источника не пропадает.

Оба набора встраиваются в WeatherMiner.mine_city(): вызываются на слоте
config.price_monitor.monitor_slot, ровно ОДИН раз в день на город (за счёт
`slot_label == self.monitor_slot` на стороне вызывающего кода — отдельного
дедупа внутри монитора нет).

poly_bets_raw (список ставок Gamma — label+price) БОЛЬШЕ НЕ МАЙНИТСЯ
WeatherMiner'ом заранее: если он не передан явно (обычный случай, см.
mine_city), PriceMonitor запрашивает его сам, ЖИВЬЁМ, через
self.gamma_bets_fetcher (инжектируется Orchestrator'ом как
Orchestrator._fetch_gamma_bets_now -> PolymarketClobMiner.fetch_gamma_bets)
— тот же Gamma-запрос/кэш на (icao, дата), что чуть ниже по коду и так
используют clob_price_fetcher/clob_top_bucket_fetcher для получения
token_id под CLOB, так что реально Gamma запрашивается ОДИН раз за проверку
города, а не дважды (раньше отдельно майнился WeatherMiner'ом, и отдельно —
резолвился PolymarketClobMiner'ом под CLOB).

Ведёт СВОЙ отдельный лог-файл (price_monitor.log, см. Orchestrator) — в него
попадает КАЖДАЯ проверка watched-города на слоте мониторинга (по обоим
наборам), включая случаи, когда сигнал не отправлен просто потому что цена
ставки вне диапазона.
"""

import os
import time
from datetime import datetime, timedelta

import requests

from .accuracy_report import (
    parse_bucket_range,
    parse_price_cents,
    parse_windy_value,
    parse_wu_value,
    polymarket_today,
    to_unit,
)
from .trader import _atomic_write_json, _load_json

WINDY_UNIT = "F"  # см. то же допущение в weather_miner.py/accuracy_report.py


class PriceMonitor:
    def __init__(
        self, config, cities, telegram, logger, watched=None, watched_max_bet=None, trading_engine=None,
        clob_price_fetcher=None, clob_top_bucket_fetcher=None, gamma_bets_fetcher=None,
    ):
        self.config = config
        self.cities = cities
        self.telegram = telegram
        self.logger = logger
        self.watched = watched or {}  # {icao: {...}} — набор 1, см. src/watched_icaos.py
        self.watched_max_bet = watched_max_bet or {}  # {icao: {...}} — набор 2
        # TradingEngine (src/trader.py) — опционально: если задан и
        # price_monitor.<набор>.auto_trading: true, сигнал ниже не только
        # шлётся в Telegram, но и открывает реальную позицию (on_signal).
        self.trading_engine = trading_engine
        # Живая цена CLOB (см. _resolve_live_price) — опционально: callable
        # (icao, now_local, target_label) -> цена в центах | None, инжектируется
        # Orchestrator'ом как Orchestrator._fetch_live_clob_price_now (та же
        # отложенная привязка, что у actuals_fetcher в TradingEngine). None —
        # PriceMonitor остаётся на цене Gamma, как раньше (например, в тестах,
        # где живого CLOB нет и не нужен).
        self.clob_price_fetcher = clob_price_fetcher
        # Живой топ-бакет CLOB (см. _resolve_top_bucket) -- опционально:
        # callable (icao, now_local) -> (label, price_cents) | None, для
        # набора max_bet -- определяет "самую дорогую ставку" ПО РЕАЛЬНЫМ
        # ценам (все бакеты события сразу), а не по последней цене сделки
        # Gamma. Инжектируется как Orchestrator._fetch_live_clob_top_bucket_now.
        self.clob_top_bucket_fetcher = clob_top_bucket_fetcher
        # Живой список ставок Gamma (label+price) -- опционально: callable
        # (icao, now_local) -> [{"label","price"}, ...], инжектируется
        # Orchestrator'ом как Orchestrator._fetch_gamma_bets_now (та же
        # отложенная привязка, что у clob_price_fetcher/clob_top_bucket_fetcher
        # выше). Используется, только если poly_bets_raw НЕ передан явно
        # вызывающим кодом (см. _fetch_gamma_bets/check_city_snapshot*) --
        # заменяет собой прежний отдельный Gamma-майнинг WeatherMiner'а.
        self.gamma_bets_fetcher = gamma_bets_fetcher
        # Дедуп "уже проверяли этот город сегодня" -- {(icao, date_str, набор)}.
        # РАНЬШЕ это получалось само собой через WeatherMiner.written_keys:
        # Gamma майнилась максимум раз в сутки, и на повторных проходах
        # run_cycle() ЧЕРЕЗ ТОТ ЖЕ ЧАС monitor_slot (WeatherMiner.run_cycle
        # вызывает mine_city КАЖДЫЙ цикл, пока час совпадает со слотом,
        # т.е. МНОГОКРАТНО за один час) poly_data была пустой и
        # check_city_snapshot*/check_city_snapshot_max_bet вообще не
        # вызывались (см. старый "and poly_data" в mine_city). После того,
        # как Gamma стала запрашиваться ЖИВЬЁМ при каждом вызове (см.
        # gamma_bets_fetcher выше), этот побочный дедуп пропал -- без
        # замены сигнал проверялся бы (и мог бы слаться/открывать позицию)
        # НА КАЖДОМ проходе цикла в течение всего часа monitor_slot, отсюда
        # задвоенные позиции. Теперь дедуп сделан явно, здесь.
        #
        # Персистентность: раньше это множество жило только в памяти процесса
        # и обнулялось при каждом перезапуске оркестратора -- после рестарта
        # города, уже проверенные и просигналившие СЕГОДНЯ, проверялись
        # заново, и в Telegram уходило повторное сообщение о сигнале (хотя
        # реальную позицию отдельная защита в TradingEngine.on_signal уже не
        # задваивала, см. _find_open_position). Поэтому множество теперь
        # зеркалится на диск (см. _load_checked_today/_mark_checked ниже) --
        # файл checked_today.json в той же папке, что open_positions.json у
        # TradingEngine, читается при старте и переживает перезапуск.
        trading_dir = getattr(config.paths, "trading_dir", os.path.join(config.paths.data_dir, "trading"))
        os.makedirs(trading_dir, exist_ok=True)
        self._checked_today_path = os.path.join(trading_dir, "checked_today.json")
        self._checked_today = self._load_checked_today()

        mon = config.price_monitor
        self.monitor_slot = mon.monitor_slot  # общий слот для обоих наборов
        # Рынок "уже решён": самый дорогой бакет по Gamma стоит >= этого порога (в центах) -- исход
        # дня фактически известен (например, температура уже достигла верхнего бакета). Ставки на
        # любой ДРУГОЙ бакет в такой день заведомо проигрышные и стоят ~0.1¢ (на них ещё и пол
        # min_order_usd раздувает лот до сотен акций), поэтому сигнал/позиция не открываются.
        # 0/null -- проверка выключена.
        self.decided_price_cents = float(getattr(mon, "decided_market_price_cents", 95) or 0)
        # Максимальная задержка сигнала (минут) от начала слота monitor_slot: проверка на проходе
        # цикла позже этого срока (бот перезапущен/сеть упала, первый успешный проход в 12:48)
        # НЕ открывает позицию -- цена к тому времени уже не цена слота. 0/null -- без ограничения
        # (окно весь час monitor_slot, как раньше).
        self.max_signal_delay_min = float(getattr(mon, "max_signal_delay_min", 0) or 0)
        self.bucket_width = {"F": config.accuracy_report.bucket_width.F, "C": config.accuracy_report.bucket_width.C}

        wf = mon.weather_forecast
        self.call_alert_enabled = wf.call_alert_enabled
        self.call_user = getattr(wf, "call_user", None)
        self.call_max_attempts = getattr(wf, "call_max_attempts", 3)
        self.call_retry_delay_sec = getattr(wf, "call_retry_delay_sec", 3)
        self.auto_trading = getattr(wf, "auto_trading", False)
        self.strategies_list = list(getattr(wf, "strategies_list", []) or [])

        mb = mon.max_bet
        self.call_alert_enabled_max_bet = mb.call_alert_enabled
        self.call_user_max_bet = getattr(mb, "call_user", None)
        self.call_max_attempts_max_bet = getattr(mb, "call_max_attempts", 3)
        self.call_retry_delay_sec_max_bet = getattr(mb, "call_retry_delay_sec", 3)
        self.auto_trading_max_bet = getattr(mb, "auto_trading", False)
        self.strategies_list_max_bet = list(getattr(mb, "strategies_list", []) or [])

    # ------------------------------------------------------------------ #
    # Персистентность self._checked_today (см. комментарий в __init__)
    # ------------------------------------------------------------------ #

    def _load_checked_today(self):
        """
        Читает checked_today.json (если есть) и разворачивает его обратно в
        set из кортежей (icao, date_str, набор) -- ровно то же представление,
        что использует остальной код класса. Файл хранит список списков
        (JSON не умеет в кортежи/множества), поэтому здесь конвертация в
        обе стороны: список <-> set из tuple.
        """
        raw = _load_json(self._checked_today_path, [])
        result = set()
        for item in raw:
            try:
                icao, date_str, group = item
            except (ValueError, TypeError):
                continue
            result.add((icao, date_str, group))
        return result

    def _save_checked_today(self):
        """
        Пишет self._checked_today на диск атомарно (см. _atomic_write_json).
        Заодно подчищает записи старше 3 суток (по date_str, простое
        строковое сравнение -- ISO-даты сравниваются как строки корректно)
        -- иначе файл рос бы бесконечно, а старые ключи уже никому не нужны
        (дедуп имеет смысл только "сегодня", везде date_str берётся из
        now_local в момент проверки).
        """
        cutoff = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
        self._checked_today = {item for item in self._checked_today if item[1] >= cutoff}
        _atomic_write_json(self._checked_today_path, [list(item) for item in self._checked_today])

    def _mark_checked(self, dedup_key):
        """Добавляет dedup_key в self._checked_today И сразу сохраняет на диск (переживает рестарт)."""
        self._checked_today.add(dedup_key)
        self._save_checked_today()

    def _signal_delay_min(self, now_local):
        """Сколько минут прошло от начала слота monitor_slot до now_local (None -- не считаем)."""
        if now_local is None:
            return None
        try:
            slot_h, slot_m = (int(x) for x in str(self.monitor_slot).split(":")[:2])
        except Exception:
            return None
        return (now_local.hour * 60 + now_local.minute) - (slot_h * 60 + slot_m)  # с точностью до минуты: 12:15:48 при лимите 15 ещё проходит

    def _skip_if_too_late(self, dedup_key, tag, info, now_local, group_label):
        """
        True -- проверка на этом проходе опоздала дальше max_signal_delay_min от начала слота: город
        отмечается проверенным (чтобы не повторять), в лог и Telegram уходит одно уведомление, позиция и
        сигнал не создаются. False -- можно продолжать.
        """
        if not self.max_signal_delay_min:
            return False
        delay = self._signal_delay_min(now_local)
        if delay is None or delay <= self.max_signal_delay_min + 1e-9:
            return False
        if dedup_key is not None:
            self._mark_checked(dedup_key)
        self.logger.warning(
            f"⏰ {group_label} {tag}: проверка в {now_local:%H:%M}, позже слота {self.monitor_slot} на {delay:.0f} мин "
            f"(лимит {self.max_signal_delay_min:g} мин, price_monitor.max_signal_delay_min) -- сигнал пропущен"
        )
        city_tag = f"#{info['name'].replace(' ', '_').replace('-', '_')}"
        try:
            self.telegram.send_message(
                f"⏰ <b>Сигнал пропущен — слишком поздно</b> {city_tag}\n"
                f"Проверка в {now_local:%H:%M}, слот {self.monitor_slot} "
                f"(допустимая задержка {self.max_signal_delay_min:g} мин). Позиция не открывалась."
            )
        except Exception as e:
            self.logger.error(f"⚠️ {tag}: не удалось отправить уведомление о пропуске сигнала: {e}")
        return True

    # ------------------------------------------------------------------ #
    # Разбор ставок/прогноза (те же форматы, что в accuracy_report.py)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _determine_scale(poly_bets_raw):
        """Шкала рынка (F/C) по символу в лейбле первой распознанной ставки."""
        for b in poly_bets_raw:
            label = b.get("label", "")
            if "°F" in label:
                return "F"
            if "°C" in label:
                return "C"
        return None

    def _bucket_index(self, value, scale):
        return round(value) // self.bucket_width[scale]

    def _parse_bets(self, poly_bets_raw, scale):
        """
        [(low_bucket_or_None, high_bucket_or_None, price, label), ...] — ставки
        в единицах НОМЕРА БАКЕТА, диапазоном (не одним "якорным" числом): у
        открытых ставок ("104°F or higher") нет единого номера бакета, под
        который попадали бы ВСЕ значения таргета внутри диапазона — см. то же
        решение в AccuracyReportBuilder._build_bucket_price_map.
        """
        bets = []
        for b in poly_bets_raw:
            low, high = parse_bucket_range(b.get("label", ""))
            if low is None:
                continue
            price = parse_price_cents(b.get("price", ""))
            if price is None:
                continue
            low_bucket = None if low == float("-inf") else self._bucket_index(low, scale)
            high_bucket = None if high == float("inf") else self._bucket_index(high, scale)
            bets.append((low_bucket, high_bucket, price, b.get("label", "")))
        return bets

    def _market_decided_elsewhere(self, bets, bet_label, tag, group_label):
        """
        True -- рынок уже решён в пользу ДРУГОГО бакета: самый дорогой бакет по Gamma (свежая цена
        на момент проверки, см. PolymarketClobMiner.fetch_gamma_bets) стоит >= decided_price_cents,
        и это не тот бакет, на который собирается ставить бот (bet_label). Тогда сигнал не
        отправляется и позиция не открывается. Если решённый бакет и есть ставка бота -- это не
        наш случай (дальше сработают обычные пороги min/max_price_limit).
        """
        if not self.decided_price_cents or not bets:
            return False
        top = max(bets, key=lambda b: b[2])
        if top[2] < self.decided_price_cents:
            return False
        if parse_bucket_range(top[3]) == parse_bucket_range(bet_label or ""):
            return False
        self.logger.info(
            f"ℹ️ {group_label} {tag}: рынок уже решён -- '{top[3]}' стоит {top[2]}¢ (>= {self.decided_price_cents:g}¢), "
            f"ставка бота '{bet_label}' не он -- без сигнала и без позиции"
        )
        return True

    @staticmethod
    def _match_bet_by_bucket(bets, target_bucket):
        for low_bucket, high_bucket, price, label in bets:
            lo_ok = (low_bucket is None) or (target_bucket >= low_bucket)
            hi_ok = (high_bucket is None) or (target_bucket <= high_bucket)
            if lo_ok and hi_ok:
                return price, label
        return None, None

    @staticmethod
    def _forecast_max(forecast_raw, scale, is_windy):
        """
        Максимум прогноза (в шкале рынка scale) по сырым точкам forecast_raw
        одного источника. Для Wunderground КАЖДАЯ точка несёт СВОЮ единицу
        (temp_text вида "94 °F" -> parse_wu_value возвращает (94, "F")) — она и
        конвертируется в scale. Для Windy единица не подписана на сайте и
        считается WINDY_UNIT (см. допущение в шапке модуля).
        """
        values = []
        for p in forecast_raw or []:
            if is_windy:
                v = parse_windy_value(p.get("temp_text"))
                if v is not None:
                    values.append(to_unit(v, WINDY_UNIT, scale))
            else:
                v, u = parse_wu_value(p.get("temp_text"))
                if v is not None:
                    values.append(to_unit(v, u, scale))
        return max(values) if values else None

    @staticmethod
    def _corr_suffix(correction):
        if correction > 0:
            return f" +{correction}"
        if correction < 0:
            return f" {correction}"
        return ""

    def _resolve_source(self, watch_cfg, wu_forecast_raw, windy_forecast_raw):
        if watch_cfg["weather_source"] == "windy":
            return "Windy", windy_forecast_raw, True
        return "Wunderground", wu_forecast_raw, False

    def _resolve_live_price(self, icao, now_local, target_label, gamma_price, tag, group_label):
        """
        Уточняет gamma_price (последняя сделка/котировка Gamma) РЕАЛЬНОЙ
        ценой ask_yes из ордербука CLOB через self.clob_price_fetcher — см.
        пояснение в шапке модуля. Тихо откатывается на gamma_price (второй
        источник цены не должен гасить сигнал целиком), если: fetcher не
        задан, now_local не передан, бакет/цена не нашлись в CLOB, либо сам
        запрос упал с исключением (сеть) — во всех этих случаях пишет
        причину отката в лог, чтобы было видно, как часто CLOB реально
        недоступен.
        """
        if self.clob_price_fetcher is None or now_local is None:
            return gamma_price
        try:
            live_price = self.clob_price_fetcher(icao, now_local, target_label)
        except Exception as e:
            self.logger.warning(f"⚠️ {group_label} {tag}: живой запрос цены CLOB упал ({e}) — использую цену Gamma")
            return gamma_price
        if live_price is None:
            self.logger.debug(
                f"⏭️ {group_label} {tag}: живая цена CLOB недоступна для '{target_label}' — использую цену Gamma"
            )
            return gamma_price
        if live_price != gamma_price:
            self.logger.debug(
                f"📡 {group_label} {tag}: цена уточнена CLOB — было {gamma_price}¢ (Gamma), стало {live_price}¢ (реальная)"
            )
        return live_price

    def _fetch_gamma_bets(self, icao, now_local, tag):
        """
        Живой запрос списка ставок Gamma (label+price) через
        self.gamma_bets_fetcher — см. пояснение в шапке модуля и в __init__.
        Заменяет собой прежний отдельный Gamma-майнинг WeatherMiner'а: раньше
        сюда всегда приходил уже готовый poly_bets_raw, теперь — только если
        вызывающий код (обычно тесты/ручные вызовы) передал его явно;
        штатный путь из WeatherMiner.mine_city ничего не передаёт, и вот
        здесь список запрашивается на месте. now_local обязателен — без него
        не определить локальную дату события. Сетевая ошибка/пустой ответ —
        не фатальны: пустой список просто гасит сигнал этой проверки (как и
        раньше гасил пустой/непереданный poly_bets_raw), причина — в логе.
        """
        if self.gamma_bets_fetcher is None or now_local is None:
            self.logger.debug(f"⏭️ {tag}: gamma_bets_fetcher не задан или нет now_local — данных Gamma нет")
            return []
        try:
            return self.gamma_bets_fetcher(icao, now_local) or []
        except Exception as e:
            self.logger.warning(f"⚠️ {tag}: живой запрос ставок Gamma упал ({e})")
            return []

    def _resolve_top_bucket(self, icao, now_local, bets, tag):
        """
        Определяет (label, price_cents) САМОГО ДОРОГОГО бакета для набора
        max_bet — ПО РЕАЛЬНЫМ ценам CLOB (self.clob_top_bucket_fetcher, ВСЕ
        бакеты события сразу, см. PolymarketClobMiner.fetch_live_top_bucket),
        если фетчер задан, now_local передан и живой запрос что-то нашёл.
        Иначе (fetcher не задан, now_local не передан, сеть упала, событие/
        бакеты не нашлись) — откат на старый способ: самая дорогая ставка ПО
        ЦЕНЕ GAMMA среди уже распарсенных bets (см. _parse_bets) — второй
        источник цены не должен гасить сигнал целиком.
        """
        if self.clob_top_bucket_fetcher is not None and now_local is not None:
            try:
                top = self.clob_top_bucket_fetcher(icao, now_local)
            except Exception as e:
                self.logger.warning(
                    f"⚠️ [max ставка] {tag}: живой запрос топ-бакета CLOB упал ({e}) — использую цену Gamma"
                )
                top = None
            if top is not None:
                label, price = top
                self.logger.debug(f"📡 [max ставка] {tag}: самый дорогой бакет по CLOB — '{label}' за {price}¢")
                return label, price
            self.logger.debug(f"⏭️ [max ставка] {tag}: живой топ-бакет CLOB недоступен — использую цену Gamma")

        _, _, price, label = max(bets, key=lambda b: b[2])
        return label, price

    # ------------------------------------------------------------------ #
    # Набор 1: таргет = прогноз погоды + поправка города
    # ------------------------------------------------------------------ #

    def check_city_snapshot(
        self, icao, info, date_str, wu_forecast_raw, windy_forecast_raw, poly_bets_raw=None, now_local=None,
    ):
        """
        icao должен быть в self.watched (config/watched_icaos_weather_forecast.yaml).
        poly_bets_raw теперь ОПЦИОНАЛЕН: штатный вызов из WeatherMiner.mine_city
        его не передаёт вовсе (Gamma больше не майнится заранее) — тогда он
        запрашивается здесь же, живьём, через self.gamma_bets_fetcher (см.
        _fetch_gamma_bets и пояснение в шапке модуля); передать его явно
        по-прежнему можно (тесты/ручные вызовы) — тогда живой запрос
        пропускается. Сигнал шлётся, если цена ставки на таргет попадает в
        диапазон [min_price_limit, max_price_limit] — ЛЮБАЯ из границ может
        быть не задана (None), тогда с этой стороны ограничения нет (см.
        src/watched_icaos.py). now_local — локальное время города
        (передаётся WeatherMiner.mine_city) — нужно и для живой цены CLOB
        (см. _resolve_live_price), и для живого запроса Gamma выше.
        """
        watch_cfg = self.watched.get(icao)
        if watch_cfg is None:
            return

        # Дедуп "один раз в сутки на город" -- см. self._checked_today в
        # __init__. WeatherMiner.run_cycle зовёт mine_city МНОГОКРАТНО, пока
        # текущий час совпадает со слотом monitor_slot -- без этой проверки
        # сигнал (и, при auto_trading, реальная сделка) уходил бы на КАЖДОМ
        # таком проходе. Ранний выход -- дешёвая проверка ДО живого запроса
        # Gamma, чтобы повторный проход не тратил сетевые запросы впустую.
        dedup_key = (icao, date_str, "weather_forecast")
        if dedup_key in self._checked_today:
            return

        # Название города перед icao в каждом лог-сообщении ниже — эти строки
        # пишутся в price_monitor.log отдельным потоком от weather_miner.log,
        # без соседней "🔎 CityName — слот ..." строки над собой (см.
        # WeatherMiner.run_cycle), так что без имени города их не разобрать,
        # не сопоставляя icao в голове/по cities.py.
        tag = f"{info['name']} {icao}"

        if self._skip_if_too_late(dedup_key, tag, info, now_local, "[прогноз]"):
            return

        if poly_bets_raw is None:
            poly_bets_raw = self._fetch_gamma_bets(icao, now_local, tag)
        if not poly_bets_raw:
            # Данных не получили (сеть/пустое событие) -- НЕ отмечаем как
            # проверенное: тот же принцип, что раньше давал написанный в
            # written_keys дедуп Gamma-майнинга -- при сбое следующий проход
            # в течение того же часа monitor_slot должен получить шанс
            # попробовать снова, а не молчать весь день.
            return
        self._mark_checked(dedup_key)

        # Стратегия города не входит в список торгуемых для этого набора —
        # город временно "выключен" целиком: ни сигнала в Telegram, ни (в
        # будущем) авто-сделки, независимо от auto_trading — см. пояснение
        # пользователя: "если бы strategies_list остался ['flat'], а все
        # города были pyramid — при auto_trading=false сигналы слать не
        # нужно, при true — не нужно торговать ботом".
        if watch_cfg["strategy"] not in self.strategies_list:
            self.logger.debug(
                f"⏭️ [прогноз] {tag}: стратегия '{watch_cfg['strategy']}' не входит в "
                f"price_monitor.weather_forecast.strategies_list {self.strategies_list} — пропуск"
            )
            return

        min_price = watch_cfg["min_price_limit"]
        max_price = watch_cfg["max_price_limit"]

        scale = self._determine_scale(poly_bets_raw)
        if scale is None:
            self.logger.debug(f"⏭️ [прогноз] {tag}: не удалось определить шкалу рынка по ставкам")
            return

        bets = self._parse_bets(poly_bets_raw, scale)
        if not bets:
            self.logger.debug(f"⏭️ [прогноз] {tag}: не удалось распарсить ни одной ставки")
            return

        source_label, forecast_raw, is_windy = self._resolve_source(watch_cfg, wu_forecast_raw, windy_forecast_raw)
        forecast_max = self._forecast_max(forecast_raw, scale, is_windy)
        if forecast_max is None:
            self.logger.info(f"ℹ️ [прогноз] {tag}: нет прогноза {source_label} для проверки на слоте {self.monitor_slot}")
            return

        correction = watch_cfg["weather_corr"]
        corr_suffix = self._corr_suffix(correction)
        forecast_bucket = self._bucket_index(forecast_max, scale)
        target_bucket = forecast_bucket + correction
        price, bet_label = self._match_bet_by_bucket(bets, target_bucket)

        if price is None:
            self.logger.info(
                f"ℹ️ [прогноз] {tag}: {source_label} таргет (прогноз{corr_suffix}) — нет подходящего бакета ставки, без сигнала"
            )
            return

        if self._market_decided_elsewhere(bets, bet_label, tag, "[прогноз]"):
            return

        price = self._resolve_live_price(icao, now_local, bet_label, price, tag, "[прогноз]")

        if min_price is not None and price < min_price:
            self.logger.info(
                f"ℹ️ [прогноз] {tag}: {source_label} таргет {bet_label} (прогноз{corr_suffix}) за {price}¢ "
                f"— дешевле нижнего порога {min_price}¢, без сигнала"
            )
            return

        if max_price is not None and price > max_price:
            self.logger.info(
                f"ℹ️ [прогноз] {tag}: {source_label} таргет {bet_label} (прогноз{corr_suffix}) за {price}¢ "
                f"— выше порога {max_price}¢, без сигнала"
            )
            return

        self._send_signal(
            icao, info, watch_cfg, date_str, source_label, correction, price, bet_label,
            target_bucket=target_bucket, scale=scale,
        )

    def _send_signal(
        self, icao, info, watch_cfg, date_str, source_label, correction, price, bet_label,
        target_bucket=None, scale=None,
    ):
        poly_url, _poly_label = polymarket_today(icao, self.cities)

        if source_label == "Windy":
            windy_url = info.get("windy_url")
            source_link = f"<a href='{windy_url}'>Windy</a>" if windy_url else "Windy"
        else:
            wu_url = info.get("weather_url")
            has_wu = bool(wu_url) and "wunderground.com" in wu_url
            wu_date_url = f"{wu_url}/date/{date_str}" if has_wu else None
            source_link = f"<a href='{wu_date_url}'>Wunderground</a>" if wu_date_url else "Wunderground"

        poly_link = f"<a href='{poly_url}'>Polymarket</a>" if poly_url else "Polymarket"
        corr_suffix = self._corr_suffix(correction)
        city_tag = f"#{info['name'].replace(' ', '_').replace('-', '_')}"

        msg = (
            f"🎯<b>На прогноз{corr_suffix}</b> {city_tag}\n"
            f"Ставка <b>{bet_label}</b>, цена <b>{price}¢</b>.\n"
            f"{source_link} <b>{self.monitor_slot}</b> | {poly_link}"
        )
        comment = watch_cfg.get("comment")
        if comment:
            msg += f"\n{comment}"

        self.logger.info(
            f"🎯 [прогноз] {info['name']} {icao}: СИГНАЛ — {source_label} таргет {bet_label} (прогноз{corr_suffix}) за {price}¢ "
            f"(диапазон {watch_cfg['min_price_limit']}¢...{watch_cfg['max_price_limit']}¢)"
        )
        self.telegram.send_message(msg)
        if self.call_alert_enabled:
            self._telegram_call_alert(
                f"Setup {info['name']}", self.call_user, self.call_max_attempts, self.call_retry_delay_sec,
            )

        if self.trading_engine and self.auto_trading and target_bucket is not None and scale is not None:
            self.trading_engine.on_signal("weather_forecast", icao, info, watch_cfg, target_bucket, scale, signal_price_cents=price)

    # ------------------------------------------------------------------ #
    # Набор 2: таргет = бакет самой дорогой ставки (без прогноза погоды)
    # ------------------------------------------------------------------ #

    def check_city_snapshot_max_bet(self, icao, info, poly_bets_raw=None, now_local=None):
        """
        Аналог check_city_snapshot, но БЕЗ прогноза погоды вообще: таргет =
        бакет самой дорогой ставки Polymarket этого снапшота (та же логика,
        что AccuracyReportBuilder.build_market_target_dataframe). icao должен
        быть в self.watched_max_bet (config/watched_icaos_max_bet.yaml).
        poly_bets_raw опционален — см. check_city_snapshot: если не передан,
        запрашивается живьём через self.gamma_bets_fetcher. now_local — см.
        check_city_snapshot (нужен и для живого CLOB, и для живого запроса
        Gamma). "Самая дорогая ставка" определяется ПО РЕАЛЬНЫМ ценам CLOB
        (все бакеты события сразу, см. _resolve_top_bucket) — цена Gamma в
        bets тут используется только как запасной вариант, если живой запрос
        CLOB недоступен.
        """
        watch_cfg = self.watched_max_bet.get(icao)
        if watch_cfg is None:
            return

        # Дедуп "один раз в сутки на город" -- см. check_city_snapshot и
        # self._checked_today в __init__. Дата берётся из now_local (этот
        # набор не получает date_str параметром); без now_local (только
        # ручные/тестовые вызовы) дедуп пропускается -- штатный путь из
        # WeatherMiner.mine_city всегда передаёт now_local.
        dedup_key = (icao, now_local.strftime("%Y-%m-%d"), "max_bet") if now_local is not None else None
        if dedup_key is not None and dedup_key in self._checked_today:
            return

        tag = f"{info['name']} {icao}"

        if self._skip_if_too_late(dedup_key, tag, info, now_local, "[max ставка]"):
            return

        if poly_bets_raw is None:
            poly_bets_raw = self._fetch_gamma_bets(icao, now_local, tag)
        if not poly_bets_raw:
            # Сбой получения данных -- НЕ отмечаем как проверенное, см.
            # тот же комментарий в check_city_snapshot.
            return
        if dedup_key is not None:
            self._mark_checked(dedup_key)

        if watch_cfg["strategy"] not in self.strategies_list_max_bet:
            self.logger.debug(
                f"⏭️ [max ставка] {tag}: стратегия '{watch_cfg['strategy']}' не входит в "
                f"price_monitor.max_bet.strategies_list {self.strategies_list_max_bet} — пропуск"
            )
            return

        min_price = watch_cfg["min_price_limit"]
        max_price = watch_cfg["max_price_limit"]

        scale = self._determine_scale(poly_bets_raw)
        if scale is None:
            self.logger.debug(f"⏭️ [max ставка] {tag}: не удалось определить шкалу рынка по ставкам")
            return

        bets = self._parse_bets(poly_bets_raw, scale)
        if not bets:
            self.logger.debug(f"⏭️ [max ставка] {tag}: не удалось распарсить ни одной ставки")
            return

        # Таргет — САМАЯ ДОРОГАЯ ставка, теперь по РЕАЛЬНЫМ ценам CLOB (все
        # бакеты события сразу — не поштучное уточнение, как у
        # check_city_snapshot: там таргет уже известен из прогноза, здесь
        # сам выбор таргета и есть "самая дорогая", так что сравнивать нужно
        # реальные цены ВСЕХ бакетов между собой, см. _resolve_top_bucket).
        bet_label, price = self._resolve_top_bucket(icao, now_local, bets, tag)

        if self._market_decided_elsewhere(bets, bet_label, tag, "[max ставка]"):
            return

        # Таргет-бакет — та же "якорная" логика, что и везде в проекте (см.
        # AccuracyReportBuilder.build_market_target_dataframe): нижняя
        # граница диапазона выбранного бакета, если она есть, иначе верхняя
        # (для открытых снизу "or lower"/"or below" диапазонов). Границы
        # берём через parse_bucket_range по label — bet_label мог прийти как
        # от Gamma (bets), так и от CLOB (свой независимый список бакетов
        # события), поэтому не полагаемся на то, что он совпадёт с одной из
        # записей bets дословно.
        low, high = parse_bucket_range(bet_label)
        low_bucket = None if low == float("-inf") else self._bucket_index(low, scale)
        high_bucket = None if high == float("inf") else self._bucket_index(high, scale)
        target_bucket = low_bucket if low_bucket is not None else high_bucket

        if min_price is not None and price < min_price:
            self.logger.info(
                f"ℹ️ [max ставка] {tag}: самая дорогая ставка {bet_label} за {price}¢ "
                f"— дешевле нижнего порога {min_price}¢, без сигнала"
            )
            return

        if max_price is not None and price > max_price:
            self.logger.info(
                f"ℹ️ [max ставка] {tag}: самая дорогая ставка {bet_label} за {price}¢ "
                f"— выше порога {max_price}¢, без сигнала"
            )
            return

        self._send_signal_max_bet(icao, info, watch_cfg, price, bet_label, target_bucket=target_bucket, scale=scale)

    def _send_signal_max_bet(self, icao, info, watch_cfg, price, bet_label, target_bucket=None, scale=None):
        poly_url, _poly_label = polymarket_today(icao, self.cities)
        poly_link = f"<a href='{poly_url}'>Polymarket</a>" if poly_url else "Polymarket"
        city_tag = f"#{info['name'].replace(' ', '_').replace('-', '_')}"

        # Ссылки на Wunderground тут нет — прогноза погоды нет вовсе.
        msg = (
            f"🎯<b>На самую дорогую ставку</b> {city_tag}\n"
            f"Ставка <b>{bet_label}</b>, цена <b>{price}¢</b>.\n"
            f"{poly_link} <b>{self.monitor_slot}</b>"
        )
        comment = watch_cfg.get("comment")
        if comment:
            msg += f"\n{comment}"

        self.logger.info(
            f"🎯 [max ставка] {info['name']} {icao}: СИГНАЛ — {bet_label} за {price}¢ "
            f"(диапазон {watch_cfg['min_price_limit']}¢...{watch_cfg['max_price_limit']}¢)"
        )
        self.telegram.send_message(msg)
        if self.call_alert_enabled_max_bet:
            self._telegram_call_alert(
                f"Setup {info['name']}",
                self.call_user_max_bet, self.call_max_attempts_max_bet, self.call_retry_delay_sec_max_bet,
            )

        if self.trading_engine and self.auto_trading_max_bet and target_bucket is not None and scale is not None:
            self.trading_engine.on_signal("max_bet", icao, info, watch_cfg, target_bucket, scale, signal_price_cents=price)

    # ------------------------------------------------------------------ #
    # Звонок (общий для обоих наборов, с СВОИМИ настройками на вызов)
    # ------------------------------------------------------------------ #

    def _telegram_call_alert(self, message, call_user, call_max_attempts, call_retry_delay_sec):
        """
        Как в poly_setup_screener.py: звонок через CallMeBot. При сетевых
        сбоях (например, "Read timed out" — CallMeBot иногда не отвечает
        вовремя) повторяет до call_max_attempts раз с паузой
        call_retry_delay_sec между попытками, прежде чем сдаться. Настройки
        передаются параметрами — у каждого из двух наборов сигналов свои
        (см. config.price_monitor.weather_forecast/max_bet).
        """
        if not call_user:
            self.logger.warning("⚠️ call_alert_enabled=true, но call_user не задан в config.yaml")
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
