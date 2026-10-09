"""
orchestrator.py

Класс Orchestrator — связывает все компоненты проекта в одну оркестрацию
(пункты 1, 2, 3, 4, 5, 6 ТЗ):

1. Непрерывно гоняет майнер исторических данных (WeatherMiner.run_cycle()) —
   один проход по всем городам за раз — и следом, на том же круге,
   PolymarketClobMiner.run_cycle() (реальные bid/ask по CLOB API, см. ниже).
2. Раз в сутки, в config.scheduler.actuals_trigger_hour по Сиэтлу, СТАВИТ
   майнер погоды НА ПАУЗУ и вместо очередного прохода запускает дневной
   пайплайн: майнер факта (ActualsMiner, только за/до target_date, см.
   _target_date), затем отчёт (AccuracyReportBuilder), затем отбор городов по
   критериям (CityCriteriaSelector) и отправку их в Telegram + сохранение
   JSONL (QualifyingCitiesNotifier).
3. После пайплайна майнинг исторических данных продолжается как ни в чём не
   бывало — просто следующая итерация основного цикла снова вызывает
   WeatherMiner.run_cycle()/PolymarketClobMiner.run_cycle().

Отдельно, ВНУТРИ каждого прохода WeatherMiner.run_cycle() (без своего цикла и
без паузы) работает PriceMonitor — "на лету" проверяет ДВА независимых набора
городов на слоте config.price_monitor.monitor_slot: watched_icaos_weather_forecast.yaml
(таргет = прогноз этого города + его поправка) и watched_icaos_max_bet.yaml
(таргет = бакет самой дорогой ставки, без прогноза погоды вовсе) — и шлёт
уведомление в Telegram (+ опционально звонок для первого набора; для второго
звонки выключены по умолчанию), если цена ставки на таргет попадает в
диапазон [min_price_limit, max_price_limit] ЭТОГО города (см. price_monitor.py,
watched_icaos.py).

PolymarketClobMiner (src/polymarket_clob_miner.py) — независимый от всего
вышеперечисленного майнер: реальные bid/ask по КАЖДОМУ бакету (Yes и No)
события Polymarket, на 15-минутной сетке (:00/:15/:30/:45) по ЛОКАЛЬНОМУ
времени каждого города, только в дневном окне [06:00, 18:00). Через CLOB API
(HTTP-запросы, без Selenium) — не связан с WeatherMiner/PriceMonitor и их
слотами 06:00/09:00/12:00 никак, кроме общего цикла Orchestrator, на котором
оба вызываются подряд.

PolymarketActualsMiner (src/polymarket_actuals_miner.py) — факт СОБСТВЕННОГО
разрешения события Polymarket (какой бакет реально выиграл, по данным самого
рынка) — отдельный от Wunderground-факта (ActualsMiner) источник истины.
Через Gamma API (без Selenium). Вызывается ИЗ ДНЕВНОГО ПАЙПЛАЙНА
(run(target_date=...)), СРАЗУ ПОСЛЕ actuals_miner.run(), тем же target_date —
ПО ВСЕМ городам сразу, тем же принципом, что и ActualsMiner (см.
_run_daily_pipeline ниже). Отдельно этот же метод — для ручного бэкфилла ПО
ВСЕМ городам произвольного месяца из JN (месяц — параметр функции).

Открытые позиции авто-торговли (см. src/trader.py) разрешаются ДВУМЯ путями,
без отдельного фонового опроса между ними:
1. TradingEngine.on_signal() -- ПРЯМО ПЕРЕД тем, как открыть новую позицию по
   пирамиде, синхронно пытается разрешить ВИСЯЩУЮ предыдущую позицию по тому
   же городу (см. TradingEngine._try_resolve_now): сначала смотрит локальный
   кэш факта, если там пусто -- делает один живой запрос к Gamma API
   (PolymarketActualsMiner.fetch_and_store_single, см. Orchestrator.
   _fetch_actuals_fact_now) ИМЕННО по этой (icao, date). Это гарантирует, что
   лот новой позиции всегда считается по максимально свежему факту, а не по
   устаревшему счётчику пирамиды (см. кейс Лакхнау, лоты 5, 10, 10 вместо
   5, 10, 5).
2. Дневной пайплайн (раз в сутки, см. _run_daily_pipeline ниже) -- разрешает
   ВСЕ ещё висящие позиции по всем городам, как финальная подстраховка на
   случай, если по городу вообще больше не будет ни одного сигнала.

Логи по компонентам — в СВОИХ ПОДПАПКАХ logs/<компонент>/ (actuals_miner,
orchestrator, price_monitor, weather_miner, polymarket_clob_miner,
polymarket_actuals_miner), а не плоско в logs/ — см. вызовы build_logger ниже
и в самих модулях.

Идея "паузы": WeatherMiner больше не содержит собственного бесконечного цикла
со сном — run_cycle() делает один проход и возвращается. Этим управляет
Orchestrator.run(): на каждой итерации своего цикла либо запускает дневной
пайплайн (если наступило время и он ещё не запускался сегодня), либо один
проход майнера погоды — так что "пауза" это просто пропуск прохода майнера
погоды на те несколько минут, что выполняется пайплайн.
"""

import logging
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .accuracy_report import AccuracyReportBuilder
from .actuals_miner import ActualsMiner
from .base_miner import build_logger, should_suppress_telegram_error
from .cities import CITIES
from .criteria import CityCriteriaSelector
from .notifier import QualifyingCitiesNotifier
from .polymarket_actuals_miner import PolymarketActualsMiner
from .polymarket_clob_miner import PolymarketClobMiner
from .price_monitor import PriceMonitor
from .telegram_client import TelegramClient
from .trader import TradingEngine
from .watched_icaos import load_watched_icaos
from .weather_miner import WeatherMiner


class Orchestrator:
    def __init__(self, config, cities=None):
        self.config = config
        self.cities = cities or CITIES
        self.logger = build_logger(
            "Orchestrator", os.path.join(config.paths.log_dir, "orchestrator"), "orchestrator.log"
        )

        self.telegram = TelegramClient(config.telegram.token, config.telegram.chat_id, self.logger)

        self.watched = load_watched_icaos(
            config.resolve_config_path(config.price_monitor.weather_forecast.watched_icaos_file),
            loss_limit=getattr(config.price_monitor.weather_forecast, "loss_limit", None),
        )
        self.logger.info(
            f"📂 Загружено {len(self.watched)} городов из {config.price_monitor.weather_forecast.watched_icaos_file}"
        )
        self.watched_max_bet = load_watched_icaos(
            config.resolve_config_path(config.price_monitor.max_bet.watched_icaos_file),
            loss_limit=getattr(config.price_monitor.max_bet, "loss_limit", None),
        )
        self.logger.info(
            f"📂 Загружено {len(self.watched_max_bet)} городов из {config.price_monitor.max_bet.watched_icaos_file}"
        )

        # TradingEngine — авто-торговля по сигналам PriceMonitor (см.
        # src/trader.py). Строится ДО PriceMonitor (которому передаётся) и
        # НЕЗАВИСИМО от auto_trading -- сами флаги auto_trading (по каждой
        # стратегии) TradingEngine читает и проверяет внутри on_signal, так
        # что даже если обе выключены, движок просто ничего не делает, но
        # already умеет разрешать когда-то открытые позиции (resolve_positions).
        # actuals_fetcher=self._fetch_actuals_fact_now -- ссылается на
        # self.polymarket_actuals_miner, который создаётся НИЖЕ по коду в
        # этом же __init__ -- это нормально: сам вызов произойдёт намного
        # позже, уже во время работы (см. TradingEngine._try_resolve_now), к
        # тому моменту __init__ давно завершится и атрибут будет на месте.
        self.trading_engine = (
            TradingEngine(
                config, self.cities, self.telegram,
                build_logger("Trader", os.path.join(config.paths.log_dir, "trader"), "trader.log"),
                watched=self.watched, watched_max_bet=self.watched_max_bet,
                actuals_fetcher=self._fetch_actuals_fact_now,
            )
            if config.price_monitor.enabled else None
        )
        self.price_monitor = (
            PriceMonitor(
                config, self.cities, self.telegram,
                build_logger(
                    "PriceMonitor", os.path.join(config.paths.log_dir, "price_monitor"), "price_monitor.log"
                ),
                watched=self.watched, watched_max_bet=self.watched_max_bet,
                trading_engine=self.trading_engine,
                clob_price_fetcher=self._fetch_live_clob_price_now,
                clob_top_bucket_fetcher=self._fetch_live_clob_top_bucket_now,
                gamma_bets_fetcher=self._fetch_gamma_bets_now,
            )
            if config.price_monitor.enabled else None
        )
        self.weather_miner = WeatherMiner(config, self.cities, self.telegram, price_monitor=self.price_monitor)
        self.actuals_miner = ActualsMiner(config, self.cities, self.telegram)
        self.polymarket_clob_miner = PolymarketClobMiner(config, self.cities, self.telegram)
        self.polymarket_actuals_miner = PolymarketActualsMiner(config, self.cities, self.telegram)
        self.report_builder = AccuracyReportBuilder(config, self.cities)
        self.criteria_selector = CityCriteriaSelector(config, self.cities)
        self.notifier = QualifyingCitiesNotifier(config, self.cities, self.telegram, self.logger)

        self._seattle_tz = ZoneInfo(config.scheduler.seattle_timezone)
        # Дата по Сиэтлу, В КОТОРУЮ (не "за которую"!) пайплайн уже
        # запускался -- сравнивается с now.date() в _should_run_pipeline_now,
        # поэтому присваивается именно она (today_seattle в
        # _run_daily_pipeline), а НЕ target_date (день, за который майнится
        # факт -- "вчера" относительно этой даты, см. _target_date).
        self._last_pipeline_date = None

    # ------------------------------------------------------------------ #
    # Расписание
    # ------------------------------------------------------------------ #

    def _seattle_now(self):
        return datetime.now(self._seattle_tz)

    def _notify_error_telegram(self, title, exc):
        """
        Отправляет ошибку в Telegram, если она не попадает под фильтр
        config.error_notifications.telegram_suppress_patterns (см.
        base_miner.should_suppress_telegram_error) — ошибка в любом случае уже
        залогирована вызывающим кодом ДО этого вызова, здесь только решается,
        уходит ли она ещё и в чат.
        """
        if should_suppress_telegram_error(self.config, exc):
            self.logger.debug(f"🔇 [{title}] Транзиентная ошибка подавлена для Telegram (см. лог выше)")
            return
        self.telegram.send_message(f"🔴 <b>{title}</b>\n{exc}")

    def _target_date(self):
        """
        День, за который майнится факт и строится отчёт. Пайплайн триггерится
        СРАЗУ ПОСЛЕ ПОЛУНОЧИ по Сиэтлу (config.scheduler.actuals_trigger_hour,
        по умолчанию 0) — Сиэтл самый западный часовой пояс среди cities.py,
        так что к этому моменту сутки уже гарантированно завершились у ВСЕХ
        городов (у остальных часовые пояса не западнее). "Текущая дата по
        Сиэтлу" на момент запуска — это уже НОВЫЙ день, а нужен тот, что
        ТОЛЬКО ЧТО завершился — поэтому offset_days=1 по умолчанию (не 0).
        Параметризовано через config.scheduler.target_date_offset_days —
        можно сдвинуть на N дней назад при необходимости.
        """
        offset_days = self.config.scheduler.target_date_offset_days
        return (self._seattle_now() - timedelta(days=offset_days)).date()

    def _should_run_pipeline_now(self):
        now = self._seattle_now()
        today = now.date()
        if self._last_pipeline_date == today:
            return False  # уже запускали сегодня
        trigger_hour = self.config.scheduler.actuals_trigger_hour
        window_min = self.config.scheduler.actuals_trigger_minute_window
        return now.hour == trigger_hour and now.minute < window_min

    # ------------------------------------------------------------------ #
    # Живой запрос факта по ОДНОЙ (icao, date) паре -- для TradingEngine
    # ------------------------------------------------------------------ #

    def _fetch_actuals_fact_now(self, icao, date_str):
        """
        Передаётся в TradingEngine как actuals_fetcher (см. конструктор
        выше) -- живой, синхронный запрос факта Polymarket ПО ОДНОЙ
        (icao, date) паре, делегируется PolymarketActualsMiner.
        fetch_and_store_single. Используется TradingEngine._try_resolve_now
        ПРЯМО ПЕРЕД тем, как посчитать лот новой позиции по пирамиде -- это
        ЕДИНСТВЕННАЯ проверка факта по открытой позиции вне дневного
        пайплайна (раз в сутки), которая гарантирует, что решение о лоте
        всегда учитывает максимально свежий факт по предыдущей позиции --
        отдельного фонового опроса "на каждом круге"/"раз в N минут" нет,
        он специально не заводился: эта проверка, прямо в момент принятия
        решения, уже достаточна.
        """
        return self.polymarket_actuals_miner.fetch_and_store_single(icao, date_str)

    # ------------------------------------------------------------------ #
    # Живая цена бакета по CLOB -- для PriceMonitor
    # ------------------------------------------------------------------ #

    def _fetch_live_clob_price_now(self, icao, now_local, target_label):
        """
        Передаётся в PriceMonitor как clob_price_fetcher (см. конструктор
        выше) -- ссылается на self.polymarket_clob_miner, который создаётся
        НИЖЕ по коду в этом же __init__ (та же отложенная привязка, что и у
        _fetch_actuals_fact_now/actuals_fetcher выше: сам вызов произойдёт
        намного позже, уже во время работы, когда __init__ давно завершится).
        Делегируется PolymarketClobMiner.fetch_live_price -- разовый живой
        запрос РЕАЛЬНОЙ цены (ask_yes через CLOB) конкретного бакета, вместо
        цены из Gamma API, которую иначе использовал бы PriceMonitor.
        """
        return self.polymarket_clob_miner.fetch_live_price(icao, now_local, target_label)

    def _fetch_live_clob_top_bucket_now(self, icao, now_local):
        """
        Передаётся в PriceMonitor как clob_top_bucket_fetcher (см.
        конструктор выше) -- та же отложенная привязка к
        self.polymarket_clob_miner, что и у _fetch_live_clob_price_now.
        Делегируется PolymarketClobMiner.fetch_live_top_bucket -- для набора
        max_bet определяет "самую дорогую ставку" ПО РЕАЛЬНЫМ ценам CLOB (по
        ВСЕМ бакетам события сразу), а не по последней цене сделки Gamma.
        """
        return self.polymarket_clob_miner.fetch_live_top_bucket(icao, now_local)

    def _fetch_gamma_bets_now(self, icao, now_local):
        """
        Передаётся в PriceMonitor как gamma_bets_fetcher (см. конструктор
        выше) — та же отложенная привязка к self.polymarket_clob_miner, что
        и у _fetch_live_clob_price_now/_fetch_live_clob_top_bucket_now.
        Делегируется PolymarketClobMiner.fetch_gamma_bets — живой список
        ставок Gamma (label+price), заменяет собой прежний отдельный
        Gamma-майнинг WeatherMiner'а (см. git-историю и price_monitor.py).
        """
        return self.polymarket_clob_miner.fetch_gamma_bets(icao, now_local)

    # ------------------------------------------------------------------ #
    # Дневной пайплайн (пункты 2, 3, 4, 5 ТЗ)
    # ------------------------------------------------------------------ #

    def _run_daily_pipeline(self):
        # today_seattle -- СЕГОДНЯШНЯЯ дата по Сиэтлу (то, что реально
        # сравнивает _should_run_pipeline_now), а не target_date (ДЕНЬ, ЗА
        # КОТОРЫЙ майнится факт/строится отчёт -- "вчера" относительно
        # today_seattle, см. _target_date/target_date_offset_days=1). Раньше
        # self._last_pipeline_date в конце этого метода присваивался именно
        # target_date ("вчера"), а _should_run_pipeline_now сравнивал его с
        # today ("сегодня") -- эти две даты НИКОГДА не совпадают (разница
        # ровно в offset_days=1 сутки), поэтому проверка "уже запускали
        # сегодня" была полностью сломана с самого начала: пайплайн
        # перезапускался на КАЖДОМ проходе цикла (cycle_sleep_sec, обычно
        # 5 минут) внутри всего 15-минутного окна триггера -- то есть
        # реально 2-3 раза подряд, а не только при сетевом сбое, который я
        # чинил в прошлый раз (та правка тоже была нужна, но это отдельная,
        # более редкая причина -- основная вот эта).
        today_seattle = self._seattle_now().date()
        target_date = self._target_date()
        self.logger.info(f"⏸️ Пауза майнера погоды — запускаю дневной пайплайн за {target_date.isoformat()}")
        # Обёрнуто в try/except, как и все шаги ниже -- раньше это был
        # ЕДИНСТВЕННЫЙ непойманный вызов в начале функции: если send_message
        # падал (сетевая ошибка/таймаут Telegram), исключение улетало прямо
        # в run() (см. его except ниже), self._last_pipeline_date так и не
        # присваивался -- и на следующем проходе цикла (cycle_sleep_sec,
        # обычно 5 минут), всё ещё внутри 15-минутного окна
        # actuals_trigger_hour/actuals_trigger_minute_window, весь пайплайн
        # запускался ЗАНОВО целиком (майнер факта, resolve_positions,
        # дневная сводка P&L -- лишний раз и ПОВТОРНО). Теперь функция
        # больше нигде не может бросить исключение наружу, поэтому
        # self._last_pipeline_date ниже гарантированно выставляется ровно
        # один раз за проход.
        try:
            self.telegram.send_message(
                f"⏸️ <b>Пауза майнера погоды</b> — запускаю факт/отчёт/отбор городов за {target_date.isoformat()}"
            )
        except Exception as e:
            self.logger.error(f"⚠️ Ошибка отправки сообщения о паузе майнера погоды: {e}")

        # --- 2. Майнер факта (только за/до target_date, инкрементально) ---
        try:
            self.actuals_miner.run(target_date=target_date)
        except Exception as e:
            self.logger.error(f"⚠️ Ошибка майнера фактических максимумов: {e}")
            self._notify_error_telegram("Ошибка майнера фактических максимумов", e)

        # --- 2б. Факт Polymarket (тот же target_date, тот же принцип) ---
        try:
            self.polymarket_actuals_miner.run(target_date=target_date)
        except Exception as e:
            self.logger.error(f"⚠️ Ошибка майнера факта Polymarket: {e}")
            self._notify_error_telegram("Ошибка майнера факта Polymarket", e)

        # --- 2в. Разрешение открытых позиций авто-торговли по свежему факту
        # Polymarket (см. src/trader.py) — СРАЗУ после факта, а не только за
        # target_date: заодно доразрешает и более ранние позиции, если факт
        # по ним домайнился с опозданием. ---
        if self.trading_engine is not None:
            try:
                self.trading_engine.resolve_positions()
            except Exception as e:
                self.logger.error(f"⚠️ Ошибка разрешения позиций авто-торговли: {e}")
                self._notify_error_telegram("Ошибка разрешения позиций авто-торговли", e)

            # --- 2г. Ежедневная сводка авто-торговли в Telegram (баланс,
            # P&L за сутки и по городам, см. config.price_monitor.daily_summary_enabled) ---
            try:
                self.trading_engine.send_daily_summary(target_date.isoformat())
            except Exception as e:
                self.logger.error(f"⚠️ Ошибка отправки дневной сводки авто-торговли: {e}")
                self._notify_error_telegram("Ошибка отправки дневной сводки авто-торговли", e)

        # --- 3. Отчёт (один файл, перезаписывается) + 4/5. отбор и рассылка ---
        try:
            df_raw, skip_info = self.report_builder.build_dataframe()
            self.report_builder.print_skip_details(skip_info)

            if df_raw.empty:
                self.logger.warning("⚠️ Нет пересекающихся данных прогноза и факта — отчёт не построен.")
                self.telegram.send_message("⚠️ Отчёт не построен — нет пересекающихся данных прогноза и факта.")
            else:
                # Второй вариант — с галочкой "1 час с максимумом не учитывается"
                # (см. AccuracyReportBuilder._effective_max) — строится только для
                # HTML-отчётов; отбор по критериям и PriceMonitor всегда работают
                # с сырым максимумом (df_raw).
                df_effective, _ = self.report_builder.build_dataframe(effective_max=True)
                dfs = {"raw": df_raw, "effective": df_effective}

                out_path = self.report_builder.save_report(dfs)
                self.logger.info(f"📊 Отчёт сохранён: {out_path}")

                watched_out_path = self.report_builder.save_watched_report(dfs, self.watched)
                if watched_out_path:
                    self.logger.info(f"📊 Скорректированный отчёт (watched_icaos) сохранён: {watched_out_path}")
                else:
                    self.logger.warning("⚠️ Скорректированный отчёт не построен — нет данных по городам из watched_icaos_weather_forecast.yaml.")

                qualifying = self.criteria_selector.select(df_raw)
                saved_path = self.notifier.send_and_save(qualifying, run_date=target_date)
                self.logger.info(
                    f"🏆 Городов по критериям: {len(qualifying)} — отправлено в Telegram, сохранено в {saved_path}"
                )
        except Exception as e:
            self.logger.error(f"⚠️ Ошибка построения отчёта/отбора городов: {e}")
            self._notify_error_telegram("Ошибка построения отчёта", e)

        # --- Отчёт по самой дорогой ставке (без прогноза погоды вовсе) —
        # строится НЕЗАВИСИМО от отчёта по прогнозу выше (своя, отдельная
        # дата-основа из Polymarket, см. build_market_target_dataframe) ---
        try:
            max_bet_out_path = self.report_builder.save_market_target_report(
                self.watched_max_bet, self.config.price_monitor.monitor_slot,
            )
            if max_bet_out_path:
                self.logger.info(f"📊 Отчёт по самой дорогой ставке (watched_icaos_max_bet) сохранён: {max_bet_out_path}")
            else:
                self.logger.warning(
                    "⚠️ Отчёт по самой дорогой ставке не построен — нет данных по городам из watched_icaos_max_bet.yaml."
                )
        except Exception as e:
            self.logger.error(f"⚠️ Ошибка построения отчёта по самой дорогой ставке: {e}")
            self._notify_error_telegram("Ошибка построения отчёта по самой дорогой ставке", e)

        # --- 6. Возобновление майнера исторических данных ---
        # self._last_pipeline_date выставляется ДО отправки сообщения ниже --
        # см. комментарий в начале метода: если именно этот send_message
        # упадёт, пайплайн всё равно не должен запускаться второй раз в том
        # же 15-минутном окне, ведь вся настоящая работа (факт, резолв,
        # отчёт, сводка) уже выполнена. ВАЖНО: тут именно today_seattle (а
        # НЕ target_date) -- см. комментарий в начале метода, это и есть
        # исправление основного бага задвоения.
        self._last_pipeline_date = today_seattle
        self.logger.info("▶️ Дневной пайплайн завершён — возобновляю майнер погоды")
        try:
            self.telegram.send_message("▶️ <b>Майнер погоды возобновлён</b>")
        except Exception as e:
            self.logger.error(f"⚠️ Ошибка отправки сообщения о возобновлении майнера погоды: {e}")

    # ------------------------------------------------------------------ #
    # Главный цикл
    # ------------------------------------------------------------------ #

    def run(self):
        self.logger.info("🚀 Оркестратор запущен")
        self.telegram.send_message("✅ <b>Оркестратор запущен</b>")
        cycle_sleep_sec = self.config.scheduler.cycle_sleep_sec

        try:
            while True:
                try:
                    if self._should_run_pipeline_now():
                        self._run_daily_pipeline()
                    else:
                        self.weather_miner.run_cycle()
                        self.polymarket_clob_miner.run_cycle()
                except Exception as e:
                    self.logger.error(f"⚠️ Ошибка основного цикла: {e}")
                    self._notify_error_telegram("Ошибка оркестратора", e)
                self.logger.info(f"Круг завершён. Спим {cycle_sleep_sec} сек.")
                time.sleep(cycle_sleep_sec)
        except KeyboardInterrupt:
            self.logger.info("⛔ Оркестратор остановлен вручную.")
            self.telegram.send_message("⛔ <b>Оркестратор остановлен вручную</b>")
