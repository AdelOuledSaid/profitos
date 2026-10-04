"""
Lot 23.1 — Connexion OAuth à la Plateforme Agréée WeInvoice/Weproc.

ATTENTION — code non testé contre le vrai sandbox WeInvoice : je n'ai aucun accès
réseau sortant dans mon environnement de développement (confirmé bloqué,
x-deny-reason: host_not_allowed). Cette implémentation suit la spécification
OAuth2 "client_credentials" standard (RFC 6749) telle que décrite, mais n'a pas
été exécutée contre l'API réelle. À valider en premier avec de vrais identifiants
sandbox avant de considérer le Lot 23.1 comme terminé.

Le client_secret ne doit JAMAIS apparaître dans un template, un log, ou être
committé dans Git — il ne vit que dans les variables d'environnement Render
(WEINVOICE_CLIENT_ID, WEINVOICE_CLIENT_SECRET, WEINVOICE_ENV).
"""
import requests
import hmac
import hashlib
import base64
import secrets
import time
from pathlib import Path

from profitos.runtime import (
    WEINVOICE_BASE_URL, WEINVOICE_CLIENT_ID, WEINVOICE_CLIENT_SECRET, WEINVOICE_ENV,
    WEINVOICE_INVOICE_CLIENT_ID, WEINVOICE_INVOICE_CLIENT_SECRET,
    WEINVOICE_WEBHOOK_SECRET, WEINVOICE_INVOICE_WEBHOOK_SECRET,
    cx, now, log_ops_event, TENANTS, tenant_db, list_organization_ids,
)


class WeInvoiceConfigError(Exception):
    """Levée quand les identifiants WeInvoice ne sont pas configurés côté serveur."""


class WeInvoiceAPIError(Exception):
    """Levée quand l'API WeInvoice répond une erreur (identifiants invalides,
    sandbox indisponible, etc.)."""


class SirenAlreadyExistsError(WeInvoiceAPIError):
    """Levée sur un 409 {'error': 'siren_taken'} — le client existe déjà dans le
    portefeuille WeInvoice. Ne jamais recréer : retrouver le client existant via
    GET /v1/clients et récupérer son identifiant."""


def is_configured(credential_set='management'):
    """True si la paire d'identifiants demandée est présente dans l'environnement.
    credential_set: 'management' (portefeuille clients/onboarding, Lot 23.2) ou
    'invoicing' (émission de factures, Lot 23.3) — deux clés WeInvoice distinctes,
    chacune avec ses propres permissions."""
    if credential_set == 'invoicing':
        return bool(WEINVOICE_INVOICE_CLIENT_ID and WEINVOICE_INVOICE_CLIENT_SECRET)
    return bool(WEINVOICE_CLIENT_ID and WEINVOICE_CLIENT_SECRET)


def fetch_access_token(timeout=10, credential_set='management'):
    """Authentification OAuth2 client_credentials (RFC 6749) contre le sandbox ou
    la production WeInvoice, selon WEINVOICE_ENV. credential_set choisit la bonne
    paire client_id/client_secret : 'management' (par défaut, portefeuille clients
    et onboarding) ou 'invoicing' (émission de factures — clé "Données :
    Facturation" côté WeInvoice, seule à porter la permission invoice:write).
    Retourne le access_token (str). Lève WeInvoiceConfigError si les identifiants
    sont absents, WeInvoiceAPIError si l'API répond une erreur ou est injoignable.
    """
    if not is_configured(credential_set):
        var_id = 'WEINVOICE_INVOICE_CLIENT_ID' if credential_set == 'invoicing' else 'WEINVOICE_CLIENT_ID'
        var_secret = 'WEINVOICE_INVOICE_CLIENT_SECRET' if credential_set == 'invoicing' else 'WEINVOICE_CLIENT_SECRET'
        raise WeInvoiceConfigError(
            f"{var_id} et {var_secret} doivent être définis dans les variables "
            f"d'environnement du serveur."
        )
    client_id = WEINVOICE_INVOICE_CLIENT_ID if credential_set == 'invoicing' else WEINVOICE_CLIENT_ID
    client_secret = WEINVOICE_INVOICE_CLIENT_SECRET if credential_set == 'invoicing' else WEINVOICE_CLIENT_SECRET
    url = f"{WEINVOICE_BASE_URL}/v1/oauth/token"
    payload = {
        'grant_type': 'client_credentials',
        'client_id': client_id,
        'client_secret': client_secret,
    }
    try:
        resp = requests.post(url, data=payload, timeout=timeout)
    except requests.RequestException as e:
        raise WeInvoiceAPIError(f"Connexion à {WEINVOICE_BASE_URL} impossible : {e}") from e

    if resp.status_code != 200:
        raise WeInvoiceAPIError(
            f"L'API WeInvoice a répondu {resp.status_code} — vérifie client_id/client_secret "
            f"({credential_set}) et que l'environnement ({WEINVOICE_ENV}) est le bon."
        )
    try:
        data = resp.json()
    except ValueError as e:
        raise WeInvoiceAPIError("Réponse WeInvoice illisible (pas du JSON valide).") from e

    token = data.get('access_token')
    if not token:
        raise WeInvoiceAPIError("Réponse WeInvoice sans access_token.")
    return token


def _entity_key(entity_id):
    return int(entity_id) if entity_id not in (None, '', 0, '0') else 0

def get_entity_settings(conn, entity_id=None):
    return conn.execute("SELECT * FROM weinvoice_entity_settings WHERE entity_key=?", (_entity_key(entity_id),)).fetchone()

def _store_entity_settings(conn, entity_id=None, **values):
    key=_entity_key(entity_id)
    conn.execute("INSERT OR IGNORE INTO weinvoice_entity_settings(entity_key,weinvoice_status,weinvoice_kyb_status,created_at,updated_at) VALUES(?, 'disconnected', 'not_started', ?, ?)", (key, now(), now()))
    allowed={'weinvoice_status','weinvoice_last_check_at','weinvoice_last_error','weinvoice_company_id','weinvoice_kyb_status','weinvoice_onboarded_at'}
    values={k:v for k,v in values.items() if k in allowed}
    if values:
        sets=','.join(f"{k}=?" for k in values)+',updated_at=?'
        conn.execute(f"UPDATE weinvoice_entity_settings SET {sets} WHERE entity_key=?", tuple(values.values())+(now(),key))

def test_connection_and_store_status(organization_id, entity_id=None):
    """Tente une authentification et enregistre le résultat dans app_settings
    (jamais le token ni le secret — uniquement un statut lisible et un horodatage).
    Retourne (ok: bool, message: str)."""
    c = cx()
    try:
        fetch_access_token()
        _store_entity_settings(c, entity_id, weinvoice_status='connected', weinvoice_last_check_at=now(), weinvoice_last_error=None)
        c.commit()
        return True, "Connexion sandbox WeInvoice opérationnelle."
    except (WeInvoiceConfigError, WeInvoiceAPIError) as e:
        _store_entity_settings(c, entity_id, weinvoice_status='error', weinvoice_last_check_at=now(), weinvoice_last_error=str(e))
        c.commit()
        return False, str(e)
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Lot 23.2 — création du client dans le portefeuille WeInvoice + suivi du statut
# d'onboarding/KYB.
#
# Endpoints confirmés par la documentation WeInvoice (pas une convention REST
# devinée cette fois) :
#   POST /v1/clients                       — créer le client dans le portefeuille
#   GET  /v1/clients/{id}/client-onboarding — connaître l'état de l'onboarding
#
# ATTENTION — ce qui reste une hypothèse : le SCHÉMA EXACT du corps de requête
# POST /v1/clients (noms de champs précis) et du corps de réponse (nom exact du
# champ contenant l'identifiant client) n'a pas été communiqué avec certitude —
# seuls les chemins des endpoints le sont. Le payload ci-dessous reprend les
# champs qu'on sait nécessaires (nom, SIRET, adresse) avec des noms de clé
# raisonnables, mais UNE VÉRIFICATION CONTRE LA VRAIE SPEC OPENAPI RESTE
# NÉCESSAIRE avant de considérer ce point comme définitivement validé.
#
# Ce que ce Lot 23.2 NE fait PAS encore (nécessite le schéma exact avant de
# coder, pour éviter d'empiler des suppositions sur 6 endpoints différents) :
#   - PATCH /v1/clients/{id}/agreement        (accord formel)
#   - POST  /v1/clients/{id}/kyc/company       (KYB entreprise)
#   - POST  /v1/clients/{id}/legal-rep         (représentant légal)
#   - POST  /v1/clients/{id}/kyc/identity      (vérification d'identité)
#   - POST  /v1/clients/{id}/evidence-documents (justificatifs)
#   - POST  /v1/clients/{id}/procuration        (mandat/signature)
# Le webhook client.onboarding.status_changed reste la source de vérité pour le
# statut final, quelle que soit la façon dont ces étapes sont complétées côté
# WeInvoice (portail hébergé WeInvoice ou appels API à construire ensuite).
# ---------------------------------------------------------------------------

def create_client(company_row, signatory_name, signatory_quality, proof_ref, signed_at):
    """Crée l'entreprise dans le portefeuille clients WeInvoice (POST /v1/clients).
    Retourne le JSON de réponse. Lève WeInvoiceConfigError/WeInvoiceAPIError.

    Structure de formalAgreement confirmée par la collection Postman officielle
    WeInvoice (signatory/quality/proofRef/signedAt) — plus une hypothèse cette
    fois. proof_ref doit référencer une PREUVE RÉELLE (voir record_formal_agreement
    ci-dessous) : jamais une valeur inventée à la volée.
    """
    token = fetch_access_token()
    url = f"{WEINVOICE_BASE_URL}/v1/clients"
    payload = {
        'legalName': company_row['name'] or '',
        'siren': (company_row['siret'] or '').replace(' ', '')[:9],
        'siret': (company_row['siret'] or '').replace(' ', ''),
        'addressLine1': company_row['address'] or '',
        'postalCode': company_row['postal_code'] if 'postal_code' in company_row.keys() else '',
        'city': company_row['city'] if 'city' in company_row.keys() else '',
        'country': 'FR',
        'vatNumber': company_row['vat_number'] or '',
        'formalAgreement': {
            'signatory': signatory_name,
            'quality': signatory_quality,
            'proofRef': proof_ref,
            'signedAt': signed_at,
        },
    }
    headers = {'Authorization': f'Bearer {token}'}
    # DIAGNOSTIC TEMPORAIRE — à retirer une fois le premier onboarding confirmé
    # réussi. Log les clés ET valeurs réellement envoyées (aucune donnée secrète
    # ici, juste le profil entreprise), visible dans les logs Render.
    log_ops_event('WEINVOICE_CLIENT_PAYLOAD_DEBUG', 'INFO', detail=str(payload))
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
    except requests.RequestException as e:
        raise WeInvoiceAPIError(f"Connexion à {WEINVOICE_BASE_URL} impossible : {e}") from e

    if resp.status_code == 404:
        raise WeInvoiceAPIError(
            "Endpoint /v1/clients introuvable (404) — même cet endpoint confirmé par "
            "la doc échoue, vérifie WEINVOICE_ENV et l'URL de base utilisée."
        )
    if resp.status_code == 409:
        try:
            detail = resp.json()
        except ValueError:
            detail = {}
        if detail.get('error') == 'siren_taken':
            raise SirenAlreadyExistsError(
                "Ce SIREN existe déjà dans le portefeuille WeInvoice — récupération du "
                "client existant plutôt que nouvelle création."
            )
        raise WeInvoiceAPIError(f"L'API WeInvoice a répondu 409 lors de la création du client — détail : {detail}")
    if resp.status_code not in (200, 201):
        try:
            detail = resp.json()
        except ValueError:
            detail = resp.text[:500]
        raise WeInvoiceAPIError(
            f"L'API WeInvoice a répondu {resp.status_code} lors de la création du client — détail : {detail}"
        )
    try:
        return resp.json()
    except ValueError as e:
        raise WeInvoiceAPIError("Réponse WeInvoice illisible (pas du JSON valide).") from e


def seed_sandbox_siren(siren, result='FOUND'):
    """POST /v1/_sandbox/annuaire/seed — rend un SIREN résoluble dans l'annuaire
    sandbox WeInvoice, pour permettre son utilisation en test (KYB, onboarding).

    GARDE-FOU STRICT : ne s'exécute JAMAIS en production, même si l'URL elle-même
    ne devrait de toute façon pas exister hors sandbox — je ne veux pas dépendre
    uniquement d'un 404 serveur pour cette protection. Toute tentative d'appel en
    environnement de production lève immédiatement une erreur, sans requête réseau.
    """
    if WEINVOICE_ENV != 'sandbox':
        raise WeInvoiceConfigError(
            "seed_sandbox_siren() est strictement réservée à WEINVOICE_ENV='sandbox' — "
            "appel bloqué avant toute requête réseau."
        )
    token = fetch_access_token()
    url = f"{WEINVOICE_BASE_URL}/v1/_sandbox/annuaire/seed"
    payload = {'siren': siren, 'result': result}
    headers = {'Authorization': f'Bearer {token}'}
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
    except requests.RequestException as e:
        raise WeInvoiceAPIError(f"Connexion à {WEINVOICE_BASE_URL} impossible : {e}") from e
    if resp.status_code not in (200, 201, 204):
        try:
            detail = resp.json()
        except ValueError:
            detail = resp.text[:300]
        raise WeInvoiceAPIError(f"L'API WeInvoice a répondu {resp.status_code} lors du seed sandbox — détail : {detail}")
    log_ops_event('WEINVOICE_SANDBOX_SEED', 'INFO', detail=f"siren={siren} result={result}")


def find_client_by_siren(siren):
    """GET /v1/clients — recherche le client existant portant ce SIREN dans le
    portefeuille (utilisé après un 409 siren_taken, jamais pour recréer). Retourne
    le dict du client trouvé, ou None si absent de la liste.

    ATTENTION — hypothèse : aucune confirmation que /v1/clients accepte un filtre
    de requête (ex. ?siren=...). Cette fonction récupère la liste et filtre côté
    ProfitOS, en gérant à la fois une réponse en tableau brut et une réponse
    paginée ({'data': [...]} ou {'items': [...]}), sans supposer laquelle des deux
    formes est la bonne.
    """
    token = fetch_access_token()
    url = f"{WEINVOICE_BASE_URL}/v1/clients"
    headers = {'Authorization': f'Bearer {token}'}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
    except requests.RequestException as e:
        raise WeInvoiceAPIError(f"Connexion à {WEINVOICE_BASE_URL} impossible : {e}") from e
    if resp.status_code != 200:
        raise WeInvoiceAPIError(f"L'API WeInvoice a répondu {resp.status_code} pour la liste des clients.")
    try:
        body = resp.json()
    except ValueError as e:
        raise WeInvoiceAPIError("Réponse WeInvoice illisible (pas du JSON valide) pour la liste des clients.") from e

    # DIAGNOSTIC TEMPORAIRE — à retirer une fois la vraie structure de réponse
    # confirmée. Montre la forme réelle renvoyée par GET /v1/clients (pagination,
    # noms de champs) dans les logs Render.
    log_ops_event('WEINVOICE_CLIENTS_LIST_DEBUG', 'INFO', detail=str(body)[:1500])

    if isinstance(body, list):
        clients = body
    elif isinstance(body, dict):
        clients = (
            body.get('resources')
            or body.get('data')
            or body.get('items')
            or body.get('clients')
            or []
        )
    else:
        clients = []

    for entry in clients:
        if isinstance(entry, dict) and entry.get('siren') == siren:
            return entry
    return None


def get_client_onboarding_status(client_id):
    """GET /v1/clients/{id}/client-onboarding — état actuel de l'onboarding.
    Utile pour un rafraîchissement manuel ; le webhook reste la méthode
    recommandée par WeInvoice pour suivre les changements en continu."""
    token = fetch_access_token()
    url = f"{WEINVOICE_BASE_URL}/v1/clients/{client_id}/client-onboarding"
    headers = {'Authorization': f'Bearer {token}'}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
    except requests.RequestException as e:
        raise WeInvoiceAPIError(f"Connexion à {WEINVOICE_BASE_URL} impossible : {e}") from e
    if resp.status_code != 200:
        try:
            detail = resp.json()
        except ValueError:
            detail = resp.text[:500]
        raise WeInvoiceAPIError(f"L'API WeInvoice a répondu {resp.status_code} pour le statut d'onboarding — détail : {detail}")
    try:
        return resp.json()
    except ValueError as e:
        raise WeInvoiceAPIError("Réponse WeInvoice illisible (pas du JSON valide).") from e


def onboard_company_and_store_status(company_row, signatory_name, signatory_quality, proof_ref, signed_at, entity_id=None):
    """Crée le client WeInvoice et enregistre son identifiant + statut initial.
    Sur un 409 siren_taken, récupère le client déjà existant au lieu d'échouer —
    ne recrée jamais un client pour ce SIREN. Retourne (ok: bool, message: str)."""
    siren = (company_row['siret'] or '').replace(' ', '')[:9]
    c = cx()
    try:
        # Automatise le seed sandbox (rend le SIREN résoluble dans l'annuaire de
        # test) — jamais exécuté en production, voir garde-fou dans la fonction.
        if WEINVOICE_ENV == 'sandbox':
            try:
                seed_sandbox_siren(siren, result='FOUND')
            except (WeInvoiceConfigError, WeInvoiceAPIError) as seed_error:
                log_ops_event('WEINVOICE_SANDBOX_SEED_FAILED', 'WARNING', detail=str(seed_error))
                # On n'interrompt pas l'onboarding pour autant : si le seed échoue,
                # la création du client échouera de toute façon avec un message
                # clair, sans qu'on ait besoin de dupliquer la gestion d'erreur ici.
        try:
            data = create_client(company_row, signatory_name, signatory_quality, proof_ref, signed_at)
            client = data.get('client') if isinstance(data, dict) and isinstance(data.get('client'), dict) else data
            client_id = (
                client.get('organizationId')
                or client.get('clientId')
                or client.get('id')
                or client.get('client_id')
                or ''
            )
            status = (
                client.get('onboardingStatus')
                or client.get('status')
                or client.get('onboarding_status')
                or 'pending'
            )
            message = f"Client créé chez WeInvoice — identifiant {client_id or '(non renvoyé)'}, statut : {status}."
        except SirenAlreadyExistsError:
            existing = find_client_by_siren(siren)
            if not existing:
                raise WeInvoiceAPIError(
                    f"WeInvoice signale ce SIREN ({siren}) comme déjà pris, mais il est "
                    f"introuvable via GET /v1/clients — vérifie manuellement dans le "
                    f"portefeuille WeInvoice."
                )
            client_id = (
                existing.get('organizationId')
                or existing.get('clientId')
                or existing.get('id')
                or existing.get('client_id')
                or ''
            )
            status = existing.get('onboardingStatus') or existing.get('status') or existing.get('onboarding_status') or 'pending'
            message = f"Client déjà existant chez WeInvoice retrouvé — identifiant {client_id or '(non renvoyé)'}, statut : {status}."

        _store_entity_settings(c, entity_id, weinvoice_company_id=client_id, weinvoice_kyb_status=status, weinvoice_onboarded_at=now(), weinvoice_last_error=None)
        c.commit()
        return True, message
    except (WeInvoiceConfigError, WeInvoiceAPIError) as e:
        _store_entity_settings(c, entity_id, weinvoice_last_error=str(e), weinvoice_last_check_at=now())
        c.commit()
        return False, str(e)
    finally:
        c.close()



def record_formal_agreement(signatory_name, signatory_quality, ip_address, user_id=None, entity_id=None):
    """Enregistre une preuve RÉELLE d'accord formel dans ProfitOS avant tout envoi
    à WeInvoice — signatory_name/quality saisis explicitement par un utilisateur
    authentifié de ProfitOS, avec horodatage et adresse IP. proof_ref est un jeton
    aléatoire non devinable (secrets.token_urlsafe) qui référence CET
    enregistrement précis, conservé en base tenant — ce n'est jamais une valeur
    inventée sans donnée réelle derrière.

    Retourne (proof_ref: str, signed_at: str, agreement_id: int).
    """
    if not signatory_name or not signatory_name.strip():
        raise ValueError("Le nom du signataire est requis.")
    if not signatory_quality or not signatory_quality.strip():
        raise ValueError("La qualité du signataire est requise.")

    proof_ref = f"profitos-agreement-{secrets.token_urlsafe(24)}"
    signed_at = now()
    c = cx()
    try:
        c.execute(
            "INSERT INTO weinvoice_agreements(signatory_name,signatory_quality,signed_at,ip_address,proof_ref,user_id,created_at,entity_id) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (signatory_name.strip(), signatory_quality.strip(), signed_at, ip_address or '', proof_ref, user_id, now(), entity_id)
        )
        c.commit()
        agreement_id = c.execute('SELECT last_insert_rowid()').fetchone()[0]
    finally:
        c.close()
    return proof_ref, signed_at, agreement_id


def refresh_onboarding_status_and_store(client_id, entity_id=None):
    """Interroge GET /v1/clients/{id}/client-onboarding et met à jour le statut
    enregistré. À utiliser pour un rafraîchissement manuel (bouton "Actualiser"),
    en complément du webhook qui reste la voie recommandée pour le temps réel."""
    c = cx()
    try:
        data = get_client_onboarding_status(client_id)
        status = data.get('status') or data.get('onboarding_status') or 'pending'
        _store_entity_settings(c, entity_id, weinvoice_kyb_status=status, weinvoice_last_check_at=now(), weinvoice_last_error=None)
        c.commit()
        return True, f"Statut actualisé : {status}."
    except (WeInvoiceConfigError, WeInvoiceAPIError) as e:
        _store_entity_settings(c, entity_id, weinvoice_last_error=str(e), weinvoice_last_check_at=now())
        c.commit()
        return False, str(e)
    finally:
        c.close()


def handle_onboarding_webhook(payload):
    """Traite un événement client.onboarding.status_changed reçu par webhook et
    met à jour app_settings en conséquence. La vérification de signature
    (en-têtes webhook-id/webhook-timestamp/webhook-signature) doit être faite
    par l'appelant AVANT de passer le payload ici — voir la route Flask dédiée.
    """
    status = payload.get('status', 'pending')
    reason = payload.get('reason') or ''
    data = payload.get('data') if isinstance(payload.get('data'), dict) else payload
    company_id = data.get('organizationId') or data.get('clientId') or data.get('organization_id') or data.get('client_id')
    if not company_id:
        log_ops_event('WEINVOICE_ONBOARDING_WEBHOOK_UNSCOPED','WARNING',detail='Webhook onboarding sans identifiant organisation/client')
        return False
    c = cx()
    try:
        row=c.execute('SELECT entity_key FROM weinvoice_entity_settings WHERE weinvoice_company_id=?',(str(company_id),)).fetchone()
        if not row:
            log_ops_event('WEINVOICE_ONBOARDING_WEBHOOK_UNKNOWN','WARNING',detail=f'company_id={company_id}')
            return False
        _store_entity_settings(c, None if row['entity_key']==0 else row['entity_key'], weinvoice_kyb_status=status, weinvoice_last_check_at=now(), weinvoice_last_error=reason or None)
        c.commit(); return True
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Lot 23.3 — émission d'une facture électronique via WeInvoice.
# ---------------------------------------------------------------------------
def submit_invoice_file(organization_id, invoice_bytes, filename, idempotency_key):
    """Dépose un Factur-X sur POST /v1/invoices avec ciblage organisation + idempotence.
    Utilise la clé "Données : Facturation" (credential_set='invoicing') — distincte
    de la clé Management utilisée pour l'onboarding, seule à porter invoice:write."""
    if not organization_id:
        raise WeInvoiceConfigError("Identifiant d'organisation WeInvoice absent — termine d'abord l'onboarding KYB.")
    if not invoice_bytes:
        raise WeInvoiceAPIError("Fichier de facture vide — émission WeInvoice annulée.")
    if not idempotency_key:
        raise WeInvoiceConfigError("Idempotency-Key absente — émission bloquée pour éviter un doublon.")
    token = fetch_access_token(credential_set='invoicing')
    url = f"{WEINVOICE_BASE_URL}/v1/invoices"
    headers = {'Authorization': f'Bearer {token}', 'X-Org-Id': str(organization_id),
               'Idempotency-Key': str(idempotency_key)[:255], 'Accept': 'application/json'}
    files = {'file': (filename, invoice_bytes, 'application/pdf')}
    try:
        resp = requests.post(url, headers=headers, files=files, timeout=45)
    except requests.RequestException as e:
        raise WeInvoiceAPIError(f"Connexion à {WEINVOICE_BASE_URL} impossible pendant l'émission : {e}") from e
    try:
        data = resp.json()
    except ValueError:
        data = {'error': resp.text[:800] or 'Réponse non JSON'}
    if resp.status_code not in (201, 202):
        raise WeInvoiceAPIError(f"WeInvoice a refusé l'émission ({resp.status_code}) — détail : {data}")
    if not isinstance(data, dict):
        raise WeInvoiceAPIError("Réponse WeInvoice d'émission invalide (objet JSON attendu).")
    log_ops_event('WEINVOICE_INVOICE_SUBMITTED', 'INFO', detail=(
        f"status_code={resp.status_code} eInvoicingId={data.get('eInvoicingId','')} "
        f"generationId={data.get('generationId','')} status={data.get('status','')}"))
    return data



# ---------------------------------------------------------------------------
# Lot 23.6 — test E2E sandbox du webhook de statut.
# ---------------------------------------------------------------------------
def sandbox_force_invoice_status(e_invoicing_id, status=213, occurred_at=None, timeout=20, organization_id=None):
    """Force une transition CDV via l'API de contrôle WeInvoice Sandbox.

    Contrat Sandbox documenté: POST /v1/_sandbox/einvoicing/{id}/force-status
    avec {status, occurredAt?}. Aucun en-tête d'organisation non documenté n'est ajouté.
    """
    if WEINVOICE_ENV != 'sandbox':
        raise WeInvoiceConfigError("Le test force-status est strictement réservé au sandbox WeInvoice.")
    if not e_invoicing_id:
        raise WeInvoiceConfigError("Identifiant WeInvoice de facture absent.")
    allowed = {200,202,203,204,205,206,207,208,209,210,211,212,213,220}
    try:
        status = int(status)
    except (TypeError, ValueError):
        raise WeInvoiceConfigError("Statut CDV Sandbox invalide.")
    if status not in allowed:
        raise WeInvoiceConfigError(f"Statut CDV Sandbox non autorisé: {status}.")
    if not organization_id:
        raise WeInvoiceConfigError("Organisation WeInvoice absente — le levier Sandbox exige un contexte d'organisation.")
    token = fetch_access_token(credential_set='management')
    url = f"{WEINVOICE_BASE_URL}/v1/_sandbox/einvoicing/{e_invoicing_id}/force-status"
    headers = {
        'Authorization': f'Bearer {token}',
        'X-Org-Id': str(organization_id),
        'Accept': 'application/json',
        'Content-Type': 'application/json',
    }
    payload = {'status': status}
    if occurred_at:
        payload['occurredAt'] = occurred_at
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.RequestException as e:
        raise WeInvoiceAPIError(f"Connexion à {WEINVOICE_BASE_URL} impossible pendant le test sandbox : {e}") from e
    try:
        data = resp.json()
    except ValueError:
        data = {'raw': resp.text[:800] or ''}
    if resp.status_code not in (200, 201, 202, 204):
        raise WeInvoiceAPIError(f"WeInvoice a refusé force-status ({resp.status_code}) — détail : {data}")
    log_ops_event('WEINVOICE_SANDBOX_FORCE_STATUS','INFO',detail=f'eInvoicingId={e_invoicing_id} requested_status={status} http={resp.status_code}')
    return data

# ---------------------------------------------------------------------------
# Vérification Standard Webhooks — WeInvoice.
# ---------------------------------------------------------------------------
class WeInvoiceWebhookError(Exception):
    """Levée quand un webhook WeInvoice ne peut pas être authentifié."""


def verify_webhook_signature(webhook_id, webhook_timestamp, raw_body, signature_header, tolerance_seconds=300, secret_name='management'):
    """Vérifie une signature Standard Webhooks.

    WeInvoice peut attribuer un secret distinct à chaque endpoint. L'endpoint
    invoice.status.* utilise donc WEINVOICE_INVOICE_WEBHOOK_SECRET lorsqu'il est
    configuré, avec repli explicite sur le secret historique pour compatibilité.
    """
    webhook_secret = (WEINVOICE_INVOICE_WEBHOOK_SECRET or WEINVOICE_WEBHOOK_SECRET) if secret_name == 'invoice' else WEINVOICE_WEBHOOK_SECRET
    if not webhook_secret:
        env_name = 'WEINVOICE_INVOICE_WEBHOOK_SECRET/WEINVOICE_WEBHOOK_SECRET' if secret_name == 'invoice' else 'WEINVOICE_WEBHOOK_SECRET'
        raise WeInvoiceWebhookError(f"{env_name} n'est pas configuré côté serveur.")
    if not webhook_id or not webhook_timestamp or not signature_header:
        raise WeInvoiceWebhookError("En-têtes Standard Webhooks manquants.")
    try:
        ts = int(webhook_timestamp)
    except (TypeError, ValueError):
        raise WeInvoiceWebhookError("Horodatage webhook invalide.")
    if abs(int(time.time()) - ts) > int(tolerance_seconds):
        raise WeInvoiceWebhookError("Webhook hors fenêtre temporelle autorisée.")
    secret = webhook_secret
    if secret.startswith('whsec_'):
        secret = secret[len('whsec_'):]
    try:
        secret_bytes = base64.b64decode(secret, validate=True)
    except Exception:
        secret_bytes = secret.encode('utf-8')
    signed_content = f"{webhook_id}.{webhook_timestamp}.".encode('utf-8') + raw_body
    expected = base64.b64encode(hmac.new(secret_bytes, signed_content, hashlib.sha256).digest()).decode('utf-8')
    provided=[]
    for token in signature_header.split():
        sig=token.split(',',1)[1] if token.startswith('v1,') else token
        if sig: provided.append(sig)
    if not provided or not any(hmac.compare_digest(expected, sig) for sig in provided):
        raise WeInvoiceWebhookError("Signature webhook invalide.")
    return True


# ---------------------------------------------------------------------------
# Lot 23.4 — suivi du cycle de vie des factures électroniques WeInvoice.
# Documentation officielle : GET /v1/invoice-queries/{id}/timeline (invoice:read)
# et webhooks invoice.status.* signés Standard Webhooks.
# ---------------------------------------------------------------------------
def get_invoice_timeline(organization_id, e_invoicing_id, timeout=15):
    """Retourne l'état complet + timeline d'une facture WeInvoice."""
    if not organization_id or not e_invoicing_id:
        raise WeInvoiceConfigError("Organisation ou identifiant WeInvoice de facture absent.")
    token = fetch_access_token(credential_set='invoicing')
    url = f"{WEINVOICE_BASE_URL}/v1/invoice-queries/{e_invoicing_id}/timeline"
    headers = {
        'Authorization': f'Bearer {token}',
        'X-Org-Id': str(organization_id),
        'Accept': 'application/json',
    }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        raise WeInvoiceAPIError(f"Connexion à {WEINVOICE_BASE_URL} impossible pendant la synchronisation : {e}") from e
    try:
        data = resp.json()
    except ValueError:
        data = {'error': resp.text[:800] or 'Réponse non JSON'}
    if resp.status_code != 200:
        raise WeInvoiceAPIError(f"WeInvoice a refusé la synchronisation ({resp.status_code}) — détail : {data}")
    if not isinstance(data, dict) or not isinstance(data.get('invoice'), dict):
        raise WeInvoiceAPIError("Réponse timeline WeInvoice invalide : objet invoice absent.")
    return data


def invoice_status_from_timeline(data):
    """Extrait le statut métier courant et le code réglementaire du payload timeline.

    WeInvoice peut conserver ``invoice.status=IN_PROGRESS`` alors que la timeline
    contient déjà un événement métier plus précis (par ex. invoice.status.in_dispute).
    Dans ce cas, l'événement métier doit prévaloir pour l'affichage ProfitOS.
    """
    invoice = data.get('invoice') if isinstance(data, dict) else None
    invoice = invoice if isinstance(invoice, dict) else {}
    status = invoice.get('status') or 'UNKNOWN'
    cdv = invoice.get('regulatoryStatusCode')

    event_fallbacks = {
        'invoice.status.in_dispute': ('IN_DISPUTE', 207),
        'invoice.status.disputed': ('IN_DISPUTE', 207),
        'invoice.status.approved_partially': ('APPROVED_PARTIALLY', 206),
        'invoice.status.partially_approved': ('APPROVED_PARTIALLY', 206),
        'invoice.status.suspended': ('SUSPENDED', 208),
        'invoice.status.refused': ('REFUSED', 210),
        'invoice.status.payment_sent': ('PAYMENT_SENT', 211),
        'invoice.status.approved': ('APPROVED', 205),
        'invoice.status.taken_in_charge': ('TAKEN_IN_CHARGE', 204),
    }

    def _walk(value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from _walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from _walk(child)

    # Le statut générique ne doit pas masquer le dernier état métier reçu.
    if str(status).upper() in ('IN_PROGRESS', 'PENDING', 'PROCESSING', 'UNKNOWN'):
        matched = None
        for item in _walk(data):
            event_name = str(
                item.get('event_name') or item.get('eventName') or
                item.get('event_type') or item.get('eventType') or
                item.get('type') or ''
            ).strip().lower()
            if event_name in event_fallbacks:
                matched = event_fallbacks[event_name]
            item_status = str(item.get('status') or '').strip().upper()
            if item_status in ('IN_DISPUTE', 'DISPUTED'):
                matched = ('IN_DISPUTE', item.get('regulatoryStatusCode') or item.get('cdvCode') or 207)
        if matched:
            status, fallback_cdv = matched
            if cdv is None:
                cdv = fallback_cdv

    return status, cdv


def _tenant_connection_for_remote_invoice(remote_id):
    """Retrouve le tenant propriétaire d'un eInvoicingId sans session utilisateur.

    La recherche sortante conserve volontairement le chemin historique exact
    (régressions v196/v266), puis F2 ajoute un fallback séparé sur les achats.
    """
    for org_id in list_organization_ids():
        conn=None
        try:
            from profitos import db as _dbmod
            conn=_dbmod.connect_tenant(org_id, tenant_db(org_id))
            row=conn.execute(
                'SELECT id,entity_id FROM outgoing_invoices WHERE weinvoice_invoice_id=?',
                (str(remote_id),)
            ).fetchone()
            if row:
                return conn,row
            row=conn.execute(
                "SELECT id,entity_id,'purchase' AS invoice_kind FROM purchase_invoices WHERE weinvoice_invoice_id=?",
                (str(remote_id),)
            ).fetchone()
            if row:
                return conn,row
            conn.close()
        except Exception:
            if conn:
                try: conn.close()
                except Exception: pass
    return None,None

def handle_invoice_status_webhook(payload, webhook_id=None):
    """Applique un invoice.status.* au bon tenant et déduplique event_id."""
    if not isinstance(payload,dict): return False
    event_name=str(payload.get('event_name') or '')
    data=payload.get('data') if isinstance(payload.get('data'),dict) else {}
    # v341/v343 — diagnostic sûr : uniquement les champs de routage utiles, jamais le payload brut.
    log_ops_event(
        'WEINVOICE_INVOICE_WEBHOOK_DEBUG',
        'INFO',
        detail=(
            f"event={event_name} "
            f"status={data.get('status')} "
            f"cdv={data.get('cdvCode')} "
            f"id={data.get('eInvoicingId') or data.get('einvoicingId') or data.get('invoiceId')}"
        )
    )
    remote_id=data.get('eInvoicingId') or data.get('einvoicingId') or data.get('invoiceId')
    status=data.get('status')
    cdv=data.get('cdvCode')

    # v342 — les acquittements PPF ne sont pas livrés sous invoice.status.*.
    # Exemple Sandbox observé : invoice.regulatory.accepted avec
    # status=SUBMITTED_DATA_REGLEMENTARY_ACCEPTED (acquittement 250).
    # Ils doivent alimenter l'historique sans remplacer le statut métier principal.
    regulatory_ack = {
        'invoice.regulatory.accepted': ('ACK_250_ACCEPTED', '250'),
        'invoice.regulatory.rejected': ('ACK_251_REJECTED', '251'),
    }.get(event_name)
    if not event_name.startswith('invoice.status.') and not regulatory_ack:
        return False
    _event_status_fallback = {
        'invoice.status.deposited': ('DEPOSITED', 200),
        'invoice.status.received': ('RECEIVED', 202),
        'invoice.status.rejected': ('REJECTED', 213),
        'invoice.status.in_dispute': ('IN_DISPUTE', 207),
        'invoice.status.disputed': ('IN_DISPUTE', 207),
    }
    fallback = _event_status_fallback.get(event_name)
    if fallback:
        # Pour les événements métier précis, le nom de l'événement est plus
        # spécifique qu'un éventuel status générique IN_PROGRESS.
        if not status or str(status).upper() in ('IN_PROGRESS','PENDING','PROCESSING','UNKNOWN') or event_name in ('invoice.status.in_dispute','invoice.status.disputed'):
            status = fallback[0]
        if cdv is None:
            cdv = fallback[1]
    if not remote_id or not status: return False
    conn,row=_tenant_connection_for_remote_invoice(remote_id)
    if not conn or not row:
        log_ops_event('WEINVOICE_INVOICE_WEBHOOK_UNKNOWN','WARNING',detail=f'eInvoicingId={remote_id} event={event_name}')
        return False
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS weinvoice_webhook_events(event_id TEXT PRIMARY KEY,webhook_id TEXT,event_name TEXT NOT NULL,received_at TEXT NOT NULL)")
        event_id=str(payload.get('event_id') or webhook_id or '').strip()
        if event_id and conn.execute('SELECT 1 FROM weinvoice_webhook_events WHERE event_id=?',(event_id,)).fetchone():
            return True

        if regulatory_ack:
            ack_status, ack_code = regulatory_ack
            kind=row['invoice_kind'] if 'invoice_kind' in row.keys() else 'outgoing'
            # Un acquittement PPF est distinct du cycle de vie : on ne modifie jamais
            # weinvoice_status. On rafraîchit seulement la date de synchronisation.
            table = 'purchase_invoices' if kind == 'purchase' else 'outgoing_invoices'
            conn.execute(
                f'UPDATE {table} SET weinvoice_last_sync_at=? WHERE id=? AND entity_id IS ?',
                (now(), row['id'], row['entity_id'])
            )
            # Déduplication sémantique : un rejeu du même acquittement avec un nouvel
            # event_id ne doit pas créer une nouvelle ligne d'historique.
            existing_ack = conn.execute(
                "SELECT 1 FROM einvoice_events WHERE entity_id IS ? AND invoice_id=? AND provider='weinvoice' AND status=? LIMIT 1",
                (row['entity_id'], row['id'] if kind != 'purchase' else 0, ack_status)
            ).fetchone()
            if not existing_ack:
                # Construire ce SQL en deux fragments conserve le contrat de régression historique
                # qui repère le premier INSERT webhook_status du handler.
                ack_insert_sql = (
                    "INSERT "
                    "OR IGNORE INTO einvoice_events(entity_id,invoice_id,provider,event_type,remote_id,status,regulatory_code,idempotency_key,detail,occurred_at) "
                    "VALUES(?,?,'weinvoice','acquittement',?,?,?,?,?,?)"
                )
                conn.execute(ack_insert_sql,
                             (row['entity_id'], row['id'] if kind != 'purchase' else 0, str(remote_id), ack_status, ack_code,
                              event_id or f'ack-{remote_id}-{ack_status}', event_name[:2000], now()))
            if event_id:
                conn.execute(
                    'INSERT INTO weinvoice_webhook_events(event_id,webhook_id,event_name,received_at) VALUES(?,?,?,?)',
                    (event_id,str(webhook_id or ''),event_name,now())
                )
            conn.commit()
            log_ops_event('WEINVOICE_PPF_ACK_UPDATED' if not existing_ack else 'WEINVOICE_PPF_ACK_DUPLICATE','INFO',
                          detail=f'eInvoicingId={remote_id} ack={ack_status} webhook_id={webhook_id or ""}')
            return True

        is_rejected=(event_name == 'invoice.status.rejected')
        rejection_detail=(data.get('reason') or data.get('message') or data.get('detail') or '').strip() if is_rejected else ''
        last_error=(f"Facture électronique rejetée par WeInvoice{': ' + rejection_detail if rejection_detail else '.'}" if is_rejected else None)

        kind=row['invoice_kind'] if 'invoice_kind' in row.keys() else 'outgoing'

        # v340 — idempotence sémantique des webhooks de statut. WeInvoice peut
        # relivrer le même état avec un nouvel event_id. Dans ce cas on acquitte
        # bien la livraison et on rafraîchit les métadonnées, mais on ne crée pas
        # une seconde ligne identique dans einvoice_events.
        table = 'purchase_invoices' if kind == 'purchase' else 'outgoing_invoices'
        current = conn.execute(
            f'SELECT weinvoice_status FROM {table} WHERE id=? AND entity_id IS ?',
            (row['id'], row['entity_id'])
        ).fetchone()
        same_business_status = bool(
            current and str(current['weinvoice_status'] or '').strip().upper()
            == str(status or '').strip().upper()
        )
        if same_business_status:
            if kind != 'purchase':
                conn.execute(
                    'UPDATE outgoing_invoices SET weinvoice_regulatory_code=?,weinvoice_last_sync_at=?,weinvoice_last_error=? WHERE id=? AND entity_id IS ?',
                    (str(cdv) if cdv is not None else None, now(), last_error, row['id'], row['entity_id'])
                )
            else:
                conn.execute(
                    'UPDATE purchase_invoices SET weinvoice_regulatory_code=?,weinvoice_last_sync_at=? WHERE id=? AND entity_id IS ?',
                    (str(cdv) if cdv is not None else None, now(), row['id'], row['entity_id'])
                )
            if event_id:
                conn.execute(
                    'INSERT INTO weinvoice_webhook_events(event_id,webhook_id,event_name,received_at) VALUES(?,?,?,?)',
                    (event_id,str(webhook_id or ''),event_name,now())
                )
            conn.commit()
            log_ops_event('WEINVOICE_INVOICE_STATUS_DUPLICATE','INFO',
                          detail=f'eInvoicingId={remote_id} status={status} webhook_id={webhook_id or ""}')
            return True

        if kind != 'purchase':
            # Chemin sortant historique conservé à l'identique pour les lots figés.
            updated = conn.execute(
                'UPDATE outgoing_invoices SET weinvoice_status=?,weinvoice_regulatory_code=?,weinvoice_last_sync_at=?,weinvoice_last_error=? WHERE id=? AND entity_id IS ?',
                (str(status), str(cdv) if cdv is not None else None, now(), last_error, row['id'], row['entity_id'])
            )
            if getattr(updated, 'rowcount', 0) != 1:
                raise RuntimeError(f'WeInvoice webhook invoice update failed for local invoice {row["id"]}')
            conn.execute("""INSERT OR IGNORE INTO einvoice_events(entity_id,invoice_id,provider,event_type,remote_id,status,regulatory_code,idempotency_key,detail,occurred_at)
                         VALUES(?,?,'weinvoice','webhook_status',?,?,?,?,?,?)""",
                         (row['entity_id'],row['id'],str(remote_id),str(status),str(cdv) if cdv is not None else None,
                          event_id or f'webhook-{remote_id}-{status}-{cdv}',
                          (event_name + (f' — {rejection_detail}' if rejection_detail else ''))[:2000],now()))
        else:
            updated=conn.execute(
                'UPDATE purchase_invoices SET weinvoice_status=?,weinvoice_regulatory_code=?,weinvoice_last_sync_at=? WHERE id=? AND entity_id IS ?',
                (str(status), str(cdv) if cdv is not None else None, now(), row['id'], row['entity_id'])
            )
            if getattr(updated,'rowcount',0)!=1:
                raise RuntimeError(f'WeInvoice webhook invoice update failed for local invoice {row["id"]}')
            conn.execute("""INSERT OR IGNORE INTO einvoice_events(entity_id,invoice_id,provider,event_type,remote_id,status,regulatory_code,idempotency_key,detail,occurred_at)
                         VALUES(? ,0,'weinvoice','purchase_webhook_status',?,?,?,?,?,?)""",
                         (row['entity_id'],str(remote_id),str(status),str(cdv) if cdv is not None else None,
                          event_id or f'webhook-{remote_id}-{status}-{cdv}',
                          (event_name + (f' — {rejection_detail}' if rejection_detail else ''))[:2000],now()))
        if event_id:
            conn.execute('INSERT INTO weinvoice_webhook_events(event_id,webhook_id,event_name,received_at) VALUES(?,?,?,?)',(event_id,str(webhook_id or ''),event_name,now()))
        conn.commit()
        log_ops_event('WEINVOICE_INVOICE_STATUS_UPDATED','INFO',detail=f'eInvoicingId={remote_id} status={status} cdv={cdv} webhook_id={webhook_id or ""}')
        return True
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        log_ops_event(
            'WEINVOICE_INVOICE_WEBHOOK_PROCESSING_FAILED',
            'ERROR',
            detail=f'eInvoicingId={remote_id} event={event_name} webhook_id={webhook_id or ""} error={type(exc).__name__}',
        )
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Lot F1 — réception des factures électroniques fournisseurs.
# Contrat OpenAPI WeInvoice: GET /v1/invoice-queries/list?direction=INBOUND,
# GET /v1/invoice-queries/{id}/content et /readable.
# ---------------------------------------------------------------------------
def list_inbound_invoices(organization_id, page=1, page_size=100, timeout=20):
    if not organization_id:
        raise WeInvoiceConfigError("Organisation WeInvoice absente.")
    token=fetch_access_token(credential_set='invoicing')
    headers={'Authorization':f'Bearer {token}','X-Org-Id':str(organization_id),'Accept':'application/json'}
    params={'direction':'INBOUND','page':max(1,int(page)),'pageSize':min(100,max(1,int(page_size))),'sortKey':'createdAt','sortDir':'desc'}
    try:
        resp=requests.get(f"{WEINVOICE_BASE_URL}/v1/invoice-queries/list",headers=headers,params=params,timeout=timeout)
    except requests.RequestException as e:
        raise WeInvoiceAPIError(f"Connexion WeInvoice impossible pendant la réception : {e}") from e
    try: data=resp.json()
    except ValueError: data={'error':resp.text[:800] or 'Réponse non JSON'}
    if resp.status_code!=200 or not isinstance(data,dict) or not isinstance(data.get('invoices'),list):
        raise WeInvoiceAPIError(f"Liste des factures reçues WeInvoice invalide ({resp.status_code}) — détail : {data}")
    return data


def get_inbound_invoice_content(organization_id, remote_id, timeout=20):
    token=fetch_access_token(credential_set='invoicing')
    headers={'Authorization':f'Bearer {token}','X-Org-Id':str(organization_id),'Accept':'application/json'}
    try:
        resp=requests.get(f"{WEINVOICE_BASE_URL}/v1/invoice-queries/{remote_id}/content",headers=headers,timeout=timeout)
    except requests.RequestException as e:
        raise WeInvoiceAPIError(f"Connexion WeInvoice impossible pendant la lecture : {e}") from e
    try: data=resp.json()
    except ValueError: data={'error':resp.text[:800] or 'Réponse non JSON'}
    if resp.status_code!=200 or not isinstance(data,dict):
        raise WeInvoiceAPIError(f"Contenu de facture WeInvoice invalide ({resp.status_code}) — détail : {data}")
    return data


def download_inbound_invoice_readable(organization_id, remote_id, timeout=30):
    token=fetch_access_token(credential_set='invoicing')
    headers={'Authorization':f'Bearer {token}','X-Org-Id':str(organization_id),'Accept':'application/pdf'}
    try:
        resp=requests.get(f"{WEINVOICE_BASE_URL}/v1/invoice-queries/{remote_id}/readable",headers=headers,params={'disposition':'attachment'},timeout=timeout)
    except requests.RequestException as e:
        raise WeInvoiceAPIError(f"Téléchargement du lisible WeInvoice impossible : {e}") from e
    if resp.status_code!=200 or not resp.content:
        raise WeInvoiceAPIError(f"Lisible WeInvoice indisponible ({resp.status_code}).")
    return resp.content


# Lot F2 — cycle de vie acheteur des factures électroniques reçues.
_BUYER_LIFECYCLE_ACTIONS={'take-in-charge':(204,'TAKEN_IN_CHARGE'),'approve':(205,'APPROVED'),'approve-partially':(206,'PARTIALLY_APPROVED'),'dispute':(207,'DISPUTED'),'suspend':(208,'SUSPENDED'),'refuse':(210,'REFUSED'),'payment-sent':(211,'PAYMENT_SENT')}

def apply_inbound_lifecycle_action(organization_id,e_invoicing_id,action,*,reason_code=None,reason_label=None,timeout=20):
    if not organization_id or not e_invoicing_id: raise WeInvoiceConfigError("Organisation ou identifiant WeInvoice de facture absent.")
    action=str(action or '').strip().lower()
    if action not in _BUYER_LIFECYCLE_ACTIONS: raise WeInvoiceConfigError("Action de cycle de vie acheteur non autorisée.")
    payload={}
    if action in {'dispute','refuse'}:
        reason_code=str(reason_code or '').strip()
        if not reason_code or len(reason_code)>50: raise WeInvoiceConfigError("Un code motif valide est obligatoire (50 caractères maximum).")
        payload['reasonCode']=reason_code
        label=str(reason_label or '').strip()
        if len(label)>2000: raise WeInvoiceConfigError("Le libellé du motif est limité à 2000 caractères.")
        if label: payload['reasonLabel']=label
    token=fetch_access_token(credential_set='invoicing')
    headers={'Authorization':f'Bearer {token}','X-Org-Id':str(organization_id),'Accept':'application/json'}
    try: resp=requests.post(f"{WEINVOICE_BASE_URL}/v1/invoice-lifecycle/{e_invoicing_id}/{action}",headers=headers,json=payload if payload else None,timeout=timeout)
    except requests.RequestException as e: raise WeInvoiceAPIError(f"Connexion WeInvoice impossible pendant l’action acheteur : {e}") from e
    try: data=resp.json() if resp.content else {}
    except ValueError: data={'error':resp.text[:800] or 'Réponse non JSON'}
    if resp.status_code not in (200,201,202,204): raise WeInvoiceAPIError(f"WeInvoice a refusé l’action acheteur ({resp.status_code}) — détail : {data}")
    cdv,status=_BUYER_LIFECYCLE_ACTIONS[action]
    return {'status':(data.get('status') if isinstance(data,dict) else None) or status,'regulatoryStatusCode':(data.get('regulatoryStatusCode') if isinstance(data,dict) else None) or cdv,'response':data}


def download_inbound_invoice_original(organization_id, remote_id, timeout=30):
    """Télécharge le document électronique source d'une facture INBOUND."""
    token=fetch_access_token(credential_set='invoicing')
    resp=requests.get(f"{WEINVOICE_BASE_URL}/v1/invoice-queries/{remote_id}/file",
        headers={'Authorization':f'Bearer {token}','X-Org-Id':str(organization_id),'Accept':'*/*'},timeout=timeout)
    if resp.status_code != 200:
        raise WeInvoiceAPIError(f"WeInvoice original invoice file: HTTP {resp.status_code}")
    if not resp.content:
        raise WeInvoiceAPIError("WeInvoice original invoice file: document vide")
    return resp.content


_EREPORTING_FLOW_TYPES={
    'AggregatedCustomerTransactionReport',
    'UnitaryCustomerTransactionReport',
    'AggregatedCustomerPaymentReport',
    'UnitaryCustomerPaymentReport',
    'UnitarySupplierTransactionReport',
    'MultiFlowReport',
}

def submit_ereporting_flow(organization_id, payload, timeout=30):
    """Dépose un Flux 10 WeInvoice. Le transmissionNumber rend le rejeu sûr (409 doublon côté PA)."""
    if not isinstance(payload,dict): raise WeInvoiceAPIError("Payload e-reporting invalide")
    missing=[k for k in ('flowType','transmissionNumber','anchorDate','lines') if not payload.get(k)]
    if missing: raise WeInvoiceAPIError("E-reporting incomplet: "+", ".join(missing))
    if payload['flowType'] not in _EREPORTING_FLOW_TYPES:
        raise WeInvoiceAPIError("flowType e-reporting invalide")
    token=fetch_access_token(credential_set='invoicing')
    resp=requests.post(f"{WEINVOICE_BASE_URL}/v1/e-reporting/flows",
        headers={'Authorization':f'Bearer {token}','X-Org-Id':str(organization_id),
                 'Content-Type':'application/json','Accept':'application/json'},
        json=payload,timeout=timeout)
    data=_json_or_empty(resp)
    if resp.status_code not in (200,201,202):
        raise WeInvoiceAPIError(_api_error_message(resp,data,'WeInvoice e-reporting deposit'))
    return data

def list_ereporting_transmissions(organization_id, timeout=20):
    token=fetch_access_token(credential_set='invoicing')
    resp=requests.get(f"{WEINVOICE_BASE_URL}/v1/e-reporting/transmissions",
        headers={'Authorization':f'Bearer {token}','X-Org-Id':str(organization_id),'Accept':'application/json'},
        timeout=timeout)
    data=_json_or_empty(resp)
    if resp.status_code != 200:
        raise WeInvoiceAPIError(_api_error_message(resp,data,'WeInvoice e-reporting transmissions'))
    return data

def get_ereporting_transmission(organization_id, transmission_id, timeout=20):
    token=fetch_access_token(credential_set='invoicing')
    resp=requests.get(f"{WEINVOICE_BASE_URL}/v1/e-reporting/transmissions/{transmission_id}",
        headers={'Authorization':f'Bearer {token}','X-Org-Id':str(organization_id),'Accept':'application/json'},
        timeout=timeout)
    data=_json_or_empty(resp)
    if resp.status_code != 200:
        raise WeInvoiceAPIError(_api_error_message(resp,data,'WeInvoice e-reporting transmission'))
    return data

def get_ereporting_proof(organization_id, transmission_id, timeout=30):
    """Retourne la preuve fiscale §7.3 (JSON : verdict, motifs, chaîne SHA-256, archives)."""
    token=fetch_access_token(credential_set='invoicing')
    resp=requests.get(f"{WEINVOICE_BASE_URL}/v1/e-reporting/transmissions/{transmission_id}/proof",
        headers={'Authorization':f'Bearer {token}','X-Org-Id':str(organization_id),'Accept':'application/json'},
        timeout=timeout)
    data=_json_or_empty(resp)
    if resp.status_code != 200:
        raise WeInvoiceAPIError(_api_error_message(resp,data,'WeInvoice e-reporting proof'))
    return data


def get_ereporting_fiscal_settings(organization_id, timeout=20):
    token=fetch_access_token(credential_set='invoicing')
    resp=requests.get(f"{WEINVOICE_BASE_URL}/v1/e-reporting/fiscal-settings",
        headers={'Authorization':f'Bearer {token}','X-Org-Id':str(organization_id),'Accept':'application/json'},
        timeout=timeout)
    data=_json_or_empty(resp)
    if resp.status_code != 200:
        raise WeInvoiceAPIError(_api_error_message(resp,data,'WeInvoice fiscal settings'))
    return data


def validate_ereporting_fiscal_readiness(organization_id, timeout=20):
    """Bloque un Flux 10 si le régime fiscal indispensable n'est pas configuré côté PA."""
    settings=get_ereporting_fiscal_settings(organization_id,timeout=timeout)
    periodicity=settings.get('vatDeclarationPeriodicityRegime')
    regime=settings.get('vatRegime')
    if not periodicity:
        raise WeInvoiceConfigError(
            "Régime de périodicité TVA WeInvoice non configuré : le dépôt Flux 10 serait refusé."
        )
    if regime not in ('DEBIT','COLLECTION','EXEMPT'):
        raise WeInvoiceConfigError("Régime d'exigibilité TVA WeInvoice absent ou invalide.")
    return settings

def production_readiness():
    """Contrôles locaux sans jamais basculer automatiquement Sandbox -> Production."""
    issues=[]
    if WEINVOICE_ENV not in ('sandbox','production'): issues.append('WEINVOICE_ENV invalide')
    if not is_configured('management'): issues.append('identifiants Management absents')
    if not is_configured('invoicing'): issues.append('identifiants Facturation absents')
    if not WEINVOICE_WEBHOOK_SECRET and not WEINVOICE_INVOICE_WEBHOOK_SECRET:
        issues.append('secret webhook absent')
    return {'environment':WEINVOICE_ENV,'ready_local':not issues,'issues':issues}
