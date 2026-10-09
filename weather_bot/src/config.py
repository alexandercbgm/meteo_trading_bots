"""
config.py

Загружает config.yaml в объект с точечным доступом (config.paths.data_dir,
config.criteria.max_streak и т.п.), не привязываясь заранее к списку секций —
любой раздел YAML со строковыми ключами превращается в SimpleNamespace
рекурсивно; разделы со НЕ строковыми ключами (например,
weather_miner.snapshot_slots: {6: "06:00", ...}) остаются обычными dict, чтобы
не терять исходные ключи-числа.
"""

import os
from types import SimpleNamespace

import yaml


def _to_namespace(obj):
    if isinstance(obj, dict):
        if obj and not all(isinstance(k, str) for k in obj):
            return obj  # ключи не строки (напр. snapshot_slots: {6: "06:00"}) — оставляем как dict
        return SimpleNamespace(**{k: _to_namespace(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_to_namespace(v) for v in obj]
    return obj


class Config:
    """Тонкая обёртка над словарём из config.yaml с точечным доступом к любой
    секции/полю. Хранит и сырой dict (self.raw) на случай, если где-то нужен
    именно словарь (напр. для логирования всей конфигурации), и self.config_dir
    — папку, где лежит сам config.yaml (обычно config/), чтобы другие модули
    могли находить файлы рядом с ним (напр. price_monitor.watched_icaos_file)
    не завязываясь на то, откуда запущен процесс."""

    def __init__(self, path="config/config.yaml"):
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        self.raw = raw
        self.config_dir = os.path.dirname(os.path.abspath(path))
        ns = _to_namespace(raw)
        self.__dict__.update(vars(ns))

    def resolve_config_path(self, relative_path):
        """Абсолютный путь к файлу, заданному относительно папки config.yaml
        (например, значение price_monitor.watched_icaos_file)."""
        return os.path.join(self.config_dir, relative_path)

    def __repr__(self):
        return f"Config({self.raw!r})"
