"""
watched_icaos.py

Загрузка config/watched_icaos_weather_forecast.yaml — по каждому городу из наблюдаемого списка
(и PriceMonitor, и второй отчёт AccuracyReportBuilder.build_watched_report):
источник прогноза, поправка к прогнозу (в "ставках"/бакетах), минимальная
цена ставки для сигнала и опциональный комментарий в уведомление.

Файл содержит блок `defaults` (общие параметры для всех городов) и
`watched_icaos` (по городу) — любое поле города, оставленное пустым,
наследует значение из defaults; заданное явно — переопределяет его только
для этого города.
"""

import yaml


def _normalize_source(value):
    v = (value or "").strip().lower()
    return "windy" if v == "windy" else "wunderground"


def _normalize_strategy(value):
    v = (value or "").strip().lower()
    return "pyramid" if v == "pyramid" else "flat"


def load_watched_icaos(path, loss_limit=None):
    """
    loss_limit -- значение по умолчанию для всех городов файла: сколько промахов
    подряд допускает пирамида, прежде чем серия сбрасывается. Живёт в конфиге
    СТРАТЕГИИ (config.yaml: price_monitor.<набор>.loss_limit), вызывающий код
    передаёт его сюда; в файле городов его больше задавать не нужно. Если же
    у города (или в блоке defaults файла) loss_limit всё-таки указан явно --
    он переопределяет значение стратегии только для этого города.

    {icao: {"city": str, "strategy": "flat"|"pyramid", "loss_limit": int|None,
             "weather_source": "wunderground"|"windy", "weather_corr": int,
             "min_price_limit": float|None, "max_price_limit": float|None,
             "comment": str}}

    Наследование из defaults: поле города, оставленное пустым/не заданным
    (YAML `null`), берётся из блока `defaults` файла; если и в defaults оно
    не задано — используется единый запасной вариант (см. ниже по каждому
    полю). Явно заданное у города значение (даже 0 или "") всегда
    переопределяет defaults для этого поля.

    Запасные варианты, если поле не задано НИ у города, НИ в defaults:
      - strategy: "" -> "flat" (обычная — покупаем по сигналу, без
        пирамидирования); "pyramid" (в любом регистре) -> "pyramid".
      - loss_limit: -> аргумент loss_limit функции (из конфига стратегии); если
        и он не передан -- None (актуально только для strategy "pyramid"; для
        "flat" игнорируется, даже если зачем-то указано).
      - weather_source: "" -> "wunderground" (источник по умолчанию); "Windy"
        (в любом регистре) -> "windy". Файл watched_icaos_max_bet.yaml вообще
        не задаёт это поле ни у одного города, ни в defaults — тогда всем
        достаётся этот же запасной вариант ("wunderground"), но реально не
        используется (у max_bet таргет не привязан к прогнозу вовсе).
      - weather_corr: -> 0 (без поправки).
      - min_price_limit: -> None — ЭТО НЕ "нет сигналов": None означает
        "ограничения снизу по цене нет" и на сигнал НИКАК не влияет —
        проверяется только max_price_limit (см. price_monitor.py). Именно
        поэтому в defaults этого файла min_price_limit оставлен пустым: по
        умолчанию нижнего порога нет ни у одного города, если явно не задан.
      - max_price_limit: -> None — аналогично, "потолка сверху нет"; на
        практике почти всегда есть значение через defaults (там 66).
      - comment: -> "" (пусто — не добавляется в уведомление).
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    defaults_raw = raw.get("defaults") or {}
    default_strategy = _normalize_strategy(defaults_raw.get("strategy"))
    default_loss_limit = defaults_raw.get("loss_limit")
    if default_loss_limit in (None, ""):
        default_loss_limit = int(loss_limit) if loss_limit not in (None, "") else None
    default_weather_source = _normalize_source(defaults_raw.get("weather_source"))
    default_weather_corr = int(defaults_raw.get("weather_corr") or 0)
    default_min_price_limit = defaults_raw.get("min_price_limit")  # None, если тоже не задано
    default_max_price_limit = defaults_raw.get("max_price_limit")  # None, если тоже не задано
    default_comment = (defaults_raw.get("comment") or "").strip()

    entries = raw.get("watched_icaos") or {}
    result = {}
    for icao, cfg in entries.items():
        cfg = cfg or {}

        strategy_raw = cfg.get("strategy")
        strategy = _normalize_strategy(strategy_raw) if strategy_raw else default_strategy

        loss_limit_raw = cfg.get("loss_limit")
        loss_limit = int(loss_limit_raw) if loss_limit_raw not in (None, "") else default_loss_limit

        source_raw = cfg.get("weather_source")
        weather_source = _normalize_source(source_raw) if source_raw else default_weather_source

        corr_raw = cfg.get("weather_corr")
        weather_corr = int(corr_raw) if corr_raw not in (None, "") else default_weather_corr

        min_price_raw = cfg.get("min_price_limit")
        min_price_limit = min_price_raw if min_price_raw not in (None, "") else default_min_price_limit

        max_price_raw = cfg.get("max_price_limit")
        max_price_limit = max_price_raw if max_price_raw not in (None, "") else default_max_price_limit

        comment_raw = cfg.get("comment")
        comment = comment_raw.strip() if comment_raw else default_comment

        result[icao] = {
            "city": cfg.get("city") or icao,
            "strategy": strategy,
            "loss_limit": loss_limit,
            "weather_source": weather_source,
            "weather_corr": weather_corr,
            "min_price_limit": min_price_limit,
            "max_price_limit": max_price_limit,
            "comment": comment,
        }
    return result
