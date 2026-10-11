"""Mode pré-lancement : site consultable avant l'immatriculation, sans faux SIREN,
inscriptions sur liste blanche, paiement fermé, preflight assoupli sans vente."""
import importlib.util
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSRF = 'p' * 64


def _pf():
    spec = importlib.util.spec_from_file_location('pf', ROOT / 'scripts' / 'cloud_preflight.py')
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def _client():
    from profitos import create_app
    from profitos.config import DevelopmentConfig
    app = create_app(DevelopmentConfig); app.config.update(TESTING=True)
    return app.test_client()


def _post(c, url, data):
    with c.session_transaction() as s:
        s['csrf_token'] = CSRF
    d = dict(data); d['csrf_token'] = CSRF
    return c.post(url, data=d)


BASE = {'PROFITOS_ENV': 'production', 'DATABASE_URL': 'postgresql://x', 'APP_BASE_URL': 'https://app.profitos.fr',
        'PROFITOS_SECRET_KEY': 'Zx9' * 20, 'RESEND_API_KEY': 're_x'}


def test_preflight_prelaunch_needs_only_name_director_contact():
    pf = _pf()
    env = dict(BASE, PROFITOS_PRELAUNCH='1', LEGAL_COMPANY_NAME='Adel Ouled Said', LEGAL_PUBLICATION_DIRECTOR='Adel Ouled Said',
               LEGAL_CONTACT_EMAIL='contact@profitos.fr')
    assert pf.production_errors(env) == []
    assert any('pré-lancement' in w for w in pf.prelaunch_warnings(env))
    # Sans pré-lancement, l'identité complète, le disque et Stripe restent exigés.
    env.pop('PROFITOS_PRELAUNCH')
    errs = ' '.join(pf.production_errors(env))
    assert 'LEGAL_SIREN' in errs and 'PROFITOS_UPLOAD_DIR' in errs and 'Stripe' in errs


def test_legal_page_says_registration_pending_without_fake_siren(monkeypatch):
    monkeypatch.setenv('PROFITOS_PRELAUNCH', '1')
    monkeypatch.setenv('LEGAL_COMPANY_NAME', 'Adel Ouled Said')
    monkeypatch.setenv('LEGAL_PUBLICATION_DIRECTOR', 'Adel Ouled Said')
    monkeypatch.setenv('LEGAL_CONTACT_EMAIL', 'contact@profitos.fr')
    for k in ('LEGAL_SIREN', 'LEGAL_FORM', 'LEGAL_ADDRESS'):
        monkeypatch.delenv(k, raising=False)
    c = _client()
    for url in ('/mentions-legales', '/cgv', '/politique-confidentialite'):
        html = c.get(url).get_data(as_text=True)
        assert "en cours d'immatriculation" in html and 'à compléter' not in html, url


def test_prelaunch_signup_is_invite_only(monkeypatch):
    monkeypatch.setenv('PROFITOS_PRELAUNCH', '1')
    monkeypatch.setenv('PRELAUNCH_ALLOWED_EMAILS', 'moi@exemple.fr')
    c = _client()
    assert 'Accès anticipé' in c.get('/signup').get_data(as_text=True)
    r = _post(c, '/signup', {'full_name': 'X', 'email': f'{uuid.uuid4().hex[:8]}@example.com', 'company_name': 'X SAS',
                             'password': 'Une-Phrase-Assez-Longue-2026', 'accept_terms': '1'})
    assert r.status_code in (302, 303) and '/demo' in r.headers['Location']
    r = _post(c, '/signup', {'full_name': 'Moi', 'email': 'moi@exemple.fr', 'company_name': 'Moi SAS',
                             'password': 'Une-Phrase-Assez-Longue-2026', 'accept_terms': '1'})
    assert '/demo' not in r.headers.get('Location', '')


def test_checkout_closed_in_prelaunch():
    src = (ROOT / 'profitos' / 'routes' / 'account.py').read_text(encoding='utf-8')
    block = src.split('def billing_checkout():', 1)[1][:400]
    assert 'is_prelaunch()' in block
