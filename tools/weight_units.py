# tools/weight_units.py
"""Weight unit conversion with one consistent rounding rule.

Garmin stores strength loads in kilograms. lb input is converted to kg rounded
to 2 decimals (115 lb -> 52.16 kg, matching what the library already holds);
kg is shown in lb rounded to 1 decimal (52.16 kg -> 115.0 lb).
"""

KG_PER_LB = 0.45359237


def lb_to_kg(lb: float) -> float:
    return round(float(lb) * KG_PER_LB, 2)


def kg_to_lb(kg: float) -> float:
    return round(float(kg) / KG_PER_LB, 1)


def grams_to_kg(g: float) -> float:
    return round(float(g) / 1000.0, 2)
