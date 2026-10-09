"""
pyramid.py

Единая формула роста лота пирамиды — её используют и бот (trader.py, режим "double"), и бэктест
в отчётах (accuracy_report.py), чтобы они не расходились.

Лот ставки с номером k в серии (k = число промахов подряд до неё, k = 0 — первая ставка) равен
lot_size * pyramid_factor(k, ...). Прогрессия (price_monitor.<набор>.pyramid_progression):

  "power"      -- m^k:                    m=2 -> 1, 2, 4, 8, 16;   m=3 -> 1, 3, 9, 27
  "cumulative" -- 1 + m + ... + m^k:      m=2 -> 1, 3, 7, 15, 31;  m=3 -> 1, 4, 13, 40
                  (каждая ставка = предыдущая * m + 1 -- лот покрывает всю серию)
  "custom"     -- явный список pyramid_custom_steps, например [1, 3, 7, 15]; за пределами списка --
                  последний элемент.
"""

PROGRESSIONS = ("power", "cumulative", "custom")


def validate_progression(name, custom_steps, where="pyramid"):
    name = str(name or "power").strip().lower()
    if name not in PROGRESSIONS:
        raise ValueError(f"{where}.pyramid_progression='{name}' -- допустимо только одно из {', '.join(PROGRESSIONS)}")
    steps = None
    if name == "custom":
        try:
            steps = [float(x) for x in (custom_steps or [])]
        except (TypeError, ValueError):
            steps = []
        if not steps or any(x <= 0 for x in steps):
            raise ValueError(f"{where}.pyramid_custom_steps должен быть непустым списком положительных чисел "
                             f"(например [1, 3, 7, 15]) при pyramid_progression: custom")
    return name, steps


def pyramid_factor(misses, progression="power", multiplier=2.0, custom_steps=None):
    """Во сколько раз лот ставки с номером misses (с нуля) больше стартового lot_size."""
    k = max(0, int(misses))
    if progression == "custom" and custom_steps:
        return float(custom_steps[min(k, len(custom_steps) - 1)])
    m = float(multiplier)
    if progression == "cumulative":
        return float(k + 1) if m == 1 else (m ** (k + 1) - 1) / (m - 1)
    return m ** k
