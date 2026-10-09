"""
notifier.py

Класс QualifyingCitiesNotifier — форматирует и отправляет в Telegram список
городов, отобранных по критериям (criteria.CityCriteriaSelector), с посчитанными
по ним метриками и ссылками на Wunderground, Windy и сегодняшнее событие
Polymarket — тем же стилем ссылок, что и в таблице «Череда промахов» отчёта
(значение-ссылка на источник). Также сохраняет ОТПРАВЛЕННЫЕ данные в JSONL-файл
с датой в reports/ (пункт 5 ТЗ).
"""

import html
import json
import os

from .accuracy_report import polymarket_today


class QualifyingCitiesNotifier:
    def __init__(self, config, cities, telegram, logger):
        self.config = config
        self.cities = cities
        self.telegram = telegram
        self.logger = logger
        self.out_dir = config.paths.qualifying_cities_dir
        self.send_ranking_to_telegram = getattr(config.criteria, "send_ranking_to_telegram", False)

    def _format_message(self, qualifying, run_date):
        if not qualifying:
            return f"ℹ️ <b>Города по критериям за {run_date.isoformat()}</b>: подходящих городов не найдено."

        lines = [f"🏆 <b>Города по критериям за {run_date.isoformat()}</b> ({len(qualifying)}):"]
        for i, row in enumerate(qualifying, start=1):
            info = self.cities.get(row["icao"], {})
            wu_url = info.get("weather_url")
            poly_url, poly_label = polymarket_today(row["icao"], self.cities)

            wu_link = f'<a href="{wu_url}">Wunderground</a>' if wu_url else "Wunderground"
            poly_link = f'<a href="{poly_url}">Polymarket ({poly_label})</a>' if poly_url else "Polymarket"

            lines.append(
                f"{i}. <b>{html.escape(row['city'])}</b> {wu_link} | {poly_link}\n"
                f"попаданий {row['hit_rate_pct']}%, серия промахов {row['max_streak']}, "
                f"цена {row['avg_price_cents']}¢/{row['median_price_cents']}¢"
            )
        return "\n".join(lines)

    def _save_jsonl(self, qualifying, run_date):
        os.makedirs(self.out_dir, exist_ok=True)
        path = os.path.join(self.out_dir, f"qualifying_cities_{run_date.isoformat()}.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for row in qualifying:
                f.write(json.dumps({**row, "date": run_date.isoformat()}, ensure_ascii=False) + "\n")
        self.logger.info(f"💾 Сохранено {len(qualifying)} городов по критериям в {path}")
        return path

    def send_and_save(self, qualifying, run_date):
        """
        Отправляет форматированное сообщение в Telegram (если
        send_ranking_to_telegram — иначе только логирует, что отправка
        отключена) и сохраняет РОВНО ТЕ ЖЕ данные, что были бы отправлены, в
        JSONL-файл с датой (пункт 5.1/5.2 ТЗ) — сохранение НЕ зависит от
        флага, отбор по критериям и его архив ведутся всегда.
        """
        if self.send_ranking_to_telegram:
            message = self._format_message(qualifying, run_date)
            self.telegram.send_message(message)
        else:
            self.logger.info("⏭️ Отправка рейтинга городов в Telegram отключена (criteria.send_ranking_to_telegram)")
        return self._save_jsonl(qualifying, run_date)
