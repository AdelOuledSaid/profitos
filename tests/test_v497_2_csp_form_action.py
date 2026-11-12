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


def test_le_bouton_de_raccordement_vise_bien_une_adresse_propre(app, pdp, monkeypatch):
    monkeypatch.setenv('APP_BASE_URL', 'https://veqalio.com')
    monkeypatch.setenv('LEGACY_HOSTS', 'app.profitos.fr')
    client, _org = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    page = client.get('/company').get_data(as_text=True)
    assert 'action="/facture-electronique/superpdp/relier"' in page
    directive = form_action(csp(client))
    assert 'https://app.profitos.fr' in directive and 'https://veqalio.com' in directive
