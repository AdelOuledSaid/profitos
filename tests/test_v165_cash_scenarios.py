from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_v165_version():
    assert 'APP_VERSION = "1.6.9"' in (ROOT/'profitos/config.py').read_text(encoding='utf-8')

def test_three_cash_scenarios_present():
    t=(ROOT/'profitos/routes/cash_intelligence.py').read_text(encoding='utf-8')
    assert "('prudent','probable','optimiste')" in t
    assert 'def _simulate_curve' in t
    assert 'def _curve_points' in t

def test_what_if_choices_present():
    t=(ROOT/'templates/cash_intelligence.html').read_text(encoding='utf-8')
    assert 'Pas d\'encaissement sur 90 jours' in t
    assert 'Sous 30 jours' in t
    assert 'POINT BAS' in t

def test_svg_chart_without_inline_handlers():
    t=(ROOT/'templates/cash_intelligence.html').read_text(encoding='utf-8')
    assert '<svg class="cash-chart"' in t
    assert 'onclick=' not in t.lower()
