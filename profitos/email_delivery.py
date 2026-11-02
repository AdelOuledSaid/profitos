"""Suivi de la distribution des e-mails (v492) : événements Resend reçus par webhook.

Resend signale pour chaque message s'il a été remis, retardé, rejeté (bounce) ou signalé
comme spam (plainte). Sans ce retour, un e-mail envoyé passait pour un e-mail reçu : une
relance d'impayé partie vers une adresse morte comptait comme une relance faite.

- base AUTH : `email_messages` (identifiant Resend → organisation, pour router l'événement
  sans connaître la session), `email_suppressions` (adresses à ne plus écrire : rejet
  définitif ou plainte), `email_webhook_events` (événements déjà traités) ;
- base de l'entreprise : `email_deliveries` (destinataire, objet, document lié, statut).

Une adresse supprimée n'est plus utilisée pour les envois externes (facture, relance,
devis) ; les e-mails de compte (`kind='system'`) restent tentés. Mission Control signale
les adresses qui ne reçoivent plus rien tant qu'une facture impayée les utilise, et la page
« E-mails non distribués » permet de corriger l'adresse en une fois.

Signature des webhooks : format Svix utilisé par Resend (en-têtes svix-id, svix-timestamp,
svix-signature ; HMAC-SHA256 de « id.timestamp.corps » avec le secret `whsec_…`).
"""
import base64
import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timedelta, timezone

from profitos import verification as V

KIND = 'email_non_distribue'
TOLERANCE_SECONDS = 5 * 60
STATUS_RANK = {'sent': 0, 'delayed': 1, 'delivered': 2, 'bounced': 3, 'complained': 4}
STATUS_LABELS = {'sent': 'Envoyé', 'delayed': 'Distribution retardée', 'delivered': 'Remis',
                 'bounced': 'Non distribué (adresse rejetée)', 'complained': 'Signalé comme spam par le destinataire'}
EVENT_STATUS = {'email.sent': 'sent', 'email.delivered': 'delivered', 'email.delivery_delayed': 'delayed',
                'email.bounced': 'bounced', 'email.complained': 'complained', 'email.failed': 'bounced'}
REF_LABELS = {'outgoing_invoice': 'Facture', 'invoice_reminder': 'Relance', 'quote': 'Devis'}
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


class SignatureError(ValueError):
    pass


def norm(email):
    return (email or '').strip().lower()


def valid_email(email):
    return bool(_EMAIL_RE.match((email or '').strip())) and len(email) <= 254


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ----------------------------------------------------------------------- signature
def verify_signature(secret, headers, body, now=None):
    """Vérifie un webhook Resend (Svix). Lève SignatureError si invalide."""
    if not secret:
        raise SignatureError('secret absent')
    msg_id = headers.get('svix-id') or headers.get('webhook-id') or ''
    ts = headers.get('svix-timestamp') or headers.get('webhook-timestamp') or ''
    sigs = headers.get('svix-signature') or headers.get('webhook-signature') or ''
    if not (msg_id and ts and sigs):
        raise SignatureError('en-têtes manquants')
    try:
        ts_int = int(ts)
    except ValueError:
        raise SignatureError('horodatage invalide')
    if abs((now or time.time()) - ts_int) > TOLERANCE_SECONDS:
        raise SignatureError('horodatage hors délai')
    raw = secret.split('_', 1)[1] if secret.startswith('whsec_') else secret
    try:
        key = base64.b64decode(raw)
    except Exception:
        raise SignatureError('secret mal formé')
    if isinstance(body, str):
        body = body.encode('utf-8')
    expected = base64.b64encode(hmac.new(key, f'{msg_id}.{ts}.'.encode() + body, hashlib.sha256).digest()).decode()
    for part in sigs.split():
        version, _, sig = part.partition(',')
        if version == 'v1' and hmac.compare_digest(sig, expected):
            return msg_id
    raise SignatureError('signature invalide')


def sign(secret, msg_id, ts, body):
    """Signature au format Svix (tests et outil de diagnostic)."""
    raw = secret.split('_', 1)[1] if secret.startswith('whsec_') else secret
    if isinstance(body, str):
        body = body.encode('utf-8')
    return 'v1,' + base64.b64encode(hmac.new(base64.b64decode(raw), f'{msg_id}.{ts}.'.encode() + body,
                                             hashlib.sha256).digest()).decode()


# --------------------------------------------------------------------- base AUTH
def suppressed(ac, email):
    return ac.execute('SELECT * FROM email_suppressions WHERE email=?', (norm(email),)).fetchone()


def suppress(ac, email, reason, detail='', org_id=None):
    if suppressed(ac, email):
        return
    ac.execute('INSERT INTO email_suppressions(email,reason,detail,organization_id,created_at) VALUES(?,?,?,?,?)',
               (norm(email), reason, (detail or '')[:300], org_id, _now()))


def unsuppress(ac, email):
    ac.execute('DELETE FROM email_suppressions WHERE email=?', (norm(email),))


# ------------------------------------------------------------ base de l'entreprise
def ensure_table(c):
    c.execute('''CREATE TABLE IF NOT EXISTS email_deliveries(
        id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id TEXT UNIQUE, entity_id INTEGER, recipient TEXT NOT NULL,
        subject TEXT, ref_type TEXT, ref_id INTEGER, status TEXT NOT NULL DEFAULT 'sent', detail TEXT,
        sent_at TEXT NOT NULL, updated_at TEXT)''')
    c.commit()


def record_send(org_id, provider_id, recipient, subject, ref=None):
    """Enregistre un envoi Resend (appelé par send_email). Ne lève jamais : le suivi ne doit
    pas faire échouer un envoi réussi."""
    if not (org_id and provider_id):
        return
    from profitos import runtime as rt
    ref = ref or {}
    try:
        ac = rt.auth_cx()
        try:
            ac.execute('INSERT INTO email_messages(provider_id,organization_id,entity_id,created_at) VALUES(?,?,?,?)',
                       (str(provider_id), int(org_id), ref.get('entity_id'), _now()))
            ac.commit()
        finally:
            ac.close()
        c = rt.tenant_cx_direct(int(org_id))
        try:
            ensure_table(c)
            c.execute("""INSERT INTO email_deliveries(provider_id,entity_id,recipient,subject,ref_type,ref_id,status,sent_at,updated_at)
                         VALUES(?,?,?,?,?,?,'sent',?,?)""",
                      (str(provider_id), ref.get('entity_id'), norm(recipient), (subject or '')[:200], ref.get('type'),
                       ref.get('id'), _now(), _now()))
            c.commit()
        finally:
            c.close()
    except Exception:
        try:
            from flask import current_app
            current_app.logger.exception('Suivi e-mail non enregistré (%s)', provider_id)
        except Exception:
            pass


def apply_event(event, event_id=None):
    """Traite un événement Resend déjà authentifié. Retourne un court libellé de résultat."""
    from profitos import runtime as rt
    status = EVENT_STATUS.get((event or {}).get('type') or '')
    data = (event or {}).get('data') or {}
    provider_id = str(data.get('email_id') or '')
    if not status or not provider_id:
        return 'ignored'
    recipients = data.get('to') or []
    if isinstance(recipients, str):
        recipients = [recipients]
    bounce = data.get('bounce') or {}
    transient = str(bounce.get('type') or '').lower() == 'transient'
    if status == 'bounced' and transient:
        status = 'delayed'   # rejet temporaire (boîte pleine…) : pas de blocage
    detail = (bounce.get('message') or data.get('reason') or '')[:300] if status in ('bounced', 'delayed') else ''
    ac = rt.auth_cx()
    try:
        if event_id:
            if ac.execute('SELECT 1 FROM email_webhook_events WHERE event_id=?', (event_id,)).fetchone():
                return 'duplicate'
            ac.execute('INSERT INTO email_webhook_events(event_id,received_at) VALUES(?,?)', (event_id, _now()))
        msg = ac.execute('SELECT * FROM email_messages WHERE provider_id=?', (provider_id,)).fetchone()
        org_id = msg['organization_id'] if msg else None
        if status in ('bounced', 'complained'):
            for r in recipients:
                suppress(ac, r, 'plainte' if status == 'complained' else 'rejet', detail, org_id)
        ac.commit()
    finally:
        ac.close()
    if not org_id:
        return 'unknown_message'
    c = rt.tenant_cx_direct(int(org_id))
    try:
        ensure_table(c)
        row = c.execute('SELECT status FROM email_deliveries WHERE provider_id=?', (provider_id,)).fetchone()
        if not row:
            return 'unknown_message'
        if STATUS_RANK[status] >= STATUS_RANK.get(row['status'], 0):
            c.execute('UPDATE email_deliveries SET status=?,detail=?,updated_at=? WHERE provider_id=?',
                      (status, detail or None, _now(), provider_id))
            c.commit()
            return status
        return 'kept'
    finally:
        c.close()


def latest_for(c, ref_type, ref_id, eid):
    try:
        ensure_table(c)
        return c.execute("""SELECT * FROM email_deliveries WHERE ref_type=? AND ref_id=? AND entity_id IS ?
                            ORDER BY id DESC LIMIT 1""", (ref_type, ref_id, eid)).fetchone()
    except Exception:
        return None


def latest_for_invoice(c, invoice_id, eid):
    """Dernier e-mail (facture ou relance) envoyé pour une facture, avec son statut."""
    try:
        ensure_table(c)
        r = c.execute("""SELECT * FROM email_deliveries WHERE ref_type IN ('outgoing_invoice','invoice_reminder')
                         AND ref_id=? AND entity_id IS ? ORDER BY id DESC LIMIT 1""", (invoice_id, eid)).fetchone()
    except Exception:
        return None
    if not r:
        return None
    d = dict(r)
    d['label'] = STATUS_LABELS.get(d['status'], d['status'])
    d['kind_label'] = REF_LABELS.get(d['ref_type'], 'E-mail')
    d['failed'] = d['status'] in ('bounced', 'complained')
    return d


def failing(c, eid, since_days=120, today=None):
    """Adresses dont le dernier message est rejeté ou signalé, encore utilisées par une
    facture non réglée de l'entité."""
    ensure_table(c)
    today = today or datetime.now(timezone.utc).date()
    since = (today - timedelta(days=since_days)).isoformat()
    rows = c.execute("""SELECT d.* FROM email_deliveries d WHERE d.entity_id IS ? AND d.sent_at>=?
                        AND d.id=(SELECT MAX(x.id) FROM email_deliveries x WHERE x.recipient=d.recipient AND x.entity_id IS ?)
                        AND d.status IN ('bounced','complained') ORDER BY d.updated_at DESC""",
                     (eid, since, eid)).fetchall()
    out = []
    for r in rows:
        invoices = c.execute("""SELECT id,invoice_number,client_name,total FROM outgoing_invoices WHERE entity_id IS ?
                                AND lower(trim(client_email))=? AND status NOT IN ('paid','cancelled','draft')
                                ORDER BY due_date""", (eid, r['recipient'])).fetchall()
        if invoices:
            out.append({'recipient': r['recipient'], 'status': r['status'], 'label': STATUS_LABELS[r['status']],
                        'detail': r['detail'] or '', 'subject': r['subject'], 'at': r['updated_at'] or r['sent_at'],
                        'invoices': [dict(i) for i in invoices], 'client': invoices[0]['client_name']})
    return out


def correct_address(c, eid, old, new, user=None):
    """Remplace une adresse rejetée par la nouvelle sur les factures non réglées et la fiche
    client de l'entité. Retourne (factures, fiches) modifiées."""
    old, new = norm(old), (new or '').strip()
    if not valid_email(new):
        raise ValueError("Adresse e-mail invalide.")
    if norm(new) == old:
        raise ValueError("La nouvelle adresse est identique à l'ancienne.")
    n_inv = c.execute("""UPDATE outgoing_invoices SET client_email=? WHERE entity_id IS ? AND lower(trim(client_email))=?
                         AND status NOT IN ('paid','cancelled')""", (new, eid, old)).rowcount
    try:
        n_cli = c.execute('UPDATE invoicing_clients SET email=? WHERE entity_id IS ? AND lower(trim(email))=?',
                          (new, eid, old)).rowcount
    except Exception:
        n_cli = 0
    c.commit()
    return n_inv or 0, n_cli or 0


# --------------------------------------------------------------- Mission Control
def detect(c, eid, today):
    out = []
    for f in failing(c, eid, today=today):
        total = sum(float(i['total'] or 0) for i in f['invoices'])
        nums = ', '.join(i['invoice_number'] or str(i['id']) for i in f['invoices'][:4])
        out.append(V.finding(
            f"{KIND}:{f['recipient']}", 'mission_control', KIND, 'important',
            f"{f['client']} ne reçoit plus vos e-mails", amount=total, amount_label='Factures non réglées concernées TTC',
            detail=f"Dernier envoi : « {f['subject'] or '—'} ». Les relances vers cette adresse sont suspendues.",
            checks=[V.check('Adresse e-mail joignable', False, f"{f['label']}" + (f" — {f['detail']}" if f['detail'] else ''),
                            'Retour de distribution Resend', "Corrigez l'adresse : les factures non réglées et la fiche client "
                            "seront mises à jour, puis renvoyez la facture.")],
            evidence=[V.evidence('Adresse', f['recipient'], 'Retour de distribution Resend', as_of=f['at']),
                      V.evidence('Factures concernées', nums, 'Facturation')],
            action={'kind': 'link', 'label': "Corriger l'adresse", 'endpoint': 'email_failures'},
            follow_up_days=3, ref={'recipient': f['recipient']}))
    return out
