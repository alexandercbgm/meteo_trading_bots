"""
base_miner.py

Общая инфраструктура для майнеров на Selenium (WeatherMiner, ActualsMiner):
- build_logger — тот же хендлер, что был в исходных скриптах (ротация в
  полночь, вывод в файл и в консоль), плюс защита от известной проблемы
  Jupyter/ipykernel, при которой вывод в ячейку "отваливается" на длинных
  фоновых циклах (см. _JupyterSafeStream ниже);
- should_suppress_telegram_error — фильтр "эту ошибку только в лог, не в
  Telegram" по подстроке в тексте исключения (config.error_notifications);
- BaseSeleniumMiner — подключение к уже запущенному Chrome (remote debugging),
  безопасное закрытие вкладок и уведомления об ошибках в Telegram,
  дедуплицированные по (день, контекст), чтобы не заспамить чат одинаковыми
  сообщениями при систематическом сбое.
"""

import logging
import os
import sys
import threading
import time
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler

import requests

# Отключаем фоновую сетевую проверку/обновление драйвера самим Selenium
# (Selenium Manager, встроен начиная с версии 4.6+) — проект подключается к
# УЖЕ запущенному Chrome через debuggerAddress (см. BaseSeleniumMiner ниже),
# сам бинарник драйвера ему для этого не критичен, а фоновая проверка версии
# иногда не может достучаться по сети — тогда в лог попадает безобидная, но
# пугающая строка вида "error managing chromedriver ... using driver found in
# the cache". requirements.txt не фиксирует версии пакетов, так что при
# обновлении зависимостей (например, после добавления новой библиотеки)
# Selenium мог незаметно обновиться до версии, где это поведение стало
# заметнее/агрессивнее, чем раньше.
os.environ.setdefault("SE_AVOID_STATS", "true")

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

# Признак геоблока Wunderground — страница открылась (URL правильный), но
# вместо контента отдаётся эта фраза где-то в <body> (VPN отвалился либо
# уже держит не тот регион, что нужен сайту).
VPN_WU_BLOCKED_TEXT = "This content is no longer available in your area"
# Признак "Polymarket вообще не открылся" — родная страница ошибки САМОГО
# Chrome (DNS/сеть недоступны), а не что-то отданное самим Polymarket.
# xpath — фиксированная разметка этой страницы у Chrome.
VPN_POLY_UNREACHABLE_XPATH = "/html/body/div[1]/div[1]/div[1]/div[2]/h1/span"
VPN_POLY_UNREACHABLE_TEXT = "Не удается получить доступ к сайту"
# Не чаще раза в столько секунд повторяем VPN-алерт (телеграм + попытка
# дозвониться), пока признак отвалившегося VPN не исчезнет.
VPN_ALERT_COOLDOWN_SEC = 5 * 60


class _JupyterSafeStream:
    """
    Прокси над sys.stderr, который на КАЖДОЙ записи заново берёт АКТУАЛЬНЫЙ
    sys.stderr, а не хранит ссылку, зафиксированную один раз в момент создания
    хендлера (как это делает обычный logging.StreamHandler() без аргумента
    stream). В Jupyter/ipykernel вывод длинного фонового цикла (Orchestrator.run(),
    сутками работающий в одной и той же ячейке) может со временем перестать
    долетать до видимой ячейки — сам процесс при этом жив и продолжает писать
    в файловый хендлер (что и наблюдается: лог-файлы пополняются, а ячейка
    "молчит", и статус ядра показывает Unknown). Разбирать причину каждый раз
    для каждой Jupyter-сборки непрактично, а получать sys.stderr динамически
    вместо кэширования — дешёвая защита без побочных эффектов.
    """

    def write(self, message):
        try:
            sys.stderr.write(message)
        except Exception:
            pass  # поток вывода недоступен — не должно ронять логирование

    def flush(self):
        try:
            sys.stderr.flush()
        except Exception:
            pass


def build_logger(name, log_dir, filename):
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False  # иначе при чужом хендлере на root сообщение печатается дважды

    if not logger.handlers:  # защита от повторного навешивания хендлеров при повторном импорте
        formatter = logging.Formatter(
            "%(asctime)s.%(msecs)03d - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )
        fh = TimedRotatingFileHandler(
            os.path.join(log_dir, filename), when="midnight", backupCount=90, encoding="utf-8"
        )
        fh.suffix = "%Y-%m-%d"
        fh.setFormatter(formatter)
        logger.addHandler(fh)

        ch = logging.StreamHandler(stream=_JupyterSafeStream())
        ch.setFormatter(formatter)
        logger.addHandler(ch)

    return logger


def should_suppress_telegram_error(config, exc):
    """
    True, если текст исключения содержит один из
    config.error_notifications.telegram_suppress_patterns — такие ошибки
    по-прежнему полностью попадают в лог (logger.error вызывается ДО
    notify_error/этой проверки везде, где используется), но НЕ уходят в
    Telegram. По умолчанию сюда попадают "пустые" WebDriverException с голым
    нативным стектрейсом chromedriver (обрыв IPC между драйвером и Chrome) —
    они на практике транзиентные, не несут полезной информации в сообщении
    (нет python-side текста, только адреса функций chromedriver.exe) и только
    засоряют чат. Используется и в BaseSeleniumMiner.notify_error, и в
    Orchestrator — единая точка правил фильтрации.
    """
    patterns = getattr(getattr(config, "error_notifications", None), "telegram_suppress_patterns", None) or []
    text = str(exc)
    return any(pattern in text for pattern in patterns)


class BaseSeleniumMiner:
    """Базовый класс для майнеров, работающих через Selenium с уже запущенным
    Chrome (remote debugging). Подклассы (WeatherMiner, ActualsMiner) должны
    выставить self.logger и вызвать super().__init__(config, telegram, logger)."""

    def __init__(self, config, telegram, logger):
        self.config = config
        self.telegram = telegram
        self.logger = logger
        self.driver = None
        self._notified_errors_today = set()  # (date, context) — не спамить одной и той же ошибкой
        self._vpn_last_alert_ts = None  # time.time() последнего VPN-алерта — см. _check_vpn_dropped
        # Раньше тут был безусловный self.init_driver() -- то есть Chrome
        # запрашивался ПРИ КАЖДОМ создании WeatherMiner/ActualsMiner, даже
        # если конфиг просит майнить всё через API и Chrome вообще не нужен
        # (см. weather_miner.py: self.chrome_needed / run_cycle()). Теперь
        # подключение ленивое: первый же ensure_driver() (вызывается только
        # из Chrome-путей) сам обнаружит self.driver is None и подключится —
        # см. ensure_driver() ниже. Если Chrome ни разу не понадобится за
        # весь запуск, к нему вообще не будет ни одного обращения.

    def init_driver(self):
        """
        Подключение к уже запущенному Chrome с remote-debugging.
        webdriver.Chrome(...) с debuggerAddress может молча зависать на
        несколько минут БЕЗ каких-либо ошибок в логе — известная проблема
        самого Selenium при подключении к Chrome с неактивными вкладками
        (github.com/SeleniumHQ/selenium/issues/14906). Поэтому попытка
        выполняется в отдельном потоке, и основной поток ждёт её не дольше
        config.chrome.connect_timeout_sec — если за это время подключение не
        завершилось, пишем понятную ошибку вместо тихого зависания.
        Сама попытка в фоновом потоке при этом может доработать и позже
        (поток daemon, завершится сам, когда Selenium сдастся или подключится) —
        просто её результат уже никто не ждёт.

        ВАЖНО: раньше здесь была логика повторных попыток (retry) при
        таймауте — она была УБРАНА, потому что она сама и ломала подключение.
        Первая попытка при зависании НЕ отменяется (поток daemon продолжает
        висеть в фоне и после join(timeout)), поэтому повторная попытка
        стартовала ВТОРОЙ параллельный webdriver.Chrome(...) к тому же
        debuggerAddress, пока первый ещё не отвалился — и оба подключения
        конкурировали за один и тот же CDP-порт уже открытого Chrome, из-за
        чего зависали уже ОБА (а не только первое), причём стабильно, даже
        после полного перезапуска Chrome. Один поток на одну попытку —
        единственная схема, которая реально работала.
        """
        opts = Options()
        opts.add_experimental_option("debuggerAddress", self.config.chrome.debugger_address)
        timeout_sec = getattr(self.config.chrome, "connect_timeout_sec", 30)

        result = {}

        def _connect():
            try:
                result["driver"] = webdriver.Chrome(options=opts)
            except Exception as e:
                result["error"] = e

        thread = threading.Thread(target=_connect, daemon=True)
        thread.start()
        thread.join(timeout_sec)

        if thread.is_alive():
            self.driver = None
            msg = (
                f"Подключение к Chrome ({self.config.chrome.debugger_address}) не завершилось за "
                f"{timeout_sec} сек. Бот сам попробует подключиться заново на следующем круге."
            )
            self.logger.error(f"❌ {msg}")
            self.notify_error("init_driver", TimeoutError(msg))
            return

        if "error" in result:
            self.logger.error(f"❌ Не удалось подключиться к Chrome: {result['error']}")
            self.notify_error("init_driver", result["error"])
            self.driver = None
            return

        self.driver = result["driver"]
        self.logger.info("✅ Selenium подключен к Chrome")

    def ensure_driver(self):
        """Проверяет, что драйвер жив (например, после падения вкладки/сессии),
        и переподключается при необходимости — заменяет разбросанный по
        исходным скриптам try/except вокруг self.driver.window_handles."""
        try:
            _ = self.driver.window_handles
        except Exception:
            self.init_driver()

    def _safe_close_tab(self):
        """Не должен сам падать: вызывается из except-блоков после уже
        произошедшей ошибки (например, окно/сессия Chrome умерли целиком —
        NoSuchWindowException), и если тут вылетит необработанное исключение,
        оно прервёт вызывающий цикл по городам целиком (весь дневной проход
        майнинга фактов оборвётся на текущем городе, а все следующие города
        так и останутся не смайненными за день)."""
        try:
            if self.driver and len(self.driver.window_handles) > 1:
                self.driver.close()
                self.driver.switch_to.window(self.driver.window_handles[0])
        except Exception as e:
            self.logger.warning(f"⚠️ [_safe_close_tab] не удалось закрыть вкладку/переключиться: {e}")

    def notify_error(self, context, exc):
        if should_suppress_telegram_error(self.config, exc):
            self.logger.debug(f"🔇 [{context}] Транзиентная ошибка подавлена для Telegram (см. лог выше)")
            return
        today = datetime.now().strftime("%Y-%m-%d")
        dedup_key = (today, context)
        if dedup_key in self._notified_errors_today:
            return
        self._notified_errors_today.add(dedup_key)
        self.telegram.send_message(f"🔴 <b>Ошибка {type(self).__name__}</b>\n{context}\n{exc}")

    def _check_vpn_dropped(self, context):
        """
        Проверяет ДВА признака отвалившегося VPN на ТЕКУЩЕЙ активной вкладке
        (вызывать сразу после открытия страницы, пока вкладка ещё не
        закрыта self._safe_close_tab()):
        1. Wunderground открылся, но геоблокирован — VPN_WU_BLOCKED_TEXT
           где-то в <body>.
        2. Polymarket вообще не открылся — VPN_POLY_UNREACHABLE_TEXT в
           фиксированном месте страницы ошибки самого Chrome
           (VPN_POLY_UNREACHABLE_XPATH).
        При срабатывании — уведомление в Telegram ВСЕГДА, звонок через
        CallMeBot — если config.vpn_check.call_alert_enabled: true.
        Дедуп — не чаще раза в VPN_ALERT_COOLDOWN_SEC (по умолчанию 5 минут,
        self._vpn_last_alert_ts), чтобы не спамить телеграмом и не звонить на
        каждый снапшот подряд, пока VPN не подняли обратно, но при этом
        повторять алерт (и попытку дозвониться — снова call_max_attempts
        попыток), пока проблема не исчезла. Возвращает True, если сработал
        хотя бы один признак (вызывающая сторона решает, продолжать ли
        разбирать страницу дальше).
        """
        try:
            body_text = self.driver.find_element(By.TAG_NAME, "body").text
        except Exception:
            body_text = ""
        wu_blocked = VPN_WU_BLOCKED_TEXT in body_text

        poly_unreachable = False
        if not wu_blocked:
            try:
                span = self.driver.find_element(By.XPATH, VPN_POLY_UNREACHABLE_XPATH)
                poly_unreachable = VPN_POLY_UNREACHABLE_TEXT in span.text
            except Exception:
                poly_unreachable = False

        if not wu_blocked and not poly_unreachable:
            return False

        resource = "Wunderground" if wu_blocked else "Polymarket"
        reason = f'"{VPN_WU_BLOCKED_TEXT}"' if wu_blocked else f'"{VPN_POLY_UNREACHABLE_TEXT}"'
        self.logger.error(f"❌ [VPN] Не удалось открыть {resource} ({context}) — на странице {reason}")

        now = time.time()
        if self._vpn_last_alert_ts is not None and (now - self._vpn_last_alert_ts) < VPN_ALERT_COOLDOWN_SEC:
            return True
        self._vpn_last_alert_ts = now

        self.telegram.send_message(f"🔴 Не удалось открыть {resource}, проверьте VPN.")

        vpn_cfg = getattr(self.config, "vpn_check", None)
        if vpn_cfg and getattr(vpn_cfg, "call_alert_enabled", False):
            self._call_alert(
                "VPN dropped",
                getattr(vpn_cfg, "call_user", None),
                getattr(vpn_cfg, "call_max_attempts", 3),
                getattr(vpn_cfg, "call_retry_delay_sec", 3),
            )
        return True

    def _report_api_connection_down(self, resource):
        """
        Тот же VPN-алерт (Telegram + опционально звонок через CallMeBot), что
        и _check_vpn_dropped выше, но для НОВЫХ безбраузерных методов
        майнинга (requests к api.weather.com вместо Selenium) — сетевая
        ошибка requests не может быть проверена через текст на странице,
        поэтому отдельный вход. Дедуп и константа cooldown — ОБЩИЕ с
        _check_vpn_dropped (self._vpn_last_alert_ts / VPN_ALERT_COOLDOWN_SEC),
        чтобы оба пути не звонили и не спамили Telegram независимо друг от
        друга. Паттерн зеркалит PolymarketClobMiner._report_connection_down.
        """
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
        """Звонок через CallMeBot — тот же паттерн, что в PriceMonitor/TradingEngine
        (poly_setup_screener.py). При сетевых сбоях (например, "Read timed
        out" — CallMeBot иногда не отвечает вовремя) повторяет до
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
