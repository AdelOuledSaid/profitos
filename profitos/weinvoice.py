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

from profitos.runtime import (
    WEINVOICE_BASE_URL, WEINVOICE_CLIENT_ID, WEINVOICE_CLIENT_SECRET, WEINVOICE_ENV,
    WEINVOICE_WEBHOOK_SECRET,
    cx, now, log_ops_event,
)


class WeInvoiceConfigError(Exception):
    """Levée quand les identifiants WeInvoice ne sont pas configurés côté serveur."""


class WeInvoiceAPIError(Exception):
    """Levée quand l'API WeInvoice répond une erreur (identifiants invalides,
    sandbox indisponible, etc.)."""


def is_configured():
    """True si les 2 identifiants nécessaires sont présents dans l'environnement."""
    return bool(WEINVOICE_CLIENT_ID and WEINVOICE_CLIENT_SECRET)


def fetch_access_token(timeout=10):
    """Authentification OAuth2 client_credentials (RFC 6749) contre le sandbox ou
    la production WeInvoice, selon WEINVOICE_ENV. Retourne le access_token (str).
    Lève WeInvoiceConfigError si les identifiants sont absents, WeInvoiceAPIError
    si l'API répond une erreur ou est injoignable.
    """
    if not is_configured():
        raise WeInvoiceConfigError(
            "WEINVOICE_CLIENT_ID et WEINVOICE_CLIENT_SECRET doivent être définis "
            "dans les variables d'environnement du serveur."
        )
    url = f"{WEINVOICE_BASE_URL}/v1/oauth/token"
    payload = {
        'grant_type': 'client_credentials',
        'client_id': WEINVOICE_CLIENT_ID,
        'client_secret': WEINVOICE_CLIENT_SECRET,
    }
    try:
        resp = requests.post(url, data=payload, timeout=timeout)
    except requests.RequestException as e:
        raise WeInvoiceAPIError(f"Connexion à {WEINVOICE_BASE_URL} impossible : {e}") from e

    if resp.status_code != 200:
        raise WeInvoiceAPIError(
            f"L'API WeInvoice a répondu {resp.status_code} — vérifie client_id/client_secret "
            f"et que l'environnement ({WEINVOICE_ENV}) est le bon."
        )
    try:
        data = resp.json()
    except ValueError as e:
        raise WeInvoiceAPIError("Réponse WeInvoice illisible (pas du JSON valide).") from e

    token = data.get('access_token')
    if not token:
        raise WeInvoiceAPIError("Réponse WeInvoice sans access_token.")
    return token


def test_connection_and_store_status(organization_id):
    """Tente une authentification et enregistre le résultat dans app_settings
    (jamais le token ni le secret — uniquement un statut lisible et un horodatage).
    Retourne (ok: bool, message: str)."""
    c = cx()
    try:
        fetch_access_token()
        c.execute(
            "UPDATE app_settings SET weinvoice_status='connected',weinvoice_last_check_at=?,weinvoice_last_error=NULL WHERE id=1",
            (now(),)
        )
        c.commit()
        return True, "Connexion sandbox WeInvoice opérationnelle."
    except (WeInvoiceConfigError, WeInvoiceAPIError) as e:
        c.execute(
            "UPDATE app_settings SET weinvoice_status='error',weinvoice_last_check_at=?,weinvoice_last_error=? WHERE id=1",
            (now(), str(e))
        )
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


def onboard_company_and_store_status(company_row, signatory_name, signatory_quality, proof_ref, signed_at):
    """Crée le client WeInvoice et enregistre son identifiant + statut initial.
    Retourne (ok: bool, message: str)."""
    c = cx()
    try:
        data = create_client(company_row, signatory_name, signatory_quality, proof_ref, signed_at)
        client_id = data.get('organizationId') or data.get('id') or data.get('client_id') or ''
        status = data.get('onboardingStatus') or data.get('status') or data.get('onboarding_status') or 'pending'
        c.execute(
            "UPDATE app_settings SET weinvoice_company_id=?,weinvoice_kyb_status=?,weinvoice_onboarded_at=?,weinvoice_last_error=NULL WHERE id=1",
            (client_id, status, now())
        )
        c.commit()
        return True, f"Client créé chez WeInvoice — identifiant {client_id or '(non renvoyé)'}, statut : {status}."
    except (WeInvoiceConfigError, WeInvoiceAPIError) as e:
        c.execute(
            "UPDATE app_settings SET weinvoice_last_error=?,weinvoice_last_check_at=? WHERE id=1",
            (str(e), now())
        )
        c.commit()
        return False, str(e)
    finally:
        c.close()


def record_formal_agreement(signatory_name, signatory_quality, ip_address, user_id=None):
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
            "INSERT INTO weinvoice_agreements(signatory_name,signatory_quality,signed_at,ip_address,proof_ref,user_id,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (signatory_name.strip(), signatory_quality.strip(), signed_at, ip_address or '', proof_ref, user_id, now())
        )
        c.commit()
        agreement_id = c.execute('SELECT last_insert_rowid()').fetchone()[0]
    finally:
        c.close()
    return proof_ref, signed_at, agreement_id


def refresh_onboarding_status_and_store(client_id):
    """Interroge GET /v1/clients/{id}/client-onboarding et met à jour le statut
    enregistré. À utiliser pour un rafraîchissement manuel (bouton "Actualiser"),
    en complément du webhook qui reste la voie recommandée pour le temps réel."""
    c = cx()
    try:
        data = get_client_onboarding_status(client_id)
        status = data.get('status') or data.get('onboarding_status') or 'pending'
        c.execute(
            "UPDATE app_settings SET weinvoice_kyb_status=?,weinvoice_last_check_at=?,weinvoice_last_error=NULL WHERE id=1",
            (status, now())
        )
        c.commit()
        return True, f"Statut actualisé : {status}."
    except (WeInvoiceConfigError, WeInvoiceAPIError) as e:
        c.execute(
            "UPDATE app_settings SET weinvoice_last_error=?,weinvoice_last_check_at=? WHERE id=1",
            (str(e), now())
        )
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
    c = cx()
    try:
        c.execute(
            "UPDATE app_settings SET weinvoice_kyb_status=?,weinvoice_last_check_at=?,weinvoice_last_error=? WHERE id=1",
            (status, now(), reason or None)
        )
        c.commit()
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Vérification de signature webhook.
#
# ATTENTION — déduction, pas une certitude confirmée : les en-têtes exacts
# cités (webhook-id, webhook-timestamp, webhook-signature) correspondent au
# format standard de la librairie Svix, largement utilisée comme infrastructure
# de webhooks par de nombreux éditeurs. Le schéma ci-dessous suit les
# conventions Svix (contenu signé = "{id}.{timestamp}.{corps brut}", HMAC-SHA256
# avec le secret décodé en base64, signature encodée en base64, éventuellement
# préfixée "v1," et avec plusieurs signatures possibles séparées par des
# espaces pour la rotation de clé). CETTE HYPOTHÈSE DOIT ÊTRE CONFIRMÉE contre
# la vraie documentation de vérification de signature WeInvoice avant de faire
# confiance à cette fonction en production — une signature qui échouerait à
# tort bloquerait des webhooks légitimes.
# ---------------------------------------------------------------------------

class WeInvoiceWebhookError(Exception):
    """Levée quand la signature d'un webhook ne peut pas être vérifiée."""


def verify_webhook_signature(webhook_id, webhook_timestamp, raw_body, signature_header):
    """Vérifie la signature d'un webhook selon les conventions Svix. raw_body doit
    être les OCTETS BRUTS du corps HTTP (pas du JSON re-sérialisé — l'ordre des
    clés et les espaces changeraient la signature). Lève WeInvoiceWebhookError
    si la signature ne correspond à aucune des signatures fournies."""
    if not WEINVOICE_WEBHOOK_SECRET:
        raise WeInvoiceWebhookError(
            "WEINVOICE_WEBHOOK_SECRET n'est pas configuré côté serveur — impossible "
            "de vérifier la signature du webhook."
        )
    secret = WEINVOICE_WEBHOOK_SECRET
    if secret.startswith('whsec_'):
        secret = secret[len('whsec_'):]
    try:
        secret_bytes = base64.b64decode(secret)
    except Exception:
        secret_bytes = secret.encode('utf-8')

    signed_content = f"{webhook_id}.{webhook_timestamp}.".encode('utf-8') + raw_body
    expected = base64.b64encode(
        hmac.new(secret_bytes, signed_content, hashlib.sha256).digest()
    ).decode('utf-8')

    provided_signatures = [
        sig.split(',', 1)[1] if ',' in sig else sig
        for sig in signature_header.split()
    ]
    if not any(hmac.compare_digest(expected, sig) for sig in provided_signatures):
        raise WeInvoiceWebhookError("Signature webhook invalide.")

