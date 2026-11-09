"""Facturation électronique par SUPER PDP, branchée dans Veqalio (v495, étape 2).

- Raccordement d'une entreprise : parcours OAuth hébergé par SUPER PDP (``start_url`` puis
  ``finish``), jetons chiffrés par organisation dans ``superpdp_connections`` ;
- dépôt d'une facture émise (``send``) et suivi de son cycle de vie (``sync``) : les codes
  ``fr:2xx`` de la plateforme sont traduits dans les statuts que Veqalio utilise déjà
  (colonnes ``weinvoice_*`` de ``outgoing_invoices``, communes à toute plateforme agréée), si
  bien que l'affichage, la suspension des relances (refus, litige, suspension) et le journal
  ``einvoice_events`` fonctionnent sans changement ;
- ``sync_pending`` : interrogation périodique (SUPER PDP n'envoie pas de webhook).

Choix de la plateforme : ``EINVOICE_PROVIDER`` (``superpdp`` ou ``weinvoice``) ; par défaut
SUPER PDP dès que ses identifiants sont présents, sinon WeInvoice.
"""
import os
import secrets
import time

from profitos import superpdp as SP
from profitos.crypto import decrypt_secret, encrypt_secret

PROVIDER = 'superpdp'

SCHEMA = """CREATE TABLE IF NOT EXISTS superpdp_connections(
    entity_key INTEGER PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'disconnected',
    company_number TEXT,
    company_name TEXT,
    access_token TEXT,
    refresh_token TEXT,
    expires_at INTEGER,
    verified INTEGER NOT NULL DEFAULT 0,
    connected_at TEXT,
    last_check_at TEXT,
    last_error TEXT,
    inbound_cursor INTEGER
)"""

# Codes SUPER PDP → statuts déjà connus de Veqalio (affichage, suspension des relances).
STATUS_BY_CODE = {
    'fr:200': 'SUBMITTED', 'fr:201': 'SUBMITTED', 'fr:202': 'RECEIVED', 'fr:203': 'MADE_AVAILABLE',
    'fr:204': 'TAKEN_IN_CHARGE', 'fr:205': 'APPROVED', 'fr:206': 'PARTIALLY_APPROVED', 'fr:207': 'DISPUTED',
    'fr:208': 'SUSPENDED', 'fr:209': 'COMPLETED', 'fr:210': 'REFUSED', 'fr:211': 'PAYMENT_SENT',
    'fr:212': 'CASHED_IN', 'fr:213': 'REJECTED',
}
# Statuts après lesquels plus rien n'arrive : la facture n'est plus interrogée.
FINAL_STATUSES = {'REJECTED', 'REFUSED', 'CASHED_IN', 'COMPLETED'}


def provider():
    chosen = (os.environ.get('EINVOICE_PROVIDER') or '').strip().lower()
    if chosen in ('superpdp', 'weinvoice'):
        return chosen
    return 'superpdp' if SP.is_configured() else 'weinvoice'


def is_active():
    return provider() == PROVIDER


def _now():
    from profitos.runtime import now
    return now()


def ensure_table(c):
    c.execute(SCHEMA)


def _key(eid):
    return int(eid or 0)


def connection(c, eid):
    ensure_table(c)
    return c.execute('SELECT * FROM superpdp_connections WHERE entity_key=?', (_key(eid),)).fetchone()


def _save(c, eid, **values):
    ensure_table(c)
    if not c.execute('SELECT 1 FROM superpdp_connections WHERE entity_key=?', (_key(eid),)).fetchone():
        c.execute('INSERT INTO superpdp_connections(entity_key) VALUES(?)', (_key(eid),))
    cols = ', '.join(f'{k}=?' for k in values)
    c.execute(f'UPDATE superpdp_connections SET {cols} WHERE entity_key=?', (*values.values(), _key(eid)))


def _store_tokens(c, eid, org_id, tokens):
    _save(c, eid, access_token=encrypt_secret(tokens['access_token'], org_id),
          refresh_token=encrypt_secret(tokens.get('refresh_token'), org_id),
          expires_at=int(tokens.get('expires_at') or 0))


def client_for(c, eid, org_id, http=None):
    """Client SUPER PDP de l'entité. Chaque rotation du jeton est enregistrée et validée tout
    de suite : un jeton de rafraîchissement perdu obligerait l'entreprise à se relier."""
    row = connection(c, eid)
    if not row or not row['refresh_token']:
        raise SP.SuperPdpConfigError("Facture électronique non reliée : reliez l'entreprise à SUPER PDP "
                                     "depuis le profil entreprise.")
    tokens = {'access_token': decrypt_secret(row['access_token'], org_id),
              'refresh_token': decrypt_secret(row['refresh_token'], org_id),
              'expires_at': int(row['expires_at'] or 0)}

    def persist(new):
        _store_tokens(c, eid, org_id, new)
        c.commit()

    return SP.Client(tokens, on_refresh=persist, http=http)


# ------------------------------------------------------------------ raccordement

def start_url(session, siren=None, login_hint=None):
    """Prépare le parcours (état anti-rejeu + vérificateur PKCE gardés en session) et
    renvoie l'adresse SUPER PDP où envoyer le dirigeant."""
    verifier, challenge = SP.pkce_pair()
    state = secrets.token_urlsafe(24)
    session['superpdp_oauth'] = {'state': state, 'verifier': verifier, 'at': int(time.time())}
    return SP.authorize_url(state, challenge, siren=siren, login_hint=login_hint)


def finish(c, eid, org_id, session, state, code, http=None):
    """Retour du parcours : contrôle de l'état, échange du code, enregistrement chiffré des
    jetons, puis lecture du statut de vérification. Retourne la ligne de connexion."""
    pending = session.pop('superpdp_oauth', None) or {}
    if not state or not code or not secrets.compare_digest(str(pending.get('state') or ''), str(state)):
        raise SP.SuperPdpError("Retour SUPER PDP non reconnu (lien expiré ou déjà utilisé) : recommencez le raccordement.")
    if int(time.time()) - int(pending.get('at') or 0) > 3600:
        raise SP.SuperPdpError("Le raccordement a pris plus d'une heure : recommencez-le.")
    tokens = SP.exchange_code(code, pending['verifier'], http=http)
    _store_tokens(c, eid, org_id, tokens)
    _save(c, eid, status='connected', connected_at=_now(), last_error=None)
    c.commit()
    refresh_status(c, eid, org_id, http=http)
    return connection(c, eid)


def refresh_status(c, eid, org_id, http=None):
    """Relit la vérification KYC/KYB et l'entreprise reliée."""
    client = client_for(c, eid, org_id, http=http)
    try:
        verified = client.is_verified()
        company = client.company() if verified else {}
    except SP.SuperPdpError as exc:
        _save(c, eid, last_check_at=_now(), last_error=str(exc)[:500])
        c.commit()
        raise
    _save(c, eid, verified=1 if verified else 0, last_check_at=_now(), last_error=None,
          company_number=(company or {}).get('number'), company_name=(company or {}).get('name'))
    c.commit()
    return verified


def disconnect(c, eid):
    _save(c, eid, status='disconnected', access_token=None, refresh_token=None, expires_at=None, verified=0)
    c.commit()


# ------------------------------------------------------------------ émission et suivi

def _event(c, inv, event_type, *, remote_id=None, status=None, code=None, key=None, detail=None):
    # La contrainte UNIQUE ne déduplique pas quand entity_id est NULL (entité principale) :
    # on vérifie donc explicitement qu'un événement de même clé n'est pas déjà journalisé.
    if key and c.execute("""SELECT 1 FROM einvoice_events WHERE entity_id IS ? AND provider=? AND idempotency_key=?
                            AND event_type=?""", (inv['entity_id'], PROVIDER, key, event_type)).fetchone():
        return
    c.execute("""INSERT OR IGNORE INTO einvoice_events(entity_id,invoice_id,provider,event_type,remote_id,status,
                 regulatory_code,idempotency_key,detail,occurred_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
              (inv['entity_id'], inv['id'], PROVIDER, event_type, remote_id, status,
               code.split(':')[-1] if code else None, key, (detail or '')[:2000], _now()))


def send(c, inv, org_id, pdf_bytes, http=None):
    """Dépose le Factur-X d'une facture émise. Idempotent côté Veqalio : une facture qui a déjà
    un identifiant distant n'est jamais redéposée (contrôle fait par l'appelant et ici)."""
    if inv['weinvoice_invoice_id']:
        raise SP.SuperPdpError(f"Facture déjà transmise (identifiant {inv['weinvoice_invoice_id']}).")
    row = connection(c, inv['entity_id'])
    if not row or not row['verified']:
        raise SP.SuperPdpError("L'entreprise n'est pas encore vérifiée par SUPER PDP : terminez le raccordement "
                               "depuis le profil entreprise.")
    client = client_for(c, inv['entity_id'], org_id, http=http)
    key = f"superpdp-{org_id}-{inv['id']}"
    try:
        data = client.send_invoice(pdf_bytes, inv['invoice_number'] or str(inv['id']))
    except SP.SuperPdpError as exc:
        c.execute('UPDATE outgoing_invoices SET weinvoice_last_error=? WHERE id=?', (str(exc)[:1500], inv['id']))
        _event(c, inv, 'submission_failed', key=f'{key}-fail-{int(time.time())}', detail=str(exc))
        c.commit()
        raise
    remote_id = str((data or {}).get('id') or '')
    if not remote_id:
        raise SP.SuperPdpError("SUPER PDP a accepté le dépôt sans renvoyer d'identifiant : vérifiez avant tout nouvel envoi.")
    code, _label, _reason = SP.current_status(data)
    status = STATUS_BY_CODE.get(code, 'SUBMITTED')
    c.execute("""UPDATE outgoing_invoices SET weinvoice_invoice_id=?, weinvoice_status=?, weinvoice_regulatory_code=?,
                 weinvoice_sent_at=?, weinvoice_idempotency_key=?, weinvoice_last_error=NULL WHERE id=?""",
              (remote_id, status, code.split(':')[-1] if code else None, _now(), key, inv['id']))
    c.execute("UPDATE outgoing_invoices SET einvoice_provider=? WHERE id=?", (PROVIDER, inv['id']))
    _event(c, inv, 'submitted', remote_id=remote_id, status=status, code=code, key=key)
    c.commit()
    return remote_id, status


def sync(c, inv, org_id, http=None):
    """Relit le cycle de vie d'une facture déposée et journalise chaque nouvel événement.
    Retourne (statut, code, motif)."""
    remote_id = inv['weinvoice_invoice_id']
    if not remote_id:
        raise SP.SuperPdpError("Cette facture n'a pas encore été transmise.")
    data = client_for(c, inv['entity_id'], org_id, http=http).get_invoice(remote_id)
    for ev in SP.events(data):
        code = ev['status_code']
        _event(c, inv, 'status_sync', remote_id=remote_id, status=STATUS_BY_CODE.get(code, code), code=code,
               key=f"superpdp-ev-{remote_id}-{ev.get('id') or code}",
               detail=(ev.get('data') or {}).get('reason') if isinstance(ev.get('data'), dict) else None)
    code, _label, reason = SP.current_status(data)
    status = STATUS_BY_CODE.get(code, inv['weinvoice_status'] or 'SUBMITTED')
    c.execute("""UPDATE outgoing_invoices SET weinvoice_status=?, weinvoice_regulatory_code=?, weinvoice_last_sync_at=?,
                 weinvoice_last_error=? WHERE id=?""",
              (status, code.split(':')[-1] if code else None, _now(),
               (f'Rejetée par la plateforme : {reason}' if code == SP.REJECTED and reason else None), inv['id']))
    c.commit()
    return status, code, reason


def sync_pending(c, org_id, limit=200, http=None):
    """Tâche planifiée : relit les factures SUPER PDP dont le cycle de vie n'est pas terminé."""
    rows = c.execute(f"""SELECT * FROM outgoing_invoices WHERE einvoice_provider=? AND COALESCE(weinvoice_invoice_id,'')<>''
                         AND COALESCE(weinvoice_status,'') NOT IN ({','.join('?' * len(FINAL_STATUSES))})
                         ORDER BY weinvoice_sent_at LIMIT ?""", (PROVIDER, *sorted(FINAL_STATUSES), limit)).fetchall()
    done, errors = 0, []
    for inv in rows:
        try:
            sync(c, inv, org_id, http=http)
            done += 1
        except SP.SuperPdpError as exc:
            errors.append(f"{inv['invoice_number'] or inv['id']} : {exc}")
    return done, errors
