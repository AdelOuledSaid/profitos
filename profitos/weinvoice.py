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

from profitos.runtime import (
    WEINVOICE_BASE_URL, WEINVOICE_CLIENT_ID, WEINVOICE_CLIENT_SECRET, WEINVOICE_ENV,
    cx, now,
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
# Lot 23.2 — onboarding entreprise (KYB) auprès de WeInvoice.
#
# ATTENTION — hypothèse non confirmée : contrairement à l'endpoint OAuth
# (POST /v1/oauth/token, explicitement confirmé par la documentation), aucun
# endpoint précis d'onboarding entreprise ne m'a été communiqué. Le chemin
# POST /v1/companies ci-dessous suit une convention REST standard mais N'A PAS
# été vérifié contre la vraie documentation WeInvoice. À confirmer avant le
# premier vrai test (voir message d'erreur si le endpoint renvoie 404).
# ---------------------------------------------------------------------------

def onboard_company(company_row):
    """Envoie les informations de l'entreprise à WeInvoice pour l'onboarding/KYB
    et retourne le JSON de réponse (contenant a priori un identifiant externe et
    un statut KYB). Lève WeInvoiceConfigError/WeInvoiceAPIError en cas d'échec."""
    token = fetch_access_token()
    url = f"{WEINVOICE_BASE_URL}/v1/companies"
    payload = {
        'name': company_row['name'] or '',
        'siret': (company_row['siret'] or '').replace(' ', ''),
        'address': company_row['address'] or '',
        'vat_number': company_row['vat_number'] or '',
    }
    headers = {'Authorization': f'Bearer {token}'}
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
    except requests.RequestException as e:
        raise WeInvoiceAPIError(f"Connexion à {WEINVOICE_BASE_URL} impossible : {e}") from e

    if resp.status_code == 404:
        raise WeInvoiceAPIError(
            "Endpoint /v1/companies introuvable (404) — l'URL d'onboarding réelle "
            "diffère probablement de celle supposée. Vérifie la documentation WeInvoice "
            "pour le bon chemin et communique-le pour correction."
        )
    if resp.status_code not in (200, 201):
        raise WeInvoiceAPIError(f"L'API WeInvoice a répondu {resp.status_code} lors de l'onboarding.")
    try:
        return resp.json()
    except ValueError as e:
        raise WeInvoiceAPIError("Réponse WeInvoice illisible (pas du JSON valide) lors de l'onboarding.") from e


def onboard_company_and_store_status(company_row):
    """Tente l'onboarding et enregistre le résultat dans app_settings (identifiant
    externe + statut KYB + horodatage). Retourne (ok: bool, message: str)."""
    c = cx()
    try:
        data = onboard_company(company_row)
        external_id = data.get('id') or data.get('company_id') or ''
        kyb_status = data.get('kyb_status') or data.get('status') or 'pending'
        c.execute(
            "UPDATE app_settings SET weinvoice_company_id=?,weinvoice_kyb_status=?,weinvoice_onboarded_at=?,weinvoice_last_error=NULL WHERE id=1",
            (external_id, kyb_status, now())
        )
        c.commit()
        return True, f"Entreprise envoyée à WeInvoice — identifiant {external_id or '(non renvoyé)'}, statut KYB : {kyb_status}."
    except (WeInvoiceConfigError, WeInvoiceAPIError) as e:
        c.execute(
            "UPDATE app_settings SET weinvoice_last_error=?,weinvoice_last_check_at=? WHERE id=1",
            (str(e), now())
        )
        c.commit()
        return False, str(e)
    finally:
        c.close()
