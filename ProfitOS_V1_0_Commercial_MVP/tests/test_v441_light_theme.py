"""v441 — Thème clair lisible : boutons principaux verts, boutons secondaires clairs,
panneaux sombres (facturation, simulateur, graphiques) repeints en clair."""
from pathlib import Path

CSS = (Path(__file__).resolve().parents[1] / 'static/theme-light.css').read_text(encoding='utf-8')


def test_light_buttons_are_not_near_black():
    assert '[data-theme="light"] .button,[data-theme="light"] button{background:#0f7a4d;color:#ffffff}' in CSS
    assert '[data-theme="light"] button.btn-secondary' in CSS
    assert '[data-theme="light"] .navmode-btn{background:none' in CSS


def test_dark_only_panels_have_light_counterparts():
    for selector in ('.billing-faq details', '.billing-plan', '.decision-constraint-group', '.decision-check',
                     '.seg-tabs', '.cash-chart-wrap', '.cash-legend .legend', '.lot-form label'):
        assert f'[data-theme="light"] {selector}' in CSS, selector


def test_unlimited_quota_is_not_drawn_as_full():
    html = (Path(__file__).resolve().parents[1] / 'templates/billing.html').read_text(encoding='utf-8')
    assert html.count('is none else 100') == 5
