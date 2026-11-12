"""Client de la plateforme agréée SUPER PDP (v495, en remplacement de WeInvoice).

API publique ``https://api.superpdp.tech``, version ``v1.beta``. Ce module ne contient que
le dialogue avec la plateforme ; le branchement dans la facturation se fait ailleurs.

Modèle « marque grise » retenu :
- Veqalio possède UNE application OAuth chez SUPER PDP (SUPERPDP_CLIENT_ID / _SECRET) ;
- chaque entreprise cliente la relie une fois, par le parcours OAuth 2.1 ``authorization_code``
  avec PKCE : elle passe le tunnel KYC/KYB et le consentement hébergés par SUPER PDP, puis
  revient sur Veqalio avec un code échangé contre un jeu de jetons propre à cette entreprise ;
- le jeton d'accès vit environ 30 minutes ; le jeton de rafraîchissement est long mais
  TOURNE à chaque rafraîchissement : le nouveau doit être enregistré à chaque fois, sinon la
  connexion est perdue (``on_refresh``) ;
- tant que l'entreprise n'est pas vérifiée (``company_verification_status`` différent de
  ``verified``), l'API répond 403 sur les routes métier.

Pas de webhook dans le contrat d'API à ce jour : statuts et factures reçues se lisent par
interrogation périodique (tâche planifiée). Le cycle de vie d'une facture se trouve dans
``events[]`` (et nulle part ailleurs) ; ``fr:202`` (reçue par la plateforme du destinataire)
et ``fr:213`` (rejetée) terminent un dépôt.

Références : client Ruby ``lgaroche/super_pdp`` (validé sur le bac à sable) et intégration
``invoicerr`` (vérifiée en conditions réelles), lus le 09/11/2026.
"""
import base64
import hashlib
import os
import re
import secrets
import time
from urllib.parse import urlencode

import requests

API_VERSION = 'v1.beta'
DEFAULT_BASE_URL = 'https://api.superpdp.tech'
TIMEOUT = 30
EXPIRY_SKEW = 60          # secondes : on rafraîchit un peu avant l'expiration

# Codes du cycle de vie (norme AFNOR XP Z12-012 / spécifications DGFiP), tels que SUPER PDP les
# renvoie dans events[].status_code.
STATUS_LABELS = {
    'fr:200': 'Déposée',
    'fr:201': 'Émise par la plateforme',
    'fr:202': 'Reçue par la plateforme du destinataire',
    'fr:203': 'Mise à disposition',
    'fr:204': 'Prise en charge',
    'fr:205': 'Approuvée',
    'fr:206': 'Approuvée partiellement',
    'fr:207': 'En litige',
    'fr:208': 'Suspendue',
    'fr:209': 'Complétée',
    'fr:210': 'Refusée',
    'fr:211': 'Paiement transmis',
    'fr:212': 'Encaissée',
    'fr:213': 'Rejetée',
}
ACCEPTED = 'fr:202'
REJECTED = 'fr:213'
TERMINAL = {ACCEPTED, REJECTED}
# Codes qu'un destinataire (ou l'émetteur, pour fr:211/fr:212) peut pousser lui-même.
PUSHABLE = {'fr:204', 'fr:205', 'fr:206', 'fr:207', 'fr:208', 'fr:209', 'fr:210', 'fr:211', 'fr:212'}


class SuperPdpError(Exception):
    """Erreur de dialogue avec SUPER PDP ; ``status`` vaut le code HTTP s'il y en a un."""

    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


class SuperPdpConfigError(SuperPdpError):
    pass


class NotVerifiedError(SuperPdpError):
    """403 : l'entreprise n'a pas terminé la vérification KYC/KYB chez SUPER PDP."""


def base_url():
    return (os.environ.get('SUPERPDP_BASE_URL') or DEFAULT_BASE_URL).rstrip('/')


def is_configured():
    return bool(os.environ.get('SUPERPDP_CLIENT_ID') and os.environ.get('SUPERPDP_CLIENT_SECRET'))


def redirect_uri():
    explicit = (os.environ.get('SUPERPDP_REDIRECT_URI') or '').strip()
    if explicit:
        return explicit
    base = (os.environ.get('APP_BASE_URL') or '').strip().rstrip('/')
    return f'{base}/facture-electronique/superpdp/retour' if base else ''


def _credentials():
    if not is_configured():
        raise SuperPdpConfigError("SUPERPDP_CLIENT_ID et SUPERPDP_CLIENT_SECRET doivent être définis.")
    return os.environ['SUPERPDP_CLIENT_ID'], os.environ['SUPERPDP_CLIENT_SECRET']


# ------------------------------------------------------------------ OAuth 2.1 + PKCE

def pkce_pair():
    """(vérificateur, défi S256). Le vérificateur reste côté serveur (session), seul le défi
    part chez SUPER PDP."""
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    return verifier, challenge


def company_number():
    """Numéro d'entreprise imposé à toutes les connexions (v497.1). Sert au bac à sable, où
    l'application est restreinte à une entreprise de test dont le numéro (000000001) n'est pas
    un SIREN valide : il ne peut donc pas venir du profil entreprise."""
    return re.sub(r'\D', '', os.environ.get('SUPERPDP_COMPANY_NUMBER') or '') or None


def company_number_scheme(number):
    """Format du numéro d'entreprise attendu par SUPER PDP : ``fr_siren`` pour un vrai SIREN,
    ``sandbox`` pour les entreprises de test du bac à sable, dont le numéro (000000001) n'a pas
    de clé de contrôle valide. SUPERPDP_COMPANY_NUMBER_SCHEME permet de l'imposer."""
    from profitos.fr_identifiers import siren_is_valid
    forced = (os.environ.get('SUPERPDP_COMPANY_NUMBER_SCHEME') or '').strip().lower()
    if forced:
        return forced
    return 'fr_siren' if siren_is_valid(number) else 'sandbox'


def authorize_url(state, code_challenge, siren=None, login_hint=None):
    """Adresse où envoyer le dirigeant : tunnel KYC/KYB et consentement hébergés par SUPER PDP.
    Le numéro d'entreprise ne fait que pré-remplir le formulaire ; sans lui, SUPER PDP le demande."""
    siren = company_number() or siren
    client_id, _secret = _credentials()
    params = {'response_type': 'code', 'client_id': client_id, 'redirect_uri': redirect_uri(),
              'code_challenge': code_challenge, 'code_challenge_method': 'S256', 'state': state}
    if siren:
        params['superpdp_company_number'] = siren
        params['superpdp_company_number_scheme'] = company_number_scheme(siren)
    if login_hint:
        params['login_hint'] = login_hint
    return f'{base_url()}/oauth2/authorize?{urlencode(params)}'


def _token_request(form, http=None):
    http = http or requests
    try:
        resp = http.post(f'{base_url()}/oauth2/token', data=form, headers={'Accept': 'application/json'},
                         timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise SuperPdpError(f'SUPER PDP injoignable : {exc}') from exc
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code != 200 or not body.get('access_token'):
        desc = body.get('error_description') or body.get('error') or f'HTTP {resp.status_code}'
        raise SuperPdpError(f'Authentification SUPER PDP refusée : {desc}', resp.status_code, body)
    return body


def _tokens(body, previous_refresh=None):
    expires_in = int(body.get('expires_in') or 1800)
    return {'access_token': body['access_token'],
            # rotation : on garde l'ancien seulement si le serveur n'en renvoie pas de nouveau
            'refresh_token': body.get('refresh_token') or previous_refresh,
            'expires_at': int(time.time()) + expires_in}


def exchange_code(code, code_verifier, http=None):
    client_id, client_secret = _credentials()
    return _tokens(_token_request({'grant_type': 'authorization_code', 'code': code,
                                   'redirect_uri': redirect_uri(), 'code_verifier': code_verifier,
                                   'client_id': client_id, 'client_secret': client_secret}, http))


def refresh(refresh_token, http=None):
    client_id, client_secret = _credentials()
    return _tokens(_token_request({'grant_type': 'refresh_token', 'refresh_token': refresh_token,
                                   'client_id': client_id, 'client_secret': client_secret}, http),
                   previous_refresh=refresh_token)


# ------------------------------------------------------------------ client d'une entreprise

class Client:
    """Client agissant pour UNE entreprise reliée. ``tokens`` : dict access_token /
    refresh_token / expires_at ; ``on_refresh(tokens)`` est appelé à chaque rotation et DOIT
    enregistrer le nouveau jeu (sinon la connexion est perdue au prochain rafraîchissement)."""

    def __init__(self, tokens, on_refresh=None, http=None):
        self.tokens = dict(tokens)
        self.on_refresh = on_refresh
        self.http = http or requests

    # -- jetons
    def _refresh(self):
        if not self.tokens.get('refresh_token'):
            raise SuperPdpError("Connexion SUPER PDP expirée : reliez à nouveau l'entreprise.", 401)
        self.tokens = refresh(self.tokens['refresh_token'], http=self.http)
        if self.on_refresh:
            self.on_refresh(dict(self.tokens))

    def _access_token(self):
        if int(self.tokens.get('expires_at') or 0) - EXPIRY_SKEW <= time.time():
            self._refresh()
        return self.tokens['access_token']

    # -- transport
    def request(self, method, path, *, params=None, json=None, data=None, content_type=None,
                raw=False, _retried=False):
        url = path if path.startswith('http') else f"{base_url()}/{API_VERSION}/{path.lstrip('/')}"
        headers = {'Authorization': f'Bearer {self._access_token()}', 'Accept': 'application/json'}
        if content_type:
            headers['Content-Type'] = content_type
        try:
            resp = self.http.request(method, url, params={k: v for k, v in (params or {}).items() if v is not None},
                                     json=json, data=data, headers=headers, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise SuperPdpError(f'SUPER PDP injoignable : {exc}') from exc
        if resp.status_code == 401 and not _retried:      # jeton révoqué ou expiré plus tôt que prévu
            self._refresh()
            return self.request(method, path, params=params, json=json, data=data,
                                content_type=content_type, raw=raw, _retried=True)
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = resp.text[:500]
            msg = (body.get('message') or body.get('error') or body.get('errorMessage')
                   if isinstance(body, dict) else None) or f'HTTP {resp.status_code}'
            cls = NotVerifiedError if resp.status_code == 403 else SuperPdpError
            raise cls(f'SUPER PDP : {msg}', resp.status_code, body)
        if raw:
            return resp.content, resp.headers.get('content-type', 'application/octet-stream')
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    # -- entreprise
    def session(self):
        """Session OAuth : ``company_verification_status`` vaut ``verified`` quand l'entreprise
        peut émettre et recevoir."""
        return self.request('GET', 'oauth2_sessions/me')

    def is_verified(self):
        return (self.session() or {}).get('company_verification_status') == 'verified'

    def company(self):
        return self.request('GET', 'companies/me')

    def update_vat_regime(self, regime, has_vat_on_debits=None):
        """``monthly``, ``quarterly``, ``simplified`` ou ``vat_exemption`` : fixe le calendrier
        selon lequel la plateforme transmet l'e-reporting à l'administration."""
        body = {'vat_regime': regime}
        if has_vat_on_debits is not None:
            body['has_vat_on_debits'] = bool(has_vat_on_debits)
        return self.request('PATCH', 'companies', json=body)

    # -- e-reporting (ventes aux particuliers)
    def create_b2c_transactions(self, transactions):
        """Déclare des ventes aux particuliers. La plateforme les enregistre aussitôt, puis les
        agrège et les transmet à l'administration selon le régime de TVA. L'API n'a pas de clé
        d'idempotence : l'appelant ne doit jamais renvoyer une vente déjà acceptée."""
        body = self.request('POST', 'b2c_transactions', json={'data': list(transactions)})
        return (body or {}).get('data') or [] if isinstance(body, dict) else (body or [])

    def create_b2c_payments(self, payments):
        """Déclare des encaissements de ventes aux particuliers (prestations de services)."""
        body = self.request('POST', 'b2c_payments', json={'data': list(payments)})
        return (body or {}).get('data') or [] if isinstance(body, dict) else (body or [])

    def ereportings(self, starting_after_id=None, limit=50):
        """Déclarations agrégées réellement transmises à l'administration, avec leur statut."""
        return self.request('GET', 'ereportings', params={'starting_after_id': starting_after_id, 'limit': limit,
                                                           'order': 'desc'})

    # -- émission
    def send_invoice(self, content, external_id, content_type='application/pdf'):
        """Dépose une facture Factur-X (PDF) ou UBL/CII (XML). ``external_id`` : numéro Veqalio,
        qui rend le dépôt identifiable côté plateforme."""
        return self.request('POST', 'invoices', params={'external_id': external_id},
                            data=content, content_type=content_type)

    def get_invoice(self, invoice_id):
        return self.request('GET', f'invoices/{int(invoice_id)}')

    def download(self, invoice_id, fmt='original'):
        """Octets du fichier (``original`` : exactement ce qui a été déposé) et son type."""
        return self.request('GET', f'invoices/{int(invoice_id)}', params={'format': fmt}, raw=True)

    def list_invoices(self, direction=None, starting_after_id=None, limit=50, expand=None):
        return self.request('GET', 'invoices', params={'direction': direction, 'limit': limit,
                                                        'starting_after_id': starting_after_id,
                                                        'order': 'asc', 'expand[]': expand})

    def received(self, starting_after_id=None, limit=50, expand='en_invoice'):
        """Factures reçues, avec leur contenu structuré EN 16931 (``en_invoice``)."""
        return self.list_invoices('in', starting_after_id, limit, expand)

    def push_status(self, invoice_id, status_code, reason_code=None, notes=None):
        """Statut du cycle de vie poussé par Veqalio (prise en charge, approbation, refus,
        paiement…). ``reason_code`` est un code motif réglementaire (liste AFNOR XP Z12-012,
        par exemple ``NON_CONFORME`` pour un refus) : un texte libre y est refusé par l'API.
        La phrase de l'utilisateur voyage dans ``notes``."""
        if status_code not in PUSHABLE:
            raise ValueError(f'statut non transmissible : {status_code}')
        body = {'invoice_id': int(invoice_id), 'status_code': status_code}
        detail = {k: v for k, v in (('reason', reason_code), ('notes', (notes or '').strip()[:2000] or None)) if v}
        if detail:
            body['details'] = [detail]
        return self.request('POST', 'invoice_events', json=body)

    # -- annuaire
    def directory(self, siren):
        """Entrées de l'annuaire national pour un SIREN : vide si l'entreprise ne peut pas
        encore recevoir de factures électroniques."""
        body = self.request('GET', 'french_directory/entries', params={'number': siren})
        return (body or {}).get('data', []) if isinstance(body, dict) else (body or [])


# ------------------------------------------------------------------ lecture du cycle de vie

def events(invoice):
    """Événements datés d'une facture, du plus ancien au plus récent."""
    out = [e for e in (invoice or {}).get('events') or [] if isinstance(e, dict) and e.get('status_code')]
    return sorted(out, key=lambda e: (e.get('created_at') or '', e.get('id') or 0))


def current_status(invoice):
    """(code, libellé, motif) du dernier événement, ou (None, 'En attente', None)."""
    evs = events(invoice)
    if not evs:
        return None, 'En attente de traitement', None
    last = evs[-1]
    code = last['status_code']
    reason = (last.get('data') or {}).get('reason') if isinstance(last.get('data'), dict) else None
    return code, last.get('status_text') or STATUS_LABELS.get(code, code), reason


def is_terminal(code):
    return code in TERMINAL
