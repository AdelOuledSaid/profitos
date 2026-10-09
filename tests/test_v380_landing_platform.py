"""Page d'accueil v2 : toute la plateforme présentée par domaine, avec de vraies captures."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LANDING = (ROOT / 'templates/landing.html').read_text(encoding='utf-8')


def test_landing_presents_every_domain_with_a_real_screenshot():
    for name in ('encaisser', 'payer', 'piloter', 'comptabiliser', 'developper'):
        assert f'id="domaine-{name}"' in LANDING
        assert f'data-tab="{name}"' in LANDING
        assert f"filename='landing/{name}.webp'" in LANDING
        assert (ROOT / f'static/landing/{name}.webp').stat().st_size > 5000
    # Les captures sont signalées comme données de démonstration
    assert LANDING.count('données de démonstration') >= 5


def test_landing_sections_reform_audiences_integrations():
    for anchor in ('facture-electronique', 'pour-qui', 'integrations', 'plateforme'):
        assert f'id="{anchor}"' in LANDING
        assert f'href="#{anchor}"' in LANDING
    for word in ('Factur-X', 'plateforme agréée', 'Powens', 'GoCardless', 'Silae', 'FEC'):
        assert word in LANDING
    # Pas de connecteur comptable présenté comme disponible
    assert 'prochainement' in LANDING


def test_landing_tabs_work_without_inline_script():
    assert '<script>' not in LANDING and 'onclick=' not in LANDING
    js = (ROOT / 'static/app.js').read_text(encoding='utf-8')
    assert "querySelector('[data-tabs]')" in js
    # Sans JS, la liste d'onglets reste masquée et tous les domaines s'affichent
    assert re.search(r'role="tablist"[^>]*hidden', LANDING)


def test_landing_renders():
    from profitos import create_app
    from profitos.config import DevelopmentConfig
    app = create_app(DevelopmentConfig)
    app.config.update(TESTING=True)
    html = app.test_client().get('/').get_data(as_text=True)
    assert 'Cinq métiers de la finance' in html
    assert '/static/landing/payer.webp' in html


def test_purchase_amounts_use_french_format():
    for tpl in ('purchase_list', 'supplier_debts', 'supplier_payment_forecast', 'purchase_analytics', 'banking'):
        t = (ROOT / f'templates/{tpl}.html').read_text(encoding='utf-8')
        assert not re.search(r"'%\.\df'\|format\([^)]*\)\s*\}\}(&nbsp;| )?€", t), tpl
