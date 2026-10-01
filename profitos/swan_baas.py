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

TOKEN_URL = 'https://oauth.swan.io/oauth2/token'

GRAPHQL_URLS = {
    'sandbox': 'https://api.swan.io/sandbox-partner/graphql',
    'live': 'https://api.swan.io/live-partner/graphql',
}


def is_configured():
    return bool(os.environ.get('SWAN_CLIENT_ID') and os.environ.get('SWAN_CLIENT_SECRET'))


def write_operations_enabled():
    # Les mutations compte/carte de cette base n'ont pas encore été validées
    # contre un projet Swan Sandbox. Fail-closed jusqu'à validation explicite.
    return (os.environ.get('SWAN_ENABLE_ACCOUNT_CARD_REQUESTS') or '').strip().lower() in ('1','true','yes','on')


def current_environment():
    env = (os.environ.get('SWAN_ENVIRONMENT') or os.environ.get('SWAN_ENV') or 'sandbox').strip().lower()
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


def graphql_query(token, query, variables=None, environment=None, user_id=None):
    """Exécute une requête ou mutation GraphQL contre l'API partenaire Swan.
    Lève ValueError en cas d'erreur réseau, HTTP, ou d'erreurs GraphQL
    renvoyées dans le corps de la réponse (jamais ignorées silencieusement)."""
    env = environment or current_environment()
    url = (os.environ.get('SWAN_GRAPHQL_URL') or '').strip() or GRAPHQL_URLS[env]
    try:
        resp = requests.post(
            url,
            headers={**{'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}, **({'x-swan-user-id': str(user_id)} if user_id else {})},
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
    """Liste les comptes Swan avec IBAN, soldes et dernières transactions.

    Lecture seule avec un project access token. Les transactions sont limitées
    aux 10 plus récentes par compte pour garder la page légère.
    """
    # GraphQL field shape: statusInfo { status }
    query = """
    query ProfitOSListAccounts {
      accounts {
        edges {
          node {
            id
            IBAN
            name
            statusInfo {
              status
            }
            balances {
              available { value currency }
              booked { value currency }
              pending { value currency }
              reserved { value currency }
            }
            transactions(first: 10) {
              edges {
                node {
                  id
                  type
                  label
                  reference
                  side
                  createdAt
                  updatedAt
                  amount { value currency }
                  statusInfo {
                    status
                  }
                }
              }
            }
          }
        }
      }
    }
    """
    data = graphql_query(token, query, environment=environment)
    edges = (data.get('accounts') or {}).get('edges') or []
    accounts = []
    for edge in edges:
        node = dict(edge.get('node') or {})
        node['status'] = (node.get('statusInfo') or {}).get('status')
        balances = node.get('balances') or {}
        node['available_balance'] = balances.get('available') or {}
        node['booked_balance'] = balances.get('booked') or {}
        tx_edges = ((node.get('transactions') or {}).get('edges') or [])
        transactions = []
        for tx_edge in tx_edges:
            tx = dict(tx_edge.get('node') or {})
            tx['status'] = (tx.get('statusInfo') or {}).get('status')
            transactions.append(tx)
        node['recent_transactions'] = transactions
        accounts.append(node)
    return accounts



def company_registry_data_fr(registration_number):
    """Read-only French RNE prefill using the concrete CompanyInfo object returned by Swan."""
    siren = ''.join(ch for ch in str(registration_number or '') if ch.isdigit())
    if len(siren) != 9:
        raise ValueError("Le SIREN doit contenir exactement 9 chiffres.")

    token = get_server_token()
    variables = {"input": {"registrationNumber": siren, "residencyAddressCountry": "FRA"}}

    # Swan Sandbox currently returns CompanyInfo directly on success.
    # Probe first so a rejection can be handled without hard-coding rejection type names.
    probe = """
    query ProfitOSCompanyRegistryProbe($input: CompanyInfoRegistryDataInput!) {
      companyInfoRegistryData(input: $input) {
        __typename
      }
    }
    """
    probe_data = graphql_query(token, probe, variables)
    payload = probe_data.get("companyInfoRegistryData") or {}
    payload_type = payload.get("__typename")
    if not payload_type:
        raise ValueError("Swan n'a retourné aucun type de réponse pour la recherche RNE.")
    if payload_type != "CompanyInfo":
        raise ValueError(
            "Entreprise non trouvée ou réponse Swan non exploitable "
            f"(type: {payload_type})."
        )

    # Query fields directly on CompanyInfo. Keep this first production read
    # deliberately minimal; representatives/UBOs are handled during onboarding.
    query = """
    query ProfitOSCompanyRegistry($input: CompanyInfoRegistryDataInput!) {
      companyInfoRegistryData(input: $input) {
        ... on CompanyInfo {
          name
          legalFormCode
          registrationDate
          address {
            addressLine1
            city
            postalCode
            country
          }
        }
      }
    }
    """
    data = graphql_query(token, query, variables)
    info = data.get("companyInfoRegistryData") or {}
    if info.get("__typename") and info.get("__typename") != "CompanyInfo":
        raise ValueError("Swan n'a retourné aucune information RNE exploitable.")
    if not info.get("name"):
        raise ValueError("Swan n'a retourné aucune information RNE exploitable.")
    info["legalForm"] = info.get("legalFormCode")
    return info



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


def company_onboarding_v2_preflight():
    """Validate Swan's current v2 company-onboarding schema without creating anything."""
    token = get_server_token()
    query = """
    query ProfitOSCompanyOnboardingV2Preflight {
      mutationType: __type(name: "Mutation") {
        fields { name }
      }
      createInput: __type(name: "CreateCompanyAccountHolderOnboardingInput") {
        inputFields { name }
      }
      companyInput: __type(name: "CompanyInfoInput") {
        inputFields { name }
      }
    }
    """
    data = graphql_query(token, query)
    mutation_names = {
        f.get("name") for f in ((data.get("mutationType") or {}).get("fields") or [])
    }
    create_fields = {
        f.get("name") for f in ((data.get("createInput") or {}).get("inputFields") or [])
    }
    company_fields = {
        f.get("name") for f in ((data.get("companyInput") or {}).get("inputFields") or [])
    }
    required_mutation = "createCompanyAccountHolderOnboarding"
    required_create = {"accountInfo", "accountAdmin", "company"}
    required_company = {
        "name", "registrationNumber", "legalFormCode",
        "businessActivity", "businessActivityDescription",
        "monthlyPaymentVolume", "regulatoryClassification",
        "relatedIndividuals",
    }
    missing = []
    if required_mutation not in mutation_names:
        missing.append(required_mutation)
    missing += sorted(required_create - create_fields)
    missing += sorted(required_company - company_fields)
    return {
        "ok": not missing,
        "missing": missing,
        "mutation": required_mutation,
        "create_fields": sorted(x for x in create_fields if x),
        "company_fields": sorted(x for x in company_fields if x),
    }


def create_company_onboarding_v2_sandbox(input_data):
    """Create one Swan v2 company onboarding in Sandbox.

    This is deliberately sandbox-only. It creates an onboarding record/link,
    not a live production bank account. Completion/verification remains on Swan.
    """
    if current_environment() != "sandbox":
        raise ValueError("Création d'onboarding bloquée hors Sandbox.")
    if not isinstance(input_data, dict):
        raise ValueError("Données d'onboarding invalides.")

    token = get_server_token()
    mutation = """
    mutation ProfitOSCreateCompanyOnboardingV2($input: CreateCompanyAccountHolderOnboardingInput!) {
      createCompanyAccountHolderOnboarding(input: $input) {
        __typename
        ... on CreateCompanyAccountHolderOnboardingSuccessPayload {
          onboarding {
            id
            onboardingUrl
            statusInfo {
              status
              ... on OnboardingInvalidStatusInfo {
                errors { field errors }
              }
            }
          }
        }
      }
    }
    """
    data = graphql_query(token, mutation, {"input": input_data}, environment="sandbox")
    result = data.get("createCompanyAccountHolderOnboarding") or {}
    typename = result.get("__typename") or ""
    onboarding = result.get("onboarding") or {}
    if typename != "CreateCompanyAccountHolderOnboardingSuccessPayload":
        raise ValueError(f"Swan a refusé l'onboarding (type: {typename or 'inconnu'}).")
    if not onboarding.get("id"):
        raise ValueError("Swan n'a renvoyé aucun identifiant d'onboarding.")
    status_info = onboarding.get("statusInfo") or {}
    return {
        "id": onboarding.get("id"),
        "onboarding_url": onboarding.get("onboardingUrl"),
        "status": status_info.get("status"),
        "errors": status_info.get("errors") or [],
    }


def create_individual_onboarding_v2_sandbox(input_data):
    """Create a Swan individual onboarding using the current API, Sandbox only."""
    if current_environment() != "sandbox":
        raise ValueError("Création d'onboarding individuel bloquée hors Sandbox.")
    if not isinstance(input_data, dict):
        raise ValueError("Données d'onboarding individuel invalides.")

    token = get_server_token()
    mutation = """
    mutation ProfitOSCreateIndividualOnboardingV2($input: CreateIndividualAccountHolderOnboardingInput!) {
      createIndividualAccountHolderOnboarding(input: $input) {
        __typename
        ... on CreateIndividualAccountHolderOnboardingSuccessPayload {
          onboarding {
            id
            onboardingUrl
            statusInfo {
              status
              ... on OnboardingInvalidStatusInfo {
                errors { field errors }
              }
            }
          }
        }
      }
    }
    """
    data = graphql_query(token, mutation, {"input": input_data}, environment="sandbox")
    result = data.get("createIndividualAccountHolderOnboarding") or {}
    typename = result.get("__typename") or ""
    onboarding = result.get("onboarding") or {}
    if typename != "CreateIndividualAccountHolderOnboardingSuccessPayload":
        raise ValueError(f"Swan a refusé l'onboarding individuel (type: {typename or 'inconnu'}).")
    if not onboarding.get("id"):
        raise ValueError("Swan n'a renvoyé aucun identifiant d'onboarding individuel.")
    status_info = onboarding.get("statusInfo") or {}
    return {
        "id": onboarding.get("id"),
        "onboarding_url": onboarding.get("onboardingUrl"),
        "status": status_info.get("status"),
        "errors": status_info.get("errors") or [],
        "kind": "individual",
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
