"""v443 — Centre d'aide public (/aide) généré depuis docs/guide-utilisateur.md, liens
vers le guide, offre annuelle affichée sur la page Tarifs."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _app():
    from profitos import create_app
    from profitos.config import DevelopmentConfig
    app = create_app(DevelopmentConfig)
    app.config.update(TESTING=True)
    return app


def test_help_center_is_public_and_complete():
    client = _app().test_client()
    r = client.get('/aide')
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'Guide utilisateur ProfitOS' in html
    for anchor in ('#presentation', '#facturation-clients', '#banque', '#tresorerie-scenarios', '#questions-frequentes-et-depannage'):
        assert f'href="{anchor}"' in html
    assert 'class="help-cycle"' in html and 'mailto:' in html
    assert '{%' not in html and '{{' not in html


def test_help_content_is_up_to_date_with_the_markdown_source():
    # Le HTML généré ne doit pas dériver de la source : relancer scripts/build_help.py
    # après toute modification de docs/guide-utilisateur.md.
    md = (ROOT / 'docs/guide-utilisateur.md').read_text(encoding='utf-8')
    html = (ROOT / 'templates/help_content.html').read_text(encoding='utf-8')
    for heading in [l[3:] for l in md.splitlines() if l.startswith('## ')]:
        assert heading.replace('&', '&amp;') in html, heading


def test_links_to_the_guide():
    assert "url_for('help_center')" in (ROOT / 'templates/base.html').read_text(encoding='utf-8')
    assert "url_for('help_center')" in (ROOT / 'templates/landing.html').read_text(encoding='utf-8')
    assert "url_for('help_center')" in (ROOT / 'templates/pricing.html').read_text(encoding='utf-8')


def test_pricing_page_shows_annual_offer():
    client = _app().test_client()
    html = client.get('/pricing').get_data(as_text=True)
    assert 'Annuel <small>2 mois offerts</small>' in html and 'Ou 290' in html
    html = client.get('/pricing?periode=annuel').get_data(as_text=True)
    assert '290&nbsp;€ <small>HT / an</small>' in html and '1 990&nbsp;€ <small>HT / an</small>' in html
