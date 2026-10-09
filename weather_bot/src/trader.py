"""
trader.py

TradingEngine — авто-торговля по сигналам PriceMonitor. Вызывается из
PriceMonitor._send_signal/_send_signal_max_bet (on_signal — открытие позиции,
только когда price_monitor.<strategy_set>.auto_trading: true) и из
Orchestrator._run_daily_pipeline, СРАЗУ ПОСЛЕ polymarket_actuals_miner.run()
(resolve_positions — разрешение открытых позиций по факту).

Логика лота/пирамиды/P&L — ТА ЖЕ, что в бэктесте отчётов (accuracy_report.py,
runBacktest/getBacktestTypeAndSteps):
  - flat: лот всегда config.price_monitor.<strategy_set>.lot_size;
  - pyramid: первая ставка серии -- lot_size; после промаха лот зависит от
    price_monitor.<набор>.pyramid_mode: "double" -- lot_size * множитель серии (pyramid_progression + pyramid_multiplier, см. pyramid.py);
    "recover" -- лот под отыгрыш накопленного убытка серии + цель (lot_size *
    pyramid_target_profit_price) по цене сигнала, с комиссией; "recover_min_multiplier" -- то же,
    но не меньше предыдущего лота x pyramid_multiplier (см. TradingEngine._compute_lot;
    цена дешевле min_bet_price_cents (5¢) -- ставка пропускается, иначе лот не меньше min_order_usd / цена). Промахи --
    свой счётчик на (strategy_set, icao), сбрасывается в 0 при попадании ИЛИ когда
    достигнут watch_cfg["loss_limit"] (per-city из watched_icaos_*.yaml) промахов
    подряд; вместе с ним сбрасывается и состояние серии (убыток, последний лот).
  - P&L: cost = order_price*lot + commission_per_share*lot, payout = lot если
    hit иначе 0, pnl = payout - cost (pnl_gross — то же самое БЕЗ комиссии,
    т.е. payout - order_price*lot — только для наглядности в логах/Telegram).

Цена лимитного ордера — ВСЕГДА max_price_limit конкретного города (или
defaults) из того же watched_icaos_*.yaml, что и сигнал — не текущая
рыночная цена и не min/max диапазон сигнала.

Размещение реальных ордеров — пакет `polymarket` (AsyncSecureClient), по
образцу проверенного пользовательского ad-hoc-скрипта: лимитный BUY,
config.price_monitor.fill_check_wait_sec секунд (по умолчанию 20) ожидания
исполнения — если НЕ исполнилась, заявка остаётся висеть в стакане (GTC),
результат (какой бы он ни был — открылась/не открылась) пишется в лог, в
JSONL И сразу в Telegram.
Приватный ключ/адрес кошелька — ТОЛЬКО из переменных окружения
(POLYMARKET_WALLET_ADDRESS, POLYMARKET_PRIVATE_KEY), никогда не из
config.yaml — см. README. AsyncSecureClient живёт в отдельном фоновом потоке
со своим event loop (создаётся один раз, лениво, при первой реальной
сделке) — тот же паттерн, что BaseSeleniumMiner.init_driver: фоновый поток +
join(timeout), основной синхронный код бота никогда не блокируется дольше
явного таймаута.

Проверка после ожидания (_reconcile, см. TradingEngine): исполнение НЕ
считается подтверждённым по одному только wait_for_fill() (были случаи,
когда settlement был подтверждён, а по факту ни ордера, ни позиции на
Polymarket не оказывалось) -- после ожидания ВСЕГДА дополнительно
проверяется реальная позиция (list_positions) и статус ордера. Если
ПОДТВЕРЖДЕНО (обе проверки прошли без ошибок), что нет ни ордера, ни
позиции -- заявка считается бесследно пропавшей и переразмещается заново,
максимум 3 попытки подряд; после 3 неудач -- лог, Telegram, сигнал
пропускается. Если ордер жив в стакане (в т.ч. когда рыночная цена ушла
выше лимита заявки) -- он НЕ переразмещается и не отменяется, это
нормальная ситуация (лог + Telegram, дальше отслеживается только при
resolve_positions). Любая ошибка САМОЙ проверки (сеть и т.п.) трактуется
консервативно как "не ясно" -- НИКОГДА не как повод переразместить заявку,
чтобы не задвоить лот.

Поздно исполнившиеся GTC-заявки: если ордер НЕ исполнился за первые
fill_check_wait_sec секунд после размещения, заявка остаётся висеть в
стакане (GTC, не отменяется).
Позиция при этом остаётся "открытой" (status: open) в open_positions.json.
Когда позже появляется факт Polymarket по этому дню/городу и
resolve_positions() пытается разрешить позицию, ОН СНАЧАЛА ЗАНОВО
опрашивает статус ордера (_verify_fill/get_order_status) — а не слепо
использует изначально заказанный lot/order_price. Поэтому:
  - если заявка успела исполниться (полностью или частично) к моменту
    разрешения — P&L считается по РЕАЛЬНО исполненному размеру
    (record["lot"] обновляется на фактический размер, исходный заказанный
    сохраняется отдельно как "requested_lot");
  - если заявка так и не исполнилась ни разу — позиция закрывается со
    status "resolved_no_fill", P&L = 0 (без costs/payout), и это НЕ
    засчитывается как промах для счётчика пирамиды (misses не меняется) —
    сделки не было вообще;
  - если сам повторный опрос статуса не удался (сетевая ошибка) — позиция
    НЕ разрешается на этом цикле (остаётся open), чтобы не насчитать P&L
    по неподтверждённым данным; попытка повторится на следующем вызове
    resolve_positions().

Журнал (config.paths.trading_dir):
  - trades_<YYYY_MM_DD>.jsonl — ПОСУТОЧНЫЙ журнал (дата = локальная дата
    ОТКРЫТИЯ позиции), одна запись JSON НА ПОЗИЦИЮ, целиком перезаписывается
    по мере смены статуса (открылась/не открылась за отведённое время/
    разрешилась по факту, с итоговым P&L без и с учётом комиссии и изменением баланса
    стратегии) — по этим файлам впоследствии можно строить график реальной
    торговли, аналогичный бэктесту в отчётах.
  - open_positions.json, pyramid_state.json, risk_state.json — небольшие
    файлы ТЕКУЩЕГО состояния (не JSONL-журнал), перечитываются при каждом
    старте бота: открытые позиции не теряются при перезапуске, счётчик
    промахов пирамиды не сбрасывается и не рвётся, накопленный риск не
    обнуляется.

Risk-менеджмент (config.price_monitor.risk + <strategy_set>.strategy_max_loss_pct):
база расчёта — РЕАЛИЗОВАННЫЙ P&L (как в бэктесте: баланс меняется только
когда позиция закрывается по факту, открытые позиции в лимит НЕ входят).
При превышении лимита — стратегия (или вся торговля) останавливается
(halted-флаг в risk_state.json, переживает перезапуск), уведомление в
Telegram отправляется всегда, звонок через CallMeBot — если соответствующий
risk_call_alert_enabled/risk.call_alert_enabled: true.
"""

import asyncio
import concurrent.futures
import json
import math
import os
import re
import threading
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from .accuracy_report import parse_bucket_range, polymarket_today
from .pyramid import pyramid_factor, validate_progression
from .watched_icaos import load_watched_icaos

GAMMA_BASE = "https://gamma-api.polymarket.com"
TEMP_LABEL_RE = re.compile(r"\d+-\d+°[CF]|\d+°[CF](?:\s+or\s+(?:below|above|higher|lower))?")

STRATEGY_SETS = ("weather_forecast", "max_bet")
STRATEGY_LABELS = {"weather_forecast": "Weather forecast", "max_bet": "Max bet"}


def _city_tag(name):
    return f"#{name.replace(' ', '_').replace('-', '_')}"


def _atomic_write_json(path, obj):
    tmp = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _load_json(path, default):
    if not os.path.isfile(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


# ---------------------------------------------------------------------- #
# Фоновый event loop + клиент размещения ордеров
# ---------------------------------------------------------------------- #

class _OrderClient:
    """
    Один фоновый поток со своим asyncio event loop на весь процесс —
    AsyncSecureClient создаётся ЛЕНИВО (при первой реальной сделке, не при
    старте бота, чтобы отсутствие POLYMARKET_* переменных/пакета `polymarket`
    не мешало остальному боту работать, пока авто-торговля выключена везде).
    Публичные методы синхронные — вызывающий код (TradingEngine) не работает
    с asyncio вообще, коду ниже НИКОГДА не требуется собственный event loop.
    """

    def __init__(self, logger):
        self.logger = logger
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._client = None
        self._client_lock = threading.Lock()

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro, timeout):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def _ensure_client(self, timeout):
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is not None:
                return self._client
            wallet = os.environ.get("POLYMARKET_WALLET_ADDRESS")
            private_key = os.environ.get("POLYMARKET_PRIVATE_KEY")
            if not wallet or not private_key:
                raise RuntimeError(
                    "POLYMARKET_WALLET_ADDRESS / POLYMARKET_PRIVATE_KEY не заданы в переменных "
                    "окружения — авто-торговля невозможна (см. README)."
                )
            from polymarket import AsyncSecureClient  # импорт здесь -- пакет нужен только при реальной торговле

            async def _create():
                return await AsyncSecureClient.create(private_key=private_key, wallet=wallet)

            self._client = self._submit(_create(), timeout)
            return self._client

    def place_limit_order(self, token_id, side, price, size, timeout=30):
        """price/size — СТРОКИ (как и у place_limit_order самого клиента)."""
        client = self._ensure_client(timeout)

        async def _place():
            return await client.place_limit_order(token_id=token_id, side=side, price=price, size=size)

        return self._submit(_place(), timeout)

    def wait_for_fill(self, response, timeout_sec):
        """True — исполнилась и рассчиталась за timeout_sec секунд; False —
        нет (заявка остаётся висеть в стакане, GTC — мы её НЕ отменяем).

        wait_for_order_fill_settlement() возвращает хэши транзакций расчёта --
        логируем их (level=info) при успехе: это подтверждение "на блокчейне",
        не зависящее от того, успеет ли позиция появиться в list_positions()
        (см. get_fill_price) -- если когда-нибудь возникнет сомнение, была ли
        сделка реальной, хэш из лога проверяется напрямую на Polygonscan."""
        client = self._ensure_client(timeout_sec)

        async def _wait():
            try:
                hashes = await asyncio.wait_for(client.wait_for_order_fill_settlement(response), timeout=timeout_sec)
                self.logger.info(f"⛓️ [Trader] Ордер рассчитан на блокчейне, транзакции: {hashes}")
                return True
            except asyncio.TimeoutError:
                return False

        try:
            return self._submit(_wait(), timeout_sec + 10)
        except concurrent.futures.TimeoutError:
            self.logger.error(
                "⚠️ [Trader] Проверка исполнения ордера не уложилась даже в увеличенный таймаут "
                "-- считаем неисполненной (заявка всё равно остаётся висеть, GTC)"
            )
            return False

    def get_order_status(self, order_id, timeout=15):
        """(status, size_matched) -- см. get_fill_price() отдельно для РЕАЛЬНОЙ
        цены исполнения: подтверждено на живом ответе (см. докстринг
        get_fill_price), что client.get_order() вообще не содержит цену
        сделки, только цену лимитки (поле "price") -- поэтому больше не
        пытаемся вытащить её отсюда."""
        client = self._ensure_client(timeout)

        async def _get():
            return await client.get_order(order_id=order_id)

        order = self._submit(_get(), timeout)
        status = _order_field(order, "status")
        size_matched = _order_field(order, "sizeMatched")
        if size_matched is None:
            size_matched = _order_field(order, "size_matched")

        # Диагностика для "vanished" (заявка отменена/не найдена, matched == 0):
        # логируем ВСЕ поля ответа get_order(), а не только status/sizeMatched --
        # пакет polymarket может отдавать причину отмены (cancel reason и т.п.)
        # в полях, которые мы иначе просто отбрасываем. Только на статусах,
        # похожих на отмену/исчезновение -- чтобы не шуметь на каждый resting-ордер.
        if status in (None, "", "NOT_FOUND", "CANCELED", "CANCELLED", "EXPIRED", "FAILED"):
            try:
                raw = order if isinstance(order, dict) else getattr(order, "__dict__", None) or repr(order)
            except Exception:
                raw = repr(order)
            self.logger.info(f"🔎 [Trader] get_order сырой ответ для order_id={order_id} (status={status}): {raw}")

        return status, size_matched

    def get_fill_price(self, token_id, timeout=20, retries=5, retry_delay_sec=2):
        """
        РЕАЛЬНАЯ цена исполнения -- ТОЛЬКО через client.list_positions(),
        НЕ через get_order(). Проверено на живом ответе get_order() после
        реального исполнения (пользовательский ad-hoc тест) -- в нём в
        принципе нет ни одного поля с ценой сделки, только "price" (цена
        лимитки заявки, ровно то, что мы и пытались перестать показывать) и
        технические поля (id/status/size_matched/...). Реальная средняя
        цена входа есть только в Position (list_positions), поле avg_price
        -- см. официальный пример
        https://docs.polymarket.com/trading/wallet-activity.

        Между реальным исполнением и появлением позиции в list_positions
        бывает небольшая задержка -- НА ПРАКТИКЕ подтверждено (без ретраев,
        одна попытка сразу после wait_for_fill(), в проде) уведомление
        стабильно падало на цену лимитки: позиция просто ещё не успевала
        проиндексироваться к этому моменту (в одном случае даже сам запрос
        list_positions() отвалился по таймауту -- см. warning в логе). Поэтому,
        если позиция не нашлась с первой попытки, повторяем ещё retries-1 раз
        с паузой retry_delay_sec (по умолчанию 5 попыток по 2 сек -- максимум
        +8 сек сверх основного времени ожидания исполнения, не путать с самим
        wait_for_fill(): та пауза про ожидание ИСПОЛНЕНИЯ заявки, эта -- про
        появление УЖЕ исполненной позиции в отдельном списке).

        Возвращает float или None (позиция так и не нашлась за все попытки
        -- вызывающий код в этом случае падает обратно на цену лимитки, с
        явным warning в лог, а не молча).
        """
        client = self._ensure_client(timeout)
        wallet = os.environ.get("POLYMARKET_WALLET_ADDRESS")

        async def _find_once():
            async for page in client.list_positions(user=wallet, page_size=100):
                for item in page.items:
                    item_token = _order_field(item, "token_id") or _order_field(item, "asset_id")
                    if item_token == token_id:
                        raw = _order_field(item, "avg_price")
                        try:
                            price = float(raw) if raw not in (None, "") else None
                        except (TypeError, ValueError):
                            price = None
                        # avg_price == 0 -- позиция уже видна в list_positions, но ещё
                        # не проиндексирована полностью (та же задержка, что описана в
                        # докстринге выше), а не реальная нулевая цена исполнения --
                        # ставки на Polymarket никогда не исполняются по 0. Считаем это
                        # "ещё не готово" и повторяем попытку, а не показываем 0¢ как
                        # реальную цену.
                        return price if price else None
            return None

        async def _get_with_retries():
            for attempt in range(1, retries + 1):
                value = await _find_once()
                if value is not None:
                    return value
                if attempt < retries:
                    await asyncio.sleep(retry_delay_sec)
            return None

        try:
            return self._submit(_get_with_retries(), timeout + retries * retry_delay_sec)
        except Exception as e:
            self.logger.warning(f"⚠️ [Trader] Не удалось получить реальную цену открытия из list_positions -- {e}")
            return None

    def get_position_info(self, token_id, timeout=20, retries=1, retry_delay_sec=2):
        """
        (size, avg_price) реальной позиции из list_positions -- ТОТ ЖЕ источник,
        что и get_fill_price(), но дополнительно с размером (current_size/
        total_size), а не только ценой. Нужен как fallback-проверка в _verify_fill,
        когда get_order_status() падает с ошибкой (см. её докстринг) -- позиция
        могла реально исполниться, даже если статус САМОГО ордера больше недоступен
        (например, "OpenOrder response did not match expected shape" -- то же самое
        семейство проблем, что и "пропавший ордер" в _reconcile, только
        обнаруженное здесь, при разрешении, для позиции, открытой ДО появления
        механизма реконсиляции в on_signal).

        retries=1 по умолчанию (не 5, как у get_fill_price) -- это fallback ПОСЛЕ
        уже провалившейся проверки статуса ордера, не критичная по времени часть
        основного пути открытия позиции; вызывающий код (_verify_fill) сам решает,
        сколько ДНЕЙ подряд повторять попытку.

        Возвращает (None, None), если позиция не нашлась ни разу или сам запрос
        не удался (тогда исключение НЕ пробрасывается -- вызывающий код трактует
        это как "не нашлась", то есть безопасно консервативно).
        """
        client = self._ensure_client(timeout)
        wallet = os.environ.get("POLYMARKET_WALLET_ADDRESS")

        async def _find_once():
            async for page in client.list_positions(user=wallet, page_size=100):
                for item in page.items:
                    item_token = _order_field(item, "token_id") or _order_field(item, "asset_id")
                    if item_token == token_id:
                        size = _order_field(item, "current_size")
                        if size is None:
                            size = _order_field(item, "total_size")
                        return size, _order_field(item, "avg_price")
            return None, None

        async def _get_with_retries():
            for attempt in range(1, retries + 1):
                size, price = await _find_once()
                if size not in (None, "", 0, 0.0):
                    return size, price
                if attempt < retries:
                    await asyncio.sleep(retry_delay_sec)
            return None, None

        try:
            size, price = self._submit(_get_with_retries(), timeout + retries * retry_delay_sec)
        except Exception as e:
            self.logger.warning(f"⚠️ [Trader] Не удалось проверить реальную позицию в list_positions -- {e}")
            return None, None
        try:
            size = float(size) if size not in (None, "") else None
        except (TypeError, ValueError):
            size = None
        try:
            price = float(price) if price not in (None, "") else None
        except (TypeError, ValueError):
            price = None
        return size, price


def _order_field(order, key):
    return order.get(key) if isinstance(order, dict) else getattr(order, key, None)


def _cents_str(price_dollars):
    """0.66 -> "66.0¢" -- тот же формат, что и в сигналах (price_monitor.py)."""
    return f"{price_dollars * 100:.1f}¢"


# ---------------------------------------------------------------------- #
# Резолв токена бакета через Gamma API (по номеру бакета, не по тексту лейбла)
# ---------------------------------------------------------------------- #

class _GammaResolver:
    def __init__(self, cities, logger, bucket_width):
        self.cities = cities
        self.logger = logger
        self.bucket_width = bucket_width
        self._cache = {}  # (icao, date_str) -> [{"label","yes_token","no_token"}, ...]

    @staticmethod
    def _extract_label(question):
        m = TEMP_LABEL_RE.search(question or "")
        return m.group(0) if m else None

    def _bucket_index(self, value, scale):
        return round(value) // self.bucket_width[scale]

    def markets_for(self, icao, now_local):
        date_str = now_local.strftime("%Y-%m-%d")
        cache_key = (icao, date_str)
        if cache_key in self._cache:
            return self._cache[cache_key]
        slug = self.cities[icao]["slug"]
        month, day, year = now_local.strftime("%B").lower(), now_local.day, now_local.year
        event_slug = f"highest-temperature-in-{slug}-on-{month}-{day}-{year}"
        try:
            resp = requests.get(f"{GAMMA_BASE}/events", params={"slug": event_slug}, timeout=15)
            resp.raise_for_status()
            events = resp.json()
        except Exception as e:
            self.logger.error(f"❌ [Trader] Gamma API ошибка {icao}: {e}")
            return []
        if not events:
            self._cache[cache_key] = []
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
            label = self._extract_label(question) or market.get("groupItemTitle") or question
            markets.append({"label": label, "yes_token": clob_ids[0], "no_token": clob_ids[1]})
        self._cache[cache_key] = markets
        return markets

    def resolve_token_for_bucket(self, icao, now_local, target_bucket, scale):
        """
        Находит рынок (бакет), диапазон которого покрывает target_bucket —
        та же логика сопоставления, что PriceMonitor._match_bet_by_bucket, но
        по НОМЕРУ БАКЕТА (не по цене) — нам нужен именно token_id этого
        бакета, а не его текущая цена.
        """
        for m in self.markets_for(icao, now_local):
            low, high = parse_bucket_range(m["label"])
            if low is None:
                continue
            low_bucket = None if low == float("-inf") else self._bucket_index(low, scale)
            high_bucket = None if high == float("inf") else self._bucket_index(high, scale)
            lo_ok = (low_bucket is None) or (target_bucket >= low_bucket)
            hi_ok = (high_bucket is None) or (target_bucket <= high_bucket)
            if lo_ok and hi_ok:
                return m
        return None


# ---------------------------------------------------------------------- #
# Журнал/состояние
# ---------------------------------------------------------------------- #

class PositionStore:
    def __init__(self, trading_dir, logger):
        self.trading_dir = trading_dir
        self.logger = logger
        os.makedirs(trading_dir, exist_ok=True)
        self.open_positions_path = os.path.join(trading_dir, "open_positions.json")
        self.pyramid_state_path = os.path.join(trading_dir, "pyramid_state.json")
        self.risk_state_path = os.path.join(trading_dir, "risk_state.json")
        self.deferred_notifications_path = os.path.join(trading_dir, "deferred_notifications.json")

        self.open_positions = _load_json(self.open_positions_path, {})    # {position_id: {...}}
        self.pyramid_state = _load_json(self.pyramid_state_path, {})       # {"strategy_set|icao": misses}
        self.risk_state = _load_json(self.risk_state_path, {})             # {"strategy_set": {"realized_pnl","halted"}, "_total": {...}}
        # Уведомления "Позиция закрыта"/"Заявка не исполнилась" по позициям, разрешённым
        # ДО ночного резолва (синхронно перед новым сигналом, см. _try_resolve_now) --
        # откладываются сюда и уходят в чат одним пакетом при ближайшем resolve_positions().
        # [{"position_id": ..., "text": ...}, ...]; хранится на диске, чтобы не потеряться
        # при перезапуске бота.
        self.deferred_notifications = _load_json(self.deferred_notifications_path, [])
        if not isinstance(self.deferred_notifications, list):
            self.deferred_notifications = []

    def _day_path(self, date_str):
        return os.path.join(self.trading_dir, f"trades_{date_str.replace('-', '_')}.jsonl")

    def append_or_update_day_record(self, date_str, record):
        """Перечитывает/перезаписывает ЦЕЛИКОМ файл суток date_str, заменяя
        запись с тем же position_id (или добавляя новую, если её ещё нет).
        Позиций в сутки немного (десятки максимум) — перезапись безопасна и
        дёшева; os.replace — атомарная подмена файла."""
        path = self._day_path(date_str)
        records = []
        found = False
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    if rec.get("position_id") == record["position_id"]:
                        records.append(record)
                        found = True
                    else:
                        records.append(rec)
        if not found:
            records.append(record)
        tmp = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}"
        with open(tmp, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.replace(tmp, path)

    def save_open_positions(self):
        _atomic_write_json(self.open_positions_path, self.open_positions)

    def save_pyramid_state(self):
        _atomic_write_json(self.pyramid_state_path, self.pyramid_state)

    def save_deferred_notifications(self):
        _atomic_write_json(self.deferred_notifications_path, self.deferred_notifications)

    def save_risk_state(self):
        _atomic_write_json(self.risk_state_path, self.risk_state)

    @staticmethod
    def _pyramid_key(strategy_set, icao):
        return f"{strategy_set}|{icao}"

    def get_misses(self, strategy_set, icao):
        return int(self.pyramid_state.get(self._pyramid_key(strategy_set, icao), 0))

    def set_misses(self, strategy_set, icao, value):
        self.pyramid_state[self._pyramid_key(strategy_set, icao)] = value
        self.save_pyramid_state()

    # --- Состояние серии для режимов пирамиды recover / recover_min_multiplier ---
    # Хранится в том же pyramid_state.json, но под отдельным ключом
    # "<strategy_set>|<icao>|series" (счётчик промахов остаётся под прежним
    # ключом "<strategy_set>|<icao>" -- формат файла обратно совместим):
    #   {"loss": накопленный чистый убыток текущей серии в $ (с комиссией),
    #    "last_lot": лот последней ставки серии}
    # None -- серии нет (после хита/сброса по loss_limit) либо состояние
    # записано старой версией бота (тогда при misses > 0 лот считается
    # удвоением, см. TradingEngine._compute_lot).

    def get_series(self, strategy_set, icao):
        value = self.pyramid_state.get(self._pyramid_key(strategy_set, icao) + "|series")
        return value if isinstance(value, dict) else None

    def set_series(self, strategy_set, icao, loss, last_lot):
        self.pyramid_state[self._pyramid_key(strategy_set, icao) + "|series"] = {
            "loss": round(float(loss), 4), "last_lot": float(last_lot),
        }
        self.save_pyramid_state()

    def clear_series(self, strategy_set, icao):
        if self.pyramid_state.pop(self._pyramid_key(strategy_set, icao) + "|series", None) is not None:
            self.save_pyramid_state()

    def get_realized_pnl(self, strategy_set):
        return float(self.risk_state.get(strategy_set, {}).get("realized_pnl", 0.0))

    def get_total_realized_pnl(self):
        return float(self.risk_state.get("_total", {}).get("realized_pnl", 0.0))

    def is_halted(self, strategy_set):
        return bool(self.risk_state.get(strategy_set, {}).get("halted", False))

    def is_totally_halted(self):
        return bool(self.risk_state.get("_total", {}).get("halted", False))

    def set_halted(self, strategy_set):
        self.risk_state.setdefault(strategy_set, {"realized_pnl": 0.0})["halted"] = True
        self.save_risk_state()

    def set_totally_halted(self):
        self.risk_state.setdefault("_total", {"realized_pnl": 0.0})["halted"] = True
        self.save_risk_state()

    def add_realized_pnl(self, strategy_set, pnl):
        s = self.risk_state.setdefault(strategy_set, {"realized_pnl": 0.0, "halted": False})
        s["realized_pnl"] = float(s.get("realized_pnl", 0.0)) + pnl
        t = self.risk_state.setdefault("_total", {"realized_pnl": 0.0, "halted": False})
        t["realized_pnl"] = float(t.get("realized_pnl", 0.0)) + pnl
        self.save_risk_state()


# ---------------------------------------------------------------------- #
# TradingEngine
# ---------------------------------------------------------------------- #

class TradingEngine:
    def __init__(
        self, config, cities, telegram, logger, watched=None, watched_max_bet=None, order_client=None,
        actuals_fetcher=None,
    ):
        self.config = config
        self.cities = cities
        self.telegram = telegram
        self.logger = logger
        # Живой, синхронный запрос факта Polymarket ПО ОДНОЙ (icao, date) паре
        # -- callable(icao, date_str) -> (bucket_label, scale) | None, обычно
        # PolymarketActualsMiner.fetch_and_store_single (см. Orchestrator._
        # fetch_actuals_fact_now). Используется ТОЛЬКО в _try_resolve_now(),
        # чтобы перед КАЖДЫМ новым сигналом проверить, не резолвилась ли уже
        # предыдущая позиция по этому городу -- даже если фоновый майнер
        # ещё не успел это закэшировать. None (по умолчанию, в т.ч. в тестах)
        # -- просто пропускаем живой запрос и полагаемся на локальный кэш.
        self.actuals_fetcher = actuals_fetcher

        pm = config.price_monitor
        self.bucket_width = {"F": config.accuracy_report.bucket_width.F, "C": config.accuracy_report.bucket_width.C}
        self.commission_per_share = getattr(pm, "commission_per_share", 0.012)
        self.min_order_usd = float(getattr(pm, "min_order_usd", 1.0))  # минимум суммы ордера на Polymarket, $
        # Минимальная цена ставки (¢): дешевле -- позиция не открывается; от неё и выше лот при необходимости
        # поднимается до min_order_usd (см. _compute_lot).
        self.min_bet_price_cents = float(getattr(pm, "min_bet_price_cents", 5))
        self.account_balance = getattr(pm, "account_balance", 100.0)
        self.fill_check_wait_sec = getattr(pm, "fill_check_wait_sec", 20)
        self.daily_summary_enabled = getattr(pm, "daily_summary_enabled", True)
        # Сколько раз ПОДРЯД (на разных вызовах resolve_positions -- т.е. дней) можно
        # не суметь подтвердить исполнение ордера (get_order_status падает с ошибкой),
        # прежде чем сдаться и закрыть позицию без данных, а не откладывать разрешение
        # бесконечно (см. _verify_fill/resolve_positions -- иначе позиция с "битым"
        # order_id виснет в open_positions.json навсегда).
        self.verify_max_attempts = getattr(pm, "verify_max_attempts", 3)
        # Проверка статуса ордера сразу после отправки (_reconcile): сколько раз подряд читаем
        # get_order_status и пауза между попытками, прежде чем перейти к запасной проверке
        # позиции в list_positions.
        self.status_check_attempts = max(1, int(getattr(pm, "status_check_attempts", 3)))
        self.status_check_delay_sec = float(getattr(pm, "status_check_delay_sec", 2))

        risk = getattr(pm, "risk", None)
        self.total_max_loss_pct = getattr(risk, "total_max_loss_pct", 30)
        self.risk_call_alert_enabled = getattr(risk, "call_alert_enabled", False)
        self.risk_call_user = getattr(risk, "call_user", None)
        self.risk_call_max_attempts = getattr(risk, "call_max_attempts", 3)
        self.risk_call_retry_delay_sec = getattr(risk, "call_retry_delay_sec", 3)

        watched_by_set = {
            "weather_forecast": watched if watched is not None else load_watched_icaos(
                config.resolve_config_path(pm.weather_forecast.watched_icaos_file),
                loss_limit=getattr(pm.weather_forecast, "loss_limit", None),
            ),
            "max_bet": watched_max_bet if watched_max_bet is not None else load_watched_icaos(
                config.resolve_config_path(pm.max_bet.watched_icaos_file),
                loss_limit=getattr(pm.max_bet, "loss_limit", None),
            ),
        }

        self.strategy_cfg = {}
        for strategy_set in STRATEGY_SETS:
            scfg = getattr(pm, strategy_set)
            test_cities_limit = getattr(scfg, "test_cities_limit", 2)
            eligible_icaos = set(list(watched_by_set[strategy_set].keys())[:test_cities_limit])
            self.strategy_cfg[strategy_set] = {
                "auto_trading": getattr(scfg, "auto_trading", False),
                "lot_size": getattr(scfg, "lot_size", 5.0),
                "pyramid_mode": self._validate_pyramid_mode(strategy_set, getattr(scfg, "pyramid_mode", "double")),
                # Множитель пирамиды: в "double" лот = lot_size * множитель^промахов; в
                # "recover_min_multiplier" -- минимум роста относительно предыдущего лота.
                # Старое имя pyramid_min_multiplier читается как запасное (обратная совместимость).
                "pyramid_progression": self._validate_progression(strategy_set, scfg)[0],
                "pyramid_custom_steps": self._validate_progression(strategy_set, scfg)[1],
                "pyramid_multiplier": self._validate_min_multiplier(
                    strategy_set,
                    getattr(scfg, "pyramid_multiplier", getattr(scfg, "pyramid_min_multiplier", 2)),
                ),
                "loss_limit": getattr(scfg, "loss_limit", None),
                "pyramid_target_profit_price": float(getattr(scfg, "pyramid_target_profit_price", 0.5)),
                "pyramid_max_lot": getattr(scfg, "pyramid_max_lot", None),
                "test_cities_limit": test_cities_limit,
                "eligible_icaos": eligible_icaos,
                "strategy_max_loss_pct": getattr(scfg, "strategy_max_loss_pct", 25),
                "risk_call_alert_enabled": getattr(scfg, "risk_call_alert_enabled", False),
                "risk_call_user": getattr(scfg, "call_user", None),
                "risk_call_max_attempts": getattr(scfg, "call_max_attempts", 3),
                "risk_call_retry_delay_sec": getattr(scfg, "call_retry_delay_sec", 3),
            }

        # Полный набор ICAO, реально настроенных сейчас (НЕ закомментированных)
        # хоть в одном из двух конфигов стратегий -- в отличие от
        # strategy_cfg[...]["eligible_icaos"] выше (тот урезан
        # test_cities_limit, это про то, что-то она вообще есть в конфиге).
        # Используется ТОЛЬКО в send_daily_summary ниже, чтобы не показывать в
        # сводке города, по которым были сделки в прошлом, но которые с тех
        # пор убрали (закомментировали) из watched_icaos_*.yaml.
        self.all_watched_icaos = set(watched_by_set["weather_forecast"].keys()) | set(watched_by_set["max_bet"].keys())

        trading_dir = getattr(config.paths, "trading_dir", os.path.join(config.paths.data_dir, "trading"))
        self.polymarket_gamma_dir = config.paths.polymarket_gamma_dir
        self.store = PositionStore(trading_dir, self.logger)
        self.resolver = _GammaResolver(cities, self.logger, self.bucket_width)
        # ЛЕНИВО: _OrderClient сам по себе (даже до первой реальной сделки)
        # запускает ФОНОВЫЙ ПОТОК со своим asyncio event loop (run_forever) --
        # если создавать его прямо здесь, этот поток стартует ПРИ КАЖДОМ
        # запуске бота (пока price_monitor.enabled: true, независимо от
        # auto_trading), ЕЩЁ ДО того, как WeatherMiner впервые подключится к
        # Chrome (TradingEngine строится раньше WeatherMiner в Orchestrator).
        # Это лишний фоновый поток, работающий всё время впустую, если
        # auto_trading не используется -- и лишняя переменная в истории
        # подключения к Chrome, если что-то в самом Selenium чувствительно к
        # тому, что творится в других потоках процесса на старте. Поэтому
        # клиент создаётся ТОЛЬКО при первом реальном обращении к нему (см.
        # _get_order_client) -- если ни одна стратегия не торгует, поток
        # вообще никогда не появляется.
        self.order_client = order_client
        self._order_client_lock = threading.Lock()

        if self.store.open_positions:
            self.logger.info(f"📂 Загружено {len(self.store.open_positions)} открытых позиций из 1 файла trading")

    def _get_order_client(self):
        """Лениво создаёт _OrderClient (и его фоновый поток) при первом
        реальном обращении -- см. пояснение в __init__. order_client,
        переданный явно в конструктор (тесты, FakeOrderClient), никогда не
        подменяется -- лениво создаётся только "настоящий" _OrderClient."""
        if self.order_client is None:
            with self._order_client_lock:
                if self.order_client is None:
                    self.order_client = _OrderClient(self.logger)
        return self.order_client

    # ------------------------------------------------------------------ #
    # Открытие позиции по сигналу
    # ------------------------------------------------------------------ #

    def is_auto_trading_enabled(self, strategy_set):
        return bool(self.strategy_cfg[strategy_set]["auto_trading"])

    def _reconcile(self, order_id, token_id, lot):
        """
        Проверяет РЕАЛЬНОЕ состояние после ожидания исполнения -- не
        доверяя одному только wait_for_fill() (см. докстринг модуля).

        Источник истины про РАЗМЕР исполнения -- ТОЛЬКО get_order_status()
        (size_matched), а не сам факт, что позиция где-то "нашлась" --
        нашедшаяся позиция подтверждает исполнение хоть НА СКОЛЬКО-ТО, но
        НЕ обязательно на весь заказанный lot (частичное исполнение --
        например, цена ушла от лимита ПОСЛЕ того, как исполнилась только
        часть заявки, и остаток так и остался висеть). Раньше "позиция
        нашлась" сразу считалось "filled" на ВЕСЬ lot -- это была ошибка:
        record["order_status"] помечался "filled", из-за чего _verify_fill
        (resolve_positions) КОРОТКИМ ПУТЁМ доверял record["lot"] как есть,
        не перепроверяя фактический matched -- реальный частично
        исполненный остаток посчитался бы как полный.

        Возвращает (state, detail):
          - ("filled", fill_price)                     -- matched >= lot целиком, цена реальная
          - ("partial", (size_matched, fill_price, status)) -- matched > 0, но < lot -- остаток
            висит (или уже закрылся) частично неисполненным; актуальный
            matched будет заново подтверждён позже в _verify_fill
          - ("resting", (status, size_matched))         -- matched == 0, ордер подтверждён живым в стакане
          - ("vanished", (status, size_matched))        -- ПОДТВЕРЖДЕНО: ни ордера, ни совпадений (matched == 0)
          - ("unknown", reason)                         -- проверка статуса ордера не удалась

        "vanished"/"resting"/"partial" различаются ТОЛЬКО по get_order_status
        -- она проверяется ПЕРВОЙ и её ошибка сразу даёт "unknown" (заявка
        НЕ переразмещается -- единственный случай для переразмещения это
        позитивное подтверждение отсутствия ОБОИМИ способами, чтобы сбой
        самой проверки никогда не привёл к повторному ордеру и задвоению
        лота). list_positions (get_fill_price) опрашивается уже ВТОРЫМ
        шагом и только если matched > 0 -- нужна только для реальной цены.
        """
        # Статус ордера читаем несколько раз подряд с короткой паузой -- разовый сетевой
        # сбой/таймаут не должен сразу давать "не удалось проверить". Детерминированные
        # ошибки (например, "response did not match expected shape") повторами не лечатся --
        # для них ниже запасной путь через list_positions.
        status = size_matched = None
        last_error = None
        for attempt in range(1, self.status_check_attempts + 1):
            try:
                status, size_matched = self._get_order_client().get_order_status(order_id)
                last_error = None
                break
            except Exception as e:
                last_error = e
                self.logger.warning(
                    f"⚠️ [Trader] Проверка статуса ордера {order_id}: попытка {attempt}/{self.status_check_attempts} "
                    f"не удалась -- {e}"
                )
                if attempt < self.status_check_attempts:
                    time.sleep(self.status_check_delay_sec)

        if last_error is not None:
            # Статус ордера прочитать не удаётся -- пробуем независимый источник: реальную
            # позицию в list_positions. Нашлась -- сделка состоялась (как и в _verify_fill).
            # ЕСЛИ НЕ нашлась -- по-прежнему "unknown" (НЕ "vanished": отсутствие позиции при
            # неработающем статусе ничего не доказывает, переразмещать заявку нельзя).
            try:
                size_found, price_found = self._get_order_client().get_position_info(token_id)
            except Exception as e:
                self.logger.warning(f"⚠️ [Trader] Запасная проверка позиции в list_positions не удалась -- {e}")
                size_found, price_found = None, None
            if size_found not in (None, 0, 0.0):
                self.logger.warning(
                    f"⚠️ [Trader] Статус ордера {order_id} не читается ({last_error}), но позиция найдена в "
                    f"list_positions: {size_found} по {price_found} -- считаю ордер исполненным"
                )
                try:
                    size_found_f = float(size_found)
                except (TypeError, ValueError):
                    size_found_f = 0.0
                if size_found_f >= lot - 1e-9:
                    return "filled", price_found
                return "partial", (size_found_f, price_found, "status_unreadable")
            return "unknown", str(last_error)

        try:
            size_matched_f = float(size_matched) if size_matched not in (None, "") else 0.0
        except (TypeError, ValueError):
            size_matched_f = 0.0

        order_exists = status not in (None, "", "NOT_FOUND", "CANCELED", "CANCELLED", "EXPIRED", "FAILED")

        if size_matched_f <= 0:
            if order_exists:
                return "resting", (status, size_matched)
            return "vanished", (status, size_matched)

        try:
            fill_price = self._get_order_client().get_fill_price(token_id)
        except Exception as e:
            fill_price = None
            self.logger.warning(f"⚠️ [Trader] Ошибка проверки реальной цены в list_positions -- {e}")

        if size_matched_f >= lot - 1e-9:
            return "filled", fill_price
        return "partial", (size_matched_f, fill_price, status)

    PYRAMID_MODES = ("double", "recover", "recover_min_multiplier")

    @staticmethod
    def _validate_pyramid_mode(strategy_set, mode):
        mode = str(mode or "double").strip().lower()
        if mode not in TradingEngine.PYRAMID_MODES:
            raise ValueError(
                f"price_monitor.{strategy_set}.pyramid_mode='{mode}' -- допустимо только одно из "
                f"{', '.join(TradingEngine.PYRAMID_MODES)}"
            )
        return mode

    @staticmethod
    def _validate_progression(strategy_set, scfg):
        return validate_progression(
            getattr(scfg, "pyramid_progression", "power"), getattr(scfg, "pyramid_custom_steps", None),
            where=f"price_monitor.{strategy_set}",
        )

    @staticmethod
    def _validate_min_multiplier(strategy_set, value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = 0.0
        if value < 1.0:
            raise ValueError(
                f"price_monitor.{strategy_set}.pyramid_multiplier должен быть числом >= 1 (получено {value!r})"
            )
        return value

    def _compute_lot(self, strategy_set, icao, strategy_type, misses, price_usd):
        """
        Итоговый лот новой позиции: лот по выбранному режиму (_compute_pyramid_lot)
        с двумя правилами по цене сигнала:
          - цена < min_bet_price_cents (по умолчанию 5¢) -- ставка слишком дешёвая:
            позиция НЕ открывается (на ставке за 0.1¢ пол ниже давал 1000 акций и
            лимит-залог в сотни $). Возвращает (None, причина);
          - иначе пол по минимальной сумме ордера Polymarket (min_order_usd, $1): лот *
            цена не может быть меньше min_order_usd, поэтому лот не меньше
            min_order_usd / цена (округляется вверх до 0.01 акции). Пол действует для
            ВСЕХ стратегий и режимов, включая первую ставку серии, и применяется после
            pyramid_max_lot -- лимит биржи важнее.
        Возвращает (lot, note); lot is None -- ставку пропускаем, note -- причина.
        """
        if price_usd is not None and price_usd * 100.0 < self.min_bet_price_cents - 1e-9:
            return None, (
                f"ставка {price_usd * 100:g}¢ меньше минимальной цены {self.min_bet_price_cents:g}¢ "
                f"(price_monitor.min_bet_price_cents)"
            )
        lot, note = self._compute_pyramid_lot(strategy_set, icao, strategy_type, misses, price_usd)
        if price_usd and price_usd > 0 and self.min_order_usd > 0:
            floor_lot = math.ceil(self.min_order_usd / price_usd * 100 - 1e-9) / 100.0
            if lot < floor_lot:
                note += f", лот поднят с {lot:g} до {floor_lot:g}: сумма ордера не меньше {self.min_order_usd:g}$"
                lot = floor_lot
        return lot, note

    def _compute_pyramid_lot(self, strategy_set, icao, strategy_type, misses, price_usd):
        """
        Лот по режиму пирамиды (без пола по минимальной сумме ордера, см.
        _compute_lot). Возвращает (lot, note) -- note для лога.

        flat, либо первая ставка серии (misses == 0) -- всегда lot_size.
        pyramid, misses > 0, режим (price_monitor.<набор>.pyramid_mode):
          - "double":       lot_size * pyramid_factor(misses) -- прогрессия pyramid_progression (power: m^k, cumulative: 1+m+..+m^k, custom: список), см. pyramid.py;
          - "recover":      (накопленный убыток серии + цель) / (1 - цена - комиссия),
                            цель = lot_size * pyramid_target_profit_price (5 * 0.5 = 2.5$) --
                            выигрыш этого лота закрывает убытки серии и приносит цель
                            (чистыми, с комиссией). Не меньше lot_size;
          - "recover_min_multiplier": то же, но не меньше (последний лот серии) * pyramid_multiplier.
        price_usd -- цена, по которой считаем прибыль с акции: цена сигнала, если
        PriceMonitor её передал, иначе цена лимитного ордера. Лот округляется ВВЕРХ до
        0.01 акции (размер ордера Polymarket) и ограничивается pyramid_max_lot (если задан).
        Нет сохранённого состояния серии (счётчик > 0, но состояние записано старой
        версией бота) -- безопасный откат на удвоение с предупреждением в лог.
        """
        scfg = self.strategy_cfg[strategy_set]
        lot_size = scfg["lot_size"]
        if strategy_type != "pyramid" or misses <= 0:
            return lot_size, ""

        mult = scfg["pyramid_multiplier"]
        factor = pyramid_factor(misses, scfg["pyramid_progression"], mult, scfg["pyramid_custom_steps"])
        doubled = math.ceil(lot_size * factor * 100 - 1e-9) / 100.0
        mode = scfg["pyramid_mode"]
        if mode == "double":
            return doubled, ""

        series = self.store.get_series(strategy_set, icao)
        if series is None:
            self.logger.warning(
                f"⚠️ [Trader/{strategy_set}] {icao}: нет сохранённого состояния серии (misses={misses}) -- "
                f"режим {mode} временно считает лот как double ({doubled})"
            )
            return doubled, f", режим {mode}: нет состояния серии -> прогрессия {scfg['pyramid_progression']}"

        loss = float(series.get("loss", 0.0))
        last_lot = float(series.get("last_lot") or lot_size)
        target = lot_size * scfg["pyramid_target_profit_price"]
        per_share = 1.0 - price_usd - self.commission_per_share
        if per_share <= 0:
            lot = max(lot_size, last_lot * mult) if mode == "recover_min_multiplier" else lot_size
            self.logger.warning(
                f"⚠️ [Trader/{strategy_set}] {icao}: цена {price_usd}$ слишком высока для расчёта отыгрыша "
                f"(прибыль с акции <= 0) -- лот {lot}"
            )
        else:
            lot = max((loss + target) / per_share, lot_size)
            if mode == "recover_min_multiplier":
                lot = max(lot, last_lot * mult)
        lot = math.ceil(lot * 100 - 1e-9) / 100.0
        cap = scfg["pyramid_max_lot"]
        capped = False
        if cap and lot > float(cap):
            lot, capped = float(cap), True
        mult_note = f" (минимум x{mult:g})" if mode == "recover_min_multiplier" else ""
        note = (f", режим {mode}{mult_note}: убыток серии {loss:.2f}$, цель {target:.2f}$, цена {price_usd}$, "
                f"предыдущий лот {last_lot:g}" + (f", ограничен pyramid_max_lot={cap}" if capped else ""))
        return lot, note

    def on_signal(self, strategy_set, icao, info, watch_cfg, target_bucket, scale, now_local=None,
                  signal_price_cents=None):
        scfg = self.strategy_cfg[strategy_set]

        if icao not in scfg["eligible_icaos"]:
            self.logger.info(
                f"⏭️ [Trader/{strategy_set}] {icao}: вне test_cities_limit ({scfg['test_cities_limit']}) "
                f"-- сигнал отправлен, ботом не торгуется"
            )
            return

        if self.store.is_totally_halted():
            self.logger.warning(f"⛔ [Trader] Вся торговля остановлена риск-менеджментом -- {icao} пропущен")
            return
        if self.store.is_halted(strategy_set):
            self.logger.warning(f"⛔ [Trader/{strategy_set}] Стратегия остановлена риск-менеджментом -- {icao} пропущен")
            return

        now_local = now_local or datetime.now(ZoneInfo(info["tz"]))
        date_str = now_local.strftime("%Y-%m-%d")

        strategy_type = watch_cfg["strategy"]

        # Защита от задвоения позиции (второй, независимый слой -- первый см.
        # PriceMonitor._checked_today): если по этому же (strategy_set, icao)
        # уже есть открытая позиция, открытая СЕГОДНЯ (opened_date == date_str,
        # по местному времени города) -- новая не открывается, каким бы путём
        # сигнал сюда ни пришёл. За сегодняшние сутки её всё равно рано
        # резолвить (рынок на Polymarket за ещё не завершившийся день не мог
        # закрыться), поэтому _try_resolve_now ниже даже не вызываем -- сразу
        # отбой одним сообщением. Проверка стоит ДО дорогого
        # resolve_token_for_bucket (сеть, Gamma API), чтобы не тратить лишний
        # запрос на сигнал, который всё равно будет отклонён.
        existing = self._find_open_position(strategy_set, icao)
        if existing is not None and existing.get("opened_date") == date_str:
            self.logger.warning(
                f"⛔ [Trader/{strategy_set}] {icao} {date_str}: сегодня уже есть открытая нерезолвленная "
                f"позиция (position_id={existing.get('position_id')}) -- новая НЕ открывается (защита от задвоения)"
            )
            return

        if strategy_type == "pyramid":
            # ПЕРЕД тем как считать лот по счётчику пирамиды -- последняя
            # попытка разрешить позицию по этому городу с ПРЕДЫДУЩИХ суток
            # (если такая всё ещё висит открытой) ПРЯМО СЕЙЧАС (см.
            # _try_resolve_now): если рынок на Polymarket уже резолвился,
            # счётчик пирамиды будет обновлён немедленно, а не только на
            # следующем фоновом цикле -- новый лот всегда считается по
            # максимально свежему факту, а не по устаревшему кэшу. Если
            # резолвить пока нечем (рынок ещё не закрылся) -- позиция так и
            # останется открытой, но НЕ блокирует сегодняшнюю сделку (см.
            # проверку above -- та смотрит только на сегодняшние сутки).
            self._try_resolve_now(strategy_set, icao)

        market = self.resolver.resolve_token_for_bucket(icao, now_local, target_bucket, scale)
        if market is None:
            self.logger.error(
                f"❌ [Trader/{strategy_set}] {icao}: не нашёл токен бакета (target_bucket={target_bucket}) "
                f"через Gamma API -- позиция НЕ открыта"
            )
            return

        poly_url, _poly_label = polymarket_today(icao, self.cities)
        poly_link = f"<a href='{poly_url}'>Polymarket</a>" if poly_url else "Polymarket"

        loss_limit = watch_cfg.get("loss_limit") or scfg.get("loss_limit") or 2
        lot_size = scfg["lot_size"]

        misses = self.store.get_misses(strategy_set, icao) if strategy_type == "pyramid" else 0

        order_price_cents = watch_cfg.get("max_price_limit")
        if order_price_cents is None:
            self.logger.error(f"❌ [Trader/{strategy_set}] {icao}: max_price_limit не задан -- позиция НЕ открыта")
            return
        order_price = round(order_price_cents / 100.0, 4)

        # Цена для расчёта лота (режимы recover/recover_min_multiplier) -- цена сигнала (реальный ask),
        # а не лимит ордера (max_price_limit -- это худший случай): ордер исполнится по рынку
        # не хуже лимита, и бэктест считает лот ровно по цене ставки.
        sizing_price = round(signal_price_cents / 100.0, 4) if signal_price_cents is not None else order_price
        lot, lot_note = self._compute_lot(strategy_set, icao, strategy_type, misses, sizing_price)
        if lot is None:
            self.logger.warning(f"⛔ [Trader/{strategy_set}] {icao}: позиция НЕ открыта -- {lot_note}")
            self._notify(
                f"⛔ <b>Позиция не открыта</b> {_city_tag(info.get('name', icao))} "
                f"({STRATEGY_LABELS.get(strategy_set, strategy_set)})\n"
                f"<b>{market['label']}</b>: {lot_note}"
            )
            return

        position_id = uuid.uuid4().hex
        strategy_label = STRATEGY_LABELS.get(strategy_set, strategy_set)
        city_tag = _city_tag(info.get("name", icao))
        wait_sec = self.fill_check_wait_sec
        max_attempts = 3

        record = {
            "position_id": position_id,
            "opened_date": date_str,
            "opened_at": now_local.isoformat(),
            "icao": icao,
            "city": info.get("name", icao),
            "strategy_set": strategy_set,
            "strategy_type": strategy_type,
            "loss_limit": loss_limit,
            "misses_before": misses,
            "pyramid_mode": scfg["pyramid_mode"] if strategy_type == "pyramid" else None,
            "bet_label": market["label"],
            "token_id": market["yes_token"],
            "target_bucket": target_bucket,
            "scale": scale,
            "lot": lot,
            "order_price": order_price,
            "fill_price": None,
            "commission_per_share": self.commission_per_share,
            "order_id": None,
            "order_status": "placing",
            "status": "open",
            "resolved_at": None,
            "hit": None,
            "payout": None,
            "pnl_gross": None,
            "pnl_net": None,
            "commission_total": None,
            "balance_before": None,
            "balance_after": None,
            "placement_attempts": 0,
        }

        misses_note = f", промахов подряд: {misses}" if strategy_type == "pyramid" else ""
        self.logger.info(
            f"🛒 [Trader/{strategy_set}] {icao}: открываю позицию -- {market['label']}, "
            f"лот {lot} по {order_price}$ (тип {strategy_type}{misses_note}{lot_note})"
        )

        for attempt in range(1, max_attempts + 1):
            record["placement_attempts"] = attempt
            attempt_note = f" (попытка {attempt}/{max_attempts})" if attempt > 1 else ""

            try:
                response = self._get_order_client().place_limit_order(
                    token_id=market["yes_token"], side="BUY", price=str(order_price), size=str(lot),
                )
            except Exception as e:
                self.logger.error(f"❌ [Trader/{strategy_set}] {icao}: ошибка размещения ордера{attempt_note} -- {e}")
                record["order_status"] = f"error: {e}"
                record["status"] = "failed"
                self.store.open_positions[position_id] = record
                self.store.save_open_positions()
                self.store.append_or_update_day_record(date_str, record)
                self._notify(f"🔴 Ошибка размещения ордера {city_tag} ({strategy_set}): {e}")
                return

            ok = response.get("ok") if isinstance(response, dict) else getattr(response, "ok", None)
            if not ok:
                message = (response.get("message") if isinstance(response, dict) else getattr(response, "message", None))
                self.logger.error(f"❌ [Trader/{strategy_set}] {icao}: заявка отклонена{attempt_note} -- {message}")
                record["order_status"] = f"rejected: {message}"
                record["status"] = "failed"
                self.store.open_positions[position_id] = record
                self.store.save_open_positions()
                self.store.append_or_update_day_record(date_str, record)
                self._notify(f"🔴 Заявка отклонена {city_tag} ({strategy_set}): {message}")
                return

            order_id = response.get("order_id") if isinstance(response, dict) else getattr(response, "order_id", None)
            record["order_id"] = order_id
            record["order_status"] = "pending"
            self.store.open_positions[position_id] = record
            self.store.save_open_positions()
            self.store.append_or_update_day_record(date_str, record)
            self.logger.info(f"📨 [Trader/{strategy_set}] {icao}: заявка отправлена{attempt_note}, order_id={order_id}")

            # wait_for_fill() -- только для лога хэша транзакции расчёта (см. докстринг
            # _OrderClient.wait_for_fill выше). Её булев результат САМ ПО СЕБЕ больше НЕ
            # считается достаточным подтверждением исполнения -- на практике был случай,
            # когда settlement был подтверждён (True, хэш в логе), а по факту ни ордера,
            # ни позиции на Polymarket не оказалось. Реальное состояние всегда проверяется
            # отдельно через _reconcile() ниже.
            self._get_order_client().wait_for_fill(response, timeout_sec=wait_sec)

            state, detail = self._reconcile(order_id, market["yes_token"], lot)

            if state == "filled":
                fill_price = detail
                record["order_status"] = "filled"
                record["fill_price"] = fill_price
                if fill_price is None:
                    self.logger.warning(
                        f"⚠️ [Trader/{strategy_set}] {icao}: реальная цена исполнения не нашлась в "
                        f"list_positions -- показываю цену лимитки вместо неё"
                    )
                display_price = fill_price if fill_price is not None else order_price
                display_price_str = _cents_str(display_price)
                self.logger.info(
                    f"✅ [Trader/{strategy_set}] {icao}: позиция открыта{attempt_note} "
                    f"(подтверждена в list_positions) по {display_price_str}"
                )
                self._notify(
                    f"✅ <b>Позиция открыта</b> за {wait_sec} сек {city_tag}\n"
                    f"{strategy_label} <b>{market['label']}</b> на <b>{lot}</b> по <b>{display_price_str}</b> | {poly_link}"
                )
                break

            if state == "resting":
                status, size_matched = detail
                record["order_status"] = f"not_filled_{wait_sec}s (status={status}, matched={size_matched})"
                self.logger.warning(
                    f"⚠️ [Trader/{strategy_set}] {icao}: не исполнилась за {wait_sec} сек -- статус {status}, "
                    f"исполнено {size_matched} из {lot}. Ордер подтверждён живым в стакане -- остаётся висеть "
                    f"(GTC), не переразмещаю (в т.ч. если цена ушла от лимита -- это нормально); дальше "
                    f"отслеживается только при разрешении позиции (resolve_positions)."
                )
                try:
                    size_matched_str = f"{float(size_matched):.1f}"
                except (TypeError, ValueError):
                    size_matched_str = str(size_matched)
                self._notify(
                    f"⏳ <b>Позиция не открылась</b> за {wait_sec} сек {city_tag}\n"
                    f"{strategy_label} <b>{market['label']}</b> на <b>{lot}</b> по <b>{_cents_str(order_price)}</b> | {poly_link}\n"
                    f"Исполнено <b>{size_matched_str}</b> из <b>{lot}</b> | статус {status}\n"
                    f"Подтверждена в стакане и остаётся висеть (GTC)."
                )
                break

            if state == "partial":
                # matched > 0, но < lot -- НЕ считаем "filled" на весь lot: остаток либо ещё
                # висит в стакане, либо уже закрылся частично неисполненным (например, цена
                # успела уйти от лимита ПОСЛЕ того, как матчнулась только часть) -- реальный
                # matched будет заново подтверждён при разрешении позиции (_verify_fill), а
                # НЕ взят как есть отсюда (record["order_status"] намеренно НЕ "filled").
                size_matched, fill_price, status = detail
                display_price = fill_price if fill_price is not None else order_price
                display_price_str = _cents_str(display_price)
                record["order_status"] = f"not_filled_{wait_sec}s (status={status}, partial matched={size_matched} of {lot})"
                record["fill_price"] = fill_price
                self.logger.warning(
                    f"⚠️ [Trader/{strategy_set}] {icao}: исполнилась ЧАСТИЧНО за {wait_sec} сек -- {size_matched} "
                    f"из {lot} по {display_price_str} (статус {status}). Остаток остаётся висеть в стакане (GTC), "
                    f"не переразмещаю; фактический итог будет заново подтверждён при разрешении позиции."
                )
                self._notify(
                    f"⚠️ <b>Позиция открылась ЧАСТИЧНО</b> за {wait_sec} сек {city_tag}\n"
                    f"{strategy_label} <b>{market['label']}</b> на <b>{size_matched}</b> из <b>{lot}</b> по "
                    f"<b>{display_price_str}</b> | {poly_link}\n"
                    f"Остаток заявки остаётся висеть в стакане (GTC), статус {status}"
                )
                break

            if state == "unknown":
                record["order_status"] = f"not_filled_{wait_sec}s (проверка не удалась: {detail})"
                self.logger.warning(
                    f"⚠️ [Trader/{strategy_set}] {icao}: не удалось подтвердить ни исполнение, ни статус ордера "
                    f"-- {detail}. На всякий случай НЕ переразмещаю заявку (могла остаться живой или уже "
                    f"исполниться) -- оставляю как есть, разрешится позже через resolve_positions."
                )
                self._notify(
                    f"⚠️ <b>Не удалось проверить статус позиции</b> за {wait_sec} сек {city_tag}\n"
                    f"{strategy_label} <b>{market['label']}</b> на <b>{lot}</b> по <b>{_cents_str(order_price)}</b> | {poly_link}\n"
                    f"⚠️ <b>Не удалось проверить статус ордера:</b>\n"
                    f"{detail}.\n"
                    f"Заявку не переразмещаю (могла исполниться), проверю позже."
                )
                break

            # state == "vanished" -- ПОДТВЕРЖДЕНО: ни ордера, ни позиции нет
            status_detail = detail[0] if detail else None
            self.logger.warning(
                f"⚠️ [Trader/{strategy_set}] {icao}: заявка{attempt_note} бесследно пропала -- ни ордера, ни "
                f"позиции не найдено (status={status_detail})"
            )
            if attempt < max_attempts:
                self.logger.info(
                    f"🔁 [Trader/{strategy_set}] {icao}: переразмещаю заявку заново (попытка {attempt + 1}/{max_attempts})"
                )
                continue

            record["order_status"] = f"vanished_after_{max_attempts}_attempts"
            record["status"] = "failed"
            self.logger.error(
                f"🔴 [Trader/{strategy_set}] {icao}: заявка пропадает {max_attempts} раза подряд (ни ордера, ни "
                f"позиции) -- пропускаю сигнал, повторных заявок по нему больше не будет"
            )
            self._notify(
                f"🔴 <b>Заявка бесследно пропала</b> {city_tag} ({strategy_set})\n"
                f"{strategy_label} <b>{market['label']}</b> на <b>{lot}</b> по <b>{_cents_str(order_price)}</b> | {poly_link}\n"
                f"После {max_attempts} попыток ни ордер, ни позиция не найдены. Сигнал пропущен, "
                f"новых заявок по нему больше не будет."
            )
            break

        self.store.open_positions[position_id] = record
        self.store.save_open_positions()
        self.store.append_or_update_day_record(date_str, record)

    # ------------------------------------------------------------------ #
    # Разрешение открытых позиций по факту
    # ------------------------------------------------------------------ #

    def _load_month_facts(self, year, month):
        path = os.path.join(self.polymarket_gamma_dir, f"polymarket_gamma_{year}_{month:02d}.jsonl")
        facts = {}
        if not os.path.isfile(path):
            return facts
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                facts[(rec["icao"], rec["date"])] = (rec["bucket"], rec["scale"])
        return facts

    def _bucket_index(self, value, scale):
        return round(value) // self.bucket_width[scale]

    def _verify_fill(self, record):
        """
        Отвечает на вопрос "а если по ордеру за 15 секунд не открылась позиция
        и он остался висеть, а потом она открылась -- бот её увидит?":

        - если позиция уже была подтверждена исполненной за 15 сек
          (order_status == "filled" на этапе on_signal) -- заново ничего не
          проверяем, actual_lot = record["lot"];
        - иначе (заявка осталась висеть в стакане, GTC) -- ПЕРЕД расчётом P&L
          заново опрашиваем статус ордера через order_client.get_order_status,
          чтобы узнать, исполнилась ли она (полностью или частично) с момента
          открытия. Именно этот повторный опрос -- ответ "да, увидит": P&L
          считается по РЕАЛЬНО исполненному размеру на момент разрешения
          позиции (когда появляется факт), а не по изначально заказанному
          lot/order_price.

        Возвращает (actual_lot, note):
          - actual_lot == 0.0 -- заявка так и не исполнилась ни на сколько
            (сделки не было вообще -- ни P&L, ни промах/попадание пирамиды
            не учитываются) -- ТАКЖЕ возвращается, когда get_order_status
            падает с ошибкой verify_max_attempts раз подряд (на разных
            вызовах resolve_positions, т.е. по факту -- дней) и позиция ни
            разу не нашлась через fallback-проверку в list_positions (см.
            ниже) -- иначе позиция с "битым" order_id виснет в
            open_positions.json НАВСЕГДА, откладываясь на каждом цикле;
          - actual_lot < record["lot"] -- частичное исполнение -- P&L
            считается по фактически исполненному размеру;
          - actual_lot is None -- проверить статус не удалось (сетевая
            ошибка и т.п.), лимит попыток (verify_max_attempts) ещё не
            исчерпан -- разрешение позиции откладывается до следующего
            цикла, чтобы не насчитать P&L по неподтверждённым данным.
        """
        if record.get("order_status") == "filled":
            return record["lot"], "filled"
        order_id = record.get("order_id")
        if not order_id:
            return 0.0, record.get("order_status", "no_order_id")
        try:
            status, size_matched = self._get_order_client().get_order_status(order_id)
        except Exception as e:
            # Статус ОРДЕРА больше недоступен/некорректен -- прежде чем откладывать
            # разрешение до следующего цикла, ДОПОЛНИТЕЛЬНО проверяем РЕАЛЬНУЮ
            # позицию напрямую в list_positions (независимый источник данных от
            # get_order_status) -- если она всё-таки нашлась, сделка состоялась,
            # несмотря на то что статус самого ордера прочитать не удаётся.
            token_id = record.get("token_id")
            size_found, price_found = (None, None)
            if token_id:
                size_found, price_found = self._get_order_client().get_position_info(token_id)
            if size_found not in (None, 0, 0.0):
                if record.get("fill_price") is None and price_found is not None:
                    record["fill_price"] = price_found
                record.pop("verify_fail_count", None)
                return size_found, f"verified_via_position_after_order_status_error: {e}"

            fail_count = record.get("verify_fail_count", 0) + 1
            record["verify_fail_count"] = fail_count
            if fail_count < self.verify_max_attempts:
                return None, f"verify_error ({fail_count}/{self.verify_max_attempts}): {e}"

            # Лимит попыток исчерпан -- статус ордера НИ РАЗУ не удалось прочитать,
            # и позиция ни разу не нашлась -- сдаёмся, закрываем без данных (как
            # "заявка не исполнилась"), а не откладываем разрешение бесконечно.
            # Это отдельный, тревожный случай (не обычное "не исполнилась") --
            # отдельный лог + Telegram, а не обычное "⏹️ Заявка не исполнилась".
            self.logger.error(
                f"🔴 [Trader] {record['icao']} {record['opened_date']}: не удалось подтвердить статус ордера "
                f"{order_id} {fail_count} раз(а) подряд ({e}), позиция ни разу не нашлась ни в list_positions -- "
                f"СДАЮСЬ, закрываю без данных (P&L 0, требуется ручная проверка на Polymarket)"
            )
            self._notify(
                f"🔴 <b>Не удалось подтвердить ордер</b> {_city_tag(record.get('city', record['icao']))}\n"
                f"{record.get('bet_label', '')}\n"
                f"Статус ордера {order_id} не читается {fail_count} раз(а) подряд, позиция не найдена ни разу. "
                f"Закрываю без данных (P&L 0). Проверьте вручную на Polymarket.\n"
                f"Ошибка: {e}"
            )
            record["_verify_gaveup"] = True
            return 0.0, f"verify_giveup_after_{fail_count}_attempts: {e}"
        record.pop("verify_fail_count", None)  # статус ордера снова читается -- счётчик неудач сбрасываем
        size_matched = float(size_matched) if size_matched not in (None, "") else 0.0
        if size_matched > 0 and record.get("fill_price") is None:
            token_id = record.get("token_id")
            if token_id:
                try:
                    fill_price = self._get_order_client().get_fill_price(token_id)
                except Exception as e:
                    fill_price = None
                    self.logger.warning(
                        f"⚠️ [Trader] Не удалось получить реальную цену позднего исполнения -- {e}, "
                        f"P&L посчитается по цене лимитки"
                    )
                if fill_price is not None:
                    record["fill_price"] = fill_price
        return size_matched, f"verified_at_resolve: status={status}, matched={size_matched}"

    def resolve_positions(self):
        """
        Разрешает ВСЕ открытые позиции, для которых уже есть факт Polymarket
        (см. PolymarketActualsMiner) — не только за конкретный target_date:
        если факт по более ранней дате домайнился с опозданием, позиция всё
        равно разрешится на следующем вызове. Вызывается Orchestrator'ом раз
        в сутки, сразу после polymarket_actuals_miner.run().

        Перед расчётом P&L КАЖДОЙ позиции, оставшейся висеть (GTC) после
        первичной 15-секундной проверки, статус ордера ПЕРЕОПРАШИВАЕТСЯ
        заново (см. _verify_fill) -- поздно исполнившаяся заявка будет
        корректно обнаружена и учтена по фактическому размеру/цене
        исполнения, а заявка, так и не исполнившаяся, не даёт ни прибыли,
        ни убытка и не сбивает счётчик пирамиды.

        В начале отправляет отложенные уведомления "Позиция закрыта" по
        позициям, которые были разрешены раньше (синхронно перед новым
        сигналом, см. _try_resolve_now) -- чтобы они тоже приходили в чат
        именно ночью, вместе с остальными.
        """
        self._flush_deferred_notifications()

        if not self.store.open_positions:
            return

        facts_cache = {}
        resolved_ids = []

        for position_id, record in list(self.store.open_positions.items()):
            if record.get("status") != "open":
                continue
            date_str = record["opened_date"]
            icao = record["icao"]
            try:
                year, month = int(date_str[:4]), int(date_str[5:7])
            except Exception:
                continue
            cache_key = (year, month)
            if cache_key not in facts_cache:
                facts_cache[cache_key] = self._load_month_facts(year, month)
            fact = facts_cache[cache_key].get((icao, date_str))
            if fact is None:
                continue  # факта ещё нет -- ждём

            if self._resolve_one_open_position(record, fact):
                resolved_ids.append(position_id)

        for position_id in resolved_ids:
            self.store.open_positions.pop(position_id, None)
        if resolved_ids:
            self.store.save_open_positions()

    def _resolve_one_open_position(self, record, fact, notify_on_close=True):
        """
        Тело разрешения ОДНОЙ позиции по уже известному факту -- вынесено из
        resolve_positions() отдельным методом, чтобы тот же код мог вызвать
        и _try_resolve_now() (см. on_signal): синхронная попытка разрешить
        ВИСЯЩУЮ предыдущую позицию по этому же (strategy_set, icao) ПРЯМО
        ПЕРЕД открытием новой -- не дожидаясь дневного пайплайна (единственное
        место, где resolve_positions() теперь вызывается фоном, раз в сутки).

        Мутирует record на месте (record["status"] и т.д.), обновляет
        realized P&L / счётчик пирамиды в self.store -- НЕ трогает
        self.store.open_positions (это остаётся на вызывающем: resolve_positions()
        удаляет позицию из словаря пачкой в конце, _try_resolve_now() -- сама).

        notify_on_close -- слать ли уведомление "Позиция закрыта"/"Заявка не
        исполнилась" в Telegram. resolve_positions() (дневной пайплайн, раз в
        сутки по Сиэтлу) вызывает с True -- это ЕДИНСТВЕННОЕ место, откуда
        теперь приходят такие уведомления. _try_resolve_now() (синхронный
        резолв ПРЯМО ПЕРЕД открытием новой позиции по этому же городу, на
        каждый сигнал) передаёт False -- само разрешение (P&L, счётчик
        пирамиды) по-прежнему считается немедленно, как и раньше, но
        сообщение в чат не уходит сразу (чтобы не плодить уведомления на
        каждый сигнал вперемешку с "Позиция открыта"/"На самую дорогую
        ставку"), а откладывается (deferred_notifications.json) и уходит
        при ближайшем ночном resolve_positions() -- иначе такая позиция
        вообще осталась бы без сообщения о закрытии. В лог
        (logger) пишется в обоих случаях одинаково.

        Возвращает True, если позиция разрешена (resolved / resolved_no_fill)
        и должна быть убрана из open_positions; False -- если разрешение
        отложено (не удалось подтвердить исполнение ордера, см. _verify_fill) --
        вызывающий должен оставить её как есть и попробовать позже.
        """
        icao = record["icao"]
        date_str = record["opened_date"]
        strategy_set = record["strategy_set"]

        fact_label, fact_scale = fact
        fact_low, fact_high = parse_bucket_range(fact_label)
        if fact_low is None:
            self.logger.warning(f"⚠️ [Trader] {icao} {date_str}: не удалось распарсить факт-бакет '{fact_label}'")
            return False
        fact_anchor = fact_low if fact_low != float("-inf") else fact_high
        actual_bucket = self._bucket_index(fact_anchor, fact_scale)

        actual_lot, verify_note = self._verify_fill(record)
        if actual_lot is None:
            self.logger.warning(
                f"⚠️ [Trader] {icao} {date_str}: факт по рынку уже есть, но не удалось подтвердить "
                f"исполнение ордера {record.get('order_id')} ({verify_note}) -- откладываю разрешение "
                f"позиции до следующего цикла"
            )
            return False

        if actual_lot <= 0:
            # заявка так и не исполнилась ни на сколько -- сделки не было:
            # ни P&L, ни промах/попадание пирамиды не учитываются
            record["status"] = "resolved_no_fill"
            record["resolved_at"] = datetime.now().isoformat()
            record["order_status"] = verify_note
            record["actual_bucket"] = actual_bucket
            record["fact_label"] = fact_label
            record["hit"] = None
            record["payout"] = 0.0
            record["pnl_gross"] = 0.0
            record["pnl_net"] = 0.0
            record["commission_total"] = 0.0
            balance_now = round(self.store.get_realized_pnl(strategy_set), 4)
            record["balance_before"] = balance_now
            record["balance_after"] = balance_now
            self.store.append_or_update_day_record(date_str, record)
            self.logger.info(
                f"⏹️ [Trader/{strategy_set}] {icao} {date_str}: заявка (order_id={record.get('order_id')}) "
                f"так и не исполнилась ни разу -- позиция закрыта без сделки, P&L 0 (пирамида не сбита)"
            )
            # если это "сдался после verify_max_attempts неудачных проверок" --
            # отдельное, более тревожное уведомление уже отправлено из _verify_fill,
            # это обычное "⏹️ Заявка не исполнилась" здесь дублировать не нужно
            if not record.pop("_verify_gaveup", False):
                self._notify_or_defer(
                    record["position_id"],
                    f"⏹️ <b>Заявка не исполнилась</b> {_city_tag(record['city'])} ({strategy_set})\n"
                    f"{record['bet_label']}. Ордер так и не заполнился за время до разрешения рынка, P&L 0.",
                    notify_on_close,
                )
            return True

        if actual_lot != record["lot"]:
            self.logger.warning(
                f"⚠️ [Trader/{strategy_set}] {icao} {date_str}: заявка исполнилась ПОЗЖЕ первичной 15-сек "
                f"проверки и/или частично -- фактически исполнено {actual_lot} из заказанных {record['lot']} "
                f"({verify_note}) -- P&L считается по фактически исполненному размеру"
            )
            record["requested_lot"] = record["lot"]
            record["order_status"] = verify_note

        hit = (record["target_bucket"] == actual_bucket)
        lot = actual_lot
        # Для P&L берём РЕАЛЬНУЮ цену исполнения (record["fill_price"]),
        # если она была получена от биржи (см. _extract_fill_price) --
        # цена лимитки (order_price) используется только как запасной
        # вариант, если реальную цену так и не удалось извлечь ни разу
        # (ни при открытии, ни при последующих проверках в _verify_fill).
        order_price = record.get("fill_price")
        if order_price is None:
            order_price = record["order_price"]
        commission_per_share = record.get("commission_per_share", self.commission_per_share)

        cost = order_price * lot + commission_per_share * lot
        payout = lot if hit else 0.0
        pnl_net = payout - cost
        pnl_gross = payout - (order_price * lot)
        commission_total = commission_per_share * lot

        balance_before = self.store.get_realized_pnl(strategy_set)
        self.store.add_realized_pnl(strategy_set, pnl_net)
        balance_after = self.store.get_realized_pnl(strategy_set)

        record["status"] = "resolved"
        record["resolved_at"] = datetime.now().isoformat()
        record["lot"] = lot  # фактически исполненный размер (может отличаться от изначально заказанного)
        record["hit"] = hit
        record["actual_bucket"] = actual_bucket
        record["fact_label"] = fact_label
        record["payout"] = payout
        record["pnl_gross"] = round(pnl_gross, 4)
        record["pnl_net"] = round(pnl_net, 4)
        record["commission_total"] = round(commission_total, 4)
        record["balance_before"] = round(balance_before, 4)
        record["balance_after"] = round(balance_after, 4)

        if record["strategy_type"] == "pyramid":
            misses = self.store.get_misses(strategy_set, icao)
            loss_limit = record.get("loss_limit") or 2
            new_misses = 0 if (hit or misses + 1 >= loss_limit) else misses + 1
            self.store.set_misses(strategy_set, icao, new_misses)
            # Состояние серии для режимов recover/recover_min_multiplier: хит либо сброс по
            # loss_limit -- серия закончена (накопленный убыток списывается, как в
            # бэктесте); иначе -- прибавляем убыток этой ставки (pnl_net < 0 при промахе)
            # и запоминаем фактический лот. Ведётся при любом pyramid_mode, чтобы
            # смена режима в конфиге не теряла серию.
            if new_misses == 0:
                self.store.clear_series(strategy_set, icao)
            else:
                prev = self.store.get_series(strategy_set, icao)
                prev_loss = float(prev.get("loss", 0.0)) if prev else 0.0
                self.store.set_series(strategy_set, icao, prev_loss + max(0.0, -pnl_net), lot)

        self.store.append_or_update_day_record(date_str, record)

        result_word = "хит ✅" if hit else "промах ❌"
        self.logger.info(
            f"📉 [Trader/{strategy_set}] {icao} {date_str}: {result_word} -- факт {fact_label}, лот {lot}, "
            f"P&L без комиссии {pnl_gross:+.2f}$, P&L с учётом комиссии ({commission_total:.3f}$) "
            f"{pnl_net:+.2f}$, баланс стратегии {balance_before:+.2f}$ -> {balance_after:+.2f}$"
        )
        strategy_label = STRATEGY_LABELS.get(strategy_set, strategy_set)
        # "факт" показываем только когда ставка НЕ сошлась (промах/убыток) --
        # когда ставка и так совпала с фактом, повторять его излишне. И
        # таргет (record["bet_label"]), и факт (fact_label) выделены жирным --
        # это главное, что нужно сравнить глазами в этом сообщении.
        fact_part = f", факт <b>{fact_label}</b>" if not hit else ""
        # notify_on_close=True (ночной резолв по Сиэтлу) -- сразу в чат; False (синхронный
        # резолв перед новым сигналом) -- не засоряем чат на каждый сигнал, а откладываем
        # и отправляем при ближайшем ночном резолве (см. _flush_deferred_notifications),
        # иначе такая позиция так и осталась бы вообще без сообщения о закрытии.
        self._notify_or_defer(
            record["position_id"],
            f"{'✅' if hit else '❌'} <b>Позиция закрыта</b> {_city_tag(record['city'])}\n"
            f"<b>{record['bet_label']}</b> на <b>{lot}</b> по <b>{_cents_str(order_price)}</b>{fact_part}\n"
            f"P&L: <b>{pnl_net:+.2f}$</b> с учётом комиссии {commission_total:.2f}$\n"
            f"Баланс стратегии <b>{strategy_label}</b>: {balance_after:+.2f}$",
            notify_on_close,
        )

        self._check_risk(strategy_set)
        return True

    def _find_open_position(self, strategy_set, icao):
        """
        Ищет в self.store.open_positions висящую ОТКРЫТУЮ (status == "open")
        позицию по этому же (strategy_set, icao) и возвращает её запись, либо
        None, если такой нет. Используется и в _try_resolve_now (попытка
        разрешить её прямо сейчас), и в on_signal (защита от задвоения --
        не открывать новую позицию, пока по этому городу/стратегии уже есть
        нерезолвленная).
        """
        for record in self.store.open_positions.values():
            if (
                record.get("status") == "open"
                and record.get("strategy_set") == strategy_set
                and record.get("icao") == icao
            ):
                return record
        return None

    def _try_resolve_now(self, strategy_set, icao):
        """
        Вызывается ИЗ on_signal(), ПЕРЕД тем как посчитать misses/лот новой
        позиции (см. реализацию ниже) -- ищет висящую ОТКРЫТУЮ позицию по
        этому же (strategy_set, icao) и пытается разрешить её ПРЯМО СЕЙЧАС,
        синхронно:

          1. сначала смотрим локальный кэш (_load_month_facts -- то, что уже
             домайнено фоновыми циклами/дневным пайплайном);
          2. если там факта ещё нет -- делаем ОДИН живой запрос к Gamma API
             (self.actuals_fetcher, см. Orchestrator._fetch_actuals_fact_now)
             ИМЕННО по этой (icao, date) паре;
          3. если факт нашёлся тем или иным способом -- разрешаем позицию
             (_resolve_one_open_position) и, если она реально закрылась,
             убираем её из open_positions -- новый сигнал увидит уже
             актуальный, только что обновлённый счётчик пирамиды;
          4. если рынок и правда ещё не резолвился -- ничего не делаем,
             новая позиция считается по тому, что уже есть в pyramid_state
             (как и раньше) -- ждать вечно новый сигнал не может.

        Раньше факт по предыдущей позиции проверялся ТОЛЬКО в фоне, раз в
        сутки в дневном пайплайне -- и НИКОГДА непосредственно перед открытием
        новой позиции: если рынок резолвился позже этой единственной суточной
        проверки, но до следующего сигнала по тому же городу, лот считался по
        устаревшему счётчику пирамиды (см. кейс Лакхнау, лоты 5, 10, 10 вместо
        5, 10, 5). Этот метод -- ЕДИНСТВЕННАЯ проверка ПРЯМО ПЕРЕД тем, как
        лот будет зафиксирован (фоновый опрос "на каждом цикле"/"раз в N минут"
        специально не заводили отдельно -- он был бы избыточен: этой проверки,
        прямо в момент, когда решение реально принимается, достаточно самой
        по себе; дневной пайплайн (раз в сутки) остаётся как финальная
        подстраховка на случай, если для города вообще больше не будет ни
        одного сигнала).
        """
        open_record = self._find_open_position(strategy_set, icao)
        if open_record is None:
            return  # висящей предыдущей позиции по этому городу/стратегии нет

        date_str = open_record["opened_date"]
        try:
            year, month = int(date_str[:4]), int(date_str[5:7])
        except Exception:
            return

        fact = self._load_month_facts(year, month).get((icao, date_str))
        if fact is None and self.actuals_fetcher is not None:
            try:
                fact = self.actuals_fetcher(icao, date_str)
            except Exception as e:
                self.logger.warning(
                    f"⚠️ [Trader] {icao} {date_str}: живой запрос факта перед новым сигналом не удался -- {e}"
                )
                fact = None

        if fact is None:
            self.logger.info(
                f"⏳ [Trader/{strategy_set}] {icao} {date_str}: предыдущая позиция ещё не резолвилась на "
                f"Polymarket -- новый лот считается по последнему известному счётчику пирамиды"
            )
            return

        position_id = open_record["position_id"]
        if self._resolve_one_open_position(open_record, fact, notify_on_close=False):
            self.store.open_positions.pop(position_id, None)
            self.store.save_open_positions()

    # ------------------------------------------------------------------ #
    # Ежедневная сводка в Telegram (config.price_monitor.daily_summary_enabled)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _plural_cities(n):
        n_abs = abs(n)
        if n_abs % 10 == 1 and n_abs % 100 != 11:
            return "город"
        if n_abs % 10 in (2, 3, 4) and n_abs % 100 not in (12, 13, 14):
            return "города"
        return "городов"

    @staticmethod
    def _colored_money(value):
        # Telegram HTML не поддерживает произвольный цвет текста -- ближайший
        # рабочий эквивалент "раскрасить цифры зелёным/красным" в сообщении
        # бота -- цветной эмодзи-индикатор перед суммой. Отдельно -- нейтральный
        # ⚪ для (фактически) нулевого значения: "не было сделок"/"ничья" --
        # не зелёный плюс и не красный минус, порог 0.005 -- ровно то, что
        # после округления до 2 знаков всё равно показалось бы как 0.00$.
        if abs(value) < 0.005:
            emoji = "⚪"
        else:
            emoji = "🟢" if value >= 0 else "🔴"
        sign = "+" if value >= 0 else "-"
        return f"{emoji}{sign}{abs(value):.2f}$"

    @staticmethod
    def _colored_pct(value):
        # Тот же принцип, что в _colored_money -- порог 0.05, под округление до 1 знака.
        if abs(value) < 0.05:
            emoji = "⚪"
        else:
            emoji = "🟢" if value >= 0 else "🔴"
        sign = "+" if value >= 0 else "-"
        return f"{emoji}{sign}{abs(value):.1f}%"

    def send_daily_summary(self, date_str):
        """
        Сводка за сутки date_str (см. п.3 ТЗ) — раз в сутки, сразу после
        resolve_positions() дневного пайплайна. Считается ПО ЖУРНАЛЬНЫМ
        файлам trades_<YYYY_MM_DD>.jsonl в config.paths.trading_dir (а не по
        risk_state.json), поэтому "с момента ведения файлов, без учёта
        перезапусков" получается естественно — сами журнальные файлы не
        зависят от рестартов бота. Учитываются только реально исполненные и
        разрешённые фактом позиции (status == "resolved"); resolved_no_fill
        (заявка так и не исполнилась) в сводку не попадает — по ней и так
        P&L == 0.
        """
        if not self.daily_summary_enabled:
            return

        trading_dir = self.store.trading_dir
        if not os.path.isdir(trading_dir):
            return

        target_file = f"trades_{date_str.replace('-', '_')}.jsonl"
        all_time_by_city = {}   # icao -> {"name": str, "pnl": float}
        today_by_city = {}      # icao -> {"name": str, "pnl": float}
        total_all_time = 0.0
        total_today = 0.0

        for fname in sorted(os.listdir(trading_dir)):
            if not (fname.startswith("trades_") and fname.endswith(".jsonl")):
                continue
            is_today = (fname == target_file)
            path = os.path.join(trading_dir, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        rec = json.loads(line)
                        if rec.get("status") != "resolved":
                            continue
                        icao = rec["icao"]
                        pnl = float(rec.get("pnl_net") or 0.0)
                        city_name = rec.get("city", icao)
                        entry = all_time_by_city.setdefault(icao, {"name": city_name, "pnl": 0.0})
                        entry["pnl"] += pnl
                        total_all_time += pnl
                        if is_today:
                            t_entry = today_by_city.setdefault(icao, {"name": city_name, "pnl": 0.0})
                            t_entry["pnl"] += pnl
                            total_today += pnl
            except Exception as e:
                self.logger.warning(f"⚠️ [Trader] Сводка: не удалось прочитать {path}: {e}")

        if not today_by_city:
            return  # ни одной реально разрешённой позиции за сутки -- сводку слать не о чем

        balance = self.account_balance + total_all_time
        total_pct = (total_all_time / self.account_balance * 100.0) if self.account_balance else 0.0
        today_pct = (total_today / self.account_balance * 100.0) if self.account_balance else 0.0
        n_cities_today = len(today_by_city)

        lines = [f"📊 <b>Сводка ставок на Polymarket за {date_str}</b> #Bet_Summary"]
        lines.append(f"Баланс: {balance:.2f}$ | {self._colored_money(total_all_time)} | {self._colored_pct(total_pct)}")
        lines.append(f"{n_cities_today} {self._plural_cities(n_cities_today)}: {self._colored_money(total_today)} | {self._colored_pct(today_pct)}")
        # Список городов -- ПО ВСЕМ городам, по которым вообще были сделки (см. журнальные
        # файлы trades_*.jsonl), а не только по тем, что торговались именно за прошедшие
        # сутки -- иначе город без сделки за сутки просто выпадал из списка. Для
        # города без сделки сегодня столбец "сегодня" -- просто 0.00$. НО только
        # среди городов, которые сейчас реально настроены (не закомментированы) хоть
        # в одном из watched_icaos_*.yaml (self.all_watched_icaos, см. __init__) --
        # город, убранный из конфига (например, Варшава), в список больше не
        # попадает, даже если по нему остались старые сделки в trades_*.jsonl. Из-за
        # этого сумма показанных all-time P&L может не совпадать с общим балансом
        # наверху -- это ожидаемо (баланс/итог всегда честные, по ВСЕЙ истории).
        for icao, at_entry in sorted(all_time_by_city.items(), key=lambda kv: kv[1]["name"]):
            if icao not in self.all_watched_icaos:
                continue
            today_entry = today_by_city.get(icao, {"pnl": 0.0})
            at_pct = (at_entry["pnl"] / self.account_balance * 100.0) if self.account_balance else 0.0
            lines.append(
                f"{_city_tag(at_entry['name'])}: {self._colored_money(today_entry['pnl'])} | "
                f"{self._colored_money(at_entry['pnl'])} ({at_pct:+.1f}%)"
            )

        self._notify("\n".join(lines))
        self.logger.info(
            f"📊 [Trader] Дневная сводка за {date_str} отправлена в Telegram "
            f"({n_cities_today} городов за сутки, {len(all_time_by_city)} всего в списке)"
        )

    # ------------------------------------------------------------------ #
    # Risk-менеджмент
    # ------------------------------------------------------------------ #

    def _check_risk(self, strategy_set):
        scfg = self.strategy_cfg[strategy_set]
        strategy_loss_limit_usd = self.account_balance * scfg["strategy_max_loss_pct"] / 100.0
        realized = self.store.get_realized_pnl(strategy_set)
        if not self.store.is_halted(strategy_set) and realized <= -strategy_loss_limit_usd:
            self.store.set_halted(strategy_set)
            msg = (
                f"🔴 <b>СТОП-ТОРГОВЛЯ: {strategy_set}</b>\n"
                f"Реализованный убыток {realized:+.2f}$ превысил допустимые {scfg['strategy_max_loss_pct']}% "
                f"от счёта ({self.account_balance:.2f}$, лимит {strategy_loss_limit_usd:.2f}$). "
                f"Стратегия остановлена."
            )
            self.logger.error(f"🔴 [Trader] {msg}")
            self._notify(msg)
            if scfg["risk_call_alert_enabled"]:
                self._call_alert(
                    f"Stop strategy {strategy_set}", scfg["risk_call_user"],
                    scfg["risk_call_max_attempts"], scfg["risk_call_retry_delay_sec"],
                )

        total_loss_limit_usd = self.account_balance * self.total_max_loss_pct / 100.0
        total_realized = self.store.get_total_realized_pnl()
        if not self.store.is_totally_halted() and total_realized <= -total_loss_limit_usd:
            self.store.set_totally_halted()
            msg = (
                f"🔴 <b>СТОП ВСЕЙ ТОРГОВЛИ</b>\n"
                f"Суммарный реализованный убыток {total_realized:+.2f}$ превысил допустимые "
                f"{self.total_max_loss_pct}% от счёта ({self.account_balance:.2f}$, "
                f"лимит {total_loss_limit_usd:.2f}$). Вся авто-торговля остановлена."
            )
            self.logger.error(f"🔴 [Trader] {msg}")
            self._notify(msg)
            if self.risk_call_alert_enabled:
                self._call_alert(
                    "Stop ALL trading", self.risk_call_user,
                    self.risk_call_max_attempts, self.risk_call_retry_delay_sec,
                )

    def _notify(self, msg):
        return self.telegram.send_message(msg)

    def _notify_or_defer(self, position_id, msg, send_now):
        """send_now=True -- отправить сразу; False -- отложить до ближайшего
        ночного резолва (resolve_positions -> _flush_deferred_notifications)."""
        if send_now:
            self._notify(msg)
            return
        self.store.deferred_notifications.append({"position_id": position_id, "text": msg})
        self.store.save_deferred_notifications()
        self.logger.info(
            f"🕒 [Trader] уведомление о закрытии позиции {position_id} отложено до ночного резолва "
            f"(в очереди: {len(self.store.deferred_notifications)})"
        )

    def _flush_deferred_notifications(self):
        """Отправляет накопленные отложенные уведомления о позициях, закрытых
        раньше ночного резолва, и очищает очередь. Вызывается в начале
        resolve_positions() -- ДО разбора позиций, ещё висящих открытыми (в том
        числе при их отсутствии), чтобы сообщения шли в хронологическом порядке."""
        queue = self.store.deferred_notifications
        if not queue:
            return
        self.logger.info(f"📨 [Trader] отправляю отложенные уведомления о закрытии позиций: {len(queue)} шт.")
        max_attempts = 3  # ночных попыток на одно сообщение, потом бросаем (чтобы не копить "ядовитые")
        remaining = []
        for item in queue:
            try:
                ok = self._notify(item["text"])
            except Exception as e:
                self.logger.error(f"⚠️ [Trader] ошибка отправки отложенного уведомления -- {e}")
                ok = False
            if ok is False:
                item["attempts"] = int(item.get("attempts", 0)) + 1
                if item["attempts"] >= max_attempts:
                    self.logger.error(
                        f"❌ [Trader] отложенное уведомление по позиции {item.get('position_id')} не доставлено "
                        f"за {max_attempts} ночных попытки -- отбрасываю: {item['text'][:200]!r}"
                    )
                else:
                    remaining.append(item)
        self.store.deferred_notifications[:] = remaining
        self.store.save_deferred_notifications()

    def _call_alert(self, message, call_user, max_attempts, retry_delay_sec):
        if not call_user:
            self.logger.warning("⚠️ [Trader] risk call_alert_enabled=true, но call_user не задан")
            return
        url = f"http://api.callmebot.com/start.php?source=web&user={call_user}&text={message}&lang=ru-RU-Standard-A"
        last_exc = None
        for attempt in range(1, max_attempts + 1):
            try:
                resp = requests.get(url, timeout=10)
                if resp.status_code == 200:
                    self.logger.info(f"📞 [Trader] Звонок инициирован (попытка {attempt}/{max_attempts})")
                else:
                    self.logger.info(
                        f"📞 [Trader] Звонок инициирован (попытка {attempt}/{max_attempts}) — "
                        f"ответ CallMeBot [{resp.status_code}]: {resp.text.strip()[:200]}"
                    )
                return
            except Exception as e:
                last_exc = e
                self.logger.warning(f"⚠️ [Trader] Звонок не удался (попытка {attempt}/{max_attempts}): {e}")
                if attempt < max_attempts:
                    time.sleep(retry_delay_sec)
        self.logger.error(f"🔴 [Trader] Звонок не удался после {max_attempts} попыток: {last_exc}")
