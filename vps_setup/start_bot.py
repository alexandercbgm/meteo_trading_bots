"""Обёртка запуска: рабочая папка, sys.path, переменные из ~/.weather_bot_env, затем run_project.py."""
import os, sys, runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

env_file = Path.home() / ".weather_bot_env"
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

runpy.run_path(str(ROOT / "run_project.py"), run_name="__main__")
