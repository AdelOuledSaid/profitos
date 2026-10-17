"""Démarrage : l'onboarding demande l'identité légale (SIRET, adresse, TVA) avec les mêmes
contrôles que le profil entreprise, et « Profil entreprise complété » n'est coché que si
toutes les mentions nécessaires pour facturer sont présentes."""
import uuid
import pytest

CSRF = 'o' * 64


@pytest.fixture()
def client():
    from profitos import create_app
    from profitos.config import DevelopmentConfig
    app = create_app(DevelopmentConfig)
    app.config.update(TESTING=True)
    c = app.test_client()
    _post(c, '/signup', {'full_name': 'Lea Martin', 'email': f'{uuid.uuid4().hex[:10]}@example.com',
                         'password': 'Tres-Long-Mot-De-Passe-2026', 'company_name': 'Demarrage SAS',
                         'accept_terms': '1', 'cgu': '1', 'cgv': '1', 'terms': '1'})
    return c


def _post(c, url, data):
    with c.session_transaction() as s:
        s['csrf_token'] = CSRF
    d = dict(data); d['csrf_token'] = CSRF
    return c.post(url, data=d)


def test_missing_fields_helper():
    from profitos.company_profile import missing_mandatory_fields
    assert 'SIRET' in missing_mandatory_fields({'name': 'X'})
    full = {'name': 'Atelier', 'legal_form': 'SAS', 'siret': '73282932000074', 'address': '1 rue A',
            'postal_code': '69002', 'city': 'Lyon', 'vat_number': 'FR44732829320', 'share_capital': '1000'}
    assert missing_mandatory_fields(full) == []
    assert missing_mandatory_fields(dict(full, vat_number='', vat_regime='franchise_293B')) == []


def test_onboarding_rejects_invalid_siret_and_keeps_values(client):
    r = _post(client, '/onboarding', {'company_name': 'Atelier Test', 'siret': '1234', 'city': 'Lyon',
                                      'address': '1 rue A', 'postal_code': '69002'})
    html = r.get_data(as_text=True)
    assert r.status_code == 400
    assert 'Atelier Test' in html and 'Vérifiez les champs suivants' in html


def test_onboarding_saves_identity(client):
    r = _post(client, '/onboarding', {'company_name': 'Atelier Valide', 'legal_form': 'SAS', 'siret': '73282932000074',
                                      'vat_number': 'FR44732829320', 'address': '18 rue de la Charité',
                                      'postal_code': '69002', 'city': 'Lyon', 'share_capital': '10000', 'rcs_city': 'Lyon'})
    assert r.status_code in (302, 303)
