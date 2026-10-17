"""v445 — audit final : montants au format français dans tous les formulaires, page Gains
robuste, statuts traduits, paramètres sans faux choix de devise/langue, cloisonnement
des sociétés, date du jour à l'heure de Paris."""
import io
import re
from pathlib import Path

import pytest

from tests.test_v421_invoice_compliance import (  # noqa: F401
    _no_boamp, app, _post, _flashes, _signup, _db)

ROOT = Path(__file__).resolve().parents[1]


def test_form_float_accepts_french_amounts():
    from profitos.runtime import form_float
    assert form_float('1 500,00') == 1500.0
    assert form_float('12,5 %') == 12.5
    assert form_float('1 234 €') == 1234.0
    assert form_float('', 7) == 7.0
    assert form_float(None) == 0.0
    with pytest.raises(ValueError):
        form_float('abc')


def test_no_bare_float_on_form_amounts():
    """Les saisies de montants passent par form_float (« 1 500,00 » refusé par float())."""
    pat = re.compile(r"(?<![_\w])float\(request\.form\.get\(['\"](amount|price|budget|principal|annual_rate|"
                     r"purchase_value|monthly_payment|residual_value|unit_price)")
    for f in (ROOT / 'profitos').rglob('*.py'):
        assert not pat.search(f.read_text(encoding='utf-8')), f


def test_fr_label_filter():
    from profitos.runtime import FR_LABELS
    assert FR_LABELS.get('active', '').lower().startswith('acti')
    t = (ROOT / 'templates/vat_summary.html').read_text(encoding='utf-8')
    assert 'fr_label' in t


def test_impact_rejects_bad_input_without_crash(app):
    client, _ = _signup(app, plan='PRO')
    for data in ({'action_id': 'x', 'amount': '10'}, {'action_id': '1', 'amount': 'abc'},
                 {'action_id': '1', 'amount': '10', 'outcome_type': 'HACK'},
                 {'action_id': '1', 'amount': '-5', 'outcome_type': 'SAVED'}):
        r = _post(client, '/impact', data)
        assert r.status_code in (302, 403), data
    assert client.get('/impact').status_code in (200, 403)


def test_settings_save_and_no_fake_currency_choice(app):
    client, org_id = _signup(app)
    html = client.get('/settings').get_data(as_text=True)
    assert 'name="currency"' not in html and 'name="locale"' not in html
    r = _post(client, '/settings', {'currency': 'USD', 'locale': 'en-US', 'notifications_enabled': 'on'})
    assert r.status_code == 302
    c = _db(org_id)
    row = c.execute('SELECT currency,locale,notifications_enabled FROM app_settings WHERE id=1').fetchone()
    c.close()
    assert row['currency'] == 'EUR' and row['locale'] == 'fr-FR' and row['notifications_enabled'] == 1


def test_purchase_pdf_import_does_not_crash(app):
    client, _ = _signup(app, plan='PRO')
    r = client.post('/facturation/achats/importer-pdf',
                    data={'pdf': (io.BytesIO(b'%PDF-1.4 not really'), 'f.pdf')},
                    content_type='multipart/form-data')
    assert r.status_code < 500


def test_entity_scoping_in_purchases_source():
    s = (ROOT / 'profitos/routes/invoicing.py').read_text(encoding='utf-8')
    for m in re.finditer(r"FROM purchase_orders WHERE status IN \([^)]*\)([^\"]*)\"", s):
        assert 'entity_id IS ?' in m.group(1)


def test_user_facing_dates_use_paris_time():
    for f in (ROOT / 'profitos').rglob('*.py'):
        if f.name in ('accounting.py', 'receivables.py'):
            continue
        assert 'date.today()' not in f.read_text(encoding='utf-8'), f
