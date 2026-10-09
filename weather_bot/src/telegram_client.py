"""
telegram_client.py

Единая точка отправки сообщений в Telegram — используется майнерами (сводки,
ошибки) и QualifyingCitiesNotifier (список городов по критериям).

ВАЖНО: `requests.post(...)` сам по себе НЕ бросает исключение на ответах
Telegram вида 4xx/5xx (в отличие от сетевых сбоев вроде таймаута/обрыва
соединения) — если не проверять `resp.status_code`/`resp.ok` явно, сообщение
может быть НЕ доставлено (например, Telegram вернул 400 Bad Request из-за
неверной HTML-разметки текста, 429 Too Many Requests при частой отправке,
или чат/токен невалидны) СОВЕРШЕННО МОЛЧА — ни исключения, ни лога, вызывающий
код увидит только то, что сообщение не пришло. Это баг, который был здесь
раньше: `send_message` не проверял ответ вообще. Теперь — проверяет, логирует
тело ответа Telegram при ошибке (там обычно прямо написано, что не так,
например конкретная причина "can't parse entities") и для 429 делает до
`max_retries` повторов с паузой `retry_after`, которую подсказывает сам
Telegram.
"""

import logging
import time

import requests


class TelegramClient:
    def __init__(self, token, chat_id, logger=None):
        self.token = token
        self.chat_id = chat_id
        self.logger = logger or logging.getLogger("TelegramClient")

    def send_message(self, text, max_retries=2):
        """
        Возвращает True, если Telegram подтвердил доставку (HTTP 200 и
        {"ok": true} в теле ответа), иначе False — ЛЮБАЯ неудача (сетевая
        ошибка, невалидная разметка, рейт-лимит после исчерпания повторов)
        обязательно логируется как ошибка, молчаливых неудач больше нет.
        """
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        for attempt in range(1, max_retries + 2):  # первая попытка + max_retries повторов
            try:
                resp = requests.post(url, json=payload, timeout=15)
            except Exception as e:
                self.logger.error(f"❌ Не удалось отправить сообщение в Telegram (сетевая ошибка): {e}")
                return False

            if resp.status_code == 200:
                try:
                    if resp.json().get("ok"):
                        return True
                except Exception:
                    pass
                self.logger.error(f"❌ Telegram вернул 200, но ok!=true: {resp.text[:500]}")
                return False

            if resp.status_code == 429 and attempt <= max_retries:
                try:
                    retry_after = resp.json().get("parameters", {}).get("retry_after", 3)
                except Exception:
                    retry_after = 3
                self.logger.warning(
                    f"⚠️ Telegram 429 Too Many Requests -- жду {retry_after} сек и повторяю "
                    f"(попытка {attempt}/{max_retries})"
                )
                time.sleep(retry_after)
                continue

            # Любой другой код (400 -- обычно неверная HTML-разметка текста,
            # 403 -- бот заблокирован/удалён из чата, и т.п.) -- тело ответа
            # Telegram, как правило, прямо называет причину.
            self.logger.error(
                f"❌ Telegram API вернул {resp.status_code}, сообщение НЕ доставлено: {resp.text[:500]}"
            )
            return False

        return False
