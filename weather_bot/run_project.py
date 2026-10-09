"""
run_project.py

Автономная точка входа — запускает Orchestrator ВНЕ Jupyter/ipykernel:

    python run_project.py

Рекомендуется для по-настоящему долгих (многосуточных) непрерывных прогонов:
Jupyter-ядро не рассчитано на выполнение одной ячейки сутками напролёт — из-за
особенностей ipykernel вывод в ячейку иногда перестаёт долетать до браузера
(процесс при этом жив, лог-файлы продолжают пополняться нормально, но статус
ядра в интерфейсе может показывать "Unknown" — см. README). Обычный
консольный/фоновый процесс (например, через `start /min python run_project.py`,
Планировщик заданий Windows или просто отдельное окно консоли) этой проблемы
лишён в принципе.

run_project.ipynb остаётся удобным для интерактивной отладки отдельных
компонентов (один прогон майнера, ручной запуск отчёта и т.п.) — для них
долгий непрерывный вывод не требуется.
"""

import sys

from src.config import Config
from src.orchestrator import Orchestrator

if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config/config.yaml"
    config = Config(config_path)
    orchestrator = Orchestrator(config)
    orchestrator.run()
