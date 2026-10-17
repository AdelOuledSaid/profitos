"""v43x — inscription (CGU obligatoires, règles du mot de passe, saisie conservée), connexion
(e-mail conservé après erreur), pages d'erreur sans menu pour un visiteur."""
import uuid

from tests.test_v421_invoice_compliance import _no_boamp, app, _post, _flashes, PASSWORD  # noqa: F401


def test_signup_requires_terms_and_keeps_input(app):
    from profitos import runtime as rt
    client = app.test_client()
    html = client.get('/signup').get_data(as_text=True)
    assert '14-DAY' not in html and 'ESSAI GRATUIT DE 14 JOURS' in html
    assert '12 caractères minimum' in html and 'name="accept_terms"' in html and '/cgu' in html and '/cgv' in html
    email = f'{uuid.uuid4().hex[:10]}@example.com'
    data = {'full_name': 'Lina Roux', 'email': email, 'password': PASSWORD, 'company_name': 'Roux SARL'}
    r = _post(client, '/signup', data)
    assert r.status_code == 302 and any('conditions générales' in m for m in _flashes(client))
    html = client.get('/signup').get_data(as_text=True)
    assert f'value="{email}"' in html and 'value="Roux SARL"' in html
    r = _post(client, '/signup', dict(data, accept_terms='1'))
    assert r.status_code == 302
    c = rt.auth_cx()
    org = c.execute('SELECT o.* FROM organizations o JOIN memberships m ON m.organization_id=o.id JOIN users u ON u.id=m.user_id WHERE u.email=?', (email,)).fetchone()
    c.close()
    assert org['terms_accepted_at'] and org['terms_version'] == '2026-10'


def test_wrong_password_keeps_email(app):
    client = app.test_client()
    _post(client, '/login', {'email': 'personne@example.com', 'password': 'mauvais'})
    html = client.get('/login').get_data(as_text=True)
    assert 'value="personne@example.com"' in html
    assert html.count('PROFITOS') == 0


def test_error_pages_for_visitors_have_no_app_menu(app):
    client = app.test_client()
    r = client.get('/page-qui-n-existe-pas')
    html = r.get_data(as_text=True)
    assert r.status_code == 404
    assert 'SECURE ERROR' not in html and 'Navigation principale' not in html and 'ERREUR 404' in html
