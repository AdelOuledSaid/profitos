from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
R=(ROOT/"profitos"/"routes"/"swan_baas.py").read_text(encoding="utf-8")
T=(ROOT/"templates"/"swan_settings.html").read_text(encoding="utf-8")

def test_swan_monthly_income_uses_confirmed_lower_enum():
    assert "'LessThan500'" in R
    assert 'value="LessThan500"' in T
    assert 'value="LessThan1500"' not in T

def test_route_accepts_lower_income_brackets():
    assert "'Between500And1500'" in R
    assert 'value="Between500And1500"' in T

def test_swan_confirmed_upper_enum_is_preserved():
    assert "'MoreThan4500'" in R
    assert 'value="MoreThan4500"' in T
