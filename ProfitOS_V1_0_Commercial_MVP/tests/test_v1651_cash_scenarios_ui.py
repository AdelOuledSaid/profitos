from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def test_cash_scenarios_stylesheet_cache_busted():
    t=(ROOT/'templates'/'base.html').read_text(encoding='utf-8')
    assert "financial-brain.css'" in t

def test_cash_scenarios_chart_css_visible():
    t=(ROOT/'static'/'financial-brain.css').read_text(encoding='utf-8')
    assert '.cash-chart{' in t
    assert 'height:230px' in t
    assert '.cash-line.prudent{stroke:#ffb454}' in t
    assert '.cash-line.probable{stroke:#79a9ff}' in t
    # Le vert de marque a été harmonisé (#5fe0ac -> #3ddc84) sur l'ensemble de l'app —
    # la couleur "optimiste" reste verte, seule la nuance exacte a changé.
    assert '.cash-line.optimiste{stroke:#3ddc84}' in t
    assert '.cash-legend .legend::before' in t

def test_cash_scenarios_template_contains_svg_curves():
    t=(ROOT/'templates'/'cash_intelligence.html').read_text(encoding='utf-8')
    assert '<svg class="cash-chart"' in t
    assert 'class="cash-line {{ curve.mode }}"' in t
    assert 'points="{{ curve.points }}"' in t
