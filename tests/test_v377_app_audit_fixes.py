"""Correctifs issus de l'audit complet de l'application (07/10/2026).

- Révision : routes d'invitation comptable jamais enregistrées (code mort après un return),
  now_iso() inexistant, LEFT() absent sous SQLite.
- E-reporting WeInvoice : helpers _json_or_empty / _api_error_message inexistants.
- Factures : taux de TVA, quantités et total négatif non contrôlés ; totaux non arrondis.
- Équipe : un identifiant d'entité altéré provoquait une erreur 500.
"""
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]


def _app():
    from profitos import create_app
    from profitos.config import DevelopmentConfig
    return create_app(DevelopmentConfig)


def test_accountant_invitation_routes_are_registered():
    endpoints = {r.endpoint for r in _app().url_map.iter_rules()}
    assert {'accountant_invite_create', 'accountant_invite_accept', 'accountant_invite_revoke'} <= endpoints


def test_every_url_for_in_live_templates_targets_an_existing_endpoint():
    import re
    endpoints = {r.endpoint for r in _app().url_map.iter_rules()}
    missing = []
    files = [p for p in (ROOT / 'templates').rglob('*.html') if '_phase2' not in p.parts]
    files += list((ROOT / 'profitos').rglob('*.py'))
    for path in files:
        for m in re.finditer(r"url_for\(\s*['\"]([\w.]+)['\"]", path.read_text(encoding='utf-8')):
            if m.group(1) not in endpoints:
                missing.append(f"{path.relative_to(ROOT)}: {m.group(1)}")
    assert not missing, missing


def test_reviews_no_longer_call_undefined_now_iso():
    assert 'now_iso(' not in (ROOT / 'profitos/routes/reviews.py').read_text(encoding='utf-8')


def test_sqlite_connection_supports_postgres_left():
    from profitos.db import SQLiteConnection
    c = SQLiteConnection(':memory:')
    assert c.execute("SELECT LEFT('411000',3)").fetchone()[0] == '411'
    assert c.execute("SELECT LEFT(NULL,3)").fetchone()[0] is None
    c.close()


def test_ereporting_helpers_exist_and_network_errors_are_api_errors(monkeypatch):
    from profitos import weinvoice as wi

    class Resp:
        status_code = 422
        text = 'bad'
        def json(self):
            return {'message': 'flowType inconnu'}

    assert wi._json_or_empty(Resp()) == {'message': 'flowType inconnu'}
    assert 'flowType inconnu' in wi._api_error_message(Resp(), {'message': 'flowType inconnu'}, 'Test')

    monkeypatch.setattr(wi, 'fetch_access_token', lambda **k: 'tok')

    def boom(*a, **k):
        raise requests.ConnectionError('down')
    monkeypatch.setattr(wi.requests, 'request', boom)
    with pytest.raises(wi.WeInvoiceAPIError):
        wi.list_ereporting_transmissions('org')


def _items(*rows):
    return [{'label': l, 'qty': q, 'unit_price': p, 'vat_rate': v, 'line_total': q * p} for l, q, p, v in rows]


def test_line_items_validation():
    from profitos.routes.invoicing import _line_items_error
    assert _line_items_error(_items(('A', 1, 100, 20))) is None
    assert _line_items_error(_items(('Livre', 2, 50, 5.5), ('Remise', 1, -10, 5.5))) is None
    assert 'TVA' in _line_items_error(_items(('A', 1, 100, 999)))
    assert 'TVA' in _line_items_error(_items(('A', 1, 100, 7)))
    assert 'Quantité' in _line_items_error(_items(('A', 0, 100, 20)))
    assert 'avoir' in _line_items_error(_items(('A', 1, -100, 20)))


def test_invoice_totals_are_rounded_to_the_cent():
    from profitos.routes.invoicing import _totals
    assert _totals(_items(('A', 1, 10.33, 5.5), ('B', 3, 0.1, 20))) == (10.63, 0.63, 11.26)


def test_team_entity_access_ignores_unknown_entity_ids():
    src = (ROOT / 'profitos/routes/account.py').read_text(encoding='utf-8')
    block = src[src.index('def team_entity_access'):]
    assert "if not v or v in known" in block
