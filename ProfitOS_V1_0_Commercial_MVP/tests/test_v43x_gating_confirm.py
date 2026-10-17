"""v43x — E2/E3 : fonctions non activées masquées, promesses de la page d'accueil exactes,
confirmation des actions irréversibles, titres de page, benchmark sur consentement."""
import re
from pathlib import Path

from tests.test_v421_invoice_compliance import (  # noqa: F401
    _no_boamp, app, _post, _flashes, _signup, _db, _create_invoice, FULL_PROFILE)

ROOT = Path(__file__).resolve().parents[1]


def test_gocardless_hidden_unless_enabled(app, monkeypatch):
    monkeypatch.delenv('GOCARDLESS_ENABLED', raising=False)
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    inv_id = _create_invoice(client, org_id)
    _post(client, f'/facturation/{inv_id}/marquer-emise')
    html = client.get(f'/facturation/{inv_id}').get_data(as_text=True)
    assert 'GoCardless' not in html and 'prelevement' not in html
    assert _post(client, f'/facturation/{inv_id}/prelevement').status_code == 404
    monkeypatch.setenv('GOCARDLESS_ENABLED', '1')
    assert 'Prélever via GoCardless' in client.get(f'/facturation/{inv_id}').get_data(as_text=True)
    r = _post(client, f'/facturation/{inv_id}/prelevement')
    assert r.status_code == 302 and not any('GOCARDLESS_ACCESS_TOKEN' in m for m in _flashes(client))


def test_supplier_inbox_shows_coming_soon_without_webhook(app, monkeypatch):
    monkeypatch.delenv('SUPPLIER_INBOX_WEBHOOK_ENABLED', raising=False)
    client, _ = _signup(app)
    from flask import url_for
    with app.test_request_context():
        url = url_for('purchase_inbox_settings')
    html = client.get(url).get_data(as_text=True)
    assert 'Bientôt disponible' in html and 'achats+' not in html
    monkeypatch.setenv('SUPPLIER_INBOX_WEBHOOK_ENABLED', '1')
    assert 'achats+' in client.get(url).get_data(as_text=True)


def test_landing_claims_are_conditional():
    t = (ROOT / 'templates/landing.html').read_text(encoding='utf-8')
    assert 'GoCardless' not in t and 'Factur-X natif' not in t and 'Plateforme agréée connectée' not in t
    assert 'Multi-entités' not in t and 'Multi-sociétés' in t
    assert 'Open Banking via Powens' not in t and 'disponible sur demande' in t


def test_irreversible_actions_require_confirmation():
    js = (ROOT / 'static/app.js').read_text(encoding='utf-8')
    assert "closest('[data-confirm]')" in js and 'window.confirm' in js
    detail = (ROOT / 'templates/invoicing_detail.html').read_text(encoding='utf-8')
    for label in ('Émettre et envoyer', 'Marquer comme émise', 'Annuler le brouillon', 'Marquer comme payée'):
        assert re.search(r'data-confirm="[^"]+">' + re.escape(label), detail), label
    team = (ROOT / 'templates/team.html').read_text(encoding='utf-8')
    assert re.search(r'data-confirm="[^"]+">Retirer', team) and re.search(r'data-confirm="[^"]+">Révoquer', team)
    assert 'data-confirm' in (ROOT / 'templates/delete_account.html').read_text(encoding='utf-8')
    assert 'data-confirm' in (ROOT / 'templates/accounting_closure.html').read_text(encoding='utf-8')
    for tpl in ROOT.glob('templates/*.html'):
        assert ' onclick=' not in tpl.read_text(encoding='utf-8'), tpl.name


def test_pages_have_distinct_titles_and_skip_link(app):
    client, _ = _signup(app)
    titles = {}
    for url in ('/', '/facturation', '/settings', '/recover', '/team'):
        html = client.get(url).get_data(as_text=True)
        m = re.search(r'<title>([^<]+)</title>', html)
        titles[url] = m.group(1)
        assert 'Aller au contenu' in html and 'id="contenu"' in html
    assert titles['/'] == 'Tableau de bord — ProfitOS'
    assert len(set(titles.values())) == len(titles)


def test_settings_french_labels_and_benchmark_opt_in(app):
    from profitos import runtime as rt
    client, org_id = _signup(app, plan='MULTI')
    html = client.get('/settings').get_data(as_text=True)
    assert 'Activity Log' not in html and "Journal d'activité" in html
    assert 'Multi-sociétés' in html and 'Propriétaire' in html and 'benchmark_opt_in' in html
    assert re.search(r'\d{2}/\d{2}/\d{4} \d{2}:\d{2}', html)
    assert not rt.benchmark_opted_in(org_id)
    rt.sync_sector_dso(org_id, 30)
    c = rt.auth_cx()
    assert c.execute('SELECT COUNT(*) n FROM sector_dso_signals WHERE organization_id=?', (org_id,)).fetchone()['n'] == 0
    c.close()
    assert rt.sector_dso_benchmark(org_id) is None
    _post(client, '/settings', {'currency': 'EUR', 'locale': 'fr-FR', 'benchmark_opt_in': 'on'})
    assert rt.benchmark_opted_in(org_id)


def test_security_events_are_french(app):
    client, org_id = _signup(app)
    html = client.get('/settings/security-events').get_data(as_text=True)
    assert 'LOGIN_SUCCESS' not in html and 'Date UTC' not in html
