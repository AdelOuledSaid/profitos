"""Prélèvement client — intégration GoCardless (SEPA Direct Debit).

AVERTISSEMENT : construit à partir de la documentation publique GoCardless
telle que je la connais, jamais vérifié contre leurs vrais serveurs (aucun
accès réseau dans cet environnement). Structure de flux correcte à ma
connaissance (redirect flow pour le mandat, création de paiement contre un
mandat existant), mais à valider en sandbox GoCardless avant tout usage réel.

Flux GoCardless pour collecter un paiement récurrent/ponctuel par
prélèvement :
  1. Le client autorise un mandat de prélèvement via un « redirect flow » —
     il est envoyé sur une page hébergée par GoCardless pour saisir son
     IBAN et consentir, puis revient sur une URL de redirection fournie.
  2. Une fois le redirect flow complété, on le « complète » côté serveur
     pour obtenir l'identifiant du mandat actif.
  3. Chaque prélèvement se fait ensuite en créant un paiement référençant
     ce mandat — jamais de saisie IBAN répétée.

Configuration requise : GOCARDLESS_ACCESS_TOKEN, GOCARDLESS_ENVIRONMENT
(sandbox|live, défaut sandbox).
"""
import os

import requests

BASE_URLS = {
    'sandbox': 'https://api-sandbox.gocardless.com',
    'live': 'https://api.gocardless.com',
}
API_VERSION = '2015-07-06'


def is_configured():
    return bool(os.environ.get('GOCARDLESS_ACCESS_TOKEN'))


def current_environment():
    env = (os.environ.get('GOCARDLESS_ENVIRONMENT') or 'sandbox').strip().lower()
    return env if env in ('sandbox', 'live') else 'sandbox'


def _headers():
    token = os.environ.get('GOCARDLESS_ACCESS_TOKEN')
    return {
        'Authorization': f'Bearer {token}',
        'GoCardless-Version': API_VERSION,
        'Content-Type': 'application/json',
    }


def _base_url(environment=None):
    return BASE_URLS[environment or current_environment()]


def _request(method, path, json_body=None, environment=None):
    url = f"{_base_url(environment)}{path}"
    try:
        resp = requests.request(method, url, headers=_headers(), json=json_body, timeout=20)
    except requests.RequestException as e:
        raise ValueError(f"Connexion à GoCardless impossible : {e}") from e
    if resp.status_code not in (200, 201):
        raise ValueError(f"Appel GoCardless échoué ({resp.status_code}) : {resp.text[:300]}")
    return resp.json()


def create_redirect_flow(client_name, client_email, description, redirect_uri, session_token, environment=None):
    """Démarre le parcours d'autorisation de mandat — renvoie l'URL
    hébergée par GoCardless vers laquelle rediriger le client. session_token
    est une valeur aléatoire propre à ProfitOS, revérifiée à la complétion
    du flow pour éviter qu'un tiers ne le détourne."""
    payload = {
        'redirect_flows': {
            'description': description,
            'session_token': session_token,
            'success_redirect_url': redirect_uri,
            'prefilled_customer': {'given_name': client_name, 'email': client_email} if client_email else {},
        }
    }
    data = _request('POST', '/redirect_flows', payload, environment)
    flow = data.get('redirect_flows', {})
    return {'id': flow.get('id'), 'authorization_url': flow.get('redirect_url')}


def complete_redirect_flow(flow_id, session_token, environment=None):
    """Complète le flow après retour du client — renvoie les identifiants
    du client et du mandat GoCardless désormais actifs."""
    payload = {'data': {'session_token': session_token}}
    data = _request('POST', f'/redirect_flows/{flow_id}/actions/complete', payload, environment)
    flow = data.get('redirect_flows', {})
    links = flow.get('links', {})
    return {'customer_id': links.get('customer'), 'mandate_id': links.get('mandate')}


def create_payment(mandate_id, amount_cents, currency, description, environment=None):
    """Crée un prélèvement contre un mandat déjà actif. amount_cents est en
    centimes (convention GoCardless, comme la plupart des API de paiement) —
    jamais des euros directement, pour éviter une erreur d'un facteur 100."""
    payload = {
        'payments': {
            'amount': int(amount_cents),
            'currency': currency,
            'description': description,
            'links': {'mandate': mandate_id},
        }
    }
    data = _request('POST', '/payments', payload, environment)
    payment = data.get('payments', {})
    return {'id': payment.get('id'), 'status': payment.get('status')}


def get_payment(payment_id, environment=None):
    data = _request('GET', f'/payments/{payment_id}', environment=environment)
    return data.get('payments', {})
