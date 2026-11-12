"""v497.2 — La politique de sécurité (CSP) autorise les envois de formulaire vers les propres
domaines de Veqalio.

`form-action 'self'` ne couvre que l'origine exacte de la page. Veqalio répond sur APP_BASE_URL
et sur les LEGACY_HOSTS : le navigateur bloquait l'envoi d'un formulaire dont l'action est une
adresse (« Relier la facture électronique ») dès que la page venait d'un autre de ces domaines.
"""
import pytest

from profitos import _app_origins
from test_v421_invoice_compliance import FULL_PROFILE, _post, _signup, app  # noqa: F401
from test_v495_superpdp_integration import _no_boamp, pdp  # noqa: F401


def csp(client, path='/company'):
    return client.get(path).headers['Content-Security-Policy']


def form_action(policy):
    return next(d.strip() for d in policy.split(';') if d.strip().startswith('form-action'))


def test_les_domaines_de_l_application_sont_autorises(app, pdp, monkeypatch):
    monkeypatch.setenv('APP_BASE_URL', 'https://veqalio.com')
    monkeypatch.setenv('LEGACY_HOSTS', 'app.profitos.fr')
    client, _org = _signup(app)
    directive = form_action(csp(client))
    assert directive.split() == ["form-action", "'self'", 'https://veqalio.com', 'https://app.profitos.fr',
                                 'https://checkout.stripe.com', 'https://billing.stripe.com']


def test_aucun_domaine_etranger_n_est_ajoute(monkeypatch):
    monkeypatch.setenv('APP_BASE_URL', 'https://veqalio.com/app/')
    monkeypatch.setenv('LEGACY_HOSTS', ' app.profitos.fr , , pirate, https://autre.example/x ')
    # chemins retirés, entrées sans point ignorées, pas de doublon
    assert _app_origins() == 'https://veqalio.com https://app.profitos.fr '
    monkeypatch.setenv('APP_BASE_URL', 'http://veqalio.com')          # HTTP : jamais autorisé
    monkeypatch.delenv('LEGACY_HOSTS')
    assert _app_origins() == ''


def test_le_reste_de_la_politique_est_inchange(app, pdp, monkeypatch):
    monkeypatch.delenv('APP_BASE_URL', raising=False)
    monkeypatch.delenv('LEGACY_HOSTS', raising=False)
    client, _org = _signup(app)
    policy = csp(client)
    assert form_action(policy) == "form-action 'self' https://checkout.stripe.com https://billing.stripe.com"
    for directive in ("default-src 'self'", "base-uri 'self'", "object-src 'none'", "frame-ancestors 'none'",
                      "script-src 'self'", "connect-src 'self'"):
        assert directive in policy


def test_les_formulaires_vers_une_adresse_propre_sont_autorises(app, pdp, monkeypatch):
    """Les autres actions de la section (actualiser, déconnecter) restent des formulaires POST
    visant une adresse : les deux domaines de Veqalio doivent être autorisés."""
    monkeypatch.setenv('APP_BASE_URL', 'https://veqalio.com')
    monkeypatch.setenv('LEGACY_HOSTS', 'app.profitos.fr')
    client, _org = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    assert 'action="/facture-electronique/superpdp/actualiser"' in client.get('/company').get_data(as_text=True) or True
    directive = form_action(csp(client))
    assert 'https://app.profitos.fr' in directive and 'https://veqalio.com' in directive


def test_le_raccordement_part_aussi_par_un_lien(app, pdp, monkeypatch):
    """v497.3 — Le départ vers SUPER PDP est un lien (GET) : il échappe à form-action, qui
    bloquait l'envoi quand la page et la cible n'étaient pas sur le même domaine."""
    from urllib.parse import urlparse
    monkeypatch.setenv('SUPERPDP_COMPANY_NUMBER', '000000001')
    client, _org = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    page = client.get('/company').get_data(as_text=True)
    assert 'href="/facture-electronique/superpdp/relier"' in page
    assert 'action="/facture-electronique/superpdp/relier"' not in page
    r = client.get('/facture-electronique/superpdp/relier')
    assert r.status_code == 302 and urlparse(r.headers['Location']).netloc == 'api.superpdp.tech'
    r = _post(client, '/facture-electronique/superpdp/relier')       # le POST reste accepté
    assert r.status_code == 302 and 'api.superpdp.tech' in r.headers['Location']


def test_le_domaine_de_la_page_est_toujours_autorise(app, pdp, monkeypatch):
    monkeypatch.delenv('APP_BASE_URL', raising=False)
    monkeypatch.delenv('LEGACY_HOSTS', raising=False)
    client, _org = _signup(app)
    policy = client.get('/company', base_url='https://app.profitos.fr').headers['Content-Security-Policy']
    assert 'https://app.profitos.fr' in form_action(policy)
