"""Compte Pro / cartes — intégration Swan (Banking-as-a-Service).

AVERTISSEMENT IMPORTANT : ce module est construit à partir de la
documentation publique officielle de Swan (docs.swan.io), consultée pour
cette tâche précise, mais je n'ai aucun accès réseau dans cet environnement
pour l'exécuter contre leurs vrais serveurs. Structurellement correct à ma
connaissance (authentification OAuth2, forme des requêtes GraphQL), mais
jamais vérifié en conditions réelles. À tester en sandbox Swan avant tout
usage réel — ce que je ne peux pas faire moi-même ici.

Différences structurelles importantes par rapport aux autres intégrations
de cette session (Dropbox/Google Drive, en REST) :
  - Swan expose une API GraphQL unique (pas de collection d'endpoints REST).
  - Deux environnements totalement séparés (sandbox / live) : jetons,
    identifiants et données ne se transposent jamais de l'un à l'autre.
  - Toute mutation sensible (émettre une carte, ouvrir un compte, lancer un
    virement) renvoie un CONSENTEMENT — une URL hébergée par Swan que
    l'utilisateur humain doit approuver avant que l'opération s'exécute
    réellement. Ce module ne fait jamais l'hypothèse qu'une mutation a
    réussi tant que ce consentement n'a pas été validé.

Ce module reste volontairement conservateur : il peut lister les comptes
existants (lecture), et DEMANDER l'ouverture d'un compte ou l'émission
d'une carte (ce qui renvoie un lien de consentement à faire approuver par
un humain) — jamais une opération qui déplacerait réellement de l'argent
sans validation explicite Swan elle-même.

Configuration requise (variables d'environnement) :
  SWAN_CLIENT_ID, SWAN_CLIENT_SECRET, SWAN_ENVIRONMENT (sandbox|live,
  défaut sandbox)
"""
import os

import requests

TOKEN_URL = 'https://oauth.swan.io/oauth2'

GRAPHQL_URLS = {
    'sandbox': 'https://api.swan.io/sandbox-partner/graphql',
    'live': 'https://api.swan.io/live-partner/graphql',
}


def is_configured():
    return bool(os.environ.get('SWAN_CLIENT_ID') and os.environ.get('SWAN_CLIENT_SECRET'))


def current_environment():
    env = (os.environ.get('SWAN_ENVIRONMENT') or 'sandbox').strip().lower()
    return env if env in ('sandbox', 'live') else 'sandbox'


def get_server_token():
    """Jeton serveur-à-serveur (client credentials) — pour les opérations
    portées par l'organisation elle-même, pas au nom d'un utilisateur final.
    Lève ValueError avec un message clair en cas d'échec."""
    client_id = os.environ.get('SWAN_CLIENT_ID')
    client_secret = os.environ.get('SWAN_CLIENT_SECRET')
    try:
        resp = requests.post(
            TOKEN_URL,
            data={'grant_type': 'client_credentials', 'client_id': client_id, 'client_secret': client_secret},
            timeout=20,
        )
    except requests.RequestException as e:
        raise ValueError(f"Connexion au serveur Swan impossible : {e}") from e
    if resp.status_code != 200:
        raise ValueError(f"Échec de l'authentification Swan ({resp.status_code}) : {resp.text[:200]}")
    payload = resp.json()
    token = payload.get('access_token')
    if not token:
        raise ValueError("Swan n'a renvoyé aucun jeton d'accès.")
    return token


def graphql_query(token, query, variables=None, environment=None):
    """Exécute une requête ou mutation GraphQL contre l'API partenaire Swan.
    Lève ValueError en cas d'erreur réseau, HTTP, ou d'erreurs GraphQL
    renvoyées dans le corps de la réponse (jamais ignorées silencieusement)."""
    env = environment or current_environment()
    url = GRAPHQL_URLS[env]
    try:
        resp = requests.post(
            url,
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
            json={'query': query, 'variables': variables or {}},
            timeout=30,
        )
    except requests.RequestException as e:
        raise ValueError(f"Connexion à l'API Swan impossible : {e}") from e
    if resp.status_code != 200:
        raise ValueError(f"Appel Swan échoué ({resp.status_code}) : {resp.text[:300]}")
    payload = resp.json()
    if payload.get('errors'):
        messages = '; '.join(e.get('message', str(e)) for e in payload['errors'])
        raise ValueError(f"Erreur GraphQL Swan : {messages}")
    return payload.get('data', {})


def list_accounts(token, environment=None):
    """Liste les comptes existants (lecture seule) — id et IBAN."""
    query = """
    query ProfitOSListAccounts {
      accounts {
        edges {
          node {
            id
            IBAN
            name
            status
          }
        }
      }
    }
    """
    data = graphql_query(token, query, environment=environment)
    edges = (data.get('accounts') or {}).get('edges') or []
    return [e['node'] for e in edges]


def request_new_account(token, name, environment=None):
    """Demande l'ouverture d'un nouveau compte. Renvoie {account_id,
    consent_url} — le compte n'est PAS actif tant que le consentement
    renvoyé n'a pas été approuvé par un humain sur l'URL Swan fournie.
    Structure de mutation non vérifiée contre l'API réelle — à valider en
    sandbox avant tout usage."""
    mutation = """
    mutation ProfitOSCreateAccount($name: String!) {
      createAccount(input: { name: $name }) {
        ... on CreateAccountSuccessPayload {
          account { id name status }
          consent { id consentUrl }
        }
        ... on Error {
          message
        }
      }
    }
    """
    data = graphql_query(token, mutation, variables={'name': name}, environment=environment)
    result = data.get('createAccount') or {}
    if 'message' in result:
        raise ValueError(f"Swan a refusé la demande de compte : {result['message']}")
    account = result.get('account') or {}
    consent = result.get('consent') or {}
    return {
        'account_id': account.get('id'),
        'status': account.get('status'),
        'consent_url': consent.get('consentUrl'),
    }


def request_card(token, swan_account_id, holder_name, environment=None):
    """Demande l'émission d'une carte pour un compte existant. Renvoie
    {card_id, consent_url} — la carte n'est PAS active tant que le
    consentement n'a pas été approuvé. Structure de mutation non vérifiée
    contre l'API réelle — à valider en sandbox avant tout usage."""
    mutation = """
    mutation ProfitOSAddCard($accountId: ID!, $holderName: String!) {
      addCards(input: { accountId: $accountId, cards: [{ cardholderName: $holderName }] }) {
        ... on AddCardsSuccessPayload {
          cards { id status }
          consent { id consentUrl }
        }
        ... on Error {
          message
        }
      }
    }
    """
    data = graphql_query(
        token, mutation,
        variables={'accountId': swan_account_id, 'holderName': holder_name},
        environment=environment,
    )
    result = data.get('addCards') or {}
    if 'message' in result:
        raise ValueError(f"Swan a refusé la demande de carte : {result['message']}")
    cards = result.get('cards') or []
    consent = result.get('consent') or {}
    return {
        'card_id': cards[0]['id'] if cards else None,
        'status': cards[0]['status'] if cards else None,
        'consent_url': consent.get('consentUrl'),
    }
