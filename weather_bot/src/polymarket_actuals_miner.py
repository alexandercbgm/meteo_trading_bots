"""
polymarket_actuals_miner.py

Класс PolymarketActualsMiner — факт СОБСТВЕННОГО разрешения события
Polymarket (какой бакет реально выиграл в этот день, по данным самого рынка:
у выигравшего исхода "Yes" outcomePrices близко к 1) — отдельный от
Wunderground-факта (ActualsMiner) источник истины, специально для
отчётов/сигналов, построенных на "самой дорогой ставке"
(AccuracyReportBuilder.build_market_target_report, watched_icaos_max_bet.yaml).

Через Gamma API (обычные HTTP-запросы, requests) — БЕЗ Selenium: к моменту
майнинга (день уже прошёл) событие обычно уже закрыто, и выигравший исход
виден напрямую по outcomePrices рынка, без скрейпинга страницы.

Один и тот же метод run(target_date=...) / mine_missing_for_month(year,
month, up_to_date) — ПО ВСЕМ городам сразу, дозаписывает НЕДОСТАЮЩИЕ дни
месяца (month — параметр функции, можно перезагрузить любой месяц из JN, не
только текущий) — используется ОБОИМИ режимами:

1. Автоматически — вызывается ИЗ ДНЕВНОГО ПАЙПЛАЙНА Orchestrator
   (_run_daily_pipeline), СРАЗУ ПОСЛЕ actuals_miner.run(target_date=...), с
   ТЕМ ЖЕ target_date (см. Orchestrator._target_date — "вчера" по Сиэтлу,
   пересчитанное сразу после полуночи там же). Раньше здесь был отдельный
   "начало суток КАЖДОГО города по отдельности" триггер (run_cycle) — от него
   отказались: к моменту полуночи по Сиэтлу сутки уже гарантированно
   завершились у ВСЕХ городов из cities.py (тот же принцип, что уже применён
   для actuals_miner, см. scheduler в config.yaml) — так что отдельный
   постоянный опрос "чьё сейчас начало суток" не нужен, событие для КАЖДОГО
   города к этому моменту уже разрешилось (или ещё нет — тогда просто
   останется в "недостающих" и домайнится на следующий день пайплайном).
2. Вручную — из JN, для бэкфилла/перезагрузки произвольного месяца.

Запросы к Gamma API — по одному на КАЖДУЮ недостающую пару (город, дата)
(_fetch_resolved_bucket). Раньше здесь была попытка батчить их в один
запрос (`?slug=a&slug=b&...`, по документации Gamma API `/events` должен
поддерживать несколько `slug`-параметров и возвращать массив найденных
событий сразу по всем) — ОТКАЧЕНО: на практике при нескольких `slug`
одновременно API возвращает результаты только по ОДНОМУ (не всегда даже
первому) из запрошенных событий, а не по каждому — часть городов из батча
молча получала "событие не найдено", хотя событие реально существует и уже
разрешилось (воспроизведено в проде: из 12 городов в одном батче нашлись
только 2, для остальных факт не записался бы никогда, пока их дата не
попадёт в батч без "конкурентов"). Один запрос на пару — медленнее при
бэкфилле большого диапазона дат, зато не теряет данные незаметно.

Записи — по одной на (город, день), по файлу на МЕСЯЦ (не на сутки) — в ТОЙ
ЖЕ папке, что живые цены слотов (config.paths.polymarket_gamma_dir),
различаются только по имени файла: polymarket_gamma_<YYYY>_<MM>.jsonl (факт,
этот модуль) vs polymarket_gamma_<YYYY>_<MM>_<DD>.jsonl (живые цены слотов
6/9/12, WeatherMiner.scrape_polymarket) — специально не разносим по разным
папкам: одно — то, что можно домайнить/перемайнить задним числом, другое —
то, что майнится строго "в моменте" и заново взять неоткуда.
{"date", "icao", "city", "bucket", "scale"} — bucket — ЛЕЙБЛ (диапазон, не
единственное число, в отличие от Wunderground-факта) победившего исхода, в
НАТИВНОЙ шкале рынка этого города (определяется по символу °F/°C в самом
лейбле — независимо от determine_city_scales() в accuracy_report.py).
"""

import json
import os
import re
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta

import requests

from .base_miner import build_logger

GAMMA_BASE = "https://gamma-api.polymarket.com"
TEMP_LABEL_RE = re.compile(r"\d+-\d+°[CF]|\d+°[CF](?:\s+or\s+(?:below|above|higher|lower))?")


class PolymarketActualsMiner:
    def __init__(self, config, cities, telegram, logger=None):
        self.cities = cities
        self.telegram = telegram
        self.data_dir = config.paths.polymarket_gamma_dir

        pam = config.polymarket_actuals_miner
        self.resolved_yes_price_threshold = pam.resolved_yes_price_threshold
        self.send_summary = getattr(pam, "send_summary", True)

        self.logger = logger or build_logger(
            "PolymarketActualsMiner", os.path.join(config.paths.log_dir, "polymarket_actuals_miner"),
            "polymarket_actuals_miner.log",
        )

    # ------------------------------------------------------------------ #
    # Инфраструктура вывода / проверка уже смайненного (тот же принцип, что в ActualsMiner)
    # ------------------------------------------------------------------ #

    def _month_output_path(self, year, month):
        """
        polymarket_gamma_<YYYY>_<MM>.jsonl — ПО МЕСЯЦУ (без дня), в ТОЙ ЖЕ
        папке, что живые цены слотов (polymarket_gamma_<YYYY>_<MM>_<DD>.jsonl,
        см. WeatherMiner.SOURCE_FILE_PREFIX) — разные типы записей различаются
        только по имени файла (день в имени есть/нет), см. пояснение в config.yaml.
        """
        os.makedirs(self.data_dir, exist_ok=True)
        return os.path.join(self.data_dir, f"polymarket_gamma_{year}_{month:02d}.jsonl")

    def _load_existing_dates(self, year, month):
        """{icao: {date_str, ...}} — уже сохранённые записи месяца."""
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
        """Как в ActualsMiner._last_day_to_mine — см. пояснение там."""
        days_in_month = monthrange(year, month)[1]
        if (year, month) == (up_to_date.year, up_to_date.month):
            return min(up_to_date.day, days_in_month)
        if (year, month) < (up_to_date.year, up_to_date.month):
            return days_in_month
        return 0

    # ------------------------------------------------------------------ #
    # Gamma API — резолв выигравшего бакета события за конкретный день
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_temp_label(question):
        match = TEMP_LABEL_RE.search(question or "")
        return match.group(0) if match else None

    @staticmethod
    def _event_slug(slug, date_str):
        y, m, d = (int(x) for x in date_str.split("-"))
        dt = date(y, m, d)
        month_name = dt.strftime("%B").lower()
        return f"highest-temperature-in-{slug}-on-{month_name}-{dt.day}-{dt.year}"

    def _resolve_bucket_from_event(self, event, icao, date_str):
        """
        {"label": str, "scale": "F"|"C"} победившего бакета УЖЕ ПОЛУЧЕННОГО
        события (event — один элемент ответа Gamma API /events), или None —
        ни один исход ещё не разрешился (не пересёк resolved_yes_price_threshold)
        либо формат вопроса не распознан.
        """
        for market in event.get("markets", []):
            question = market.get("question", "")
            try:
                outcomes = json.loads(market["outcomes"])
                prices = json.loads(market["outcomePrices"])
            except (KeyError, json.JSONDecodeError, TypeError):
                continue
            if "Yes" not in outcomes:
                continue
            yes_idx = outcomes.index("Yes")
            try:
                yes_price = float(prices[yes_idx])
            except (ValueError, IndexError):
                continue
            if yes_price < self.resolved_yes_price_threshold:
                continue

            label = self._extract_temp_label(question)
            if not label:
                continue
            scale = "F" if "°F" in label else ("C" if "°C" in label else None)
            if scale is None:
                continue
            return {"label": label, "scale": scale}

        self.logger.info(
            f"ℹ️ [Gamma-API] {icao} {date_str}: ни один исход ещё не разрешился "
            f"(Yes >= {self.resolved_yes_price_threshold})"
        )
        return None

    def _fetch_resolved_bucket(self, icao, date_str):
        """Один HTTP-запрос -- один event_slug (см. пояснение в докстринге модуля,
        почему не батчами)."""
        slug = self.cities[icao]["slug"]
        event_slug = self._event_slug(slug, date_str)
        try:
            resp = requests.get(f"{GAMMA_BASE}/events", params={"slug": event_slug}, timeout=15)
            resp.raise_for_status()
            events = resp.json()
        except Exception as e:
            self.logger.error(f"❌ [Gamma-API] Gamma API ошибка {icao} {date_str}: {e}")
            return None
        if not events:
            self.logger.info(f"ℹ️ [Gamma-API] {icao} {date_str}: событие не найдено ({event_slug})")
            return None
        return self._resolve_bucket_from_event(events[0], icao, date_str)

    def _fetch_resolved_buckets(self, items):
        """
        items — [(icao, date_str), ...]. По одному запросу на каждую пару
        (см. докстринг модуля -- почему не батчами). Возвращает
        {(icao, date_str): {"label", "scale"} | None}.
        """
        return {(icao, date_str): self._fetch_resolved_bucket(icao, date_str) for icao, date_str in items}

    # ------------------------------------------------------------------ #
    # Основной проход: недостающие дни месяца, до up_to_date включительно
    # (опционально — только для подмножества городов, см. icaos)
    # ------------------------------------------------------------------ #

    def _mine_dates_in_month(self, year, month, wanted_dates, icaos=None):
        """
        Дозаписывает (не перезаписывает!) файл месяца: для КАЖДОГО города из
        `icaos` (по умолчанию — из self.cities, то есть ВСЕХ) майнит ТОЛЬКО
        те даты из wanted_dates, которых ещё нет в файле. icaos=[...] — режим
        "только этот(и) город(а)" для run_cycle() (майнит вчера ровно ОДНОГО
        города, не трогая остальные на каждом срабатывании триггера).
        Возвращает число реально дозаписанных строк.
        """
        target_cities = {icao: self.cities[icao] for icao in icaos} if icaos is not None else self.cities

        path = self._month_output_path(year, month)
        existing = self._load_existing_dates(year, month)

        missing_items = []  # [(icao, date_str), ...] по ВСЕМ городам сразу
        for icao in target_cities:
            already = existing.get(icao, set())
            missing_dates = wanted_dates - already
            if not missing_dates:
                self.logger.debug(f"⏭️ [Gamma-API] {icao} {year}-{month:02d}: уже смайнено всё нужное, пропуск")
                continue
            missing_items.extend((icao, date_str) for date_str in sorted(missing_dates))

        if not missing_items:
            self.logger.info(f"💾 [Gamma-API] {year}-{month:02d}: дозаписано 0 новых строк в {path} (нечего майнить)")
            return 0

        resolved_by_item = self._fetch_resolved_buckets(missing_items)

        written = 0
        with open(path, "a", encoding="utf-8") as f:
            for icao, date_str in missing_items:
                resolved = resolved_by_item.get((icao, date_str))
                if resolved is None:
                    continue
                record = {
                    "date": date_str, "icao": icao, "city": target_cities[icao]["name"],
                    "bucket": resolved["label"], "scale": resolved["scale"],
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                written += 1
                self.logger.info(f"💾 [Gamma-API] {icao} {date_str}: {resolved['label']}")

        self.logger.info(f"💾 [Gamma-API] {year}-{month:02d}: дозаписано {written} новых строк в {path}")
        return written

    def mine_missing_for_month(self, year, month, up_to_date):
        """Дозаписывает недостающие даты месяца в диапазоне [1, last_day_to_mine]
        ПО ВСЕМ городам — см. ActualsMiner.mine_missing_for_month."""
        last_day = self._last_day_to_mine(year, month, up_to_date)
        if last_day == 0:
            self.logger.info(f"⏭️ [Gamma-API] {year}-{month:02d}: месяц ещё не наступил относительно {up_to_date}, пропуск")
            return 0
        wanted_dates = {f"{year}-{month:02d}-{d:02d}" for d in range(1, last_day + 1)}
        return self._mine_dates_in_month(year, month, wanted_dates)

    def fetch_and_store_single(self, icao, date_str):
        """
        Разовый синхронный запрос факта ПО ОДНОЙ (icao, date) паре -- для
        TradingEngine.on_signal (см. TradingEngine._try_resolve_now и
        Orchestrator._fetch_actuals_fact_now): нужно узнать результат
        предыдущей позиции ПРЯМО СЕЙЧАС, перед тем как посчитать лот новой
        по пирамиде -- это ЕДИНСТВЕННОЕ место, где факт по открытой позиции
        авто-торговли проверяется вне дневного пайплайна (раз в сутки).

        Идемпотентно: если факт уже есть на диске -- просто читает его
        оттуда, без HTTP-запроса; если нет -- делает ОДИН запрос и, если
        рынок уже резолвился, дописывает строку (та же дедупликация по
        already-existing, что и в run() -- повторные вызовы не плодят
        дубликаты в файле месяца).

        Возвращает (bucket_label, scale) -- тот же формат, что и значения
        _load_month_facts() в trader.py -- или None, если рынок ещё не
        резолвился.
        """
        year, month = int(date_str[:4]), int(date_str[5:7])
        existing = self._load_existing_dates(year, month)
        if date_str in existing.get(icao, set()):
            path = self._month_output_path(year, month)
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    if rec["icao"] == icao and rec["date"] == date_str:
                        return rec["bucket"], rec["scale"]
            return None  # не должно случиться (icao/date числится в existing) -- на всякий случай

        resolved = self._fetch_resolved_bucket(icao, date_str)
        if resolved is None:
            return None

        path = self._month_output_path(year, month)
        record = {
            "date": date_str, "icao": icao, "city": self.cities[icao]["name"],
            "bucket": resolved["label"], "scale": resolved["scale"],
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.logger.info(
            f"💾 [Gamma-API] {icao} {date_str}: {resolved['label']} (живой запрос перед новым сигналом авто-торговли)"
        )
        return resolved["label"], resolved["scale"]

    def mine_missing_for_dates(self, dates):
        """Как ActualsMiner.mine_missing_for_dates — строго переданный набор
        конкретных дат, сгруппированный по месяцам."""
        by_month = defaultdict(set)
        for d in dates:
            by_month[(d.year, d.month)].add(f"{d.year}-{d.month:02d}-{d.day:02d}")

        total_written = 0
        for (year, month), wanted_dates in sorted(by_month.items()):
            total_written += self._mine_dates_in_month(year, month, wanted_dates)
        return total_written

    # ------------------------------------------------------------------ #
    # Точка входа (ручной/догоняющий запуск — как ActualsMiner.run)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_date(value):
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return datetime.strptime(value, "%Y-%m-%d").date()

    def run(self, target_date=None):
        """
        target_date — как у ActualsMiner.run: None (сегодня), ОДНА дата
        (догоняющий режим — весь месяц с 1 числа до неё), диапазон-tuple
        (обе границы включительно) или список конкретных дат.

        Уведомления в Telegram — "начал"/"закончил работу" (БЕЗУСЛОВНО, тем
        же принципом, что у ActualsMiner.run) + сводка по ВСЕМ городам ОДНИМ
        сообщением (гасится флагом send_summary) — вызывается ИЗ ТОГО ЖЕ
        дневного пайплайна Orchestrator, что и ActualsMiner (после полуночи
        по Сиэтлу, см. Orchestrator._run_daily_pipeline/_target_date) — а не
        отдельным "начало суток по каждому городу" триггером, как было
        раньше (см. объяснение в шапке модуля).
        """
        if isinstance(target_date, tuple):
            if len(target_date) != 2:
                raise ValueError("target_date как диапазон (tuple) должен содержать ровно 2 даты: (начало, конец)")
            start, end = sorted(self._parse_date(v) for v in target_date)
            dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
            label = f"{len(dates)} дат ({dates[0].isoformat()} .. {dates[-1].isoformat()})"
            self.logger.info(f"🚀 [Gamma-API] Майнер факта Polymarket: {label}")
            self.telegram.send_message(f"✅ <b>Майнер факта Polymarket запущен</b>\nЗа {label}")

            written = self.mine_missing_for_dates(dates)
            self.telegram.send_message(f"📋 <b>Майнер факта Polymarket завершил работу</b>\nЗа {label}: {written} новых строк")
            if self.send_summary:
                self.telegram.send_message(self._build_poly_actuals_daily_summary(dates[-1].isoformat()))
            return written

        if isinstance(target_date, list):
            dates = sorted({self._parse_date(v) for v in target_date})
            label = f"{len(dates)} дат ({dates[0].isoformat()} .. {dates[-1].isoformat()})" if len(dates) > 1 else dates[0].isoformat()
            self.logger.info(f"🚀 [Gamma-API] Майнер факта Polymarket: {label}")
            self.telegram.send_message(f"✅ <b>Майнер факта Polymarket запущен</b>\nЗа {label}")

            written = self.mine_missing_for_dates(dates)
            self.telegram.send_message(f"📋 <b>Майнер факта Polymarket завершил работу</b>\nЗа {label}: {written} новых строк")
            if self.send_summary:
                self.telegram.send_message(self._build_poly_actuals_daily_summary(dates[-1].isoformat()))
            return written

        target_date = self._parse_date(target_date) if target_date is not None else datetime.now().date()
        year, month = target_date.year, target_date.month
        self.logger.info(f"🚀 [Gamma-API] Майнер факта Polymarket: {year}-{month:02d} до {target_date.isoformat()}")
        self.telegram.send_message(
            f"✅ <b>Майнер факта Polymarket запущен</b>\nЗа {year}-{month:02d} до {target_date.isoformat()} включительно"
        )

        written = self.mine_missing_for_month(year, month, target_date)
        self.telegram.send_message(
            f"📋 <b>Майнер факта Polymarket завершил работу</b>\n{year}-{month:02d}: {written} новых строк"
        )
        if self.send_summary:
            self.telegram.send_message(self._build_poly_actuals_daily_summary(target_date.isoformat()))
        return written

    def _build_poly_actuals_daily_summary(self, date_str):
        """Сводка ПО ВСЕМ городам за одну дату, одним сообщением — тот же
        принцип, что у ActualsMiner._build_actuals_daily_summary."""
        year, month, _day = (int(x) for x in date_str.split("-"))
        existing = self._load_existing_dates(year, month)

        bucket_by_icao = {}
        path = self._month_output_path(year, month)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    if rec.get("date") == date_str:
                        bucket_by_icao[rec["icao"]] = rec["bucket"]

        lines = [f"📋 <b>Сводка факта Polymarket</b> за {date_str} #Fact_Summary"]
        for icao, info in self.cities.items():
            if date_str in existing.get(icao, set()) and icao in bucket_by_icao:
                lines.append(f"{info['name']}: {bucket_by_icao[icao]}✅")
            else:
                lines.append(f"{info['name']}: ❌")
        return "\n".join(lines)
