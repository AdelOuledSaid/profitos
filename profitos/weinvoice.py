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
